from __future__ import annotations

from pathlib import Path


def ensure_runtime_layout(root_dir: str | Path, runtime_dir: str | Path, data_dir: str | Path, reference_dir: str | Path) -> dict[str, Path]:
    root = Path(root_dir).resolve()
    runtime = (root / runtime_dir).resolve() if not Path(runtime_dir).is_absolute() else Path(runtime_dir).resolve()
    data = (root / data_dir).resolve() if not Path(data_dir).is_absolute() else Path(data_dir).resolve()
    reference = (root / reference_dir).resolve() if not Path(reference_dir).is_absolute() else Path(reference_dir).resolve()

    layout = {
        "root": root,
        "runtime": runtime,
        "data": data,
        "reference": reference,
        "db": runtime / "index.sqlite",
        "logs": runtime / "logs" / "runs",
        "artifacts": runtime / "artifacts",
        "artifact_queries": runtime / "artifacts" / "queries",
        "artifact_videos": runtime / "artifacts" / "videos",
        "artifact_clips": runtime / "artifacts" / "clips",
        "artifact_annotations": runtime / "artifacts" / "annotations",
        "artifact_rejections": runtime / "artifacts" / "rejections",
        "exports": runtime / "exports",
        "tmp_videos": runtime / "tmp" / "videos",
        "data_raw": data / "raw",
        "data_interim": data / "interim",
        "data_processed": data / "processed",
    }
    for path in layout.values():
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    return layout
