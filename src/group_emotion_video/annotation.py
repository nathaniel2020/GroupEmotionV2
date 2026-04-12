from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from .ids import make_annotation_uid
from .validation import flatten_annotation_payload, validate_annotation


class SingleModelAnnotator:
    def __init__(self, config: dict[str, Any], *, domains: dict[str, Any], llm: Any):
        self.config = config
        self.domains = domains
        self.llm = llm

    def _schema_lines(self) -> str:
        lines = []
        for field_name, field_spec in self.domains["fields"].items():
            allowed_values = field_spec.get("allowed_values")
            if allowed_values:
                lines.append(f"- {field_name}: one of {allowed_values}")
            elif field_spec.get("type") == "integer":
                lines.append(f"- {field_name}: integer {field_spec.get('min_value')}..{field_spec.get('max_value')}")
            else:
                lines.append(f"- {field_name}: free text")
        return "\n".join(lines)

    def annotate(self, clip_record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, float], list[str], str, bool]:
        clip_manifest = json.loads(Path(clip_record["manifest_path"]).read_text(encoding="utf-8"))
        subtitle_payload = json.loads(Path(clip_record["subtitle_path"]).read_text(encoding="utf-8"))
        keyframe_payload = json.loads(Path(clip_record["keyframes_manifest_path"]).read_text(encoding="utf-8"))
        image_paths = [frame["path"] for frame in keyframe_payload.get("frames", []) if frame.get("path")]

        system_prompt = (
            "你是群体情感视频标注器。只输出 JSON。"
            "所有字段必须完整，enum 字段必须精确命中允许值。"
            "如果判定不是群体样本，只允许使用 rejected sentinel values。"
        )
        user_prompt = (
            f"clip_manifest={json.dumps(clip_manifest, ensure_ascii=False)}\n"
            f"text_evidence={json.dumps(subtitle_payload, ensure_ascii=False)}\n"
            f"请输出这些字段:\n{self._schema_lines()}\n"
            "同时返回 field_confidence，格式示例："
            '{"final_annotation": {...}, "field_confidence": {"overall": 0.8, "scene_description": 0.7}}'
        )
        raw_payload = self.llm.json_completion(system_prompt, user_prompt, image_paths=image_paths, request_label="annotate.single_model")
        candidate_annotation = raw_payload.get("final_annotation", raw_payload)
        field_confidence = raw_payload.get("field_confidence", {})
        flattened_annotation, inline_confidence = flatten_annotation_payload(candidate_annotation if isinstance(candidate_annotation, dict) else {})
        merged_confidence = {**inline_confidence, **(field_confidence if isinstance(field_confidence, dict) else {})}

        rejected_keys = {
            "group_size_estimate": {"single", "none"},
            "group_behavior_type": {"none"},
            "group_emotion": {"none"},
        }
        annotation_status = "done"
        if any(str(flattened_annotation.get(key) or "") in values for key, values in rejected_keys.items()):
            annotation_status = "rejected"

        final_annotation, merged_confidence, quality_flags, fatal = validate_annotation(
            annotation=flattened_annotation,
            field_confidence=merged_confidence,
            domains=self.domains,
            annotation_status=annotation_status,
        )
        if fatal:
            annotation_status = "failed"
        overall = float(merged_confidence.get("overall", 0.0))
        if overall < float(self.config["annotation"].get("min_overall_confidence", 0.6)):
            quality_flags = list(dict.fromkeys([*quality_flags, "low_confidence"]))
        review_required = "low_confidence" in quality_flags and annotation_status == "done"
        return {"single_model": raw_payload}, final_annotation, merged_confidence, quality_flags, annotation_status, review_required


class AnnotationService:
    def __init__(self, config: dict[str, Any], *, annotator: Any, annotation_repo: Any, clip_repo: Any, layout: dict[str, Path], logger: Any):
        self.config = config
        self.annotator = annotator
        self.annotation_repo = annotation_repo
        self.clip_repo = clip_repo
        self.layout = layout
        self.logger = logger

    def annotate_clip_record(self, clip_record: dict[str, Any]) -> bool:
        annotation_uid = make_annotation_uid(clip_record["clip_uid"])
        started_at = datetime.now().isoformat(timespec="seconds")
        start_clock = time.perf_counter()
        self.logger.info("annotation_start clip_uid=%s", clip_record["clip_uid"])
        try:
            agent_outputs, final_annotation, field_confidence, quality_flags, status, review_required = self.annotator.annotate(clip_record)
        except Exception as exc:
            finished_at = datetime.now().isoformat(timespec="seconds")
            elapsed_sec = round(time.perf_counter() - start_clock, 3)
            self.clip_repo.update_annotation_status(clip_record["clip_uid"], "failed")
            artifact_path = self.layout["artifact_annotations"] / f"{annotation_uid}.failed.json"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "annotation_uid": annotation_uid,
                "clip_uid": clip_record["clip_uid"],
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_sec": elapsed_sec,
                "error": str(exc),
            }
            artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self.annotation_repo.upsert(
                {
                    "annotation_uid": annotation_uid,
                    "clip_uid": clip_record["clip_uid"],
                    "status": "failed",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "elapsed_sec": elapsed_sec,
                    "review_required": False,
                    "final_annotation": {},
                    "field_confidence": {},
                    "quality_flags": ["parse_error"],
                    "artifact_path": str(artifact_path),
                }
            )
            self.logger.exception("annotation_failed clip_uid=%s error=%s", clip_record["clip_uid"], exc)
            return False

        finished_at = datetime.now().isoformat(timespec="seconds")
        elapsed_sec = round(time.perf_counter() - start_clock, 3)
        artifact_path = self.layout["artifact_annotations"] / f"{annotation_uid}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "annotation_uid": annotation_uid,
            "clip_uid": clip_record["clip_uid"],
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_sec": elapsed_sec,
            "agent_outputs": agent_outputs,
            "final_annotation": final_annotation,
            "field_confidence": field_confidence,
            "quality_flags": quality_flags,
            "status": status,
            "review_required": review_required,
        }
        artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.annotation_repo.upsert(
            {
                "annotation_uid": annotation_uid,
                "clip_uid": clip_record["clip_uid"],
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_sec": elapsed_sec,
                "review_required": review_required,
                "final_annotation": final_annotation,
                "field_confidence": field_confidence,
                "quality_flags": quality_flags,
                "artifact_path": str(artifact_path),
            }
        )
        self.clip_repo.update_annotation_status(clip_record["clip_uid"], status)
        self.logger.info("annotation_finish clip_uid=%s status=%s elapsed_sec=%.3f", clip_record["clip_uid"], status, elapsed_sec)
        return status in {"done", "rejected"}

    def annotate(self, clips: list[dict[str, Any]]) -> int:
        if not clips:
            return 0
        completed = 0
        with ThreadPoolExecutor(max_workers=min(len(clips), int(self.config["annotation"].get("clip_workers", 4)))) as executor:
            futures = [executor.submit(self.annotate_clip_record, clip_record) for clip_record in clips]
            for future in as_completed(futures):
                completed += int(bool(future.result()))
        return completed
