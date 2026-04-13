from __future__ import annotations

from pathlib import Path

import group_emotion_video.acquisition as acquisition_module
from group_emotion_video.acquisition import BilibiliAdapter, build_search_query
from group_emotion_video.types import CandidateVideo
from yt_dlp.cookies import YoutubeDLCookieJar


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


def _adapter_config() -> dict:
    return {
        "sources": {
            "bilibili": {
                "max_pages_per_query": 1,
                "search_timeout_sec": 10,
                "enrich_timeout_sec": 10,
            }
        },
        "crawl": {
            "retry": {
                "max_attempts": 3,
                "base_sleep_sec": 0.01,
                "max_sleep_sec": 0.01,
                "backoff_factor": 1.0,
                "jitter_sec": 0.0,
            },
            "download": {
                "retries": 1,
                "fragment_retries": 1,
                "socket_timeout_sec": 10,
            },
        },
    }


class _RetryingExtractAdapter(BilibiliAdapter):
    def __init__(self, config: dict, failures: list[Exception]):
        super().__init__(config)
        self.failures = list(failures)
        self.calls = 0

    def _run_yt_dlp_extract_info(self, url: str, *, download: bool, output_dir: str | None = None, progress_label: str | None = None) -> dict:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        if download:
            output_path = Path(output_dir or ".") / "BV1TEST12345.mp4"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"fake-video")
            return {
                "id": "BV1TEST12345",
                "ext": "mp4",
                "requested_downloads": [{"filepath": str(output_path)}],
            }
        return {
            "id": "BV1TEST12345",
            "title": "课堂欢呼",
            "duration": 36,
            "webpage_url": url,
            "tags": ["课堂"],
        }


def _candidate() -> CandidateVideo:
    return CandidateVideo(
        platform="bilibili",
        platform_video_id="BV1TEST12345",
        url="https://www.bilibili.com/video/BV1TEST12345",
        title="课堂欢呼",
        duration_sec=36.0,
    )


def test_bilibili_adapter_download_retries_after_412(tmp_path: Path, monkeypatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(acquisition_module.time, "sleep", lambda sec: sleep_calls.append(sec))
    adapter = _RetryingExtractAdapter(
        _adapter_config(),
        failures=[Exception("ERROR: [BiliBili] 116389872472343: Unable to download webpage: HTTP Error 412: Precondition Failed")],
    )

    result = adapter.download(_candidate(), str(tmp_path))

    assert result.status == "downloaded"
    assert adapter.calls == 2
    assert sleep_calls == [0.01]


def test_bilibili_adapter_enrich_retries_after_timeout(monkeypatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(acquisition_module.time, "sleep", lambda sec: sleep_calls.append(sec))
    adapter = _RetryingExtractAdapter(
        _adapter_config(),
        failures=[Exception("The read operation timed out")],
    )

    enriched = adapter.enrich(_candidate())

    assert enriched.title == "课堂欢呼"
    assert adapter.calls == 2
    assert sleep_calls == [0.01]


def test_bilibili_adapter_http_headers_include_cookie_from_env(monkeypatch) -> None:
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=test; bili_jct=test")
    adapter = BilibiliAdapter(_adapter_config())

    headers = adapter._http_headers(user_agent="UA_TEST")

    assert headers["User-Agent"] == "UA_TEST"
    assert headers["Referer"] == "https://www.bilibili.com/"
    assert "Cookie" not in headers


def test_bilibili_adapter_cookie_file_uses_netscape_format(monkeypatch) -> None:
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=test; bili_jct=test")
    adapter = BilibiliAdapter(_adapter_config())

    cookie_file = adapter._ensure_cookie_file()

    assert cookie_file is not None
    content = Path(cookie_file).read_text(encoding="utf-8")
    assert "# Netscape HTTP Cookie File" in content
    assert "# This file is generated by yt-dlp.  Do not edit." in content
    jar = YoutubeDLCookieJar(cookie_file)
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = {(cookie.name, cookie.value) for cookie in jar}
    assert ("SESSDATA", "test") in cookies
    assert ("bili_jct", "test") in cookies
    adapter._cleanup_cookie_file()


def test_bilibili_adapter_uses_cookies_from_browser_when_configured() -> None:
    config = _adapter_config()
    config["sources"]["bilibili"]["cookies_from_browser"] = "chrome"
    adapter = BilibiliAdapter(config)

    options = adapter._yt_dlp_options(url="https://www.bilibili.com/video/BV1TEST12345")

    assert options["cookiesfrombrowser"] == ("chrome", None, None, None)
    assert "cookiefile" not in options


def test_bilibili_adapter_parses_cookies_from_browser_profile_spec() -> None:
    config = _adapter_config()
    config["sources"]["bilibili"]["cookies_from_browser"] = "chrome:~/.config/google-chrome"
    adapter = BilibiliAdapter(config)

    assert adapter._cookies_from_browser_spec() == ("chrome", "~/.config/google-chrome", None, None)


def test_bilibili_adapter_uses_configured_cookie_file_before_env_cookie(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=env_cookie")
    config = _adapter_config()
    cookie_source = tmp_path / "bilibili_cookies.txt"
    cookie_source.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                "# This file is generated by yt-dlp.  Do not edit.",
                "",
                ".google.com\tTRUE\t/\tTRUE\t13412954220180216\tNID\tgoogle_cookie",
                ".bilibili.com\tTRUE\t/\tTRUE\t13422374117938388\tSESSDATA\tfile_cookie",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config["sources"]["bilibili"]["cookie_file"] = str(cookie_source)
    adapter = BilibiliAdapter(config)

    options = adapter._yt_dlp_options(url="https://www.bilibili.com/video/BV1TEST12345")

    assert options["cookiefile"] == str(cookie_source)
    assert "cookiesfrombrowser" not in options


def test_bilibili_adapter_sanitizes_malformed_configured_cookie_file(tmp_path: Path) -> None:
    cookie_source = tmp_path / "bilibili_cookies.txt"
    cookie_source.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                "# This file is generated by yt-dlp.  Do not edit.",
                "",
                ".bilibili.com\tTRUE\t/\tFALSE\t13452051255000000\tbrowser_re",
                ".bilibili.com\tTRUE\t/\tTRUE\t13422374117938388\tSESSDATA\tfile_cookie",
                "www.bilibili.com\tFALSE\t/\tFALSE\t0\tbmg_af_switch\t1",
                ".google.com\tTRUE\t/\tTRUE\t13412954220180216\tNID\tgoogle_cookie",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config = _adapter_config()
    config["sources"]["bilibili"]["cookie_file"] = str(cookie_source)
    adapter = BilibiliAdapter(config)

    cookies = {(cookie.domain, cookie.name, cookie.value) for cookie in adapter._configured_cookie_entries()}

    assert (".bilibili.com", "SESSDATA", "file_cookie") in cookies
    assert ("www.bilibili.com", "bmg_af_switch", "1") in cookies
    assert all(cookie[1] != "browser_re" for cookie in cookies)
    assert all(cookie[1] != "NID" for cookie in cookies)


def test_bilibili_adapter_uses_cookie_file_from_env(tmp_path: Path, monkeypatch) -> None:
    cookie_source = tmp_path / "bilibili_cookies.txt"
    cookie_source.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                "# This file is generated by yt-dlp.  Do not edit.",
                "",
                ".bilibili.com\tTRUE\t/\tTRUE\t13422374117938388\tSESSDATA\tfile_cookie",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BILIBILI_COOKIE_FILE", str(cookie_source))
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=env_cookie")
    adapter = BilibiliAdapter(_adapter_config())

    options = adapter._yt_dlp_options(url="https://www.bilibili.com/video/BV1TEST12345")

    assert options["cookiefile"] == str(cookie_source)
    assert "cookiesfrombrowser" not in options


def test_bilibili_adapter_allows_cookie_free_download() -> None:
    config = _adapter_config()
    config["sources"]["bilibili"]["cookie_env"] = ""
    config["sources"]["bilibili"]["cookie_file_env"] = ""
    adapter = BilibiliAdapter(config)

    options = adapter._yt_dlp_options(url="https://www.bilibili.com/video/BV1TEST12345")

    assert "cookiefile" not in options
    assert "cookiesfrombrowser" not in options


def test_bilibili_adapter_applies_configured_cookie_file_to_session(tmp_path: Path) -> None:
    cookie_source = tmp_path / "bilibili_cookies.txt"
    cookie_source.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                "# This file is generated by yt-dlp.  Do not edit.",
                "",
                ".bilibili.com\tTRUE\t/\tTRUE\t13422374117938388\tSESSDATA\tfile_cookie",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config = _adapter_config()
    config["sources"]["bilibili"]["cookie_file"] = str(cookie_source)
    adapter = BilibiliAdapter(config)
    adapter._requests = object()

    class _CookieSink:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []

        def set(self, name: str, value: str, **kwargs) -> None:
            self.calls.append((name, value, kwargs))

    class _Session:
        def __init__(self) -> None:
            self.cookies = _CookieSink()

    session = _Session()
    adapter._apply_session_cookies(session)

    assert session.cookies.calls == [
        ("SESSDATA", "file_cookie", {"domain": ".bilibili.com", "path": "/", "secure": True})
    ]
