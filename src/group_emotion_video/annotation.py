from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from .ids import make_annotation_uid
from .validation import flatten_annotation_payload, validate_annotation


AGENT_INSTRUCTIONS = {
    "scene_behavior": "优先判断场景、群体规模、可见行为和互动对象。",
    "event_emotion": "优先判断触发事件、情绪类别、情绪强度和外显线索。",
    "social_cognition": "优先判断群体身份、规范约束、内外群体关系和社会认知线索。",
    "propagation_temporal": "优先判断情绪扩散路径、同步性、阶段变化和时间线索。",
    "general": "综合视频画面、文本证据和上下文进行完整标注。",
}

FULL_CANDIDATE_INSTRUCTION = (
    "作为完整候选模型独立完成全字段标注，覆盖场景行为、事件情绪、社会认知与传播时序线索，"
    "不要只承担单一子任务。"
)

LLM_CONFIG_KEYS = ("base_url", "api_key_env", "timeout_sec", "temperature", "max_images_per_prompt")


class AdjudicatedAnnotator:
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

    def _candidate_specs(self) -> list[dict[str, Any]]:
        raw_specs = self.config.get("annotation", {}).get("candidate_models")
        if not raw_specs:
            raw_specs = [
                {
                    "name": "primary",
                    "model": self.config.get("llm", {}).get("model"),
                    "enabled": True,
                }
            ]
        specs: list[dict[str, Any]] = []
        for index, raw_spec in enumerate(raw_specs, start=1):
            if isinstance(raw_spec, str):
                model = raw_spec.strip()
                if not model:
                    continue
                specs.append({"name": model, "agent": None, "model": model})
                continue
            if not isinstance(raw_spec, dict):
                continue
            if raw_spec.get("enabled", True) is False:
                continue
            model = str(raw_spec.get("model") or "").strip() or None
            name = str(raw_spec.get("name") or model or f"candidate_{index}").strip()
            agent = str(raw_spec.get("agent") or "").strip() or None
            specs.append(
                {
                    "name": name,
                    "agent": agent,
                    "model": model,
                    "llm_config": self._llm_config_overrides(raw_spec),
                }
            )
        if not specs:
            raise ValueError("annotation.candidate_models has no enabled candidates.")
        return specs

    @staticmethod
    def _llm_config_overrides(raw_cfg: dict[str, Any]) -> dict[str, Any]:
        llm_config: dict[str, Any] = {}
        nested_cfg = raw_cfg.get("llm")
        if isinstance(nested_cfg, dict):
            llm_config.update({key: value for key, value in nested_cfg.items() if key in LLM_CONFIG_KEYS})
        llm_config.update({key: raw_cfg[key] for key in LLM_CONFIG_KEYS if key in raw_cfg})
        return llm_config

    def _judge_config(self) -> dict[str, Any]:
        raw_cfg = self.config.get("annotation", {}).get("judge") or {}
        return raw_cfg if isinstance(raw_cfg, dict) else {}

    def _load_context(self, clip_record: dict[str, Any]) -> dict[str, Any]:
        clip_manifest = json.loads(Path(clip_record["manifest_path"]).read_text(encoding="utf-8"))
        subtitle_payload = json.loads(Path(clip_record["subtitle_path"]).read_text(encoding="utf-8"))
        keyframe_payload = json.loads(Path(clip_record["keyframes_manifest_path"]).read_text(encoding="utf-8"))
        image_paths = [frame["path"] for frame in keyframe_payload.get("frames", []) if frame.get("path")]
        return {
            "clip_manifest": clip_manifest,
            "subtitle_payload": subtitle_payload,
            "keyframe_payload": keyframe_payload,
            "image_paths": image_paths,
        }

    @staticmethod
    def _status_from_annotation(annotation: dict[str, Any]) -> str:
        rejected_keys = {
            "group_size_estimate": {"single", "none"},
            "group_behavior_type": {"none"},
            "group_emotion": {"none"},
        }
        if any(str(annotation.get(key) or "") in values for key, values in rejected_keys.items()):
            return "rejected"
        return "done"

    @staticmethod
    def _quality_flags(raw_value: Any) -> list[str]:
        if not raw_value:
            return []
        if isinstance(raw_value, list):
            return [str(item) for item in raw_value if str(item).strip()]
        return [str(raw_value)]

    def _json_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        image_paths: list[str],
        request_label: str,
        model: str | None,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "image_paths": image_paths,
            "request_label": request_label,
        }
        if model:
            kwargs["model"] = model
        if llm_config:
            kwargs["llm_config"] = llm_config
        try:
            return self.llm.json_completion(system_prompt, user_prompt, **kwargs)
        except TypeError:
            # Test doubles and legacy LLM wrappers may not support per-call config or model overrides.
            pass

        if llm_config:
            kwargs.pop("llm_config", None)
            try:
                return self.llm.json_completion(system_prompt, user_prompt, **kwargs)
            except TypeError:
                pass
        kwargs.pop("model", None)
        return self.llm.json_completion(
            system_prompt,
            user_prompt,
            image_paths=image_paths,
            request_label=request_label,
        )

    def _validate_payload(self, raw_payload: dict[str, Any], *, extra_quality_flags: list[str] | None = None) -> tuple[dict[str, Any], dict[str, float], list[str], str]:
        candidate_annotation = raw_payload.get("final_annotation", raw_payload)
        field_confidence = raw_payload.get("field_confidence", {})
        flattened_annotation, inline_confidence = flatten_annotation_payload(candidate_annotation if isinstance(candidate_annotation, dict) else {})
        merged_confidence = {**inline_confidence, **(field_confidence if isinstance(field_confidence, dict) else {})}
        annotation_status = self._status_from_annotation(flattened_annotation)

        final_annotation, merged_confidence, quality_flags, fatal = validate_annotation(
            annotation=flattened_annotation,
            field_confidence=merged_confidence,
            domains=self.domains,
            annotation_status=annotation_status,
        )
        quality_flags = list(dict.fromkeys([*quality_flags, *(extra_quality_flags or [])]))
        if fatal:
            annotation_status = "failed"
        return final_annotation, merged_confidence, quality_flags, annotation_status

    def _candidate_system_prompt(self, spec: dict[str, Any]) -> str:
        agent = str(spec.get("agent") or "").strip()
        if agent:
            role_instruction = f"当前专责智能体角色：{agent}。关注重点：{AGENT_INSTRUCTIONS.get(agent, AGENT_INSTRUCTIONS['general'])}"
        else:
            role_instruction = FULL_CANDIDATE_INSTRUCTION
        return (
            "你是群体情感视频标注器。只输出 JSON。"
            "所有字段必须完整，enum 字段必须精确命中允许值。"
            "如果判定不是群体样本，只允许使用 rejected sentinel values。"
            f"{role_instruction}"
        )

    def _candidate_user_prompt(self, context: dict[str, Any]) -> str:
        return (
            f"clip_manifest={json.dumps(context['clip_manifest'], ensure_ascii=False)}\n"
            f"text_evidence={json.dumps(context['subtitle_payload'], ensure_ascii=False)}\n"
            f"请输出这些字段:\n{self._schema_lines()}\n"
            "同时返回 field_confidence，格式示例："
            '{"final_annotation": {...}, "field_confidence": {"overall": 0.8, "scene_description": 0.7}}'
        )

    def _run_candidate(self, context: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        raw_payload = self._json_completion(
            self._candidate_system_prompt(spec),
            self._candidate_user_prompt(context),
            image_paths=context["image_paths"],
            request_label=f"annotate.candidate.{spec['name']}",
            model=spec.get("model"),
            llm_config=spec.get("llm_config"),
        )
        final_annotation, field_confidence, quality_flags, status = self._validate_payload(raw_payload)
        return {
            "candidate_id": spec["name"],
            "agent": spec.get("agent"),
            "model": spec.get("model") or getattr(self.llm, "model", None),
            "status": status,
            "raw_payload": raw_payload,
            "final_annotation": final_annotation,
            "field_confidence": field_confidence,
            "quality_flags": quality_flags,
        }

    def _run_candidates(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        specs = self._candidate_specs()
        if len(specs) == 1:
            try:
                return [self._run_candidate(context, specs[0])]
            except Exception as exc:
                return [self._failed_candidate(specs[0], exc)]

        workers = max(1, int(self.config.get("annotation", {}).get("candidate_workers") or len(specs)))
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(len(specs), workers)) as executor:
            futures = {executor.submit(self._run_candidate, context, spec): spec for spec in specs}
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(self._failed_candidate(spec, exc))
        return sorted(results, key=lambda item: str(item.get("candidate_id") or ""))

    @staticmethod
    def _failed_candidate(spec: dict[str, Any], exc: Exception) -> dict[str, Any]:
        return {
            "candidate_id": spec["name"],
            "agent": spec.get("agent"),
            "model": spec.get("model"),
            "status": "failed",
            "error": str(exc),
            "raw_payload": {},
            "final_annotation": {},
            "field_confidence": {},
            "quality_flags": ["candidate_failed"],
        }

    def _judge_prompt(self, context: dict[str, Any], candidate_results: list[dict[str, Any]]) -> tuple[str, str]:
        candidate_summaries = [
            {
                "candidate_id": item.get("candidate_id"),
                "agent": item.get("agent"),
                "model": item.get("model"),
                "status": item.get("status"),
                "final_annotation": item.get("final_annotation"),
                "field_confidence": item.get("field_confidence"),
                "quality_flags": item.get("quality_flags"),
            }
            for item in candidate_results
        ]
        system_prompt = (
            "你是群体情感标注裁决智能体。只输出 JSON。"
            "请基于同一份视频证据和候选标注进行字段级裁决，最终字段必须符合 schema 值域。"
            "输出必须包含 final_annotation、field_confidence、selected_candidate_ids、conflict_fields、decision_reasoning。"
        )
        user_prompt = (
            f"clip_manifest={json.dumps(context['clip_manifest'], ensure_ascii=False)}\n"
            f"text_evidence={json.dumps(context['subtitle_payload'], ensure_ascii=False)}\n"
            f"candidate_annotations={json.dumps(candidate_summaries, ensure_ascii=False)}\n"
            f"schema:\n{self._schema_lines()}\n"
            "请裁决并输出最终 JSON。"
        )
        return system_prompt, user_prompt

    def _run_judge(self, context: dict[str, Any], candidate_results: list[dict[str, Any]]) -> dict[str, Any]:
        cfg = self._judge_config()
        system_prompt, user_prompt = self._judge_prompt(context, candidate_results)
        raw_payload = self._json_completion(
            system_prompt,
            user_prompt,
            image_paths=context["image_paths"],
            request_label="annotate.judge",
            model=str(cfg.get("model") or "").strip() or None,
            llm_config=self._llm_config_overrides(cfg),
        )
        final_annotation, field_confidence, quality_flags, status = self._validate_payload(
            raw_payload,
            extra_quality_flags=self._quality_flags(raw_payload.get("quality_flags")),
        )
        return {
            "enabled": True,
            "status": status,
            "raw_payload": raw_payload,
            "selected_candidate_ids": raw_payload.get("selected_candidate_ids") or [],
            "conflict_fields": raw_payload.get("conflict_fields") or [],
            "decision_reasoning": raw_payload.get("decision_reasoning"),
            "final_annotation": final_annotation,
            "field_confidence": field_confidence,
            "quality_flags": quality_flags,
        }

    @staticmethod
    def _best_candidate(candidate_results: list[dict[str, Any]]) -> dict[str, Any]:
        usable = [item for item in candidate_results if item.get("status") in {"done", "rejected"}]
        if not usable:
            return candidate_results[0]
        return max(usable, key=lambda item: float((item.get("field_confidence") or {}).get("overall", 0.0)))

    @staticmethod
    def _candidate_disagreement_fields(candidate_results: list[dict[str, Any]]) -> list[str]:
        usable_annotations = [
            item.get("final_annotation")
            for item in candidate_results
            if item.get("status") == "done" and isinstance(item.get("final_annotation"), dict)
        ]
        if len(usable_annotations) < 2:
            return []
        field_names = sorted({field for annotation in usable_annotations for field in annotation})
        disagreements: list[str] = []
        for field_name in field_names:
            values = {
                json.dumps(annotation.get(field_name), ensure_ascii=False, sort_keys=True)
                for annotation in usable_annotations
            }
            if len(values) > 1:
                disagreements.append(field_name)
        return disagreements

    def _apply_confidence_policy(
        self,
        *,
        final_annotation: dict[str, Any],
        field_confidence: dict[str, float],
        quality_flags: list[str],
        status: str,
    ) -> tuple[dict[str, Any], dict[str, float], list[str], str, bool]:
        annotation_cfg = self.config.get("annotation", {})
        overall = float(field_confidence.get("overall", 0.0))
        routing_cfg = annotation_cfg.get("confidence_routing") or {}
        if routing_cfg.get("enabled") and status == "done":
            reject_below = float(routing_cfg.get("reject_below", 0.80) or 0.80)
            review_below = float(routing_cfg.get("review_below", 0.85) or 0.85)
            if overall < reject_below:
                quality_flags = list(dict.fromkeys([*quality_flags, "confidence_below_reject_threshold"]))
                return final_annotation, field_confidence, quality_flags, "rejected", False
            if overall < review_below:
                quality_flags = list(dict.fromkeys([*quality_flags, "confidence_review_band"]))
                return final_annotation, field_confidence, quality_flags, status, True

        if overall < float(annotation_cfg.get("min_overall_confidence", 0.6)):
            quality_flags = list(dict.fromkeys([*quality_flags, "low_confidence"]))
        review_flags = {"low_confidence", "judge_failed", "judge_schema_invalid", "candidate_disagreement"}
        review_required = status == "done" and bool(review_flags.intersection(quality_flags))
        return final_annotation, field_confidence, quality_flags, status, review_required

    def annotate(self, clip_record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, float], list[str], str, bool]:
        context = self._load_context(clip_record)
        candidate_results = self._run_candidates(context)
        best_candidate = self._best_candidate(candidate_results)
        disagreement_fields = self._candidate_disagreement_fields(candidate_results)
        judge_payload: dict[str, Any] = {"enabled": False, "reason": "disabled"}

        final_annotation = dict(best_candidate.get("final_annotation") or {})
        field_confidence = dict(best_candidate.get("field_confidence") or {})
        quality_flags = self._quality_flags(best_candidate.get("quality_flags"))
        annotation_status = str(best_candidate.get("status") or "failed")

        judge_enabled = bool(self._judge_config().get("enabled", False))
        if judge_enabled and any(item.get("status") in {"done", "rejected"} for item in candidate_results):
            try:
                judge_payload = self._run_judge(context, candidate_results)
                if judge_payload.get("status") in {"done", "rejected"}:
                    final_annotation = dict(judge_payload.get("final_annotation") or {})
                    field_confidence = dict(judge_payload.get("field_confidence") or {})
                    quality_flags = self._quality_flags(judge_payload.get("quality_flags"))
                    annotation_status = str(judge_payload.get("status"))
                else:
                    quality_flags = list(dict.fromkeys([*quality_flags, "judge_schema_invalid"]))
                    judge_payload["fallback_candidate_id"] = best_candidate.get("candidate_id")
            except Exception as exc:
                quality_flags = list(dict.fromkeys([*quality_flags, "judge_failed"]))
                judge_payload = {
                    "enabled": True,
                    "status": "failed",
                    "error": str(exc),
                    "fallback_candidate_id": best_candidate.get("candidate_id"),
                }
        elif disagreement_fields:
            quality_flags = list(dict.fromkeys([*quality_flags, "candidate_disagreement"]))

        agent_outputs = {
            "annotation_framework": "adjudicated_v1",
            "candidate_results": candidate_results,
            "judge": judge_payload,
            "candidate_disagreement_fields": disagreement_fields,
        }
        if len(candidate_results) == 1:
            agent_outputs["single_model"] = candidate_results[0].get("raw_payload", {})

        final_annotation, field_confidence, quality_flags, annotation_status, review_required = self._apply_confidence_policy(
            final_annotation=final_annotation,
            field_confidence=field_confidence,
            quality_flags=quality_flags,
            status=annotation_status,
        )
        return agent_outputs, final_annotation, field_confidence, quality_flags, annotation_status, review_required


class SingleModelAnnotator(AdjudicatedAnnotator):
    """Backward-compatible name for the adjudicated annotation framework."""


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
            self.annotation_repo.record_annotation_audit(
                annotation_uid=annotation_uid,
                clip_uid=clip_record["clip_uid"],
                agent_outputs={"annotation_framework": "adjudicated_v1", "candidate_results": [], "judge": {"enabled": False}},
                final_annotation={},
                field_confidence={},
                quality_flags=["parse_error"],
                review_required=False,
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
        self.annotation_repo.record_annotation_audit(
            annotation_uid=annotation_uid,
            clip_uid=clip_record["clip_uid"],
            agent_outputs=agent_outputs,
            final_annotation=final_annotation,
            field_confidence=field_confidence,
            quality_flags=quality_flags,
            review_required=review_required,
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
