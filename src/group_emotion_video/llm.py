from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class LLMConcurrencyGate:
    def __init__(self, enabled: bool, max_inflight_requests: int, acquire_timeout_sec: float):
        self.enabled = enabled and max_inflight_requests > 1
        self.max_inflight_requests = max(1, int(max_inflight_requests))
        self.acquire_timeout_sec = float(acquire_timeout_sec)
        self._semaphore = threading.BoundedSemaphore(self.max_inflight_requests)

    @contextmanager
    def acquire(self):
        if not self.enabled:
            yield
            return
        acquired = self._semaphore.acquire(timeout=self.acquire_timeout_sec)
        if not acquired:
            raise TimeoutError("Timed out waiting for LLM concurrency gate.")
        try:
            yield
        finally:
            self._semaphore.release()


class OpenAICompatibleLLM:
    def __init__(self, config: dict[str, Any]):
        llm_cfg = config["llm"]
        parallel_cfg = llm_cfg.get("parallel", {})
        self.enabled = bool(llm_cfg.get("enabled", False))
        self.base_url = str(llm_cfg.get("base_url", "")).strip()
        self.model = str(llm_cfg.get("model", "")).strip()
        self.timeout_sec = int(llm_cfg.get("timeout_sec", 180))
        self.temperature = float(llm_cfg.get("temperature", 0.1))
        self.max_images_per_prompt = int(llm_cfg.get("max_images_per_prompt", 4))
        self.api_key_env = str(llm_cfg.get("api_key_env", "OPENAI_API_KEY")).strip()
        self._client: Any | None = None
        self._thread_state = threading.local()
        self.gate = LLMConcurrencyGate(
            enabled=bool(parallel_cfg.get("enabled", False)),
            max_inflight_requests=int(parallel_cfg.get("max_inflight_requests", 1)),
            acquire_timeout_sec=float(parallel_cfg.get("acquire_timeout_sec", 300)),
        )

    @property
    def last_error_context(self) -> dict[str, Any] | None:
        return getattr(self._thread_state, "last_error_context", None)

    @last_error_context.setter
    def last_error_context(self, value: dict[str, Any] | None) -> None:
        self._thread_state.last_error_context = value

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("openai package is required for live LLM calls.") from exc
        api_key = str(os.getenv(self.api_key_env, "")).strip() or "EMPTY"
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=self.timeout_sec,
            max_retries=0,
        )
        return self._client

    @staticmethod
    def _data_url_for_image(path: str) -> str | None:
        image_path = Path(path)
        if not image_path.exists():
            return None
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _strip_fences(text: str) -> str:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        return match.group(1).strip() if match else text.strip()

    def _build_user_content(self, user_prompt: str, image_paths: list[str] | None) -> Any:
        usable_paths = [str(path) for path in image_paths or [] if Path(path).exists()][: self.max_images_per_prompt]
        if not usable_paths:
            return user_prompt
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for path in usable_paths:
            data_url = self._data_url_for_image(path)
            if data_url:
                content.append({"type": "image_url", "image_url": {"url": data_url}})
        return content

    def _raw_completion(self, *, system_prompt: str, user_prompt: str, image_paths: list[str] | None, request_label: str | None) -> str:
        client = self._ensure_client()
        user_content = self._build_user_content(user_prompt, image_paths)
        self.last_error_context = {
            "request_label": request_label,
            "model": self.model,
            "base_url": self.base_url,
        }
        with self.gate.acquire():
            response = client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
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
                    text = getattr(item, "text", None)
                    if text:
                        parts.append(str(text))
            return "\n".join(parts)
        return str(content)

    def json_completion(self, system_prompt: str, user_prompt: str, *, image_paths: list[str] | None = None, request_label: str | None = None) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("LLM is disabled.")
        raw_text = self._raw_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_paths=image_paths,
            request_label=request_label,
        )
        try:
            return json.loads(self._strip_fences(raw_text))
        except json.JSONDecodeError as exc:
            self.last_error_context = {
                "request_label": request_label,
                "model": self.model,
                "base_url": self.base_url,
                "raw_text": raw_text,
            }
            raise ValueError(f"Failed to parse JSON response: {exc}") from exc
