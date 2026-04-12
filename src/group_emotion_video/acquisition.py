from __future__ import annotations

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from .hashing import file_sha256
from .ids import make_query_id, make_video_uid
from .types import CandidateVideo, DownloadResult


_QUERY_SPLIT_PATTERN = re.compile(r"[、,，/／|｜]+")


def _normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url.lstrip('/')}"


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
        terms = scene_terms or _extract_search_terms(query_text)
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
        return self._session

    def _extract_info(self, url: str, *, download: bool, output_dir: str | None = None, progress_label: str | None = None) -> dict[str, Any]:
        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("yt-dlp package is required for live acquisition.") from exc
        download_cfg = self.config["crawl"]["download"]
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "continuedl": True,
            "retries": int(download_cfg.get("retries", 8)),
            "fragment_retries": int(download_cfg.get("fragment_retries", 8)),
            "socket_timeout": int(download_cfg.get("socket_timeout_sec", 30)),
            "noprogress": True,
            "progress_hooks": [self._build_progress_hook(progress_label or str(url))],
        }
        if output_dir is not None:
            options["outtmpl"] = str(Path(output_dir) / "%(id)s.%(ext)s")
        with YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=download)

    def search(self, query_text: str, limit: int) -> list[CandidateVideo]:
        session = self._ensure_requests()
        output: list[CandidateVideo] = []
        seen_ids: set[str] = set()
        max_pages = int(self.source_cfg.get("max_pages_per_query", 1))
        for page in range(1, max_pages + 1):
            params = {"keyword": query_text, "page": page}
            rows: list[dict[str, Any]] = []
            for url in self.SEARCH_URLS:
                session.headers["User-Agent"] = random.choice(self._user_agents)
                response = session.get(url, params=params, timeout=int(self.source_cfg.get("search_timeout_sec", 20)))
                response.raise_for_status()
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
                bvid = row.get("bvid") or row.get("bv_id")
                if not bvid or bvid in seen_ids:
                    continue
                seen_ids.add(bvid)
                title = re.sub(r"<[^>]+>", "", str(row.get("title") or row.get("desc") or "")).strip()
                tags = [item for item in str(row.get("tag") or "").split(",") if item]
                output.append(
                    CandidateVideo(
                        platform="bilibili",
                        platform_video_id=bvid,
                        url=_normalize_url(row.get("arcurl") or f"https://www.bilibili.com/video/{bvid}") or "",
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
        session = self._ensure_requests()
        subtitle_sources = [info.get("subtitles") or {}, info.get("automatic_captions") or {}]
        for source in subtitle_sources:
            for entries in source.values():
                for entry in entries or []:
                    url = entry.get("url")
                    ext = str(entry.get("ext") or "").lower()
                    if not url or ext not in {"json", "srt", "vtt"}:
                        continue
                    response = session.get(url, timeout=int(self.source_cfg.get("enrich_timeout_sec", 30)))
                    response.raise_for_status()
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
        info = self._extract_info(candidate.url, download=False, progress_label=candidate.platform_video_id)
        subtitle_segments = self._fetch_subtitle_segments(info)
        return CandidateVideo(
            platform=candidate.platform,
            platform_video_id=candidate.platform_video_id,
            url=candidate.url,
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
                "webpage_url": info.get("webpage_url") or candidate.url,
                "subtitles": bool(info.get("subtitles")),
                "automatic_captions": bool(info.get("automatic_captions")),
            },
            extra={
                **candidate.extra,
                "subtitle_segments": subtitle_segments,
            },
        )

    def download(self, candidate: CandidateVideo, output_dir: str) -> DownloadResult:
        if self.logger:
            self.logger.info("download_start bvid=%s title=%s", candidate.platform_video_id, candidate.title)
        try:
            info = self._extract_info(candidate.url, download=True, output_dir=output_dir, progress_label=candidate.platform_video_id)
        except Exception as exc:  # pragma: no cover
            if self.logger:
                self.logger.exception("download_failed bvid=%s error=%s", candidate.platform_video_id, exc)
            return DownloadResult(status="failed", error_message=str(exc))
        requested = info.get("requested_downloads") or []
        local_path = None
        if requested:
            local_path = requested[0].get("filepath")
        if not local_path:
            local_path = str(Path(output_dir) / f"{info.get('id', candidate.platform_video_id)}.{info.get('ext', 'mp4')}")
        path = Path(local_path)
        size = int(path.stat().st_size) if path.exists() else None
        if self.logger:
            self.logger.info("download_finish bvid=%s status=%s file_size_bytes=%s", candidate.platform_video_id, "downloaded" if path.exists() else "failed", size)
        return DownloadResult(status="downloaded" if path.exists() else "failed", local_path=str(path), file_size_bytes=size)


class AcquisitionService:
    def __init__(self, config: dict[str, Any], *, query_repo: Any, video_repo: Any, layout: dict[str, Path], logger: Any, adapter: Any | None = None):
        self.config = config
        self.query_repo = query_repo
        self.video_repo = video_repo
        self.layout = layout
        self.logger = logger
        self.adapter = adapter or BilibiliAdapter(config, logger=logger)
        self._download_host_gate = threading.BoundedSemaphore(int(config["crawl"]["download"].get("max_inflight_per_host", 2)))

    @staticmethod
    def load_query_seed_catalog(path: str | Path) -> dict[str, Any]:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def seed_queries(self, catalog_path: str | Path) -> int:
        catalog = self.load_query_seed_catalog(catalog_path)
        count = 0
        for row in catalog.get("rows", []):
            for query_text in row.get("query_texts", []):
                self.query_repo.upsert(
                    {
                        "query_id": make_query_id(row["seed_id"], query_text),
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

    def crawl(self, limit: int | None = None) -> int:
        pending_queries = self.query_repo.list_pending(limit if limit is not None else int(self.config["crawl"]["max_queries_per_run"]))
        processed = 0
        for query in pending_queries:
            search_query = build_search_query(query)
            candidates = self.adapter.search(search_query, int(self.config["crawl"]["max_items_per_query"]))
            hit_count = len(candidates)
            accepted_for_download: list[CandidateVideo] = []
            enriched_by_bvid: dict[str, CandidateVideo] = {}
            with ThreadPoolExecutor(max_workers=int(self.config["crawl"].get("enrich_workers", 2))) as executor:
                futures = {
                    executor.submit(self.adapter.enrich, candidate): candidate.platform_video_id
                    for candidate in candidates
                    if not self.video_repo.exists(candidate.platform_video_id)
                }
                for future in as_completed(futures):
                    enriched = future.result()
                    enriched_by_bvid[enriched.platform_video_id] = enriched
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
                        processed += 1
                    else:
                        accepted_for_download.append(enriched)

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
            self.query_repo.mark_done(query["query_id"], hit_count)
        return processed
