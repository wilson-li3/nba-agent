"""Per-client rate limiting, weighted by how expensive each route actually is.

A plain requests-per-minute cap would be wrong here: `/scores` costs nothing
while one `/betting/simulate` can fire ~17 OpenAI calls. So each route has a
credit cost and clients spend from a per-minute and a per-day allowance.

In-memory and per-process — correct for a single instance, which is what this
deploys as. Behind multiple workers, move the buckets to Redis.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.config import settings

# Credits charged per request. Roughly proportional to LLM calls downstream.
ROUTE_COST = {
    "/ask": 5,
    "/game-preview": 6,
    "/betting/simulate": 10,
    "/betting/picks": 2,
}
DEFAULT_COST = 1

# Routes that never touch the LLM — cheap to serve, no reason to throttle hard.
FREE_ROUTES = {"/health", "/usage", "/scores", "/headlines", "/"}

_minute: dict[str, deque] = defaultdict(deque)
_daily: dict[str, list] = defaultdict(lambda: [0.0, 0])  # [day_start_ts, credits]


def route_cost(path: str) -> int:
    for prefix, cost in ROUTE_COST.items():
        if path.startswith(prefix):
            return cost
    return DEFAULT_COST


def client_key(request) -> str:
    """Real client IP, honouring the proxy header a PaaS will set."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check(request) -> tuple[bool, str, int]:
    """Returns (allowed, message, retry_after_seconds)."""
    path = request.url.path
    if path in FREE_ROUTES or path.startswith("/static"):
        return True, "", 0

    cost = route_cost(path)
    key = client_key(request)
    now = time.time()

    # ── per-minute sliding window ──
    window = settings.RATE_LIMIT_WINDOW_SECONDS
    bucket = _minute[key]
    while bucket and bucket[0][0] <= now - window:
        bucket.popleft()
    spent = sum(c for _, c in bucket)
    if spent + cost > settings.RATE_LIMIT_CREDITS:
        retry = int(window - (now - bucket[0][0])) + 1 if bucket else window
        return False, (
            "You're sending requests faster than this demo allows. "
            f"Try again in {retry}s."
        ), retry

    # ── per-day allowance ──
    day = _daily[key]
    day_start = now - (now % 86400)
    if day[0] != day_start:
        day[0], day[1] = day_start, 0
    if day[1] + cost > settings.RATE_LIMIT_DAILY_CREDITS:
        return False, (
            "You've hit the daily limit for this demo. It resets at midnight UTC."
        ), 3600

    bucket.append((now, cost))
    day[1] += cost
    return True, "", 0


def snapshot() -> dict:
    now = time.time()
    window = settings.RATE_LIMIT_WINDOW_SECONDS
    active = 0
    for bucket in _minute.values():
        if bucket and bucket[-1][0] > now - window:
            active += 1
    return {
        "tracked_clients": len(_minute),
        "active_last_window": active,
        "credits_per_window": settings.RATE_LIMIT_CREDITS,
        "window_seconds": window,
        "daily_credits_per_client": settings.RATE_LIMIT_DAILY_CREDITS,
    }
