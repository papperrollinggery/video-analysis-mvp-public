from __future__ import annotations

import argparse
import json
import sys

from .pipeline import run_audio, run_full_pipeline, run_ingest_only, run_report, run_vision, run_visual
from .schemas import AnalysisProfile, StatusEnvelope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analyze-video", description="Local-first video analysis MVP.")
    parser.add_argument("--workspace", default=None, help="Workspace for analysis projects.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run ingest, visual, audio, synthesis, and report.")
    run.add_argument("source")
    run.add_argument("--profile", choices=[item.value for item in AnalysisProfile], default=AnalysisProfile.ads.value)
    run.add_argument("--password", default=None)
    run.add_argument("--project-id", default=None)
    run.add_argument("--language", default="auto")
    run.add_argument("--skip-asr", action="store_true", help="Skip speech transcription and still generate rhythm/music outputs.")

    ingest = sub.add_parser("ingest", help="Create a canonical media package.")
    ingest.add_argument("source")
    ingest.add_argument("--profile", choices=[item.value for item in AnalysisProfile], default=AnalysisProfile.ads.value)
    ingest.add_argument("--password", default=None)
    ingest.add_argument("--project-id", default=None)

    visual = sub.add_parser("visual", help="Run visual analysis for an existing project.")
    visual.add_argument("project_id")

    audio = sub.add_parser("audio", help="Run audio analysis for an existing project.")
    audio.add_argument("project_id")
    audio.add_argument("--language", default="auto")
    audio.add_argument("--skip-asr", action="store_true")

    report = sub.add_parser("report", help="Regenerate report for an existing project.")
    report.add_argument("project_id")

    vision = sub.add_parser("vision", help="Run vision annotation for TapNow-style shot analysis.")
    vision.add_argument("project_id")
    vision.add_argument("--provider", choices=["openai", "minimax_mcp"], default=None)
    vision.add_argument("--model", default=None)
    vision.add_argument("--limit", type=int, default=None)

    serve = sub.add_parser("serve", help="Start the local web UI.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = run_full_pipeline(
                args.source,
                profile=AnalysisProfile(args.profile),
                password=args.password,
                workspace=args.workspace,
                project_id=args.project_id,
                language=args.language,
                skip_asr=args.skip_asr,
            )
        elif args.command == "ingest":
            result = run_ingest_only(
                args.source,
                profile=AnalysisProfile(args.profile),
                password=args.password,
                workspace=args.workspace,
                project_id=args.project_id,
            )
        elif args.command == "visual":
            result = run_visual(args.project_id, workspace=args.workspace)
        elif args.command == "audio":
            result = run_audio(args.project_id, workspace=args.workspace, language=args.language, skip_asr=args.skip_asr)
        elif args.command == "report":
            result = run_report(args.project_id, workspace=args.workspace)
        elif args.command == "vision":
            result = run_vision(
                args.project_id,
                workspace=args.workspace,
                model=args.model,
                limit=args.limit,
                provider=args.provider,
            )
        elif args.command == "serve":
            from .web import serve

            serve(host=args.host, port=args.port, workspace=args.workspace)
            return 0
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
