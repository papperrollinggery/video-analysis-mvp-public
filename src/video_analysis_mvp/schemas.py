from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class AnalysisProfile(str, Enum):
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
    transition_in: str = "cut"
    transition_out: str = "cut"
    shot_scale: str = "unknown"
    camera_angle: str = "unknown"
    camera_motion: str = "unknown"
    lens: str = "TBD"
    equipment: str = "TBD"
    composition: str = "unknown"
    subject: str = "unknown"
    action: str = "unknown"
    location: str = "unknown"
    int_ext: str = "unknown"
    props: str = ""
    visual_description: str = ""
    content_summary: str = ""
    scene_type: str = ""
    style_notes: str = ""
    prompt_en: str = ""
    prompt_zh: str = ""
    direction_notes: str = ""
    lighting_vfx: str = ""
    palette: list[str] = Field(default_factory=list)
    onscreen_text: str = ""
    dialogue: str = ""
    speech_summary: str = ""
    sound_design: str = "unknown"
    sound_sync: str = "sync"
    audio_notes: str = ""
    music_state: str = "unknown"
    beat_density: float = 0.0
    rhythm_notes: str = ""
    motifs: list[str] = Field(default_factory=list)
    continuity_notes: str = ""
    preferred_take: str = ""
    estimated_production_time: str = ""
    shoot_day: str = ""
    review_notes: str = ""
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
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _jsonable(model_or_data)
    path.write_text(__import__("json").dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return __import__("json").loads(path.read_text(encoding="utf-8"))


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
