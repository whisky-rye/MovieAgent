"""P4.2 缓存：Redis 优先，故障/未启动时进程内 TTL 缓存兜底。

设计要点：
- 启动时尝试连接 Redis（protocol=2 兼容老版本 3.x）；连不上 / ping 失败 → 标记 use_redis=False，全程走进程内缓存。
- 每次 get/set 若 Redis 抛异常，自动回退本地，绝不阻塞主推荐链路。
- 单例由模块级 get_cache() 提供（uvicorn 多 worker 时每进程各一实例，开发期单进程足够）。
"""
import hashlib
import json
import logging
import time
from typing import Any, Optional

from redis import Redis

from config.settings import settings

logger = logging.getLogger("movie_agent.cache")


class DualCache:
    """优先 Redis，失败时进程内 dict（带 TTL）兜底。"""

    def __init__(
        self,
        host: str,
        port: int,
        db: int,
        password: Optional[str],
        ttl: int = 300,
        protocol: int = 2,
    ):
        self.ttl = ttl
        self._local: dict[str, tuple[float, Any]] = {}
        self.use_redis = False
        self._r: Optional[Redis] = None
        try:
            self._r = Redis(
                host=host,
                port=port,
                db=db,
                password=password or None,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
                protocol=protocol,
            )
            self._r.ping()
            self.use_redis = True
            logger.info("DualCache: 使用 Redis 后端 %s:%s/db%s", host, port, db)
        except Exception as e:  # noqa: BLE001 - 任何连接/协议错误都走兜底
            self._r = None
            self.use_redis = False
            logger.warning("DualCache: Redis 不可用，启用进程内兜底缓存：%s", e)

    def get(self, key: str) -> Optional[Any]:
        if self.use_redis and self._r is not None:
            try:
                raw = self._r.get(key)
                if raw is not None:
                    return json.loads(raw)
            except Exception as e:  # noqa: BLE001
                logger.warning("DualCache: Redis get 失败，转本地：%s", e)
        # 本地兜底
        item = self._local.get(key)
        if item is not None and item[0] > time.time():
            return item[1]
        self._local.pop(key, None)
        return None

    def set(self, key: str, value: Any) -> None:
        if self.use_redis and self._r is not None:
            try:
                self._r.set(key, json.dumps(value, ensure_ascii=False), ex=self.ttl)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("DualCache: Redis set 失败，转本地：%s", e)
        self._local[key] = (time.time() + self.ttl, value)

    def health(self) -> bool:
        if self.use_redis and self._r is not None:
            try:
                return bool(self._r.ping())
            except Exception:  # noqa: BLE001
                return False
        return False

    def backend(self) -> str:
        return "redis" if self.use_redis else "local"


def _make_cache() -> DualCache:
    return DualCache(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD,
        ttl=settings.REDIS_TTL,
    )


_CACHE: Optional[DualCache] = None


def get_cache() -> DualCache:
    """模块级单例。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = _make_cache()
    return _CACHE


def cache_key(*parts: str) -> str:
    """对多个字符串拼接做 md5，作为缓存 key（避免中文/特殊字符直接进 key）。"""
    h = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()
    return f"recall:{h}"
