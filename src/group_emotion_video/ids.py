from __future__ import annotations

from .hashing import text_sha1


def make_seed_id(excel_row: int, scene_text: str, trigger_text: str) -> str:
    return f"seed_{text_sha1(f'{excel_row}|{scene_text}|{trigger_text}')[:12]}"


def make_query_id(seed_id: str, query_text: str) -> str:
    return f"query_{text_sha1(f'{seed_id}|{query_text}')[:12]}"


def make_video_uid(platform: str, platform_video_id: str) -> str:
    return f"video_{text_sha1(f'{platform}|{platform_video_id}')[:16]}"


def make_clip_uid(video_uid: str, start_sec: float, end_sec: float) -> str:
    return f"clip_{text_sha1(f'{video_uid}|{start_sec:.3f}|{end_sec:.3f}')[:16]}"


def make_annotation_uid(clip_uid: str) -> str:
    return f"ann_{text_sha1(clip_uid)[:16]}"
