from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Literal, get_type_hints

from .safe_io import atomic_write_bytes, read_regular_bytes

try:
    from pydantic import BaseModel, Field
except ModuleNotFoundError:  # pragma: no cover - exercised when deps are not installed
    class _FieldValue:
        def __init__(self, default: Any = None, default_factory: Any = None) -> None:
            self.default = default
            self.default_factory = default_factory

        def value(self) -> Any:
            if self.default_factory is not None:
                return self.default_factory()
            return self.default

    def Field(default: Any = None, default_factory: Any = None) -> _FieldValue:
        return _FieldValue(default=default, default_factory=default_factory)

    class BaseModel:
        def __init__(self, **data: Any) -> None:
            for name, annotation in self._fields().items():
                if name in data:
                    value = data[name]
                else:
                    default = getattr(type(self), name, None)
                    value = default.value() if isinstance(default, _FieldValue) else default
                setattr(self, name, self._coerce(annotation, value))

        @classmethod
        def model_validate(cls, data: Any) -> Any:
            if isinstance(data, cls):
                return data
            if isinstance(data, dict):
                return cls(**data)
            raise TypeError(f"Cannot validate {type(data).__name__} as {cls.__name__}")

        def model_dump(self, mode: str | None = None) -> dict[str, Any]:
            return {name: _jsonable(getattr(self, name)) for name in self._fields()}

        @classmethod
        def _fields(cls) -> dict[str, Any]:
            try:
                return {key: value for key, value in get_type_hints(cls).items() if not key.startswith("_")}
            except Exception:
                pass
            fields: dict[str, Any] = {}
            for base in reversed(cls.__mro__):
                fields.update(getattr(base, "__annotations__", {}))
            return {key: value for key, value in fields.items() if not key.startswith("_")}

        @staticmethod
        def _coerce(annotation: Any, value: Any) -> Any:
            try:
                if isinstance(annotation, type) and issubclass(annotation, Enum) and not isinstance(value, annotation):
                    return annotation(value)
            except TypeError:
                pass
            return value


class AnalysisProfile(str, Enum):
    research = "research"
    ads = "ads"
    streaming = "streaming"
    shortform = "shortform"
    festival = "festival"


class SourceType(str, Enum):
    file = "file"
    url = "url"


class StatusEnvelope(BaseModel):
    status: Literal["success", "warning", "error"]
    summary: str
    next_actions: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)
    error: str | None = None


class CanonicalMediaPackage(BaseModel):
    project_id: str
    source_type: SourceType
    source: str
    local_master_path: str
    review_copy_path: str
    audio_path: str
    duration_seconds: float
    frame_rate: float
    resolution: str
    aspect_ratio: float
    status: Literal["created", "analyzed", "error"]
    analysis_profile: AnalysisProfile
    metadata: dict[str, Any] = Field(default_factory=dict)


class Shot(BaseModel):
    scene_no: str = ""
    shot_id: str
    shot_no: int = 0
    setup_id: str = ""
    take_no: str = ""
    start_time: float
    end_time: float
    duration: float
    timecode: str = ""
    frame_ref: str = ""
    primary_frame_ref: str = ""
    frame_refs: list[str] = Field(default_factory=list)
    boundary_confidence: str = "low"
    transition_in: str = "cut"
    transition_out: str = "cut"
    shot_scale: str = "unknown"
    camera_angle: str = "unknown"
    camera_motion: str = "unknown"
    lens: str = "TBD"
    equipment: str = "TBD"
    composition: str = "unknown"
    subject: str = "unknown"
    subject_zh: str = ""
    action: str = "unknown"
    action_zh: str = ""
    location: str = "unknown"
    int_ext: str = "unknown"
    props: str = ""
    visual_description: str = ""
    content_summary: str = ""
    content_summary_zh: str = ""
    scene_type: str = ""
    story_beat: str = ""
    style_notes: str = ""
    style_notes_zh: str = ""
    prompt_en: str = ""
    prompt_zh: str = ""
    remake_notes: str = ""
    remake_notes_zh: str = ""
    direction_notes: str = ""
    direction_notes_zh: str = ""
    lighting_vfx: str = ""
    palette: list[str] = Field(default_factory=list)
    onscreen_text: str = ""
    dialogue: str = ""
    speech_summary: str = ""
    sound_design: str = "unknown"
    sound_sync: str = "sync"
    audio_notes: str = ""
    sound_rhythm: str = ""
    music_state: str = "unknown"
    beat_density: float = 0.0
    rhythm_notes: str = ""
    motifs: list[str] = Field(default_factory=list)
    continuity_notes: str = ""
    preferred_take: str = ""
    estimated_production_time: str = ""
    shoot_day: str = ""
    review_notes: str = ""
    annotation_source: str = "machine"
    visual_confidence: float = 0.0
    readiness_status: str = "draft"
    readiness_reasons: list[str] = Field(default_factory=list)
    professional_ready: bool = False
    confidence: float = 0.35


class Scene(BaseModel):
    scene_id: str
    start_time: float
    end_time: float
    shot_ids: list[str]
    scene_function: str = "unknown"
    pace_label: str = "unknown"
    confidence: float = 0.35


class TranscriptSegment(BaseModel):
    segment_id: str
    start_time: float
    end_time: float
    text: str
    language: str = "unknown"
    speaker: str = "unknown"
    confidence: float = 0.0


class BeatEvent(BaseModel):
    time: float
    strength: float
    source: str = "energy_peak"
    confidence: float = 0.45


class MusicProfile(BaseModel):
    start_time: float
    end_time: float
    energy_level: str
    tempo_bucket: str
    style_tags: list[str] = Field(default_factory=list)
    mood_tags: list[str] = Field(default_factory=list)
    confidence: float = 0.35


class AnalysisReport(BaseModel):
    project_id: str
    profile: AnalysisProfile
    summary: str
    technical: dict[str, Any]
    visual_observations: list[str]
    audio_observations: list[str]
    rhythm_observations: list[str]
    client_takeaways: list[str]
    artifacts: dict[str, str]


class ProjectManifest(BaseModel):
    project_id: str
    profile: AnalysisProfile
    root_path: str
    source: str
    status: str
    artifacts: dict[str, str]


def dump_json(path: Path, model_or_data: BaseModel | list[BaseModel] | dict[str, Any] | list[Any]) -> None:
    data = _jsonable(model_or_data)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    atomic_write_bytes(path, payload)


def load_json(path: Path) -> Any:
    return json.loads(
        read_regular_bytes(path).decode("utf-8"),
        parse_constant=_reject_json_constant,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value
