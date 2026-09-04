from __future__ import annotations

import json
import platform
import resource
import sys
import time
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audio_features import measure_audio
from .readiness import evaluate_project_readiness
from .run_lifecycle import read_analysis_run, start_analysis_run
from .safe_io import atomic_output_path, atomic_write_text
from .schemas import Shot, StatusEnvelope, load_json
from .utils import require_tool, run_command

JsonDict = dict[str, Any]
BENCHMARK_SCHEMA_VERSION = 1
BOUNDARY_TOLERANCE_SECONDS = 0.6
MIN_BOUNDARY_PRECISION = 0.8
MIN_BOUNDARY_RECALL = 0.5
MAX_CASE_ELAPSED_SECONDS = 60.0
MAX_BENCHMARK_ELAPSED_SECONDS = 240.0
MAX_SELF_PEAK_RSS_BYTES = 1536 * 1024 * 1024
AUDIO_ONSET_TOLERANCE_SECONDS = 0.021
AUDIO_BPM_TOLERANCE = 1.0
AUDIO_RMS_TOLERANCE = 0.0001
MAX_AUDIO_BENCHMARK_SECONDS = 5.0
REQUIRED_ARTIFACTS = (
    "project_manifest.json",
    "data/media_package.json",
    "data/visual_generation.json",
    "data/audio_generation.json",
    "data/shots.json",
    "reports/report.html",
    "reports/codex_handoff.md",
    "data/visualization_dataset.json",
)


def benchmark_case_definitions() -> list[JsonDict]:
    """Return the stable six-case, synthetic-only benchmark contract."""
    return [
        {
            "id": "hard-cuts-landscape",
            "category": "hard_cuts",
            "description": "Three high-contrast landscape plates with two hard cuts.",
            "duration_seconds": 3.6,
            "expected_boundaries": [1.2, 2.4],
        },
        {
            "id": "fade-dissolve",
            "category": "fade_dissolve",
            "description": "Two generated patterns joined by a 0.6 second crossfade.",
            "duration_seconds": 3.4,
            "expected_boundaries": [1.7],
        },
        {
            "id": "animation-pattern",
            "category": "animation",
            "description": "Continuously animated test pattern with no editorial cut.",
            "duration_seconds": 3.0,
            "expected_boundaries": [],
        },
        {
            "id": "vertical-hard-cut",
            "category": "vertical_video",
            "description": "Portrait-format plates with one hard cut.",
            "duration_seconds": 3.0,
            "expected_boundaries": [1.5],
        },
        {
            "id": "variable-frame-rate",
            "category": "variable_frame_rate",
            "description": "Generated motion with deliberately irregular retained frame timestamps.",
            "duration_seconds": 3.0,
            "expected_boundaries": [],
        },
        {
            "id": "audio-dense-surrogate",
            "category": "speech_heavy_surrogate",
            "description": "No-cut video with dense synthetic audio; this does not measure ASR accuracy.",
            "duration_seconds": 3.0,
            "expected_boundaries": [],
        },
    ]


def run_synthetic_benchmark(output: str | Path) -> StatusEnvelope:
    output_root = Path(output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    fixtures_root = output_root / "fixtures"
    workspace = output_root / "workspace"
    fixtures_root.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    benchmark_id = f"vew-benchmark-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    cases = benchmark_case_definitions()
    started = time.monotonic()
    results: list[JsonDict] = []
    for case in cases:
        fixture = fixtures_root / f"{case['id']}.mp4"
        _generate_fixture(case, fixture)
        project_id = f"{case['id']}-{uuid.uuid4().hex[:6]}"
        run = start_analysis_run(
            workspace,
            {
                "source": str(fixture),
                "project_id": project_id,
                "profile": "research",
                "language": "auto",
                "delivery_language": "en",
                "skip_asr": True,
                "max_duration_seconds": 30.0,
            },
        )
        terminal = _wait_for_run(workspace, str(run["run_id"]))
        results.append(_case_result(case, fixture, workspace, terminal))

    audio_quality = run_audio_quality_benchmark(fixtures_root)
    elapsed = round(time.monotonic() - started, 3)
    visual_passed, summary = benchmark_summary(results)
    peak_rss = _self_peak_rss_bytes()
    overall_performance = {
        "elapsed_seconds": elapsed,
        "maximum_elapsed_seconds": MAX_BENCHMARK_ELAPSED_SECONDS,
        "self_peak_rss_bytes": peak_rss,
        "maximum_self_peak_rss_bytes": MAX_SELF_PEAK_RSS_BYTES,
        "passed": elapsed <= MAX_BENCHMARK_ELAPSED_SECONDS and peak_rss <= MAX_SELF_PEAK_RSS_BYTES,
        "scope": "current Python process only; child-process RSS is not inferred",
    }
    passed = visual_passed and audio_quality["passed"] and overall_performance["passed"]
    receipt: JsonDict = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": benchmark_id,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "passed" if passed else "failed",
        "scope": {
            "fixtures": "deterministically generated from FFmpeg filters; no external media assets",
            "redistribution": "CC0-1.0",
            "asr_accuracy": "not_run; no redistributable speech recording or explicit local model configured",
            "semantic_voice_music_sfx_accuracy": "not_run; deterministic baseline preserves unknown identity",
            "external_vision": "not invoked",
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "ffmpeg": _ffmpeg_version(),
        },
        "thresholds": {
            "boundary_tolerance_seconds": BOUNDARY_TOLERANCE_SECONDS,
            "required_artifacts": list(REQUIRED_ARTIFACTS),
            "accuracy_gated_categories": [
                "hard_cuts",
                "animation",
                "vertical_video",
                "variable_frame_rate",
                "speech_heavy_surrogate",
            ],
            "observational_categories": ["fade_dissolve"],
            "boundary_minimum_precision": MIN_BOUNDARY_PRECISION,
            "boundary_minimum_recall": MIN_BOUNDARY_RECALL,
            "no_cut_maximum_predicted_boundaries": 0,
            "pre_review_readiness_must_be_blocked": True,
            "case_maximum_elapsed_seconds": MAX_CASE_ELAPSED_SECONDS,
            "benchmark_maximum_elapsed_seconds": MAX_BENCHMARK_ELAPSED_SECONDS,
            "self_peak_rss_maximum_bytes": MAX_SELF_PEAK_RSS_BYTES,
            "audio_onset_tolerance_seconds": AUDIO_ONSET_TOLERANCE_SECONDS,
            "audio_bpm_tolerance": AUDIO_BPM_TOLERANCE,
            "audio_rms_tolerance": AUDIO_RMS_TOLERANCE,
            "audio_benchmark_maximum_elapsed_seconds": MAX_AUDIO_BENCHMARK_SECONDS,
        },
        "elapsed_seconds": elapsed,
        "performance": overall_performance,
        "cases": results,
        "audio_quality": audio_quality,
        "summary": summary,
    }
    receipt_path = output_root / "benchmark-receipt.json"
    atomic_write_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2), root=output_root)
    status = "success" if passed else "error"
    return StatusEnvelope(
        status=status,
        summary=(
            f"Synthetic benchmark {receipt['status']}: "
            f"{summary['functional_passed']}/{len(results)} functional, "
            f"{summary['accuracy_gated_passed']}/{summary['accuracy_gated']} accuracy-gated; "
            f"{summary['observational']} observational; "
            f"audio deterministic {audio_quality['passed_case_count']}/{audio_quality['case_count']}."
        ),
        next_actions=(
            ["Inspect failed case receipts before changing detector thresholds."]
            if not passed
            else ["Keep the receipt with the exact candidate revision when evaluating release readiness."]
        ),
        artifacts={"benchmark_receipt": str(receipt_path), "benchmark_workspace": str(workspace)},
        error=None if passed else "One or more synthetic benchmark gates failed.",
    )


def boundary_metrics(expected: list[float], predicted: list[float], tolerance: float = BOUNDARY_TOLERANCE_SECONDS) -> JsonDict:
    unmatched = list(predicted)
    matched = 0
    errors: list[float] = []
    for boundary in expected:
        candidates = [
            (abs(candidate - boundary), candidate)
            for candidate in unmatched
            if abs(candidate - boundary) <= tolerance + 1e-9
        ]
        if not candidates:
            continue
        error, candidate = min(candidates)
        unmatched.remove(candidate)
        matched += 1
        errors.append(round(error, 3))
    precision = matched / len(predicted) if predicted else (1.0 if not expected else 0.0)
    recall = matched / len(expected) if expected else 1.0
    return {
        "expected": expected,
        "predicted": predicted,
        "matched": matched,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0.0,
        "mean_absolute_error_seconds": round(sum(errors) / len(errors), 3) if errors else None,
    }


def benchmark_summary(results: list[JsonDict]) -> tuple[bool, JsonDict]:
    functional_passed = sum(1 for item in results if item.get("functional_passed") is True)
    accuracy_gated = [item for item in results if item.get("quality_gate", {}).get("mode") == "accuracy"]
    accuracy_gated_passed = sum(1 for item in accuracy_gated if item.get("passed") is True)
    observational = sum(1 for item in results if item.get("quality_gate", {}).get("mode") == "observational")
    performance_passed = sum(
        1 for item in results if item.get("performance_gate", {}).get("passed") is True
    )
    summary = {
        "case_count": len(results),
        "functional_passed": functional_passed,
        "functional_failed": len(results) - functional_passed,
        "accuracy_gated": len(accuracy_gated),
        "accuracy_gated_passed": accuracy_gated_passed,
        "observational": observational,
        "performance_passed": performance_passed,
        "failed": sum(1 for item in results if item.get("status") == "failed"),
    }
    passed = (
        functional_passed == len(results)
        and accuracy_gated_passed == len(accuracy_gated)
        and performance_passed == len(results)
    )
    return passed, summary


def quality_gate(category: str, metrics: JsonDict) -> JsonDict:
    """Evaluate only declared detector-quality gates.

    Fade/dissolve remains observational until the detector has a reviewed
    threshold for that transition family. It cannot increase the pass count.
    """
    if category == "fade_dissolve":
        return {
            "mode": "observational",
            "passed": None,
            "reasons": ["Fade/dissolve accuracy is recorded but is not a release gate."],
        }
    if category in {"hard_cuts", "vertical_video"}:
        passed = (
            float(metrics.get("precision", 0.0)) >= MIN_BOUNDARY_PRECISION
            and float(metrics.get("recall", 0.0)) >= MIN_BOUNDARY_RECALL
        )
        return {
            "mode": "accuracy",
            "passed": passed,
            "reasons": [] if passed else [
                f"Boundary precision must be >= {MIN_BOUNDARY_PRECISION:.1f} and recall >= {MIN_BOUNDARY_RECALL:.1f}."
            ],
        }
    passed = len(metrics.get("predicted", [])) == 0
    return {
        "mode": "accuracy",
        "passed": passed,
        "reasons": [] if passed else ["No-cut fixture produced one or more false-positive boundaries."],
    }


def _case_result(case: JsonDict, fixture: Path, workspace: Path, run: JsonDict) -> JsonDict:
    project_id = str(run["project_id"])
    project = workspace / project_id
    missing = [relative for relative in REQUIRED_ARTIFACTS if not (project / relative).is_file()]
    shots: list[Shot] = []
    if not missing and (project / "data" / "shots.json").is_file():
        shots = [Shot.model_validate(item) for item in load_json(project / "data" / "shots.json")]
    predicted = [round(float(shot.start_time), 3) for shot in shots if float(shot.start_time) > 0.1]
    metrics = boundary_metrics(list(case["expected_boundaries"]), predicted)
    readiness: JsonDict = {"status": "unavailable", "reasons": []}
    if run.get("state") == "completed":
        try:
            readiness = evaluate_project_readiness(project, workspace_root=workspace)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            readiness = {"status": "error", "reasons": [str(exc)]}
    fail_closed = readiness.get("status") == "blocked"
    functional_passed = run.get("state") == "completed" and not missing and fail_closed
    gate = quality_gate(str(case["category"]), metrics)
    quality_passed = gate["passed"]
    elapsed = (run.get("result") or {}).get("elapsed_seconds") if isinstance(run.get("result"), dict) else None
    performance_gate = {
        "elapsed_seconds": elapsed,
        "maximum_elapsed_seconds": MAX_CASE_ELAPSED_SECONDS,
        "passed": isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed <= MAX_CASE_ELAPSED_SECONDS,
    }
    passed = (
        functional_passed and performance_gate["passed"] and quality_passed
        if quality_passed is not None
        else None
    )
    status = (
        "observational"
        if functional_passed and performance_gate["passed"] and quality_passed is None
        else "passed" if passed else "failed"
    )
    return {
        **case,
        "fixture": f"fixtures/{fixture.name}",
        "fixture_size_bytes": fixture.stat().st_size,
        "run_id": run.get("run_id"),
        "project_id": project_id,
        "run_state": run.get("state"),
        "attempt": run.get("attempt"),
        "stage_timings": [
            {
                "id": stage.get("id"),
                "state": stage.get("state"),
                "elapsed_seconds": stage.get("elapsed_seconds"),
            }
            for stage in run.get("stages", [])
            if isinstance(stage, dict)
        ],
        "boundary_metrics": metrics,
        "artifact_completeness": {"required": len(REQUIRED_ARTIFACTS), "missing": missing},
        "pre_review_readiness": {"status": readiness.get("status"), "reasons": readiness.get("reasons", [])},
        "functional_passed": functional_passed,
        "quality_gate": gate,
        "performance_gate": performance_gate,
        "status": status,
        "passed": passed,
        "failure": run.get("error") if run.get("state") != "completed" else None,
    }


def run_audio_quality_benchmark(fixtures_root: Path) -> JsonDict:
    """Measure deterministic PCM behavior; never claim ASR or sound identity accuracy."""
    started = time.monotonic()
    results: list[JsonDict] = []

    silence = fixtures_root / "audio-silence.wav"
    _write_pcm(silence, 1.13)
    measured = measure_audio(silence)
    results.append(
        {
            "id": "silence-range",
            "passed": measured.rms == 0.0 and measured.silence_ranges == ((0.0, 1.13),),
            "metrics": {"rms": measured.rms, "silence_ranges": measured.silence_ranges},
            "thresholds": {"rms": 0.0, "range_tolerance_seconds": 0.0},
        }
    )

    pulses = fixtures_root / "audio-pulses-120bpm.wav"
    pulse_times = [0.3 + 0.5 * index for index in range(9)]
    _write_pcm(
        pulses,
        5.0,
        lambda current: 0.8
        if any(0 <= current - start < 0.04 for start in pulse_times)
        else 0.0,
    )
    measured = measure_audio(pulses)
    onset_errors = [
        abs(expected - actual.time)
        for expected, actual in zip(pulse_times, measured.onsets, strict=False)
    ]
    results.append(
        {
            "id": "regular-pulse-timing",
            "passed": (
                len(measured.onsets) == len(pulse_times)
                and bool(onset_errors)
                and max(onset_errors) <= AUDIO_ONSET_TOLERANCE_SECONDS
                and measured.estimated_bpm is not None
                and abs(measured.estimated_bpm - 120.0) <= AUDIO_BPM_TOLERANCE
            ),
            "metrics": {
                "expected_onsets": len(pulse_times),
                "measured_onsets": len(measured.onsets),
                "maximum_onset_error_seconds": max(onset_errors) if onset_errors else None,
                "estimated_bpm": measured.estimated_bpm,
            },
            "thresholds": {
                "maximum_onset_error_seconds": AUDIO_ONSET_TOLERANCE_SECONDS,
                "bpm_absolute_error": AUDIO_BPM_TOLERANCE,
            },
        }
    )

    stereo = fixtures_root / "audio-stereo-phase.wav"
    _write_pcm(stereo, 0.1, lambda _current: (0.5, -0.5), channels=2)
    measured = measure_audio(stereo)
    results.append(
        {
            "id": "stereo-energy",
            "passed": measured.channels == 2 and abs(measured.rms - 0.5) <= AUDIO_RMS_TOLERANCE,
            "metrics": {"channels": measured.channels, "rms": measured.rms},
            "thresholds": {"rms_target": 0.5, "rms_absolute_error": AUDIO_RMS_TOLERANCE},
        }
    )

    irregular = fixtures_root / "audio-irregular-transients.wav"
    irregular_times = [0.1, 0.5, 1.3, 1.56, 2.4, 2.8]
    _write_pcm(
        irregular,
        3.0,
        lambda current: 0.7
        if any(0 <= current - start < 0.03 for start in irregular_times)
        else 0.0,
    )
    measured = measure_audio(irregular)
    results.append(
        {
            "id": "irregular-no-tempo-claim",
            "passed": measured.estimated_bpm is None,
            "metrics": {"estimated_bpm": measured.estimated_bpm},
            "thresholds": {"tempo_claim": None},
        }
    )

    corrupt = fixtures_root / "audio-corrupt.wav"
    corrupt.write_bytes(b"not-wave")
    corrupt_rejected = False
    try:
        measure_audio(corrupt)
    except ValueError:
        corrupt_rejected = True
    results.append(
        {
            "id": "corrupt-fail-closed",
            "passed": corrupt_rejected,
            "metrics": {"rejected": corrupt_rejected},
            "thresholds": {"must_reject": True},
        }
    )

    for path in (silence, pulses, stereo, irregular, corrupt):
        path.unlink(missing_ok=True)
    elapsed = round(time.monotonic() - started, 3)
    passed_count = sum(item["passed"] is True for item in results)
    return {
        "schema_version": 1,
        "fixture_source": "generated PCM only; no external recordings",
        "redistribution": "CC0-1.0",
        "case_count": len(results),
        "passed_case_count": passed_count,
        "elapsed_seconds": elapsed,
        "maximum_elapsed_seconds": MAX_AUDIO_BENCHMARK_SECONDS,
        "passed": passed_count == len(results) and elapsed <= MAX_AUDIO_BENCHMARK_SECONDS,
        "cases": results,
        "asr_accuracy": {"status": "not_run", "reason": "no licensed speech recording and explicit model configured"},
        "semantic_identity_accuracy": {"status": "not_run", "reason": "baseline deliberately preserves voice/music/SFX identity as unknown"},
    }


def _write_pcm(
    path: Path,
    duration: float,
    sample: Any | None = None,
    *,
    channels: int = 1,
    rate: int = 8000,
) -> None:
    sampler = sample or (lambda _current: 0.0)
    payload = bytearray()
    for index in range(round(duration * rate)):
        values = sampler(index / rate)
        if not isinstance(values, tuple):
            values = (values,) * channels
        for value in values:
            integer = max(-32768, min(32767, round(float(value) * 32768)))
            payload.extend(integer.to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as output:
        output.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(payload)


def _self_peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _wait_for_run(workspace: Path, run_id: str, timeout: float = 300.0) -> JsonDict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = read_analysis_run(workspace, run_id)
        if run.get("state") in {"completed", "failed", "interrupted"}:
            return run
        time.sleep(0.1)
    raise TimeoutError(f"Benchmark run {run_id} did not finish within {timeout:.0f} seconds")


def _generate_fixture(case: JsonDict, destination: Path) -> None:
    require_tool("ffmpeg")
    category = case["category"]
    common = ["-y", "-hide_banner", "-loglevel", "error"]
    if category == "hard_cuts":
        args = [
            *common,
            "-f", "lavfi", "-i", "color=c=red:s=320x180:r=24:d=1.2",
            "-f", "lavfi", "-i", "color=c=lime:s=320x180:r=24:d=1.2",
            "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24:d=1.2",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-map", "3:a", "-t", "3.6",
        ]
    elif category == "fade_dissolve":
        args = [
            *common,
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=2",
            "-f", "lavfi", "-i", "smptebars=size=320x180:rate=24:duration=2",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-filter_complex", "[0:v][1:v]xfade=transition=fade:duration=0.6:offset=1.4[v]",
            "-map", "[v]", "-map", "2:a", "-t", "3.4",
        ]
    elif category == "vertical_video":
        args = [
            *common,
            "-f", "lavfi", "-i", "color=c=yellow:s=180x320:r=24:d=1.5",
            "-f", "lavfi", "-i", "color=c=purple:s=180x320:r=24:d=1.5",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a", "-t", "3.0",
        ]
    elif category == "variable_frame_rate":
        args = [
            *common,
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=3",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-vf", "select='not(mod(n,2))+not(mod(n,5))'",
            "-fps_mode", "vfr", "-map", "0:v", "-map", "1:a", "-t", "3.0",
        ]
    elif category == "speech_heavy_surrogate":
        args = [
            *common,
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=3",
            "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.25:sample_rate=44100",
            "-map", "0:v", "-map", "1:a", "-t", "3.0",
        ]
    else:
        args = [
            *common,
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=44100",
            "-map", "0:v", "-map", "1:a", "-t", "3.0",
        ]
    with atomic_output_path(destination, root=destination.parent) as temporary:
        run_command(
            [
                "ffmpeg",
                *args,
                "-shortest",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                str(temporary),
            ],
            timeout=120,
        )


def _ffmpeg_version() -> str:
    result = run_command([require_tool("ffmpeg"), "-version"], timeout=20)
    return result.stdout.splitlines()[0] if result.stdout else "unknown"
