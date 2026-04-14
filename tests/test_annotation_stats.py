from __future__ import annotations

import json
from pathlib import Path

from group_emotion_video.annotation_stats import organize_annotation_jsons, summarize_available_annotation_jsons


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_summarize_available_annotation_jsons_filters_done_and_reject(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "usable.json",
        {
            "annotation_uid": "ann_ok",
            "status": "done",
            "final_annotation": {"group_emotion": "joy"},
        },
    )
    _write_json(
        tmp_path / "with_reject_value.json",
        {
            "annotation_uid": "ann_bad",
            "status": "done",
            "final_annotation": {"group_emotion": "rejected"},
        },
    )
    _write_json(
        tmp_path / "failed.json",
        {
            "annotation_uid": "ann_failed",
            "status": "failed",
            "error": "timeout",
        },
    )
    (tmp_path / "invalid.json").write_text("{bad json", encoding="utf-8")
    _write_json(tmp_path / "nested" / "other.failed.json", {"error": "timeout"})

    summary = summarize_available_annotation_jsons(tmp_path)

    assert summary["total_json_files"] == 5
    assert summary["invalid_json_files"] == 1
    assert summary["non_object_json_files"] == 0
    assert summary["status_done_files"] == 2
    assert summary["status_not_done_files"] == 2
    assert summary["done_with_reject_keyword_files"] == 1
    assert summary["usable_json_files"] == 1
    assert summary["category_counts"] == {
        "done_with_reject_keyword": 1,
        "invalid_json": 1,
        "status_failed": 1,
        "status_missing": 1,
        "usable": 1,
    }
    assert summary["status_counts"] == {"done": 2, "failed": 1, "missing": 1}


def test_organize_annotation_jsons_writes_classified_output(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "organized"

    _write_json(
        input_dir / "usable.json",
        {
            "annotation_uid": "ann_ok",
            "status": "done",
            "final_annotation": {"group_emotion": "joy"},
        },
    )
    _write_json(
        input_dir / "done_reject.json",
        {
            "annotation_uid": "ann_bad",
            "status": "done",
            "final_annotation": {"group_emotion": "rejected"},
        },
    )
    _write_json(
        input_dir / "failed.json",
        {
            "annotation_uid": "ann_failed",
            "status": "failed",
        },
    )

    summary = organize_annotation_jsons(input_dir, output_dir, transfer_mode="copy")

    assert summary["usable_json_files"] == 1
    assert summary["done_with_reject_keyword_files"] == 1
    assert summary["status_not_done_files"] == 1
    assert (output_dir / "usable" / "usable.json").exists()
    assert (output_dir / "done_with_reject_keyword" / "done_reject.json").exists()
    assert (output_dir / "status_failed" / "failed.json").exists()
    assert (output_dir / "manifests" / "usable.jsonl").exists()
    assert (output_dir / "summary.json").exists()
