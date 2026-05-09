from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from group_emotion_video.annotation import AdjudicatedAnnotator


class QueueLLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.model = "default-model"

    def json_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        image_paths: list[str] | None = None,
        request_label: str | None = None,
        model: str | None = None,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "image_paths": image_paths or [],
                "request_label": request_label,
                "model": model,
                "llm_config": llm_config,
            }
        )
        if not self.responses:
            raise AssertionError("No queued LLM response.")
        return self.responses.pop(0)


def _domains() -> dict[str, Any]:
    return {
        "fields": {
            "group_emotion": {
                "type": "enum",
                "required": True,
                "required_when": None,
                "description": "群体主导情绪",
                "allowed_values": ["Joy", "Anxiety"],
                "allow_rejected_only_values": ["none"],
            }
        }
    }


def _clip_record(tmp_path: Path) -> dict[str, str]:
    clip_manifest_path = tmp_path / "clip_manifest.json"
    subtitle_path = tmp_path / "subtitle_or_text.json"
    keyframes_path = tmp_path / "keyframes_manifest.json"
    clip_manifest_path.write_text(
        json.dumps({"clip_uid": "clip_1", "video_uid": "video_1", "start_sec": 0, "end_sec": 8}),
        encoding="utf-8",
    )
    subtitle_path.write_text(json.dumps({"mode": "metadata_fallback", "full_text": "学生集体欢呼"}), encoding="utf-8")
    keyframes_path.write_text(json.dumps({"frames": []}), encoding="utf-8")
    return {
        "clip_uid": "clip_1",
        "manifest_path": str(clip_manifest_path),
        "subtitle_path": str(subtitle_path),
        "keyframes_manifest_path": str(keyframes_path),
    }


def test_adjudicated_annotator_uses_judge_final_annotation(tmp_path: Path) -> None:
    llm = QueueLLM(
        [
            {
                "final_annotation": {"group_emotion": "Joy"},
                "field_confidence": {"overall": 0.81, "group_emotion": 0.8},
            },
            {
                "final_annotation": {"group_emotion": "Anxiety"},
                "field_confidence": {"overall": 0.92, "group_emotion": 0.9},
                "selected_candidate_ids": ["primary"],
                "conflict_fields": ["group_emotion"],
                "decision_reasoning": "视频证据更支持紧张而非喜悦。",
            },
        ]
    )
    config = {
        "annotation": {
            "min_overall_confidence": 0.6,
            "candidate_workers": 1,
            "candidate_models": [
                {"name": "primary", "agent": "event_emotion", "model": "candidate-model", "enabled": True}
            ],
            "judge": {"enabled": True, "model": "judge-model"},
        }
    }

    annotator = AdjudicatedAnnotator(config, domains=_domains(), llm=llm)
    agent_outputs, final_annotation, field_confidence, quality_flags, status, review_required = annotator.annotate(
        _clip_record(tmp_path)
    )

    assert final_annotation == {"group_emotion": "Anxiety"}
    assert field_confidence["overall"] == 0.92
    assert quality_flags == []
    assert status == "done"
    assert review_required is False
    assert agent_outputs["annotation_framework"] == "adjudicated_v1"
    assert agent_outputs["candidate_results"][0]["candidate_id"] == "primary"
    assert agent_outputs["candidate_results"][0]["final_annotation"] == {"group_emotion": "Joy"}
    assert agent_outputs["judge"]["selected_candidate_ids"] == ["primary"]
    assert [call["request_label"] for call in llm.calls] == ["annotate.candidate.primary", "annotate.judge"]
    assert [call["model"] for call in llm.calls] == ["candidate-model", "judge-model"]


def test_candidate_models_can_be_plain_model_names(tmp_path: Path) -> None:
    llm = QueueLLM(
        [
            {
                "final_annotation": {"group_emotion": "Joy"},
                "field_confidence": {"overall": 0.81, "group_emotion": 0.8},
            },
            {
                "final_annotation": {"group_emotion": "Anxiety"},
                "field_confidence": {"overall": 0.84, "group_emotion": 0.82},
            },
            {
                "final_annotation": {"group_emotion": "Joy"},
                "field_confidence": {"overall": 0.86, "group_emotion": 0.84},
            },
        ]
    )
    config = {
        "annotation": {
            "min_overall_confidence": 0.6,
            "candidate_workers": 1,
            "candidate_models": ["gemma-4-26b-a4b-it", "qwen3.5-35b", "internvl3_5-30b-a3b-hf"],
            "judge": {"enabled": False},
        }
    }

    annotator = AdjudicatedAnnotator(config, domains=_domains(), llm=llm)
    agent_outputs, *_ = annotator.annotate(_clip_record(tmp_path))

    assert [call["model"] for call in llm.calls] == [
        "gemma-4-26b-a4b-it",
        "qwen3.5-35b",
        "internvl3_5-30b-a3b-hf",
    ]
    assert [item["candidate_id"] for item in agent_outputs["candidate_results"]] == [
        "gemma-4-26b-a4b-it",
        "internvl3_5-30b-a3b-hf",
        "qwen3.5-35b",
    ]
    assert all(item["agent"] is None for item in agent_outputs["candidate_results"])
    assert "当前专责智能体角色" not in llm.calls[0]["system_prompt"]
    assert "完整候选模型" in llm.calls[0]["system_prompt"]


def test_candidate_models_can_override_llm_connection_config(tmp_path: Path) -> None:
    llm = QueueLLM(
        [
            {
                "final_annotation": {"group_emotion": "Joy"},
                "field_confidence": {"overall": 0.81, "group_emotion": 0.8},
            },
            {
                "final_annotation": {"group_emotion": "Joy"},
                "field_confidence": {"overall": 0.86, "group_emotion": 0.84},
            },
        ]
    )
    config = {
        "annotation": {
            "min_overall_confidence": 0.6,
            "candidate_workers": 1,
            "candidate_models": [
                {
                    "name": "gemma",
                    "model": "gemma-4-26b-a4b-it",
                    "base_url": "http://127.0.0.1:8101/v1",
                    "api_key_env": "GEMMA_API_KEY",
                    "temperature": 0.0,
                }
            ],
            "judge": {
                "enabled": True,
                "model": "judge-model",
                "llm": {"base_url": "http://127.0.0.1:8109/v1", "api_key_env": "JUDGE_API_KEY"},
            },
        }
    }

    AdjudicatedAnnotator(config, domains=_domains(), llm=llm).annotate(_clip_record(tmp_path))

    assert llm.calls[0]["model"] == "gemma-4-26b-a4b-it"
    assert llm.calls[0]["llm_config"] == {
        "base_url": "http://127.0.0.1:8101/v1",
        "api_key_env": "GEMMA_API_KEY",
        "temperature": 0.0,
    }
    assert llm.calls[1]["model"] == "judge-model"
    assert llm.calls[1]["llm_config"] == {
        "base_url": "http://127.0.0.1:8109/v1",
        "api_key_env": "JUDGE_API_KEY",
    }


def test_adjudicated_annotator_falls_back_when_judge_schema_invalid(tmp_path: Path) -> None:
    llm = QueueLLM(
        [
            {
                "final_annotation": {"group_emotion": "Joy"},
                "field_confidence": {"overall": 0.88, "group_emotion": 0.86},
            },
            {
                "final_annotation": {"group_emotion": "Invalid"},
                "field_confidence": {"overall": 0.91, "group_emotion": 0.9},
                "selected_candidate_ids": ["primary"],
                "conflict_fields": [],
                "decision_reasoning": "invalid enum should not be accepted",
            },
        ]
    )
    config = {
        "annotation": {
            "min_overall_confidence": 0.6,
            "candidate_models": [{"name": "primary", "agent": "general", "enabled": True}],
            "judge": {"enabled": True},
        }
    }

    annotator = AdjudicatedAnnotator(config, domains=_domains(), llm=llm)
    agent_outputs, final_annotation, field_confidence, quality_flags, status, review_required = annotator.annotate(
        _clip_record(tmp_path)
    )

    assert final_annotation == {"group_emotion": "Joy"}
    assert field_confidence["overall"] == 0.88
    assert quality_flags == ["judge_schema_invalid"]
    assert status == "done"
    assert review_required is True
    assert agent_outputs["judge"]["status"] == "failed"
    assert agent_outputs["judge"]["fallback_candidate_id"] == "primary"


def test_adjudicated_annotator_routes_confidence_bands(tmp_path: Path) -> None:
    config = {
        "annotation": {
            "min_overall_confidence": 0.6,
            "confidence_routing": {"enabled": True, "reject_below": 0.80, "review_below": 0.85},
            "candidate_models": [{"name": "primary", "agent": "general", "enabled": True}],
            "judge": {"enabled": False},
        }
    }
    low_llm = QueueLLM(
        [
            {
                "final_annotation": {"group_emotion": "Joy"},
                "field_confidence": {"overall": 0.79, "group_emotion": 0.79},
            }
        ]
    )
    low_result = AdjudicatedAnnotator(config, domains=_domains(), llm=low_llm).annotate(_clip_record(tmp_path))

    assert low_result[3] == ["confidence_below_reject_threshold"]
    assert low_result[4] == "rejected"
    assert low_result[5] is False

    review_llm = QueueLLM(
        [
            {
                "final_annotation": {"group_emotion": "Joy"},
                "field_confidence": {"overall": 0.82, "group_emotion": 0.82},
            }
        ]
    )
    review_result = AdjudicatedAnnotator(config, domains=_domains(), llm=review_llm).annotate(_clip_record(tmp_path))

    assert review_result[3] == ["confidence_review_band"]
    assert review_result[4] == "done"
    assert review_result[5] is True
