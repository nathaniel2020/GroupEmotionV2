from __future__ import annotations

from group_emotion_video.acquisition import build_search_query


def test_build_search_query_prefers_parenthetical_aliases_and_scene_markers() -> None:
    query = {
        "scene_text": "封闭室内大空间（礼堂/体育馆）",
        "trigger_text": "公开正面能力认可（当众表扬/荣誉称号/竞赛获奖/推荐提名）",
        "query_text": "封闭室内大空间（礼堂/体育馆） 公开正面能力认可",
    }
    assert build_search_query(query) == "礼堂 体育馆 当众表扬 荣誉称号 竞赛获奖 推荐提名"


def test_build_search_query_keeps_scene_terms_for_live_suffix() -> None:
    query = {
        "scene_text": "固定座位空间（教室/考场）",
        "trigger_text": "同伴间横向能力比较（排名对比/成绩比较/社会比较效应）",
        "query_text": "固定座位空间（教室/考场） 现场",
    }
    assert build_search_query(query) == "教室 考场 现场"


def test_build_search_query_scene_only_keeps_trigger_constraints() -> None:
    query = {
        "scene_text": "封闭室内大空间（礼堂/体育馆）",
        "trigger_text": "公开正面能力认可（当众表扬/荣誉称号/竞赛获奖/推荐提名）",
        "query_text": "封闭室内大空间（礼堂/体育馆）",
    }
    assert build_search_query(query) == "礼堂 体育馆 当众表扬 荣誉称号 竞赛获奖 推荐提名"
