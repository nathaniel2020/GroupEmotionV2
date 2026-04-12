from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

from group_emotion_video.reference import build_query_seed_catalog, prepare_reference_files


def _write_seed_excel(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "情感因素"
    sheet.append(["情境场景", "触发事件"])
    sheet.append(["外部诱因空间 (X_ext)", ""])
    sheet.append(["【物理空间属性】", "【能力与成就事件】"])
    sheet.append(["封闭室内小空间（宿舍/办公室）", "正式量化能力评定结果公布（考试成绩/GPA排名/学分绩点）"])
    workbook.save(path)


def test_prepare_reference_files_generates_stable_catalog_and_domains(tmp_path: Path) -> None:
    excel_path = tmp_path / "seed.xlsx"
    schema_path = tmp_path / "schema.json"
    label_seed_path = tmp_path / "labels.json"
    _write_seed_excel(excel_path)

    schema_path.write_text(
        json.dumps(
            [
                {"字段名": "group_size_estimate", "数据类型": "enum", "值域": "small(3-10) | medium(11-30)", "必填": "是", "说明": "规模"},
                {"字段名": "trigger_event_type", "数据类型": "enum", "值域": "speech_act | none", "必填": "是", "说明": "触发"},
                {"字段名": "trigger_event_description", "数据类型": "string", "值域": "自由文本", "必填": "type≠none 时必填", "说明": "触发描述"},
                {"字段名": "group_emotion", "数据类型": "enum", "值域": "联合标签池", "必填": "是", "说明": "情绪"},
                {"字段名": "valence", "数据类型": "integer", "值域": "1–9（SAM 9点量表）", "必填": "是", "说明": "效价"},
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    label_seed_path.write_text(json.dumps({"labels": ["Joy", "Anxiety"]}, ensure_ascii=False), encoding="utf-8")

    first = build_query_seed_catalog(excel_path=excel_path, sheet_name="情感因素")
    second = build_query_seed_catalog(excel_path=excel_path, sheet_name="情感因素")
    assert first["rows"] == second["rows"]
    assert len(first["rows"]) == 1
    assert len(first["rows"][0]["query_texts"]) == 3

    outputs = prepare_reference_files(
        excel_path=excel_path,
        sheet_name="情感因素",
        schema_source_path=schema_path,
        label_seed_path=label_seed_path,
        query_seed_catalog_path=tmp_path / "query_seed_catalog.json",
        annotation_domains_path=tmp_path / "annotation_domains.json",
    )
    assert outputs["query_seed_catalog_path"].exists()
    assert outputs["annotation_domains_path"].exists()

    domains = json.loads(outputs["annotation_domains_path"].read_text(encoding="utf-8"))
    assert domains["fields"]["group_emotion"]["allowed_values"] == ["Joy", "Anxiety"]
    assert domains["fields"]["valence"]["min_value"] == 1
    assert domains["fields"]["trigger_event_description"]["required_when"] == "trigger_event_type!=none"
