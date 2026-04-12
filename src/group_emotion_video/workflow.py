from __future__ import annotations

import json
import math
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any

from .acquisition import AcquisitionService
from .annotation import AnnotationService, SingleModelAnnotator
from .config import load_config
from .exporter import ExportService
from .llm import OpenAICompatibleLLM
from .logging_utils import setup_logging
from .preprocessing import PreprocessingService
from .reference import prepare_reference_files
from .repositories import AnnotationRepository, ClipRepository, QueryRepository, VideoRepository
from .runtime_layout import ensure_runtime_layout
from .sqlite_db import SQLiteDB, initialize_schema
from .validation import load_annotation_domains


class Workflow:
    def __init__(self, config: dict[str, Any], *, adapter: Any | None = None, annotator: Any | None = None):
        self.config = config
        project = config["project"]
        self.layout = ensure_runtime_layout(
            project["root_dir"],
            project["runtime_dir"],
            project["data_dir"],
            project["reference_dir"],
        )
        self.logger, self.log_path = setup_logging(self.layout["logs"])
        self.db = SQLiteDB(self.layout["db"])
        initialize_schema(self.db)
        self.query_repo = QueryRepository(self.db)
        self.video_repo = VideoRepository(self.db)
        self.clip_repo = ClipRepository(self.db)
        self.annotation_repo = AnnotationRepository(self.db)
        self.llm = OpenAICompatibleLLM(config)
        self.acquisition = AcquisitionService(
            config,
            query_repo=self.query_repo,
            video_repo=self.video_repo,
            layout=self.layout,
            logger=self.logger,
            adapter=adapter,
        )
        self._annotator_override = annotator

    def _resolve_path(self, raw_path: str | Path) -> Path:
        path = Path(raw_path)
        return path.resolve() if path.is_absolute() else (self.layout["root"] / path).resolve()

    @classmethod
    def from_config_paths(cls, config_paths: str | list[str] | None = None, **kwargs: Any) -> "Workflow":
        return cls(load_config(config_paths), **kwargs)

    def _require_reference(self) -> tuple[Path, Path]:
        query_seed_catalog_path = self._resolve_path(self.config["project"]["query_seed_catalog_path"])
        annotation_domains_path = self._resolve_path(self.config["project"]["annotation_domains_path"])
        if not query_seed_catalog_path.exists() or not annotation_domains_path.exists():
            raise FileNotFoundError("Reference files are missing. Run `prepare-reference` first.")
        return query_seed_catalog_path, annotation_domains_path

    def prepare_reference(self) -> dict[str, str]:
        project = self.config["project"]
        outputs = prepare_reference_files(
            excel_path=self._resolve_path(project["query_seed_excel_path"]),
            sheet_name=project["query_seed_sheet_name"],
            schema_source_path=self._resolve_path(project["annotation_schema_source_path"]),
            label_seed_path=self._resolve_path(project["group_emotion_label_seed_path"]),
            query_seed_catalog_path=self._resolve_path(project["query_seed_catalog_path"]),
            annotation_domains_path=self._resolve_path(project["annotation_domains_path"]),
        )
        return {key: str(path) for key, path in outputs.items()}

    def seed_queries(self) -> int:
        query_seed_catalog_path, _ = self._require_reference()
        return self.acquisition.seed_queries(query_seed_catalog_path)

    def crawl(self) -> int:
        self._require_reference()
        return self.acquisition.crawl()

    def _build_preprocessing_service(self) -> PreprocessingService:
        return PreprocessingService(
            self.config,
            video_repo=self.video_repo,
            clip_repo=self.clip_repo,
            layout=self.layout,
            logger=self.logger,
            llm=self.llm,
        )

    def preprocess(self) -> int:
        self._require_reference()
        service = self._build_preprocessing_service()
        return service.preprocess()

    def _build_annotation_service(self) -> AnnotationService:
        _, annotation_domains_path = self._require_reference()
        domains = load_annotation_domains(annotation_domains_path)
        annotator = self._annotator_override or SingleModelAnnotator(self.config, domains=domains, llm=self.llm)
        return AnnotationService(
            self.config,
            annotator=annotator,
            annotation_repo=self.annotation_repo,
            clip_repo=self.clip_repo,
            layout=self.layout,
            logger=self.logger,
        )

    def annotate(self) -> int:
        service = self._build_annotation_service()
        clips = self.video_repo.list_processed_clips_for_annotation(int(self.config["annotation"]["max_clips_per_run"]))
        return service.annotate(clips)

    def export(self) -> str:
        _, annotation_domains_path = self._require_reference()
        rows = self.video_repo.list_exportable()
        export_dir = ExportService(layout=self.layout, logger=self.logger).export(rows, annotation_domains_path)
        return str(export_dir)

    def status(self) -> dict[str, Any]:
        query_summary = self.query_repo.summary() if hasattr(self.query_repo, "summary") else {"total": self.query_repo.count()}
        video_summary = self.video_repo.summary()
        clip_summary = self.clip_repo.summary()
        annotation_summary = self.annotation_repo.summary()
        clip_summary = {
            **clip_summary,
            "oldest_pending_age_sec": _iso_age_seconds(clip_summary.get("oldest_pending_created_at")),
        }
        average_durations_sec = {
            "download_per_downloaded_video": video_summary["avg_download_sec"],
            "preprocess_per_processed_video": video_summary["avg_preprocess_sec_per_video"],
            "preprocess_per_accepted_clip": video_summary["avg_preprocess_sec_per_accepted_clip"],
            "annotate_per_completed_clip": annotation_summary["avg_completed_sec"],
            "annotate_per_done_clip": annotation_summary["avg_done_sec"],
        }
        return {
            "db_path": str(self.layout["db"]),
            "log_path": str(self.log_path),
            "queries": query_summary,
            "videos": video_summary,
            "clips": clip_summary,
            "annotations": {
                "done": annotation_summary["done"],
                "rejected": annotation_summary["rejected"],
                "failed": annotation_summary["failed"],
                "completed": annotation_summary["completed"],
            },
            "average_durations_sec": average_durations_sec,
            "projection_5d": self._build_five_day_projection(video_summary, annotation_summary),
        }

    def download_loop(self) -> dict[str, Any]:
        self._require_reference()
        cfg = self._loop_cfg("download_loop")
        cycle = 0
        while True:
            cycle += 1
            processed = self.acquisition.crawl(limit=int(cfg.get("max_queries_per_cycle", self.config["crawl"]["max_queries_per_run"])))
            snapshot = self.status()
            self._log_loop_snapshot("download-loop", cycle, snapshot, {"processed_items": processed})
            if self._should_stop_loop(cfg, cycle=cycle, idle=processed <= 0, snapshot=snapshot):
                break
            time.sleep(float(cfg.get("idle_sleep_sec" if processed <= 0 else "poll_interval_sec", 5.0)))
        return self.status()

    def pipeline_loop(self) -> dict[str, Any]:
        self._require_reference()
        cfg = self._loop_cfg("pipeline_loop")
        preprocess_service = self._build_preprocessing_service()
        annotation_service = self._build_annotation_service()
        preprocess_workers = max(1, int(self.config["preprocessing"].get("workers", 1)))
        annotation_workers = max(1, int(self.config["annotation"].get("clip_workers", 1)))
        preprocess_claim_limit = max(1, int(cfg.get("preprocess_claim_limit", preprocess_workers)))
        annotation_batch_size = max(1, int(cfg.get("annotation_batch_size", annotation_workers)))
        annotation_trigger_size = max(1, int(cfg.get("annotation_trigger_size", annotation_workers)))
        annotation_trigger_timeout_sec = float(cfg.get("annotation_trigger_timeout_sec", 30.0))
        queue_max_clips = max(annotation_trigger_size, int(cfg.get("queue_max_clips", annotation_trigger_size * 5)))
        cycle = 0

        with ThreadPoolExecutor(max_workers=preprocess_workers) as preprocess_executor, ThreadPoolExecutor(max_workers=annotation_workers) as annotation_executor:
            preprocess_futures: dict[Any, dict[str, Any]] = {}
            annotation_futures: dict[Any, dict[str, Any]] = {}
            while True:
                cycle += 1
                self._drain_futures(preprocess_futures, kind="preprocess")
                self._drain_futures(annotation_futures, kind="annotation")

                snapshot = self.status()
                pending_annotation = int(snapshot["clips"]["pending_annotation"])
                pending_annotation_age_sec = float(snapshot["clips"]["oldest_pending_age_sec"] or 0.0)

                available_preprocess_slots = max(0, preprocess_workers - len(preprocess_futures))
                if available_preprocess_slots > 0 and pending_annotation < queue_max_clips:
                    videos = self.video_repo.claim_downloaded_for_preprocess(min(preprocess_claim_limit, available_preprocess_slots))
                    for video_record in videos:
                        future = preprocess_executor.submit(preprocess_service._process_video, video_record)
                        preprocess_futures[future] = video_record

                should_dispatch_annotations = (
                    pending_annotation > 0
                    and (
                        pending_annotation >= annotation_trigger_size
                        or pending_annotation_age_sec >= annotation_trigger_timeout_sec
                        or (
                            len(preprocess_futures) == 0
                            and int(snapshot["videos"]["pending_preprocess"]) == 0
                        )
                    )
                )
                available_annotation_slots = max(0, annotation_workers - len(annotation_futures))
                if should_dispatch_annotations and available_annotation_slots > 0:
                    clips = self.clip_repo.claim_pending_annotations(min(annotation_batch_size, available_annotation_slots))
                    for clip_record in clips:
                        future = annotation_executor.submit(annotation_service.annotate_clip_record, clip_record)
                        annotation_futures[future] = clip_record

                snapshot = self.status()
                idle = (
                    len(preprocess_futures) == 0
                    and len(annotation_futures) == 0
                    and int(snapshot["videos"]["pending_preprocess"]) == 0
                    and int(snapshot["clips"]["pending_annotation"]) == 0
                )
                self._log_loop_snapshot(
                    "pipeline-loop",
                    cycle,
                    snapshot,
                    {
                        "active_preprocess_workers": len(preprocess_futures),
                        "active_annotation_workers": len(annotation_futures),
                        "queue_max_clips": queue_max_clips,
                        "annotation_trigger_size": annotation_trigger_size,
                    },
                )
                if self._should_stop_loop(cfg, cycle=cycle, idle=idle, snapshot=snapshot):
                    break

                if preprocess_futures or annotation_futures:
                    self._wait_for_progress(preprocess_futures, annotation_futures, timeout_sec=float(cfg.get("poll_interval_sec", 2.0)))
                else:
                    time.sleep(float(cfg.get("idle_sleep_sec", 5.0)))
        return self.status()

    def _build_five_day_projection(self, video_summary: dict[str, Any], annotation_summary: dict[str, Any]) -> dict[str, Any]:
        horizon_days = 5
        horizon_sec = horizon_days * 24 * 60 * 60
        download_workers = int(self.config["crawl"]["download"].get("workers", 1))
        preprocess_workers = int(self.config["preprocessing"].get("workers", 1))
        annotation_workers = int(self.config["annotation"].get("clip_workers", 1))

        download_videos = _capacity_for_horizon(horizon_sec, download_workers, video_summary.get("avg_download_sec"))
        preprocess_videos = _capacity_for_horizon(horizon_sec, preprocess_workers, video_summary.get("avg_preprocess_sec_per_video"))
        annotate_clips = _capacity_for_horizon(horizon_sec, annotation_workers, annotation_summary.get("avg_completed_sec"))

        accepted_clips_per_video = video_summary.get("accepted_clips_per_processed_video")
        done_rate = annotation_summary.get("done_rate")

        upstream_accepted_clips = None
        if accepted_clips_per_video is not None and download_videos is not None and preprocess_videos is not None:
            upstream_accepted_clips = math.floor(min(download_videos, preprocess_videos) * accepted_clips_per_video)

        completed_annotations = None
        if upstream_accepted_clips is not None and annotate_clips is not None:
            completed_annotations = min(upstream_accepted_clips, annotate_clips)

        done_annotations = None
        if completed_annotations is not None and done_rate is not None:
            done_annotations = math.floor(completed_annotations * done_rate)

        return {
            "horizon_days": horizon_days,
            "workers": {
                "download": download_workers,
                "preprocess": preprocess_workers,
                "annotate": annotation_workers,
            },
            "stage_capacity": {
                "download_videos": download_videos,
                "preprocess_videos": preprocess_videos,
                "annotate_clips": annotate_clips,
            },
            "yield_assumptions": {
                "accepted_clips_per_processed_video": accepted_clips_per_video,
                "done_rate_after_annotation": done_rate,
            },
            "estimated_outputs": {
                "accepted_clips": upstream_accepted_clips,
                "completed_annotations": completed_annotations,
                "done_annotations": done_annotations,
            },
        }

    def _loop_cfg(self, key: str) -> dict[str, Any]:
        return dict((self.config.get("service") or {}).get(key) or {})

    def _should_stop_loop(self, cfg: dict[str, Any], *, cycle: int, idle: bool, snapshot: dict[str, Any]) -> bool:
        max_cycles = int(cfg.get("max_cycles", 0))
        if max_cycles > 0 and cycle >= max_cycles:
            return True
        if idle and bool(cfg.get("stop_when_idle", False)):
            return True
        if bool(cfg.get("stop_when_queries_done", False)) and int(snapshot["queries"].get("pending", 0)) == 0 and int(snapshot["queries"].get("retry", 0)) == 0:
            return True
        return False

    def _drain_futures(self, futures: dict[Any, dict[str, Any]], *, kind: str) -> None:
        done = [future for future in futures if future.done()]
        for future in done:
            record = futures.pop(future)
            try:
                future.result()
            except Exception as exc:
                if kind == "preprocess":
                    self.video_repo.reset_preprocess_claim(record["video_uid"])
                    self.logger.exception("pipeline_preprocess_future_failed video_uid=%s error=%s", record["video_uid"], exc)
                else:
                    self.logger.exception("pipeline_annotation_future_failed clip_uid=%s error=%s", record["clip_uid"], exc)

    def _wait_for_progress(self, preprocess_futures: dict[Any, dict[str, Any]], annotation_futures: dict[Any, dict[str, Any]], *, timeout_sec: float) -> None:
        active = list(preprocess_futures.keys()) + list(annotation_futures.keys())
        if not active:
            return
        wait(active, timeout=max(timeout_sec, 0.1), return_when=FIRST_COMPLETED)

    def _log_loop_snapshot(self, loop_name: str, cycle: int, snapshot: dict[str, Any], extras: dict[str, Any] | None = None) -> None:
        payload = {
            "cycle": cycle,
            "queries": snapshot["queries"],
            "videos": {
                "downloading": snapshot["videos"].get("downloading"),
                "downloaded": snapshot["videos"].get("downloaded"),
                "pending_preprocess": snapshot["videos"].get("pending_preprocess"),
                "preprocessing": snapshot["videos"].get("preprocessing"),
                "processed": snapshot["videos"].get("processed"),
            },
            "clips": {
                "accepted": snapshot["clips"].get("accepted"),
                "pending_annotation": snapshot["clips"].get("pending_annotation"),
                "annotating": snapshot["clips"].get("annotating"),
                "rejected": snapshot["clips"].get("rejected"),
                "oldest_pending_age_sec": snapshot["clips"].get("oldest_pending_age_sec"),
            },
            "annotations": snapshot["annotations"],
            "avg_sec": snapshot["average_durations_sec"],
        }
        if extras:
            payload["extras"] = extras
        self.logger.info("%s %s", loop_name, json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def run(self, steps: list[str]) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for step in steps:
            if step == "prepare-reference":
                results[step] = self.prepare_reference()
            elif step == "seed-queries":
                results[step] = self.seed_queries()
            elif step == "crawl":
                results[step] = self.crawl()
            elif step == "preprocess":
                results[step] = self.preprocess()
            elif step == "annotate":
                results[step] = self.annotate()
            elif step == "download-loop":
                results[step] = self.download_loop()
            elif step == "pipeline-loop":
                results[step] = self.pipeline_loop()
            elif step == "export":
                results[step] = self.export()
            elif step == "status":
                results[step] = self.status()
            else:
                raise ValueError(f"Unsupported step: {step}")
        return results


def _capacity_for_horizon(horizon_sec: int, workers: int, avg_sec: float | None) -> int | None:
    if avg_sec is None or avg_sec <= 0:
        return None
    return math.floor((horizon_sec * max(workers, 1)) / avg_sec)


def _iso_age_seconds(raw_value: str | None) -> float | None:
    if not raw_value:
        return None
    try:
        created_at = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    return round(max((datetime.now() - created_at).total_seconds(), 0.0), 3)
