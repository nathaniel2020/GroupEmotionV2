from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from group_emotion_video.llm import LLMConcurrencyGate, OpenAICompatibleLLM


def test_llm_concurrency_gate_limits_parallelism() -> None:
    gate = LLMConcurrencyGate(enabled=True, max_inflight_requests=2, acquire_timeout_sec=2)
    lock = threading.Lock()
    current = 0
    max_seen = 0

    def task() -> None:
        nonlocal current, max_seen
        with gate.acquire():
            with lock:
                current += 1
                max_seen = max(max_seen, current)
            time.sleep(0.05)
            with lock:
                current -= 1

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(task) for _ in range(6)]
        for future in futures:
            future.result()

    assert max_seen <= 2


def test_openai_compatible_llm_merges_per_call_config() -> None:
    llm = OpenAICompatibleLLM(
        {
            "llm": {
                "enabled": True,
                "base_url": "http://127.0.0.1:8103/v1",
                "api_key_env": "OPENAI_API_KEY",
                "model": "default-model",
                "timeout_sec": 180,
                "temperature": 0.1,
                "max_images_per_prompt": 4,
                "parallel": {"enabled": False, "max_inflight_requests": 1, "acquire_timeout_sec": 30},
            }
        }
    )

    request_cfg = llm._effective_request_config(
        {
            "base_url": "http://127.0.0.1:8109/v1",
            "api_key_env": "JUDGE_API_KEY",
            "timeout_sec": 60,
            "temperature": 0.0,
        }
    )

    assert request_cfg == {
        "base_url": "http://127.0.0.1:8109/v1",
        "api_key_env": "JUDGE_API_KEY",
        "timeout_sec": 60,
        "temperature": 0.0,
        "max_images_per_prompt": 4,
    }
