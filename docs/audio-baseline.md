# Local audio baseline

The audio stage now creates an input-bound `audio-timeline/v1` alongside the five legacy audio outputs. It is part of the existing ingest → evidence → analysis → review → Finalize → explicit-export workflow, not a separate analysis mode.

## What runs by default

```bash
analyze-video --workspace ./analysis-projects audio PROJECT_ID
```

No model process, dependency installation, network request, or checkpoint download is initiated by the default baseline. It measures integer PCM WAV samples with the Python standard library:

| Result | Meaning and precision |
| --- | --- |
| Energy | RMS across all channels, 20 ms measurement windows; approximately 500 ms timeline summaries. Opposite-phase stereo does not cancel out. |
| Silence intervals | RMS at or below −50 dBFS for at least 100 ms. This is threshold silence, not proof of no sound. |
| Onsets | Conservative energy-rise candidates with a 120 ms refractory interval. They may be speech, effects, music, or noise. |
| Pulse tempo | At least five sufficiently regular onset candidates; 20–300 BPM estimate. Confidence is capped at 0.65. No musical meter/downbeat assertion. |
| Transcript | `unknown` without an explicitly supplied local checkpoint; an empty legacy SRT does not prove absence of speech. |
| Music, SFX, VO identity | `unknown` until an evidence-backed classification or human review. Energy alone does not identify them. |

Baseline events use `mixed` for unclassified measurements/candidates and `silence` for threshold silence. Acoustic measurements are `measured`; onset and tempo estimates are `machine_estimated`. No fabricated music style/mood tags are added. Legacy `beats.json` retains its name for compatibility but explicitly labels its entries `pcm_energy_onset_candidate`; `music_profile.json` is only a compatibility energy summary, with identity confidence zero and empty style/mood tags.

## Optional local transcription

Install and manage Whisper and its trusted checkpoint separately. This project does not install or download them automatically. Supply an **absolute path to an existing regular checkpoint file**:

```bash
analyze-video --workspace ./analysis-projects audio PROJECT_ID \
  --asr-model /absolute/path/to/checkpoint.pt --language zh
```

The same `--asr-model` flag is available on `run`. `--skip-asr` always skips transcription. The current web background-run entry uses the baseline only; configuration/UI for advanced adapters is a later Task. No checkpoint path is persisted by these CLI flags.

Whisper receives the private WAV snapshot, a local checkpoint pathname, CPU device, four threads, `fp16=False`, and a 300-second subprocess deadline. The child gets a minimal environment with temporary HOME/cache directories, without inherited API credentials or proxies. Output is the exact expected JSON filename, capped at 4 MiB, with duplicate keys, non-finite numbers, malformed/out-of-order segments and out-of-range times rejected. Transcription is an estimate, not verified verbatim VO; speaker and voice role remain unknown.

This is not an OS network sandbox, a checkpoint malware scanner, or a guarantee about arbitrary executables named `whisper`. Use a trusted installed executable and checkpoint. The official [Whisper loader](https://github.com/openai/whisper/blob/main/whisper/__init__.py) distinguishes known model names (download-capable) from local file paths. Model SHA-256 is checked before and after execution; this detects observable changes, not a hostile swap-and-restore between checks. Engine version is not invented.

| ASR status | Meaning |
| --- | --- |
| `produced` | A valid bounded result was returned; zero segments is a legitimate empty result. |
| `unknown` | No explicit checkpoint or no installed Whisper executable. |
| `skipped` | User requested `--skip-asr`. |
| `failed` | Attempted execution, checkpoint validation, or output validation failed; safe diagnostics remain in the timeline. |

## Bounds and input integrity

Supported: uncompressed integer PCM, 8–192 kHz, 1–8 channels, 8/16/24/32-bit samples. Python's [WAV reader](https://docs.python.org/3/library/wave.html) only supports uncompressed PCM; extensible PCM support depends on Python version (3.12+). Canonical ingest produces mono 16 kHz WAV.

Hard limits: 256 MiB input, 120 million sample values, 3,600 seconds, and a 30-second measurement-loop deadline. Checkpoint limit is 4 GiB. Limits bound individual stages, not total end-to-end execution including hashing and storage I/O. Corrupt/truncated/empty/unsupported WAVs fail explicitly before output replacement.

Every analyzer uses one private snapshot. The current canonical WAV must match that snapshot before publication and again when the timeline receipt is built; final receipt validation also verifies the current WAV. PCM/video duration differences above 100 ms are rejected. Smaller codec padding is clipped; a short audio tail is not invented as silence. Threshold/quantization uncertainty is not sample-exact event timing.

Files are:

```text
data/audio_intelligence.json
data/audio_intelligence_generation.json
```

The new timeline binds the legacy generation, media package and WAV digest. New durable runs require both valid legacy outputs **and** this input-bound timeline before reusing an audio stage. Older projects without the additive timeline remain readable, but are not reused as completed new audio analyses.

Publication uses the existing shots lock. Legacy outputs and the additive timeline retain separate commit markers; a failure between them can leave a valid legacy generation and an invalid/stale timeline, never a valid new combined analysis. Retry verification detects this. Existing human audio decisions are protected: rerunning audio is refused rather than erasing reviews; analyze a new project revision when necessary.

Temporary snapshots and ASR output directories are cleaned when the call exits. Timeline files replace fixed managed paths; no PDF/Excel is generated here. Finalize and customer exports remain explicit actions.

## Verification

```bash
.venv/bin/python -m pytest tests/test_audio.py tests/test_audio_transcription.py \
  tests/test_audio_intelligence_schema.py tests/test_transaction_concurrency.py \
  tests/test_run_lifecycle.py -q
```

Tests generate disposable synthetic WAVs. They check silence, RMS, all supported sample widths, stereo energy, pulse timing, irregular transients, malformed input, private-snapshot binding, rollback/retry, human-review preservation, ASR capability distinctions, strict output validation and real subprocess environment filtering. Mocked Whisper protocol tests do not establish real-model transcription accuracy. No real checkpoint benchmark or professional audio semantic evaluation has been performed by these tests.
