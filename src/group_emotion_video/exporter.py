from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


class ExportService:
    def __init__(self, *, layout: dict[str, Path], logger: Any):
        self.layout = layout
        self.logger = logger

    def export(self, rows: list[dict[str, Any]], annotation_domains_path: str | Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_dir = self.layout["exports"] / f"dataset_export_{stamp}"
        videos_dir = export_dir / "videos"
        metadata_dir = export_dir / "metadata"
        videos_dir.mkdir(parents=True, exist_ok=True)
        metadata_dir.mkdir(parents=True, exist_ok=True)

        manifest = {"created_at": stamp, "sample_count": len(rows), "samples": []}
        shutil.copy2(annotation_domains_path, metadata_dir / "annotation_domains.json")

        for row in rows:
            clip_uid = row["clip_uid"]
            clip_path = Path(row["clip_path"])
            annotation_path = Path(row["artifact_path"])
            clip_manifest_path = Path(row["manifest_path"])
            video_meta_path = Path(row["video_meta_path"])
            target_video_path = videos_dir / f"{clip_uid}{clip_path.suffix or '.mp4'}"
            shutil.copy2(clip_path, target_video_path)
            shutil.copy2(annotation_path, metadata_dir / f"{clip_uid}.annotation.json")
            shutil.copy2(clip_manifest_path, metadata_dir / f"{clip_uid}.clip_manifest.json")
            shutil.copy2(video_meta_path, metadata_dir / f"{clip_uid}.video_meta.json")
            manifest["samples"].append(
                {
                    "clip_uid": clip_uid,
                    "video_path": str(target_video_path.relative_to(export_dir)),
                    "annotation_path": str((metadata_dir / f"{clip_uid}.annotation.json").relative_to(export_dir)),
                }
            )

        (export_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return export_dir
