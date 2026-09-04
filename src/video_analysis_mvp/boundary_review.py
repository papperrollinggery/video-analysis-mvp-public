from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .safe_io import read_regular_bytes
from .schemas import Shot


BOUNDARY_REVIEW_SCHEMA_VERSION = 1
BOUNDARY_REVIEW_RECEIPT_TYPE = "video_boundary_human_review"
BOUNDARY_REVIEW_ASSERTION_POLICY = (
    "Local single-user operator assertion: each listed shot boundary was checked "
    "against the current bound video and timecode."
)
MAX_BOUNDARY_REVIEW_BYTES = 256 * 1024


def build_boundary_review_receipt(
    project: Path,
    shots: list[Shot],
    visual_binding: dict[str, Any],
    reviewed_shot_ids: set[str],
) -> dict[str, Any]:
    current_ids = [shot.shot_id for shot in shots]
    if not reviewed_shot_ids.issubset(set(current_ids)):
        raise ValueError("boundary review contains an unknown shot id")
    core = {
        "schema_version": BOUNDARY_REVIEW_SCHEMA_VERSION,
        "receipt_type": BOUNDARY_REVIEW_RECEIPT_TYPE,
        "project_id": project.name,
        "visual_generation_id": visual_binding.get("generation_id"),
        "visual_generation_receipt_sha256": visual_binding.get("receipt_sha256"),
        "reviewed_shot_ids": [shot_id for shot_id in current_ids if shot_id in reviewed_shot_ids],
        "assertion_policy": BOUNDARY_REVIEW_ASSERTION_POLICY,
    }
    return {**core, "receipt_digest": _digest(core)}


def validate_boundary_review_receipt(
    project: Path,
    shots: list[Shot],
    visual_binding: dict[str, Any] | None,
) -> dict[str, Any]:
    path = project / "data" / "boundary_review.json"
    if not os.path.lexists(path):
        return {
            "valid": False,
            "present": False,
            "reviewed_shot_ids": set(),
            "binding": None,
            "reasons": [],
        }
    try:
        raw = read_regular_bytes(path, root=project, max_bytes=MAX_BOUNDARY_REVIEW_BYTES)
        receipt = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        expected_fields = {
            "schema_version",
            "receipt_type",
            "project_id",
            "visual_generation_id",
            "visual_generation_receipt_sha256",
            "reviewed_shot_ids",
            "assertion_policy",
            "receipt_digest",
        }
        if type(receipt) is not dict or set(receipt) != expected_fields:
            raise ValueError("boundary review receipt fields are invalid")
        if receipt.get("schema_version") != BOUNDARY_REVIEW_SCHEMA_VERSION:
            raise ValueError("boundary review schema version is unsupported")
        if receipt.get("receipt_type") != BOUNDARY_REVIEW_RECEIPT_TYPE:
            raise ValueError("boundary review receipt type is invalid")
        if receipt.get("project_id") != project.name:
            raise ValueError("boundary review project binding is invalid")
        if receipt.get("assertion_policy") != BOUNDARY_REVIEW_ASSERTION_POLICY:
            raise ValueError("boundary review assertion policy is invalid")
        if not visual_binding:
            raise ValueError("boundary review requires a current visual generation")
        if (
            receipt.get("visual_generation_id") != visual_binding.get("generation_id")
            or receipt.get("visual_generation_receipt_sha256") != visual_binding.get("receipt_sha256")
        ):
            raise ValueError("boundary review visual generation binding is stale")
        reviewed = receipt.get("reviewed_shot_ids")
        if type(reviewed) is not list or any(type(item) is not str or not item for item in reviewed):
            raise ValueError("boundary review shot ids are invalid")
        if len(reviewed) != len(set(reviewed)):
            raise ValueError("boundary review shot ids must be unique")
        current_ids = [shot.shot_id for shot in shots]
        if any(item not in current_ids for item in reviewed):
            raise ValueError("boundary review contains an unknown shot id")
        if reviewed != [shot_id for shot_id in current_ids if shot_id in set(reviewed)]:
            raise ValueError("boundary review shot ids must follow current shot order")
        core = {key: value for key, value in receipt.items() if key != "receipt_digest"}
        if receipt.get("receipt_digest") != _digest(core):
            raise ValueError("boundary review receipt digest is invalid")
        receipt_sha256 = hashlib.sha256(raw).hexdigest()
        return {
            "valid": True,
            "present": True,
            "reviewed_shot_ids": set(reviewed),
            "binding": {
                "schema_version": BOUNDARY_REVIEW_SCHEMA_VERSION,
                "receipt_sha256": receipt_sha256,
                "receipt_digest": receipt["receipt_digest"],
                "reviewed_shot_count": len(reviewed),
            },
            "reasons": [],
        }
    except Exception as exc:
        return {
            "valid": False,
            "present": True,
            "reviewed_shot_ids": set(),
            "binding": None,
            "reasons": [f"boundary review receipt is invalid ({type(exc).__name__})"],
        }


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")
