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
        self._clients: dict[tuple[str, str, int], Any] = {}
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

    @staticmethod
    def _override_value(overrides: dict[str, Any], key: str, default: Any) -> Any:
        value = overrides.get(key)
        return default if value is None or value == "" else value

    def _effective_request_config(self, llm_config: dict[str, Any] | None = None) -> dict[str, Any]:
        overrides = llm_config if isinstance(llm_config, dict) else {}
        return {
            "base_url": str(self._override_value(overrides, "base_url", self.base_url)).strip(),
            "api_key_env": str(self._override_value(overrides, "api_key_env", self.api_key_env)).strip(),
            "timeout_sec": int(self._override_value(overrides, "timeout_sec", self.timeout_sec)),
            "temperature": float(self._override_value(overrides, "temperature", self.temperature)),
            "max_images_per_prompt": int(self._override_value(overrides, "max_images_per_prompt", self.max_images_per_prompt)),
        }

    def _ensure_client(self, request_cfg: dict[str, Any] | None = None) -> Any:
        cfg = request_cfg or self._effective_request_config()
        cache_key = (str(cfg["base_url"]), str(cfg["api_key_env"]), int(cfg["timeout_sec"]))
        if cache_key in self._clients:
            return self._clients[cache_key]
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("openai package is required for live LLM calls.") from exc
        api_key = str(os.getenv(str(cfg["api_key_env"]), "")).strip() or "EMPTY"
        client = OpenAI(
            api_key=api_key,
            base_url=str(cfg["base_url"]),
            timeout=int(cfg["timeout_sec"]),
            max_retries=0,
        )
        self._clients[cache_key] = client
        if cache_key == (self.base_url, self.api_key_env, self.timeout_sec):
            self._client = client
        return client

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

    def _build_user_content(self, user_prompt: str, image_paths: list[str] | None, *, max_images_per_prompt: int | None = None) -> Any:
        image_limit = self.max_images_per_prompt if max_images_per_prompt is None else max(0, int(max_images_per_prompt))
        usable_paths = [str(path) for path in image_paths or [] if Path(path).exists()][:image_limit]
        if not usable_paths:
            return user_prompt
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for path in usable_paths:
            data_url = self._data_url_for_image(path)
            if data_url:
                content.append({"type": "image_url", "image_url": {"url": data_url}})
        return content

    def _raw_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image_paths: list[str] | None,
        request_label: str | None,
        model: str | None = None,
        llm_config: dict[str, Any] | None = None,
    ) -> str:
        request_cfg = self._effective_request_config(llm_config)
        client = self._ensure_client(request_cfg)
        user_content = self._build_user_content(
            user_prompt,
            image_paths,
            max_images_per_prompt=int(request_cfg["max_images_per_prompt"]),
        )
        model_name = str(model or self.model).strip() or self.model
        self.last_error_context = {
            "request_label": request_label,
            "model": model_name,
            "base_url": request_cfg["base_url"],
        }
        with self.gate.acquire():
            response = client.chat.completions.create(
                model=model_name,
                temperature=float(request_cfg["temperature"]),
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

    def json_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        image_paths: list[str] | None = None,
        request_label: str | None = None,
        model: str | None = None,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("LLM is disabled.")
        request_cfg = self._effective_request_config(llm_config)
        raw_text = self._raw_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_paths=image_paths,
            request_label=request_label,
            model=model,
            llm_config=request_cfg,
        )
        try:
            return json.loads(self._strip_fences(raw_text))
        except json.JSONDecodeError as exc:
            self.last_error_context = {
                "request_label": request_label,
                "model": str(model or self.model).strip() or self.model,
                "base_url": request_cfg["base_url"],
                "raw_text": raw_text,
            }
            raise ValueError(f"Failed to parse JSON response: {exc}") from exc
