from __future__ import annotations

from datetime import datetime
from typing import Any

from .sqlite_db import SQLiteDB


class QueryRepository:
    def __init__(self, db: SQLiteDB):
        self.db = db

    def upsert(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO queries (query_id, seed_id, excel_row, scene_text, trigger_text, query_text, status, last_run_at, hit_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_id) DO UPDATE SET
              seed_id=excluded.seed_id,
              excel_row=excluded.excel_row,
              scene_text=excluded.scene_text,
              trigger_text=excluded.trigger_text,
              query_text=excluded.query_text,
              status=excluded.status,
              last_run_at=excluded.last_run_at,
              hit_count=excluded.hit_count
            """,
            (
                record["query_id"],
                record["seed_id"],
                int(record["excel_row"]),
                record["scene_text"],
                record["trigger_text"],
                record["query_text"],
                record.get("status", "pending"),
                record.get("last_run_at"),
                int(record.get("hit_count", 0)),
            ),
        )

    def list_pending(self, limit: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT * FROM queries
            WHERE status IN ('pending', 'retry')
            ORDER BY COALESCE(last_run_at, ''), excel_row, query_text
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(row) for row in rows]

    def mark_done(self, query_id: str, hit_count: int) -> None:
        self.db.execute(
            "UPDATE queries SET status='done', last_run_at=?, hit_count=? WHERE query_id=?",
            (datetime.now().isoformat(timespec="seconds"), int(hit_count), query_id),
        )

    def count(self) -> int:
        row = self.db.fetchone("SELECT COUNT(*) AS count FROM queries")
        return int(row["count"]) if row else 0


class VideoRepository:
    def __init__(self, db: SQLiteDB):
        self.db = db

    def exists(self, bvid: str) -> bool:
        row = self.db.fetchone("SELECT 1 FROM videos WHERE bvid=?", (bvid,))
        return row is not None

    def upsert(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO videos (
              video_uid, platform, bvid, url, title, uploader, publish_time, duration_sec, cover_url, description,
              tags_json, categories_json, view_count, like_count, comment_count, play_count, danmaku_count, type,
              webpage_url, query_id, scene_text, trigger_text, source_excel_row, subtitles_available, rights_status,
              download_status, reject_reason, retention_status, accepted_clip_count, raw_video_path, video_meta_path,
              platform_metadata_json, extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_uid) DO UPDATE SET
              platform=excluded.platform,
              bvid=excluded.bvid,
              url=excluded.url,
              title=excluded.title,
              uploader=excluded.uploader,
              publish_time=excluded.publish_time,
              duration_sec=excluded.duration_sec,
              cover_url=excluded.cover_url,
              description=excluded.description,
              tags_json=excluded.tags_json,
              categories_json=excluded.categories_json,
              view_count=excluded.view_count,
              like_count=excluded.like_count,
              comment_count=excluded.comment_count,
              play_count=excluded.play_count,
              danmaku_count=excluded.danmaku_count,
              type=excluded.type,
              webpage_url=excluded.webpage_url,
              query_id=excluded.query_id,
              scene_text=excluded.scene_text,
              trigger_text=excluded.trigger_text,
              source_excel_row=excluded.source_excel_row,
              subtitles_available=excluded.subtitles_available,
              rights_status=excluded.rights_status,
              download_status=excluded.download_status,
              reject_reason=excluded.reject_reason,
              retention_status=excluded.retention_status,
              accepted_clip_count=excluded.accepted_clip_count,
              raw_video_path=excluded.raw_video_path,
              video_meta_path=excluded.video_meta_path,
              platform_metadata_json=excluded.platform_metadata_json,
              extra_json=excluded.extra_json
            """,
            (
                record["video_uid"],
                record["platform"],
                record["bvid"],
                record["url"],
                record["title"],
                record.get("uploader"),
                record.get("publish_time"),
                record.get("duration_sec"),
                record.get("cover_url"),
                record.get("description"),
                self.db.to_json(record.get("tags", [])),
                self.db.to_json(record.get("categories", [])),
                record.get("view_count"),
                record.get("like_count"),
                record.get("comment_count"),
                record.get("play_count"),
                record.get("danmaku_count"),
                record.get("type"),
                record.get("webpage_url"),
                record["query_id"],
                record["scene_text"],
                record["trigger_text"],
                int(record["source_excel_row"]),
                int(bool(record.get("subtitles_available"))),
                record.get("rights_status", "unknown"),
                record["download_status"],
                record.get("reject_reason"),
                record.get("retention_status"),
                int(record.get("accepted_clip_count", 0)),
                record.get("raw_video_path"),
                record.get("video_meta_path"),
                self.db.to_json(record.get("platform_metadata", {})),
                self.db.to_json(record.get("extra", {})),
            ),
        )

    def add_download_event(self, video_uid: str, status: str, *, local_path: str | None = None, file_size_bytes: int | None = None, error_message: str | None = None, extra: dict[str, Any] | None = None) -> None:
        self.db.execute(
            """
            INSERT INTO download_events (video_uid, status, local_path, file_size_bytes, error_message, extra_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_uid,
                status,
                local_path,
                file_size_bytes,
                error_message,
                self.db.to_json(extra or {}),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    def list_downloaded_for_preprocess(self, limit: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT * FROM videos
            WHERE download_status='downloaded' AND raw_video_path IS NOT NULL AND COALESCE(retention_status, '')=''
            ORDER BY source_excel_row, title
            LIMIT ?
            """,
            (int(limit),),
        )
        return [self._deserialize(row) for row in rows]

    def list_processed_clips_for_annotation(self, limit: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT * FROM clips
            WHERE filter_status='accepted' AND annotation_status='pending'
            ORDER BY clip_uid
            LIMIT ?
            """,
            (int(limit),),
        )
        return [self._deserialize_clip(row) for row in rows]

    def list_exportable(self) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT a.*, c.video_uid, c.clip_path, c.manifest_path, v.video_meta_path
            FROM annotations a
            JOIN clips c ON c.clip_uid=a.clip_uid
            JOIN videos v ON v.video_uid=c.video_uid
            WHERE a.status='done'
            ORDER BY a.annotation_uid
            """
        )
        return [dict(row) for row in rows]

    def update_video_cleanup(self, video_uid: str, *, retention_status: str, raw_video_path: str | None, accepted_clip_count: int) -> None:
        self.db.execute(
            "UPDATE videos SET retention_status=?, raw_video_path=?, accepted_clip_count=? WHERE video_uid=?",
            (retention_status, raw_video_path, int(accepted_clip_count), video_uid),
        )

    def _deserialize(self, row: Any) -> dict[str, Any]:
        result = dict(row)
        result["tags"] = self.db.from_json(result.pop("tags_json"), [])
        result["categories"] = self.db.from_json(result.pop("categories_json"), [])
        result["platform_metadata"] = self.db.from_json(result.pop("platform_metadata_json"), {})
        result["extra"] = self.db.from_json(result.pop("extra_json"), {})
        result["subtitles_available"] = bool(result["subtitles_available"])
        return result

    def _deserialize_clip(self, row: Any) -> dict[str, Any]:
        result = dict(row)
        result["filter_reasons"] = self.db.from_json(result.pop("filter_reasons_json"), [])
        result["scores"] = self.db.from_json(result.pop("scores_json"), {})
        return result


class ClipRepository:
    def __init__(self, db: SQLiteDB):
        self.db = db

    def upsert(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO clips (
              clip_uid, video_uid, start_sec, end_sec, duration_sec, segmentation_mode, filter_status,
              filter_reasons_json, retention_status, clip_path, manifest_path, keyframes_manifest_path,
              subtitle_path, scores_json, annotation_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(clip_uid) DO UPDATE SET
              video_uid=excluded.video_uid,
              start_sec=excluded.start_sec,
              end_sec=excluded.end_sec,
              duration_sec=excluded.duration_sec,
              segmentation_mode=excluded.segmentation_mode,
              filter_status=excluded.filter_status,
              filter_reasons_json=excluded.filter_reasons_json,
              retention_status=excluded.retention_status,
              clip_path=excluded.clip_path,
              manifest_path=excluded.manifest_path,
              keyframes_manifest_path=excluded.keyframes_manifest_path,
              subtitle_path=excluded.subtitle_path,
              scores_json=excluded.scores_json,
              annotation_status=excluded.annotation_status
            """,
            (
                record["clip_uid"],
                record["video_uid"],
                float(record["start_sec"]),
                float(record["end_sec"]),
                float(record["duration_sec"]),
                record["segmentation_mode"],
                record["filter_status"],
                self.db.to_json(record.get("filter_reasons", [])),
                record.get("retention_status"),
                record.get("clip_path"),
                record.get("manifest_path"),
                record.get("keyframes_manifest_path"),
                record.get("subtitle_path"),
                self.db.to_json(record.get("scores", {})),
                record.get("annotation_status", "pending"),
            ),
        )

    def update_annotation_status(self, clip_uid: str, status: str) -> None:
        self.db.execute("UPDATE clips SET annotation_status=? WHERE clip_uid=?", (status, clip_uid))


class AnnotationRepository:
    def __init__(self, db: SQLiteDB):
        self.db = db

    def upsert(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO annotations (
              annotation_uid, clip_uid, status, review_required, final_annotation_json,
              field_confidence_json, quality_flags_json, artifact_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(annotation_uid) DO UPDATE SET
              clip_uid=excluded.clip_uid,
              status=excluded.status,
              review_required=excluded.review_required,
              final_annotation_json=excluded.final_annotation_json,
              field_confidence_json=excluded.field_confidence_json,
              quality_flags_json=excluded.quality_flags_json,
              artifact_path=excluded.artifact_path
            """,
            (
                record["annotation_uid"],
                record["clip_uid"],
                record["status"],
                int(bool(record.get("review_required"))),
                self.db.to_json(record.get("final_annotation", {})),
                self.db.to_json(record.get("field_confidence", {})),
                self.db.to_json(record.get("quality_flags", [])),
                record.get("artifact_path"),
            ),
        )

    def summary(self) -> dict[str, int]:
        rows = self.db.fetchall("SELECT status, COUNT(*) AS count FROM annotations GROUP BY status")
        summary = {"done": 0, "rejected": 0, "failed": 0}
        for row in rows:
            summary[str(row["status"])] = int(row["count"])
        return summary
