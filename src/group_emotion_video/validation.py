from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_annotation_domains(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text.isdigit():
            return int(text)
        try:
            if "." in text:
                return float(text)
        except ValueError:
            return text
        return text
    return value


def flatten_annotation_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, float]]:
    final_annotation: dict[str, Any] = {}
    field_confidence: dict[str, float] = {}

    def walk(value: Any, prefix: str | None) -> None:
        if prefix and isinstance(value, dict) and "value" in value:
            final_annotation[prefix] = normalize_scalar(value.get("value"))
            confidence = value.get("confidence")
            if isinstance(confidence, (int, float)):
                field_confidence[prefix] = float(confidence)
            elif isinstance(confidence, str):
                try:
                    field_confidence[prefix] = float(confidence.strip())
                except ValueError:
                    pass
            return
        if prefix and not isinstance(value, dict):
            final_annotation[prefix] = normalize_scalar(value)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                next_prefix = str(key) if not prefix else f"{prefix}.{key}"
                walk(item, next_prefix)

    walk(payload, None)
    return final_annotation, field_confidence


def _conditional_required(field_name: str, field_spec: dict[str, Any], annotation: dict[str, Any]) -> bool:
    required_when = field_spec.get("required_when")
    if required_when != "trigger_event_type!=none":
        return bool(field_spec.get("required"))
    trigger_value = str(annotation.get("trigger_event_type") or "").strip()
    return trigger_value.lower() != "none"


def validate_annotation(
    *,
    annotation: dict[str, Any],
    field_confidence: dict[str, Any],
    domains: dict[str, Any],
    annotation_status: str,
) -> tuple[dict[str, Any], dict[str, float], list[str], bool]:
    normalized = {key: normalize_scalar(value) for key, value in annotation.items()}
    normalized_confidence: dict[str, float] = {}
    quality_flags: list[str] = []
    fatal = False
    fields = domains["fields"]

    for key, raw_value in field_confidence.items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            quality_flags.append("schema_invalid_range")
            fatal = True
            continue
        if value < 0.0 or value > 1.0:
            quality_flags.append("schema_invalid_range")
            fatal = True
            continue
        normalized_confidence[str(key)] = value

    if "overall" not in normalized_confidence:
        collected = [value for key, value in normalized_confidence.items() if key != "overall"]
        normalized_confidence["overall"] = round(sum(collected) / len(collected), 3) if collected else 0.0

    for field_name, field_spec in fields.items():
        value = normalized.get(field_name)
        if value in (None, ""):
            if _conditional_required(field_name, field_spec, normalized):
                quality_flags.append("schema_missing_field")
                fatal = True
            continue

        rejected_only = set(field_spec.get("allow_rejected_only_values") or [])
        if rejected_only and str(value) in rejected_only and annotation_status != "rejected":
            quality_flags.append("schema_invalid_enum")
            fatal = True
            continue

        allowed_values = field_spec.get("allowed_values")
        if allowed_values:
            effective_allowed = set(str(item) for item in allowed_values)
            if annotation_status == "rejected":
                effective_allowed |= rejected_only
            if str(value) not in effective_allowed:
                quality_flags.append("schema_invalid_enum")
                fatal = True
                continue

        if field_spec.get("type") == "integer":
            if not isinstance(value, int):
                quality_flags.append("schema_invalid_range")
                fatal = True
                continue
            min_value = field_spec.get("min_value")
            max_value = field_spec.get("max_value")
            if min_value is not None and int(value) < int(min_value):
                quality_flags.append("schema_invalid_range")
                fatal = True
            if max_value is not None and int(value) > int(max_value):
                quality_flags.append("schema_invalid_range")
                fatal = True

    known_fields = set(fields)
    if any(key not in known_fields for key in normalized):
        quality_flags.append("schema_unknown_field")

    deduped = list(dict.fromkeys(quality_flags))
    return normalized, normalized_confidence, deduped, fatal
