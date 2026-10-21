"""知识图谱多跳召回（F1/O12）单元测试。

用 FakeFetcher 注入替代 MySQL，验证：
- 多跳 BFS：影片→人物→影片 + 可选类型/地区扩展；
- 路径可解释（人类可读关系链）；
- 种子影片被排除、按综合分排序、top_k 截断；
- 实体中心召回（人物名 → 作品 + 合作网络）；
- 邻接缓存容量上限不崩溃。
集成验证（需 MySQL 在线）见 test_knowledge_graph_live（离线自动跳过）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from graph.schema import Relation, ENTITY_MOVIE, ENTITY_PERSON
from graph.store import KnowledgeGraphStore
from graph.recall import graph_recall, graph_recall_by_entity
from retrieval.candidates import Candidate


class FakeFetcher:
    """最小图：M0(导演D) → D → M1(导演D,主演A) ；M1 → A → M2(主演A) ；类型 科幻 → M0,M9。"""

    def relations_from(self, key):
        G = "genre:科幻"
        if key == "douban:1":  # 星际穿越
            return [
                Relation("douban:1", "directed_by", "douban_name:10", 2.5, role="director"),
                Relation("douban:1", "has_genre", G, 0.8),
            ]
        if key == "douban_name:10":  # 诺兰
            return [
                Relation("douban_name:10", "directed_by", "douban:1", 2.5, role="director"),
                Relation("douban_name:10", "directed_by", "douban:2", 2.5, role="director"),
            ]
        if key == "douban:2":  # 盗梦空间
            return [
                Relation("douban:2", "directed_by", "douban_name:10", 2.5, role="director"),
                Relation("douban:2", "acted_by", "douban_name:20", 1.0, role="cast"),
            ]
        if key == "douban_name:20":  # 小李子
            return [
                Relation("douban_name:20", "acted_by", "douban:2", 1.0, role="cast"),
                Relation("douban_name:20", "acted_by", "douban:3", 1.0, role="cast"),
            ]
        if key == "douban:3":  # 泰坦尼克号
            return [Relation("douban:3", "acted_by", "douban_name:20", 1.0, role="cast")]
        if key == G:
            return [
                Relation(G, "has_genre", "douban:1", 0.8),
                Relation(G, "has_genre", "douban:9", 0.8),
            ]
        if key == "douban:9":
            return [Relation("douban:9", "has_genre", G, 0.8)]
        return []

    def movie_meta(self, ids):
        M = {
            "douban:1": {"source": "douban", "title": "星际穿越", "year": 2014, "rating": 9.0,
                         "rating_count": 100, "genres": "科幻", "country": "美国", "douban_id": "1"},
            "douban:2": {"source": "douban", "title": "盗梦空间", "year": 2010, "rating": 9.2,
                         "rating_count": 200, "genres": "科幻", "country": "美国", "douban_id": "2"},
            "douban:3": {"source": "douban", "title": "泰坦尼克号", "year": 1997, "rating": 9.5,
                         "rating_count": 300, "genres": "爱情", "country": "美国", "douban_id": "3"},
            "douban:9": {"source": "douban", "title": "银翼杀手", "year": 1982, "rating": 8.5,
                         "rating_count": 50, "genres": "科幻", "country": "美国", "douban_id": "9"},
        }
        return {k: M[k] for k in ids if k in M}

    def person_label(self, k):
        return {"douban_name:10": "诺兰", "douban_name:20": "小李子"}.get(k, "")

    def search_person(self, name, limit=5):
        return ["douban_name:10"] if "诺兰" in name else []


def _store(cache_max=20000):
    return KnowledgeGraphStore(FakeFetcher(), cache_max=cache_max)


def test_multi_hop_finds_director_other_film_and_genre():
    st = _store()
    hits = st.traverse(["douban:1"], max_hops=2, top_k=10)
    ids = [h.item_id for h in hits]
    assert "douban:1" not in ids            # 种子被排除
    assert "douban:2" in ids                # 同导演其它作品（2 跳）
    assert "douban:9" in ids                # 同类型扩展（2 跳）


def test_path_explanation_readable():
    st = _store()
    hits = st.traverse(["douban:1"], max_hops=2, top_k=10)
    by_id = {h.item_id: h for h in hits}
    assert "《星际穿越》 → 导演:诺兰 → 《盗梦空间》" in by_id["douban:2"].paths


def test_hop_depth_limits_reach():
    st = _store()
    # 泰坦尼克号需 4 跳才能从 M0 到达（M0→D→M1→A→M3），max_hops=2 不应出现
    hits = st.traverse(["douban:1"], max_hops=2, top_k=10)
    assert "douban:3" not in [h.item_id for h in hits]
    # max_hops=4 则出现
    hits4 = st.traverse(["douban:1"], max_hops=4, top_k=10)
    assert "douban:3" in [h.item_id for h in hits4]


def test_entity_recall_from_person():
    st = _store()
    hits = st.traverse(["douban_name:10"], max_hops=1, top_k=10)
    ids = [h.item_id for h in hits]
    assert "douban:1" in ids and "douban:2" in ids
    assert hits[0].paths and hits[0].paths[0].startswith("人物:诺兰")


def test_cache_cap_no_crash():
    st = _store(cache_max=2)  # 极小缓存，触发 FIFO 淘汰路径
    st.traverse(["douban:1"], max_hops=2, top_k=10)
    assert len(st._cache) <= 2 + 1


def test_graph_recall_returns_candidates_with_paths():
    from config.settings import settings
    settings.GRAPH_ENABLED = True
    cands = graph_recall("douban:1", top_k=10, store=_store())
    assert cands, "graph_recall 应返回候选"
    assert all(c.recall_source == "graph" for c in cands)
    assert all(isinstance(c.graph_paths, list) and c.graph_paths for c in cands)
    assert "douban:1" not in [c.id for c in cands]


def test_graph_recall_disabled_returns_empty():
    from config.settings import settings
    prev = settings.GRAPH_ENABLED
    settings.GRAPH_ENABLED = False
    try:
        assert graph_recall("douban:1", store=_store()) == []
    finally:
        settings.GRAPH_ENABLED = prev


def test_graph_recall_by_entity():
    from config.settings import settings
    settings.GRAPH_ENABLED = True
    cands = graph_recall_by_entity("诺兰", store=_store())
    assert cands, "实体中心召回应返回候选"
    assert all(c.recall_source == "graph" for c in cands)
