"""O8：语义缓存单元测（注入 fake 嵌入 + fake 向量索引，无需 Ollama/Chroma）。

验证：精确命中、近义命中（仅复用候选→exact=False）、不命中、跨用户隔离、嵌入失败降级。
"""
import importlib
from typing import List, Optional

import pytest


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class FakeIndex:
    """内存向量索引：按 user_id 过滤，返回最近邻 (id,doc,meta,distance)。"""

    def __init__(self):
        self.rows = []

    def upsert(self, ids, docs, metas, vectors):
        for i, d, m, v in zip(ids, docs, metas, vectors):
            self.rows.append((i, d, m, v))

    def query(self, vector, top_k=1, where=None, genres=None):
        best = None
        for (i, d, m, v) in self.rows:
            if where and m.get("user_id") != where.get("user_id"):
                continue
            sim = _cosine(v, vector)
            dist = 1.0 - sim
            if best is None or dist < best[3]:
                best = (i, d, m, dist)
        return [best] if best else []


# 确定性嵌入：相同/近义映射到相近向量，无关 query 映射到正交向量
_VECS = {
    "科幻片": [1.0, 0.0, 0.0],
    "科幻电影": [0.95, 0.1, 0.0],   # 与「科幻片」余弦≈0.994，高于阈值 0.9
    "太空科幻片": [0.9, 0.2, 0.0],  # 余弦≈0.983，近义
    "爱情片": [0.0, 1.0, 0.0],      # 余弦 0，不命中
}


def _embed(texts: List[str]) -> List[List[float]]:
    return [_VECS.get(t, [0.0, 0.0, 1.0]) for t in texts]


@pytest.fixture
def sem():
    api_sem = importlib.import_module("api.semcache")
    idx = FakeIndex()
    return api_sem.SemanticCache(
        embed_fn=_embed, index_backend=idx, threshold=0.9, enabled=True
    ), idx


def _sample_value(items):
    return {
        "items": items,
        "intent": "rec",
        "answer": "推荐如下",
        "sources": {},
        "steps": [],
        "thinking": {},
    }


def test_exact_hit(sem):
    cache, idx = sem
    val = _sample_value([{"id": "imdb:1", "title": "X", "genres": ["科幻"]}])
    cache.index_query("科幻片", "u1", 10, val)
    m = cache.match("科幻片", "u1", 10)
    assert m is not None
    assert m["exact"] is True
    assert m["value"]["items"][0]["id"] == "imdb:1"


def test_near_hit_returns_exact_false(sem):
    cache, idx = sem
    val = _sample_value([{"id": "imdb:1", "title": "X", "genres": ["科幻"]}])
    cache.index_query("科幻片", "u1", 10, val)
    m = cache.match("科幻电影", "u1", 10)  # 近义，非完全相同
    assert m is not None
    assert m["exact"] is False
    assert m["value"]["items"][0]["id"] == "imdb:1"


def test_no_hit_when_dissimilar(sem):
    cache, idx = sem
    val = _sample_value([{"id": "imdb:1"}])
    cache.index_query("科幻片", "u1", 10, val)
    assert cache.match("爱情片", "u1", 10) is None


def test_cross_user_isolated(sem):
    cache, idx = sem
    val = _sample_value([{"id": "imdb:1"}])
    cache.index_query("科幻片", "u1", 10, val)
    # 另一用户查询，即使语义相近也不复用（where 过滤）
    m = cache.match("科幻电影", "u2", 10)
    assert m is None


def test_embed_failure_degrades_gracefully():
    api_sem = importlib.import_module("api.semcache")

    def _boom(texts):
        raise RuntimeError("embed down")

    cache = api_sem.SemanticCache(
        embed_fn=_boom, index_backend=FakeIndex(), threshold=0.9, enabled=True
    )
    # 索引写入时嵌入失败，仅落精确缓存、不抛错
    val = _sample_value([{"id": "imdb:1"}])
    cache.index_query("科幻片", "u1", 10, val)  # 不应抛异常
    # 嵌入失败 → 语义层不命中（退回精确/正常流程）
    assert cache.match("科幻片", "u1", 10) is None


def test_disabled_returns_none():
    api_sem = importlib.import_module("api.semcache")
    cache = api_sem.SemanticCache(
        embed_fn=_embed, index_backend=FakeIndex(), threshold=0.9, enabled=False
    )
    val = _sample_value([{"id": "imdb:1"}])
    cache.index_query("科幻片", "u1", 10, val)
    assert cache.match("科幻片", "u1", 10) is None
