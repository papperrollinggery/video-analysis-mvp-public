# Audio intelligence schema v1

Status: implemented schema, validation, transaction, receipt, and [deterministic local PCM baseline](../audio-baseline.md). Optional local Whisper requires an explicit checkpoint; implementation does not prove that any real model, diarization, separation, or classification adapter ran.

## Compatibility boundary

The new files are additive:

```text
data/audio_intelligence.json
data/audio_intelligence_generation.json
```

They do not add fields to, rewrite, or weaken `audio_generation.json`. The existing v1 audio generation still commits exactly its five legacy artifacts plus its marker. A project with neither new file is valid and reports audio intelligence as unavailable; it is not treated as damaged.

New durable analysis runs additionally require a valid input-bound timeline before reusing the audio stage. This does not retroactively invalidate legacy project reads. Baseline publication refuses to overwrite existing human audio decisions.

## Dataset root

`audio_intelligence.json` contains exactly:

| Field | Contract |
| --- | --- |
| `schema_id` | exactly `audio-timeline/v1` |
| `time_range_semantics` | exactly `[start,end)` |
| `media_duration_seconds` | finite and greater than zero |
| `sources` | strict, unique provenance records |
| `capabilities` | one explicit result for every supported capability |
| `events` | strict, deterministically ordered timeline events |

Events may overlap. `mixed` is a valid event kind. Missing capability is represented as `unknown`, `failed`, or `skipped` with a reason; it is never converted into silence or an empty successful result.

## Sources and capabilities

Supported capabilities are:

- `baseline_features`
- `asr`
- `diarization`
- `separation`
- `classification`

Every source records a bounded anonymous `source_id`, capability, source type, adapter/engine/model metadata, status, and bounded diagnostics. Source type is one of `measured`, `deterministic_detector`, `adapter`, or `imported`; human assertions belong in `review`, never in a machine proposal source. Private absolute paths, embedded private paths, credential-shaped keys, and credential-shaped metadata values are rejected. Provider tokens and personal identities have no schema field.

Capability status is one of `produced`, `unknown`, `failed`, or `skipped`. A produced capability requires one selected produced source binding. Every source marked `produced` must be that selected source; an unused produced source is invalid. An event may reference only the selected source, not another candidate for the same capability. `unknown` and `skipped` never select a source; `failed` binds the failed source that produced its diagnostics.

Capability and event kind are also checked together: ASR and diarization emit voice events; separation and classification may emit voice, music, SFX, or mixed events; deterministic baseline features may express measured or estimated voice activity, music, SFX, silence, and mixed regions. A non-voice event cannot carry transcript text, language, speaker, or a non-unknown voice role. Proposal verification must agree with the source type.

## Events

Each event contains exactly:

| Field | Contract |
| --- | --- |
| `event_id` | unique bounded identifier |
| `start_time`, `end_time` | finite, `0 <= start < end <= media duration` |
| `kind` | `voice`, `music`, `sfx`, `silence`, or `mixed` |
| `source_id` | existing produced evidence source |
| `proposal` | immutable machine/measured proposal |
| `review` | `null` or a digest-bound human review |

Events are sorted by `(start_time, end_time, event_id)`. Sorting does not prohibit overlap.

The proposal includes label, text, language, anonymous speaker cluster, voice role, energy, onset density, estimated BPM, confidence, and verification category. `label` may be an explicit empty string when no bounded description can be inferred. Speaker IDs must use an opaque `speaker_0007`, `spk-12`, or `cluster_3`-style identifier; identity-like labels such as a person's name are invalid. The product does not infer a real person. `voice_role` is `voice_over`, `dialogue`, `singing`, or `unknown`.

Machine proposal fields have a strict owner. A single source cannot claim results produced by another capability:

| Capability | Fields it may assert in addition to label, confidence, and verification |
| --- | --- |
| `baseline_features` | `energy`, `onset_density`, `estimated_bpm`; text is empty, language/role are `unknown`, speaker is `null` |
| `asr` | `text`, `language`; speaker is `null`, role is `unknown`, acoustic fields are `null` |
| `diarization` | required anonymous `speaker_id`; text is empty and all unrelated fields are unknown/null |
| `separation` | no transcript, speaker, role, or acoustic measurement claims |
| `classification` | `voice_role` for voice/mixed events; no transcript, speaker, or acoustic measurement claims |

The UI/export layer may join overlapping events from these sources into a combined view. That join does not rewrite the individual event provenance.

Proposal verification is one of:

- `measured`
- `machine_estimated`
- `model_interpreted`

A non-`unknown` voice role is always an estimate in a machine proposal and cannot be marked `measured`, including imported classifications. Human review may confirm the role through a digest-bound override.

## Human review

A review contains:

- `status`: `reviewed`, `rejected`, or `needs_work`;
- `expected_proposal_sha256`: optimistic binding to the exact proposal;
- sparse `overrides`;
- bounded notes;
- `verification: human_reviewed` for `reviewed` or `rejected`, and `human_draft` for `needs_work`.

The proposal is never overwritten. Effective values are resolved at read time. A missing override means “use the proposal”; an explicit `{"text": ""}` means the operator confirmed an empty value and must not fall back. A rejected event and a `needs_work` draft both have no effective proposal. Only `reviewed` applies overrides and upgrades the result to `human_reviewed`. The immutable machine proposal is validated against its source capability; a human override is independently provenance-bound by the review digest and revalidated against the original event kind. Therefore a reviewer may confirm VO text or role without pretending that ASR produced diarization/classification, while a music-only event still cannot acquire a speaker cluster.

## Generation receipt

`audio_intelligence_generation.json` contains exactly:

- schema and dataset versions;
- a canonical generation ID;
- `state: committed` and SHA-256 digest policy;
- bounded, secret-free generation parameters;
- the exact capability map;
- bindings to the current legacy audio generation, raw `media_package.json`, and canonical `assets/audio.wav`;
- the digest and size of `audio_intelligence.json`.

The generation ID hashes the canonical receipt core excluding only the generation ID itself. Verification recomputes every bound input/output, validates dataset semantics, compares capability maps, and requires dataset duration and canonical audio path to match the current media package. Receipt, media-package, and dataset JSON reject duplicate object keys. Dataset digest, size, parsing, and schema validation use the same safely opened bytes.

Producers may pass `expected_audio_wav` (SHA-256 and byte size) to `stage_and_commit_audio_intelligence`. The baseline uses this guard to reject publication when its private analyzed snapshot differs from the current WAV; the receipt must not bind old measurements to new bytes.

## Commit and failure behavior

Generation creates and immediately holds a private stage-directory descriptor, writes both staged files descriptor-relative, validates the dataset, acquires the project shots lock, binds current inputs, then replaces:

1. `audio_intelligence.json`
2. `audio_intelligence_generation.json` as the final commit marker

The transaction holds stable descriptors for the project root, `data`, stage, and recovery directories; checks, replacements, rollback, and directory sync are descriptor-relative. After both replacements, it performs a full binding read against the same stable `data` descriptor before deleting the previous generation. If replacement, sync, or post-commit binding fails, the previous dataset and marker are restored. If restoration itself fails, the API reports failure and retains every unrestored old byte in a durable project-relative `.audio-intelligence-recovery-*` directory instead of deleting the only backup.

If the new generation is valid but deleting old recovery bytes fails, the generation remains available and the public binding reports `cleanup_required: true` plus project-relative `recovery_directories`. `cleanup_audio_intelligence_recovery()` first revalidates the current generation, rejects unsafe or unexpected recovery entries, and then retries cleanup under the project lock. Cleanup is never silently represented as complete.

Staging cleanup runs with guaranteed descriptor closure. A post-commit cleanup/sync failure returns the committed generation with `cleanup_required: true` and operation-scoped `cleanup_warnings`; it does not misreport the commit as rolled back. Those warnings describe the generating call, while subsequent binding reads report current persistent recovery directories. Safe file opens are nonblocking before the regular-file check so FIFOs and other special files fail closed rather than holding the project lock indefinitely.

The public `audio_intelligence.py` module is a stable façade. Schema/semantic validation, secret/path filtering, receipt/binding, and transaction/recovery live in separate private modules so each trust boundary can be tested independently. Missing, partial, forged, stale, unsafe, non-finite, or semantically invalid generations fail closed. The transaction does not modify any legacy audio artifact. [Shot/audio associations](audio-associations-v1.md) use an explicit report-generation v4 migration to bind this timeline; v3 reads remain supported when no audio-intelligence files are present.
