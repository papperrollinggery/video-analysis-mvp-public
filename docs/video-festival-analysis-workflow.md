# Festival analysis profile

The `festival` profile applies the same evidence pipeline to a short film, trailer, or review cut. It prioritizes concept clarity, mood continuity, audiovisual intent, and reviewable source references. It does not decide whether a film will be selected.

## Permission and privacy first

- Confirm that you are authorized to download and analyze the source.
- Keep passwords, private URLs, screeners, and generated transcripts out of commits and issues.
- Prefer a local source file when one is available.
- Unset external provider credentials when the screener must remain local.
- Review a festival's current rules on its official site; eligibility and deadlines can change.

## Local-first run

```bash
unset OPENAI_API_KEY MINIMAX_API_KEY

PYTHONPATH=src .venv/bin/python -m video_analysis_mvp.cli \
  --workspace ./analysis-projects \
  run ./path/to/authorized-screener.mp4 \
  --project-id festival-sample \
  --profile festival \
  --delivery-language en \
  --skip-asr
```

If dialogue structure matters and local Whisper is installed, remove `--skip-asr`.

For a supported public remote source, pass the URL through the trusted-operator CLI with `--acknowledge-url-risk`; the browser and FastAPI creation surfaces deliberately reject remote URLs. Put signed/private URL values in an owner-only `--source-value-file` and passwords in an owner-only `--password-file`; plaintext argv passwords are rejected. Never copy private values into public documentation, and use an egress-restricted downloader when the URL itself is not trusted.

## Evidence review sequence

1. Confirm duration, frame rate, resolution, and review-copy quality.
2. Watch the source from beginning to end before using generated summaries.
3. Compare every proposed shot boundary with the edit.
4. Review the contact sheet for sequence, recurring motifs, and missing visual regions.
5. Review transcript timing and mark uncertain speech.
6. Inspect music/rhythm output as a coarse cue, not a score analysis.
7. Resolve or record blocked readiness reasons.
8. Write interpretation only after linking it to shots and timecodes.

## Suggested research questions

- Can the premise be stated using evidence from the opening shots?
- Where does the visual or narrative mode change?
- Which motifs recur, and at which timecodes?
- Does sound introduce information not visible in the keyframes?
- Which conclusions depend on transcript or vision fields that remain unreviewed?
- What festival-fit claim requires external rule research rather than video evidence?

## Codex handoff

Generate the package, then start a separate Codex task with `reports/codex_handoff.md`. A bounded task brief:

```text
Audit this festival-profile evidence package. Separate source observations,
machine annotations, human review state, and interpretation. Cite shot_id,
timecode, and evidence path for every claim. Identify missing evidence before
drafting a one-page programming-fit memo. Do not predict selection.
```

Festival rules, previous programs, and submission policy are external research. Verify them against current official sources and cite them separately from the video analysis.

## Deliverables

Useful outputs include:

- `reports/storyboard.html` for sequence review;
- `reports/shot_list.csv` for coding and comparison;
- `reports/profile_analysis.html` for the profile-aware evidence report;
- `reports/report.html` for a readable overview;
- `data/shots.json` and `data/transcript.json` for analysis;
- `data/readiness.json` for unresolved review gaps;
- `reports/codex_handoff.md` for an explicit follow-on task;
- `data/visualization_dataset.json` for an optional shot timeline.

## Limits

- Profile wording is not a festival-programming model.
- Sparse frames cannot establish performance nuance or editing rhythm by themselves.
- Transcript and vision output can misread names, languages, text, symbolism, or cultural context.
- A passing readiness gate does not establish rights, eligibility, originality, or artistic quality.
- Selection forecasts without a documented historical dataset are speculation.
