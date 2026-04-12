from __future__ import annotations

import json
import math
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from .ids import make_clip_uid


GROUP_HINTS = {
    "群体",
    "集体",
    "观众",
    "人群",
    "众人",
    "大家",
    "全班",
    "班级",
    "班上",
    "同学",
    "同学们",
    "学生",
    "师生",
    "老师",
    "教师",
    "队友",
    "队员",
    "球迷",
    "粉丝",
    "现场",
    "audience",
    "crowd",
    "students",
    "student",
    "classmates",
    "class",
    "classroom",
    "team",
    "fans",
}
EMOTION_HINTS = {
    "开心",
    "高兴",
    "喜悦",
    "喜极而泣",
    "兴奋",
    "欢呼",
    "欢笑",
    "鼓掌",
    "喝彩",
    "庆祝",
    "尖叫",
    "呐喊",
    "感动",
    "激动",
    "紧张",
    "焦虑",
    "害怕",
    "恐惧",
    "愤怒",
    "生气",
    "崩溃",
    "失望",
    "难过",
    "伤心",
    "委屈",
    "哭",
    "惊讶",
    "沸腾",
    "尴尬",
    "翻唱",
    "合唱",
    "演唱",
    "唱歌",
    "歌声",
    "表扬",
    "表彰",
    "夸奖",
    "荣誉",
    "获奖",
    "夺冠",
    "晋级",
    "录取",
    "通过",
    "成功",
    "认可",
    "批评",
    "责备",
    "质疑",
    "淘汰",
    "拒绝",
    "拒稿",
    "失败",
    "落选",
    "未过",
    "冲突",
    "争执",
    "争吵",
    "打架",
    "吵架",
    "reaction",
    "reactions",
    "reaction",
    "emotion",
    "emotional",
    "cheer",
    "cheering",
    "celebrate",
    "celebration",
    "applause",
    "clap",
    "excited",
    "happy",
    "joy",
    "nervous",
    "anxious",
    "angry",
    "sad",
    "cry",
    "sing",
    "chorus",
}


class PreprocessingService:
    def __init__(self, config: dict[str, Any], *, video_repo: Any, clip_repo: Any, layout: dict[str, Path], logger: Any, llm: Any | None = None):
        self.config = config
        self.video_repo = video_repo
        self.clip_repo = clip_repo
        self.layout = layout
        self.logger = logger
        self.llm = llm

    def _build_windows(self, video_record: dict[str, Any]) -> list[dict[str, Any]]:
        extra = dict(video_record.get("extra") or {})
        subtitle_segments = list(extra.get("subtitle_segments") or [])
        cfg = self.config["preprocessing"]
        windows: list[dict[str, Any]] = []
        if subtitle_segments and bool(cfg.get("prefer_subtitles", True)):
            current: list[dict[str, Any]] = []
            current_start = None
            for segment in subtitle_segments:
                start = float(segment.get("start", 0.0))
                end = float(segment.get("end", start))
                if current_start is None:
                    current_start = start
                current.append(segment)
                duration = end - current_start
                if duration >= float(cfg["subtitle_target_min_sec"]) and (duration >= float(cfg["subtitle_target_max_sec"]) or len(current) >= 4):
                    windows.append(
                        {
                            "start_sec": current_start,
                            "end_sec": end,
                            "segmentation_mode": "subtitle",
                            "transcript_segments": list(current),
                        }
                    )
                    current = []
                    current_start = None
            if current and current_start is not None:
                windows.append(
                    {
                        "start_sec": current_start,
                        "end_sec": float(current[-1].get("end", current_start)),
                        "segmentation_mode": "subtitle",
                        "transcript_segments": list(current),
                    }
                )
            return windows

        duration = float(video_record.get("duration_sec") or 0.0)
        step = float(cfg["fixed_window_sec"]) - float(cfg["overlap_sec"])
        step = max(step, 1.0)
        cursor = 0.0
        while cursor < duration:
            end_sec = min(duration, cursor + float(cfg["fixed_window_sec"]))
            windows.append(
                {
                    "start_sec": cursor,
                    "end_sec": end_sec,
                    "segmentation_mode": "fixed_window",
                    "transcript_segments": [],
                }
            )
            if end_sec >= duration:
                break
            cursor += step
        return windows

    @staticmethod
    def _window_text(video_record: dict[str, Any], window: dict[str, Any]) -> str:
        segments = window.get("transcript_segments") or []
        if segments:
            return " ".join(str(segment.get("text") or "").strip() for segment in segments if str(segment.get("text") or "").strip())
        return PreprocessingService._metadata_text(video_record)

    @staticmethod
    def _metadata_text(video_record: dict[str, Any]) -> str:
        tokens = [
            str(video_record.get("title") or ""),
            str(video_record.get("description") or ""),
            " ".join(video_record.get("tags") or []),
            str(video_record.get("scene_text") or ""),
            str(video_record.get("trigger_text") or ""),
        ]
        return " ".join(token for token in tokens if token).strip()

    @staticmethod
    def _has_any_hint(text: str, hints: set[str]) -> float:
        if not text:
            return 0.0
        lowered = text.lower()
        return 1.0 if any(token.lower() in lowered for token in hints) else 0.0

    def _l1_filter(self, window: dict[str, Any]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        duration = float(window["end_sec"]) - float(window["start_sec"])
        if duration < float(self.config["preprocessing"]["min_clip_duration_sec"]):
            reasons.append("duration_too_short")
        if duration > float(self.config["preprocessing"]["max_clip_duration_sec"]):
            reasons.append("duration_too_long")
        return (not reasons), reasons

    def _l2_filter(self, video_record: dict[str, Any], window: dict[str, Any]) -> tuple[bool, dict[str, float], list[str]]:
        transcript_text = self._window_text({"title": "", "description": "", "tags": [], "scene_text": "", "trigger_text": ""}, window)
        metadata_text = self._metadata_text(video_record)
        transcript_group_score = self._has_any_hint(transcript_text, GROUP_HINTS)
        metadata_group_score = self._has_any_hint(metadata_text, GROUP_HINTS)
        transcript_emotion_score = self._has_any_hint(transcript_text, EMOTION_HINTS)
        metadata_emotion_score = self._has_any_hint(metadata_text, EMOTION_HINTS)
        group_score = max(transcript_group_score, metadata_group_score)
        emotion_score = max(transcript_emotion_score, metadata_emotion_score)
        scores = {
            "group_presence_score": group_score,
            "emotion_signal_score": emotion_score,
            "transcript_group_signal_score": transcript_group_score,
            "metadata_group_signal_score": metadata_group_score,
            "transcript_emotion_signal_score": transcript_emotion_score,
            "metadata_emotion_signal_score": metadata_emotion_score,
        }
        reasons: list[str] = []
        use_llm_filter = bool(self.config["preprocessing"].get("use_llm_filter", False))
        hard_reject = group_score <= 0.0 and (emotion_score <= 0.0 and not use_llm_filter)
        if hard_reject and group_score <= 0.0:
            reasons.append("weak_group_signal")
        if hard_reject and emotion_score <= 0.0 and not use_llm_filter:
            reasons.append("weak_emotion_signal")
        return (not reasons), scores, reasons

    def _copy_or_trim_clip(self, source_path: str, target_path: Path, start_sec: float, duration_sec: float) -> None:
        mode = str(self.config["preprocessing"].get("clip_export_mode", "ffmpeg")).strip().lower()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "copy":
            shutil.copyfile(source_path, target_path)
            return
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            str(start_sec),
            "-i",
            source_path,
            "-t",
            str(duration_sec),
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(target_path),
        ]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _extract_keyframes(self, clip_path: Path, clip_uid: str, duration_sec: float) -> Path:
        keyframe_dir = self.layout["artifact_clips"] / clip_uid / "frames"
        keyframe_dir.mkdir(parents=True, exist_ok=True)
        frames: list[dict[str, Any]] = []
        if duration_sec > 0:
            step = duration_sec / max(1, int(self.config["preprocessing"].get("keyframe_count", 4)))
        else:
            step = 1.0
        for index in range(int(self.config["preprocessing"].get("keyframe_count", 4))):
            timestamp = round(min(duration_sec, index * step + 0.01), 3)
            frame_path = keyframe_dir / f"frame_{index + 1:02d}.jpg"
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-ss",
                        str(timestamp),
                        "-i",
                        str(clip_path),
                        "-frames:v",
                        "1",
                        str(frame_path),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if frame_path.exists():
                    frames.append({"frame_index": index + 1, "clip_timestamp_sec": timestamp, "path": str(frame_path)})
            except Exception:
                break
        manifest_path = self.layout["artifact_clips"] / clip_uid / "keyframes_manifest.json"
        manifest_path.write_text(json.dumps({"frames": frames}, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path

    def _write_subtitle_artifact(self, video_record: dict[str, Any], window: dict[str, Any], clip_uid: str) -> Path:
        path = self.layout["artifact_clips"] / clip_uid / "subtitle_or_text.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        segments = window.get("transcript_segments") or []
        payload = {
            "mode": "subtitle" if segments else "metadata_fallback",
            "segments": segments,
            "full_text": self._window_text(video_record, window),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _write_clip_manifest(self, payload: dict[str, Any], clip_uid: str) -> Path:
        path = self.layout["artifact_clips"] / clip_uid / "clip_manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _write_clip_rejection(self, payload: dict[str, Any], clip_uid: str) -> None:
        path = self.layout["artifact_rejections"] / "clips"
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{clip_uid}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _delete_file(path: str | None) -> None:
        if path:
            file_path = Path(path)
            if file_path.exists():
                file_path.unlink()

    def _process_video(self, video_record: dict[str, Any]) -> int:
        preprocess_started_at = datetime.now().isoformat(timespec="seconds")
        start_clock = time.perf_counter()
        self.logger.info("preprocess_start video_uid=%s title=%s", video_record["video_uid"], video_record.get("title"))
        windows = self._build_windows(video_record)
        accepted_count = 0
        for window in windows:
            duration_sec = round(float(window["end_sec"]) - float(window["start_sec"]), 3)
            clip_uid = make_clip_uid(video_record["video_uid"], window["start_sec"], window["end_sec"])
            l1_ok, l1_reasons = self._l1_filter(window)
            l2_ok, scores, l2_reasons = self._l2_filter(video_record, window)
            reasons = list(dict.fromkeys([*l1_reasons, *l2_reasons]))
            if not (l1_ok and l2_ok):
                payload = {
                    "clip_uid": clip_uid,
                    "video_uid": video_record["video_uid"],
                    "start_sec": window["start_sec"],
                    "end_sec": window["end_sec"],
                    "duration_sec": duration_sec,
                    "filter_status": "rejected",
                    "filter_reasons": reasons,
                }
                self._write_clip_rejection(payload, clip_uid)
                self.clip_repo.upsert(
                    {
                        "clip_uid": clip_uid,
                        "video_uid": video_record["video_uid"],
                        "start_sec": window["start_sec"],
                        "end_sec": window["end_sec"],
                        "duration_sec": duration_sec,
                        "segmentation_mode": window["segmentation_mode"],
                        "filter_status": "rejected",
                        "filter_reasons": reasons,
                        "retention_status": "rejected_deleted",
                        "clip_path": None,
                        "manifest_path": None,
                        "keyframes_manifest_path": None,
                        "subtitle_path": None,
                        "scores": scores,
                        "annotation_status": "rejected",
                    }
                )
                continue

            clip_dir = self.layout["artifact_clips"] / clip_uid
            clip_dir.mkdir(parents=True, exist_ok=True)
            clip_path = clip_dir / "clip.mp4"
            self._copy_or_trim_clip(video_record["raw_video_path"], clip_path, float(window["start_sec"]), duration_sec)
            subtitle_path = self._write_subtitle_artifact(video_record, window, clip_uid)
            keyframes_manifest_path = self._extract_keyframes(clip_path, clip_uid, duration_sec)
            manifest_payload = {
                "clip_uid": clip_uid,
                "video_uid": video_record["video_uid"],
                "start_sec": window["start_sec"],
                "end_sec": window["end_sec"],
                "duration_sec": duration_sec,
                "segmentation_mode": window["segmentation_mode"],
                "filter_status": "accepted",
                "filter_reasons": [],
                "artifact_paths": {
                    "clip_path": str(clip_path),
                    "keyframes_manifest_path": str(keyframes_manifest_path),
                    "subtitle_path": str(subtitle_path),
                },
            }
            manifest_path = self._write_clip_manifest(manifest_payload, clip_uid)
            self.clip_repo.upsert(
                {
                    "clip_uid": clip_uid,
                    "video_uid": video_record["video_uid"],
                    "start_sec": window["start_sec"],
                    "end_sec": window["end_sec"],
                    "duration_sec": duration_sec,
                    "segmentation_mode": window["segmentation_mode"],
                    "filter_status": "accepted",
                    "filter_reasons": [],
                    "retention_status": "accepted_kept",
                    "clip_path": str(clip_path),
                    "manifest_path": str(manifest_path),
                    "keyframes_manifest_path": str(keyframes_manifest_path),
                    "subtitle_path": str(subtitle_path),
                    "scores": scores,
                    "annotation_status": "pending",
                }
            )
            accepted_count += 1

        self._delete_file(video_record.get("raw_video_path"))
        retention_status = "processed_deleted"
        self.video_repo.update_video_cleanup(
            video_record["video_uid"],
            retention_status=retention_status,
            raw_video_path=None,
            accepted_clip_count=accepted_count,
            preprocess_started_at=preprocess_started_at,
            preprocess_finished_at=datetime.now().isoformat(timespec="seconds"),
            preprocess_elapsed_sec=round(time.perf_counter() - start_clock, 3),
        )
        self.logger.info(
            "preprocess_finish video_uid=%s accepted_clips=%s windows=%s",
            video_record["video_uid"],
            accepted_count,
            len(windows),
        )
        return accepted_count

    def process_records(self, videos: list[dict[str, Any]]) -> int:
        if not videos:
            return 0
        accepted = 0
        with ThreadPoolExecutor(max_workers=min(len(videos), int(self.config["preprocessing"].get("workers", 2)))) as executor:
            futures = {executor.submit(self._process_video, video_record): video_record for video_record in videos}
            for future in as_completed(futures):
                video_record = futures[future]
                try:
                    accepted += int(future.result())
                except Exception as exc:
                    self.video_repo.reset_preprocess_claim(video_record["video_uid"])
                    self.logger.exception("preprocess_failed video_uid=%s error=%s", video_record["video_uid"], exc)
        return accepted

    def preprocess(self, limit: int | None = None) -> int:
        videos = self.video_repo.list_downloaded_for_preprocess(limit or int(self.config["preprocessing"]["max_videos_per_run"]))
        return self.process_records(videos)
