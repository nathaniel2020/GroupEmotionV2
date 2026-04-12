from __future__ import annotations

import json
import re
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


def _enum_key(value: Any) -> str:
    return re.sub(r"[\s_/\-]+", "", str(value or "").strip().lower())


def _coerce_confidence_value(raw_value: Any) -> float | None:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if value > 1.0 and value <= 100.0:
        value = value / 100.0
    if value < 0.0 or value > 1.0:
        return None
    return round(value, 3)


def _enum_aliases(field_name: str) -> dict[str, str]:
    if field_name == "social_structure.ingroup_salience":
        return {
            "academichierarchy": "high",
            "studentvsprofessors": "high",
            "studentteacherhierarchy": "high",
            "grouphigh": "high",
            "groupidentityhigh": "high",
            "salient": "high",
            "notsalient": "low",
        }
    return {}


def _coerce_enum_value(field_name: str, value: Any, candidates: set[str]) -> str | Any:
    if value is None:
        return value
    raw_text = str(value).strip()
    if not raw_text:
        return ""
    normalized = _enum_key(raw_text)
    alias_map = _enum_aliases(field_name)
    if normalized in alias_map and alias_map[normalized] in candidates:
        return alias_map[normalized]

    candidate_map: dict[str, str] = {}
    for candidate in candidates:
        candidate_map[_enum_key(candidate)] = candidate
        if "(" in candidate:
            candidate_map.setdefault(_enum_key(candidate.split("(", 1)[0]), candidate)

    if normalized in candidate_map:
        return candidate_map[normalized]

    # For compact enum sets such as none/low/high, allow substring recovery.
    if all(len(_enum_key(candidate)) <= 12 for candidate in candidates):
        for candidate in candidates:
            candidate_key = _enum_key(candidate)
            if candidate_key and candidate_key in normalized:
                return candidate
    return value


def _coerce_integer_value(value: Any, *, min_value: int | None, max_value: int | None) -> Any:
    candidate = value
    if isinstance(candidate, str):
        stripped = candidate.strip()
        if not stripped:
            return ""
        try:
            candidate = float(stripped) if "." in stripped else int(stripped)
        except ValueError:
            return value
    if isinstance(candidate, float):
        rounded = int(round(candidate))
        if min_value is not None and rounded < int(min_value):
            return value
        if max_value is not None and rounded > int(max_value):
            return value
        return rounded
    return candidate


def _coerce_annotation_values(annotation: dict[str, Any], fields: dict[str, Any], annotation_status: str) -> dict[str, Any]:
    coerced: dict[str, Any] = {}
    for field_name, value in annotation.items():
        field_spec = fields.get(field_name)
        if not field_spec:
            coerced[field_name] = value
            continue
        next_value = value
        allowed_values = set(str(item) for item in field_spec.get("allowed_values") or [])
        rejected_only = set(str(item) for item in field_spec.get("allow_rejected_only_values") or [])
        candidate_values = set(allowed_values)
        if annotation_status == "rejected":
            candidate_values |= rejected_only
        if candidate_values:
            next_value = _coerce_enum_value(field_name, next_value, candidate_values)
        if field_spec.get("type") == "integer":
            next_value = _coerce_integer_value(
                next_value,
                min_value=field_spec.get("min_value"),
                max_value=field_spec.get("max_value"),
            )
        coerced[field_name] = next_value
    return coerced


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
    fields = domains["fields"]
    normalized = {key: normalize_scalar(value) for key, value in annotation.items()}
    normalized = _coerce_annotation_values(normalized, fields, annotation_status)
    normalized_confidence: dict[str, float] = {}
    quality_flags: list[str] = []
    fatal = False

    for key, raw_value in field_confidence.items():
        value = _coerce_confidence_value(raw_value)
        if value is None:
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
