from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from group_emotion_video.config import load_config


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
        "--config",
        action="append",
        dest="config_paths",
        default=[],
        help="Optional config overlay path. When provided, defaults are read from project.runtime_dir and project.annotation_domains_path.",
    )
    parser.add_argument(
        "--runtime-root",
        help="Runtime directory that contains index.sqlite and exports/. Default: config project.runtime_dir, or runtime",
    )
    parser.add_argument(
        "--db-path",
        help="SQLite database path. Overrides config/default runtime_root/index.sqlite.",
    )
    parser.add_argument(
        "--output-root",
        help="Directory for dataset_export_<timestamp>. Default: config/default <runtime-root>/exports",
    )
    parser.add_argument(
        "--annotation-domains",
        help=(
            "Optional annotation_domains.json path. Used only to order final_annotation fields. "
            "If omitted, the script tries config project.annotation_domains_path, "
            "then <runtime-root>/reference/annotation_domains.json, then data/reference/annotation_domains.json."
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
        help="Skip rows whose clip is missing and cannot be copied or rebuilt from a retained source video.",
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


def load_project_settings(config_paths: list[str]) -> dict[str, Any]:
    if not config_paths:
        return {}
    config = load_config(config_paths)
    project = config.get("project")
    return project if isinstance(project, dict) else {}


def load_annotation_field_order(path: Path | None) -> list[str]:
    if path is None:
        return []
    payload = load_json(path)
    fields = payload.get("fields", {})
    if not isinstance(fields, dict):
        return []
    return list(fields)


class ProgressBar:
    def __init__(self, total: int, *, enabled: bool = True, width: int = 32, stream: Any | None = None) -> None:
        self.total = max(0, total)
        self.enabled = enabled
        self.width = width
        self.stream = stream or sys.stderr
        isatty = getattr(self.stream, "isatty", None)
        self.interactive = bool(callable(isatty) and isatty())
        self.report_every = 1 if self.total <= 20 else max(1, self.total // 20)
        self.last_line_len = 0
        self.last_line = ""
        self.last_reported_current: int | None = None

    def _format_line(self, current: int, *, exported: int, skipped: int, carriage_return: bool) -> str:
        prefix = "\r" if carriage_return else ""
        if self.total <= 0:
            ratio = 1.0
        else:
            ratio = min(max(current / self.total, 0.0), 1.0)
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        percent = ratio * 100
        return (
            f"{prefix}Exporting [{bar}] {current}/{self.total} "
            f"{percent:6.2f}% exported={exported} skipped={skipped}"
        )

    def update(self, current: int, *, exported: int, skipped: int) -> None:
        if not self.enabled:
            return

        line = self._format_line(
            current,
            exported=exported,
            skipped=skipped,
            carriage_return=self.interactive,
        )
        if line == self.last_line:
            return

        if self.interactive:
            padding = " " * max(0, self.last_line_len - len(line))
            self.stream.write(line + padding)
            self.stream.flush()
            self.last_line_len = len(line)
        else:
            should_report = (
                self.last_reported_current is None
                or current == 0
                or current >= self.total
                or current - self.last_reported_current >= self.report_every
            )
            if not should_report:
                return
            self.stream.write(line + "\n")
            self.stream.flush()
            self.last_reported_current = current
        self.last_line = line

    def finish(self, *, current: int, exported: int, skipped: int) -> None:
        if not self.enabled:
            return
        self.update(current, exported=exported, skipped=skipped)
        if self.interactive:
            self.stream.write("\n")
            self.stream.flush()

    def newline(self) -> None:
        if self.enabled and self.interactive:
            self.stream.write("\n")
            self.stream.flush()


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
              a.status AS annotation_status,
              a.started_at AS annotation_started_at,
              a.finished_at AS annotation_finished_at,
              a.elapsed_sec AS annotation_elapsed_sec,
              a.review_required,
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
              v.raw_video_path,
              v.video_meta_path,
              (
                SELECT de.local_path
                FROM download_events de
                WHERE de.video_uid = v.video_uid
                  AND de.status = 'downloaded'
                  AND de.local_path IS NOT NULL
                ORDER BY de.event_id DESC
                LIMIT 1
              ) AS downloaded_local_path
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


def first_existing_path(*candidates: Path | None) -> Path | None:
    fallback: Path | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if fallback is None:
            fallback = candidate
        if candidate.exists():
            return candidate
    return fallback


def default_artifact_paths(runtime_root: Path, row: sqlite3.Row) -> dict[str, Path]:
    return {
        "clip": runtime_root / "artifacts" / "clips" / str(row["clip_uid"]) / "clip.mp4",
        "annotation": runtime_root / "artifacts" / "annotations" / f"{row['annotation_uid']}.json",
        "manifest": runtime_root / "artifacts" / "clips" / str(row["clip_uid"]) / "clip_manifest.json",
        "video_meta": runtime_root / "artifacts" / "videos" / str(row["video_uid"]) / "video_meta.json",
    }


def resolve_artifact_paths(runtime_root: Path, row: sqlite3.Row) -> dict[str, Path | None]:
    defaults = default_artifact_paths(runtime_root, row)
    return {
        "clip": first_existing_path(resolve_optional_path(row["clip_path"]), defaults["clip"]),
        "annotation": first_existing_path(resolve_optional_path(row["artifact_path"]), defaults["annotation"]),
        "manifest": first_existing_path(resolve_optional_path(row["manifest_path"]), defaults["manifest"]),
        "video_meta": first_existing_path(resolve_optional_path(row["video_meta_path"]), defaults["video_meta"]),
    }


def resolve_source_video_path(row: sqlite3.Row) -> Path | None:
    for candidate in (
        resolve_optional_path(row["raw_video_path"]),
        resolve_optional_path(row["downloaded_local_path"]),
    ):
        if candidate is not None and candidate.exists():
            return candidate
    return None


def source_video_hint(row: sqlite3.Row) -> Path | None:
    return first_existing_path(
        resolve_optional_path(row["raw_video_path"]),
        resolve_optional_path(row["downloaded_local_path"]),
    )


def build_raw_annotation_payload(row: sqlite3.Row, annotation_path: Path | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if annotation_path is not None and annotation_path.exists():
        loaded = load_json(annotation_path)
        if isinstance(loaded, dict):
            payload = loaded

    final_annotation = payload.get("final_annotation")
    if not isinstance(final_annotation, dict):
        final_annotation = loads_json(row["final_annotation_json"], {})

    field_confidence = payload.get("field_confidence")
    if not isinstance(field_confidence, dict):
        field_confidence = loads_json(row["field_confidence_json"], {})

    quality_flags = payload.get("quality_flags")
    if not isinstance(quality_flags, list):
        quality_flags = loads_json(row["quality_flags_json"], [])

    return {
        **payload,
        "annotation_uid": payload.get("annotation_uid", row["annotation_uid"]),
        "clip_uid": payload.get("clip_uid", row["clip_uid"]),
        "started_at": payload.get("started_at", row["annotation_started_at"]),
        "finished_at": payload.get("finished_at", row["annotation_finished_at"]),
        "elapsed_sec": payload.get("elapsed_sec", row["annotation_elapsed_sec"]),
        "final_annotation": final_annotation,
        "field_confidence": field_confidence,
        "quality_flags": quality_flags,
        "status": payload.get("status", row["annotation_status"]),
        "review_required": payload.get("review_required", bool(row["review_required"])),
    }


def trim_clip_from_source(source_path: Path, target_path: Path, *, start_sec: float, duration_sec: float) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start_sec),
        "-i",
        str(source_path),
        "-t",
        str(duration_sec),
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(target_path),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise SystemExit("ffmpeg not found while rebuilding a missing clip during export") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"failed to rebuild missing clip from source video: source={source_path} target={target_path}"
        ) from exc


def export_clip_file(
    *,
    row: sqlite3.Row,
    source_clip_path: Path | None,
    source_video_path: Path | None,
    target_path: Path,
) -> None:
    if source_clip_path is not None and source_clip_path.exists():
        shutil.copy2(source_clip_path, target_path)
        return

    if source_video_path is None or not source_video_path.exists():
        raise SystemExit(
            "missing required clip source for "
            f"clip_uid={row['clip_uid']}: clip={source_clip_path or '<empty>'}, "
            f"source_video={source_video_path or '<empty>'}"
        )

    trim_clip_from_source(
        source_video_path,
        target_path,
        start_sec=float(row["start_sec"]),
        duration_sec=float(row["clip_duration_sec"]),
    )


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
            "- `raw_annotations/` 保留原始标注 JSON；若原文件缺失，则自动用数据库内容回填生成。",
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
    runtime_root: Path,
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
        paths = resolve_artifact_paths(runtime_root, row)
        source_clip = paths["clip"] if paths["clip"] is not None and paths["clip"].exists() else None
        source_annotation = paths["annotation"] if paths["annotation"] is not None and paths["annotation"].exists() else None
        source_manifest = paths["manifest"] if paths["manifest"] is not None and paths["manifest"].exists() else None
        source_video_meta = paths["video_meta"] if paths["video_meta"] is not None and paths["video_meta"].exists() else None
        source_video = resolve_source_video_path(row)
        source_video_path_hint = source_video_hint(row)

        if source_clip is None and source_video is None:
            if skip_missing:
                skipped.append({"clip_uid": row["clip_uid"], "missing": "clip,source_video"})
                progress.update(index, exported=exported, skipped=len(skipped))
                continue
            progress.newline()
            missing_text = ", ".join(
                [
                    f"clip={paths['clip'] or '<empty>'}",
                    f"source_video={source_video_path_hint or '<empty>'}",
                ]
            )
            raise SystemExit(f"missing required file for clip_uid={row['clip_uid']}: {missing_text}")

        clip_uid = row["clip_uid"]
        clip_suffix = source_clip.suffix if source_clip is not None and source_clip.suffix else ".mp4"
        target_clip = clips_dir / f"{clip_uid}{clip_suffix}"
        export_clip_file(
            row=row,
            source_clip_path=source_clip,
            source_video_path=source_video,
            target_path=target_clip,
        )

        annotation_payload = build_raw_annotation_payload(row, source_annotation)
        raw_annotation_target = raw_annotations_dir / f"{clip_uid}.json"
        if source_annotation is not None and source_annotation.exists():
            shutil.copy2(source_annotation, raw_annotation_target)
        else:
            raw_annotation_target.write_text(
                json.dumps(annotation_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

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
    project = load_project_settings(args.config_paths)

    runtime_root_raw = args.runtime_root or project.get("runtime_dir") or "runtime"
    runtime_root = resolve_path(runtime_root_raw)
    db_path = resolve_path(args.db_path) if args.db_path else runtime_root / "index.sqlite"
    output_root = resolve_path(args.output_root) if args.output_root else runtime_root / "exports"

    annotation_domains_raw = args.annotation_domains
    if annotation_domains_raw is None:
        config_annotation_domains = project.get("annotation_domains_path")
        if isinstance(config_annotation_domains, str) and config_annotation_domains.strip():
            annotation_domains_raw = config_annotation_domains
    annotation_domains_path = find_annotation_domains(runtime_root, annotation_domains_raw)
    caps = parse_caps(args.cap)

    result = export_dataset(
        db_path=db_path,
        runtime_root=runtime_root,
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
