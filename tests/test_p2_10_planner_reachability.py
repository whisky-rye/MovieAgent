"""P2-10 回归：规划器六路可达性——prod 按开关接入，graph 经 similar 展开可达。

修复前：架构宣称 6 路召回，但 _plan 只产 4 种（search_by_text/search_similar/
high_quality_board/search_by_person）；search_graph 与 search_prod 在规划器
不可达，MCP_ENABLED=false 时 _run_search_prod 恒返回空，「生产库补充通道」
是装饰性接线；get_tools() 全仓无 tool-calling 循环消费。

修复：recommend/explain 的 plan 在 MCP_ENABLED=true 时追加 search_prod；
similar 经 P2-8 的三路展开使 graph 路可达（与独立 search_graph 同源，不双路
双权重）；可达路径在 _plan / _run_search_prod / get_tools 文档中明示。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import agent.tools as tools_mod
from agent import graph as graph_mod
from config.settings import settings


def _state(intent, **kw):
    base = {"user_id": "u", "query": "q", "intent": intent, "slots": {}}
    base.update(kw)
    return base


# ---------- recommend / explain：prod 按开关接入 ----------

def test_recommend_plan_without_mcp(monkeypatch):
    monkeypatch.setattr(settings, "MCP_ENABLED", False)
    out = graph_mod._plan(_state("recommend"))
    assert out["plan"] == ["search_by_text"]


def test_recommend_plan_with_mcp(monkeypatch):
    """MCP_ENABLED=true → plan 追加 search_prod（生产库补充通道真正接入）。"""
    monkeypatch.setattr(settings, "MCP_ENABLED", True)
    out = graph_mod._plan(_state("recommend"))
    assert out["plan"] == ["search_by_text", "search_prod"]


def test_explain_plan_with_mcp(monkeypatch):
    monkeypatch.setattr(settings, "MCP_ENABLED", True)
    out = graph_mod._plan(_state("explain"))
    assert out["plan"] == ["search_by_text", "search_prod"]


def test_mcp_off_prod_runner_returns_empty():
    """开关关闭时 runner 恒空（不进 plan 也不产出，双保险）。"""
    old = settings.MCP_ENABLED
    settings.MCP_ENABLED = False
    try:
        assert tools_mod._run_search_prod({"query": "x"}, {}, top_k=5) == []
    finally:
        settings.MCP_ENABLED = old


# ---------- 其余意图不受影响 ----------

def test_other_intents_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "MCP_ENABLED", True)  # 即便开也只加在 recommend 路
    assert graph_mod._plan(_state("board"))["plan"] == ["high_quality_board"]
    assert graph_mod._plan(_state("preference"))["plan"] == []
    by_person = graph_mod._plan(_state("by_person", slots={"name": "诺兰"}))
    assert by_person["plan"] == ["search_by_person"]
    assert by_person["slots"]["name"] == "诺兰"


def test_similar_plan_resolves_item(monkeypatch):
    """similar 且能解析出 item → search_similar（其 graph 路即图谱多跳召回）。"""
    monkeypatch.setattr(graph_mod, "_resolve_item_id", lambda item: "douban:1")
    out = graph_mod._plan(_state("similar", slots={"item_hint": "星际穿越"}))
    assert out["plan"] == ["search_similar"]
    assert out["slots"]["item_id"] == "douban:1"


def test_similar_plan_falls_back_to_text(monkeypatch):
    """similar 但解析不出 item → 退回内容召回（不带 item_id 的 similar 无意义）。"""
    monkeypatch.setattr(graph_mod, "_resolve_item_id", lambda item: None)
    out = graph_mod._plan(_state("similar", slots={"item_hint": "随便聊聊"}))
    assert out["plan"] == ["search_by_text"]


# ---------- graph 路经 similar 展开可达（P2-8 联动） ----------

def test_similar_expansion_reaches_graph_source(monkeypatch):
    """_execute 把 similar 展开为 content/relation/graph —— graph 源在生产可达。"""
    from retrieval.candidates import Candidate

    class _FakeStore:
        def get_prefs(self, uid):
            return {}

    captured = {}

    def _fake_rerank(recall, **kw):
        captured["recall"] = {k: list(v) for k, v in recall.items()}
        return {"ranked": [], "fusion_detail": {}}

    monkeypatch.setattr(graph_mod, "rerank", _fake_rerank)
    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: _FakeStore())
    monkeypatch.setattr(graph_mod, "_feedback_profile_for", lambda uid: None)
    monkeypatch.setitem(graph_mod.RUNNERS, "search_similar",
                        lambda item_id, top_k=20: {
                            "content": [Candidate(id="douban:1", source="douban",
                                                  title="A", score=0.9)],
                            "relation": [],
                            "graph": [Candidate(id="douban:2", source="douban",
                                                title="B", score=0.8)],
                        })
    state = {"user_id": "u", "query": "q", "slots": {"item_id": "douban:1"},
             "plan": ["search_similar"], "ranked": [], "candidates": {}}
    graph_mod._execute(state)
    assert "graph" in captured["recall"], "graph 源应经 similar 展开进入顶层 recall"
    assert [c.id for c in captured["recall"]["graph"]] == ["douban:2"]
