from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class QueryRecord:
    query_id: str
    seed_id: str
    excel_row: int
    scene_text: str
    trigger_text: str
    query_text: str
    status: str = "pending"
    last_run_at: str | None = None
    hit_count: int = 0


@dataclass(slots=True)
class CandidateVideo:
    platform: str
    platform_video_id: str
    url: str
    title: str
    uploader: str | None = None
    publish_time: str | None = None
    duration_sec: float | None = None
    cover_url: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    subtitles_available: bool = False
    platform_metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DownloadResult:
    status: str
    local_path: str | None = None
    file_size_bytes: int | None = None
    error_message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
