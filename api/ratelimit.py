"""P5 补充：令牌桶限流（内存级，开发期单进程）。

设计目标：限制"真实触发 LLM 的速率"，防止云端额度被刷爆 / 并发排队雪崩。

- 按 key（默认 user_id）隔离，不同用户互不影响；
- 令牌桶：capacity=burst，按 rate 持续补充，消耗型；
- 仅在「护栏放行 + 非缓存命中」时消耗令牌（接入点见 api/main._run，在锁之前）。

生产建议：多进程 / 多机部署需换 Redis 令牌桶（redis-py + Lua 脚本原子扣减），
否则每个 worker 各自一个桶，实际总配额 = 单桶 × worker 数。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

from config.settings import settings


class RateLimited(Exception):
    """限流触发异常，携带建议重试等待秒数。"""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__("rate limited")


class TokenBucket:
    """单个 key 的令牌桶（线程安全）。"""

    def __init__(self, rate: float, capacity: float):
        self.rate = rate          # 令牌补充速率（个/秒）
        self.capacity = capacity  # 桶容量（突发上限）
        self.tokens = capacity    # 初始满桶
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, tokens: float = 1.0) -> Tuple[bool, float]:
        """尝试取 tokens 个令牌。返回 (是否成功, 需等待秒数)。

        补充公式：now_tokens = min(capacity, tokens + elapsed * rate)
        """
        now = time.monotonic()
        with self.lock:
            elapsed = now - self.last
            if elapsed > 0:
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True, 0.0
            # 还差多少令牌，按 rate 反推需等待时长
            deficit = tokens - self.tokens
            wait = deficit / self.rate if self.rate > 0 else float("inf")
            return False, wait


class RateLimiter:
    """多 key 令牌桶管理器（懒创建，单例）。"""

    def __init__(self):
        self.rate = float(settings.RATELIMIT_RATE)
        self.burst = float(settings.RATELIMIT_BURST)
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def consume(self, key: str, tokens: float = 1.0) -> Tuple[bool, float]:
        if not settings.RATELIMIT_ENABLED:
            return True, 0.0
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self.rate, self.burst)
                self._buckets[key] = bucket
        return bucket.consume(tokens)

    def reset(self):
        """清空所有桶（测试 / 运维用）。"""
        with self._lock:
            self._buckets.clear()


_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def get_limiter() -> RateLimiter:
    """进程内单例。"""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter()
    return _limiter
