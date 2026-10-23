"""P1-6 / P1-7 / P1-8 修复回归（一把梭三个任务）。

P1-6 跨源归并键缺失：
- graph/store.py:MysqlGraphFetcher.movie_meta 豆瓣分支 SELECT 补 imdb_id
  （graph_recall 与 person_recall 的共同源头）；
- retrieval/relation_recall.py:_relation_douban 豆瓣候选补 imdb_id；
→ work_key 优先 imdb_id 归并双源同片，缺它则 similar 意图下同一部豆瓣影片
  出现两条、曝光/反馈双计。

P1-7 单路召回隔离 + 边界：
- agent/tools.py:_safe_recall 包裹 by_text/similar 三路/graph，单路失败
  降级空列表（此前仅 person/prod 有降级，similar 三路裸调）；
- retrieval/content_recall.py：item_id 不在向量库时 get_embeddings 返回空
  → 返回 []（此前 vecs[0] IndexError 整个请求 500）。

P1-8 静默吞异常留痕：
- graph.py 三处裸 except（_parse_intent / _resolve_item_id / _reflect）
  补 warning 日志，_reflect 另落失败 step。

测试要点：SQL 层修复用「列感知假 fetch」——只返回 SELECT 中实际出现的列，
SELECT 漏列会直接导致断言失败，杜绝假通过。
"""
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib

import pytest

from agent import graph as graph_mod
from agent import tools as tools_mod
from graph.recall import graph_recall_by_entity, person_recall
from graph.store import KnowledgeGraphStore, MysqlGraphFetcher
from retrieval.candidates import Candidate, work_key

# 用 importlib 直接取子模块对象（retrieval/__init__ 把同名函数重导出，
# 「from retrieval import xxx」会绑定到函数而非模块）
rrr = importlib.import_module("retrieval.relation_recall")
ccr = importlib.import_module("retrieval.content_recall")


def _column_aware_row(sql: str, full_row: dict) -> dict:
    """按 SQL 的 SELECT 列清单裁剪行——漏选列等于数据库不返回该列。"""
    m = re.search(r"select\s+(.+?)\s+from", sql, re.I | re.S)
    if not m:
        return dict(full_row)
    cols = [c.strip() for c in m.group(1).split(",")]
    return {c: full_row[c] for c in cols if c in full_row}


# ---------------------------------------------------------------------------
# P1-6：movie_meta / relation_recall 豆瓣候选带 imdb_id
# ---------------------------------------------------------------------------

_DOUBAN_ROW = {
    "id": 10, "douban_id": "999", "imdb_id": "tt999",
    "title_text_cn": "导演片", "original_title": None,
    "release_date_year": 2001, "rating": 8.0, "rating_count": 100,
    "genres": "剧情", "country": "中国",
}


def test_movie_meta_douban_includes_imdb_id():
    """movie_meta 豆瓣分支必须选出并透传 imdb_id（graph/person 路径共同源头）。"""
    f = object.__new__(MysqlGraphFetcher)  # 绕过 __init__ 的建连探测
    seen = {}

    def _fake_fetch(sql, params):
        seen["sql"] = sql
        return [_column_aware_row(sql, _DOUBAN_ROW)]

    f._fetch = _fake_fetch
    meta = f.movie_meta(["douban:10"])
    assert meta["douban:10"]["imdb_id"] == "tt999", \
        f"movie_meta 未透传 imdb_id（SELECT 可能漏列）: {seen.get('sql', '')[:120]}"


def test_relation_recall_douban_includes_imdb_id(monkeypatch):
    """relation_recall 豆瓣候选必须带 imdb_id，与 content 召回同片可归并。"""

    def _fake_fetch(sql, params):
        if "credits_type" in sql and "SUM(" not in sql:
            return [{"name_id": "p1", "credits_type": "director"}]
        if "SUM(" in sql:
            return [{"did": "999", "shared": 2.5}]
        if "douban_main WHERE douban_id" in sql:
            return [_column_aware_row(sql, _DOUBAN_ROW)]
        if "SELECT douban_id FROM douban_main WHERE id" in sql:
            return [{"douban_id": "555"}]
        return []

    monkeypatch.setattr(rrr, "fetch_all", _fake_fetch)
    out = rrr.relation_recall("douban:1", top_k=5)
    assert out and out[0].douban_id == "999"
    assert out[0].imdb_id == "tt999", "relation 豆瓣候选缺 imdb_id（work_key 无法归并）"
    # 带 imdb_id 后，与 content 召回的同片候选归并键一致
    other = Candidate(id="douban:10", source="douban", title="导演片", imdb_id="tt999")
    assert work_key(out[0]) == work_key(other) == "imdb:tt999"


class _R:
    def __init__(self, subj, pred, obj, weight, role=""):
        self.subj, self.pred, self.obj, self.weight, self.role = subj, pred, obj, weight, role


class _ImdbFakeFetcher:
    """带 imdb_id 的最小图 fetcher：诺兰 → douban:1。"""

    def relations_from(self, key):
        if key == "douban_name:10":
            return [_R(key, "directed_by", "douban:1", 2.5, role="director")]
        return []

    def movie_meta(self, ids):
        M = {"douban:1": {"source": "douban", "title": "星际穿越", "year": 2014,
                          "rating": 9.0, "rating_count": 100, "genres": "科幻",
                          "country": "美国", "douban_id": "1", "imdb_id": "tt0816692"}}
        return {k: M[k] for k in ids if k in M}

    def person_label(self, person_key_):
        return {"douban_name:10": "诺兰"}.get(person_key_, "")

    def search_person(self, name, limit=5):
        return ["douban_name:10"] if "诺兰" in name else []


def test_person_filmography_hits_carry_imdb_id(monkeypatch):
    """person_recall 产出的候选必须带 imdb_id（走 movie_meta 同源修复）。"""
    monkeypatch.setattr("graph.recall.get_graph_store",
                        lambda: KnowledgeGraphStore(_ImdbFakeFetcher()))
    cands = person_recall("诺兰", top_k=5)
    assert cands and cands[0].title == "星际穿越"
    assert cands[0].imdb_id == "tt0816692"


def test_graph_recall_by_entity_carries_imdb_id(monkeypatch):
    monkeypatch.setattr("graph.recall.get_graph_store",
                        lambda: KnowledgeGraphStore(_ImdbFakeFetcher()))
    cands = graph_recall_by_entity("诺兰", top_k=5)
    assert cands and cands[0].imdb_id == "tt0816692"


# ---------------------------------------------------------------------------
# P1-7：单路召回隔离 + item_id 向量缺失边界
# ---------------------------------------------------------------------------

class _EmptyVecBackend:
    """向量库里没有目标 item：get_embeddings 返回空列表。"""

    def get_embeddings(self, ids):
        return []

    def query(self, *a, **k):
        raise AssertionError("取不到向量时不应继续 query")


class _Emb:
    def embed_query(self, text):
        return [0.1, 0.2]


def test_content_recall_missing_item_returns_empty(monkeypatch):
    """item 已不在向量库 → 返回 []，不得 vecs[0] IndexError（P1-7）。"""
    monkeypatch.setattr(ccr, "get_vector_backend", lambda: _EmptyVecBackend())
    monkeypatch.setattr(ccr, "get_embed_client", lambda: _Emb())
    assert ccr.content_recall(item_id="douban:404", top_k=5) == []


def test_run_search_similar_isolates_failing_path(monkeypatch):
    """similar 三路中一路炸 → 只丢该路，其余路候选仍进融合结果（P1-7）。"""
    good = Candidate(id="douban:2", source="douban", title="盗梦空间",
                     recall_source="relation", score=1.0)

    def _boom(**kwargs):
        raise RuntimeError("vector store down")

    monkeypatch.setattr(tools_mod, "content_recall", _boom)
    monkeypatch.setattr(tools_mod, "relation_recall", lambda *a, **k: [good])
    monkeypatch.setattr(tools_mod, "graph_recall", lambda *a, **k: [])
    out = tools_mod._run_search_similar("douban:1", top_k=5)
    assert [c.id for c in out] == ["douban:2"]


def test_run_search_similar_all_fail_returns_empty(monkeypatch):
    """三路全挂 → 空列表（交 reflect 冷启动兜底），不抛异常。"""

    def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(tools_mod, "content_recall", _boom)
    monkeypatch.setattr(tools_mod, "relation_recall", _boom)
    monkeypatch.setattr(tools_mod, "graph_recall", _boom)
    assert tools_mod._run_search_similar("douban:1", top_k=5) == []


def test_run_search_by_text_isolates_failure(monkeypatch):
    """主内容路失败也降级空列表（由冷启动兜底接住），不再整请求 500。"""

    def _boom(**kwargs):
        raise RuntimeError("embed down")

    monkeypatch.setattr(tools_mod, "content_recall", _boom)
    assert tools_mod._run_search_by_text({"query": "科幻"}, {}, top_k=5) == []


# ---------------------------------------------------------------------------
# P1-8：静默吞异常必须留痕
# ---------------------------------------------------------------------------

def test_parse_intent_failure_logs_warning(monkeypatch, caplog):
    """意图解析 LLM 失败：走启发式 + warning 留痕（曾零日志）。"""

    class _BoomLLM:
        def invoke(self, *a, **k):
            raise RuntimeError("cloud all down")

    monkeypatch.setattr(graph_mod, "chat_light", lambda *a, **k: _BoomLLM())
    with caplog.at_level(logging.WARNING):
        out = graph_mod._parse_intent({"user_id": "u", "query": "诺兰的电影", "request_id": ""})
    assert out["intent"] == "by_person"  # 启发式兜底仍然工作
    assert any("意图解析" in r.message or "启发式" in r.message
               for r in caplog.records), "失败未留痕"


def test_resolve_item_id_failure_logs_warning(monkeypatch, caplog):
    """作品定位失败：退化为 None + warning 留痕（曾零日志）。"""

    def _boom(**kwargs):
        raise RuntimeError("vector store timeout")

    monkeypatch.setattr(graph_mod, "content_recall", _boom)
    with caplog.at_level(logging.WARNING):
        assert graph_mod._resolve_item_id("星际穿越") is None
    assert any("定位失败" in r.message for r in caplog.records), "失败未留痕"


def test_reflect_fallback_failure_logs_warning_and_step(monkeypatch, caplog):
    """冷启动兜底失败：warning + 失败 step（曾裸 return {}）。"""

    class _FakeStore:
        def get_prefs(self, uid):
            return {}

    def _boom(*a, **k):
        raise RuntimeError("board query failed")

    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: _FakeStore())
    monkeypatch.setitem(graph_mod.RUNNERS, "high_quality_board", _boom)
    state = {"user_id": "u", "query": "q", "slots": {}, "ranked": [], "candidates": {}}
    with caplog.at_level(logging.WARNING):
        out = graph_mod._reflect(state)
    assert any("兜底失败" in r.message for r in caplog.records), "失败未留痕"
    assert out.get("steps") and out["steps"][-1]["summary"] == "冷启动兜底失败"
