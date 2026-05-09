from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .sqlite_db import SQLiteDB


class QueryRepository:
    def __init__(self, db: SQLiteDB, *, shard_count: int = 1, shard_index: int = 0):
        self.db = db
        self.shard_count = max(int(shard_count), 1)
        self.shard_index = int(shard_index)
        if self.shard_index < 0 or self.shard_index >= self.shard_count:
            raise ValueError(f"Invalid query shard index={self.shard_index} for shard_count={self.shard_count}")

    def owns_query_id(self, query_id: str) -> bool:
        if self.shard_count <= 1:
            return True
        digest = hashlib.sha1(str(query_id).encode("utf-8")).hexdigest()
        return int(digest, 16) % self.shard_count == self.shard_index

    def upsert(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO queries (query_id, seed_id, excel_row, scene_text, trigger_text, query_text, status, last_run_at, hit_count, run_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                int(record.get("run_count", 0)),
            ),
        )

    def get_by_query_text(self, query_text: str) -> dict[str, Any] | None:
        row = self.db.fetchone("SELECT * FROM queries WHERE query_text=?", (query_text,))
        return dict(row) if row else None

    def next_excel_row(self, *, minimum: int = 1) -> int:
        row = self.db.fetchone("SELECT MAX(excel_row) AS max_excel_row FROM queries")
        max_excel_row = int(row["max_excel_row"]) if row and row["max_excel_row"] is not None else minimum - 1
        return max(max_excel_row + 1, int(minimum))

    def list_pending(self, limit: int | None) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT * FROM queries
            WHERE status IN ('pending', 'retry')
            ORDER BY COALESCE(last_run_at, ''), excel_row, query_text
            """
        )
        pending = [dict(row) for row in rows if self.owns_query_id(str(row["query_id"]))]
        if limit is None or int(limit) <= 0:
            return pending
        return pending[: int(limit)]

    def mark_done(self, query_id: str, hit_count: int) -> None:
        self.db.execute(
            "UPDATE queries SET status='done', last_run_at=?, hit_count=?, run_count=COALESCE(run_count, 0) + 1 WHERE query_id=?",
            (datetime.now().isoformat(timespec="seconds"), int(hit_count), query_id),
        )

    def recycle_done(self, limit: int | None, *, cooldown_sec: float = 0.0, max_runs_per_query: int = 0) -> int:
        cooldown_sec = max(float(cooldown_sec), 0.0)
        max_runs_per_query = max(int(max_runs_per_query), 0)
        rows = self.db.fetchall(
            """
            SELECT query_id, last_run_at, run_count
            FROM queries
            WHERE status='done'
            ORDER BY COALESCE(run_count, 0), COALESCE(last_run_at, ''), excel_row, query_text
            """
        )
        if not rows:
            return 0
        now = datetime.now()
        recyclable: list[str] = []
        for row in rows:
            query_id = str(row["query_id"])
            if not self.owns_query_id(query_id):
                continue
            run_count = int(row["run_count"] or 0)
            if max_runs_per_query > 0 and run_count >= max_runs_per_query:
                continue
            last_run_at = str(row["last_run_at"] or "").strip()
            if last_run_at:
                try:
                    elapsed_sec = (now - datetime.fromisoformat(last_run_at)).total_seconds()
                except ValueError:
                    elapsed_sec = cooldown_sec
                if elapsed_sec < cooldown_sec:
                    continue
            recyclable.append(query_id)
            if limit is not None and int(limit) > 0 and len(recyclable) >= int(limit):
                break
        if not recyclable:
            return 0
        self.db.executemany(
            "UPDATE queries SET status='pending' WHERE query_id=?",
            [(query_id,) for query_id in recyclable],
        )
        return len(recyclable)

    def count(self) -> int:
        row = self.db.fetchone("SELECT COUNT(*) AS count FROM queries")
        return int(row["count"]) if row else 0

    def summary(self, *, owned_only: bool = False) -> dict[str, int]:
        summary = {"total": 0, "pending": 0, "retry": 0, "done": 0}
        if owned_only and self.shard_count > 1:
            rows = self.db.fetchall("SELECT query_id, status FROM queries")
            for row in rows:
                query_id = str(row["query_id"])
                if not self.owns_query_id(query_id):
                    continue
                status = str(row["status"])
                summary["total"] += 1
                summary[status] = summary.get(status, 0) + 1
            return summary
        rows = self.db.fetchall("SELECT status, COUNT(*) AS count FROM queries GROUP BY status")
        for row in rows:
            status = str(row["status"])
            count = int(row["count"])
            summary["total"] += count
            summary[status] = count
        return summary


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
              webpage_url, query_id, scene_text, trigger_text, source_excel_row, subtitles_available, rights_status, download_status,
              download_started_at, download_finished_at, download_elapsed_sec, reject_reason, retention_status, accepted_clip_count,
              preprocess_started_at, preprocess_finished_at, preprocess_elapsed_sec, raw_video_path, video_meta_path,
              platform_metadata_json, extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
              download_started_at=excluded.download_started_at,
              download_finished_at=excluded.download_finished_at,
              download_elapsed_sec=excluded.download_elapsed_sec,
              reject_reason=excluded.reject_reason,
              retention_status=excluded.retention_status,
              accepted_clip_count=excluded.accepted_clip_count,
              preprocess_started_at=excluded.preprocess_started_at,
              preprocess_finished_at=excluded.preprocess_finished_at,
              preprocess_elapsed_sec=excluded.preprocess_elapsed_sec,
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
                record.get("download_started_at"),
                record.get("download_finished_at"),
                record.get("download_elapsed_sec"),
                record.get("reject_reason"),
                record.get("retention_status"),
                int(record.get("accepted_clip_count", 0)),
                record.get("preprocess_started_at"),
                record.get("preprocess_finished_at"),
                record.get("preprocess_elapsed_sec"),
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

    def list_downloaded_for_preprocess(self, limit: int | None) -> list[dict[str, Any]]:
        if limit is None or int(limit) <= 0:
            rows = self.db.fetchall(
                """
                SELECT * FROM videos
                WHERE download_status='downloaded'
                  AND raw_video_path IS NOT NULL
                  AND COALESCE(retention_status, '')=''
                  AND preprocess_started_at IS NULL
                  AND preprocess_finished_at IS NULL
                ORDER BY source_excel_row, title
                """
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM videos
                WHERE download_status='downloaded'
                  AND raw_video_path IS NOT NULL
                  AND COALESCE(retention_status, '')=''
                  AND preprocess_started_at IS NULL
                  AND preprocess_finished_at IS NULL
                ORDER BY source_excel_row, title
                LIMIT ?
                """,
                (int(limit),),
            )
        return [self._deserialize(row) for row in rows]

    def claim_downloaded_for_preprocess(self, limit: int | None) -> list[dict[str, Any]]:
        rows = self.list_downloaded_for_preprocess(limit)
        if not rows:
            return []
        started_at = datetime.now().isoformat(timespec="seconds")
        for row in rows:
            self.mark_preprocess_started(row["video_uid"], started_at=started_at)
            row["preprocess_started_at"] = started_at
        return rows

    def list_processed_clips_for_annotation(self, limit: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT * FROM clips
            WHERE filter_status='accepted' AND annotation_status='pending'
            ORDER BY COALESCE(created_at, ''), clip_uid
            LIMIT ?
            """,
            (int(limit),),
        )
        return [self._deserialize_clip(row) for row in rows]

    def mark_preprocess_started(self, video_uid: str, *, started_at: str | None = None) -> None:
        self.db.execute(
            """
            UPDATE videos
            SET preprocess_started_at=?,
                preprocess_finished_at=NULL,
                preprocess_elapsed_sec=NULL
            WHERE video_uid=?
            """,
            (started_at or datetime.now().isoformat(timespec="seconds"), video_uid),
        )

    def reset_preprocess_claim(self, video_uid: str) -> None:
        self.db.execute(
            """
            UPDATE videos
            SET preprocess_started_at=NULL,
                preprocess_finished_at=NULL,
                preprocess_elapsed_sec=NULL
            WHERE video_uid=?
            """,
            (video_uid,),
        )

    def list_exportable(
        self,
        limit: int | None = None,
        min_confidence: float | None = None,
        caps: dict[tuple[str, str], int] | None = None,
        require_review_clear: bool = False,
    ) -> list[dict[str, Any]]:
        if limit is not None and int(limit) < 0:
            raise ValueError("export limit must be >= 0")
        if limit is not None and int(limit) == 0:
            return []
        conditions = ["a.status='done'"]
        params: list[Any] = []
        if require_review_clear:
            conditions.append("a.review_required=0")
        if min_confidence is not None:
            conditions.append(
                "CAST(json_extract(a.field_confidence_json, '$.overall') AS REAL) >= ?"
            )
            params.append(float(min_confidence))
        where_clause = " AND ".join(conditions)
        sql = f"""
            SELECT a.*, c.video_uid, c.clip_path, c.manifest_path, v.video_meta_path
            FROM annotations a
            JOIN clips c ON c.clip_uid=a.clip_uid
            JOIN videos v ON v.video_uid=c.video_uid
            WHERE {where_clause}
            ORDER BY a.annotation_uid
        """
        rows = self.db.fetchall(sql, tuple(params))
        all_rows = [dict(row) for row in rows]
        import random
        random.shuffle(all_rows)
        result = self._apply_caps(all_rows, caps)
        if limit is not None:
            result = result[: int(limit)]
        return result

    @staticmethod
    def _apply_caps(rows: list[dict[str, Any]], caps: dict[tuple[str, str], int] | None) -> list[dict[str, Any]]:
        if not caps:
            return rows
        import json as _json
        counters: dict[tuple[str, str], int] = {key: 0 for key in caps}
        result: list[dict[str, Any]] = []
        for row in rows:
            annotation = _json.loads(row.get("final_annotation_json") or "{}")
            skip = False
            for (field, value), max_count in caps.items():
                if str(annotation.get(field, "")) == value:
                    if counters[(field, value)] >= max_count:
                        skip = True
                        break
                    counters[(field, value)] += 1
            if not skip:
                result.append(row)
        return result

    def update_video_cleanup(
        self,
        video_uid: str,
        *,
        retention_status: str,
        raw_video_path: str | None,
        accepted_clip_count: int,
        preprocess_started_at: str | None = None,
        preprocess_finished_at: str | None = None,
        preprocess_elapsed_sec: float | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE videos
            SET retention_status=?,
                raw_video_path=?,
                accepted_clip_count=?,
                preprocess_started_at=COALESCE(?, preprocess_started_at),
                preprocess_finished_at=COALESCE(?, preprocess_finished_at),
                preprocess_elapsed_sec=COALESCE(?, preprocess_elapsed_sec)
            WHERE video_uid=?
            """,
            (
                retention_status,
                raw_video_path,
                int(accepted_clip_count),
                preprocess_started_at,
                preprocess_finished_at,
                preprocess_elapsed_sec,
                video_uid,
            ),
        )

    def summary(self) -> dict[str, Any]:
        row = self.db.fetchone(
            """
            SELECT
              COUNT(*) AS total_videos,
              SUM(CASE WHEN download_status='downloaded' THEN 1 ELSE 0 END) AS downloaded_videos,
              SUM(CASE WHEN download_status='downloading' AND download_started_at IS NOT NULL AND download_finished_at IS NULL THEN 1 ELSE 0 END) AS downloading_videos,
              SUM(CASE WHEN download_status='downloaded' AND raw_video_path IS NOT NULL AND COALESCE(retention_status, '')='' AND preprocess_started_at IS NULL AND preprocess_finished_at IS NULL THEN 1 ELSE 0 END) AS pending_preprocess_videos,
              SUM(CASE WHEN preprocess_started_at IS NOT NULL AND preprocess_finished_at IS NULL THEN 1 ELSE 0 END) AS preprocessing_videos,
              SUM(CASE WHEN preprocess_finished_at IS NOT NULL THEN 1 ELSE 0 END) AS processed_videos,
              COALESCE(SUM(CASE WHEN preprocess_finished_at IS NOT NULL THEN accepted_clip_count ELSE 0 END), 0) AS accepted_clips_after_preprocess,
              AVG(CASE WHEN download_status='downloaded' AND download_elapsed_sec IS NOT NULL THEN download_elapsed_sec END) AS avg_download_sec,
              AVG(CASE WHEN preprocess_finished_at IS NOT NULL AND preprocess_elapsed_sec IS NOT NULL THEN preprocess_elapsed_sec END) AS avg_preprocess_sec_per_video,
              CASE
                WHEN COALESCE(SUM(CASE WHEN preprocess_finished_at IS NOT NULL THEN accepted_clip_count ELSE 0 END), 0) > 0
                THEN SUM(CASE WHEN preprocess_finished_at IS NOT NULL AND preprocess_elapsed_sec IS NOT NULL THEN preprocess_elapsed_sec ELSE 0 END)
                     / SUM(CASE WHEN preprocess_finished_at IS NOT NULL THEN accepted_clip_count ELSE 0 END)
              END AS avg_preprocess_sec_per_accepted_clip
            FROM videos
            """
        )
        processed_videos = int(row["processed_videos"] or 0) if row else 0
        accepted_clips_after_preprocess = int(row["accepted_clips_after_preprocess"] or 0) if row else 0
        accepted_clips_per_processed_video = None
        if processed_videos > 0:
            accepted_clips_per_processed_video = accepted_clips_after_preprocess / processed_videos
        return {
            "total": int(row["total_videos"] or 0) if row else 0,
            "downloaded": int(row["downloaded_videos"] or 0) if row else 0,
            "downloading": int(row["downloading_videos"] or 0) if row else 0,
            "pending_preprocess": int(row["pending_preprocess_videos"] or 0) if row else 0,
            "preprocessing": int(row["preprocessing_videos"] or 0) if row else 0,
            "processed": processed_videos,
            "accepted_clips_after_preprocess": accepted_clips_after_preprocess,
            "avg_download_sec": _nullable_float(row["avg_download_sec"] if row else None),
            "avg_preprocess_sec_per_video": _nullable_float(row["avg_preprocess_sec_per_video"] if row else None),
            "avg_preprocess_sec_per_accepted_clip": _nullable_float(row["avg_preprocess_sec_per_accepted_clip"] if row else None),
            "accepted_clips_per_processed_video": _nullable_float(accepted_clips_per_processed_video),
        }

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
              clip_uid, video_uid, start_sec, end_sec, duration_sec, created_at, segmentation_mode, filter_status,
              filter_reasons_json, retention_status, clip_path, manifest_path, keyframes_manifest_path,
              subtitle_path, scores_json, annotation_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(clip_uid) DO UPDATE SET
              video_uid=excluded.video_uid,
              start_sec=excluded.start_sec,
              end_sec=excluded.end_sec,
              duration_sec=excluded.duration_sec,
              created_at=COALESCE(clips.created_at, excluded.created_at),
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
                record.get("created_at") or datetime.now().isoformat(timespec="seconds"),
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

    def claim_pending_annotations(self, limit: int | None) -> list[dict[str, Any]]:
        if limit is None or int(limit) <= 0:
            rows = self.db.fetchall(
                """
                SELECT * FROM clips
                WHERE filter_status='accepted' AND annotation_status='pending'
                ORDER BY COALESCE(created_at, ''), clip_uid
                """
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM clips
                WHERE filter_status='accepted' AND annotation_status='pending'
                ORDER BY COALESCE(created_at, ''), clip_uid
                LIMIT ?
                """,
                (int(limit),),
            )
        if not rows:
            return []
        clip_uids = [str(row["clip_uid"]) for row in rows]
        self.db.executemany(
            "UPDATE clips SET annotation_status='annotating' WHERE clip_uid=?",
            [(clip_uid,) for clip_uid in clip_uids],
        )
        return [self._deserialize(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        row = self.db.fetchone(
            """
            SELECT
              COUNT(*) AS total_clips,
              SUM(CASE WHEN filter_status='accepted' THEN 1 ELSE 0 END) AS accepted_clips,
              SUM(CASE WHEN filter_status='rejected' THEN 1 ELSE 0 END) AS rejected_clips,
              SUM(CASE WHEN annotation_status='pending' THEN 1 ELSE 0 END) AS pending_annotation_clips,
              SUM(CASE WHEN annotation_status='annotating' THEN 1 ELSE 0 END) AS annotating_clips,
              MIN(CASE WHEN annotation_status='pending' THEN created_at END) AS oldest_pending_created_at
            FROM clips
            """
        )
        oldest_pending_created_at = str(row["oldest_pending_created_at"]) if row and row["oldest_pending_created_at"] else None
        rejection_reason_counts: dict[str, int] = {}
        for rejected_row in self.db.fetchall("SELECT filter_reasons_json FROM clips WHERE filter_status='rejected'"):
            for reason in self.db.from_json(rejected_row["filter_reasons_json"], []):
                normalized = str(reason).strip()
                if not normalized:
                    continue
                rejection_reason_counts[normalized] = rejection_reason_counts.get(normalized, 0) + 1
        top_rejection_reasons = [
            {"reason": reason, "count": count}
            for reason, count in sorted(rejection_reason_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]
        return {
            "total": int(row["total_clips"] or 0) if row else 0,
            "accepted": int(row["accepted_clips"] or 0) if row else 0,
            "rejected": int(row["rejected_clips"] or 0) if row else 0,
            "pending_annotation": int(row["pending_annotation_clips"] or 0) if row else 0,
            "annotating": int(row["annotating_clips"] or 0) if row else 0,
            "oldest_pending_created_at": oldest_pending_created_at,
            "top_rejection_reasons": top_rejection_reasons,
        }

    def _deserialize(self, row: Any) -> dict[str, Any]:
        result = dict(row)
        result["filter_reasons"] = self.db.from_json(result.pop("filter_reasons_json"), [])
        result["scores"] = self.db.from_json(result.pop("scores_json"), {})
        return result


class AnnotationRepository:
    def __init__(self, db: SQLiteDB):
        self.db = db

    @staticmethod
    def _audit_uid(*parts: Any) -> str:
        text = "::".join(str(part) for part in parts)
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    def upsert(self, record: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO annotations (
              annotation_uid, clip_uid, status, started_at, finished_at, elapsed_sec, review_required, final_annotation_json,
              field_confidence_json, quality_flags_json, artifact_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(annotation_uid) DO UPDATE SET
              clip_uid=excluded.clip_uid,
              status=excluded.status,
              started_at=excluded.started_at,
              finished_at=excluded.finished_at,
              elapsed_sec=excluded.elapsed_sec,
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
                record.get("started_at"),
                record.get("finished_at"),
                record.get("elapsed_sec"),
                int(bool(record.get("review_required"))),
                self.db.to_json(record.get("final_annotation", {})),
                self.db.to_json(record.get("field_confidence", {})),
                self.db.to_json(record.get("quality_flags", [])),
                record.get("artifact_path"),
            ),
        )

    def record_annotation_audit(
        self,
        *,
        annotation_uid: str,
        clip_uid: str,
        agent_outputs: dict[str, Any],
        final_annotation: dict[str, Any],
        field_confidence: dict[str, Any],
        quality_flags: list[str],
        review_required: bool,
    ) -> None:
        candidate_results = agent_outputs.get("candidate_results") or []
        if isinstance(candidate_results, list):
            for candidate in candidate_results:
                if isinstance(candidate, dict):
                    self._upsert_candidate(annotation_uid=annotation_uid, clip_uid=clip_uid, candidate=candidate)

        judge_payload = agent_outputs.get("judge")
        if isinstance(judge_payload, dict) and bool(judge_payload.get("enabled")):
            self._upsert_judge_decision(annotation_uid=annotation_uid, clip_uid=clip_uid, judge_payload=judge_payload)

        if review_required:
            conflict_fields: list[str] = []
            if isinstance(judge_payload, dict):
                raw_conflicts = judge_payload.get("conflict_fields") or []
                if isinstance(raw_conflicts, list):
                    conflict_fields = [str(item) for item in raw_conflicts if str(item).strip()]
            self._upsert_review_task(
                annotation_uid=annotation_uid,
                clip_uid=clip_uid,
                conflict_fields=conflict_fields,
                before_annotation=final_annotation,
            )

        self._refresh_lineage_for_annotation(
            annotation_uid=annotation_uid,
            clip_uid=clip_uid,
            agent_outputs=agent_outputs,
            review_required=review_required,
            field_confidence=field_confidence,
            quality_flags=quality_flags,
        )

    def record_export_lineage(self, *, export_dir: str, rows: list[dict[str, Any]]) -> None:
        export_batch_id = str(export_dir).rstrip("/").split("/")[-1]
        for row in rows:
            annotation_uid = str(row.get("annotation_uid") or "")
            clip_uid = str(row.get("clip_uid") or "")
            if annotation_uid:
                self._upsert_lineage_edge(
                    parent_type="annotation",
                    parent_id=annotation_uid,
                    child_type="export_batch",
                    child_id=export_batch_id,
                    relation="included_in_export",
                    extra={"clip_uid": clip_uid, "export_dir": export_dir},
                )

    def list_human_reviews(self, *, status: str = "pending", limit: int = 20) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if status and status != "all":
            conditions.append("hr.status=?")
            params.append(status)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.db.fetchall(
            f"""
            SELECT hr.*, a.status AS annotation_status, a.quality_flags_json, a.field_confidence_json
            FROM human_reviews hr
            JOIN annotations a ON a.annotation_uid=hr.annotation_uid
            {where_clause}
            ORDER BY COALESCE(hr.updated_at, hr.created_at, ''), hr.review_uid
            LIMIT ?
            """,
            (*params, int(limit)),
        )
        return [self._deserialize_human_review(row) for row in rows]

    def complete_human_review(
        self,
        *,
        review_uid: str,
        after_annotation: dict[str, Any],
        reviewer: str | None = None,
        review_notes: str | None = None,
    ) -> dict[str, Any]:
        row = self.db.fetchone(
            """
            SELECT hr.*, a.quality_flags_json
            FROM human_reviews hr
            JOIN annotations a ON a.annotation_uid=hr.annotation_uid
            WHERE hr.review_uid=?
            """,
            (review_uid,),
        )
        if row is None:
            raise ValueError(f"Unknown human review: {review_uid}")
        now = datetime.now().isoformat(timespec="seconds")
        annotation_uid = str(row["annotation_uid"])
        clip_uid = str(row["clip_uid"])
        quality_flags = self.db.from_json(row["quality_flags_json"], [])
        quality_flags = list(dict.fromkeys([*quality_flags, "human_review_completed"]))
        self.db.execute(
            """
            UPDATE human_reviews
            SET status='completed',
                after_annotation_json=?,
                reviewer=?,
                review_notes=?,
                updated_at=?
            WHERE review_uid=?
            """,
            (
                self.db.to_json(after_annotation),
                reviewer,
                review_notes,
                now,
                review_uid,
            ),
        )
        self.db.execute(
            """
            UPDATE annotations
            SET status='done',
                review_required=0,
                final_annotation_json=?,
                quality_flags_json=?
            WHERE annotation_uid=?
            """,
            (
                self.db.to_json(after_annotation),
                self.db.to_json(quality_flags),
                annotation_uid,
            ),
        )
        self.db.execute("UPDATE clips SET annotation_status='done' WHERE clip_uid=?", (clip_uid,))
        self._upsert_lineage_edge(
            parent_type="human_review",
            parent_id=review_uid,
            child_type="annotation",
            child_id=annotation_uid,
            relation="review_completed",
            extra={"reviewer": reviewer, "updated_at": now},
        )
        return {
            "review_uid": review_uid,
            "annotation_uid": annotation_uid,
            "clip_uid": clip_uid,
            "status": "completed",
        }

    def cancel_human_review(
        self,
        *,
        review_uid: str,
        reviewer: str | None = None,
        review_notes: str | None = None,
    ) -> dict[str, Any]:
        row = self.db.fetchone("SELECT annotation_uid, clip_uid FROM human_reviews WHERE review_uid=?", (review_uid,))
        if row is None:
            raise ValueError(f"Unknown human review: {review_uid}")
        now = datetime.now().isoformat(timespec="seconds")
        self.db.execute(
            """
            UPDATE human_reviews
            SET status='cancelled',
                reviewer=?,
                review_notes=?,
                updated_at=?
            WHERE review_uid=?
            """,
            (reviewer, review_notes, now, review_uid),
        )
        return {
            "review_uid": review_uid,
            "annotation_uid": str(row["annotation_uid"]),
            "clip_uid": str(row["clip_uid"]),
            "status": "cancelled",
        }

    def _upsert_candidate(self, *, annotation_uid: str, clip_uid: str, candidate: dict[str, Any]) -> None:
        candidate_id = str(candidate.get("candidate_id") or "candidate")
        candidate_uid = f"candidate_{self._audit_uid(annotation_uid, candidate_id)}"
        self.db.execute(
            """
            INSERT INTO annotation_candidates (
              candidate_uid, annotation_uid, clip_uid, candidate_id, agent, model, status,
              final_annotation_json, field_confidence_json, quality_flags_json, raw_payload_json,
              error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_uid) DO UPDATE SET
              annotation_uid=excluded.annotation_uid,
              clip_uid=excluded.clip_uid,
              candidate_id=excluded.candidate_id,
              agent=excluded.agent,
              model=excluded.model,
              status=excluded.status,
              final_annotation_json=excluded.final_annotation_json,
              field_confidence_json=excluded.field_confidence_json,
              quality_flags_json=excluded.quality_flags_json,
              raw_payload_json=excluded.raw_payload_json,
              error_message=excluded.error_message
            """,
            (
                candidate_uid,
                annotation_uid,
                clip_uid,
                candidate_id,
                candidate.get("agent"),
                candidate.get("model"),
                str(candidate.get("status") or "unknown"),
                self.db.to_json(candidate.get("final_annotation", {})),
                self.db.to_json(candidate.get("field_confidence", {})),
                self.db.to_json(candidate.get("quality_flags", [])),
                self.db.to_json(candidate.get("raw_payload", {})),
                candidate.get("error"),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._upsert_lineage_edge(
            parent_type="annotation",
            parent_id=annotation_uid,
            child_type="annotation_candidate",
            child_id=candidate_uid,
            relation="has_candidate",
            extra={"candidate_id": candidate_id, "status": candidate.get("status")},
        )

    def _upsert_judge_decision(self, *, annotation_uid: str, clip_uid: str, judge_payload: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO judge_decisions (
              annotation_uid, clip_uid, status, selected_candidate_ids_json, conflict_fields_json,
              decision_reasoning, final_annotation_json, field_confidence_json, quality_flags_json,
              raw_payload_json, fallback_candidate_id, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(annotation_uid) DO UPDATE SET
              clip_uid=excluded.clip_uid,
              status=excluded.status,
              selected_candidate_ids_json=excluded.selected_candidate_ids_json,
              conflict_fields_json=excluded.conflict_fields_json,
              decision_reasoning=excluded.decision_reasoning,
              final_annotation_json=excluded.final_annotation_json,
              field_confidence_json=excluded.field_confidence_json,
              quality_flags_json=excluded.quality_flags_json,
              raw_payload_json=excluded.raw_payload_json,
              fallback_candidate_id=excluded.fallback_candidate_id,
              error_message=excluded.error_message
            """,
            (
                annotation_uid,
                clip_uid,
                str(judge_payload.get("status") or "unknown"),
                self.db.to_json(judge_payload.get("selected_candidate_ids", [])),
                self.db.to_json(judge_payload.get("conflict_fields", [])),
                judge_payload.get("decision_reasoning"),
                self.db.to_json(judge_payload.get("final_annotation", {})),
                self.db.to_json(judge_payload.get("field_confidence", {})),
                self.db.to_json(judge_payload.get("quality_flags", [])),
                self.db.to_json(judge_payload.get("raw_payload", {})),
                judge_payload.get("fallback_candidate_id"),
                judge_payload.get("error"),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._upsert_lineage_edge(
            parent_type="annotation",
            parent_id=annotation_uid,
            child_type="judge_decision",
            child_id=annotation_uid,
            relation="has_judge_decision",
            extra={"status": judge_payload.get("status")},
        )

    def _upsert_review_task(
        self,
        *,
        annotation_uid: str,
        clip_uid: str,
        conflict_fields: list[str],
        before_annotation: dict[str, Any],
    ) -> None:
        review_uid = f"review_{self._audit_uid(annotation_uid)}"
        existing = self.db.fetchone("SELECT status FROM human_reviews WHERE review_uid=?", (review_uid,))
        existing_status = str(existing["status"]) if existing else ""
        if existing_status in {"completed", "cancelled"}:
            return
        now = datetime.now().isoformat(timespec="seconds")
        self.db.execute(
            """
            INSERT INTO human_reviews (
              review_uid, annotation_uid, clip_uid, status, conflict_fields_json, before_annotation_json,
              after_annotation_json, reviewer, review_notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(review_uid) DO UPDATE SET
              annotation_uid=excluded.annotation_uid,
              clip_uid=excluded.clip_uid,
              status=excluded.status,
              conflict_fields_json=excluded.conflict_fields_json,
              before_annotation_json=excluded.before_annotation_json,
              updated_at=excluded.updated_at
            """,
            (
                review_uid,
                annotation_uid,
                clip_uid,
                "pending",
                self.db.to_json(conflict_fields),
                self.db.to_json(before_annotation),
                None,
                None,
                None,
                now,
                now,
            ),
        )
        self._upsert_lineage_edge(
            parent_type="annotation",
            parent_id=annotation_uid,
            child_type="human_review",
            child_id=review_uid,
            relation="requires_review",
            extra={"conflict_fields": conflict_fields},
        )

    def _refresh_lineage_for_annotation(
        self,
        *,
        annotation_uid: str,
        clip_uid: str,
        agent_outputs: dict[str, Any],
        review_required: bool,
        field_confidence: dict[str, Any],
        quality_flags: list[str],
    ) -> None:
        row = self.db.fetchone(
            """
            SELECT c.video_uid, v.query_id
            FROM clips c
            JOIN videos v ON v.video_uid=c.video_uid
            WHERE c.clip_uid=?
            """,
            (clip_uid,),
        )
        if row:
            query_id = str(row["query_id"])
            video_uid = str(row["video_uid"])
            self._upsert_lineage_edge(
                parent_type="query",
                parent_id=query_id,
                child_type="video",
                child_id=video_uid,
                relation="retrieved_video",
                extra={},
            )
            self._upsert_lineage_edge(
                parent_type="video",
                parent_id=video_uid,
                child_type="clip",
                child_id=clip_uid,
                relation="generated_clip",
                extra={},
            )
        self._upsert_lineage_edge(
            parent_type="clip",
            parent_id=clip_uid,
            child_type="annotation",
            child_id=annotation_uid,
            relation="annotated_as",
            extra={
                "framework": agent_outputs.get("annotation_framework"),
                "review_required": review_required,
                "overall_confidence": field_confidence.get("overall"),
                "quality_flags": quality_flags,
            },
        )

    def _upsert_lineage_edge(
        self,
        *,
        parent_type: str,
        parent_id: str,
        child_type: str,
        child_id: str,
        relation: str,
        extra: dict[str, Any],
    ) -> None:
        edge_uid = f"edge_{self._audit_uid(parent_type, parent_id, child_type, child_id, relation)}"
        self.db.execute(
            """
            INSERT INTO lineage_edges (
              edge_uid, parent_type, parent_id, child_type, child_id, relation, created_at, extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edge_uid) DO UPDATE SET
              parent_type=excluded.parent_type,
              parent_id=excluded.parent_id,
              child_type=excluded.child_type,
              child_id=excluded.child_id,
              relation=excluded.relation,
              extra_json=excluded.extra_json
            """,
            (
                edge_uid,
                parent_type,
                parent_id,
                child_type,
                child_id,
                relation,
                datetime.now().isoformat(timespec="seconds"),
                self.db.to_json(extra),
            ),
        )

    def _deserialize_human_review(self, row: Any) -> dict[str, Any]:
        result = dict(row)
        result["conflict_fields"] = self.db.from_json(result.pop("conflict_fields_json"), [])
        result["before_annotation"] = self.db.from_json(result.pop("before_annotation_json"), {})
        result["after_annotation"] = self.db.from_json(result.pop("after_annotation_json"), None)
        result["quality_flags"] = self.db.from_json(result.pop("quality_flags_json"), [])
        result["field_confidence"] = self.db.from_json(result.pop("field_confidence_json"), {})
        return result

    def summary(self) -> dict[str, Any]:
        rows = self.db.fetchall("SELECT status, COUNT(*) AS count FROM annotations GROUP BY status")
        summary = {"done": 0, "rejected": 0, "failed": 0}
        for row in rows:
            summary[str(row["status"])] = int(row["count"])
        quality_flag_counts: dict[str, int] = {}
        for annotation_row in self.db.fetchall("SELECT quality_flags_json FROM annotations"):
            for flag in self.db.from_json(annotation_row["quality_flags_json"], []):
                normalized = str(flag).strip()
                if not normalized:
                    continue
                quality_flag_counts[normalized] = quality_flag_counts.get(normalized, 0) + 1
        top_quality_flags = [
            {"flag": flag, "count": count}
            for flag, count in sorted(quality_flag_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]
        timing_row = self.db.fetchone(
            """
            SELECT
              AVG(CASE WHEN elapsed_sec IS NOT NULL THEN elapsed_sec END) AS avg_completed_sec,
              AVG(CASE WHEN status='done' AND elapsed_sec IS NOT NULL THEN elapsed_sec END) AS avg_done_sec
            FROM annotations
            """
        )
        completed = sum(summary.values())
        done_rate = None
        if completed > 0:
            done_rate = summary["done"] / completed
        return {
            **summary,
            "completed": completed,
            "avg_completed_sec": _nullable_float(timing_row["avg_completed_sec"] if timing_row else None),
            "avg_done_sec": _nullable_float(timing_row["avg_done_sec"] if timing_row else None),
            "done_rate": _nullable_float(done_rate),
            "top_quality_flags": top_quality_flags,
        }


def _nullable_float(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)
