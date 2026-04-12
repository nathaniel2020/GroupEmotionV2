from __future__ import annotations

import json
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

    def preprocess(self) -> int:
        self._require_reference()
        service = PreprocessingService(
            self.config,
            video_repo=self.video_repo,
            clip_repo=self.clip_repo,
            layout=self.layout,
            logger=self.logger,
            llm=self.llm,
        )
        return service.preprocess()

    def annotate(self) -> int:
        _, annotation_domains_path = self._require_reference()
        domains = load_annotation_domains(annotation_domains_path)
        annotator = self._annotator_override or SingleModelAnnotator(self.config, domains=domains, llm=self.llm)
        service = AnnotationService(
            self.config,
            annotator=annotator,
            annotation_repo=self.annotation_repo,
            clip_repo=self.clip_repo,
            layout=self.layout,
            logger=self.logger,
        )
        clips = self.video_repo.list_processed_clips_for_annotation(int(self.config["annotation"]["max_clips_per_run"]))
        return service.annotate(clips)

    def export(self) -> str:
        _, annotation_domains_path = self._require_reference()
        rows = self.video_repo.list_exportable()
        export_dir = ExportService(layout=self.layout, logger=self.logger).export(rows, annotation_domains_path)
        return str(export_dir)

    def status(self) -> dict[str, Any]:
        query_count = self.query_repo.count()
        annotation_summary = self.annotation_repo.summary()
        return {
            "db_path": str(self.layout["db"]),
            "log_path": str(self.log_path),
            "query_count": query_count,
            "annotation_summary": annotation_summary,
        }

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
            elif step == "export":
                results[step] = self.export()
            elif step == "status":
                results[step] = self.status()
            else:
                raise ValueError(f"Unsupported step: {step}")
        return results
