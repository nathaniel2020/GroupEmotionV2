from __future__ import annotations

import logging
from pathlib import Path

from group_emotion_video.preprocessing import PreprocessingService
from group_emotion_video.repositories import AnnotationRepository, ClipRepository
from group_emotion_video.sqlite_db import SQLiteDB, initialize_schema


def _build_service(tmp_path: Path) -> PreprocessingService:
    config = {
        "preprocessing": {
            "max_videos_per_run": 1,
            "workers": 1,
            "prefer_subtitles": True,
            "fixed_window_sec": 12,
            "overlap_sec": 2,
            "subtitle_target_min_sec": 8,
            "subtitle_target_max_sec": 15,
            "min_clip_duration_sec": 4,
            "max_clip_duration_sec": 20,
            "clip_export_mode": "copy",
            "keyframe_count": 2,
            "use_llm_filter": False,
            "duplicate_overlap_threshold": 0.8,
        }
    }
    return PreprocessingService(
        config,
        video_repo=None,
        clip_repo=None,
        layout={"artifact_clips": tmp_path / "clips", "artifact_rejections": tmp_path / "rejections"},
        logger=logging.getLogger("test-preprocessing"),
        llm=None,
    )


def test_l2_filter_uses_metadata_when_subtitles_are_dialogue_only(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    video_record = {
        "title": "学生群体现场欢呼",
        "description": "大家一起很激动",
        "tags": ["学生", "欢呼"],
        "scene_text": "固定座位空间（教室/考场）",
        "trigger_text": "公开正面能力认可（当众表扬/荣誉称号）",
    }
    window = {
        "start_sec": 0.0,
        "end_sec": 10.0,
        "segmentation_mode": "subtitle",
        "transcript_segments": [
            {"segment_id": "seg1", "start": 0.0, "end": 5.0, "text": "今天公布结果"},
            {"segment_id": "seg2", "start": 5.0, "end": 10.0, "text": "请先回到座位"},
        ],
    }

    accepted, scores, reasons = service._l2_filter(video_record, window)

    assert accepted
    assert reasons == []
    assert scores["transcript_group_signal_score"] == 0.0
    assert scores["transcript_emotion_signal_score"] == 0.0
    assert scores["metadata_group_signal_score"] == 1.0
    assert scores["metadata_emotion_signal_score"] == 1.0


def test_clip_summary_reports_top_rejection_reasons(tmp_path: Path) -> None:
    db = SQLiteDB(tmp_path / "runtime" / "index.sqlite")
    initialize_schema(db)
    repo = ClipRepository(db)
    repo.upsert(
        {
            "clip_uid": "clip_a",
            "video_uid": "video_a",
            "start_sec": 0.0,
            "end_sec": 10.0,
            "duration_sec": 10.0,
            "segmentation_mode": "subtitle",
            "filter_status": "rejected",
            "filter_reasons": ["weak_group_signal", "weak_emotion_signal"],
            "retention_status": "rejected_deleted",
            "clip_path": None,
            "manifest_path": None,
            "keyframes_manifest_path": None,
            "subtitle_path": None,
            "scores": {},
            "annotation_status": "rejected",
        }
    )
    repo.upsert(
        {
            "clip_uid": "clip_b",
            "video_uid": "video_b",
            "start_sec": 10.0,
            "end_sec": 20.0,
            "duration_sec": 10.0,
            "segmentation_mode": "subtitle",
            "filter_status": "rejected",
            "filter_reasons": ["weak_group_signal"],
            "retention_status": "rejected_deleted",
            "clip_path": None,
            "manifest_path": None,
            "keyframes_manifest_path": None,
            "subtitle_path": None,
            "scores": {},
            "annotation_status": "rejected",
        }
    )

    summary = repo.summary()

    assert summary["rejected"] == 2
    assert summary["top_rejection_reasons"] == [
        {"reason": "weak_group_signal", "count": 2},
        {"reason": "weak_emotion_signal", "count": 1},
    ]


def test_annotation_summary_reports_top_quality_flags(tmp_path: Path) -> None:
    db = SQLiteDB(tmp_path / "runtime" / "index.sqlite")
    initialize_schema(db)
    repo = AnnotationRepository(db)
    repo.upsert(
        {
            "annotation_uid": "ann_a",
            "clip_uid": "clip_a",
            "status": "failed",
            "started_at": None,
            "finished_at": None,
            "elapsed_sec": 1.0,
            "review_required": False,
            "final_annotation": {},
            "field_confidence": {},
            "quality_flags": ["schema_invalid_enum", "low_confidence"],
            "artifact_path": None,
        }
    )
    repo.upsert(
        {
            "annotation_uid": "ann_b",
            "clip_uid": "clip_b",
            "status": "failed",
            "started_at": None,
            "finished_at": None,
            "elapsed_sec": 2.0,
            "review_required": False,
            "final_annotation": {},
            "field_confidence": {},
            "quality_flags": ["schema_invalid_enum"],
            "artifact_path": None,
        }
    )

    summary = repo.summary()

    assert summary["failed"] == 2
    assert summary["top_quality_flags"] == [
        {"flag": "schema_invalid_enum", "count": 2},
        {"flag": "low_confidence", "count": 1},
    ]
