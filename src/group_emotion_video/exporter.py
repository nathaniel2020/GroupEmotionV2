from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


VIDEO_META_FIELDS = [
    "video_uid",
    "platform",
    "bvid",
    "url",
    "webpage_url",
    "title",
    "uploader",
    "publish_time",
    "duration_sec",
    "cover_url",
    "description",
    "tags",
    "view_count",
    "like_count",
    "comment_count",
    "play_count",
    "danmaku_count",
    "type",
    "subtitles_available",
    "query_id",
    "scene_text",
    "trigger_text",
    "source_excel_row",
]

VIDEO_META_DEFAULTS = {
    "tags": [],
    "subtitles_available": False,
}

VIDEO_META_DESCRIPTIONS = {
    "video_uid": "项目内视频唯一标识。",
    "platform": "视频来源平台。",
    "bvid": "平台视频 ID；本地导入视频会写入本地生成的 ID。",
    "url": "原始视频访问地址。",
    "webpage_url": "视频网页地址。",
    "title": "视频标题。",
    "uploader": "发布者或上传者。",
    "publish_time": "原始发布时间。",
    "duration_sec": "原始整视频时长，单位秒。",
    "cover_url": "封面图地址。",
    "description": "原始视频简介。",
    "tags": "原始标签列表。",
    "view_count": "播放/观看计数。",
    "like_count": "点赞计数。",
    "comment_count": "评论计数。",
    "play_count": "平台侧播放计数。",
    "danmaku_count": "弹幕计数。",
    "type": "平台侧内容类型。",
    "subtitles_available": "是否存在字幕或可用文本证据。",
    "query_id": "命中该视频的 query 唯一标识。",
    "scene_text": "该 query 对应的场景短语。",
    "trigger_text": "该 query 对应的触发事件短语。",
    "source_excel_row": "原始种子 Excel 行号，用于溯源。",
}

CLIP_INFO_FIELDS = [
    "clip_uid",
    "video_uid",
    "start_sec",
    "end_sec",
    "duration_sec",
    "segmentation_mode",
    "clip_file",
]

CLIP_INFO_DESCRIPTIONS = {
    "clip_uid": "项目内切片唯一标识。",
    "video_uid": "该切片所属整视频 ID。",
    "start_sec": "切片起始时间，单位秒。",
    "end_sec": "切片结束时间，单位秒。",
    "duration_sec": "切片时长，单位秒。",
    "segmentation_mode": "切片生成方式，如 subtitle / fixed_window。",
    "clip_file": "切片视频在导出目录中的相对路径。",
}

TOP_LEVEL_SECTION_DESCRIPTIONS = {
    "final_annotation": "最终通过校验的扁平化标注结果，仅保留面向数据消费的最终字段。",
    "video_meta": "与当前切片对应的整视频元信息，保留数据溯源字段。",
    "clip_info": "当前切片的时间范围、切分方式和导出后视频路径。",
}


class ExportService:
    def __init__(self, *, layout: dict[str, Path], logger: Any):
        self.layout = layout
        self.logger = logger

    def export(self, rows: list[dict[str, Any]], annotation_domains_path: str | Path) -> Path:
        now = datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        export_dir = self.layout["exports"] / f"dataset_export_{stamp}"
        clips_dir = export_dir / "clips"
        annotations_dir = export_dir / "annotations"
        raw_annotations_dir = export_dir / "raw_annotations"
        clips_dir.mkdir(parents=True, exist_ok=True)
        annotations_dir.mkdir(parents=True, exist_ok=True)
        raw_annotations_dir.mkdir(parents=True, exist_ok=True)

        domains = self._load_json(annotation_domains_path)
        field_specs = domains.get("fields", {})

        for row in rows:
            clip_uid = str(row["clip_uid"])
            clip_path = Path(row["clip_path"])
            annotation_payload = self._load_json(row["artifact_path"])
            video_meta_payload = self._load_json(row["video_meta_path"])
            clip_manifest_payload = self._load_json(row["manifest_path"])

            target_clip_path = clips_dir / f"{clip_uid}{clip_path.suffix or '.mp4'}"
            shutil.copy2(clip_path, target_clip_path)
            shutil.copy2(row["artifact_path"], raw_annotations_dir / f"{clip_uid}.json")

            exported_annotation = {
                "final_annotation": self._build_final_annotation(annotation_payload, field_specs),
                "video_meta": self._build_video_meta(video_meta_payload),
                "clip_info": self._build_clip_info(
                    clip_manifest_payload,
                    clip_file=str(target_clip_path.relative_to(export_dir)),
                ),
            }
            (annotations_dir / f"{clip_uid}.json").write_text(
                json.dumps(exported_annotation, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        readme_text = self._build_readme(
            export_dir_name=export_dir.name,
            created_at=now,
            sample_count=len(rows),
            field_specs=field_specs,
        )
        (export_dir / "README.md").write_text(readme_text, encoding="utf-8")
        self.logger.info("export_finish export_dir=%s sample_count=%s", export_dir, len(rows))
        return export_dir

    @staticmethod
    def _load_json(path: str | Path) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def _build_final_annotation(self, annotation_payload: dict[str, Any], field_specs: dict[str, Any]) -> dict[str, Any]:
        raw_annotation = annotation_payload.get("final_annotation", {})
        if not isinstance(raw_annotation, dict):
            raw_annotation = {}
        ordered: dict[str, Any] = {}
        for field_name in field_specs:
            if field_name in raw_annotation:
                ordered[field_name] = raw_annotation[field_name]
        for field_name, value in raw_annotation.items():
            if field_name not in ordered:
                ordered[field_name] = value
        return ordered

    def _build_video_meta(self, video_meta_payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name in VIDEO_META_FIELDS:
            if field_name in video_meta_payload:
                result[field_name] = video_meta_payload[field_name]
                continue
            default_value = VIDEO_META_DEFAULTS.get(field_name)
            if isinstance(default_value, list):
                result[field_name] = list(default_value)
            else:
                result[field_name] = default_value
        return result

    @staticmethod
    def _build_clip_info(clip_manifest_payload: dict[str, Any], *, clip_file: str) -> dict[str, Any]:
        return {
            "clip_uid": clip_manifest_payload.get("clip_uid"),
            "video_uid": clip_manifest_payload.get("video_uid"),
            "start_sec": clip_manifest_payload.get("start_sec"),
            "end_sec": clip_manifest_payload.get("end_sec"),
            "duration_sec": clip_manifest_payload.get("duration_sec"),
            "segmentation_mode": clip_manifest_payload.get("segmentation_mode"),
            "clip_file": clip_file,
        }

    def _build_readme(
        self,
        *,
        export_dir_name: str,
        created_at: datetime,
        sample_count: int,
        field_specs: dict[str, Any],
    ) -> str:
        lines = [
            "# 群体情感视频数据集导出说明",
            "",
            "## 数据集介绍",
            f"- 导出时间：`{created_at.isoformat(timespec='seconds')}`",
            f"- 样本数量：`{sample_count}`",
            "- 样本范围：仅包含最终状态为 `done` 的切片样本。",
            "- 每个样本对应一个切片视频文件和一个同名 JSON 标注文件。",
            "- 标注 JSON 仅保留 `final_annotation`、`video_meta`、`clip_info` 三部分，不包含 runtime 内部字段，如 `agent_outputs`、`field_confidence`、`quality_flags`、运行时间戳等。",
            "- `final_annotation` 使用扁平 dotted-key 结构；`video_meta` 保留 `query_id`、`scene_text`、`trigger_text`、`source_excel_row` 等溯源字段。",
            "",
            "## 文件结构",
            "```text",
            f"{export_dir_name}/",
            "  README.md",
            "  clips/",
            "    <clip_uid>.mp4",
            "  annotations/",
            "    <clip_uid>.json",
            "```",
            "- `clips/`：导出的 CLIP 切片视频。",
            "- `annotations/`：与切片同名的标注 JSON 文件。",
            "- `README.md`：当前导出目录的说明文档。",
            "",
            "## 标注文件顶层结构",
            "| 字段 | 类型 | 说明 |",
            "|---|---|---|",
        ]
        for field_name in ("final_annotation", "video_meta", "clip_info"):
            lines.append(
                f"| `{field_name}` | `object` | {self._escape_md(TOP_LEVEL_SECTION_DESCRIPTIONS[field_name])} |"
            )

        lines.extend(
            [
                "",
                "## final_annotation 字段说明",
                "| 字段 | 类型 | 必填 | 约束/值域 | 说明 |",
                "|---|---|---|---|---|",
            ]
        )
        for field_name, field_spec in field_specs.items():
            lines.append(
                "| "
                f"`{field_name}` | "
                f"`{self._field_type(field_spec)}` | "
                f"{self._required_text(field_spec)} | "
                f"{self._escape_md(self._constraint_text(field_spec))} | "
                f"{self._escape_md(str(field_spec.get('description') or '-'))} |"
            )

        lines.extend(
            [
                "",
                "## video_meta 字段说明",
                "| 字段 | 类型 | 说明 |",
                "|---|---|---|",
            ]
        )
        for field_name in VIDEO_META_FIELDS:
            lines.append(
                f"| `{field_name}` | `mixed` | {self._escape_md(VIDEO_META_DESCRIPTIONS[field_name])} |"
            )

        lines.extend(
            [
                "",
                "## clip_info 字段说明",
                "| 字段 | 类型 | 说明 |",
                "|---|---|---|",
            ]
        )
        for field_name in CLIP_INFO_FIELDS:
            lines.append(
                f"| `{field_name}` | `mixed` | {self._escape_md(CLIP_INFO_DESCRIPTIONS[field_name])} |"
            )

        lines.extend(
            [
                "",
                "## 使用说明",
                "- `annotations/<clip_uid>.json` 与 `clips/<clip_uid>.mp4` 通过相同的 `clip_uid` 一一对应。",
                "- `clip_info.clip_file` 保存了切片视频在当前导出目录中的相对路径。",
                "- 如果需要回溯来源 query，可使用 `video_meta.query_id`、`video_meta.scene_text`、`video_meta.trigger_text` 与 `video_meta.source_excel_row`。",
            ]
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _field_type(field_spec: dict[str, Any]) -> str:
        return str(field_spec.get("type") or "unknown")

    @staticmethod
    def _required_text(field_spec: dict[str, Any]) -> str:
        required_when = field_spec.get("required_when")
        if required_when == "trigger_event_type!=none":
            return "是（当 `trigger_event_type != none`）"
        return "是" if field_spec.get("required") else "否"

    @staticmethod
    def _constraint_text(field_spec: dict[str, Any]) -> str:
        allowed_values = field_spec.get("allowed_values") or []
        if allowed_values:
            return ", ".join(f"`{value}`" for value in allowed_values)
        field_type = str(field_spec.get("type") or "")
        if field_type == "integer":
            min_value = field_spec.get("min_value")
            max_value = field_spec.get("max_value")
            if min_value is not None or max_value is not None:
                return f"{min_value}..{max_value}"
            return "整数"
        if field_type == "ordinal":
            return "有序标注值（当前 schema 未冻结固定枚举）"
        if field_type == "string":
            return "自由文本"
        return "-"

    @staticmethod
    def _escape_md(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", "<br>")
