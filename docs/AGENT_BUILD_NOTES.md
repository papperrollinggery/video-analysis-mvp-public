# Agent Build Notes

This project is intended as a concrete AI/Agent-assisted engineering artifact.

## Build Scope

The prototype turns a source video into a structured local analysis package:

- media ingest through local files or `yt-dlp` supported URLs
- FFmpeg-based review asset generation
- keyframe extraction and contact sheet generation
- first-pass shot and scene structures
- optional local ASR transcript generation
- rhythm peak and coarse music profile extraction
- bilingual report generation
- CSV/SRT/JSON/HTML deliverables
- local Web UI for project creation, report access, and model config

## Agent Usage

AI coding agents were used for:

- translating product requirements into a CLI-first architecture
- designing Pydantic schemas for stable report artifacts
- implementing pipeline modules across ingest, visual analysis, audio analysis, synthesis, and UI
- generating bilingual report templates
- debugging local media-processing workflows
- preparing public-safe documentation

## MiMo Integration Plan

Xiaomi MiMo is a good candidate for the next model backend because this workflow has Chinese-heavy analysis and reporting needs.

Planned tests:

- Chinese shot description from keyframes
- subtitle summarization and dialogue cleanup
- client-facing report drafting
- video prompt generation from shot rows
- long-context review of generated reports
- comparison against existing OpenAI-compatible model backends

Expected output:

- model comparison notes
- updated README usage examples
- optional MiMo provider adapter
- sample prompt templates for video-analysis agents
