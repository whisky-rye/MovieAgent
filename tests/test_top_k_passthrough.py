"""P2-11 回归（离线部分）：top_k 从 state 下传，rerank 收口与召回池随它缩放。

修复前：/recommend 接受 top_k≤50，但 graph._execute 调 rerank 硬编码
top_k=20——top_k>20 被静默截断（接口收 50 实际最多返 20），且每路召回池也
固定 20，放大 top_k 时候选池不跟着放大。

修复：state 新增 top_k 字段（/recommend、/recommend/stream 下传 req.top_k）；
_execute 读取后：rerank(top_k=top_k) 收口，召回池 recall_k=max(20, top_k)
（池有下限 20——融合/MMR/精排需要足够候选才有挑选空间，与最终条数解耦）。
HTTP 层的端到端验证见 tests/e2e/test_top_k_passthrough.py。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import graph as graph_mod
from retrieval.candidates import Candidate


class _FakeStore:
    def get_prefs(self, uid):
        return {}


def _c(i, score=0.9):
    return Candidate(id=f"douban:{i}", source="douban", title=f"片{i}",
                     year=2000, genres=["剧情"], score=score)


def _run_execute(monkeypatch, top_k, plan=("search_by_text",)):
    """以受控 fake 跑一次 _execute，返回捕获到的 rerank/runner 参数。"""
    captured = {"rerank": {}, "runner_top_k": []}

    def _fake_rerank(recall, **kw):
        captured["rerank"] = kw
        return {"ranked": [], "fusion_detail": {}}

    def _fake_runner(slots, prefs, top_k=20):
        captured["runner_top_k"].append(top_k)
        return [_c(1)]

    monkeypatch.setattr(graph_mod, "rerank", _fake_rerank)
    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: _FakeStore())
    monkeypatch.setattr(graph_mod, "_feedback_profile_for", lambda uid: None)
    for name in plan:
        monkeypatch.setitem(graph_mod.RUNNERS, name, _fake_runner)
    state = {"user_id": "u", "query": "q", "slots": {"query": "q"},
             "plan": list(plan), "ranked": [], "candidates": {}}
    if top_k is not None:
        state["top_k"] = top_k
    graph_mod._execute(state)
    return captured


def test_top_k_passed_to_rerank(monkeypatch):
    cap = _run_execute(monkeypatch, top_k=50)
    assert cap["rerank"]["top_k"] == 50, "rerank 收口应跟 top_k"
    assert cap["runner_top_k"] == [50], "召回池应随 top_k 放大（≥20）"


def test_recall_pool_has_floor_of_20(monkeypatch):
    """top_k 小于 20 时召回池仍为 20（融合/MMR 需要挑选空间）。"""
    cap = _run_execute(monkeypatch, top_k=5)
    assert cap["rerank"]["top_k"] == 5
    assert cap["runner_top_k"] == [20]


def test_default_top_k_is_20(monkeypatch):
    """state 无 top_k（/chat 等入口）→ 20，保持历史行为。"""
    cap = _run_execute(monkeypatch, top_k=None)
    assert cap["rerank"]["top_k"] == 20
    assert cap["runner_top_k"] == [20]


def test_top_k_one_edge(monkeypatch):
    cap = _run_execute(monkeypatch, top_k=1)
    assert cap["rerank"]["top_k"] == 1
    assert cap["runner_top_k"] == [20]
