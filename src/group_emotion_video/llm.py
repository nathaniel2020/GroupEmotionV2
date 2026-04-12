from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import mimetypes
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from .logging_utils import RunLogger


RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class LLMResult:
    call_id: str
    payload: dict[str, Any]
    raw_text: str
    status: str
    latency_sec: float
    usage: dict[str, Any] | None
    request_path: str
    response_path: str


class MultimodalLLMClient:
    def __init__(self, config: dict[str, Any], run_logger: RunLogger):
        self.config = config
        self.llm_config = dict(config["llm"])
        self.run_logger = run_logger
        self._client: Any | None = None
        self._api_connection_error: type[Exception] | None = None
        self._api_timeout_error: type[Exception] | None = None
        self._api_status_error: type[Exception] | None = None

    @staticmethod
    def _is_remote_url(value: str | None) -> bool:
        if not value:
            return False
        return str(value).startswith(("http://", "https://"))

    @staticmethod
    def _image_data_url(path: Path) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("The `openai` package is required for live model calls.") from exc

        self._api_connection_error = APIConnectionError
        self._api_status_error = APIStatusError
        self._api_timeout_error = APITimeoutError
        self._client = OpenAI(
            api_key=self._resolve_api_key(),
            base_url=str(self.llm_config["base_url"]),
            timeout=int(self.llm_config["timeout_sec"]),
            max_retries=0,
        )
        return self._client

    def _should_retry(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code in RETRYABLE_STATUS_CODES:
            return True
        retryable_types = tuple(
            error_type
            for error_type in (
                self._api_connection_error,
                self._api_timeout_error,
            )
            if error_type is not None
        )
        return bool(retryable_types and isinstance(exc, retryable_types))

    def _resolve_api_key(self) -> str:
        env_name = str(self.llm_config.get("api_key_env", "OPENAI_API_KEY"))
        value = str(__import__("os").getenv(env_name, "")).strip()
        if value:
            return value
        return "EMPTY"

    def _build_content(
        self,
        *,
        user_prompt: str,
        frame_paths: list[str],
        video_url: str | None,
    ) -> str | list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]

        if video_url:
            if not bool(self.llm_config.get("use_remote_video_url", False)):
                raise ValueError("Remote video URL input is disabled in config.")
            if not self._is_remote_url(video_url):
                raise ValueError("Only remote video URLs are allowed in the lightweight client.")
            content.insert(0, {"type": "video_url", "video_url": {"url": video_url}})

        max_images = max(0, int(self.llm_config.get("max_images_per_request", 6)))
        for raw_path in frame_paths[:max_images]:
            path = Path(raw_path)
            if self._is_remote_url(raw_path):
                image_url = raw_path
            elif path.exists():
                image_url = self._image_data_url(path)
            else:
                continue
            content.append({"type": "image_url", "image_url": {"url": image_url}})
        return content if len(content) > 1 else user_prompt

    @staticmethod
    def _extract_text(response: Any) -> str:
        message = response.choices[0].message
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    text_value = getattr(item, "text", None)
                    if text_value:
                        parts.append(str(text_value))
            return "\n".join(part for part in parts if part)
        return str(content or "")

    @staticmethod
    def _usage_payload(response: Any) -> dict[str, Any] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        if hasattr(usage, "model_dump"):
            return usage.model_dump()
        if isinstance(usage, dict):
            return usage
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }

    def annotate_json(
        self,
        *,
        sample_id: str,
        system_prompt: str,
        user_prompt: str,
        frame_paths: list[str] | None = None,
        video_url: str | None = None,
        dry_run: bool = False,
    ) -> LLMResult:
        call_id = f"call_{uuid4().hex[:10]}"
        call_dir = self.run_logger.allocate_llm_call_dir(call_id)
        request_path = call_dir / "request.json"
        response_path = call_dir / "response.json"
        frame_paths = [str(path) for path in frame_paths or []]
        request_payload = {
            "model": self.llm_config["model"],
            "temperature": float(self.llm_config.get("temperature", 0.1)),
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": self._build_content(
                        user_prompt=user_prompt,
                        frame_paths=frame_paths,
                        video_url=video_url,
                    ),
                },
            ],
        }
        if str(self.llm_config.get("response_format", "")).strip().lower() == "json_object":
            request_payload["response_format"] = {"type": "json_object"}
        request_path.write_text(json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        if dry_run or not bool(self.llm_config.get("enabled", False)):
            payload = {
                "status": "needs_review",
                "summary": "Dry run output. Live LLM call was skipped.",
                "group_emotion": "unknown",
                "emotion_intensity": "unknown",
                "confidence": 0.0,
                "evidence": [],
                "review_reasons": ["dry_run_or_llm_disabled"],
            }
            response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self.run_logger.llm_call(
                call_id=call_id,
                sample_id=sample_id,
                model=self.llm_config["model"],
                status="skipped",
                latency_sec=0.0,
                request_path=str(request_path),
                response_path=str(response_path),
                attempt=0,
            )
            return LLMResult(
                call_id=call_id,
                payload=payload,
                raw_text=json.dumps(payload, ensure_ascii=False),
                status="skipped",
                latency_sec=0.0,
                usage=None,
                request_path=str(request_path),
                response_path=str(response_path),
            )

        max_retries = max(1, int(self.llm_config.get("max_retries", 3)))
        backoff_sec = max(0.0, float(self.llm_config.get("retry_backoff_sec", 2)))
        last_exc: Exception | None = None

        for attempt in range(1, max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._ensure_client().chat.completions.create(**request_payload)
                latency_sec = round(time.perf_counter() - started, 3)
                raw_response = response.model_dump() if hasattr(response, "model_dump") else str(response)
                raw_text = self._extract_text(response)
                payload = json.loads(raw_text)
                response_path.write_text(json.dumps(raw_response, ensure_ascii=False, indent=2), encoding="utf-8")
                usage = self._usage_payload(response)
                self.run_logger.llm_call(
                    call_id=call_id,
                    sample_id=sample_id,
                    model=self.llm_config["model"],
                    status="success",
                    latency_sec=latency_sec,
                    request_path=str(request_path),
                    response_path=str(response_path),
                    attempt=attempt,
                    usage=usage,
                )
                return LLMResult(
                    call_id=call_id,
                    payload=payload,
                    raw_text=raw_text,
                    status="success",
                    latency_sec=latency_sec,
                    usage=usage,
                    request_path=str(request_path),
                    response_path=str(response_path),
                )
            except json.JSONDecodeError as exc:
                latency_sec = round(time.perf_counter() - started, 3)
                last_exc = exc
                response_path.write_text(
                    json.dumps({"raw_text": raw_text, "parse_error": str(exc)}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self.run_logger.llm_call(
                    call_id=call_id,
                    sample_id=sample_id,
                    model=self.llm_config["model"],
                    status="parse_error",
                    latency_sec=latency_sec,
                    request_path=str(request_path),
                    response_path=str(response_path),
                    attempt=attempt,
                    error_type=exc.__class__.__name__,
                )
                raise ValueError("LLM response was not valid JSON.") from exc
            except Exception as exc:  # pragma: no cover
                latency_sec = round(time.perf_counter() - started, 3)
                last_exc = exc
                error_type = exc.__class__.__name__
                status_code = getattr(exc, "status_code", None)
                response_path.write_text(
                    json.dumps(
                        {
                            "error_type": error_type,
                            "error_message": str(exc),
                            "status_code": status_code,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                self.run_logger.llm_call(
                    call_id=call_id,
                    sample_id=sample_id,
                    model=self.llm_config["model"],
                    status="error",
                    latency_sec=latency_sec,
                    request_path=str(request_path),
                    response_path=str(response_path),
                    attempt=attempt,
                    error_type=error_type,
                    status_code=status_code,
                )
                if attempt >= max_retries or not self._should_retry(exc):
                    raise
                time.sleep(backoff_sec * attempt)

        if last_exc is not None:  # pragma: no cover
            raise last_exc
        raise RuntimeError("LLM call exited without a result.")
