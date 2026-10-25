"""P2-8 回归：similar 意图取消「双重融合」，三路原始列表交外层 RRF 统一融合。

修复前：`_run_search_similar` 内层先做 score 预融合（min-max）再当单路返回，
graph._execute 把它存为 recall["similar"]，外层 RRF 经 _PLAN_TO_CANON 把整包
当 content(1.0)——relation 0.8 / graph 0.7 的配置权重实际失效，且 min-max
的量级脆弱性在该路径复活。

修复后：runner 返回 {content, relation, graph} 三路原始列表，_execute 展开进
顶层 recall，与 by_text 等路径走同一条 RRF 链路（源权重生效、量级免疫）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import agent.tools as tools_mod
from agent import graph as graph_mod
from ranker.rrf import rrf_fuse
from retrieval.candidates import Candidate


class _FakeStore:
    def get_prefs(self, uid):
        return {}

    def sediment_preferences(self, uid, **kw):
        return {}


def _c(i, score):
    return Candidate(id=f"douban:{i}", source="douban", title=f"片{i}",
                     year=2000, genres=["剧情"], score=score)


# ---------- runner 契约 ----------

def test_runner_returns_three_raw_lists(monkeypatch):
    sim = [_c(1, 0.9)]
    rel = [_c(2, 5.0)]
    graph = [_c(3, 1.2)]

    monkeypatch.setattr(tools_mod, "content_recall", lambda **kw: sim)
    monkeypatch.setattr(tools_mod, "relation_recall", lambda *a, **kw: rel)
    monkeypatch.setattr(tools_mod, "graph_recall", lambda *a, **kw: graph)
    out = tools_mod._run_search_similar("douban:1", top_k=5)
    assert out == {"content": sim, "relation": rel, "graph": graph}


def test_runner_no_inner_rerank(monkeypatch):
    """runner 内不再调 rerank（内层预融合已移除）。"""
    monkeypatch.setattr(tools_mod, "content_recall", lambda **kw: [_c(1, 0.9)])
    monkeypatch.setattr(tools_mod, "relation_recall", lambda *a, **kw: [])
    monkeypatch.setattr(tools_mod, "graph_recall", lambda *a, **kw: [])

    def _boom(*a, **k):
        raise AssertionError("similar runner 不应再触发内层 rerank")

    monkeypatch.setattr(tools_mod, "rerank", _boom)
    tools_mod._run_search_similar("douban:1", top_k=5)


# ---------- graph._execute 展开 ----------

def test_execute_expands_similar_into_three_sources(monkeypatch):
    """_execute 把 similar 的三路展开进顶层 recall（不再有 "similar" 整包键）。"""
    captured = {}

    def _fake_rerank(recall, **kw):
        captured["recall"] = {k: list(v) for k, v in recall.items()}
        return {"ranked": [], "fusion_detail": {}}

    monkeypatch.setattr(graph_mod, "rerank", _fake_rerank)
    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: _FakeStore())
    monkeypatch.setattr(graph_mod, "_feedback_profile_for", lambda uid: None)
    monkeypatch.setitem(graph_mod.RUNNERS, "search_similar",
                        lambda item_id, top_k=20: {
                            "content": [_c(1, 0.9)],
                            "relation": [_c(2, 5.0)],
                            "graph": [_c(3, 1.2)],
                        })
    state = {"user_id": "u", "query": "q", "slots": {"item_id": "douban:1"},
             "plan": ["search_similar"], "ranked": [], "candidates": {}}
    out = graph_mod._execute(state)
    assert set(captured["recall"].keys()) == {"content", "relation", "graph"}
    assert "similar" not in captured["recall"]
    # 步骤详情现在分三路计数（可观测性提升）
    detail = out["steps"][-1]["detail"]
    assert detail["recall_counts"] == {"content": 1, "relation": 1, "graph": 1}


def test_execute_merges_similar_with_other_sources(monkeypatch):
    """similar 与其它路（如 search_graph）同现时按源合并，不互相覆盖。"""
    captured = {}

    def _fake_rerank(recall, **kw):
        captured["recall"] = {k: list(v) for k, v in recall.items()}
        return {"ranked": [], "fusion_detail": {}}

    monkeypatch.setattr(graph_mod, "rerank", _fake_rerank)
    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: _FakeStore())
    monkeypatch.setattr(graph_mod, "_feedback_profile_for", lambda uid: None)
    monkeypatch.setitem(graph_mod.RUNNERS, "search_similar",
                        lambda item_id, top_k=20: {"content": [_c(1, 0.9)],
                                                   "relation": [], "graph": []})
    monkeypatch.setitem(graph_mod.RUNNERS, "search_graph",
                        lambda slots, prefs, top_k=20: [_c(9, 0.5)])
    state = {"user_id": "u", "query": "q", "slots": {"item_id": "douban:1"},
             "plan": ["search_similar", "search_graph"],
             "ranked": [], "candidates": {}}
    graph_mod._execute(state)
    # similar 的非空路进顶层；空路不建键；独立 search_graph 存于自己的规划名下
    # （RRF 层经 _PLAN_TO_CANON 归一到 graph 权重，两者不互相覆盖）
    assert [c.id for c in captured["recall"]["content"]] == ["douban:1"]
    assert "relation" not in captured["recall"] and "graph" not in captured["recall"]
    assert [c.id for c in captured["recall"]["search_graph"]] == ["douban:9"]


# ---------- 外层权重真实生效（P2-8 的核心主张） ----------

def test_outer_rrf_applies_source_weights():
    """三路各一个候选时，融合分按 content 1.0 > relation 0.8 > graph 0.7 递减。"""
    a, b, c = _c(1, 0.9), _c(2, 0.9), _c(3, 0.9)
    out = rrf_fuse({"content": [a], "relation": [b], "graph": [c]},
                   weights={"content": 1.0, "relation": 0.8, "graph": 0.7},
                   k=60)
    assert [x.id for x in out] == ["douban:1", "douban:2", "douban:3"]
    scores = {x.id: x.score for x in out}
    assert scores["douban:1"] > scores["douban:2"] > scores["douban:3"]


def test_outer_rrf_plan_keys_still_normalized():
    """规划名 key（search_by_text/search_graph）仍规范化到规范权重键。"""
    from config.settings import settings

    w = settings.RRF_WEIGHTS_DICT
    a, b = _c(1, 0.9), _c(2, 0.9)
    out = rrf_fuse({"search_by_text": [a], "search_graph": [b]}, weights=w, k=60)
    assert [x.id for x in out] == ["douban:1", "douban:2"]  # content 权重高于 graph
