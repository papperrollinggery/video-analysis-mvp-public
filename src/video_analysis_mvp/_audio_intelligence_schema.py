from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from ._audio_intelligence_metadata import (
    MAX_DIAGNOSTIC_BYTES,
    bounded_text,
    optional_metadata,
)

AUDIO_TIMELINE_SCHEMA_ID = "audio-timeline/v1"
AUDIO_TIME_RANGE_SEMANTICS = "[start,end)"
CAPABILITIES = frozenset(
    {"baseline_features", "asr", "diarization", "separation", "classification"}
)
CAPABILITY_STATUSES = frozenset({"produced", "unknown", "failed", "skipped"})
SOURCE_TYPES = frozenset({"measured", "deterministic_detector", "adapter", "imported"})
EVENT_KINDS = frozenset({"voice", "music", "sfx", "silence", "mixed"})
VOICE_ROLES = frozenset({"voice_over", "dialogue", "singing", "unknown"})
PROPOSAL_VERIFICATIONS = frozenset(
    {"measured", "machine_estimated", "model_interpreted"}
)
REVIEW_STATUSES = frozenset({"reviewed", "rejected", "needs_work"})
IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
ANONYMOUS_SPEAKER_PATTERN = re.compile(r"(?:speaker|spk|cluster)[-_][0-9]{1,6}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

CAPABILITY_EVENT_KINDS = {
    "baseline_features": EVENT_KINDS,
    "asr": frozenset({"voice"}),
    "diarization": frozenset({"voice"}),
    "separation": frozenset({"voice", "music", "sfx", "mixed"}),
    "classification": frozenset({"voice", "music", "sfx", "mixed"}),
}
SOURCE_TYPE_CAPABILITIES = {
    "measured": frozenset({"baseline_features"}),
    "deterministic_detector": frozenset({"baseline_features", "classification"}),
    "adapter": frozenset({"asr", "diarization", "separation", "classification"}),
    "imported": CAPABILITIES,
}
SOURCE_TYPE_PROPOSAL_VERIFICATIONS = {
    "measured": frozenset({"measured"}),
    "deterministic_detector": frozenset({"measured", "machine_estimated"}),
    "adapter": frozenset({"machine_estimated", "model_interpreted"}),
    "imported": frozenset({"measured"}),
}

DATASET_KEYS = frozenset(
    {
        "schema_id",
        "time_range_semantics",
        "media_duration_seconds",
        "sources",
        "capabilities",
        "events",
    }
)
SOURCE_KEYS = frozenset(
    {
        "source_id",
        "capability",
        "source_type",
        "adapter",
        "adapter_version",
        "engine",
        "engine_version",
        "model",
        "device",
        "status",
        "diagnostics",
    }
)
CAPABILITY_RESULT_KEYS = frozenset({"status", "source_id", "reason"})
EVENT_KEYS = frozenset(
    {"event_id", "start_time", "end_time", "kind", "source_id", "proposal", "review"}
)
PROPOSAL_KEYS = frozenset(
    {
        "label",
        "text",
        "language",
        "speaker_id",
        "voice_role",
        "energy",
        "onset_density",
        "estimated_bpm",
        "confidence",
        "verification",
    }
)
REVIEW_KEYS = frozenset(
    {"status", "expected_proposal_sha256", "overrides", "review_notes", "verification"}
)


def validate_audio_timeline(dataset: Any) -> dict[str, Any]:
    if type(dataset) is not dict or set(dataset) != DATASET_KEYS:
        raise ValueError("audio timeline fields are invalid")
    if dataset.get("schema_id") != AUDIO_TIMELINE_SCHEMA_ID:
        raise ValueError("audio timeline schema_id is unsupported")
    if dataset.get("time_range_semantics") != AUDIO_TIME_RANGE_SEMANTICS:
        raise ValueError("audio timeline time range semantics are unsupported")
    duration = _finite_number(
        dataset.get("media_duration_seconds"),
        "media_duration_seconds",
        minimum=0.0,
        strictly_greater=True,
    )

    raw_sources = dataset.get("sources")
    if type(raw_sources) is not list:
        raise ValueError("audio timeline sources must be a list")
    sources = [_validate_source(item, index) for index, item in enumerate(raw_sources)]
    source_ids = [item["source_id"] for item in sources]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("audio timeline source ids must be unique")
    source_by_id = {item["source_id"]: item for item in sources}

    capabilities = _validate_capabilities(dataset.get("capabilities"), source_by_id)
    _validate_source_capability_consistency(sources, capabilities)

    raw_events = dataset.get("events")
    if type(raw_events) is not list:
        raise ValueError("audio timeline events must be a list")
    events = [
        _validate_event(
            item,
            index,
            duration=duration,
            source_by_id=source_by_id,
            capabilities=capabilities,
        )
        for index, item in enumerate(raw_events)
    ]
    event_ids = [item["event_id"] for item in events]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("audio timeline event ids must be unique")
    expected_order = sorted(
        events,
        key=lambda item: (item["start_time"], item["end_time"], item["event_id"]),
    )
    if events != expected_order:
        raise ValueError(
            "audio timeline events must use deterministic time and id order"
        )
    return {
        "schema_id": AUDIO_TIMELINE_SCHEMA_ID,
        "time_range_semantics": AUDIO_TIME_RANGE_SEMANTICS,
        "media_duration_seconds": duration,
        "sources": sources,
        "capabilities": capabilities,
        "events": events,
    }


def proposal_sha256(proposal: Any) -> str:
    validated = _validate_proposal(proposal, event_kind=None)
    return hashlib.sha256(canonical_json_bytes(validated)).hexdigest()


def resolve_effective_proposal(event: Any) -> dict[str, Any] | None:
    if type(event) is not dict:
        raise ValueError("audio event must be an object")
    proposal = _validate_proposal(event.get("proposal"), event_kind=event.get("kind"))
    review = _validate_review(
        event.get("review"), proposal, event_kind=event.get("kind")
    )
    if review is None:
        return proposal
    if review["status"] != "reviewed":
        return None
    effective = dict(proposal)
    effective.update(review["overrides"])
    effective["verification"] = "human_reviewed"
    return effective


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_sha256(value: Any, label: str) -> str:
    if type(value) is not str or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def validate_capabilities(
    value: Any,
    source_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    return _validate_capabilities(value, source_by_id)


def _validate_source(item: Any, index: int) -> dict[str, Any]:
    if type(item) is not dict or set(item) != SOURCE_KEYS:
        raise ValueError(f"audio source {index} fields are invalid")
    source_id = _identifier(item.get("source_id"), f"audio source {index} source_id")
    capability = item.get("capability")
    source_type = item.get("source_type")
    status = item.get("status")
    if type(capability) is not str or capability not in CAPABILITIES:
        raise ValueError(f"audio source {source_id} capability is unsupported")
    if type(source_type) is not str or source_type not in SOURCE_TYPES:
        raise ValueError(f"audio source {source_id} source_type is unsupported")
    if capability not in SOURCE_TYPE_CAPABILITIES[source_type]:
        raise ValueError(
            f"audio source {source_id} source_type is incompatible with {capability}"
        )
    if type(status) is not str or status not in CAPABILITY_STATUSES:
        raise ValueError(f"audio source {source_id} status is unsupported")
    diagnostics = item.get("diagnostics")
    if type(diagnostics) is not list:
        raise ValueError(f"audio source {source_id} diagnostics must be a list")
    normalized_diagnostics = [
        bounded_text(
            value,
            f"audio source {source_id} diagnostic",
            maximum=MAX_DIAGNOSTIC_BYTES,
            forbid_private_path=True,
            forbid_sensitive_value=True,
        )
        for value in diagnostics
    ]
    return {
        "source_id": source_id,
        "capability": capability,
        "source_type": source_type,
        "adapter": optional_metadata(item.get("adapter"), "adapter"),
        "adapter_version": optional_metadata(
            item.get("adapter_version"), "adapter_version"
        ),
        "engine": optional_metadata(item.get("engine"), "engine"),
        "engine_version": optional_metadata(
            item.get("engine_version"), "engine_version"
        ),
        "model": optional_metadata(item.get("model"), "model"),
        "device": optional_metadata(item.get("device"), "device"),
        "status": status,
        "diagnostics": normalized_diagnostics,
    }


def _validate_capabilities(
    value: Any,
    source_by_id: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if type(value) is not dict or set(value) != CAPABILITIES:
        raise ValueError(
            "audio timeline capabilities must contain every supported capability"
        )
    result: dict[str, dict[str, Any]] = {}
    for capability in sorted(CAPABILITIES):
        item = value.get(capability)
        if type(item) is not dict or set(item) != CAPABILITY_RESULT_KEYS:
            raise ValueError(f"audio capability fields are invalid: {capability}")
        status = item.get("status")
        if type(status) is not str or status not in CAPABILITY_STATUSES:
            raise ValueError(f"audio capability status is invalid: {capability}")
        source_id = item.get("source_id")
        if source_id is not None:
            source_id = _identifier(
                source_id, f"audio capability {capability} source_id"
            )
        reason = item.get("reason")
        if reason is not None:
            reason = bounded_text(
                reason,
                f"audio capability {capability} reason",
                maximum=MAX_DIAGNOSTIC_BYTES,
                forbid_private_path=True,
                forbid_sensitive_value=True,
            )
        if status == "produced" and source_id is None:
            raise ValueError(
                f"produced audio capability requires source_id: {capability}"
            )
        if status == "produced" and reason is not None:
            raise ValueError(
                f"produced audio capability must not contain a reason: {capability}"
            )
        if status in {"unknown", "failed", "skipped"} and reason is None:
            raise ValueError(
                f"non-produced audio capability requires reason: {capability}"
            )
        if status in {"unknown", "skipped"} and source_id is not None:
            raise ValueError(
                f"audio capability status must not select a source: {capability}"
            )
        if status == "failed" and source_id is None:
            raise ValueError(
                f"failed audio capability requires source_id: {capability}"
            )
        if source_by_id is not None and source_id is not None:
            source = source_by_id.get(source_id)
            if source is None or source["capability"] != capability:
                raise ValueError(
                    f"audio capability source binding is invalid: {capability}"
                )
            if source["status"] != status:
                raise ValueError(
                    f"audio capability/source status is inconsistent: {capability}"
                )
        result[capability] = {
            "status": status,
            "source_id": source_id,
            "reason": reason,
        }
    return result


def _validate_source_capability_consistency(
    sources: list[dict[str, Any]], capabilities: dict[str, dict[str, Any]]
) -> None:
    for source in sources:
        if source["status"] != "produced":
            continue
        result = capabilities[source["capability"]]
        if result["status"] != "produced" or result["source_id"] != source["source_id"]:
            raise ValueError(
                f"produced audio source is not the selected capability source: {source['source_id']}"
            )


def _validate_event(
    item: Any,
    index: int,
    *,
    duration: float,
    source_by_id: dict[str, dict[str, Any]],
    capabilities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if type(item) is not dict or set(item) != EVENT_KEYS:
        raise ValueError(f"audio event {index} fields are invalid")
    event_id = _identifier(item.get("event_id"), f"audio event {index} event_id")
    start = _finite_number(
        item.get("start_time"), f"audio event {event_id} start_time", minimum=0.0
    )
    end = _finite_number(
        item.get("end_time"), f"audio event {event_id} end_time", minimum=0.0
    )
    if start >= end:
        raise ValueError(f"audio event {event_id} must satisfy start_time < end_time")
    if end > duration:
        raise ValueError(f"audio event {event_id} exceeds media duration")
    kind = item.get("kind")
    if type(kind) is not str or kind not in EVENT_KINDS:
        raise ValueError(f"audio event kind is unsupported: {event_id}")
    source_id = _identifier(item.get("source_id"), f"audio event {event_id} source_id")
    source = source_by_id.get(source_id)
    if source is None:
        raise ValueError(f"audio event references an unknown source: {event_id}")
    if (
        source["status"] != "produced"
        or capabilities[source["capability"]]["status"] != "produced"
    ):
        raise ValueError(f"audio event source capability is not produced: {event_id}")
    if capabilities[source["capability"]]["source_id"] != source_id:
        raise ValueError(
            f"audio event does not use the selected capability source: {event_id}"
        )
    if kind not in CAPABILITY_EVENT_KINDS[source["capability"]]:
        raise ValueError(
            f"audio event kind is incompatible with source capability: {event_id}"
        )
    proposal = _validate_proposal(item.get("proposal"), event_kind=kind, source=source)
    review = _validate_review(item.get("review"), proposal, event_kind=kind)
    return {
        "event_id": event_id,
        "start_time": start,
        "end_time": end,
        "kind": kind,
        "source_id": source_id,
        "proposal": proposal,
        "review": review,
    }


def _validate_proposal(
    value: Any,
    *,
    event_kind: Any,
    source: dict[str, Any] | None = None,
    human_assertion: bool = False,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != PROPOSAL_KEYS:
        raise ValueError("audio event proposal fields are invalid")
    label = bounded_text(value.get("label"), "audio proposal label")
    text = bounded_text(value.get("text"), "audio proposal text")
    language = bounded_text(
        value.get("language"), "audio proposal language", allow_empty=False
    )
    speaker_id = value.get("speaker_id")
    if speaker_id is not None and (
        type(speaker_id) is not str
        or ANONYMOUS_SPEAKER_PATTERN.fullmatch(speaker_id) is None
    ):
        raise ValueError("audio proposal speaker_id must be an anonymous cluster id")
    voice_role = value.get("voice_role")
    if type(voice_role) is not str or voice_role not in VOICE_ROLES:
        raise ValueError("audio proposal voice_role is unsupported")
    if event_kind not in {None, "voice", "mixed"} and speaker_id is not None:
        raise ValueError("speaker_id is allowed only for voice or mixed events")
    if event_kind not in {None, "voice", "mixed"} and text:
        raise ValueError("transcript text is allowed only for voice or mixed events")
    if event_kind not in {None, "voice", "mixed"} and voice_role != "unknown":
        raise ValueError("voice_role is allowed only for voice or mixed events")
    if event_kind not in {None, "voice", "mixed"} and language != "unknown":
        raise ValueError("non-voice audio proposal language must be unknown")
    verification = value.get("verification")
    if type(verification) is not str or verification not in PROPOSAL_VERIFICATIONS:
        raise ValueError("audio proposal verification is unsupported")
    if not human_assertion and voice_role != "unknown" and verification == "measured":
        raise ValueError("unreviewed voice_role must remain an estimate, not measured")
    if (
        source is not None
        and verification
        not in SOURCE_TYPE_PROPOSAL_VERIFICATIONS[source["source_type"]]
    ):
        raise ValueError(
            "audio proposal verification is incompatible with its source type"
        )
    normalized = {
        "label": label,
        "text": text,
        "language": language,
        "speaker_id": speaker_id,
        "voice_role": voice_role,
        "energy": _optional_finite_number(
            value.get("energy"), "audio proposal energy", minimum=0.0, maximum=1.0
        ),
        "onset_density": _optional_finite_number(
            value.get("onset_density"), "audio proposal onset_density", minimum=0.0
        ),
        "estimated_bpm": _optional_finite_number(
            value.get("estimated_bpm"),
            "audio proposal estimated_bpm",
            minimum=20.0,
            maximum=300.0,
        ),
        "confidence": _finite_number(
            value.get("confidence"),
            "audio proposal confidence",
            minimum=0.0,
            maximum=1.0,
        ),
        "verification": verification,
    }
    if source is not None:
        _validate_capability_field_ownership(normalized, source["capability"])
    return normalized


def _validate_capability_field_ownership(
    proposal: dict[str, Any], capability: str
) -> None:
    acoustic_fields = ("energy", "onset_density", "estimated_bpm")
    if capability != "baseline_features" and any(
        proposal[field] is not None for field in acoustic_fields
    ):
        raise ValueError(
            f"audio proposal acoustic fields are not owned by {capability}"
        )
    if capability != "asr" and proposal["text"]:
        raise ValueError(f"audio proposal transcript text is not owned by {capability}")
    if capability != "asr" and proposal["language"] != "unknown":
        raise ValueError(f"audio proposal language is not owned by {capability}")
    if capability != "diarization" and proposal["speaker_id"] is not None:
        raise ValueError(f"audio proposal speaker_id is not owned by {capability}")
    if capability == "diarization" and proposal["speaker_id"] is None:
        raise ValueError("diarization proposal requires an anonymous speaker_id")
    if capability != "classification" and proposal["voice_role"] != "unknown":
        raise ValueError(f"audio proposal voice_role is not owned by {capability}")


def _validate_review(
    value: Any,
    proposal: dict[str, Any],
    *,
    event_kind: Any,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != REVIEW_KEYS:
        raise ValueError("audio event review fields are invalid")
    status = value.get("status")
    if type(status) is not str or status not in REVIEW_STATUSES:
        raise ValueError("audio event review status is unsupported")
    expected = value.get("expected_proposal_sha256")
    validate_sha256(expected, "audio event expected proposal digest")
    if expected != hashlib.sha256(canonical_json_bytes(proposal)).hexdigest():
        raise ValueError("audio event review proposal binding is stale")
    overrides = value.get("overrides")
    if type(overrides) is not dict or not set(overrides).issubset(
        PROPOSAL_KEYS - {"verification"}
    ):
        raise ValueError("audio event review overrides are invalid")
    candidate = dict(proposal)
    candidate.update(overrides)
    candidate["verification"] = proposal["verification"]
    normalized_candidate = _validate_proposal(
        candidate, event_kind=event_kind, human_assertion=True
    )
    normalized_overrides = {key: normalized_candidate[key] for key in overrides}
    notes = bounded_text(value.get("review_notes"), "audio event review_notes")
    expected_verification = (
        "human_draft" if status == "needs_work" else "human_reviewed"
    )
    if value.get("verification") != expected_verification:
        raise ValueError(
            f"audio event review verification must be {expected_verification}"
        )
    if status == "rejected" and normalized_overrides:
        raise ValueError("rejected audio events must not contain effective overrides")
    return {
        "status": status,
        "expected_proposal_sha256": expected,
        "overrides": normalized_overrides,
        "review_notes": notes,
        "verification": expected_verification,
    }


def _identifier(value: Any, label: str) -> str:
    if type(value) is not str or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _finite_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strictly_greater: bool = False,
) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if minimum is not None:
        if strictly_greater and result <= minimum:
            raise ValueError(f"{label} must be greater than {minimum}")
        if not strictly_greater and result < minimum:
            raise ValueError(f"{label} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{label} must be at most {maximum}")
    return result


def _optional_finite_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label, minimum=minimum, maximum=maximum)
