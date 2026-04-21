from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


VIDEO_META_FIELDS = (
    "video_uid",
    "platform",
    "bvid",
    "url",
    "webpage_url",
    "title",
    "uploader",
    "publish_time",
    "duration_sec",
    "cover_url",
    "description",
    "tags",
    "view_count",
    "like_count",
    "comment_count",
    "play_count",
    "danmaku_count",
    "type",
    "subtitles_available",
    "query_id",
    "scene_text",
    "trigger_text",
    "source_excel_row",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone exporter for done group-emotion video samples.",
    )
    parser.add_argument(
        "--runtime-root",
        default="runtime",
        help="Runtime directory that contains index.sqlite and exports/. Default: runtime",
    )
    parser.add_argument(
        "--db-path",
        help="SQLite database path. Overrides --runtime-root/index.sqlite.",
    )
    parser.add_argument(
        "--output-root",
        help="Directory for dataset_export_<timestamp>. Default: <runtime-root>/exports",
    )
    parser.add_argument(
        "--annotation-domains",
        help=(
            "Optional annotation_domains.json path. Used only to order final_annotation fields. "
            "If omitted, the script tries <runtime-root>/reference/annotation_domains.json "
            "then data/reference/annotation_domains.json."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Export at most N done samples. Omit this option to export all done samples.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        help="Only export samples whose field_confidence.overall is at least this value.",
    )
    parser.add_argument(
        "--cap",
        action="append",
        default=[],
        help="Cap samples per annotation value: field=value:max_count. Can be repeated.",
    )
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="Skip rows whose clip, manifest, video_meta, or raw annotation file is missing.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the stderr progress bar.",
    )
    return parser


def resolve_path(raw_path: str | Path, *, base_dir: Path = PROJECT_ROOT) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def resolve_optional_path(raw_path: str | None) -> Path | None:
    if raw_path is None or raw_path == "":
        return None
    return resolve_path(raw_path)


def require_path(path: Path | None, name: str) -> Path:
    if path is None:
        raise SystemExit(f"{name} path is empty")
    return path


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def loads_json(raw: str | None, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    return json.loads(raw)


def parse_caps(raw_caps: list[str]) -> dict[tuple[str, str], int]:
    caps: dict[tuple[str, str], int] = {}
    for token in raw_caps:
        try:
            field_value, count_text = token.rsplit(":", 1)
            field, value = field_value.split("=", 1)
            count = int(count_text)
        except ValueError:
            raise SystemExit(f"Invalid --cap format '{token}', expected field=value:max_count")
        if count < 0:
            raise SystemExit(f"Invalid --cap count '{token}', max_count must be >= 0")
        caps[(field.strip(), value.strip())] = count
    return caps


def find_annotation_domains(runtime_root: Path, explicit_path: str | None) -> Path | None:
    if explicit_path:
        path = resolve_path(explicit_path)
        if not path.exists():
            raise SystemExit(f"annotation domains file not found: {path}")
        return path

    candidates = (
        runtime_root / "reference" / "annotation_domains.json",
        PROJECT_ROOT / "data" / "reference" / "annotation_domains.json",
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def load_annotation_field_order(path: Path | None) -> list[str]:
    if path is None:
        return []
    payload = load_json(path)
    fields = payload.get("fields", {})
    if not isinstance(fields, dict):
        return []
    return list(fields)


class ProgressBar:
    def __init__(self, total: int, *, enabled: bool = True, width: int = 32) -> None:
        self.total = max(0, total)
        self.enabled = enabled
        self.width = width
        self.last_line_len = 0
        self.last_line = ""

    def update(self, current: int, *, exported: int, skipped: int) -> None:
        if not self.enabled:
            return

        if self.total <= 0:
            ratio = 1.0
        else:
            ratio = min(max(current / self.total, 0.0), 1.0)
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        percent = ratio * 100
        line = (
            f"\rExporting [{bar}] {current}/{self.total} "
            f"{percent:6.2f}% exported={exported} skipped={skipped}"
        )
        if line == self.last_line:
            return
        padding = " " * max(0, self.last_line_len - len(line))
        sys.stderr.write(line + padding)
        sys.stderr.flush()
        self.last_line_len = len(line)
        self.last_line = line

    def finish(self, *, current: int, exported: int, skipped: int) -> None:
        if not self.enabled:
            return
        self.update(current, exported=exported, skipped=skipped)
        sys.stderr.write("\n")
        sys.stderr.flush()

    def newline(self) -> None:
        if self.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()


def query_done_rows(db_path: Path, min_confidence: float | None) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        conditions = ["a.status = 'done'"]
        params: list[Any] = []
        if min_confidence is not None:
            conditions.append("CAST(json_extract(a.field_confidence_json, '$.overall') AS REAL) >= ?")
            params.append(float(min_confidence))

        sql = f"""
            SELECT
              a.annotation_uid,
              a.clip_uid,
              a.final_annotation_json,
              a.field_confidence_json,
              a.quality_flags_json,
              a.artifact_path,
              c.video_uid,
              c.start_sec,
              c.end_sec,
              c.duration_sec AS clip_duration_sec,
              c.segmentation_mode,
              c.clip_path,
              c.manifest_path,
              v.platform,
              v.bvid,
              v.url,
              v.webpage_url,
              v.title,
              v.uploader,
              v.publish_time,
              v.duration_sec AS video_duration_sec,
              v.cover_url,
              v.description,
              v.tags_json,
              v.view_count,
              v.like_count,
              v.comment_count,
              v.play_count,
              v.danmaku_count,
              v.type,
              v.subtitles_available,
              v.query_id,
              v.scene_text,
              v.trigger_text,
              v.source_excel_row,
              v.video_meta_path
            FROM annotations a
            JOIN clips c ON c.clip_uid = a.clip_uid
            JOIN videos v ON v.video_uid = c.video_uid
            WHERE {" AND ".join(conditions)}
            ORDER BY a.annotation_uid
        """
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def apply_caps(rows: list[sqlite3.Row], caps: dict[tuple[str, str], int]) -> list[sqlite3.Row]:
    if not caps:
        return rows

    counters: dict[tuple[str, str], int] = {key: 0 for key in caps}
    kept: list[sqlite3.Row] = []
    for row in rows:
        final_annotation = loads_json(row["final_annotation_json"], {})
        skip = False
        for key, max_count in caps.items():
            field, value = key
            if str(final_annotation.get(field, "")) != value:
                continue
            if counters[key] >= max_count:
                skip = True
                break
            counters[key] += 1
        if not skip:
            kept.append(row)
    return kept


def order_final_annotation(final_annotation: dict[str, Any], field_order: list[str]) -> dict[str, Any]:
    if not field_order:
        return final_annotation

    ordered: dict[str, Any] = {}
    for field_name in field_order:
        if field_name in final_annotation:
            ordered[field_name] = final_annotation[field_name]
    for field_name, value in final_annotation.items():
        if field_name not in ordered:
            ordered[field_name] = value
    return ordered


def build_video_meta(row: sqlite3.Row, video_meta_path: Path | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if video_meta_path is not None and video_meta_path.exists():
        loaded = load_json(video_meta_path)
        if isinstance(loaded, dict):
            payload = loaded

    fallback = {
        "video_uid": row["video_uid"],
        "platform": row["platform"],
        "bvid": row["bvid"],
        "url": row["url"],
        "webpage_url": row["webpage_url"],
        "title": row["title"],
        "uploader": row["uploader"],
        "publish_time": row["publish_time"],
        "duration_sec": row["video_duration_sec"],
        "cover_url": row["cover_url"],
        "description": row["description"],
        "tags": loads_json(row["tags_json"], []),
        "view_count": row["view_count"],
        "like_count": row["like_count"],
        "comment_count": row["comment_count"],
        "play_count": row["play_count"],
        "danmaku_count": row["danmaku_count"],
        "type": row["type"],
        "subtitles_available": bool(row["subtitles_available"]),
        "query_id": row["query_id"],
        "scene_text": row["scene_text"],
        "trigger_text": row["trigger_text"],
        "source_excel_row": row["source_excel_row"],
    }

    result: dict[str, Any] = {}
    for field_name in VIDEO_META_FIELDS:
        result[field_name] = payload.get(field_name, fallback.get(field_name))
    return result


def build_clip_info(row: sqlite3.Row, clip_manifest_path: Path | None, clip_file: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if clip_manifest_path is not None and clip_manifest_path.exists():
        loaded = load_json(clip_manifest_path)
        if isinstance(loaded, dict):
            payload = loaded

    return {
        "clip_uid": payload.get("clip_uid", row["clip_uid"]),
        "video_uid": payload.get("video_uid", row["video_uid"]),
        "start_sec": payload.get("start_sec", row["start_sec"]),
        "end_sec": payload.get("end_sec", row["end_sec"]),
        "duration_sec": payload.get("duration_sec", row["clip_duration_sec"]),
        "segmentation_mode": payload.get("segmentation_mode", row["segmentation_mode"]),
        "clip_file": clip_file,
    }


def required_paths(row: sqlite3.Row) -> dict[str, Path | None]:
    return {
        "clip": resolve_optional_path(row["clip_path"]),
        "annotation": resolve_optional_path(row["artifact_path"]),
        "manifest": resolve_optional_path(row["manifest_path"]),
        "video_meta": resolve_optional_path(row["video_meta_path"]),
    }


def write_readme(export_dir: Path, sample_count: int, created_at: datetime, db_path: Path) -> None:
    readme = "\n".join(
        [
            "# 群体情感视频数据集导出说明",
            "",
            f"- 导出时间：`{created_at.isoformat(timespec='seconds')}`",
            f"- 样本数量：`{sample_count}`",
            f"- 来源数据库：`{db_path}`",
            "- 样本范围：仅包含 `annotations.status = 'done'` 的切片样本。",
            "- 每个样本包含一个 `clips/<clip_uid>.*` 视频文件和一个同名 `annotations/<clip_uid>.json` 标注文件。",
            "- `raw_annotations/` 保留原始标注 JSON，便于追溯。",
            "",
            "## 文件结构",
            "",
            "```text",
            f"{export_dir.name}/",
            "  README.md",
            "  clips/",
            "  annotations/",
            "  raw_annotations/",
            "```",
            "",
        ]
    )
    (export_dir / "README.md").write_text(readme, encoding="utf-8")


def export_dataset(
    *,
    db_path: Path,
    output_root: Path,
    annotation_domains_path: Path | None,
    limit: int | None,
    min_confidence: float | None,
    caps: dict[tuple[str, str], int],
    skip_missing: bool,
    progress_enabled: bool,
) -> dict[str, Any]:
    if limit is not None and limit < 0:
        raise SystemExit("--limit must be >= 0")
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    rows = query_done_rows(db_path, min_confidence)
    rows = apply_caps(rows, caps)
    if limit is not None:
        rows = rows[:limit]

    created_at = datetime.now()
    export_dir = output_root / f"dataset_export_{created_at.strftime('%Y%m%d_%H%M%S')}"
    clips_dir = export_dir / "clips"
    annotations_dir = export_dir / "annotations"
    raw_annotations_dir = export_dir / "raw_annotations"
    clips_dir.mkdir(parents=True, exist_ok=True)
    annotations_dir.mkdir(parents=True, exist_ok=True)
    raw_annotations_dir.mkdir(parents=True, exist_ok=True)

    field_order = load_annotation_field_order(annotation_domains_path)
    exported = 0
    skipped: list[dict[str, str]] = []
    progress = ProgressBar(len(rows), enabled=progress_enabled)
    progress.update(0, exported=exported, skipped=len(skipped))

    for index, row in enumerate(rows, start=1):
        paths = required_paths(row)
        missing = [name for name, path in paths.items() if path is None or not path.exists()]
        if missing:
            if skip_missing:
                skipped.append({"clip_uid": row["clip_uid"], "missing": ",".join(missing)})
                progress.update(index, exported=exported, skipped=len(skipped))
                continue
            progress.newline()
            missing_text = ", ".join(f"{name}={paths[name] or '<empty>'}" for name in missing)
            raise SystemExit(f"missing required file for clip_uid={row['clip_uid']}: {missing_text}")

        clip_uid = row["clip_uid"]
        source_clip = require_path(paths["clip"], "clip")
        source_annotation = require_path(paths["annotation"], "annotation")
        source_manifest = require_path(paths["manifest"], "manifest")
        source_video_meta = require_path(paths["video_meta"], "video_meta")
        target_clip = clips_dir / f"{clip_uid}{source_clip.suffix or '.mp4'}"
        shutil.copy2(source_clip, target_clip)
        shutil.copy2(source_annotation, raw_annotations_dir / f"{clip_uid}.json")

        annotation_payload = load_json(source_annotation)
        final_annotation = annotation_payload.get("final_annotation")
        if not isinstance(final_annotation, dict):
            final_annotation = loads_json(row["final_annotation_json"], {})

        exported_annotation = {
            "final_annotation": order_final_annotation(final_annotation, field_order),
            "video_meta": build_video_meta(row, source_video_meta),
            "clip_info": build_clip_info(
                row,
                source_manifest,
                str(target_clip.relative_to(export_dir)),
            ),
        }
        (annotations_dir / f"{clip_uid}.json").write_text(
            json.dumps(exported_annotation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        exported += 1
        progress.update(index, exported=exported, skipped=len(skipped))

    progress.finish(current=len(rows), exported=exported, skipped=len(skipped))
    write_readme(export_dir, exported, created_at, db_path)
    return {
        "export_dir": str(export_dir),
        "db_path": str(db_path),
        "exported_samples": exported,
        "skipped_samples": len(skipped),
        "skipped_examples": skipped[:20],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = resolve_path(args.runtime_root)
    db_path = resolve_path(args.db_path) if args.db_path else runtime_root / "index.sqlite"
    output_root = resolve_path(args.output_root) if args.output_root else runtime_root / "exports"
    annotation_domains_path = find_annotation_domains(runtime_root, args.annotation_domains)
    caps = parse_caps(args.cap)

    result = export_dataset(
        db_path=db_path,
        output_root=output_root,
        annotation_domains_path=annotation_domains_path,
        limit=args.limit,
        min_confidence=args.min_confidence,
        caps=caps,
        skip_missing=args.skip_missing,
        progress_enabled=not args.no_progress,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
