"""P5 补充：令牌桶限流。

设计目标：限制"真实触发 LLM 的速率"，防止云端额度被刷爆 / 并发排队雪崩。

- 按 key（默认 user_id）隔离，不同用户互不影响；
- 令牌桶：capacity=burst，按 rate 持续补充，消耗型；
- 仅在「护栏放行 + 非缓存命中」时消耗令牌（接入点见 api/main._run，在锁之前）。

后端（RATELIMIT_BACKEND）：
- memory：进程内单例桶（开发期单进程 / Demo）。多 worker 时每进程各一桶，
          实际总配额 = 单桶 × worker 数（不精准，仅适合单进程）。
- redis  ：共享令牌桶，用 Lua 脚本原子地完成「补充 + 扣减」，多进程/多机总配额稳定；
          依赖 redis-py（已装）。Redis 不可达时自动回退 memory 并记一次告警，
          不影响服务可用性。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from config.settings import settings
from observability.metrics import metrics

logger = __import__("logging").getLogger("movie_agent.ratelimit")


class RateLimited(Exception):
    """限流触发异常，携带建议重试等待秒数。"""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__("rate limited")


class TokenBucket:
    """单个 key 的令牌桶（线程安全，内存版）。"""

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
            deficit = tokens - self.tokens
            return False, _wait_seconds(deficit, self.rate)


class RedisTokenBucket:
    """单个 key 的令牌桶（Redis 共享版，Lua 原子扣减）。

    Lua 脚本原子地完成「补充 + 判定 + 扣减」，避免 read-modify-write 竞态。
    key 结构：ratelimit:{key} 存 {tokens, ts}；TTL = 桶排空时间 + 余量，自动过期回收。
    """

    _LUA = """
    local key = KEYS[1]
    local rate = tonumber(ARGV[1])
    local capacity = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local cost = tonumber(ARGV[4])
    local d = redis.call('HMGET', key, 'tokens', 'ts')
    local tokens = d[1] and tonumber(d[1]) or capacity
    local ts = d[2] and tonumber(d[2]) or now
    local elapsed = now - ts
    if elapsed > 0 then tokens = math.min(capacity, tokens + elapsed * rate) end
    ts = now
    if tokens >= cost then
        tokens = tokens - cost
        redis.call('HMSET', key, 'tokens', tokens, 'ts', ts)
        redis.call('EXPIRE', key, math.ceil(capacity / rate) + 5)
        return {1, tokens}
    else
        return {0, tokens}
    end
    """

    def __init__(self, client, rate: float, capacity: float):
        self._r = client
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._script = client.register_script(self._LUA)

    def consume(self, key: str, tokens: float = 1.0) -> Tuple[bool, float]:
        now = time.time()
        allowed, remaining = self._script(
            keys=[f"ratelimit:{key}"],
            args=[self.rate, self.capacity, now, tokens],
        )
        if allowed:
            return True, 0.0
        deficit = tokens - float(remaining)
        return False, _wait_seconds(deficit, self.rate)


def _build_redis_client() -> Optional[object]:
    """懒加载 Redis 客户端；失败返回 None（由调用方决定回退）。"""
    try:
        import redis  # 已装 redis-py 8.x
    except Exception as exc:  # pragma: no cover - 依赖缺失时回退
        logger.warning("限流 Redis 后端不可用（redis 未安装）：回退 memory。%s", exc)
        return None
    url = settings.RATELIMIT_REDIS_URL or settings.REDIS_URL
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)
        client.ping()
        return client
    except Exception as exc:
        logger.warning(
            "限流 Redis 后端连接失败（%s）：回退 memory 后端。", exc
        )
        return None


# 内存令牌桶的 key 数硬顶（P2-16）：调试模式按 IP 限流后，恶意/扫描流量会造出
# 大量一次性 key；无淘汰时 _memory 无界增长（慢速内存泄漏），达顶后按 LRU 逐出
# 最久未用的桶（被逐出的 key 下次请求自然重建，语义无损）。
_MEMORY_MAX_KEYS = 10000

# 等待秒数上限（P2-16）：rate=0（不补充令牌）时 deficit/rate 为 inf，直接进
# Retry-After 头/提示文案会把 int(inf) 算崩（OverflowError 把 429 变 500）。
# 封顶为 1 小时——对客户端只是「稍后重试」的参考值，语义无损。
_MAX_WAIT_S = 3600.0


def _wait_seconds(deficit: float, rate: float) -> float:
    """桶排空所需的等待秒数（rate<=0 时封顶，绝不返回 inf）。"""
    if rate <= 0:
        return _MAX_WAIT_S
    return min(deficit / rate, _MAX_WAIT_S)


class RateLimiter:
    """多 key 令牌桶管理器（懒创建，单例）。

    按 settings.RATELIMIT_BACKEND 选择内存 / Redis 实现；Redis 初始化失败时
    自动降级到内存模式（仅记一次告警），保证服务不中断。
    """

    def __init__(self, rate: float | None = None, burst: float | None = None):
        # rate/burst 可覆盖（P2-17：auth 路由用独立的「错误尝试」规格，与 LLM
        # 入口的默认规格隔离）；省略时取 settings 默认值。
        self.rate = float(settings.RATELIMIT_RATE if rate is None else rate)
        self.burst = float(settings.RATELIMIT_BURST if burst is None else burst)
        self._memory: "OrderedDict[str, TokenBucket]" = OrderedDict()
        self._lock = threading.Lock()
        self._redis: Optional[RedisTokenBucket] = None
        self._redis_tried = False
        self._redis_failed = False

    def _ensure_redis(self) -> Optional[RedisTokenBucket]:
        if settings.RATELIMIT_BACKEND != "redis":
            return None
        if self._redis_tried:
            return self._redis
        self._redis_tried = True
        client = _build_redis_client()
        if client is None:
            self._redis_failed = True
            return None
        self._redis = RedisTokenBucket(client, self.rate, self.burst)
        logger.info("限流后端=redis（共享令牌桶已就绪）")
        return self._redis

    def consume(self, key: str, tokens: float = 1.0) -> Tuple[bool, float]:
        if not settings.RATELIMIT_ENABLED:
            return True, 0.0
        rb = self._ensure_redis()
        if rb is not None:
            ok, wait = rb.consume(key, tokens)
        else:
            # 内存回退（含 Redis 初始化失败的情形）
            with self._lock:
                bucket = self._memory.get(key)
                if bucket is not None:
                    self._memory.move_to_end(key)  # LRU：命中即刷新时序
                else:
                    if len(self._memory) >= _MEMORY_MAX_KEYS:
                        # 逐出最久未用的一项（被逐出的 key 下次请求自然重建）
                        self._memory.popitem(last=False)
                    bucket = TokenBucket(self.rate, self.burst)
                    self._memory[key] = bucket
            ok, wait = bucket.consume(tokens)
        # E4/O18：限流触发埋点（统一入口，覆盖全部端点）
        if not ok:
            try:
                metrics.incr("rate_limited_total", 1, {"backend": "redis" if rb is not None else "memory"})
            except Exception:  # noqa: BLE001
                pass
        return ok, wait

    def reset(self):
        """清空内存桶（测试 / 运维用）。"""
        with self._lock:
            self._memory.clear()


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
