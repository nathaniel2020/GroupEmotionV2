from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import traceback
from typing import Any
from uuid import uuid4

from .config import config_fingerprint


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json_dumps(payload))
        handle.write("\n")


@dataclass(slots=True)
class RunPaths:
    run_id: str
    run_dir: Path
    llm_dir: Path
    manifest_path: Path
    events_path: Path
    llm_calls_path: Path
    errors_path: Path
    log_path: Path


class RunLogger:
    def __init__(self, config: dict[str, Any], *, stage: str):
        runtime_dir = Path(config["project"]["runtime_dir"])
        run_root = runtime_dir / "runs"
        run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{stage}_{uuid4().hex[:8]}"
        run_dir = run_root / run_id
        llm_dir = run_dir / "llm"
        run_dir.mkdir(parents=True, exist_ok=True)
        llm_dir.mkdir(parents=True, exist_ok=True)

        self.config = config
        self.stage = stage
        self.paths = RunPaths(
            run_id=run_id,
            run_dir=run_dir,
            llm_dir=llm_dir,
            manifest_path=run_dir / "manifest.json",
            events_path=run_dir / "events.jsonl",
            llm_calls_path=run_dir / "llm_calls.jsonl",
            errors_path=run_dir / "errors.jsonl",
            log_path=run_dir / "run.log",
        )
        self._manifest: dict[str, Any] = {
            "run_id": run_id,
            "stage": stage,
            "status": "running",
            "started_at": utc_now(),
            "project": config["project"]["name"],
            "config_hash": config_fingerprint(config),
            "config_paths": config["project"].get("config_paths", []),
            "command": sys.argv,
        }
        self._logger = self._setup_python_logger()
        self._write_manifest()
        self.event("run_started", stage=stage)

    def _setup_python_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"group_emotion_video.{self.paths.run_id}")
        logger.setLevel(getattr(logging, str(self.config["logging"]["level"]).upper(), logging.INFO))
        logger.propagate = False
        logger.handlers.clear()

        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

        file_handler = logging.FileHandler(self.paths.log_path, encoding="utf-8")
        file_handler.setLevel(getattr(logging, str(self.config["logging"]["level"]).upper(), logging.INFO))
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(
            getattr(logging, str(self.config["logging"]["console_level"]).upper(), logging.INFO)
        )
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        return logger

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    def _write_manifest(self) -> None:
        self.paths.manifest_path.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def event(self, event_type: str, **payload: Any) -> None:
        record = {
            "timestamp": utc_now(),
            "event_type": event_type,
            "run_id": self.paths.run_id,
            **payload,
        }
        append_jsonl(self.paths.events_path, record)
        self._logger.info("%s | %s", event_type, json_dumps(payload))

    def error(self, event_type: str, exc: Exception, **payload: Any) -> None:
        record = {
            "timestamp": utc_now(),
            "event_type": event_type,
            "run_id": self.paths.run_id,
            "error_type": exc.__class__.__name__,
            "error_message": str(exc),
            "traceback": "".join(traceback.format_exception(exc)),
            **payload,
        }
        append_jsonl(self.paths.errors_path, record)
        self._logger.exception("%s", event_type)

    def llm_call(self, **payload: Any) -> None:
        record = {
            "timestamp": utc_now(),
            "run_id": self.paths.run_id,
            **payload,
        }
        append_jsonl(self.paths.llm_calls_path, record)

    def allocate_llm_call_dir(self, call_id: str) -> Path:
        call_dir = self.paths.llm_dir / call_id
        call_dir.mkdir(parents=True, exist_ok=True)
        return call_dir

    def finalize(self, status: str, *, summary: dict[str, Any] | None = None) -> None:
        self._manifest["status"] = status
        self._manifest["finished_at"] = utc_now()
        if summary:
            self._manifest["summary"] = summary
        self._write_manifest()
        self.event("run_finished", status=status, summary=summary or {})

