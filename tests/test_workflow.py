from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

from group_emotion_video.cli import main as cli_main
from group_emotion_video.ids import make_query_id, make_seed_id
from group_emotion_video.ids import make_video_uid
from group_emotion_video.legacy_runtime_import import LegacyRuntimeImporter
from group_emotion_video.local_registry import LocalVideoRegistry
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


class FailingEnrichFakeBilibiliAdapter(FakeBilibiliAdapter):
    def enrich(self, candidate: CandidateVideo):
        raise Exception("ERROR: [BiliBili] BV1cZ421g7RQ: No video formats found!")

    def _format_download_error(self, exc: Exception) -> str:
        return f"formatted::{exc}"


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
    assert status["annotations"] == {"done": 1, "rejected": 0, "failed": 0, "completed": 1, "top_quality_flags": []}
    assert status["average_durations_sec"]["download_per_downloaded_video"] is not None
    assert status["average_durations_sec"]["preprocess_per_processed_video"] is not None
    assert status["average_durations_sec"]["preprocess_per_accepted_clip"] is not None
    assert status["average_durations_sec"]["annotate_per_completed_clip"] is not None
    assert status["average_durations_sec"]["annotate_per_done_clip"] is not None
    assert status["projection_5d"]["workers"] == {"download": 2, "preprocess": 1, "annotate": 2}
    assert status["projection_5d"]["yield_assumptions"]["accepted_clips_per_processed_video"] == 1.0
    assert status["projection_5d"]["yield_assumptions"]["done_rate_after_annotation"] == 1.0


def test_workflow_crawl_survives_enrich_failure_and_marks_rejection(tmp_path: Path) -> None:
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

    workflow = Workflow(config, adapter=FailingEnrichFakeBilibiliAdapter(), annotator=FakeAnnotator())
    workflow.prepare_reference()
    assert workflow.seed_queries() == 3

    processed = workflow.crawl()
    status = workflow.status()
    row = workflow.db.fetchone("SELECT download_status, reject_reason, raw_video_path FROM videos WHERE bvid=?", ("BV_TEST_001",))

    assert processed == 1
    assert row is not None
    assert row["download_status"] == "rejected"
    assert row["reject_reason"] == "formatted::ERROR: [BiliBili] BV1cZ421g7RQ: No video formats found!"
    assert row["raw_video_path"] is None
    assert status["queries"] == {"total": 3, "pending": 0, "retry": 0, "done": 3}
    assert status["videos"]["total"] == 1
    assert status["videos"]["downloaded"] == 0
    assert status["videos"]["pending_preprocess"] == 0


def test_prepare_reference_accepts_manual_input_paths_from_cli(tmp_path: Path, capsys) -> None:
    excel_path = tmp_path / "manual_seed.xlsx"
    schema_path = tmp_path / "manual_schema.json"
    label_seed_path = tmp_path / "manual_labels.json"
    config_path = tmp_path / "config.yaml"
    query_seed_catalog_path = tmp_path / "outputs" / "query_seed_catalog.json"
    annotation_domains_path = tmp_path / "outputs" / "annotation_domains.json"

    _write_seed_excel(excel_path)
    _write_schema_json(schema_path)
    label_seed_path.write_text(json.dumps({"labels": ["Joy", "Anxiety"]}, ensure_ascii=False), encoding="utf-8")
    config_path.write_text(
        "\n".join(
            [
                "project:",
                "  data_dir: data",
                "  runtime_dir: runtime",
                "  reference_dir: data/reference",
                "  query_seed_catalog_path: data/reference/query_seed_catalog.json",
                "  annotation_domains_path: data/reference/annotation_domains.json",
                "  query_seed_excel_path: missing.xlsx",
                "  query_seed_sheet_name: 情感因素",
                "  annotation_schema_source_path: missing_schema.json",
                "  group_emotion_label_seed_path: missing_labels.json",
                "sources:",
                "  default: bilibili",
                "  bilibili:",
                "    enabled: true",
                "crawl:",
                "  max_queries_per_run: 1",
                "  min_duration_sec: 15",
                "  max_duration_sec: 1200",
                "  max_video_size_mb: 300",
                "  enrich_workers: 1",
                "  max_items_per_query: 1",
                "  download:",
                "    enabled: false",
                "preprocessing:",
                "  max_videos_per_run: 1",
                "  workers: 1",
                "  prefer_subtitles: true",
                "  fixed_window_sec: 12",
                "  overlap_sec: 2",
                "  subtitle_target_min_sec: 8",
                "  subtitle_target_max_sec: 15",
                "  min_clip_duration_sec: 4",
                "  max_clip_duration_sec: 20",
                "  clip_export_mode: copy",
                "  keyframe_count: 2",
                "  use_llm_filter: false",
                "  duplicate_overlap_threshold: 0.8",
                "annotation:",
                "  max_clips_per_run: 1",
                "  clip_workers: 1",
                "  min_overall_confidence: 0.6",
                "  export_requires_review_clear: false",
                "llm:",
                "  enabled: false",
                "  base_url: https://example.com/v1",
                "  api_key_env: OPENAI_API_KEY",
                "  model: dummy",
                "  timeout_sec: 30",
                "  temperature: 0.1",
                "  max_images_per_prompt: 0",
                "  parallel:",
                "    enabled: false",
                "    max_inflight_requests: 1",
                "    acquire_timeout_sec: 30",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "--config",
            str(config_path),
            "prepare-reference",
            "--excel-path",
            str(excel_path),
            "--sheet-name",
            "情感因素",
            "--schema-source-path",
            str(schema_path),
            "--label-seed-path",
            str(label_seed_path),
            "--query-seed-catalog-path",
            str(query_seed_catalog_path),
            "--annotation-domains-path",
            str(annotation_domains_path),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert Path(payload["query_seed_catalog_path"]).resolve() == query_seed_catalog_path.resolve()
    assert Path(payload["annotation_domains_path"]).resolve() == annotation_domains_path.resolve()
    assert query_seed_catalog_path.exists()
    assert annotation_domains_path.exists()


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


def test_status_counts_inflight_downloads_and_download_loop_logs_are_stage_scoped(tmp_path: Path) -> None:
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
            "download_loop": {"max_queries_per_cycle": 10, "poll_interval_sec": 0.01, "idle_sleep_sec": 0.01, "max_cycles": 1, "stop_when_idle": False},
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
    video_uid = make_video_uid("bilibili", "BV_INFLIGHT_001")
    workflow.video_repo.upsert(
        {
            "video_uid": video_uid,
            "platform": "bilibili",
            "bvid": "BV_INFLIGHT_001",
            "url": "https://www.bilibili.com/video/BV_INFLIGHT_001",
            "title": "下载中样本",
            "uploader": "tester",
            "publish_time": "2026-04-13T00:00:00",
            "duration_sec": 36.0,
            "cover_url": None,
            "description": "downloading",
            "tags": [],
            "categories": [],
            "view_count": 1,
            "like_count": 1,
            "comment_count": 0,
            "play_count": 1,
            "danmaku_count": 0,
            "type": "education",
            "webpage_url": "https://www.bilibili.com/video/BV_INFLIGHT_001",
            "query_id": "q1",
            "scene_text": "教室",
            "trigger_text": "表扬",
            "source_excel_row": 1,
            "subtitles_available": False,
            "rights_status": "unknown",
            "download_status": "downloading",
            "download_started_at": "2026-04-13T00:00:00",
            "download_finished_at": None,
            "download_elapsed_sec": None,
            "reject_reason": None,
            "retention_status": None,
            "accepted_clip_count": 0,
            "preprocess_started_at": None,
            "preprocess_finished_at": None,
            "preprocess_elapsed_sec": None,
            "raw_video_path": None,
            "video_meta_path": str(tmp_path / "video_meta.json"),
            "platform_metadata": {},
            "extra": {},
        }
    )

    status = workflow.status()
    assert status["videos"]["downloading"] == 1

    workflow.download_loop()
    download_messages = []
    for line in Path(workflow.log_path).read_text(encoding="utf-8").splitlines():
        if " download-loop " not in line:
            continue
        download_messages.append(line.split(" download-loop ", 1)[1])
    assert download_messages
    payload = json.loads(download_messages[-1])
    assert "annotations" not in payload
    assert "clips" not in payload
    assert payload["avg_sec"] == {"download_per_downloaded_video": None}
    assert payload["extras"]["idle_reason"] == "no_pending_queries"
    assert payload["extras"]["query_count"] == 0
    assert payload["extras"]["hits"] == 0


def test_register_local_videos_can_feed_preprocess(tmp_path: Path, monkeypatch) -> None:
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

    local_root = tmp_path / "legacy_batch"
    source_video = local_root / "封闭室内中空间（教室/会议室）" / "sample.mp4"
    source_video.parent.mkdir(parents=True, exist_ok=True)
    source_video.write_bytes(b"fake-video-bytes")

    monkeypatch.setattr(LocalVideoRegistry, "_probe_duration_sec", lambda self, path: 18.0)

    workflow = Workflow(config, adapter=FakeBilibiliAdapter(), annotator=FakeAnnotator())
    workflow.prepare_reference()
    import_result = workflow.register_local_videos(input_root=local_root, tags=["群体", "emotion"])
    accepted = workflow.preprocess()
    status = workflow.status()

    assert import_result["registered_videos"] == 1
    assert accepted >= 1
    assert status["videos"]["downloaded"] == 1
    assert status["videos"]["processed"] == 1
    assert status["clips"]["accepted"] >= 1
    assert status["clips"]["pending_annotation"] >= 1


def test_import_01301_runtime_moves_bilibili_videos_into_pipeline_queue(tmp_path: Path, monkeypatch) -> None:
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
        "service": {},
    }

    legacy_runtime_root = tmp_path / "legacy_runtime"
    source_video = legacy_runtime_root / "artifacts" / "raw_videos" / "bilibili" / "high_flat" / "BV1TEST12345.mp4"
    source_video.parent.mkdir(parents=True, exist_ok=True)
    source_video.write_bytes(b"fake-video-bytes")

    monkeypatch.setattr(LegacyRuntimeImporter, "_probe_duration_sec", lambda self, path: 18.0)

    workflow = Workflow(config, adapter=FakeBilibiliAdapter(), annotator=FakeAnnotator())
    result = workflow.import_01301_runtime(legacy_runtime_root=legacy_runtime_root, progress=False)
    status = workflow.status()
    rows = workflow.db.fetchall("SELECT bvid, platform, query_id, raw_video_path, download_status FROM videos")

    assert result["imported_videos"] == 1
    assert result["moved_files"] == 1
    assert not source_video.exists()
    assert len(rows) == 1
    assert rows[0]["bvid"] == "BV1TEST12345"
    assert rows[0]["platform"] == "bilibili"
    assert rows[0]["download_status"] == "downloaded"
    expected_seed_id = make_seed_id(950000, "高约束-扁平", "legacy_import::high_flat")
    assert rows[0]["query_id"] == make_query_id(expected_seed_id, "legacy_import::high_flat")
    assert Path(str(rows[0]["raw_video_path"])).exists()
    assert status["videos"]["downloaded"] == 1


def test_import_01301_runtime_resume_continues_query_excel_rows_after_skips(tmp_path: Path, monkeypatch) -> None:
    config = {
        "project": {
            "name": "test_project",
            "root_dir": str(tmp_path),
            "data_dir": "data",
            "runtime_dir": "runtime",
            "reference_dir": "data/reference",
            "query_seed_catalog_path": "data/reference/query_seed_catalog.json",
            "annotation_domains_path": "data/reference/annotation_domains.json",
            "query_seed_excel_path": str(tmp_path / "missing.xlsx"),
            "query_seed_sheet_name": "情感因素",
            "annotation_schema_source_path": str(tmp_path / "schema.json"),
            "group_emotion_label_seed_path": str(tmp_path / "labels.json"),
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
        "preprocess": {
            "workers": 1,
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
        "service": {},
    }

    legacy_runtime_root = tmp_path / "legacy_runtime"
    source_root = legacy_runtime_root / "artifacts" / "raw_videos" / "bilibili"
    first_video = source_root / "high_flat" / "BV1TEST12345.mp4"
    second_video = source_root / "low_flat" / "BV1TEST67890.mp4"
    first_video.parent.mkdir(parents=True, exist_ok=True)
    first_video.write_bytes(b"fake-video-bytes-1")

    monkeypatch.setattr(LegacyRuntimeImporter, "_probe_duration_sec", lambda self, path: 18.0)

    workflow = Workflow(config, adapter=FakeBilibiliAdapter(), annotator=FakeAnnotator())
    first_result = workflow.import_01301_runtime(legacy_runtime_root=legacy_runtime_root, progress=False)

    first_video.parent.mkdir(parents=True, exist_ok=True)
    first_video.write_bytes(b"fake-video-bytes-1")
    second_video.parent.mkdir(parents=True, exist_ok=True)
    second_video.write_bytes(b"fake-video-bytes-2")
    second_result = workflow.import_01301_runtime(legacy_runtime_root=legacy_runtime_root, progress=False)

    query_rows = workflow.db.fetchall("SELECT query_id, excel_row, query_text FROM queries ORDER BY excel_row")
    video_rows = workflow.db.fetchall("SELECT bvid, query_id FROM videos ORDER BY bvid")

    assert first_result["imported_videos"] == 1
    assert second_result["imported_videos"] == 1
    assert second_result["skipped_existing"] == 1
    assert [row["excel_row"] for row in query_rows] == [950000, 950001]
    assert [row["query_text"] for row in query_rows] == ["legacy_import::high_flat", "legacy_import::low_flat"]

    first_seed_id = make_seed_id(950000, "高约束-扁平", "legacy_import::high_flat")
    second_seed_id = make_seed_id(950001, "低约束-扁平", "legacy_import::low_flat")
    assert [row["query_id"] for row in query_rows] == [
        make_query_id(first_seed_id, "legacy_import::high_flat"),
        make_query_id(second_seed_id, "legacy_import::low_flat"),
    ]
    assert [row["query_id"] for row in video_rows] == [query_rows[0]["query_id"], query_rows[1]["query_id"]]


def test_download_loop_auto_prepares_reference_when_missing(tmp_path: Path) -> None:
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
                "auto_prepare_reference_if_missing": True,
                "max_queries_per_cycle": 10,
                "auto_seed_if_empty": True,
                "auto_refill_done_queries": False,
                "poll_interval_sec": 0.01,
                "idle_sleep_sec": 0.01,
                "max_cycles": 1,
                "stop_when_idle": False,
            }
        },
    }

    workflow = Workflow(config, adapter=FakeBilibiliAdapter(), annotator=FakeAnnotator())
    download_status = workflow.download_loop()

    assert (tmp_path / "data" / "reference" / "query_seed_catalog.json").exists()
    assert (tmp_path / "data" / "reference" / "annotation_domains.json").exists()
    assert download_status["queries"]["done"] == 3


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


def test_query_shard_only_seeds_owned_queries(tmp_path: Path) -> None:
    reference_dir = tmp_path / "data" / "reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = reference_dir / "query_seed_catalog.json"
    domains_path = reference_dir / "annotation_domains.json"
    catalog = {
        "rows": [
            {
                "seed_id": "seed_alpha",
                "excel_row": 1,
                "scene_text": "教室",
                "trigger_text": "表扬",
                "query_texts": ["教室 表扬", "教室 鼓掌"],
            },
            {
                "seed_id": "seed_beta",
                "excel_row": 2,
                "scene_text": "操场",
                "trigger_text": "冲突",
                "query_texts": ["操场 冲突", "操场 争执"],
            },
        ]
    }
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    domains_path.write_text(json.dumps({"domains": []}, ensure_ascii=False), encoding="utf-8")

    config = {
        "project": {
            "name": "test_project",
            "root_dir": str(tmp_path),
            "data_dir": "data",
            "runtime_dir": "runtime_shard0",
            "reference_dir": "data/reference",
            "query_seed_catalog_path": "data/reference/query_seed_catalog.json",
            "annotation_domains_path": "data/reference/annotation_domains.json",
            "query_seed_excel_path": str(catalog_path),
            "query_seed_sheet_name": "情感因素",
            "annotation_schema_source_path": str(domains_path),
            "group_emotion_label_seed_path": str(domains_path),
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
            "query_shard": {"count": 2, "index": 0},
            "download": {"enabled": False, "workers": 1, "max_inflight_per_host": 1, "retries": 1, "fragment_retries": 1, "socket_timeout_sec": 10},
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
        "service": {"download_loop": {"max_queries_per_cycle": 10}},
    }

    all_query_ids = {
        make_query_id("seed_alpha", "教室 表扬"),
        make_query_id("seed_alpha", "教室 鼓掌"),
        make_query_id("seed_beta", "操场 冲突"),
        make_query_id("seed_beta", "操场 争执"),
    }
    workflow0 = Workflow(config)
    seeded0 = workflow0.seed_queries()
    shard0_query_ids = {str(row["query_id"]) for row in workflow0.query_repo.list_pending(limit=0)}

    config_shard1 = json.loads(json.dumps(config))
    config_shard1["project"]["runtime_dir"] = "runtime_shard1"
    config_shard1["crawl"]["query_shard"]["index"] = 1
    workflow1 = Workflow(config_shard1)
    seeded1 = workflow1.seed_queries()
    shard1_query_ids = {str(row["query_id"]) for row in workflow1.query_repo.list_pending(limit=0)}

    assert seeded0 == len(shard0_query_ids)
    assert seeded1 == len(shard1_query_ids)
    assert shard0_query_ids.isdisjoint(shard1_query_ids)
    assert shard0_query_ids | shard1_query_ids == all_query_ids
    assert all(workflow0.query_repo.owns_query_id(query_id) for query_id in shard0_query_ids)
    assert all(workflow1.query_repo.owns_query_id(query_id) for query_id in shard1_query_ids)


def test_download_loop_uses_owned_query_summary_for_refill_and_status(tmp_path: Path) -> None:
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
            "query_shard": {"count": 2, "index": 0},
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
                "max_queries_per_cycle": 1,
                "auto_seed_if_empty": False,
                "auto_refill_done_queries": True,
                "refill_batch_size": 1,
                "query_recycle_cooldown_sec": 0,
                "max_runs_per_query": 0,
                "poll_interval_sec": 0.01,
                "idle_sleep_sec": 0.01,
                "max_cycles": 1,
                "stop_when_idle": False,
            }
        },
    }

    workflow = Workflow(config, adapter=FakeBilibiliAdapter(), annotator=FakeAnnotator())
    workflow.prepare_reference()

    owned_queries: list[str] = []
    foreign_queries: list[str] = []
    excel_row = 1
    candidate_index = 1
    while len(owned_queries) < 2 or len(foreign_queries) < 2:
        seed_id = f"seed_{candidate_index}"
        scene_text = f"场景{candidate_index}"
        trigger_text = f"触发{candidate_index}"
        query_text = f"{scene_text} {trigger_text}"
        query_id = make_query_id(seed_id, query_text)
        owns_query = workflow.query_repo.owns_query_id(query_id)
        if owns_query and len(owned_queries) >= 2:
            candidate_index += 1
            continue
        if (not owns_query) and len(foreign_queries) >= 2:
            candidate_index += 1
            continue
        workflow.query_repo.upsert(
            {
                "query_id": query_id,
                "seed_id": seed_id,
                "excel_row": excel_row,
                "scene_text": scene_text,
                "trigger_text": trigger_text,
                "query_text": query_text,
                "status": "done" if owns_query else "pending",
                "last_run_at": "2026-04-13T00:00:00" if owns_query else None,
                "hit_count": 1 if owns_query else 0,
                "run_count": 1 if owns_query else 0,
            }
        )
        if owns_query:
            owned_queries.append(query_id)
        else:
            foreign_queries.append(query_id)
        excel_row += 1
        candidate_index += 1

    status = workflow.download_loop()
    crawl_stats = workflow.acquisition.last_crawl_stats()

    assert crawl_stats["query_count"] == 1
    assert status["queries"] == {"total": 2, "pending": 0, "retry": 0, "done": 2}
    assert status["videos"]["downloaded"] == 1


def test_video_shard_prevents_same_bvid_from_downloading_on_both_machines(tmp_path: Path) -> None:
    reference_dir = tmp_path / "data" / "reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = reference_dir / "query_seed_catalog.json"
    domains_path = reference_dir / "annotation_domains.json"
    catalog_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "seed_id": "seed_alpha",
                        "excel_row": 1,
                        "scene_text": "教室",
                        "trigger_text": "表扬",
                        "query_texts": ["教室 表扬"],
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    domains_path.write_text(json.dumps({"domains": []}, ensure_ascii=False), encoding="utf-8")

    config = {
        "project": {
            "name": "test_project",
            "root_dir": str(tmp_path),
            "data_dir": "data",
            "runtime_dir": "runtime_video_shard0",
            "reference_dir": "data/reference",
            "query_seed_catalog_path": "data/reference/query_seed_catalog.json",
            "annotation_domains_path": "data/reference/annotation_domains.json",
            "query_seed_excel_path": str(catalog_path),
            "query_seed_sheet_name": "情感因素",
            "annotation_schema_source_path": str(domains_path),
            "group_emotion_label_seed_path": str(domains_path),
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
            "query_shard": {"count": 1, "index": 0},
            "video_shard": {"count": 2, "index": 0},
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
        "service": {"download_loop": {"max_queries_per_cycle": 10}},
    }

    workflow0 = Workflow(config, adapter=FakeBilibiliAdapter())
    workflow0.seed_queries()
    workflow0.crawl()
    shard0_bvids = {str(row["bvid"]) for row in workflow0.db.fetchall("SELECT bvid FROM videos")}

    config_shard1 = json.loads(json.dumps(config))
    config_shard1["project"]["runtime_dir"] = "runtime_video_shard1"
    config_shard1["crawl"]["video_shard"]["index"] = 1
    workflow1 = Workflow(config_shard1, adapter=FakeBilibiliAdapter())
    workflow1.seed_queries()
    workflow1.crawl()
    shard1_bvids = {str(row["bvid"]) for row in workflow1.db.fetchall("SELECT bvid FROM videos")}

    assert shard0_bvids.isdisjoint(shard1_bvids)
    assert shard0_bvids | shard1_bvids == {"BV_TEST_001"}


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
