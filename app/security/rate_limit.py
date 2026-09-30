"""In-process sliding-window rate limiter.

The robot state lives in one process (single uvicorn worker), so an in-memory
limiter is exact. If you ever scale to several workers, move this to Redis.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional

from fastapi import HTTPException, Request, status


class SlidingWindowLimiter:
    def __init__(self, max_keys: int = 50_000):
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def hit(self, key: str, limit: int, window_s: float) -> Optional[float]:
        """Record a hit. Returns None if allowed, else seconds until retry."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self._max_keys:
                    self._evict(now, window_s)
                q = self._hits[key] = deque()
            while q and now - q[0] >= window_s:
                q.popleft()
            if len(q) >= limit:
                return max(0.0, window_s - (now - q[0]))
            q.append(now)
            return None

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()

    def _evict(self, now: float, window_s: float) -> None:
        stale = [k for k, q in self._hits.items() if not q or now - q[-1] >= max(window_s, 3600)]
        for k in stale:
            del self._hits[k]
        if len(self._hits) >= self._max_keys:  # still full: drop oldest half
            for k in sorted(self._hits, key=lambda k: self._hits[k][-1] if self._hits[k] else 0)[: self._max_keys // 2]:
                del self._hits[k]


limiter = SlidingWindowLimiter()


def client_ip(request: Request) -> str:
    # request.client is already the real client when uvicorn runs with --proxy-headers and
    # --forwarded-allow-ips=<your proxy>. We never parse X-Forwarded-For ourselves (spoofable).
    return request.client.host if request.client else "unknown"


class RateLimit:
    """FastAPI dependency: `Depends(RateLimit("login", 5, 60))`."""

    def __init__(self, name: str, limit: int, window_s: float):
        self.name, self.limit, self.window_s = name, limit, window_s

    def __call__(self, request: Request) -> None:
        retry = limiter.hit(f"{self.name}:{client_ip(request)}", self.limit, self.window_s)
        if retry is not None:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests",
                                headers={"Retry-After": str(int(retry) + 1)})
