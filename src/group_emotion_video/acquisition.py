from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.cookiejar import Cookie
from http.cookies import SimpleCookie
from pathlib import Path
import tempfile
from typing import Any

from .hashing import file_sha256
from .ids import make_query_id, make_video_uid
from .types import CandidateVideo, DownloadResult


_QUERY_SPLIT_PATTERN = re.compile(r"[、,，/／|｜]+")
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_BILIBILI_BVID_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)
_RETRYABLE_ERROR_PATTERNS = (
    "http error 412",
    "precondition failed",
    "http error 429",
    "too many requests",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "connection reset",
    "remote end closed connection",
    "429 too many requests",
)
_FORMAT_NOT_FOUND_ERROR_PATTERNS = (
    "no video formats found",
    "requested format is not available",
)
_COOKIES_FROM_BROWSER_PATTERN = re.compile(
    r"""(?x)
    (?P<name>[^+:]+)
    (?:\s*\+\s*(?P<keyring>[^:]+))?
    (?:\s*:\s*(?!:)(?P<profile>.+?))?
    (?:\s*::\s*(?P<container>.+))?
    """
)
_BILIBILI_COOKIE_DOMAINS = ("bilibili.com", "bilibili.cn")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_PATTERN.sub("", text)


class _YtDlpLogger:
    def __init__(self, logger: Any | None, *, label: str):
        self.logger = logger
        self.label = label
        self.last_error: str | None = None

    def debug(self, message: Any) -> None:
        self._log(logging.DEBUG, message)

    def warning(self, message: Any) -> None:
        self._log(logging.WARNING, message)

    def error(self, message: Any) -> None:
        cleaned = _strip_ansi(str(message or "").strip())
        if cleaned:
            self.last_error = cleaned
        # yt-dlp will raise after calling logger.error. Avoid duplicating the same
        # raw extractor message on stderr; the adapter wraps it into a structured
        # error string later.
        self._log(logging.DEBUG, cleaned)

    def _log(self, level: int, message: Any) -> None:
        if not self.logger:
            return
        cleaned = _strip_ansi(str(message or "").strip())
        if not cleaned:
            return
        self.logger.log(level, "yt_dlp target=%s message=%s", self.label, cleaned)


def _normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url.lstrip('/')}"


def _canonical_bilibili_video_id(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _BILIBILI_BVID_PATTERN.search(text)
    if not match:
        return text
    return match.group(1)


def _safe_dir_name(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text).strip("._") or "scene"


def _dedupe_terms(terms: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = re.sub(r"\s+", " ", term).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def _extract_search_terms(text: str) -> list[str]:
    paren_terms: list[str] = []
    for content in re.findall(r"[（(]([^()（）]+)[）)]", text):
        paren_terms.extend(part.strip() for part in _QUERY_SPLIT_PATTERN.split(content) if part.strip())
    if paren_terms:
        return _dedupe_terms(paren_terms)
    cleaned = re.sub(r"[（(][^()（）]+[）)]", " ", text)
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", cleaned)
    return _dedupe_terms(cleaned.split())


def build_search_query(query_record: dict[str, Any]) -> str:
    scene_text = str(query_record.get("scene_text") or "").strip()
    trigger_text = str(query_record.get("trigger_text") or "").strip()
    query_text = str(query_record.get("query_text") or "").strip()
    scene_terms = _extract_search_terms(scene_text)
    trigger_terms = _extract_search_terms(trigger_text)
    if query_text == scene_text:
        # Pure scene queries are too broad for live retrieval. If the seed has a
        # trigger, keep the scene anchor but carry trigger terms into search.
        terms = [*scene_terms, *trigger_terms] if trigger_terms else (scene_terms or _extract_search_terms(query_text))
    elif query_text.endswith("现场"):
        terms = [*(scene_terms or _extract_search_terms(query_text.replace("现场", " ").strip())), "现场"]
    elif scene_text and trigger_text and query_text.startswith(scene_text):
        terms = [*scene_terms, *trigger_terms] if scene_terms or trigger_terms else _extract_search_terms(query_text)
    else:
        terms = _extract_search_terms(query_text)
    return " ".join(_dedupe_terms(terms)) or query_text


def _parse_duration_to_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if ":" not in text:
        try:
            return float(text)
        except ValueError:
            return None
    total = 0
    for part in text.split(":"):
        total = total * 60 + int(part)
    return float(total)


class BilibiliAdapter:
    SEARCH_URLS = [
        "https://api.bilibili.com/x/web-interface/search/all/v2",
        "https://api.bilibili.com/x/web-interface/wbi/search/all/v2",
    ]

    def __init__(self, config: dict[str, Any], *, logger: Any | None = None):
        self.config = config
        self.source_cfg = config["sources"]["bilibili"]
        self.logger = logger
        self._requests = None
        self._session = None
        self._cookie_file_path: str | None = None
        self._cookie_file_lock = threading.Lock()
        self._warned_cookieless_download = False
        self._user_agents = [
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]

    def _build_progress_hook(self, video_id: str):
        last_bucket = {"value": -1}

        def hook(payload: dict[str, Any]) -> None:
            if not self.logger:
                return
            status = str(payload.get("status") or "")
            if status == "finished":
                if last_bucket["value"] >= 10:
                    return
                last_bucket["value"] = 10
                self.logger.info("download_progress bvid=%s percent=100", video_id)
                return
            if status != "downloading":
                return
            downloaded_bytes = payload.get("downloaded_bytes") or 0
            total_bytes = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
            if not total_bytes:
                return
            percent = int((float(downloaded_bytes) / float(total_bytes)) * 100)
            bucket = min(10, percent // 10)
            if bucket <= last_bucket["value"]:
                return
            last_bucket["value"] = bucket
            self.logger.info("download_progress bvid=%s percent=%s", video_id, bucket * 10)

        return hook

    def _ensure_requests(self):
        if self._session is not None:
            return self._session
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("requests package is required for live Bilibili access.") from exc
        self._requests = requests
        self._session = requests.Session()
        self._apply_session_cookies(self._session)
        return self._session

    def _cookie_value(self) -> str | None:
        inline_cookie = str(self.source_cfg.get("cookie") or "").strip()
        if inline_cookie:
            return inline_cookie
        raw_env_name = self.source_cfg.get("cookie_env")
        if raw_env_name is None:
            env_name = "BILIBILI_COOKIE"
        else:
            env_name = str(raw_env_name).strip()
        if not env_name:
            return None
        env_cookie = str(os.getenv(env_name) or "").strip()
        return env_cookie or None

    def _configured_cookie_file_source(self) -> str | None:
        raw_path = str(self.source_cfg.get("cookie_file") or "").strip()
        if raw_path:
            return os.path.expanduser(raw_path)
        raw_env_name = self.source_cfg.get("cookie_file_env")
        if raw_env_name is None:
            env_name = "BILIBILI_COOKIE_FILE"
        else:
            env_name = str(raw_env_name).strip()
        if not env_name:
            return None
        env_path = str(os.getenv(env_name) or "").strip()
        return os.path.expanduser(env_path) if env_path else None

    @staticmethod
    def _is_bilibili_cookie_domain(domain: str | None) -> bool:
        normalized = str(domain or "").strip().lower().lstrip(".")
        return any(normalized == root or normalized.endswith(f".{root}") for root in _BILIBILI_COOKIE_DOMAINS)

    def _cookie_pairs(self) -> list[tuple[str, str]]:
        cookie_value = self._cookie_value()
        if not cookie_value:
            return []
        jar = SimpleCookie()
        jar.load(cookie_value)
        pairs = [(key, morsel.value) for key, morsel in jar.items()]
        if pairs:
            return pairs
        output: list[tuple[str, str]] = []
        for chunk in cookie_value.split(";"):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key:
                output.append((key, value))
        return output

    def _cookies_from_browser_spec(self) -> tuple[str, str | None, str | None, str | None] | None:
        raw_spec = str(self.source_cfg.get("cookies_from_browser") or "").strip()
        if not raw_spec:
            return None
        match = _COOKIES_FROM_BROWSER_PATTERN.fullmatch(raw_spec)
        if match is None:
            raise ValueError(f"Invalid cookies_from_browser spec: {raw_spec}")
        browser_name, keyring, profile, container = match.group("name", "keyring", "profile", "container")
        return (
            browser_name.lower(),
            profile,
            keyring.upper() if keyring else None,
            container,
        )

    def _apply_session_cookies(self, session: Any) -> None:
        if not self._requests:
            return
        configured_cookies = self._configured_cookie_entries()
        if configured_cookies:
            for cookie in configured_cookies:
                session.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path, secure=cookie.secure)
            return
        for key, value in self._cookie_pairs():
            session.cookies.set(key, value, domain=".bilibili.com", path="/", secure=True)

    @staticmethod
    def _netscape_cookie(
        name: str,
        value: str,
        *,
        domain: str = ".bilibili.com",
        path: str = "/",
        secure: bool = True,
        expires: int | None = None,
    ) -> Cookie:
        return Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=bool(domain),
            domain_initial_dot=domain.startswith("."),
            path=path or "/",
            path_specified=True,
            secure=secure,
            expires=expires,
            discard=expires in (None, 0),
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )

    def _log_cookie_skip(self, *, source: str, lineno: int, reason: str, line: str) -> None:
        if not self.logger:
            return
        preview = line.strip()
        if len(preview) > 120:
            preview = f"{preview[:117]}..."
        self.logger.warning(
            "cookie_file_skip path=%s line=%s reason=%s content=%r",
            source,
            lineno,
            reason,
            preview,
        )

    def _parse_inline_cookie_text(self, text: str) -> list[Cookie]:
        pairs: list[tuple[str, str]] = []
        jar = SimpleCookie()
        jar.load(text)
        pairs.extend((key, morsel.value) for key, morsel in jar.items())
        if not pairs:
            for chunk in text.split(";"):
                if "=" not in chunk:
                    continue
                key, value = chunk.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                pairs.append((key, value.strip()))
        deduped: dict[str, str] = {}
        for key, value in pairs:
            deduped[key] = value
        return [self._netscape_cookie(name, value) for name, value in deduped.items()]

    def _parse_json_cookie_text(self, text: str) -> list[Cookie]:
        stripped = text.lstrip()
        if not stripped or stripped[0] not in "[{":
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict):
            raw_items = payload.get("cookies")
        else:
            raw_items = payload
        if not isinstance(raw_items, list):
            return []
        cookies: list[Cookie] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or item.get("host") or "").strip()
            name = str(item.get("name") or "").strip()
            if not name or not self._is_bilibili_cookie_domain(domain):
                continue
            raw_expires = item.get("expirationDate", item.get("expires", item.get("expiry")))
            expires: int | None = None
            if raw_expires not in (None, "", 0, "0"):
                try:
                    expires = int(float(raw_expires))
                except (TypeError, ValueError):
                    expires = None
            cookies.append(
                self._netscape_cookie(
                    name,
                    str(item.get("value") or ""),
                    domain=domain,
                    path=str(item.get("path") or "/") or "/",
                    secure=bool(item.get("secure")),
                    expires=expires,
                )
            )
        return cookies

    def _parse_netscape_cookie_text(self, text: str, *, source: str) -> list[Cookie]:
        cookies: list[Cookie] = []
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.lstrip("\ufeff")
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                if "bilibili" in line.lower():
                    self._log_cookie_skip(source=source, lineno=lineno, reason=f"invalid_length_{len(parts)}", line=line)
                continue
            domain_name, _include_subdomains, path, https_only, expires_at, name, *value_parts = parts
            domain_name = domain_name.strip()
            name = name.strip()
            if not name or not self._is_bilibili_cookie_domain(domain_name):
                continue
            expires: int | None = None
            expires_text = expires_at.strip()
            if expires_text and expires_text != "0":
                if expires_text.isdigit():
                    expires = int(expires_text)
                else:
                    self._log_cookie_skip(source=source, lineno=lineno, reason="invalid_expires", line=line)
                    continue
            cookies.append(
                self._netscape_cookie(
                    name,
                    "\t".join(value_parts),
                    domain=domain_name,
                    path=path.strip() or "/",
                    secure=https_only.strip().upper() == "TRUE",
                    expires=expires,
                )
            )
        return cookies

    def _configured_cookie_entries(self) -> list[Cookie]:
        source = self._configured_cookie_file_source()
        if not source:
            return []
        path = Path(os.path.expanduser(source))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            if self.logger:
                self.logger.warning("cookie_file_unreadable path=%s error=%s", str(path), exc)
            return []
        cookies = self._parse_json_cookie_text(text)
        if cookies:
            return cookies
        cookies = self._parse_netscape_cookie_text(text, source=str(path))
        if cookies:
            return cookies
        stripped = text.strip()
        if stripped and "\n" not in stripped and "\t" not in stripped and "=" in stripped:
            return self._parse_inline_cookie_text(stripped)
        return []

    def _write_cookie_file(self, cookies: list[Cookie]) -> str | None:
        if not cookies:
            return None
        with self._cookie_file_lock:
            if self._cookie_file_path is not None:
                return self._cookie_file_path
            try:
                from yt_dlp.cookies import YoutubeDLCookieJar
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("yt-dlp package is required for live acquisition.") from exc
            handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="bilibili_cookie_", suffix=".txt", delete=False)
            handle.close()
            jar = YoutubeDLCookieJar(handle.name)
            for cookie in cookies:
                jar.set_cookie(cookie)
            jar.save(ignore_discard=True, ignore_expires=True)
            os.chmod(handle.name, 0o600)
            self._cookie_file_path = handle.name
            atexit.register(self._cleanup_cookie_file)
            return self._cookie_file_path

    def _ensure_cookie_file(self) -> str | None:
        configured_cookie_file = self._configured_cookie_file_source()
        if configured_cookie_file:
            path = Path(configured_cookie_file)
            if path.is_file() and os.access(path, os.R_OK):
                return str(path)
            if self.logger:
                self.logger.warning("cookie_file_unreadable path=%s error=not_readable", str(path))
            return None
        pairs = self._cookie_pairs()
        if not pairs:
            return None
        return self._write_cookie_file([self._netscape_cookie(key, value) for key, value in pairs])

    def _cleanup_cookie_file(self) -> None:
        if not self._cookie_file_path:
            return
        try:
            os.unlink(self._cookie_file_path)
        except FileNotFoundError:
            pass
        self._cookie_file_path = None

    def _http_headers(self, *, user_agent: str | None = None) -> dict[str, str]:
        headers = {
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        return headers

    def _canonical_video_id(self, value: str | None) -> str | None:
        return _canonical_bilibili_video_id(value)

    def _canonical_video_url(self, url: str | None, *, platform_video_id: str | None = None) -> str:
        normalized_url = _normalize_url(url) or ""
        canonical_id = self._canonical_video_id(platform_video_id) or self._canonical_video_id(normalized_url)
        if canonical_id:
            return f"https://www.bilibili.com/video/{canonical_id}"
        return normalized_url

    def _retry_cfg(self) -> dict[str, float]:
        cfg = (self.config.get("crawl") or {}).get("retry") or {}
        return {
            "max_attempts": max(int(cfg.get("max_attempts", 4)), 1),
            "base_sleep_sec": max(float(cfg.get("base_sleep_sec", 60.0)), 0.0),
            "max_sleep_sec": max(float(cfg.get("max_sleep_sec", 900.0)), 0.0),
            "backoff_factor": max(float(cfg.get("backoff_factor", 2.0)), 1.0),
            "jitter_sec": max(float(cfg.get("jitter_sec", 5.0)), 0.0),
        }

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        text = _strip_ansi(str(exc)).lower()
        return any(pattern in text for pattern in _RETRYABLE_ERROR_PATTERNS)

    def _cookie_source_label(self) -> str:
        cookies_from_browser = self._cookies_from_browser_spec()
        if cookies_from_browser:
            browser_name, profile, keyring, container = cookies_from_browser
            details = [browser_name]
            if profile:
                details.append(f"profile={profile}")
            if keyring:
                details.append(f"keyring={keyring}")
            if container:
                details.append(f"container={container}")
            return f"cookies_from_browser:{','.join(details)}"
        configured_cookie_file = self._configured_cookie_file_source()
        if configured_cookie_file:
            path = Path(os.path.expanduser(configured_cookie_file))
            if path.is_file() and os.access(path, os.R_OK):
                return f"cookie_file:{path}"
            return f"cookie_file_unreadable:{path}"
        if self._cookie_value():
            if str(self.source_cfg.get("cookie") or "").strip():
                return "inline_cookie"
            raw_env_name = self.source_cfg.get("cookie_env")
            env_name = "BILIBILI_COOKIE" if raw_env_name is None else str(raw_env_name).strip()
            return f"cookie_env:{env_name or 'disabled'}"
        return "none"

    @staticmethod
    def _yt_dlp_version() -> str | None:
        try:
            from yt_dlp.version import __version__
        except ImportError:
            return None
        return str(__version__).strip() or None

    def _yt_dlp_logger(self, label: str) -> _YtDlpLogger:
        return _YtDlpLogger(self.logger, label=label)

    def _format_download_error(self, exc: Exception) -> str:
        message = _strip_ansi(str(exc).strip())
        lowered = message.lower()
        if not any(pattern in lowered for pattern in _FORMAT_NOT_FOUND_ERROR_PATTERNS):
            return message
        details = [
            "Bilibili exposed no playable formats to yt-dlp.",
            f"cookie_source={self._cookie_source_label()}.",
        ]
        version = self._yt_dlp_version()
        if version:
            details.append(f"yt_dlp_version={version}.")
        details.append(
            "Refresh yt-dlp and provide a fresh logged-in cookie via "
            "sources.bilibili.cookies_from_browser, sources.bilibili.cookie_file, or BILIBILI_COOKIE_FILE."
        )
        details.append(
            "If the same account cannot play the video in a normal browser session, the pipeline will not be able to download it either."
        )
        return f"{message} {' '.join(details)}"

    def _retry_sleep_sec(self, attempt: int) -> float:
        cfg = self._retry_cfg()
        delay = cfg["base_sleep_sec"] * (cfg["backoff_factor"] ** max(attempt - 1, 0))
        delay = min(delay, cfg["max_sleep_sec"])
        jitter_sec = cfg["jitter_sec"]
        if jitter_sec > 0:
            delay += random.uniform(0.0, jitter_sec)
        return round(delay, 3)

    def _log_retry(self, *, action: str, label: str, attempt: int, max_attempts: int, sleep_sec: float, exc: Exception) -> None:
        if not self.logger:
            return
        self.logger.warning(
            "acquisition_retry action=%s target=%s attempt=%s/%s sleep_sec=%s error=%s",
            action,
            label,
            attempt,
            max_attempts,
            sleep_sec,
            _strip_ansi(str(exc)),
        )

    def _sleep_before_retry(self, *, action: str, label: str, attempt: int, max_attempts: int, exc: Exception) -> None:
        sleep_sec = self._retry_sleep_sec(attempt)
        self._log_retry(action=action, label=label, attempt=attempt, max_attempts=max_attempts, sleep_sec=sleep_sec, exc=exc)
        time.sleep(sleep_sec)

    def _request_with_retry(self, url: str, *, params: dict[str, Any] | None = None, timeout: int, label: str):
        session = self._ensure_requests()
        max_attempts = int(self._retry_cfg()["max_attempts"])
        for attempt in range(1, max_attempts + 1):
            try:
                session.headers.update(self._http_headers(user_agent=random.choice(self._user_agents)))
                response = session.get(url, params=params, timeout=timeout)
                response.raise_for_status()
                return response
            except Exception as exc:
                if attempt >= max_attempts or not self._is_retryable_error(exc):
                    raise
                self._sleep_before_retry(action="http_get", label=label, attempt=attempt, max_attempts=max_attempts, exc=exc)

    def _yt_dlp_options(self, *, url: str, output_dir: str | None = None, progress_label: str | None = None) -> dict[str, Any]:
        download_cfg = self.config["crawl"]["download"]
        label = progress_label or str(url)
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "continuedl": True,
            "retries": int(download_cfg.get("retries", 8)),
            "fragment_retries": int(download_cfg.get("fragment_retries", 8)),
            "socket_timeout": int(download_cfg.get("socket_timeout_sec", 30)),
            "noprogress": True,
            "progress_hooks": [self._build_progress_hook(label)],
            "http_headers": self._http_headers(user_agent=random.choice(self._user_agents)),
            "logger": self._yt_dlp_logger(label),
        }
        cookies_from_browser = self._cookies_from_browser_spec()
        if cookies_from_browser:
            options["cookiesfrombrowser"] = cookies_from_browser
        else:
            cookie_file = self._ensure_cookie_file()
            if cookie_file:
                options["cookiefile"] = cookie_file
        if "cookiesfrombrowser" not in options and "cookiefile" not in options and self.logger and not self._warned_cookieless_download:
            self._warned_cookieless_download = True
            self.logger.warning(
                "bilibili_download_without_cookie url=%s hint=%s",
                url,
                "set sources.bilibili.cookies_from_browser or sources.bilibili.cookie_file / BILIBILI_COOKIE_FILE",
            )
        if output_dir is not None:
            options["outtmpl"] = str(Path(output_dir) / "%(id)s.%(ext)s")
        return options

    def _run_yt_dlp_extract_info(self, url: str, *, download: bool, output_dir: str | None = None, progress_label: str | None = None) -> dict[str, Any]:
        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("yt-dlp package is required for live acquisition.") from exc
        options = self._yt_dlp_options(url=url, output_dir=output_dir, progress_label=progress_label)
        with YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=download)

    def _extract_info(self, url: str, *, download: bool, output_dir: str | None = None, progress_label: str | None = None) -> dict[str, Any]:
        max_attempts = int(self._retry_cfg()["max_attempts"])
        label = progress_label or url
        for attempt in range(1, max_attempts + 1):
            try:
                return self._run_yt_dlp_extract_info(url, download=download, output_dir=output_dir, progress_label=progress_label)
            except Exception as exc:
                if attempt >= max_attempts or not self._is_retryable_error(exc):
                    raise
                self._sleep_before_retry(action="yt_dlp_extract", label=label, attempt=attempt, max_attempts=max_attempts, exc=exc)
        raise RuntimeError(f"Failed to extract info for {label}")

    def search(self, query_text: str, limit: int) -> list[CandidateVideo]:
        output: list[CandidateVideo] = []
        seen_ids: set[str] = set()
        max_pages = int(self.source_cfg.get("max_pages_per_query", 1))
        for page in range(1, max_pages + 1):
            params = {"keyword": query_text, "page": page}
            rows: list[dict[str, Any]] = []
            for url in self.SEARCH_URLS:
                response = self._request_with_retry(
                    url,
                    params=params,
                    timeout=int(self.source_cfg.get("search_timeout_sec", 20)),
                    label=f"search:{query_text}:page={page}",
                )
                payload = response.json()
                if payload.get("code") != 0:
                    continue
                for block in payload.get("data", {}).get("result", []):
                    data = block.get("data") or []
                    if isinstance(data, dict):
                        data = [data]
                    rows.extend(data)
                if rows:
                    break
            for row in rows:
                bvid = self._canonical_video_id(row.get("bvid") or row.get("bv_id"))
                if not bvid or bvid in seen_ids:
                    continue
                seen_ids.add(bvid)
                title = re.sub(r"<[^>]+>", "", str(row.get("title") or row.get("desc") or "")).strip()
                tags = [item for item in str(row.get("tag") or "").split(",") if item]
                output.append(
                    CandidateVideo(
                        platform="bilibili",
                        platform_video_id=bvid,
                        url=self._canonical_video_url(row.get("arcurl") or f"https://www.bilibili.com/video/{bvid}", platform_video_id=bvid),
                        title=title,
                        uploader=row.get("author") or row.get("uname"),
                        publish_time=str(row.get("pubdate") or row.get("ctime") or ""),
                        duration_sec=_parse_duration_to_seconds(row.get("duration")),
                        cover_url=_normalize_url(row.get("cover") or row.get("pic")),
                        description=row.get("description") or row.get("desc"),
                        tags=tags,
                        categories=[],
                        subtitles_available=False,
                        platform_metadata={
                            "play_count": row.get("play"),
                            "danmaku_count": row.get("video_review"),
                            "type": row.get("typename"),
                        },
                        extra={},
                    )
                )
                if len(output) >= limit:
                    return output
        return output

    def _fetch_subtitle_segments(self, info: dict[str, Any]) -> list[dict[str, Any]]:
        subtitle_sources = [info.get("subtitles") or {}, info.get("automatic_captions") or {}]
        for source in subtitle_sources:
            for entries in source.values():
                for entry in entries or []:
                    url = entry.get("url")
                    ext = str(entry.get("ext") or "").lower()
                    if not url or ext not in {"json", "srt", "vtt"}:
                        continue
                    response = self._request_with_retry(
                        url,
                        timeout=int(self.source_cfg.get("enrich_timeout_sec", 30)),
                        label=f"subtitle:{info.get('id') or info.get('webpage_url') or url}",
                    )
                    if ext == "json":
                        payload = response.json()
                        body = payload.get("body") or []
                        segments = []
                        for index, item in enumerate(body, start=1):
                            content = str(item.get("content") or "").strip()
                            if not content:
                                continue
                            segments.append(
                                {
                                    "segment_id": f"seg_{index}",
                                    "start": float(item.get("from", 0.0)),
                                    "end": float(item.get("to", 0.0)),
                                    "text": content,
                                }
                            )
                        if segments:
                            return segments
        return []

    def enrich(self, candidate: CandidateVideo) -> CandidateVideo:
        canonical_video_id = self._canonical_video_id(candidate.platform_video_id) or candidate.platform_video_id
        canonical_url = self._canonical_video_url(candidate.url, platform_video_id=canonical_video_id)
        info = self._extract_info(canonical_url, download=False, progress_label=canonical_video_id)
        subtitle_segments = self._fetch_subtitle_segments(info)
        return CandidateVideo(
            platform=candidate.platform,
            platform_video_id=canonical_video_id,
            url=canonical_url,
            title=info.get("title") or candidate.title,
            uploader=info.get("uploader") or candidate.uploader,
            publish_time=str(info.get("timestamp") or candidate.publish_time or ""),
            duration_sec=float(info.get("duration") or candidate.duration_sec or 0) or candidate.duration_sec,
            cover_url=info.get("thumbnail") or candidate.cover_url,
            description=info.get("description") or candidate.description,
            tags=list(info.get("tags") or candidate.tags),
            categories=list(info.get("categories") or []),
            subtitles_available=bool(info.get("subtitles") or info.get("automatic_captions")),
            platform_metadata={
                **candidate.platform_metadata,
                "view_count": info.get("view_count"),
                "like_count": info.get("like_count"),
                "comment_count": info.get("comment_count"),
                "webpage_url": info.get("webpage_url") or canonical_url,
                "subtitles": bool(info.get("subtitles")),
                "automatic_captions": bool(info.get("automatic_captions")),
            },
            extra={
                **candidate.extra,
                "subtitle_segments": subtitle_segments,
            },
        )

    def download(self, candidate: CandidateVideo, output_dir: str) -> DownloadResult:
        canonical_video_id = self._canonical_video_id(candidate.platform_video_id) or candidate.platform_video_id
        canonical_url = self._canonical_video_url(candidate.url, platform_video_id=canonical_video_id)
        if self.logger:
            self.logger.info(
                "download_start bvid=%s title=%s cookie_source=%s yt_dlp_version=%s",
                canonical_video_id,
                candidate.title,
                self._cookie_source_label(),
                self._yt_dlp_version() or "unknown",
            )
        try:
            info = self._extract_info(canonical_url, download=True, output_dir=output_dir, progress_label=canonical_video_id)
        except Exception as exc:  # pragma: no cover
            error_message = self._format_download_error(exc)
            if self.logger:
                self.logger.exception("download_failed bvid=%s error=%s", canonical_video_id, error_message)
            return DownloadResult(status="failed", error_message=error_message)
        requested = info.get("requested_downloads") or []
        local_path = None
        if requested:
            local_path = requested[0].get("filepath")
        if not local_path:
            local_path = str(Path(output_dir) / f"{info.get('id', canonical_video_id)}.{info.get('ext', 'mp4')}")
        path = Path(local_path)
        size = int(path.stat().st_size) if path.exists() else None
        if self.logger:
            self.logger.info("download_finish bvid=%s status=%s file_size_bytes=%s", canonical_video_id, "downloaded" if path.exists() else "failed", size)
        return DownloadResult(status="downloaded" if path.exists() else "failed", local_path=str(path), file_size_bytes=size)


class AcquisitionService:
    def __init__(self, config: dict[str, Any], *, query_repo: Any, video_repo: Any, layout: dict[str, Path], logger: Any, adapter: Any | None = None):
        self.config = config
        self.query_repo = query_repo
        self.video_repo = video_repo
        self.layout = layout
        self.logger = logger
        self.adapter = adapter or BilibiliAdapter(config, logger=logger)
        video_shard_cfg = config.get("crawl", {}).get("video_shard") or {}
        self.video_shard_count = max(int(video_shard_cfg.get("count", 1)), 1)
        self.video_shard_index = int(video_shard_cfg.get("index", 0))
        if self.video_shard_index < 0 or self.video_shard_index >= self.video_shard_count:
            raise ValueError(f"Invalid video shard index={self.video_shard_index} for shard_count={self.video_shard_count}")
        self._download_host_gate = threading.BoundedSemaphore(int(config["crawl"]["download"].get("max_inflight_per_host", 2)))
        self._last_crawl_stats = self._new_crawl_stats()

    @staticmethod
    def _new_crawl_stats() -> dict[str, int]:
        return {
            "query_count": 0,
            "hits": 0,
            "existing_skipped": 0,
            "shard_skipped": 0,
            "rejected_before_download": 0,
            "queued_for_download": 0,
            "downloaded": 0,
            "download_failed": 0,
            "download_rejected": 0,
        }

    def last_crawl_stats(self) -> dict[str, int]:
        return dict(self._last_crawl_stats)

    @staticmethod
    def load_query_seed_catalog(path: str | Path) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def seed_queries(self, catalog_path: str | Path) -> int:
        catalog = self.load_query_seed_catalog(catalog_path)
        count = 0
        for row in catalog.get("rows", []):
            for query_text in row.get("query_texts", []):
                query_id = make_query_id(row["seed_id"], query_text)
                if not self.query_repo.owns_query_id(query_id):
                    continue
                self.query_repo.upsert(
                    {
                        "query_id": query_id,
                        "seed_id": row["seed_id"],
                        "excel_row": row["excel_row"],
                        "scene_text": row["scene_text"],
                        "trigger_text": row["trigger_text"],
                        "query_text": query_text,
                        "status": "pending",
                        "last_run_at": None,
                        "hit_count": 0,
                    }
                )
                count += 1
        return count

    def _owns_video_id(self, platform: str, platform_video_id: str) -> bool:
        if self.video_shard_count <= 1:
            return True
        digest = hashlib.sha1(f"{platform}:{platform_video_id}".encode("utf-8")).hexdigest()
        return int(digest, 16) % self.video_shard_count == self.video_shard_index

    def _reject_reason(self, candidate: CandidateVideo) -> str | None:
        if not candidate.platform_video_id or not candidate.title or not candidate.url or candidate.duration_sec is None:
            return "missing_core_metadata"
        if candidate.duration_sec < float(self.config["crawl"]["min_duration_sec"]):
            return "too_short"
        if candidate.duration_sec > float(self.config["crawl"]["max_duration_sec"]):
            return "too_long"
        return None

    def _video_artifact_dir(self, video_uid: str) -> Path:
        path = self.layout["artifact_videos"] / video_uid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_video_meta(self, payload: dict[str, Any], *, video_uid: str) -> Path:
        path = self._video_artifact_dir(video_uid) / "video_meta.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _write_video_rejection(self, payload: dict[str, Any], *, video_uid: str) -> None:
        path = self.layout["artifact_rejections"] / "videos"
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{video_uid}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _serialize_video_meta(
        self,
        candidate: CandidateVideo,
        *,
        query: dict[str, Any],
        video_uid: str,
        download_status: str,
        reject_reason: str | None,
        retention_status: str | None,
        raw_video_path: str | None = None,
        file_size_bytes: int | None = None,
        download_started_at: str | None = None,
        download_finished_at: str | None = None,
        download_elapsed_sec: float | None = None,
    ) -> dict[str, Any]:
        metadata = dict(candidate.platform_metadata)
        extra = dict(candidate.extra)
        subtitle_segments = extra.get("subtitle_segments") or []
        return {
            "video_uid": video_uid,
            "platform": candidate.platform,
            "bvid": candidate.platform_video_id,
            "url": candidate.url,
            "title": candidate.title,
            "uploader": candidate.uploader,
            "publish_time": candidate.publish_time,
            "duration_sec": candidate.duration_sec,
            "cover_url": candidate.cover_url,
            "description": candidate.description,
            "tags": candidate.tags,
            "categories": candidate.categories,
            "view_count": metadata.get("view_count"),
            "like_count": metadata.get("like_count"),
            "comment_count": metadata.get("comment_count"),
            "play_count": metadata.get("play_count"),
            "danmaku_count": metadata.get("danmaku_count"),
            "type": metadata.get("type"),
            "webpage_url": metadata.get("webpage_url"),
            "query_id": query["query_id"],
            "scene_text": query["scene_text"],
            "trigger_text": query["trigger_text"],
            "source_excel_row": query["excel_row"],
            "subtitles_available": bool(candidate.subtitles_available or subtitle_segments),
            "rights_status": metadata.get("rights_status", "unknown"),
            "download_status": download_status,
            "download_started_at": download_started_at,
            "download_finished_at": download_finished_at,
            "download_elapsed_sec": download_elapsed_sec,
            "reject_reason": reject_reason,
            "retention_status": retention_status,
            "accepted_clip_count": 0,
            "preprocess_started_at": None,
            "preprocess_finished_at": None,
            "preprocess_elapsed_sec": None,
            "raw_video_path": raw_video_path,
            "file_size_bytes": file_size_bytes,
            "platform_metadata": metadata,
            "extra": extra,
        }

    def _download_candidate(self, candidate: CandidateVideo, output_dir: Path) -> DownloadResult:
        with self._download_host_gate:
            return self.adapter.download(candidate, str(output_dir))

    def _timed_download_candidate(self, candidate: CandidateVideo, output_dir: Path) -> tuple[DownloadResult, str, str, float]:
        started_at = datetime.now().isoformat(timespec="seconds")
        start_clock = time.perf_counter()
        result = self._download_candidate(candidate, output_dir)
        elapsed_sec = round(time.perf_counter() - start_clock, 3)
        finished_at = datetime.now().isoformat(timespec="seconds")
        return result, started_at, finished_at, elapsed_sec

    def _format_enrich_error(self, exc: Exception) -> str:
        formatter = getattr(self.adapter, "_format_download_error", None)
        if callable(formatter):
            try:
                return str(formatter(exc))
            except Exception:
                pass
        return _strip_ansi(str(exc).strip())

    def crawl(self, limit: int | None = None) -> int:
        cycle_stats = self._new_crawl_stats()
        pending_queries = self.query_repo.list_pending(limit if limit is not None else int(self.config["crawl"]["max_queries_per_run"]))
        processed = 0
        for query in pending_queries:
            query_started_clock = time.perf_counter()
            search_query = build_search_query(query)
            candidates = self.adapter.search(search_query, int(self.config["crawl"]["max_items_per_query"]))
            hit_count = len(candidates)
            cycle_stats["query_count"] += 1
            cycle_stats["hits"] += hit_count
            accepted_for_download: list[CandidateVideo] = []
            existing_skipped = 0
            shard_skipped = 0
            rejected_before_download = 0
            downloaded_count = 0
            download_failed_count = 0
            download_rejected_count = 0
            with ThreadPoolExecutor(max_workers=int(self.config["crawl"].get("enrich_workers", 2))) as executor:
                futures = {}
                for candidate in candidates:
                    if self.video_repo.exists(candidate.platform_video_id):
                        existing_skipped += 1
                        continue
                    if not self._owns_video_id(candidate.platform, candidate.platform_video_id):
                        shard_skipped += 1
                        continue
                    futures[executor.submit(self.adapter.enrich, candidate)] = candidate.platform_video_id
                for future in as_completed(futures):
                    original_bvid = futures[future]
                    try:
                        enriched = future.result()
                    except Exception as exc:
                        candidate = next(
                            (item for item in candidates if item.platform_video_id == original_bvid),
                            None,
                        )
                        if candidate is None:
                            raise
                        reject_reason = self._format_enrich_error(exc) or "enrich_failed"
                        video_uid = make_video_uid(candidate.platform, candidate.platform_video_id)
                        payload = self._serialize_video_meta(
                            candidate,
                            query=query,
                            video_uid=video_uid,
                            download_status="rejected",
                            reject_reason=reject_reason,
                            retention_status="rejected_deleted",
                        )
                        video_meta_path = self._write_video_meta(payload, video_uid=video_uid)
                        self._write_video_rejection(payload, video_uid=video_uid)
                        self.video_repo.upsert({**payload, "video_meta_path": str(video_meta_path)})
                        self.video_repo.add_download_event(video_uid, "rejected", error_message=reject_reason)
                        rejected_before_download += 1
                        processed += 1
                        if self.logger:
                            self.logger.exception("enrich_failed bvid=%s error=%s", candidate.platform_video_id, reject_reason)
                        continue
                    reason = self._reject_reason(enriched)
                    video_uid = make_video_uid(enriched.platform, enriched.platform_video_id)
                    if reason:
                        payload = self._serialize_video_meta(
                            enriched,
                            query=query,
                            video_uid=video_uid,
                            download_status="rejected",
                            reject_reason=reason,
                            retention_status="rejected_deleted",
                        )
                        video_meta_path = self._write_video_meta(payload, video_uid=video_uid)
                        self._write_video_rejection(payload, video_uid=video_uid)
                        self.video_repo.upsert({**payload, "video_meta_path": str(video_meta_path)})
                        self.video_repo.add_download_event(video_uid, "rejected", error_message=reason)
                        rejected_before_download += 1
                        processed += 1
                    else:
                        accepted_for_download.append(enriched)
            cycle_stats["existing_skipped"] += existing_skipped
            cycle_stats["shard_skipped"] += shard_skipped
            cycle_stats["rejected_before_download"] += rejected_before_download
            cycle_stats["queued_for_download"] += len(accepted_for_download)

            if accepted_for_download and bool(self.config["crawl"]["download"].get("enabled", True)):
                output_root = self.layout["tmp_videos"] / _safe_dir_name(query["scene_text"])
                output_root.mkdir(parents=True, exist_ok=True)
                download_started_by_video_uid: dict[str, str] = {}
                for candidate in accepted_for_download:
                    video_uid = make_video_uid(candidate.platform, candidate.platform_video_id)
                    started_at = datetime.now().isoformat(timespec="seconds")
                    download_started_by_video_uid[video_uid] = started_at
                    payload = self._serialize_video_meta(
                        candidate,
                        query=query,
                        video_uid=video_uid,
                        download_status="downloading",
                        reject_reason=None,
                        retention_status=None,
                        raw_video_path=None,
                        file_size_bytes=None,
                        download_started_at=started_at,
                        download_finished_at=None,
                        download_elapsed_sec=None,
                    )
                    video_meta_path = self._write_video_meta(payload, video_uid=video_uid)
                    self.video_repo.upsert({**payload, "video_meta_path": str(video_meta_path)})
                with ThreadPoolExecutor(max_workers=int(self.config["crawl"]["download"].get("workers", 4))) as executor:
                    futures = {
                        executor.submit(self._timed_download_candidate, candidate, output_root): candidate
                        for candidate in accepted_for_download
                    }
                    for future in as_completed(futures):
                        candidate = futures[future]
                        result, download_started_at, download_finished_at, download_elapsed_sec = future.result()
                        video_uid = make_video_uid(candidate.platform, candidate.platform_video_id)
                        reject_reason = None
                        retention_status = None
                        if result.status != "downloaded":
                            reject_reason = result.error_message or "download_failed"
                            retention_status = "rejected_deleted"
                        elif result.file_size_bytes and result.file_size_bytes > int(self.config["crawl"]["max_video_size_mb"]) * 1024 * 1024:
                            reject_reason = "video_too_large"
                            retention_status = "rejected_deleted"
                            path = Path(result.local_path or "")
                            if path.exists():
                                path.unlink()
                            result = DownloadResult(status="rejected", error_message=reject_reason)
                        if result.status == "downloaded":
                            downloaded_count += 1
                        elif result.status == "rejected":
                            download_rejected_count += 1
                        else:
                            download_failed_count += 1
                        payload = self._serialize_video_meta(
                            candidate,
                            query=query,
                            video_uid=video_uid,
                            download_status="downloaded" if result.status == "downloaded" else "rejected",
                            reject_reason=reject_reason,
                            retention_status=retention_status,
                            raw_video_path=result.local_path if result.status == "downloaded" else None,
                            file_size_bytes=result.file_size_bytes,
                            download_started_at=download_started_by_video_uid.get(video_uid, download_started_at),
                            download_finished_at=download_finished_at,
                            download_elapsed_sec=download_elapsed_sec,
                        )
                        video_meta_path = self._write_video_meta(payload, video_uid=video_uid)
                        if result.status != "downloaded":
                            self._write_video_rejection(payload, video_uid=video_uid)
                        self.video_repo.upsert({**payload, "video_meta_path": str(video_meta_path)})
                        file_size = result.file_size_bytes
                        if result.local_path and Path(result.local_path).exists():
                            file_size = int(Path(result.local_path).stat().st_size)
                        self.video_repo.add_download_event(
                            video_uid,
                            result.status,
                            local_path=result.local_path,
                            file_size_bytes=file_size,
                            error_message=result.error_message,
                            extra={"sha256": file_sha256(result.local_path) if result.local_path and Path(result.local_path).exists() else None},
                        )
                        processed += 1
            cycle_stats["downloaded"] += downloaded_count
            cycle_stats["download_failed"] += download_failed_count
            cycle_stats["download_rejected"] += download_rejected_count
            self.query_repo.mark_done(query["query_id"], hit_count)
            if self.logger:
                self.logger.info(
                    "crawl_query query_id=%s search_query=%s hits=%s existing_skipped=%s shard_skipped=%s rejected_before_download=%s queued_for_download=%s downloaded=%s download_failed=%s download_rejected=%s elapsed_sec=%s",
                    query["query_id"],
                    search_query,
                    hit_count,
                    existing_skipped,
                    shard_skipped,
                    rejected_before_download,
                    len(accepted_for_download),
                    downloaded_count,
                    download_failed_count,
                    download_rejected_count,
                    round(time.perf_counter() - query_started_clock, 3),
                )
        self._last_crawl_stats = cycle_stats
        return processed
