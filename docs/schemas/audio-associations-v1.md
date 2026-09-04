# Shot/audio associations v1

`shot-audio-associations/v1` is a deterministic, read-only projection of the validated audio timeline onto shot and narrative ranges. It is embedded once in the existing `data/visualization_dataset.json` as `audio_associations`; it does not create another independent truth file or generate Excel/PDF. Each outer shot's `audio.association_ref` is a same-document JSON Pointer to its canonical association record; link tables are not serialized a second time.

## Evidence and timing

- `events` retains original proposals, digest-bound reviews, effective proposals and project-relative event references. Rejected/needs-work events remain traceable, but have no effective proposal.
- `shots` and `scenes` contain event links clipped to the strict half-open intersection `[max(starts), min(ends))`. Touching boundaries do not overlap. No epsilon snapping, extrapolation, word alignment or cross-source event merging is performed.
- A link records overlap duration, event/range fractions, and continuation flags. The original event timestamps are unchanged.
- Existing video ranges are stored at millisecond precision. A media-tail overrun of at most 0.500001 ms is accepted for compatibility; ranges are not rewritten, and overlap still clips against the real event end. Larger overruns fail.
- Transcript links reference the original event. Full text is not copied into every shot; a sentence spanning shots remains one source event, not fabricated word-level clips.
- Per-kind `event_coverage_seconds` is the union of usable recorded intervals, not a sum of overlapping intervals and not proof of a source's presence/absence. Zero recorded coverage with an unknown capability does not mean silence.
- Scene labels remain narrative interpretations. Scene memberships and ranges come from existing scene records; no narrative cause or emotional function is invented by the join.

## Review and identity

Raw `kind` is the upstream event category, not independent proof of identity. `identity_status` is `unknown` for baseline-only identity claims, `machine_estimated` for ASR/classification/separation proposals, or `human_reviewed` for an effective human review. It does not identify a real person.

Every shot exposes unresolved event IDs. Unreviewed estimates, low-confidence measurements, unresolved identity claims and `needs_work` events require review. Rejected events remain visible as excluded rather than silently disappearing.

Human/provider shot annotations are retained separately as `protected_annotation`, including explicit empty values. This projection does not authenticate their self-reported source or upgrade them into verified media facts. Only machine-owned legacy display fields are refreshed. Full original audio proposals and reviews are never rewritten by report generation.

The existing, translatable beat-density `rhythm_notes` label is preserved. The separate `sound_rhythm` summary uses the project's delivery language. Both `summary.en` and `summary.zh` remain in the projection. Linked RMS records and pulse-estimate ranges are shown as acoustic information, without manufacturing an unidentified `MusicProfile` as if music had been detected.

All profile/storyboard/creative delivery renderers receive presentation text and music values derived from effective events when a timeline is available. An explicitly cleared or rejected sentence cannot reappear through a legacy-renderer fallback. Legacy `transcript.json` and `transcript.srt` remain original source evidence, not the effective reviewed transcript; they are not rewritten to hide the original proposal.

Legacy dialogue/speech-summary fields remain bounded 220-character previews, with an explicit full-text marker when shortened. The internal HTML evidence table uses a marked 1,200-character preview for unusually long content and at most 240 link rows, with displayed/total counts and the complete JSON reference. Full text and all links remain in the audio event data; professional client renderers must use the full data, not these compatibility previews.

## Binding and compatibility

`build_project_audio_associations` verifies the committed audio receipt, reads the safely opened timeline bytes, compares their SHA-256 to the receipt, and validates the schema. Missing legacy audio intelligence is explicit `available=false`; partial, stale, corrupt or unsafe input fails closed. Pure `associate_audio_events` is a transformation helper, not an on-disk verification API.

`source_binding` records the audio generation/dataset hashes. `geometry_sha256` binds the participating IDs, ranges and scene memberships. `association_digest` covers the entire projection, including proposals, reviews, narrative metadata and protected annotation data. These are reproducibility digests, not cryptographic proof of the author of a claim.

New reports use `report_generation.schema_version=4`. Its `source_receipts` has exactly visual generation, legacy audio generation, readiness, and nullable `audio_intelligence`. Adding, removing or editing an audio timeline invalidates a finalized report even if all legacy audio files stay unchanged. Synthesis verifies the audio binding again before committing; a change during rendering cannot make a new receipt describe old derived content.

Committed v3 reports without audio intelligence remain readable. Once audio intelligence is present, they need explicit report regeneration/Finalize to acquire the v4 binding. The reader does not automatically rewrite old manifests or audio data. The legacy five-file `audio_generation` contract is unchanged.

## Bounds and consumers

The join rejects invalid/non-finite geometry, duplicate range IDs, references to unknown shot IDs, more than 10,000 ranges of a type, or more than 100,000 shot/scene links. Limits fail explicitly; source events are not silently dropped. The early range-scan exit relies on the timeline validator enforcing `(start_time, end_time, event_id)` order. This is intended for bounded local projects, not an unlimited streaming index.

The existing HTML report and Codex/visualization dataset consume the same in-memory projection. The Markdown handoff includes guidance and references but deliberately excludes raw transcripts and event labels. All text in evidence files is untrusted data, never execution instructions. HTML escapes event text, labels and IDs.

Tests: `tests/test_audio_synthesis.py` covers half-open boundaries, overlapping coverage, cross-shot text, de-duplication, preserved/rejected/blank reviews, geometry/digest changes, stale inputs, v3/v4 compatibility, publication-time input changes and HTML/prompt-injection boundaries. Audio-review UI and professional-export acceptance live in separate tests; this contract does not establish real-model audio accuracy.
