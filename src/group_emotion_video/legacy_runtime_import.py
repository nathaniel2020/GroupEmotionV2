from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .ids import make_query_id, make_seed_id, make_video_uid
from .repositories import QueryRepository, VideoRepository


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

LEGACY_SCENE_MAP = {
    "high_flat": {"scene_title": "高约束-扁平", "norm_context": "High_Constraint", "power_gradient": "flat"},
    "high_mild": {"scene_title": "高约束-温和", "norm_context": "High_Constraint", "power_gradient": "mild"},
    "high_steep": {"scene_title": "高约束-陡峭", "norm_context": "High_Constraint", "power_gradient": "steep"},
    "high_extreme": {"scene_title": "高约束-极端", "norm_context": "High_Constraint", "power_gradient": "extreme"},
    "moderate_flat": {"scene_title": "中约束-扁平", "norm_context": "Moderate_Constraint", "power_gradient": "flat"},
    "moderate_mild": {"scene_title": "中约束-温和", "norm_context": "Moderate_Constraint", "power_gradient": "mild"},
    "moderate_steep": {"scene_title": "中约束-陡峭", "norm_context": "Moderate_Constraint", "power_gradient": "steep"},
    "moderate_extreme": {"scene_title": "中约束-极端", "norm_context": "Moderate_Constraint", "power_gradient": "extreme"},
    "low_flat": {"scene_title": "低约束-扁平", "norm_context": "Low_Constraint", "power_gradient": "flat"},
    "low_mild": {"scene_title": "低约束-温和", "norm_context": "Low_Constraint", "power_gradient": "mild"},
    "low_steep": {"scene_title": "低约束-陡峭", "norm_context": "Low_Constraint", "power_gradient": "steep"},
    "low_extreme": {"scene_title": "低约束-极端", "norm_context": "Low_Constraint", "power_gradient": "extreme"},
    "face_emotion": {"scene_title": "面子情绪", "norm_context": "overlay", "power_gradient": "overlay"},
    "relational_obligation": {"scene_title": "关系义务", "norm_context": "overlay", "power_gradient": "overlay"},
    "collective_honor": {"scene_title": "集体荣耻", "norm_context": "overlay", "power_gradient": "overlay"},
}


def _safe_dir_name(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text).strip("._") or "scene"


def _normalize_video_stem(stem: str) -> str:
    return re.sub(r"_p\d+$", "", stem, flags=re.IGNORECASE)


def _normalize_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value)).isoformat(timespec="seconds")
    text = str(value).strip()
    return text or None


def _default_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


def _file_size_bytes(path: Path) -> int:
    return int(path.stat().st_size)


def _move_file(source: Path, destination: Path, *, overwrite: bool) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not overwrite:
            return "reused_existing"
        if destination.is_file() or destination.is_symlink():
            destination.unlink()
        else:
            raise IsADirectoryError(f"destination is not a file: {destination}")
    shutil.move(str(source), str(destination))
    return "move"


class ProgressBar:
    def __init__(self, total: int, *, enabled: bool):
        self.total = max(int(total), 0)
        self.enabled = bool(enabled) and sys.stderr.isatty()
        self.current = 0

    def update(self, *, moved: int, skipped: int, scene: str | None) -> None:
        self.current += 1
        if not self.enabled or self.total <= 0:
            return
        width = 28
        ratio = min(max(self.current / self.total, 0.0), 1.0)
        filled = int(width * ratio)
        bar = "#" * filled + "-" * (width - filled)
        sys.stderr.write(
            "\r"
            f"[{bar}] {self.current}/{self.total} "
            f"{ratio * 100:5.1f}% moved={moved} skipped={skipped} scene={scene or '-'}"
        )
        sys.stderr.flush()

    def close(self) -> None:
        if self.enabled and self.total > 0:
            sys.stderr.write("\n")
            sys.stderr.flush()


@dataclass(slots=True)
class LegacyQuery:
    legacy_query_id: str
    query_text: str
    scene_id: str
    scene_title: str


class LegacyMetadataIndex:
    def __init__(self, runtime_root: Path):
        self.runtime_root = runtime_root
        self.query_by_legacy_id: dict[str, LegacyQuery] = {}
        self.video_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self.query_ids_by_video_uid: dict[str, list[str]] = {}
        self.scene_titles: dict[str, str] = {}
        self._load_keyword_db(runtime_root / "db" / "keyword_index.db")
        self._load_video_db(runtime_root / "db" / "video_meta.db")

    def _connect(self, path: Path) -> sqlite3.Connection | None:
        if not path.exists():
            return None
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_keyword_db(self, path: Path) -> None:
        conn = self._connect(path)
        if conn is None:
            return
        try:
            for row in conn.execute("SELECT scene_id, scene_title FROM scene_cells"):
                self.scene_titles[str(row["scene_id"])] = str(row["scene_title"])
            for row in conn.execute("SELECT query_id, query_text, scene_id, scene_title FROM query_nodes"):
                self.query_by_legacy_id[str(row["query_id"])] = LegacyQuery(
                    legacy_query_id=str(row["query_id"]),
                    query_text=str(row["query_text"]),
                    scene_id=str(row["scene_id"]),
                    scene_title=str(row["scene_title"]),
                )
        finally:
            conn.close()

    def _load_video_db(self, path: Path) -> None:
        conn = self._connect(path)
        if conn is None:
            return
        try:
            media_by_uid: dict[str, dict[str, Any]] = {}
            for row in conn.execute("SELECT * FROM media_signals"):
                media_by_uid[str(row["video_uid"])] = dict(row)

            query_ids_by_uid: dict[str, list[str]] = {}
            for row in conn.execute("SELECT video_uid, query_id, rank FROM video_query_links ORDER BY video_uid, rank, query_id"):
                query_ids_by_uid.setdefault(str(row["video_uid"]), []).append(str(row["query_id"]))
            self.query_ids_by_video_uid = query_ids_by_uid

            for row in conn.execute("SELECT * FROM video_assets"):
                item = dict(row)
                item["platform_metadata"] = json.loads(item.get("platform_metadata_json") or "{}")
                item["extra"] = json.loads(item.get("extra_json") or "{}")
                item["scene_tags"] = json.loads(item.get("scene_tags_json") or "{}")
                item["lineage_parent_ids"] = json.loads(item.get("lineage_parent_ids_json") or "[]")
                item["media_signals"] = media_by_uid.get(str(item["video_uid"]), {})
                scene_id = str(item.get("scene_id") or "")
                platform_video_id = str(item.get("platform_video_id") or "")
                if platform_video_id:
                    self.video_by_key[(scene_id, platform_video_id)] = item
        finally:
            conn.close()

    def lookup_video(self, *, scene_id: str, platform_video_id: str) -> dict[str, Any] | None:
        return self.video_by_key.get((scene_id, platform_video_id))

    def lookup_queries(self, video_item: dict[str, Any] | None) -> list[LegacyQuery]:
        if not video_item:
            return []
        video_uid = str(video_item.get("video_uid") or "")
        legacy_ids = list(self.query_ids_by_video_uid.get(video_uid, []))
        if not legacy_ids and video_item.get("query_id"):
            legacy_ids = [str(video_item["query_id"])]
        result: list[LegacyQuery] = []
        for legacy_id in legacy_ids:
            query = self.query_by_legacy_id.get(legacy_id)
            if query and query not in result:
                result.append(query)
        return result


class LegacyRuntimeImporter:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        query_repo: QueryRepository,
        video_repo: VideoRepository,
        layout: dict[str, Path],
        logger: Any,
    ):
        self.config = config
        self.query_repo = query_repo
        self.video_repo = video_repo
        self.layout = layout
        self.logger = logger
        self._query_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._query_cache_by_text: dict[str, dict[str, Any]] = {}
        self._next_excel_row = 950000

    def _video_artifact_dir(self, video_uid: str) -> Path:
        path = self.layout["artifact_videos"] / video_uid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_video_meta(self, payload: dict[str, Any], *, video_uid: str) -> Path:
        path = self._video_artifact_dir(video_uid) / "video_meta.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _probe_duration_sec(self, path: Path) -> float:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return round(float(str(result.stdout).strip()), 3)

    def _ensure_query(self, *, scene_text: str, trigger_text: str, query_text: str) -> dict[str, Any]:
        key = (scene_text, trigger_text, query_text)
        cached = self._query_cache.get(key)
        if cached is not None:
            return cached
        cached_by_text = self._query_cache_by_text.get(query_text)
        if cached_by_text is not None:
            self._query_cache[key] = cached_by_text
            return cached_by_text
        excel_row = self._next_excel_row
        self._next_excel_row += 1
        seed_id = make_seed_id(excel_row, scene_text, trigger_text)
        query_id = make_query_id(seed_id, query_text)
        record = {
            "query_id": query_id,
            "seed_id": seed_id,
            "excel_row": excel_row,
            "scene_text": scene_text,
            "trigger_text": trigger_text,
            "query_text": query_text,
            "status": "done",
            "last_run_at": datetime.now().isoformat(timespec="seconds"),
            "hit_count": 0,
            "run_count": 0,
        }
        self.query_repo.upsert(record)
        self._query_cache[key] = record
        self._query_cache_by_text[query_text] = record
        return record

    def _candidate_files(self, raw_root: Path, limit: int | None) -> list[Path]:
        files = [
            path
            for path in raw_root.rglob("*")
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and len(path.relative_to(raw_root).parts) >= 2
        ]
        files.sort()
        if limit is not None and int(limit) > 0:
            return files[: int(limit)]
        return files

    def import_runtime(
        self,
        legacy_runtime_root: str | Path,
        *,
        raw_root: str | Path | None = None,
        limit: int | None = None,
        replace_existing: bool = False,
        progress: bool = True,
    ) -> dict[str, Any]:
        runtime_root = Path(legacy_runtime_root).resolve()
        source_root = Path(raw_root).resolve() if raw_root else (runtime_root / "artifacts" / "raw_videos" / "bilibili")
        if not source_root.exists():
            raise FileNotFoundError(f"Legacy raw video root does not exist: {source_root}")

        metadata_index = LegacyMetadataIndex(runtime_root)
        files = self._candidate_files(source_root, limit)
        bar = ProgressBar(len(files), enabled=progress)
        summary = {
            "legacy_runtime_root": str(runtime_root),
            "source_root": str(source_root),
            "db_path": str(self.layout["db"]),
            "discovered_files": len(files),
            "imported_videos": 0,
            "replaced_videos": 0,
            "skipped_existing": 0,
            "moved_files": 0,
            "reused_existing_files": 0,
            "duration_probed_videos": 0,
        }

        try:
            for file_path in files:
                relative = file_path.relative_to(source_root)
                scene_id = relative.parts[0]
                scene_info = LEGACY_SCENE_MAP.get(scene_id, {})
                scene_text = str(metadata_index.scene_titles.get(scene_id) or scene_info.get("scene_title") or scene_id)
                platform_video_id = _normalize_video_stem(file_path.stem)
                video_uid = make_video_uid("bilibili", platform_video_id)

                existing = self.video_repo.exists(platform_video_id)
                if existing and not replace_existing:
                    summary["skipped_existing"] += 1
                    bar.update(moved=summary["moved_files"], skipped=summary["skipped_existing"], scene=scene_id)
                    continue

                video_item = metadata_index.lookup_video(scene_id=scene_id, platform_video_id=platform_video_id)
                query_items = metadata_index.lookup_queries(video_item)
                if query_items:
                    query_records = [
                        self._ensure_query(
                            scene_text=str(item.scene_title or scene_text),
                            trigger_text=str(item.query_text or ""),
                            query_text=str(item.query_text or ""),
                        )
                        for item in query_items
                    ]
                else:
                    fallback_query_text = f"legacy_import::{scene_id}"
                    query_records = [
                        self._ensure_query(
                            scene_text=scene_text,
                            trigger_text=fallback_query_text,
                            query_text=fallback_query_text,
                        )
                    ]

                primary_query = query_records[0]
                target_dir = self.layout["tmp_videos"] / _safe_dir_name(primary_query["scene_text"])
                target_path = target_dir / f"{platform_video_id}{file_path.suffix.lower()}"
                move_result = _move_file(file_path, target_path, overwrite=replace_existing)
                if move_result == "move":
                    summary["moved_files"] += 1
                else:
                    summary["reused_existing_files"] += 1

                platform_metadata = dict((video_item or {}).get("platform_metadata") or {})
                media_signals = dict((video_item or {}).get("media_signals") or {})
                extra = dict((video_item or {}).get("extra") or {})

                duration_sec = (video_item or {}).get("duration_sec")
                if duration_sec is None:
                    duration_sec = self._probe_duration_sec(target_path)
                    summary["duration_probed_videos"] += 1
                publish_time = _normalize_timestamp((video_item or {}).get("publish_time"))
                if publish_time is None:
                    publish_time = _normalize_timestamp(platform_metadata.get("publish_time"))

                tags = list(json.loads(media_signals.get("tag_texts_json") or "[]")) if media_signals.get("tag_texts_json") else []
                if not tags:
                    tags = [scene_id, scene_text]
                description = (
                    media_signals.get("description_text")
                    or platform_metadata.get("description")
                    or str((video_item or {}).get("title") or platform_video_id)
                )
                started_at = datetime.now().isoformat(timespec="seconds")
                payload = {
                    "video_uid": video_uid,
                    "platform": "bilibili",
                    "bvid": platform_video_id,
                    "url": str((video_item or {}).get("url") or _default_url(platform_video_id)),
                    "title": str((video_item or {}).get("title") or platform_video_id),
                    "uploader": (video_item or {}).get("uploader"),
                    "publish_time": publish_time,
                    "duration_sec": float(duration_sec) if duration_sec is not None else None,
                    "cover_url": (video_item or {}).get("cover_url"),
                    "description": str(description),
                    "tags": [str(item) for item in tags if str(item).strip()],
                    "categories": [scene_id],
                    "view_count": platform_metadata.get("view_count"),
                    "like_count": platform_metadata.get("like_count"),
                    "comment_count": platform_metadata.get("comment_count"),
                    "play_count": platform_metadata.get("play_count"),
                    "danmaku_count": platform_metadata.get("danmaku_count"),
                    "type": platform_metadata.get("type"),
                    "webpage_url": str((video_item or {}).get("url") or _default_url(platform_video_id)),
                    "query_id": primary_query["query_id"],
                    "scene_text": primary_query["scene_text"],
                    "trigger_text": primary_query["trigger_text"],
                    "source_excel_row": primary_query["excel_row"],
                    "subtitles_available": bool(extra.get("subtitle_segments")),
                    "rights_status": str(platform_metadata.get("rights_status") or "unknown"),
                    "download_status": "downloaded",
                    "download_started_at": started_at,
                    "download_finished_at": started_at,
                    "download_elapsed_sec": 0.0,
                    "reject_reason": None,
                    "retention_status": None,
                    "accepted_clip_count": 0,
                    "preprocess_started_at": None,
                    "preprocess_finished_at": None,
                    "preprocess_elapsed_sec": None,
                    "raw_video_path": str(target_path),
                    "platform_metadata": {
                        **platform_metadata,
                        "legacy_scene_id": scene_id,
                        "legacy_runtime_import": True,
                    },
                    "extra": {
                        **extra,
                        "legacy_runtime_import": {
                            "source_path": str(file_path),
                            "source_runtime_root": str(runtime_root),
                            "scene_id": scene_id,
                            "query_texts": [record["query_text"] for record in query_records],
                        }
                    },
                }
                video_meta_path = self._write_video_meta(payload, video_uid=video_uid)
                self.video_repo.upsert({**payload, "video_meta_path": str(video_meta_path)})
                self.video_repo.add_download_event(
                    video_uid,
                    "downloaded",
                    local_path=str(target_path),
                    file_size_bytes=_file_size_bytes(target_path),
                    extra={
                        "import_mode": "legacy_runtime_move",
                        "legacy_scene_id": scene_id,
                    },
                )

                if existing and replace_existing:
                    summary["replaced_videos"] += 1
                else:
                    summary["imported_videos"] += 1
                self.logger.info(
                    "legacy_runtime_imported video_uid=%s bvid=%s scene_id=%s source=%s target=%s",
                    video_uid,
                    platform_video_id,
                    scene_id,
                    file_path,
                    target_path,
                )
                bar.update(moved=summary["moved_files"], skipped=summary["skipped_existing"], scene=scene_id)
        finally:
            bar.close()

        return summary
