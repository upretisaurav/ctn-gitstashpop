"""Token buckets that mirror the team-wide limits, so the bot paces itself
instead of being refused. The package also sleeps through 429s, so this is
about spending each slot on the best action, not about correctness."""

from __future__ import annotations

import threading
import time


class Bucket:
    def __init__(self, per_minute: float, burst: float | None = None):
        self.rate = per_minute / 60.0
        self.capacity = burst if burst is not None else max(1.0, per_minute)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.blocked_until = 0.0
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def available(self) -> bool:
        with self._lock:
            if time.monotonic() < self.blocked_until:
                return False
            self._refill()
            return self.tokens >= 1.0

    def take(self) -> bool:
        with self._lock:
            if time.monotonic() < self.blocked_until:
                return False
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    def wait_take(self, timeout: float = 120.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.take():
                return True
            time.sleep(0.25)
        return False

    def seconds_until_available(self) -> float:
        with self._lock:
            self._refill()
            wait = max(0.0, self.blocked_until - time.monotonic())
            if self.tokens >= 1.0:
                return wait
            return max(wait, (1.0 - self.tokens) / self.rate)

    def block_for(self, seconds: float) -> None:
        with self._lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)
            self.tokens = 0.0


class Limits:
    def __init__(self, cfg):
        # Writes: small burst so we never queue a backlog behind the server.
        self.write = Bucket(cfg.writes_per_min, burst=1)
        self.like = Bucket(cfg.likes_per_min, burst=5)
        self.follow = Bucket(cfg.follows_per_min, burst=5)
        self.report_post = Bucket(cfg.reports_per_min, burst=1)
        self.report_account = Bucket(cfg.reports_per_min, burst=1)
        self.feed = Bucket(cfg.feeds_per_min, burst=2)
        self.read = Bucket(cfg.reads_per_min, burst=20)
        self.login = Bucket(cfg.logins_per_min, burst=1)
