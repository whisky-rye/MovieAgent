"""O8：语义缓存——近义 query 命中同一份缓存。

思路：
- 主缓存仍是 api/cache.DualCache（按 cache_key(query,user_id,top_k) 精确命中）。
- 另建一份「query 嵌入索引」：每次缓存写入时，把 query 文本 + 其嵌入向量 + 对应 cache_key
  写入向量库独立集合（QUERY_CACHE_COLLECTION），复用已接入的 Chroma/Qdrant/Milvus 抽象层。
- 查缓存时：先精确命中；未命中则算新 query 嵌入，ANN 检索最相近的历史 query；
  相似度 ≥ SEMANTIC_CACHE_THRESHOLD 视为同义，直接取对应 cache_key 的整包结果。

降级策略（绝不阻断主链路）：
- 嵌入服务不可用 → match 返回 None，退回精确缓存 + 正常跑 Agent；
- 索引为空 / 相似度不足 → 退回正常流程；
- SEMANTIC_CACHE_ENABLED=false → 整个语义层关闭，等价于原精确缓存。

提示：语义命中只复用「候选列表」（用户已确认「仅复用候选 items」），answer 由调用方
（api/main.py）基于复用候选重跑 LLM 生成，保证解说与新 query 一致、避免 stale 文案。
"""
import logging
from typing import Any, Callable, Optional

from config.settings import settings
from api.cache import get_cache, cache_key

logger = logging.getLogger("movie_agent.semcache")


def _norm(q: str) -> str:
    """归一化：去首尾空白、压缩内部空白，用于判断是否「完全相同 query」。"""
    return " ".join((q or "").split())


def _cosine_sim_from_distance(dist: float) -> float:
    """各后端 query 统一返回 distance（越低越相似，cosine 距离=1-相似度）→ 还原相似度。"""
    return 1.0 - float(dist)


class SemanticCache:
    """精确缓存(DualCache) + query 嵌入索引(向量库) 的语义命中层。

    embed_fn: 可注入的嵌入函数 (list[str]) -> list[list[float]]，便于测试替换；
              默认走 vector.embed_client.get_embed_client().embed。
    index_backend: 可注入的向量后端（实现 upsert / query），便于测试替换；
                   默认走 vector.backend.get_vector_backend(QUERY_CACHE_COLLECTION)。
    """

    def __init__(
        self,
        embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
        index_backend=None,
        threshold: Optional[float] = None,
        enabled: Optional[bool] = None,
    ):
        self.cache = get_cache()
        self.threshold = settings.SEMANTIC_CACHE_THRESHOLD if threshold is None else threshold
        self.enabled = settings.SEMANTIC_CACHE_ENABLED if enabled is None else enabled
        self._embed_fn = embed_fn
        self._index = index_backend

    # ---- 延迟解析默认依赖，避免在模块导入期强依赖 Ollama/向量库 ----
    @property
    def embed_fn(self):
        if self._embed_fn is None:
            from vector.embed_client import get_embed_client

            client = get_embed_client()
            self._embed_fn = lambda texts: client.embed(texts)
        return self._embed_fn

    @property
    def index(self):
        if self._index is None:
            from vector.backend import get_vector_backend

            self._index = get_vector_backend(settings.QUERY_CACHE_COLLECTION)
        return self._index

    def _embed_one(self, text: str) -> Optional[list[float]]:
        try:
            vecs = self.embed_fn([text])
            return vecs[0] if vecs else None
        except Exception as e:  # noqa: BLE001 - 嵌入失败整体降级
            logger.warning("语义缓存：嵌入失败，退回精确缓存（不命中语义）: %s", e)
            return None

    def match(self, query: str, user_id: str, top_k: int,
              session_id: str = "") -> Optional[dict]:
        """语义命中查询。返回命中结构或 None。

        返回 dict：
          {"value": <缓存整包>, "exact": bool}
          exact=True 表示命中的是「完全相同」的 query（直接复用整包）；
          exact=False 表示「近义」命中（调用方应仅复用 items 并重生成 answer）。

        session_id：会话维度隔离（P0-7）。推荐 answer 在会话历史上下文下生成，
        跨会话复用会把「看过别的会话历史」的答案串到当前会话；语义索引与回源
        key 均按 (user_id, session_id) 过滤/派生，临时对话（空 session）各自共享。
        """
        if not self.enabled:
            return None
        emb = self._embed_one(query)
        if emb is None:
            return None
        try:
            rows = self.index.query(
                emb, top_k=1,
                where={"user_id": user_id, "session_id": session_id or ""},
            )
        except Exception as e:  # noqa: BLE001 - 索引不可用 → 降级
            logger.warning("语义缓存：索引检索失败，退回精确缓存: %s", e)
            return None
        if not rows:
            return None
        _id, _doc, meta, dist = rows[0]
        sim = _cosine_sim_from_distance(dist)
        if sim < self.threshold:
            return None
        key = meta.get("cache_key") if isinstance(meta, dict) else None
        if not key:
            return None
        val = self.cache.get(key)
        if val is None:
            return None
        exact = (meta.get("query", "") == _norm(query))
        return {"value": val, "exact": exact}

    def index_query(self, query: str, user_id: str, top_k: int, value: Any,
                    session_id: str = "") -> None:
        """写入一条 query→cache_key 的语义索引（连同主缓存精确写入）。

        key 派生与索引过滤均带 session_id（P0-7），与 api/main.py 的精确缓存键
        保持一致；旧索引（无 session_id 维度）自然失配，等同一次缓存预热。
        """
        if not self.enabled:
            return
        key = cache_key(query, user_id, str(top_k), session_id or "")
        self.cache.set(key, value)  # 保证精确 key 一定存在（语义命中回源用）
        emb = self._embed_one(query)
        if emb is None:
            return
        cid = f"qc:{key}"
        try:
            self.index.upsert(
                ids=[cid],
                docs=[_norm(query)],
                metas=[{"cache_key": key, "query": _norm(query),
                        "user_id": user_id, "session_id": session_id or ""}],
                vectors=[emb],
            )
        except Exception as e:  # noqa: BLE001 - 索引写入失败不影响主缓存
            logger.warning("语义缓存：索引写入失败，仅保留精确缓存: %s", e)


_SEM: Optional[SemanticCache] = None


def get_sem_cache() -> SemanticCache:
    """模块级单例（与 get_cache 一致）。"""
    global _SEM
    if _SEM is None:
        _SEM = SemanticCache()
    return _SEM
