from __future__ import annotations

import json
from pathlib import Path

from group_emotion_video.dataset_stats import DatasetStatsService
from group_emotion_video.repositories import AnnotationRepository, ClipRepository, VideoRepository
from group_emotion_video.sqlite_db import SQLiteDB, initialize_schema


def _insert_sample(video_repo: VideoRepository, clip_repo: ClipRepository, annotation_repo: AnnotationRepository) -> None:
    video_repo.upsert(
        {
            "video_uid": "video_1",
            "platform": "bilibili",
            "bvid": "BV_AUDIT_001",
            "url": "https://www.bilibili.com/video/BV_AUDIT_001",
            "title": "学生群体欢呼",
            "uploader": "tester",
            "publish_time": "2026-05-05T00:00:00",
            "duration_sec": 30.0,
            "cover_url": None,
            "description": "全班一起欢呼",
            "tags": ["学生", "欢呼"],
            "categories": ["教育"],
            "view_count": 1,
            "like_count": 1,
            "comment_count": 0,
            "play_count": 1,
            "danmaku_count": 0,
            "type": "education",
            "webpage_url": "https://www.bilibili.com/video/BV_AUDIT_001",
            "query_id": "query_1",
            "scene_text": "教室",
            "trigger_text": "公开表扬",
            "source_excel_row": 2,
            "subtitles_available": True,
            "rights_status": "public",
            "download_status": "downloaded",
            "download_started_at": None,
            "download_finished_at": None,
            "download_elapsed_sec": None,
            "reject_reason": None,
            "retention_status": "processed_deleted",
            "accepted_clip_count": 1,
            "preprocess_started_at": None,
            "preprocess_finished_at": None,
            "preprocess_elapsed_sec": None,
            "raw_video_path": None,
            "video_meta_path": None,
            "platform_metadata": {},
            "extra": {},
        }
    )
    clip_repo.upsert(
        {
            "clip_uid": "clip_1",
            "video_uid": "video_1",
            "start_sec": 0.0,
            "end_sec": 10.0,
            "duration_sec": 10.0,
            "segmentation_mode": "fixed_window",
            "filter_status": "accepted",
            "filter_reasons": [],
            "retention_status": "accepted_kept",
            "clip_path": "/tmp/clip_1.mp4",
            "manifest_path": "/tmp/manifest.json",
            "keyframes_manifest_path": "/tmp/keyframes.json",
            "subtitle_path": "/tmp/subtitle.json",
            "scores": {"group_presence_score": 1.0},
            "annotation_status": "done",
        }
    )
    annotation_repo.upsert(
        {
            "annotation_uid": "ann_1",
            "clip_uid": "clip_1",
            "status": "done",
            "started_at": "2026-05-05T00:00:00",
            "finished_at": "2026-05-05T00:00:01",
            "elapsed_sec": 1.0,
            "review_required": True,
            "final_annotation": {
                "scene_description": "教室里学生集体欢呼",
                "trigger_event_type": "speech_act",
                "group_behavior_type": "cheering",
                "group_emotion": "Joy",
                "heterogeneity.alignment_type": "convergent",
            },
            "field_confidence": {"overall": 0.82, "group_emotion": 0.8},
            "quality_flags": ["candidate_disagreement"],
            "artifact_path": "/tmp/ann_1.json",
        }
    )


def test_annotation_audit_tables_and_dataset_stats(tmp_path: Path) -> None:
    db = SQLiteDB(tmp_path / "index.sqlite")
    initialize_schema(db)
    video_repo = VideoRepository(db)
    clip_repo = ClipRepository(db)
    annotation_repo = AnnotationRepository(db)
    _insert_sample(video_repo, clip_repo, annotation_repo)

    final_annotation = {
        "scene_description": "教室里学生集体欢呼",
        "trigger_event_type": "speech_act",
        "group_behavior_type": "cheering",
        "group_emotion": "Joy",
        "heterogeneity.alignment_type": "convergent",
    }
    agent_outputs = {
        "annotation_framework": "adjudicated_v1",
        "candidate_results": [
            {
                "candidate_id": "primary",
                "agent": "event_emotion",
                "model": "candidate-model",
                "status": "done",
                "final_annotation": final_annotation,
                "field_confidence": {"overall": 0.82},
                "quality_flags": [],
                "raw_payload": {"final_annotation": final_annotation},
            }
        ],
        "judge": {
            "enabled": True,
            "status": "done",
            "selected_candidate_ids": ["primary"],
            "conflict_fields": ["group_emotion"],
            "decision_reasoning": "候选存在差异，裁决为 Joy。",
            "final_annotation": final_annotation,
            "field_confidence": {"overall": 0.82},
            "quality_flags": [],
            "raw_payload": {"selected_candidate_ids": ["primary"]},
        },
    }
    annotation_repo.record_annotation_audit(
        annotation_uid="ann_1",
        clip_uid="clip_1",
        agent_outputs=agent_outputs,
        final_annotation=final_annotation,
        field_confidence={"overall": 0.82},
        quality_flags=["candidate_disagreement"],
        review_required=True,
    )
    annotation_repo.record_export_lineage(export_dir=str(tmp_path / "exports" / "dataset_export_20260505_000000"), rows=[{"annotation_uid": "ann_1", "clip_uid": "clip_1"}])

    domains_path = tmp_path / "annotation_domains.json"
    domains_path.write_text(
        json.dumps(
            {
                "fields": {
                    "scene_description": {},
                    "trigger_event_type": {},
                    "group_behavior_type": {},
                    "group_emotion": {},
                    "heterogeneity.alignment_type": {},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stats = DatasetStatsService(db, annotation_domains_path=domains_path).build(top_n=5)

    assert db.fetchone("SELECT COUNT(*) AS count FROM annotation_candidates")["count"] == 1
    assert db.fetchone("SELECT COUNT(*) AS count FROM judge_decisions")["count"] == 1
    assert db.fetchone("SELECT COUNT(*) AS count FROM human_reviews WHERE status='pending'")["count"] == 1
    assert db.fetchone("SELECT COUNT(*) AS count FROM lineage_edges")["count"] >= 6
    assert stats["summary"]["done_annotations"] == 1
    assert stats["coverage"]["scene_distribution"] == [{"value": "教室", "count": 1}]
    assert stats["coverage"]["group_emotion_distribution"] == [{"value": "Joy", "count": 1}]
    assert stats["coverage"]["field_coverage"][-1] == {
        "field": "heterogeneity.alignment_type",
        "present": 1,
        "total": 1,
        "coverage": 1.0,
    }
    assert stats["audit"]["annotation_candidates"] == 1
    assert stats["audit"]["pending_human_reviews"] == 1


def test_human_review_completion_releases_exportable_sample(tmp_path: Path) -> None:
    db = SQLiteDB(tmp_path / "index.sqlite")
    initialize_schema(db)
    video_repo = VideoRepository(db)
    clip_repo = ClipRepository(db)
    annotation_repo = AnnotationRepository(db)
    _insert_sample(video_repo, clip_repo, annotation_repo)

    final_annotation = {
        "scene_description": "教室里学生集体欢呼",
        "trigger_event_type": "speech_act",
        "group_behavior_type": "cheering",
        "group_emotion": "Joy",
    }
    annotation_repo.record_annotation_audit(
        annotation_uid="ann_1",
        clip_uid="clip_1",
        agent_outputs={
            "annotation_framework": "adjudicated_v1",
            "candidate_results": [],
            "judge": {"enabled": False},
        },
        final_annotation=final_annotation,
        field_confidence={"overall": 0.82},
        quality_flags=["confidence_review_band"],
        review_required=True,
    )

    assert video_repo.list_exportable(require_review_clear=True) == []
    reviews = annotation_repo.list_human_reviews(status="pending", limit=10)
    assert len(reviews) == 1

    result = annotation_repo.complete_human_review(
        review_uid=reviews[0]["review_uid"],
        after_annotation={**final_annotation, "group_emotion": "Joy"},
        reviewer="tester",
        review_notes="confirmed",
    )

    assert result["status"] == "completed"
    assert db.fetchone("SELECT review_required FROM annotations WHERE annotation_uid='ann_1'")["review_required"] == 0
    assert db.fetchone("SELECT status FROM human_reviews WHERE review_uid=?", (reviews[0]["review_uid"],))["status"] == "completed"
    assert len(video_repo.list_exportable(require_review_clear=True)) == 1
