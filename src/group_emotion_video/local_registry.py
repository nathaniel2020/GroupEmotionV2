from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .ids import make_query_id, make_seed_id, make_video_uid
from .repositories import QueryRepository, VideoRepository


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _safe_dir_name(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text).strip("._") or "scene"


def _normalize_scene_token(value: str) -> str:
    return re.sub(r"[\s\-_]+", "", str(value or "")).lower()


def _display_title(relative_path: Path) -> str:
    parts = [part for part in relative_path.with_suffix("").parts if part not in {".", ""}]
    title = " ".join(parts).strip()
    return title or relative_path.stem


def _dedupe_texts(values: list[str | None]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _transfer_file(source: Path, destination: Path, mode: str, overwrite: bool) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not overwrite:
            return "reused_existing"
        if destination.is_file() or destination.is_symlink():
            destination.unlink()
        else:
            raise IsADirectoryError(f"destination is not a file: {destination}")

    try:
        if source.resolve() == destination.resolve():
            return "reused_existing"
    except FileNotFoundError:
        pass

    if mode == "copy":
        shutil.copy2(source, destination)
    elif mode == "move":
        shutil.move(str(source), str(destination))
    elif mode == "hardlink":
        os.link(source, destination)
    elif mode == "symlink":
        os.symlink(source, destination)
    else:
        raise ValueError(f"unsupported transfer mode: {mode}")
    return mode


class LocalVideoRegistry:
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

    def _video_artifact_dir(self, video_uid: str) -> Path:
        path = self.layout["artifact_videos"] / video_uid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_video_meta(self, payload: dict[str, Any], *, video_uid: str) -> Path:
        path = self._video_artifact_dir(video_uid) / "video_meta.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _load_catalog_rows(self, catalog_path: Path | None) -> list[dict[str, Any]]:
        if not catalog_path or not catalog_path.exists():
            return []
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        rows = payload.get("rows")
        return rows if isinstance(rows, list) else []

    def _match_catalog_row(self, scene_token: str, catalog_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        normalized_token = _normalize_scene_token(scene_token)
        if not normalized_token:
            return None
        exact_matches = [row for row in catalog_rows if _normalize_scene_token(row.get("scene_text", "")) == normalized_token]
        if len(exact_matches) == 1:
            return exact_matches[0]
        fuzzy_matches = [
            row
            for row in catalog_rows
            if normalized_token in _normalize_scene_token(row.get("scene_text", ""))
            or _normalize_scene_token(row.get("scene_text", "")) in normalized_token
        ]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0]
        return None

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

    def _ensure_query(self, *, scene_text: str, trigger_text: str, query_text: str, excel_row: int) -> dict[str, Any]:
        seed_id = make_seed_id(excel_row, scene_text, trigger_text)
        query_id = make_query_id(seed_id, query_text)
        self.query_repo.upsert(
            {
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
        )
        return {
            "query_id": query_id,
            "seed_id": seed_id,
            "excel_row": excel_row,
            "scene_text": scene_text,
            "trigger_text": trigger_text,
            "query_text": query_text,
        }

    def register(
        self,
        input_root: str | Path,
        *,
        catalog_path: str | Path | None = None,
        scene_text: str | None = None,
        trigger_text: str | None = None,
        query_text: str | None = None,
        tags: list[str] | None = None,
        transfer_mode: str = "copy",
        replace_existing: bool = False,
        default_duration_sec: float | None = None,
    ) -> dict[str, Any]:
        root = Path(input_root).resolve()
        if not root.exists():
            raise FileNotFoundError(f"Input root does not exist: {root}")

        catalog_rows = self._load_catalog_rows(Path(catalog_path).resolve() if catalog_path else None)
        files = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS)

        summary = {
            "input_root": str(root),
            "db_path": str(self.layout["db"]),
            "discovered_files": len(files),
            "registered_videos": 0,
            "replaced_videos": 0,
            "skipped_existing": 0,
            "skipped_duration_probe_failed": 0,
            "staged_transfer_modes": {"copy": 0, "move": 0, "hardlink": 0, "symlink": 0, "reused_existing": 0},
        }
        if not files:
            return summary

        for index, source_path in enumerate(files, start=1):
            relative_path = source_path.relative_to(root)
            inferred_scene_token = relative_path.parts[0] if len(relative_path.parts) > 1 else ""
            matched_row = None if scene_text else self._match_catalog_row(inferred_scene_token, catalog_rows)

            resolved_scene_text = str(scene_text or (matched_row or {}).get("scene_text") or inferred_scene_token or "本地导入").strip()
            resolved_trigger_text = str(trigger_text if trigger_text is not None else (matched_row or {}).get("trigger_text") or "").strip()
            resolved_query_text = str(query_text or f"LOCAL_IMPORT::{resolved_scene_text}").strip()
            excel_row = int((matched_row or {}).get("excel_row") or (900000 + index))
            query_record = self._ensure_query(
                scene_text=resolved_scene_text,
                trigger_text=resolved_trigger_text,
                query_text=resolved_query_text,
                excel_row=excel_row,
            )

            stat = source_path.stat()
            platform_video_id = f"LOCAL_{make_seed_id(excel_row, str(relative_path), str(stat.st_mtime_ns))[-12:]}"
            video_uid = make_video_uid("local", platform_video_id)
            already_exists = self.video_repo.exists(platform_video_id)
            if already_exists and not replace_existing:
                summary["skipped_existing"] += 1
                continue

            started_at = datetime.now().isoformat(timespec="seconds")
            transfer_started = time.perf_counter()
            staged_dir = self.layout["tmp_videos"] / "local_import" / _safe_dir_name(resolved_scene_text)
            staged_path = staged_dir / f"{platform_video_id}{source_path.suffix.lower()}"
            transfer_result = _transfer_file(source_path, staged_path, transfer_mode, overwrite=replace_existing)
            transfer_elapsed_sec = round(time.perf_counter() - transfer_started, 3)
            finished_at = datetime.now().isoformat(timespec="seconds")
            summary["staged_transfer_modes"][transfer_result] += 1

            try:
                duration_sec = self._probe_duration_sec(source_path)
            except Exception:
                if default_duration_sec is None:
                    if staged_path.exists():
                        staged_path.unlink()
                    summary["skipped_duration_probe_failed"] += 1
                    self.logger.exception("local_register_duration_probe_failed source_path=%s", source_path)
                    continue
                duration_sec = float(default_duration_sec)

            relative_parent = str(relative_path.parent) if str(relative_path.parent) != "." else ""
            title = _display_title(relative_path)
            merged_tags = _dedupe_texts([*(tags or []), inferred_scene_token, resolved_scene_text, resolved_trigger_text])
            payload = {
                "video_uid": video_uid,
                "platform": "local",
                "bvid": platform_video_id,
                "url": f"file://{source_path}",
                "title": title,
                "uploader": "local_import",
                "publish_time": None,
                "duration_sec": duration_sec,
                "cover_url": None,
                "description": " ".join(part for part in [resolved_scene_text, resolved_trigger_text, relative_parent] if part).strip() or title,
                "tags": merged_tags,
                "categories": ["local_import"],
                "view_count": None,
                "like_count": None,
                "comment_count": None,
                "play_count": None,
                "danmaku_count": None,
                "type": "local_import",
                "webpage_url": f"file://{source_path}",
                "query_id": query_record["query_id"],
                "scene_text": resolved_scene_text,
                "trigger_text": resolved_trigger_text,
                "source_excel_row": query_record["excel_row"],
                "subtitles_available": False,
                "rights_status": "local_imported",
                "download_status": "downloaded",
                "download_started_at": started_at,
                "download_finished_at": finished_at,
                "download_elapsed_sec": transfer_elapsed_sec,
                "reject_reason": None,
                "retention_status": None,
                "accepted_clip_count": 0,
                "preprocess_started_at": None,
                "preprocess_finished_at": None,
                "preprocess_elapsed_sec": None,
                "raw_video_path": str(staged_path),
                "platform_metadata": {
                    "type": "local_import",
                    "rights_status": "local_imported",
                    "webpage_url": f"file://{source_path}",
                    "source_path": str(source_path),
                    "source_rel_path": str(relative_path),
                    "transfer_mode": transfer_result,
                },
                "extra": {
                    "local_import": {
                        "source_path": str(source_path),
                        "source_rel_path": str(relative_path),
                        "transfer_mode": transfer_result,
                        "imported_at": finished_at,
                    }
                },
            }
            video_meta_path = self._write_video_meta(payload, video_uid=video_uid)
            self.video_repo.upsert({**payload, "video_meta_path": str(video_meta_path)})
            self.video_repo.add_download_event(
                video_uid,
                "downloaded",
                local_path=str(staged_path),
                file_size_bytes=int(staged_path.stat().st_size),
                extra={"source_path": str(source_path), "import_mode": transfer_result},
            )

            if already_exists and replace_existing:
                summary["replaced_videos"] += 1
            else:
                summary["registered_videos"] += 1

            self.logger.info(
                "local_video_registered video_uid=%s source=%s staged=%s scene_text=%s",
                video_uid,
                source_path,
                staged_path,
                resolved_scene_text,
            )

        return summary
