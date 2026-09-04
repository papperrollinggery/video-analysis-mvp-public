from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .doctor import run_doctor
from .media import DEFAULT_MAX_DURATION_SECONDS
from .pipeline import (
    run_audio,
    run_full_pipeline,
    run_ingest_only,
    run_report,
    run_vision,
    run_visual,
)
from .schemas import AnalysisProfile, StatusEnvelope

MAX_CLI_SOURCE_VALUE_BYTES = 16 * 1024
MAX_CLI_PASSWORD_BYTES = 4 * 1024


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _read_private_value_file(path: Path, *, label: str, maximum: int) -> str:
    candidate = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        parent = candidate.parent.resolve(strict=True)
        target = parent / candidate.name
    except OSError as exc:
        raise ValueError(f"{label} file is unavailable") from exc
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name == "posix":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    else:  # pragma: no cover - Windows is not a verified release target
        try:
            if stat.S_ISLNK(target.lstat().st_mode):
                raise ValueError(f"{label} file must be a regular non-symlink file")
        except OSError as exc:
            raise ValueError(f"{label} file is unavailable") from exc
    try:
        descriptor = os.open(target, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{label} file must be a regular non-symlink file")
            if os.name == "posix" and (
                info.st_mode & 0o077
                or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
            ):
                raise ValueError(
                    f"{label} file must be owned by the current user with mode 0600"
                )
            if info.st_size > maximum:
                raise ValueError(f"{label} file exceeds {maximum} bytes")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(maximum + 1)
            if len(payload) > maximum:
                raise ValueError(f"{label} file exceeds {maximum} bytes")
        finally:
            os.close(descriptor)
        text = payload.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} file is unreadable or exceeds {maximum} bytes") from exc
    if text.endswith("\r\n"):
        value = text[:-2]
    elif text.endswith(("\r", "\n")):
        value = text[:-1]
    else:
        value = text
    if not value or any(character in value for character in ("\r", "\n", "\0")):
        raise ValueError(f"{label} file must contain exactly one non-empty UTF-8 value")
    return value


def _ingest_values(args: argparse.Namespace) -> tuple[str, str | None]:
    source_file = args.source_value_file
    if args.source is not None and source_file is not None:
        raise ValueError("Use either the source argument or --source-value-file, not both")
    if source_file is not None:
        source = _read_private_value_file(
            source_file,
            label="Source value",
            maximum=MAX_CLI_SOURCE_VALUE_BYTES,
        )
    else:
        source = str(args.source or "")
    if not source:
        raise ValueError("A source argument or --source-value-file is required")

    parsed = urlsplit(source)
    is_url = parsed.scheme in {"http", "https"}
    if is_url and source_file is None:
        raise ValueError(
            "URL values must use --source-value-file to avoid argv exposure"
        )
    if is_url and not args.acknowledge_url_risk:
        raise ValueError(
            "CLI URL ingest requires --acknowledge-url-risk; redirects and DNS rebinding are not sandboxed"
        )
    if args.legacy_password is not None:
        raise ValueError("Plaintext --password is not supported; use --password-file")
    password = (
        _read_private_value_file(
            args.password_file,
            label="Password",
            maximum=MAX_CLI_PASSWORD_BYTES,
        )
        if args.password_file is not None
        else None
    )
    if password is not None and not is_url:
        raise ValueError("--password-file is supported only for explicit CLI URL ingest")
    return source, password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analyze-video",
        description="Local-first, auditable shot-level video evidence workbench.",
    )
    parser.add_argument("--workspace", default=None, help="Workspace for analysis projects.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run ingest, visual, audio, synthesis, and report.")
    run.add_argument("source", nargs="?")
    run.add_argument("--profile", choices=[item.value for item in AnalysisProfile], default=AnalysisProfile.research.value)
    run.add_argument("--source-value-file", type=Path, default=None, help="Read a private or signed source value from one owner-only file instead of argv.")
    run.add_argument("--password-file", type=Path, default=None, help="Read the video password from one owner-only file; plaintext argv passwords are rejected.")
    run.add_argument("--password", dest="legacy_password", default=None, help=argparse.SUPPRESS)
    run.add_argument("--acknowledge-url-risk", action="store_true", help="Required for CLI URL ingest; acknowledges that yt-dlp redirects/DNS rebinding are outside the app sandbox.")
    run.add_argument("--project-id", default=None)
    run.add_argument("--max-duration-seconds", type=float, default=DEFAULT_MAX_DURATION_SECONDS)
    run.add_argument("--language", default="auto")
    run.add_argument("--delivery-language", choices=["zh", "en"], default="zh", help="Report language: zh or en.")
    run.add_argument("--skip-asr", action="store_true", help="Skip speech transcription and still generate rhythm/music outputs.")
    run.add_argument("--asr-model", default=None, help="Explicit local Whisper checkpoint path; no model is downloaded.")
    run.add_argument(
        "--with-vision",
        action="store_true",
        help="Explicitly send selected frames to the configured external vision provider.",
    )

    ingest = sub.add_parser("ingest", help="Create a canonical media package.")
    ingest.add_argument("source", nargs="?")
    ingest.add_argument("--profile", choices=[item.value for item in AnalysisProfile], default=AnalysisProfile.research.value)
    ingest.add_argument("--source-value-file", type=Path, default=None, help="Read a private or signed source value from one owner-only file instead of argv.")
    ingest.add_argument("--password-file", type=Path, default=None, help="Read the video password from one owner-only file; plaintext argv passwords are rejected.")
    ingest.add_argument("--password", dest="legacy_password", default=None, help=argparse.SUPPRESS)
    ingest.add_argument("--acknowledge-url-risk", action="store_true", help="Required for CLI URL ingest; acknowledges that yt-dlp redirects/DNS rebinding are outside the app sandbox.")
    ingest.add_argument("--project-id", default=None)
    ingest.add_argument("--max-duration-seconds", type=float, default=DEFAULT_MAX_DURATION_SECONDS)

    visual = sub.add_parser("visual", help="Run visual analysis for an existing project.")
    visual.add_argument("project_id")

    audio = sub.add_parser("audio", help="Run audio analysis for an existing project.")
    audio.add_argument("project_id")
    audio.add_argument("--language", default="auto")
    audio.add_argument("--skip-asr", action="store_true")
    audio.add_argument("--asr-model", default=None, help="Explicit local Whisper checkpoint path; no model is downloaded.")

    review = sub.add_parser("audio-review", help="Inspect audio evidence or save an explicit operator review; never auto-Finalize/export.")
    review_actions = review.add_subparsers(dest="audio_review_action", required=True)
    for action in ("list", "show", "apply"):
        item = review_actions.add_parser(action)
        item.add_argument("project_id")
        if action in {"show", "apply"}:
            item.add_argument("event_id")
        if action == "apply":
            item.add_argument("--request", required=True, type=Path, help="Strict JSON review request, including explicit operator confirmation.")
        else:
            item.add_argument("--expected-generation-id", default=None)
        if action == "list":
            item.add_argument("--offset", type=int, default=0)
            item.add_argument("--limit", type=int, default=50)
            item.add_argument("--kind", choices=["voice", "music", "sfx", "silence", "mixed"], default=None)
            item.add_argument("--review-status", choices=["unreviewed", "reviewed", "rejected", "needs_work", "needs_review"], default=None)
            item.add_argument("--shot-id", default=None)

    report = sub.add_parser("report", help="Regenerate report for an existing project.")
    report.add_argument("project_id")
    report.add_argument("--delivery-language", choices=["zh", "en"], default=None, help="Override report language.")

    export = sub.add_parser("export", help="Explicitly generate or manage professional client exports.")
    export_actions = export.add_subparsers(dest="export_action", required=True)
    export_generate = export_actions.add_parser("generate")
    export_generate.add_argument("project_id")
    export_generate.add_argument("--format", action="append", choices=["xlsx", "pdf"], required=True)
    export_generate.add_argument("--idempotency-key", required=True)
    export_generate.add_argument("--language", choices=["zh", "en", "bilingual"], default=None)
    export_generate.add_argument("--density", choices=["client", "compact"], default=None)
    export_generate.add_argument("--project-subtitle", default=None)
    export_generate.add_argument("--logo-path", default=None)
    export_generate.add_argument("--accent-color", default=None)
    for action in ("status", "recover"):
        item = export_actions.add_parser(action)
        item.add_argument("project_id")
    export_cancel = export_actions.add_parser("cancel")
    export_cancel.add_argument("project_id")
    export_cancel.add_argument("request_digest")
    for action in ("save", "delete"):
        item = export_actions.add_parser(action)
        item.add_argument("project_id")
        item.add_argument("version_id")

    vision = sub.add_parser("vision", help="Run optional vision annotation for shot evidence.")
    vision.add_argument("project_id")
    vision.add_argument("--provider", choices=["openai", "minimax_mcp", "bridgedeck"], default=None)
    vision.add_argument("--model", default=None)
    vision.add_argument("--base-url", default=None, help="Explicit one-run endpoint override; BridgeDeck requires an account-scoped numeric-loopback URL.")
    vision.add_argument("--limit", type=_positive_int, default=None)

    codex = sub.add_parser("codex", help="Use the current Codex task inside the existing analysis/review workflow; no extra API key.")
    codex_actions = codex.add_subparsers(dest="codex_action", required=True)
    for action in ("prepare", "status", "apply"):
        action_parser = codex_actions.add_parser(action)
        action_parser.add_argument("project_id")
        if action == "apply":
            action_parser.add_argument("--result", required=True, type=Path)

    migrate = sub.add_parser(
        "migrate",
        help="Inspect or explicitly prepare one supported legacy project for re-Finalize.",
    )
    migrate.add_argument("project_id")
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="Recover any interrupted migration, invalidate legacy publication, and require re-Finalize.",
    )

    serve = sub.add_parser("serve", help="Start the local web UI.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)

    doctor = sub.add_parser("doctor", help="Check local dependencies, workspace, keys, and optional sample input.")
    doctor.add_argument("--sample", default=None, help="Optional local .mp4/.mov sample to verify existence and size.")

    benchmark = sub.add_parser("benchmark", help="Run the six-case local synthetic product benchmark.")
    benchmark.add_argument("--output", required=True, help="Directory for generated fixtures, projects, and the JSON receipt.")

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            source, password = _ingest_values(args)
            result = run_full_pipeline(
                source,
                profile=AnalysisProfile(args.profile),
                password=password,
                workspace=args.workspace,
                project_id=args.project_id,
                language=args.language,
                delivery_language=args.delivery_language,
                skip_asr=args.skip_asr,
                asr_model=args.asr_model,
                with_vision=args.with_vision,
                max_duration_seconds=args.max_duration_seconds,
            )
        elif args.command == "ingest":
            source, password = _ingest_values(args)
            result = run_ingest_only(
                source,
                profile=AnalysisProfile(args.profile),
                password=password,
                workspace=args.workspace,
                project_id=args.project_id,
                max_duration_seconds=args.max_duration_seconds,
            )
        elif args.command == "visual":
            result = run_visual(args.project_id, workspace=args.workspace)
        elif args.command == "audio":
            result = run_audio(args.project_id, workspace=args.workspace, language=args.language, skip_asr=args.skip_asr, asr_model=args.asr_model)
        elif args.command == "audio-review":
            from .audio_review import (
                apply_audio_review,
                get_audio_event,
                read_audio_review,
                read_review_request,
            )
            from .paths import ProjectPaths
            from .store import workspace_path
            from .workspace_api import ApiError, validated_project_root

            try:
                paths = ProjectPaths(validated_project_root(workspace_path(args.workspace), args.project_id))
                if args.audio_review_action == "apply":
                    payload = apply_audio_review(paths, args.event_id, read_review_request(args.request))
                elif args.audio_review_action == "show":
                    payload = get_audio_event(paths, args.event_id, args.expected_generation_id)
                else:
                    options = {key: getattr(args, key) for key in ("offset", "limit", "kind", "review_status", "shot_id", "expected_generation_id") if getattr(args, key) is not None}
                    payload = read_audio_review(paths, options)
            except ApiError as exc:
                print(json.dumps({"error": {"message": exc.message, "details": exc.details, "status": exc.status}}, ensure_ascii=False), file=sys.stderr)
                return 1
            print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        elif args.command == "report":
            result = run_report(args.project_id, workspace=args.workspace, delivery_language=args.delivery_language)
        elif args.command == "export":
            from .export_service import (
                cancel_client_export,
                delete_saved_export,
                generate_client_export,
                pdf_runtime_from_environment,
                read_export_state,
                recover_client_exports,
                save_current_export,
            )
            from .paths import ProjectPaths
            from .store import workspace_path
            from .workspace_api import validated_project_root

            workspace_root = workspace_path(args.workspace)
            paths = ProjectPaths(validated_project_root(workspace_root, args.project_id))
            if args.export_action == "generate":
                settings = {
                    key: value
                    for key, value in {
                        "language": args.language,
                        "density": args.density,
                        "project_subtitle": args.project_subtitle,
                        "logo_path": args.logo_path,
                        "accent_color": args.accent_color,
                    }.items()
                    if value is not None
                }
                payload = generate_client_export(
                    paths,
                    formats=args.format,
                    settings=settings,
                    idempotency_key=args.idempotency_key,
                    pdf_runtime=pdf_runtime_from_environment(),
                )
            elif args.export_action == "status":
                payload = read_export_state(paths)
            elif args.export_action == "cancel":
                payload = cancel_client_export(paths, args.request_digest)
            elif args.export_action == "save":
                payload = save_current_export(paths, args.version_id)
            elif args.export_action == "delete":
                payload = delete_saved_export(paths, args.version_id)
            else:
                payload = recover_client_exports(paths)
            print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        elif args.command == "vision":
            result = run_vision(
                args.project_id,
                workspace=args.workspace,
                model=args.model,
                limit=args.limit,
                provider=args.provider,
                base_url=args.base_url,
            )
        elif args.command == "codex":
            from .codex_analysis import (
                apply_codex_analysis,
                codex_analysis_status,
                prepare_codex_analysis,
                read_codex_response,
            )
            from .paths import ProjectPaths, resolve_project_root
            from .store import workspace_path

            paths = ProjectPaths(resolve_project_root(args.project_id, workspace_path(args.workspace)))
            if args.codex_action == "prepare":
                payload = prepare_codex_analysis(paths)
            elif args.codex_action == "apply":
                payload = apply_codex_analysis(paths, read_codex_response(args.result))
            else:
                payload = codex_analysis_status(paths)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1 if payload["status"] == "incomplete" else 0
        elif args.command == "migrate":
            from .migration import prepare_project_migration
            from .paths import ProjectPaths
            from .store import workspace_path
            from .workspace_api import validated_project_root

            workspace_root = workspace_path(args.workspace)
            paths = ProjectPaths(
                validated_project_root(workspace_root, args.project_id)
            )
            payload = prepare_project_migration(paths, apply=args.apply)
            print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        elif args.command == "serve":
            from .web import serve

            serve(host=args.host, port=args.port, workspace=args.workspace)
            return 0
        elif args.command == "doctor":
            result = run_doctor(workspace=args.workspace, sample=args.sample)
        elif args.command == "benchmark":
            from .benchmark import run_synthetic_benchmark

            result = run_synthetic_benchmark(args.output)
        else:
            parser.error("Unknown command")
            return 2
    except Exception as exc:
        result = StatusEnvelope(
            status="error",
            summary="Command failed.",
            next_actions=["Inspect the error and rerun the command with a supported video source."],
            error=str(exc),
        )
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 1 if result.status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
