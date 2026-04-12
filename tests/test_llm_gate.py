from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from group_emotion_video.llm import LLMConcurrencyGate


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
