from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .sqlite_db import SQLiteDB


CONFIDENCE_BUCKETS = (
    ("0.00-0.60", 0.0, 0.6),
    ("0.60-0.80", 0.6, 0.8),
    ("0.80-0.85", 0.8, 0.85),
    ("0.85-1.00", 0.85, 1.0000001),
)

DURATION_BUCKETS = (
    ("0-5s", 0.0, 5.0),
    ("5-10s", 5.0, 10.0),
    ("10-20s", 10.0, 20.0),
    ("20-60s", 20.0, 60.0),
    ("60s+", 60.0, None),
)


class DatasetStatsService:
    def __init__(self, db: SQLiteDB, *, annotation_domains_path: str | Path | None = None):
        self.db = db
        self.annotation_domains_path = Path(annotation_domains_path) if annotation_domains_path else None

    def build(self, *, top_n: int = 20) -> dict[str, Any]:
        top_n = max(int(top_n), 1)
        return {
            "summary": self._summary_counts(),
            "coverage": {
                "scene_distribution": self._done_distribution("v.scene_text", top_n=top_n),
                "platform_distribution": self._done_distribution("v.platform", top_n=top_n),
                "trigger_event_type_distribution": self._annotation_distribution("trigger_event_type", top_n=top_n),
                "group_behavior_type_distribution": self._annotation_distribution("group_behavior_type", top_n=top_n),
                "group_emotion_distribution": self._annotation_distribution("group_emotion", top_n=top_n),
                "duration_distribution": self._duration_distribution(),
                "confidence_distribution": self._confidence_distribution(),
                "quality_flag_distribution": self._quality_flag_distribution(top_n=top_n),
                "field_coverage": self._field_coverage(),
            },
            "audit": self._audit_counts(),
        }

    def _summary_counts(self) -> dict[str, int]:
        row = self.db.fetchone(
            """
            SELECT
              (SELECT COUNT(*) FROM queries) AS queries,
              (SELECT COUNT(*) FROM videos) AS videos,
              (SELECT COUNT(*) FROM clips) AS clips,
              (SELECT COUNT(*) FROM annotations) AS annotations,
              (SELECT COUNT(*) FROM annotations WHERE status='done') AS done_annotations,
              (SELECT COUNT(*) FROM annotations WHERE status='rejected') AS rejected_annotations,
              (SELECT COUNT(*) FROM annotations WHERE status='failed') AS failed_annotations,
              (SELECT COUNT(*) FROM annotations WHERE status='done' AND review_required=0) AS exportable_without_review
            """
        )
        return {key: int(row[key] or 0) for key in row.keys()} if row else {}

    def _done_distribution(self, expression: str, *, top_n: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            f"""
            SELECT COALESCE({expression}, '') AS value, COUNT(*) AS count
            FROM annotations a
            JOIN clips c ON c.clip_uid=a.clip_uid
            JOIN videos v ON v.video_uid=c.video_uid
            WHERE a.status='done'
            GROUP BY value
            ORDER BY count DESC, value
            LIMIT ?
            """,
            (top_n,),
        )
        return [{"value": str(row["value"] or "unknown"), "count": int(row["count"])} for row in rows]

    def _annotation_distribution(self, field_name: str, *, top_n: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT COALESCE(json_extract(final_annotation_json, ?), '') AS value, COUNT(*) AS count
            FROM annotations
            WHERE status='done'
            GROUP BY value
            ORDER BY count DESC, value
            LIMIT ?
            """,
            (_json_path(field_name), top_n),
        )
        return [{"value": str(row["value"] or "missing"), "count": int(row["count"])} for row in rows]

    def _duration_distribution(self) -> list[dict[str, Any]]:
        total_rows = self.db.fetchall(
            """
            SELECT c.duration_sec
            FROM annotations a
            JOIN clips c ON c.clip_uid=a.clip_uid
            WHERE a.status='done'
            """
        )
        values = [float(row["duration_sec"] or 0.0) for row in total_rows]
        return _bucket_counts(values, DURATION_BUCKETS)

    def _confidence_distribution(self) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT CAST(json_extract(field_confidence_json, '$.overall') AS REAL) AS confidence
            FROM annotations
            WHERE status='done'
            """
        )
        values = [float(row["confidence"] or 0.0) for row in rows]
        return _bucket_counts(values, CONFIDENCE_BUCKETS)

    def _quality_flag_distribution(self, *, top_n: int) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in self.db.fetchall("SELECT quality_flags_json FROM annotations"):
            for flag in self.db.from_json(row["quality_flags_json"], []):
                normalized = str(flag).strip()
                if not normalized:
                    continue
                counts[normalized] = counts.get(normalized, 0) + 1
        return [
            {"value": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:top_n]
        ]

    def _field_coverage(self) -> list[dict[str, Any]]:
        fields = self._annotation_fields()
        total = int(self.db.fetchone("SELECT COUNT(*) AS count FROM annotations WHERE status='done'")["count"] or 0)
        output: list[dict[str, Any]] = []
        for field_name in fields:
            row = self.db.fetchone(
                """
                SELECT COUNT(*) AS count
                FROM annotations
                WHERE status='done'
                  AND json_extract(final_annotation_json, ?) IS NOT NULL
                  AND json_extract(final_annotation_json, ?) != ''
                """,
                (_json_path(field_name), _json_path(field_name)),
            )
            present = int(row["count"] or 0) if row else 0
            output.append(
                {
                    "field": field_name,
                    "present": present,
                    "total": total,
                    "coverage": round(present / total, 4) if total else None,
                }
            )
        return output

    def _annotation_fields(self) -> list[str]:
        if self.annotation_domains_path and self.annotation_domains_path.exists():
            payload = json.loads(self.annotation_domains_path.read_text(encoding="utf-8"))
            fields = payload.get("fields")
            if isinstance(fields, dict):
                return list(fields)
        rows = self.db.fetchall("SELECT final_annotation_json FROM annotations WHERE status='done' LIMIT 100")
        fields: list[str] = []
        seen: set[str] = set()
        for row in rows:
            payload = self.db.from_json(row["final_annotation_json"], {})
            if not isinstance(payload, dict):
                continue
            for field_name in payload:
                if field_name in seen:
                    continue
                seen.add(field_name)
                fields.append(field_name)
        return fields

    def _audit_counts(self) -> dict[str, int]:
        row = self.db.fetchone(
            """
            SELECT
              (SELECT COUNT(*) FROM annotation_candidates) AS annotation_candidates,
              (SELECT COUNT(*) FROM judge_decisions) AS judge_decisions,
              (SELECT COUNT(*) FROM human_reviews) AS human_reviews,
              (SELECT COUNT(*) FROM human_reviews WHERE status='pending') AS pending_human_reviews,
              (SELECT COUNT(*) FROM lineage_edges) AS lineage_edges
            """
        )
        return {key: int(row[key] or 0) for key in row.keys()} if row else {}


def _bucket_counts(values: list[float], buckets: tuple[tuple[str, float, float | None], ...]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for label, lower, upper in buckets:
        if upper is None:
            count = sum(1 for value in values if value >= lower)
        else:
            count = sum(1 for value in values if value >= lower and value < upper)
        output.append({"bucket": label, "count": count})
    return output


def _json_path(field_name: str) -> str:
    escaped = str(field_name).replace('"', '\\"')
    return f'$."{escaped}"'
