from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

from group_emotion_video.types import CandidateVideo, DownloadResult
from group_emotion_video.workflow import Workflow


class FakeBilibiliAdapter:
    def search(self, query_text: str, limit: int):
        return [
            CandidateVideo(
                platform="bilibili",
                platform_video_id="BV_TEST_001",
                url="https://www.bilibili.com/video/BV_TEST_001",
                title="学生群体现场欢呼",
                uploader="tester",
                publish_time="2026-04-12T10:00:00",
                duration_sec=36.0,
                cover_url="https://example.com/cover.jpg",
                description="学生群体很激动，大家一起欢呼。",
                tags=["学生", "群体", "欢呼"],
                categories=["教育"],
                subtitles_available=True,
                platform_metadata={"type": "education", "view_count": 10},
                extra={},
            )
        ][:limit]

    def enrich(self, candidate: CandidateVideo):
        return CandidateVideo(
            platform=candidate.platform,
            platform_video_id=candidate.platform_video_id,
            url=candidate.url,
            title=candidate.title,
            uploader=candidate.uploader,
            publish_time=candidate.publish_time,
            duration_sec=candidate.duration_sec,
            cover_url=candidate.cover_url,
            description=candidate.description,
            tags=candidate.tags,
            categories=candidate.categories,
            subtitles_available=True,
            platform_metadata={
                **candidate.platform_metadata,
                "webpage_url": candidate.url,
                "rights_status": "public",
            },
            extra={
                "subtitle_segments": [
                    {"segment_id": "seg1", "start": 0.0, "end": 5.0, "text": "大家都很激动"},
                    {"segment_id": "seg2", "start": 5.0, "end": 10.0, "text": "学生群体一起欢呼"},
                ]
            },
        )

    def download(self, candidate: CandidateVideo, output_dir: str):
        output_path = Path(output_dir) / f"{candidate.platform_video_id}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-video-bytes")
        return DownloadResult(status="downloaded", local_path=str(output_path), file_size_bytes=output_path.stat().st_size)


class CyclingFakeBilibiliAdapter(FakeBilibiliAdapter):
    def __init__(self) -> None:
        self._search_counter = 0

    def search(self, query_text: str, limit: int):
        self._search_counter += 1
        suffix = f"{self._search_counter:03d}"
        return [
            CandidateVideo(
                platform="bilibili",
                platform_video_id=f"BV_TEST_{suffix}",
                url=f"https://www.bilibili.com/video/BV_TEST_{suffix}",
                title=f"学生群体现场欢呼 {suffix}",
                uploader="tester",
                publish_time="2026-04-12T10:00:00",
                duration_sec=36.0,
                cover_url="https://example.com/cover.jpg",
                description="学生群体很激动，大家一起欢呼。",
                tags=["学生", "群体", "欢呼"],
                categories=["教育"],
                subtitles_available=True,
                platform_metadata={"type": "education", "view_count": 10},
                extra={},
            )
        ][:limit]


class FakeAnnotator:
    def annotate(self, clip_record):
        final_annotation = {
            "scene_description": "课堂里学生集体欢呼",
            "group_size_estimate": "medium(11-30)",
            "valence": 8,
            "arousal": 7,
            "trigger_event_type": "speech_act",
            "trigger_event_description": "老师宣布结果",
            "group_behavior_type": "watching",
            "group_behavior_description": "学生先看向老师，随后一起欢呼",
            "group_emotion": "Joy",
            "behavior_sync": "high",
            "heterogeneity.dominant_proportion": "majority(60-90%)",
            "heterogeneity.minority_dissent": "none",
            "heterogeneity.alignment_type": "convergent",
            "appraisal.novelty": "expected",
            "appraisal.goal_congruence": "congruent",
            "appraisal.agency": "other_person",
            "contagion.origin": "external_stimulus",
            "contagion.spread_pattern": "collective_burst",
            "temporal.trajectory_type": "peak_valley",
            "emotion_layers.layer_consistency": "consistent",
            "social_structure.norm_context": "High_Constraint",
            "social_structure.ingroup_salience": "high",
        }
        return (
            {"single_model": {"final_annotation": final_annotation}},
            final_annotation,
            {"overall": 0.91, "group_emotion": 0.95},
            [],
            "done",
            False,
        )


def _write_seed_excel(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "情感因素"
    sheet.append(["情境场景", "触发事件"])
    sheet.append(["封闭室内中空间（教室/会议室）", "公开正面能力认可（当众表扬/荣誉称号/竞赛获奖/推荐提名）"])
    workbook.save(path)


def _write_schema_json(path: Path) -> None:
    fields = [
        ("scene_description", "string", "自由文本", "是"),
        ("group_size_estimate", "enum", "small(3-10) | medium(11-30) | large(31-100) | massive(100+)", "是"),
        ("valence", "integer", "1–9（SAM 9点量表）", "是"),
        ("arousal", "integer", "1–9（SAM 9点量表）", "是"),
        ("trigger_event_type", "enum", "speech_act | physical_action | external_event | social_interaction | environmental_change | media_stimulus | ritual_procedure | none", "是"),
        ("trigger_event_description", "string", "自由文本", "type≠none 时必填"),
        ("group_behavior_type", "enum", "cheering | applauding | laughing | crying | shouting | arguing | silent | chanting | moving_together | dispersing | watching | working | other", "是"),
        ("group_behavior_description", "string", "自由文本", "是"),
        ("group_emotion", "enum", "联合标签池", "是"),
        ("behavior_sync", "enum", "low | medium | high", "是"),
        ("heterogeneity.dominant_proportion", "enum", "near_all(>90%) | majority(60-90%) | plurality(40-60%) | minority(<40%)", "是"),
        ("heterogeneity.minority_dissent", "enum", "none | mild | strong", "是"),
        ("heterogeneity.alignment_type", "enum", "convergent | polarized | fragmented", "是"),
        ("appraisal.novelty", "enum", "expected | novel | extremely_novel", "是"),
        ("appraisal.goal_congruence", "enum", "congruent | incongruent | mixed", "是"),
        ("appraisal.agency", "enum", "self_group | other_person | other_group | circumstance", "是"),
        ("contagion.origin", "enum", "individual | subgroup | external_stimulus | indeterminate", "是"),
        ("contagion.spread_pattern", "enum", "radial | chain | collective_burst | none", "是"),
        ("temporal.trajectory_type", "enum", "monotonic_rise | monotonic_fall | peak_valley | valley_peak | plateau | complex", "是"),
        ("emotion_layers.layer_consistency", "enum", "consistent | suppression | amplification | masking", "是"),
        ("social_structure.norm_context", "enum", "High_Constraint | Moderate_Constraint | Low_Constraint", "是"),
        ("social_structure.ingroup_salience", "enum", "none | low | high", "是"),
    ]
    rows = [
        {"字段名": name, "数据类型": dtype, "值域": domain, "必填": required, "说明": name}
        for name, dtype, domain, required in fields
    ]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def test_workflow_end_to_end_offline(tmp_path: Path) -> None:
    excel_path = tmp_path / "原子情感因素.xlsx"
    schema_path = tmp_path / "schema.json"
    label_seed_path = tmp_path / "labels.json"
    _write_seed_excel(excel_path)
    _write_schema_json(schema_path)
    label_seed_path.write_text(json.dumps({"labels": ["Joy", "Anxiety"]}, ensure_ascii=False), encoding="utf-8")

    config = {
        "project": {
            "name": "test_project",
            "root_dir": str(tmp_path),
            "data_dir": "data",
            "runtime_dir": "runtime",
            "reference_dir": "data/reference",
            "query_seed_catalog_path": "data/reference/query_seed_catalog.json",
            "annotation_domains_path": "data/reference/annotation_domains.json",
            "query_seed_excel_path": str(excel_path),
            "query_seed_sheet_name": "情感因素",
            "annotation_schema_source_path": str(schema_path),
            "group_emotion_label_seed_path": str(label_seed_path),
        },
        "sources": {
            "default": "bilibili",
            "bilibili": {"enabled": True, "max_pages_per_query": 1, "max_items_per_query": 3},
        },
        "crawl": {
            "max_queries_per_run": 10,
            "min_duration_sec": 15,
            "max_duration_sec": 1200,
            "max_video_size_mb": 300,
            "enrich_workers": 1,
            "max_items_per_query": 3,
            "download": {"enabled": True, "workers": 2, "max_inflight_per_host": 2, "retries": 1, "fragment_retries": 1, "socket_timeout_sec": 10},
        },
        "preprocessing": {
            "max_videos_per_run": 5,
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
        },
        "annotation": {"max_clips_per_run": 10, "clip_workers": 2, "min_overall_confidence": 0.6, "export_requires_review_clear": False},
        "llm": {
            "enabled": False,
            "base_url": "https://yunwu.ai/v1/",
            "api_key_env": "YUNWU_API_KEY",
            "model": "gemini-3.1-pro-preview",
            "timeout_sec": 30,
            "temperature": 0.1,
            "max_images_per_prompt": 2,
            "parallel": {"enabled": True, "max_inflight_requests": 2, "acquire_timeout_sec": 30},
        },
        "service": {
            "download_loop": {"max_queries_per_cycle": 10, "poll_interval_sec": 0.01, "idle_sleep_sec": 0.01, "max_cycles": 0, "stop_when_idle": True},
            "pipeline_loop": {
                "preprocess_claim_limit": 1,
                "annotation_trigger_size": 1,
                "annotation_batch_size": 1,
                "annotation_trigger_timeout_sec": 0,
                "queue_max_clips": 8,
                "poll_interval_sec": 0.01,
                "idle_sleep_sec": 0.01,
                "max_cycles": 0,
                "stop_when_idle": True,
            },
        },
    }

    workflow = Workflow(config, adapter=FakeBilibiliAdapter(), annotator=FakeAnnotator())
    workflow.prepare_reference()
    assert workflow.seed_queries() == 3
    assert workflow.crawl() == 1
    assert workflow.preprocess() == 1
    assert workflow.annotate() == 1
    status = workflow.status()
    export_dir = Path(workflow.export())

    assert export_dir.exists()
    exported_videos = list((export_dir / "videos").glob("*.mp4"))
    assert exported_videos

    tmp_videos = list((tmp_path / "runtime" / "tmp" / "videos").rglob("*.mp4"))
    assert not tmp_videos

    annotation_domains_copy = export_dir / "metadata" / "annotation_domains.json"
    assert annotation_domains_copy.exists()

    assert status["queries"] == {"total": 3, "pending": 0, "retry": 0, "done": 3}
    assert status["videos"]["downloaded"] == 1
    assert status["videos"]["pending_preprocess"] == 0
    assert status["clips"]["total"] == 1
    assert status["clips"]["pending_annotation"] == 0
    assert status["annotations"] == {"done": 1, "rejected": 0, "failed": 0, "completed": 1}
    assert status["average_durations_sec"]["download_per_downloaded_video"] is not None
    assert status["average_durations_sec"]["preprocess_per_processed_video"] is not None
    assert status["average_durations_sec"]["preprocess_per_accepted_clip"] is not None
    assert status["average_durations_sec"]["annotate_per_completed_clip"] is not None
    assert status["average_durations_sec"]["annotate_per_done_clip"] is not None
    assert status["projection_5d"]["workers"] == {"download": 2, "preprocess": 1, "annotate": 2}
    assert status["projection_5d"]["yield_assumptions"]["accepted_clips_per_processed_video"] == 1.0
    assert status["projection_5d"]["yield_assumptions"]["done_rate_after_annotation"] == 1.0


def test_service_loops_process_download_and_pipeline(tmp_path: Path) -> None:
    excel_path = tmp_path / "原子情感因素.xlsx"
    schema_path = tmp_path / "schema.json"
    label_seed_path = tmp_path / "labels.json"
    _write_seed_excel(excel_path)
    _write_schema_json(schema_path)
    label_seed_path.write_text(json.dumps({"labels": ["Joy", "Anxiety"]}, ensure_ascii=False), encoding="utf-8")

    config = {
        "project": {
            "name": "test_project",
            "root_dir": str(tmp_path),
            "data_dir": "data",
            "runtime_dir": "runtime",
            "reference_dir": "data/reference",
            "query_seed_catalog_path": "data/reference/query_seed_catalog.json",
            "annotation_domains_path": "data/reference/annotation_domains.json",
            "query_seed_excel_path": str(excel_path),
            "query_seed_sheet_name": "情感因素",
            "annotation_schema_source_path": str(schema_path),
            "group_emotion_label_seed_path": str(label_seed_path),
        },
        "sources": {
            "default": "bilibili",
            "bilibili": {"enabled": True, "max_pages_per_query": 1, "max_items_per_query": 3},
        },
        "crawl": {
            "max_queries_per_run": 10,
            "min_duration_sec": 15,
            "max_duration_sec": 1200,
            "max_video_size_mb": 300,
            "enrich_workers": 1,
            "max_items_per_query": 3,
            "download": {"enabled": True, "workers": 1, "max_inflight_per_host": 1, "retries": 1, "fragment_retries": 1, "socket_timeout_sec": 10},
        },
        "preprocessing": {
            "max_videos_per_run": 5,
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
        },
        "annotation": {"max_clips_per_run": 10, "clip_workers": 2, "min_overall_confidence": 0.6, "export_requires_review_clear": False},
        "llm": {
            "enabled": False,
            "base_url": "https://yunwu.ai/v1/",
            "api_key_env": "YUNWU_API_KEY",
            "model": "gemini-3.1-pro-preview",
            "timeout_sec": 30,
            "temperature": 0.1,
            "max_images_per_prompt": 2,
            "parallel": {"enabled": True, "max_inflight_requests": 2, "acquire_timeout_sec": 30},
        },
        "service": {
            "download_loop": {"max_queries_per_cycle": 10, "poll_interval_sec": 0.01, "idle_sleep_sec": 0.01, "max_cycles": 0, "stop_when_idle": True},
            "pipeline_loop": {
                "preprocess_claim_limit": 1,
                "annotation_trigger_size": 1,
                "annotation_batch_size": 1,
                "annotation_trigger_timeout_sec": 0,
                "queue_max_clips": 8,
                "poll_interval_sec": 0.01,
                "idle_sleep_sec": 0.01,
                "max_cycles": 0,
                "stop_when_idle": True,
            },
        },
    }

    workflow = Workflow(config, adapter=FakeBilibiliAdapter(), annotator=FakeAnnotator())
    workflow.prepare_reference()
    workflow.seed_queries()
    download_status = workflow.download_loop()
    pipeline_status = workflow.pipeline_loop()

    assert download_status["videos"]["downloaded"] == 1
    assert pipeline_status["videos"]["processed"] == 1
    assert pipeline_status["clips"]["accepted"] == 1
    assert pipeline_status["clips"]["pending_annotation"] == 0
    assert pipeline_status["clips"]["annotating"] == 0
    assert pipeline_status["annotations"]["done"] == 1


def test_download_loop_auto_seeds_and_recycles_queries(tmp_path: Path) -> None:
    excel_path = tmp_path / "原子情感因素.xlsx"
    schema_path = tmp_path / "schema.json"
    label_seed_path = tmp_path / "labels.json"
    _write_seed_excel(excel_path)
    _write_schema_json(schema_path)
    label_seed_path.write_text(json.dumps({"labels": ["Joy", "Anxiety"]}, ensure_ascii=False), encoding="utf-8")

    config = {
        "project": {
            "name": "test_project",
            "root_dir": str(tmp_path),
            "data_dir": "data",
            "runtime_dir": "runtime",
            "reference_dir": "data/reference",
            "query_seed_catalog_path": "data/reference/query_seed_catalog.json",
            "annotation_domains_path": "data/reference/annotation_domains.json",
            "query_seed_excel_path": str(excel_path),
            "query_seed_sheet_name": "情感因素",
            "annotation_schema_source_path": str(schema_path),
            "group_emotion_label_seed_path": str(label_seed_path),
        },
        "sources": {
            "default": "bilibili",
            "bilibili": {"enabled": True, "max_pages_per_query": 1, "max_items_per_query": 3},
        },
        "crawl": {
            "max_queries_per_run": 10,
            "min_duration_sec": 15,
            "max_duration_sec": 1200,
            "max_video_size_mb": 300,
            "enrich_workers": 1,
            "max_items_per_query": 3,
            "download": {"enabled": True, "workers": 1, "max_inflight_per_host": 1, "retries": 1, "fragment_retries": 1, "socket_timeout_sec": 10},
        },
        "preprocessing": {
            "max_videos_per_run": 5,
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
        },
        "annotation": {"max_clips_per_run": 10, "clip_workers": 2, "min_overall_confidence": 0.6, "export_requires_review_clear": False},
        "llm": {
            "enabled": False,
            "base_url": "https://yunwu.ai/v1/",
            "api_key_env": "YUNWU_API_KEY",
            "model": "gemini-3.1-pro-preview",
            "timeout_sec": 30,
            "temperature": 0.1,
            "max_images_per_prompt": 2,
            "parallel": {"enabled": True, "max_inflight_requests": 2, "acquire_timeout_sec": 30},
        },
        "service": {
            "download_loop": {
                "max_queries_per_cycle": 3,
                "auto_seed_if_empty": True,
                "auto_refill_done_queries": True,
                "refill_batch_size": 1,
                "query_recycle_cooldown_sec": 0,
                "max_runs_per_query": 2,
                "poll_interval_sec": 0.01,
                "idle_sleep_sec": 0.01,
                "max_cycles": 4,
                "stop_when_idle": False,
            }
        },
    }

    workflow = Workflow(config, adapter=CyclingFakeBilibiliAdapter(), annotator=FakeAnnotator())
    workflow.prepare_reference()
    status = workflow.download_loop()

    rows = workflow.db.fetchall("SELECT query_id, run_count, status FROM queries ORDER BY query_id")
    run_counts = [int(row["run_count"]) for row in rows]

    assert status["queries"]["total"] == 3
    assert status["queries"]["done"] == 3
    assert all(count >= 1 for count in run_counts)
    assert max(run_counts) == 2
    assert status["videos"]["downloaded"] >= 4


def test_zero_limits_mean_unbounded_loop_behavior(tmp_path: Path) -> None:
    excel_path = tmp_path / "原子情感因素.xlsx"
    schema_path = tmp_path / "schema.json"
    label_seed_path = tmp_path / "labels.json"
    _write_seed_excel(excel_path)
    _write_schema_json(schema_path)
    label_seed_path.write_text(json.dumps({"labels": ["Joy", "Anxiety"]}, ensure_ascii=False), encoding="utf-8")

    config = {
        "project": {
            "name": "test_project",
            "root_dir": str(tmp_path),
            "data_dir": "data",
            "runtime_dir": "runtime",
            "reference_dir": "data/reference",
            "query_seed_catalog_path": "data/reference/query_seed_catalog.json",
            "annotation_domains_path": "data/reference/annotation_domains.json",
            "query_seed_excel_path": str(excel_path),
            "query_seed_sheet_name": "情感因素",
            "annotation_schema_source_path": str(schema_path),
            "group_emotion_label_seed_path": str(label_seed_path),
        },
        "sources": {
            "default": "bilibili",
            "bilibili": {"enabled": True, "max_pages_per_query": 1, "max_items_per_query": 3},
        },
        "crawl": {
            "max_queries_per_run": 1,
            "min_duration_sec": 15,
            "max_duration_sec": 1200,
            "max_video_size_mb": 300,
            "enrich_workers": 1,
            "max_items_per_query": 3,
            "download": {"enabled": True, "workers": 1, "max_inflight_per_host": 1, "retries": 1, "fragment_retries": 1, "socket_timeout_sec": 10},
        },
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
        },
        "annotation": {"max_clips_per_run": 1, "clip_workers": 1, "min_overall_confidence": 0.6, "export_requires_review_clear": False},
        "llm": {
            "enabled": False,
            "base_url": "https://yunwu.ai/v1/",
            "api_key_env": "YUNWU_API_KEY",
            "model": "gemini-3.1-pro-preview",
            "timeout_sec": 30,
            "temperature": 0.1,
            "max_images_per_prompt": 2,
            "parallel": {"enabled": True, "max_inflight_requests": 1, "acquire_timeout_sec": 30},
        },
        "service": {
            "download_loop": {
                "max_queries_per_cycle": 0,
                "auto_seed_if_empty": False,
                "auto_refill_done_queries": False,
                "poll_interval_sec": 0.01,
                "idle_sleep_sec": 0.01,
                "max_cycles": 1,
                "stop_when_idle": False,
            },
            "pipeline_loop": {
                "preprocess_claim_limit": 0,
                "annotation_trigger_size": 0,
                "annotation_batch_size": 0,
                "annotation_trigger_timeout_sec": 30,
                "queue_max_clips": 0,
                "poll_interval_sec": 0.01,
                "idle_sleep_sec": 0.01,
                "max_cycles": 2,
                "stop_when_idle": False,
            },
        },
    }

    workflow = Workflow(config, adapter=FakeBilibiliAdapter(), annotator=FakeAnnotator())
    workflow.prepare_reference()
    workflow.seed_queries()

    download_status = workflow.download_loop()
    pipeline_status = workflow.pipeline_loop()

    assert download_status["queries"]["done"] == 3
    assert download_status["videos"]["downloaded"] == 1
    assert pipeline_status["videos"]["processed"] == 1
    assert pipeline_status["annotations"]["done"] == 1
