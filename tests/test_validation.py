from __future__ import annotations

from group_emotion_video.validation import validate_annotation


DOMAINS = {
    "fields": {
        "group_size_estimate": {
            "type": "enum",
            "required": True,
            "required_when": None,
            "allowed_values": ["small(3-10)", "medium(11-30)"],
            "allow_rejected_only_values": ["single", "none"],
        },
        "group_behavior_type": {
            "type": "enum",
            "required": True,
            "required_when": None,
            "allowed_values": ["watching", "working"],
            "allow_rejected_only_values": ["none"],
        },
        "group_emotion": {
            "type": "enum",
            "required": True,
            "required_when": None,
            "allowed_values": ["Joy", "Anxiety"],
            "allow_rejected_only_values": ["none"],
        },
        "trigger_event_type": {
            "type": "enum",
            "required": True,
            "required_when": None,
            "allowed_values": ["speech_act", "none"],
            "allow_rejected_only_values": [],
        },
        "trigger_event_description": {
            "type": "string",
            "required": True,
            "required_when": "trigger_event_type!=none",
            "allowed_values": None,
            "allow_rejected_only_values": [],
        },
        "valence": {
            "type": "integer",
            "required": True,
            "required_when": None,
            "allowed_values": None,
            "allow_rejected_only_values": [],
            "min_value": 1,
            "max_value": 9,
        },
        "arousal": {
            "type": "integer",
            "required": True,
            "required_when": None,
            "allowed_values": None,
            "allow_rejected_only_values": [],
            "min_value": 1,
            "max_value": 9,
        },
    }
}


def test_validate_annotation_accepts_valid_done_annotation() -> None:
    annotation, confidence, flags, fatal = validate_annotation(
        annotation={
            "group_size_estimate": "small(3-10)",
            "group_behavior_type": "watching",
            "group_emotion": "Joy",
            "trigger_event_type": "speech_act",
            "trigger_event_description": "teacher announcement",
            "valence": 7,
            "arousal": 6,
        },
        field_confidence={"overall": 0.8, "group_emotion": 0.9},
        domains=DOMAINS,
        annotation_status="done",
    )
    assert not fatal
    assert not flags
    assert confidence["overall"] == 0.8
    assert annotation["group_emotion"] == "Joy"


def test_validate_annotation_rejects_invalid_enum_and_conditional_missing() -> None:
    _, _, flags, fatal = validate_annotation(
        annotation={
            "group_size_estimate": "small(3-10)",
            "group_behavior_type": "watching",
            "group_emotion": "UnknownEmotion",
            "trigger_event_type": "speech_act",
            "valence": 7,
            "arousal": 6,
        },
        field_confidence={"overall": 0.8},
        domains=DOMAINS,
        annotation_status="done",
    )
    assert fatal
    assert "schema_invalid_enum" in flags
    assert "schema_missing_field" in flags


def test_validate_annotation_allows_rejected_only_sentinels_only_for_rejected() -> None:
    _, _, flags_done, fatal_done = validate_annotation(
        annotation={
            "group_size_estimate": "single",
            "group_behavior_type": "none",
            "group_emotion": "none",
            "trigger_event_type": "none",
            "trigger_event_description": "",
            "valence": 5,
            "arousal": 5,
        },
        field_confidence={"overall": 0.8},
        domains=DOMAINS,
        annotation_status="done",
    )
    assert fatal_done
    assert "schema_invalid_enum" in flags_done

    _, _, flags_rejected, fatal_rejected = validate_annotation(
        annotation={
            "group_size_estimate": "single",
            "group_behavior_type": "none",
            "group_emotion": "none",
            "trigger_event_type": "none",
            "trigger_event_description": "",
            "valence": 5,
            "arousal": 5,
        },
        field_confidence={"overall": 0.8},
        domains=DOMAINS,
        annotation_status="rejected",
    )
    assert not fatal_rejected
    assert "schema_invalid_enum" not in flags_rejected
