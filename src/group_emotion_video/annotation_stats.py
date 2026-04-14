from __future__ import annotations

import json
import os
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any


def _contains_keyword(value: Any, keyword: str) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if keyword in str(key).lower():
                return True
            if _contains_keyword(nested, keyword):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_keyword(item, keyword) for item in value)
    if isinstance(value, str):
        return keyword in value.lower()
    return False


def _sanitize_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return token or "unknown"


def _relative_output_path(base_dir: Path, target_path: Path) -> str:
    try:
        return str(target_path.relative_to(base_dir))
    except ValueError:
        return str(target_path)


def _build_summary(root_path: Path, reject_keyword: str, *, output_root: Path | None = None, transfer_mode: str | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path": str(root_path),
        "reject_keyword": reject_keyword,
        "total_json_files": 0,
        "invalid_json_files": 0,
        "non_object_json_files": 0,
        "status_done_files": 0,
        "status_not_done_files": 0,
        "done_with_reject_keyword_files": 0,
        "usable_json_files": 0,
        "category_counts": {},
        "status_counts": {},
    }
    if output_root is not None:
        summary["output_root"] = str(output_root)
    if transfer_mode is not None:
        summary["transfer_mode"] = transfer_mode
    return summary


def scan_annotation_jsons(root: str | Path, *, reject_keyword: str = "reject") -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        raise FileNotFoundError(f"Annotation root does not exist: {root_path}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"Annotation root is not a directory: {root_path}")

    summary = _build_summary(root_path, reject_keyword)
    keyword = reject_keyword.lower()
    category_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    records: list[dict[str, Any]] = []

    for path in sorted(root_path.rglob("*.json")):
        summary["total_json_files"] += 1
        relative_path = str(path.relative_to(root_path))
        record: dict[str, Any] = {
            "source_path": str(path),
            "relative_path": relative_path,
            "status": None,
            "category": None,
            "contains_reject_keyword": False,
        }

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            record["category"] = "invalid_json"
            record["error"] = str(exc)
            summary["invalid_json_files"] += 1
            category_counts[record["category"]] += 1
            records.append(record)
            continue

        if not isinstance(payload, dict):
            record["category"] = "non_object_json"
            summary["non_object_json_files"] += 1
            category_counts[record["category"]] += 1
            records.append(record)
            continue

        raw_status = payload.get("status")
        status = str(raw_status).strip().lower() if raw_status is not None else ""
        record["status"] = status or None
        normalized_status = status or "missing"
        status_counts[normalized_status] += 1

        if status != "done":
            record["category"] = f"status_{_sanitize_token(normalized_status)}"
            summary["status_not_done_files"] += 1
            category_counts[record["category"]] += 1
            records.append(record)
            continue

        summary["status_done_files"] += 1
        contains_reject_keyword = _contains_keyword(payload, keyword)
        record["contains_reject_keyword"] = contains_reject_keyword
        if contains_reject_keyword:
            record["category"] = "done_with_reject_keyword"
            summary["done_with_reject_keyword_files"] += 1
        else:
            record["category"] = "usable"
            summary["usable_json_files"] += 1

        category_counts[record["category"]] += 1
        records.append(record)

    summary["category_counts"] = dict(sorted(category_counts.items()))
    summary["status_counts"] = dict(sorted(status_counts.items()))
    return {"summary": summary, "records": records}


def summarize_available_annotation_jsons(root: str | Path, *, reject_keyword: str = "reject") -> dict[str, Any]:
    return scan_annotation_jsons(root, reject_keyword=reject_keyword)["summary"]


def _transfer_file(source: Path, target: Path, *, transfer_mode: str) -> None:
    if target.exists() or target.is_symlink():
        target.unlink()

    if transfer_mode == "copy":
        shutil.copy2(source, target)
        return
    if transfer_mode == "hardlink":
        os.link(source, target)
        return
    if transfer_mode == "symlink":
        target.symlink_to(source)
        return
    raise ValueError(f"Unsupported transfer mode: {transfer_mode}")


def organize_annotation_jsons(
    root: str | Path,
    output_root: str | Path,
    *,
    reject_keyword: str = "reject",
    transfer_mode: str = "copy",
    clear_output: bool = False,
) -> dict[str, Any]:
    scan_result = scan_annotation_jsons(root, reject_keyword=reject_keyword)
    summary = dict(scan_result["summary"])
    records = scan_result["records"]

    root_path = Path(summary["path"]).resolve()
    output_path = Path(output_root).expanduser().resolve()
    if output_path == root_path or root_path in output_path.parents or output_path in root_path.parents:
        raise ValueError("Output root must not contain input root, and input root must not contain output root.")

    if output_path.exists():
        if clear_output:
            shutil.rmtree(output_path)
        elif any(output_path.iterdir()):
            raise FileExistsError(f"Output root already exists and is not empty: {output_path}")

    output_path.mkdir(parents=True, exist_ok=True)
    manifests_dir = output_path / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    for record in records:
        source_path = Path(record["source_path"])
        category_dir = output_path / str(record["category"])
        target_path = category_dir / str(record["relative_path"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _transfer_file(source_path, target_path, transfer_mode=transfer_mode)
        record["target_path"] = str(target_path)
        record["target_relative_path"] = _relative_output_path(output_path, target_path)

    for category in sorted({str(record["category"]) for record in records}):
        manifest_path = manifests_dir / f"{category}.jsonl"
        with manifest_path.open("w", encoding="utf-8") as handle:
            for record in records:
                if record["category"] != category:
                    continue
                handle.write(json.dumps(record, ensure_ascii=False))
                handle.write("\n")

    summary["output_root"] = str(output_path)
    summary["transfer_mode"] = transfer_mode
    summary["manifest_dir"] = str(manifests_dir)
    summary_path = output_path / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
