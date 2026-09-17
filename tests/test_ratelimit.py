"""P5 令牌桶限流（api/ratelimit.py）单测。

验证：
- TokenBucket 满桶初始、消耗后失败且给出等待时间、随时间补充；
- RateLimiter 在 RATELIMIT_ENABLED=False 时直接放行；
- RateLimiter 在 memory 后端下的突发上限语义。
均不依赖 Redis。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config.settings as cs
from api.ratelimit import RateLimiter, TokenBucket


def test_bucket_full_initial():
    b = TokenBucket(rate=10, capacity=2)
    ok, wait = b.consume(2)
    assert ok and wait == 0


def test_bucket_exhaust_and_refill(monkeypatch):
    b = TokenBucket(rate=10, capacity=2)
    assert b.consume(2)[0] is True
    ok, wait = b.consume(1)
    assert ok is False and wait > 0
    # 推进时间 1s -> 补充 10 个令牌（封顶 capacity=2）
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 1.0)
    assert b.consume(1)[0] is True


def test_limiter_disabled(monkeypatch):
    monkeypatch.setattr(cs.settings, "RATELIMIT_ENABLED", False)
    lim = RateLimiter()
    ok, wait = lim.consume("user1")
    assert ok and wait == 0


def test_limiter_memory_backend(monkeypatch):
    monkeypatch.setattr(cs.settings, "RATELIMIT_ENABLED", True)
    monkeypatch.setattr(cs.settings, "RATELIMIT_BACKEND", "memory")
    monkeypatch.setattr(cs.settings, "RATELIMIT_RATE", 100.0)
    monkeypatch.setattr(cs.settings, "RATELIMIT_BURST", 1.0)
    lim = RateLimiter()
    assert lim.consume("u", tokens=1)[0] is True
    assert lim.consume("u", tokens=1)[0] is False  # 突发用完
