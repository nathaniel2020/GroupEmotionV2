from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .llm import LLMResult, MultimodalLLMClient
from .logging_utils import RunLogger


class LightweightPipeline:
    STAGES = [
        "sample_ingest",
        "annotate",
        "review_gate",
        "export",
    ]

    def __init__(self, config: dict[str, Any], run_logger: RunLogger):
        self.config = config
        self.run_logger = run_logger
        self.llm = MultimodalLLMClient(config, run_logger)
        self.output_root = Path(config["project"]["output_dir"])
        self.output_root.mkdir(parents=True, exist_ok=True)

    def describe(self) -> list[str]:
        return list(self.STAGES)

    def annotate_sample(self, sample_path: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
        sample_file = Path(sample_path)
        sample = json.loads(sample_file.read_text(encoding="utf-8"))
        sample_id = str(sample.get("sample_id") or sample_file.stem)
        self.run_logger.event("sample_loaded", sample_id=sample_id, sample_path=str(sample_file))

        try:
            result = self.llm.annotate_json(
                sample_id=sample_id,
                system_prompt=self._load_system_prompt(),
                user_prompt=self._build_user_prompt(sample),
                frame_paths=[str(path) for path in sample.get("frame_paths", [])],
                video_url=sample.get("video_url"),
                dry_run=dry_run,
            )
            gate = self._quality_gate(result.payload)
            record = self._save_annotation_record(sample, result, gate)
            self.run_logger.event(
                "annotation_completed",
                sample_id=sample_id,
                gate_status=gate["status"],
                confidence=gate["confidence"],
                reasons=gate["reasons"],
            )
            return record
        except Exception as exc:
            self.run_logger.event(
                "annotation_failed",
                sample_id=sample_id,
                sample_path=str(sample_file),
                error_type=exc.__class__.__name__,
            )
            failed_path = self.output_root / "failed" / f"{sample_id}.json"
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            failed_path.write_text(
                json.dumps(
                    {
                        "sample_id": sample_id,
                        "status": "failed",
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            raise

    def status(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"approved": 0, "needs_review": 0, "failed": 0}
        for key in list(summary):
            directory = self.output_root / key
            if directory.exists():
                summary[key] = sum(1 for path in directory.glob("*.json") if path.is_file())
        summary["run_root"] = str(Path(self.config["project"]["runtime_dir"]) / "runs")
        return summary

    def _load_system_prompt(self) -> str:
        prompt_path = Path(self.config["project"]["prompts_dir"]) / "annotation_system.md"
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _build_user_prompt(sample: dict[str, Any]) -> str:
        schema_hint = {
            "status": "approved | needs_review",
            "summary": "一句话概括",
            "group_emotion": "如 joy / tension / sadness / mixed / unknown",
            "emotion_intensity": "low | medium | high | unknown",
            "confidence": "0.0 - 1.0",
            "evidence": ["列出支持判断的证据点"],
            "review_reasons": ["当需要复核时给出原因"],
        }
        payload = {
            "sample_id": sample.get("sample_id"),
            "scene": sample.get("scene"),
            "transcript": sample.get("transcript"),
            "notes": sample.get("notes"),
            "frame_paths": sample.get("frame_paths", []),
            "video_url": sample.get("video_url"),
            "expected_schema": schema_hint,
        }
        return (
            "请根据以下样本信息，判断群体情感并输出 JSON。\n"
            "如果证据不足，请把 status 设为 needs_review。\n\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    def _quality_gate(self, payload: dict[str, Any]) -> dict[str, Any]:
        annotation_cfg = self.config["annotation"]
        reasons: list[str] = []

        for field in annotation_cfg.get("required_fields", []):
            if field not in payload:
                reasons.append(f"missing_field:{field}")

        evidence = payload.get("evidence")
        if annotation_cfg.get("require_evidence", True) and not evidence:
            reasons.append("missing_evidence")

        confidence = self._coerce_confidence(payload.get("confidence"))
        if confidence < float(annotation_cfg.get("min_confidence", 0.65)):
            reasons.append("low_confidence")

        if str(payload.get("status", "")).strip().lower() == "needs_review":
            reasons.append("model_requested_review")

        approved = not reasons and confidence >= float(annotation_cfg.get("review_confidence", 0.80))
        return {
            "status": "approved" if approved else "needs_review",
            "confidence": confidence,
            "reasons": reasons,
        }

    @staticmethod
    def _coerce_confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    def _save_annotation_record(
        self,
        sample: dict[str, Any],
        result: LLMResult,
        gate: dict[str, Any],
    ) -> dict[str, Any]:
        sample_id = str(sample.get("sample_id") or "unknown")
        record = {
            "sample_id": sample_id,
            "sample": sample,
            "annotation": result.payload,
            "gate": gate,
            "llm": {
                "call_id": result.call_id,
                "model": self.config["llm"]["model"],
                "status": result.status,
                "latency_sec": result.latency_sec,
                "usage": result.usage,
                "request_path": result.request_path,
                "response_path": result.response_path,
            },
        }
        output_path = self.output_root / gate["status"] / f"{sample_id}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        if gate["status"] == "needs_review":
            review_queue_path = self.output_root / "needs_review" / "review_queue.jsonl"
            with review_queue_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "sample_id": sample_id,
                            "call_id": result.call_id,
                            "confidence": gate["confidence"],
                            "reasons": gate["reasons"],
                        },
                        ensure_ascii=False,
                    )
                )
                handle.write("\n")
        return record
