"""P2-14 回归：图谱邻接缓存消费 GRAPH_CACHE_MAX + 真 FIFO 淘汰。

修复前：
- settings.GRAPH_CACHE_MAX 定义不消费——KnowledgeGraphStore 与
  get_graph_store 都硬编码 20000，调缓存上限只能改代码；
- docstring 声称「FIFO 淘汰」，实际是达上限后整体 clear——缓存全部失效，
  引发一轮打库抖动，且「FIFO」名不副实；
- （RERANK_PROVIDER 注释矛盾已于 P1-9 修正，本项不再涉及。）

修复后：cache_max 省略时取 settings.GRAPH_CACHE_MAX（显式传入优先）；
OrderedDict + popitem(last=False) 逐出最早入库项；容量 ≤0 直通不缓存。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import graph.store as gs
from config.settings import settings


class _CountingFetcher:
    """记录 relations_from 调用次数的最小 fetcher。"""

    def __init__(self):
        self.calls = {}

    def relations_from(self, key):
        self.calls[key] = self.calls.get(key, 0) + 1
        from graph.schema import Relation
        return [Relation(key, "has_genre", "genre:科幻", 0.8)]


# ---------- settings 消费 ----------

def test_store_consumes_graph_cache_max_setting(monkeypatch):
    """cache_max 省略 → 取 settings.GRAPH_CACHE_MAX（配置项不再形同虚设）。"""
    monkeypatch.setattr(settings, "GRAPH_CACHE_MAX", 7)
    st = gs.KnowledgeGraphStore(_CountingFetcher())
    assert st._cache_max == 7


def test_explicit_cache_max_wins(monkeypatch):
    monkeypatch.setattr(settings, "GRAPH_CACHE_MAX", 7)
    st = gs.KnowledgeGraphStore(_CountingFetcher(), cache_max=3)
    assert st._cache_max == 3


def test_get_graph_store_reads_setting(monkeypatch):
    """单例工厂省略参数时消费 settings（调用时读取，非导入期绑定）。"""
    monkeypatch.setattr(settings, "GRAPH_CACHE_MAX", 11)
    monkeypatch.setattr(gs, "MysqlGraphFetcher", _CountingFetcher)
    old = gs._store_singleton
    gs._store_singleton = None
    try:
        st = gs.get_graph_store()
        assert st._cache_max == 11
    finally:
        gs._store_singleton = old


def test_get_graph_store_explicit_wins(monkeypatch):
    monkeypatch.setattr(settings, "GRAPH_CACHE_MAX", 11)
    monkeypatch.setattr(gs, "MysqlGraphFetcher", _CountingFetcher)
    old = gs._store_singleton
    gs._store_singleton = None
    try:
        assert gs.get_graph_store(cache_max=4)._cache_max == 4
    finally:
        gs._store_singleton = old


# ---------- 真 FIFO 淘汰 ----------

def test_fifo_evicts_oldest_not_clears_all():
    """超限时逐出最早入库项，其余保留（此前整体 clear → 缓存全失效）。"""
    f = _CountingFetcher()
    st = gs.KnowledgeGraphStore(f, cache_max=3)
    for k in ("douban:1", "douban:2", "douban:3"):
        st._neighbors(k)
    assert list(st._cache.keys()) == ["douban:1", "douban:2", "douban:3"]
    st._neighbors("douban:4")                       # 触发淘汰
    assert list(st._cache.keys()) == ["douban:2", "douban:3", "douban:4"]
    assert len(st._cache) == 3, "容量必须守住（不得整体清空后的 0/1）"


def test_fifo_rejected_key_refetches_newest_cached():
    """被逐出的 key 再取会重新打库；仍在缓存的 key 不再打库。"""
    f = _CountingFetcher()
    st = gs.KnowledgeGraphStore(f, cache_max=2)
    st._neighbors("douban:1")
    st._neighbors("douban:2")
    st._neighbors("douban:3")                       # 逐出 douban:1
    assert f.calls == {"douban:1": 1, "douban:2": 1, "douban:3": 1}
    st._neighbors("douban:3")                       # 命中缓存，不打库
    assert f.calls["douban:3"] == 1
    st._neighbors("douban:1")                       # 已被逐出 → 重新打库
    assert f.calls["douban:1"] == 2


def test_cache_max_zero_disables_cache():
    """容量 0 = 不缓存（直通），不应逐出空字典崩错。"""
    f = _CountingFetcher()
    st = gs.KnowledgeGraphStore(f, cache_max=0)
    st._neighbors("douban:1")
    st._neighbors("douban:1")
    assert st._cache == {}
    assert f.calls["douban:1"] == 2


def test_empty_relation_list_is_cached():
    """空出边也要缓存（负缓存），用 .get+is not None 判定而非真值。"""
    class _EmptyFetcher(_CountingFetcher):
        def relations_from(self, key):
            self.calls[key] = self.calls.get(key, 0) + 1
            return []

    f = _EmptyFetcher()
    st = gs.KnowledgeGraphStore(f, cache_max=5)
    assert st._neighbors("douban:1") == []
    assert st._neighbors("douban:1") == []
    assert f.calls["douban:1"] == 1, "空结果应命中负缓存"
