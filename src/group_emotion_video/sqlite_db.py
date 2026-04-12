from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class SQLiteDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return cursor

    def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> None:
        with self._lock:
            self.conn.executemany(sql, params)
            self.conn.commit()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    @staticmethod
    def to_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def from_json(value: str | None, default: Any) -> Any:
        if not value:
            return default
        return json.loads(value)


def initialize_schema(db: SQLiteDB) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS queries (
            query_id TEXT PRIMARY KEY,
            seed_id TEXT NOT NULL,
            excel_row INTEGER NOT NULL,
            scene_text TEXT NOT NULL,
            trigger_text TEXT NOT NULL,
            query_text TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            last_run_at TEXT,
            hit_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            video_uid TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            bvid TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            uploader TEXT,
            publish_time TEXT,
            duration_sec REAL,
            cover_url TEXT,
            description TEXT,
            tags_json TEXT NOT NULL,
            categories_json TEXT NOT NULL,
            view_count INTEGER,
            like_count INTEGER,
            comment_count INTEGER,
            play_count INTEGER,
            danmaku_count INTEGER,
            type TEXT,
            webpage_url TEXT,
            query_id TEXT NOT NULL,
            scene_text TEXT NOT NULL,
            trigger_text TEXT NOT NULL,
            source_excel_row INTEGER NOT NULL,
            subtitles_available INTEGER NOT NULL DEFAULT 0,
            rights_status TEXT NOT NULL,
            download_status TEXT NOT NULL,
            reject_reason TEXT,
            retention_status TEXT,
            accepted_clip_count INTEGER NOT NULL DEFAULT 0,
            raw_video_path TEXT,
            video_meta_path TEXT,
            platform_metadata_json TEXT NOT NULL,
            extra_json TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS clips (
            clip_uid TEXT PRIMARY KEY,
            video_uid TEXT NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            duration_sec REAL NOT NULL,
            segmentation_mode TEXT NOT NULL,
            filter_status TEXT NOT NULL,
            filter_reasons_json TEXT NOT NULL,
            retention_status TEXT,
            clip_path TEXT,
            manifest_path TEXT,
            keyframes_manifest_path TEXT,
            subtitle_path TEXT,
            scores_json TEXT NOT NULL,
            annotation_status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS annotations (
            annotation_uid TEXT PRIMARY KEY,
            clip_uid TEXT NOT NULL,
            status TEXT NOT NULL,
            review_required INTEGER NOT NULL DEFAULT 0,
            final_annotation_json TEXT NOT NULL,
            field_confidence_json TEXT NOT NULL,
            quality_flags_json TEXT NOT NULL,
            artifact_path TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS download_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_uid TEXT NOT NULL,
            status TEXT NOT NULL,
            local_path TEXT,
            file_size_bytes INTEGER,
            error_message TEXT,
            extra_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
