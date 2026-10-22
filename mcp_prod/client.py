"""生产库只读通道的项目侧消费封装。

定位（调研报告 §5）：MCP 是「补充/校验通道」，**主召回仍走本地强 MySQL + 向量库**。
因此本模块的设计原则是**永不阻断主链路**：

- `MCP_ENABLED=false`（默认）时全部方法直接返回 None，调用方等价于「该路召回不存在」；
- 远端不可达 / 熔断 / 限流 / 查询失败 → 捕获并返回 None，**不抛异常给业务层**；
- 短 TTL 缓存（默认 60s）降低对生产库的重复压力；
- 转换出的 `Candidate` 携带 `imdb_id`，可与本地召回结果做跨源归并（work_key）。

🔴 本模块不产生任何写操作；底层 SQL 仍受 `mcp_prod/guard.py` 约束。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from config.settings import settings
from retrieval.candidates import Candidate
from mcp_prod import queries as Q
from mcp_prod.connection import (
    McpCircuitOpen,
    McpError,
    McpNotConfigured,
    McpRateLimited,
    McpReadOnlyViolation,
    get_prod_client,
)

logger = logging.getLogger("movie_agent.mcp")


class ProdChannel:
    """生产库只读通道（进程内调用，带缓存与降级）。"""

    def __init__(self, client=None):
        self._client = client
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # —— 基础 ——
    @property
    def enabled(self) -> bool:
        return bool(settings.MCP_ENABLED)

    @property
    def client(self):
        if self._client is None:
            self._client = get_prod_client()
        return self._client

    def is_available(self) -> bool:
        """通道是否可用（已启用 + 已配置凭据 + 未熔断）。"""
        if not self.enabled:
            return False
        try:
            return self.client.configured and not self.client.health().get("circuit_open")
        except Exception:  # noqa: BLE001
            return False

    def _cache_get(self, key: str):
        with self._lock:
            item = self._cache.get(key)
            if not item:
                return None
            expire, val = item
            if expire < time.time():
                self._cache.pop(key, None)
                return None
            return val

    def _cache_put(self, key: str, value) -> None:
        ttl = max(0, int(settings.MCP_CACHE_TTL))
        if ttl <= 0:
            return
        with self._lock:
            self._cache[key] = (time.time() + ttl, value)
            # 简易容量控制：防止无界增长
            if len(self._cache) > 512:
                now = time.time()
                expired = [k for k, (exp, _) in self._cache.items() if exp < now]
                for k in expired:
                    self._cache.pop(k, None)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _call(self, tool: str, fn, *args, **kwargs):
        """统一调用封装：异常一律降级为 None（并记日志）。"""
        if not self.enabled:
            return None
        try:
            return fn(*args, **kwargs)
        except McpReadOnlyViolation as e:
            logger.error("MCP 只读护栏拦截（已阻断，未触达远端）: %s", e)
        except McpRateLimited as e:
            logger.warning("MCP 限流，降级本地召回: %s", e)
        except McpCircuitOpen as e:
            logger.warning("MCP 熔断，降级本地召回: %s", e)
        except McpNotConfigured as e:
            logger.warning("MCP 未配置凭据，降级本地召回: %s", e)
        except McpError as e:
            logger.warning("MCP 通道错误，降级本地召回: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP 通道异常，降级本地召回: %s", str(e)[:200])
        return None

    # —— 工具封装 ——
    def search_movie(self, keyword: str, source: str = "douban",
                     limit: int = 10) -> Optional[list[Candidate]]:
        """按标题检索（返回统一 Candidate；不可用时返回 None）。"""
        key = f"search_movie|{source}|{keyword}|{limit}"
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached)
        data = self._call("search_movie", Q.search_movie, keyword, source, limit,
                          client=self.client)
        if not data:
            return None
        cands = [_to_candidate(i) for i in data.get("items", [])]
        cands = [c for c in cands if c is not None]
        self._cache_put(key, cands)
        return cands

    def get_movie_detail(self, source: str, movie_id: str) -> Optional[dict]:
        key = f"detail|{source}|{movie_id}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        data = self._call("get_movie_detail", Q.get_movie_detail, source, movie_id,
                          client=self.client)
        if not data:
            return None
        self._cache_put(key, data)
        return data

    def get_movie_relations(self, source: str, movie_id: str,
                            limit: int = 20) -> Optional[list[Candidate]]:
        key = f"relations|{source}|{movie_id}|{limit}"
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached)
        data = self._call("get_movie_relations", Q.get_movie_relations,
                          source, movie_id, limit, client=self.client)
        if not data:
            return None
        cands = [_to_candidate(i) for i in data.get("items", [])]
        cands = [c for c in cands if c is not None]
        self._cache_put(key, cands)
        return cands

    def compare_cross_source(self, keyword: str = "", limit: int = 20) -> Optional[list[dict]]:
        key = f"compare|{keyword}|{limit}"
        cached = self._cache_get(key)
        if cached is not None:
            return list(cached)
        data = self._call("compare_cross_source", Q.compare_cross_source, keyword, limit,
                          client=self.client)
        if not data:
            return None
        items = data.get("items", [])
        self._cache_put(key, items)
        return items

    def health(self) -> dict:
        """通道健康状态（供监控/看板）。"""
        info = {"enabled": self.enabled, "available": False}
        try:
            info["available"] = self.is_available()
            info.update(self.client.health())
        except Exception as e:  # noqa: BLE001
            info["error"] = str(e)[:200]
        return info


def _to_candidate(item: dict) -> Optional[Candidate]:
    """远端归一化影片 → 本项目 Candidate。

    id 约定与本地一致：douban 源用 `douban:{PK}`，imdb 源用 `imdb:{imdb_id}`；
    同时携带 imdb_id/douban_id，便于与本地召回结果做跨源归并（work_key）。
    """
    if not item:
        return None
    source = item.get("source") or ""
    if source == "douban":
        pk = item.get("id") or ""
        if not pk:
            return None
        cid = f"douban:{pk}"
    elif source == "imdb":
        iid = item.get("imdb_id") or item.get("source_id") or ""
        if not iid:
            return None
        cid = f"imdb:{iid}"
    else:
        return None
    genres = item.get("genres") or []
    countries = item.get("country") or []
    return Candidate(
        id=cid,
        source=source,
        title=item.get("title") or "",
        year=int(item.get("year") or 0),
        genres=[g for g in genres if g],
        country=countries[0] if countries else "",
        rating=float(item.get("rating") or 0.0),
        rating_count=int(item.get("rating_count") or 0),
        recall_source="prod",
        score=float(item.get("score") or 0.0),
        douban_id=str(item.get("douban_id") or ""),
        imdb_id=str(item.get("imdb_id") or ""),
    )


#: 全局通道单例
_CHANNEL: Optional[ProdChannel] = None
_CHANNEL_LOCK = threading.Lock()


def get_prod_channel() -> ProdChannel:
    global _CHANNEL
    if _CHANNEL is None:
        with _CHANNEL_LOCK:
            if _CHANNEL is None:
                _CHANNEL = ProdChannel()
    return _CHANNEL


def reset_prod_channel() -> None:
    """重置单例（测试用）。"""
    global _CHANNEL
    with _CHANNEL_LOCK:
        if _CHANNEL is not None:
            _CHANNEL.clear_cache()
        _CHANNEL = None
