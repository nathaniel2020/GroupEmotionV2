from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

from group_emotion_video.ids import make_annotation_uid
from group_emotion_video.repositories import AnnotationRepository, ClipRepository, VideoRepository
from group_emotion_video.sqlite_db import SQLiteDB, initialize_schema


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_dataset.py"
MODULE_SPEC = importlib.util.spec_from_file_location("export_dataset_script", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
export_dataset_script = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(export_dataset_script)


def test_progress_bar_falls_back_to_newline_output_for_non_tty() -> None:
    stream = io.StringIO()
    progress = export_dataset_script.ProgressBar(total=100, enabled=True, stream=stream)

    progress.update(0, exported=0, skipped=0)
    progress.update(1, exported=1, skipped=0)
    progress.update(5, exported=5, skipped=0)
    progress.update(6, exported=6, skipped=0)
    progress.finish(current=100, exported=100, skipped=0)

    output = stream.getvalue()
    assert "\r" not in output
    assert "Exporting [--------------------------------] 0/100" in output
    assert "Exporting [#-------------------------------] 5/100" in output
    assert "Exporting [################################] 100/100" in output


def _seed_sample(tmp_path: Path, *, clip_exists: bool, raw_video_exists: bool) -> dict[str, Path | str]:
    runtime_root = tmp_path / "runtime"
    db = SQLiteDB(runtime_root / "index.sqlite")
    initialize_schema(db)

    video_repo = VideoRepository(db)
    clip_repo = ClipRepository(db)
    annotation_repo = AnnotationRepository(db)

    video_uid = "video_test"
    clip_uid = "clip_test"
    annotation_uid = make_annotation_uid(clip_uid)

    canonical_clip_path = runtime_root / "artifacts" / "clips" / clip_uid / "clip.mp4"
    canonical_clip_path.parent.mkdir(parents=True, exist_ok=True)
    if clip_exists:
        canonical_clip_path.write_bytes(b"clip-bytes")
    canonical_annotation_path = runtime_root / "artifacts" / "annotations" / f"{annotation_uid}.json"

    source_video_path = tmp_path / "source_video.mp4"
    if raw_video_exists:
        source_video_path.write_bytes(b"source-video-bytes")

    stale_root = tmp_path / "stale_runtime"
    stale_clip_path = stale_root / "artifacts" / "clips" / clip_uid / "clip.mp4"
    stale_manifest_path = stale_root / "artifacts" / "clips" / clip_uid / "clip_manifest.json"
    stale_annotation_path = stale_root / "artifacts" / "annotations" / f"{annotation_uid}.json"
    stale_video_meta_path = stale_root / "artifacts" / "videos" / video_uid / "video_meta.json"

    video_repo.upsert(
        {
            "video_uid": video_uid,
            "platform": "bilibili",
            "bvid": "BV_TEST_001",
            "url": "https://www.bilibili.com/video/BV_TEST_001",
            "title": "样本标题",
            "uploader": "tester",
            "publish_time": "2026-04-15T00:00:00",
            "duration_sec": 30.0,
            "cover_url": None,
            "description": "样本描述",
            "tags": ["群体", "情绪"],
            "categories": ["教育"],
            "view_count": 10,
            "like_count": 5,
            "comment_count": 1,
            "play_count": 10,
            "danmaku_count": 0,
            "type": "education",
            "webpage_url": "https://www.bilibili.com/video/BV_TEST_001",
            "query_id": "query_test",
            "scene_text": "教室",
            "trigger_text": "当众表扬",
            "source_excel_row": 1,
            "subtitles_available": True,
            "rights_status": "public",
            "download_status": "downloaded",
            "download_started_at": None,
            "download_finished_at": None,
            "download_elapsed_sec": None,
            "reject_reason": None,
            "retention_status": "processed_deleted",
            "accepted_clip_count": 1,
            "preprocess_started_at": None,
            "preprocess_finished_at": None,
            "preprocess_elapsed_sec": None,
            "raw_video_path": str(source_video_path) if raw_video_exists else None,
            "video_meta_path": str(stale_video_meta_path),
            "platform_metadata": {},
            "extra": {},
        }
    )
    clip_repo.upsert(
        {
            "clip_uid": clip_uid,
            "video_uid": video_uid,
            "start_sec": 1.0,
            "end_sec": 6.0,
            "duration_sec": 5.0,
            "segmentation_mode": "subtitle",
            "filter_status": "accepted",
            "filter_reasons": [],
            "retention_status": "accepted_kept",
            "clip_path": str(stale_clip_path),
            "manifest_path": str(stale_manifest_path),
            "keyframes_manifest_path": None,
            "subtitle_path": None,
            "scores": {},
            "annotation_status": "done",
        }
    )
    annotation_repo.upsert(
        {
            "annotation_uid": annotation_uid,
            "clip_uid": clip_uid,
            "status": "done",
            "started_at": "2026-04-15T00:00:00",
            "finished_at": "2026-04-15T00:00:01",
            "elapsed_sec": 1.0,
            "review_required": False,
            "final_annotation": {"group_emotion": "Joy", "scene_description": "全班一起欢呼"},
            "field_confidence": {"overall": 0.95},
            "quality_flags": [],
            "artifact_path": str(stale_annotation_path),
        }
    )

    return {
        "runtime_root": runtime_root,
        "db_path": runtime_root / "index.sqlite",
        "clip_uid": clip_uid,
        "annotation_uid": annotation_uid,
        "canonical_clip_path": canonical_clip_path,
        "canonical_annotation_path": canonical_annotation_path,
        "source_video_path": source_video_path,
    }


def test_export_dataset_reconstructs_missing_sidecar_files_from_db(tmp_path: Path) -> None:
    sample = _seed_sample(tmp_path, clip_exists=True, raw_video_exists=False)

    result = export_dataset_script.export_dataset(
        db_path=sample["db_path"],
        runtime_root=sample["runtime_root"],
        output_root=Path(sample["runtime_root"]) / "exports",
        annotation_domains_path=None,
        limit=None,
        min_confidence=None,
        caps={},
        skip_missing=False,
        progress_enabled=False,
    )

    export_dir = Path(result["export_dir"])
    clip_uid = str(sample["clip_uid"])

    assert result["exported_samples"] == 1
    assert (export_dir / "clips" / f"{clip_uid}.mp4").read_bytes() == b"clip-bytes"

    raw_annotation = json.loads((export_dir / "raw_annotations" / f"{clip_uid}.json").read_text(encoding="utf-8"))
    assert raw_annotation["annotation_uid"] == sample["annotation_uid"]
    assert raw_annotation["final_annotation"]["group_emotion"] == "Joy"
    assert raw_annotation["field_confidence"]["overall"] == 0.95

    exported_annotation = json.loads((export_dir / "annotations" / f"{clip_uid}.json").read_text(encoding="utf-8"))
    assert exported_annotation["final_annotation"] == {
        "group_emotion": "Joy",
        "scene_description": "全班一起欢呼",
    }
    assert exported_annotation["video_meta"]["title"] == "样本标题"
    assert exported_annotation["clip_info"]["start_sec"] == 1.0
    assert exported_annotation["clip_info"]["duration_sec"] == 5.0


def test_export_dataset_rebuilds_missing_clip_from_source_video(tmp_path: Path, monkeypatch) -> None:
    sample = _seed_sample(tmp_path, clip_exists=False, raw_video_exists=True)

    def fake_ffmpeg(command: list[str], check: bool, stdout, stderr):
        Path(command[-1]).write_bytes(b"rebuilt-clip")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(export_dataset_script.subprocess, "run", fake_ffmpeg)

    result = export_dataset_script.export_dataset(
        db_path=sample["db_path"],
        runtime_root=sample["runtime_root"],
        output_root=Path(sample["runtime_root"]) / "exports",
        annotation_domains_path=None,
        limit=None,
        min_confidence=None,
        caps={},
        skip_missing=False,
        progress_enabled=False,
    )

    export_dir = Path(result["export_dir"])
    clip_uid = str(sample["clip_uid"])

    assert result["exported_samples"] == 1
    assert (export_dir / "clips" / f"{clip_uid}.mp4").read_bytes() == b"rebuilt-clip"


def _write_test_source_video(path: Path, *, duration_sec: int = 7) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=25",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            str(duration_sec),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_export_dataset_cli_exports_after_clip_and_annotation_deleted(tmp_path: Path) -> None:
    sample = _seed_sample(tmp_path, clip_exists=True, raw_video_exists=True)
    canonical_clip_path = Path(sample["canonical_clip_path"])
    canonical_annotation_path = Path(sample["canonical_annotation_path"])
    source_video_path = Path(sample["source_video_path"])
    runtime_root = Path(sample["runtime_root"])

    _write_test_source_video(source_video_path)
    canonical_annotation_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_annotation_path.write_text(json.dumps({"deleted": True}), encoding="utf-8")
    canonical_clip_path.unlink()
    canonical_annotation_path.unlink()

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--runtime-root",
            str(runtime_root),
            "--output-root",
            str(runtime_root / "exports"),
            "--no-progress",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    export_dir = Path(result["export_dir"])
    clip_uid = str(sample["clip_uid"])

    assert result["exported_samples"] == 1
    assert (export_dir / "clips" / f"{clip_uid}.mp4").exists()
    assert (export_dir / "clips" / f"{clip_uid}.mp4").stat().st_size > 0

    raw_annotation = json.loads((export_dir / "raw_annotations" / f"{clip_uid}.json").read_text(encoding="utf-8"))
    assert raw_annotation["annotation_uid"] == sample["annotation_uid"]
    assert raw_annotation["final_annotation"]["group_emotion"] == "Joy"

    exported_annotation = json.loads((export_dir / "annotations" / f"{clip_uid}.json").read_text(encoding="utf-8"))
    assert exported_annotation["clip_info"]["clip_file"] == f"clips/{clip_uid}.mp4"


def test_export_dataset_uses_runtime_and_annotation_domains_from_config(tmp_path: Path, capsys) -> None:
    sample = _seed_sample(tmp_path, clip_exists=True, raw_video_exists=False)
    runtime_root = Path(sample["runtime_root"])
    annotation_domains_path = tmp_path / "reference" / "annotation_domains.json"
    annotation_domains_path.parent.mkdir(parents=True, exist_ok=True)
    annotation_domains_path.write_text(
        json.dumps({"fields": {"scene_description": {}, "group_emotion": {}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    config_path = tmp_path / "export_profile.yaml"
    config_path.write_text(
        "\n".join(
            [
                "project:",
                f"  runtime_dir: {runtime_root}",
                f"  annotation_domains_path: {annotation_domains_path}",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = export_dataset_script.main(
        [
            "--config",
            str(config_path),
            "--no-progress",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    export_dir = Path(payload["export_dir"])
    clip_uid = str(sample["clip_uid"])

    assert exit_code == 0
    assert payload["db_path"] == str(runtime_root / "index.sqlite")
    assert payload["exported_samples"] == 1

    exported_annotation = json.loads((export_dir / "annotations" / f"{clip_uid}.json").read_text(encoding="utf-8"))
    assert list(exported_annotation["final_annotation"]) == ["scene_description", "group_emotion"]
