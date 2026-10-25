"""P1-5 / P1-9 修复回归。

P1-5 reflect 兜底被 error 覆盖：
- _execute 失败写 error + ranked=[]；_reflect 冷启动兜底成功只返回 ranked，
  未清 error（无 reducer 普通键，写入后不被后续节点清除）→ _generate 见
  error 即返回「检索出了点问题」失败文案，把兜底回来的 ranked 整份丢弃
  （任一路召回抖动 → 高分榜已补回候选 → 用户仍看到失败文案）。
- 修复：reflect 成功路径返回 error=""；_generate 条件加「and not ranked」双保险。

P1-9 精排默认全开：
- RERANK_LLM_TOPN 默认 30，与注释「0=关闭（默认，控成本）」相反 → 每次推荐
  对 top-30 走云端重排（RERANK_PROVIDER 默认 cloud），固定产生调用/额度消耗。
- 修复：默认改 0（settings 默认值 + .env 活动配置对齐）；provider 注释修正
  （cloud 为当前默认、缺 Key 自动回退 local）。
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib

import pytest

from config.settings import Settings, settings
from agent import graph as graph_mod
from retrieval.candidates import Candidate

rr_mod = importlib.import_module("ranker.rerank")


def _cands(n=6, prefix="douban"):
    return [Candidate(id=f"{prefix}:{i}", source=prefix, title=f"片{i}",
                      rating=8.0, genres=["剧情"]) for i in range(n)]


# ---------------------------------------------------------------------------
# P1-9：精排默认关闭
# ---------------------------------------------------------------------------

def test_rerank_topn_default_is_zero():
    """默认值必须是 0（与「0=关闭（默认，控成本）」的注释一致）。"""
    assert Settings.model_fields["RERANK_LLM_TOPN"].default == 0


def test_rerank_skips_provider_when_topn_zero(monkeypatch):
    """llm_topn=0 时绝不应调用任何精排实现（RRF+MMR 已足够）。"""
    monkeypatch.setattr(settings, "RERANK_LLM_TOPN", 0)

    def _boom(*a, **k):
        raise AssertionError("RERANK_LLM_TOPN=0 时不应调用精排实现")

    monkeypatch.setattr(rr_mod, "cloud_rerank", _boom)
    monkeypatch.setattr(rr_mod, "llm_rerank", _boom)
    out = rr_mod.rerank(
        {"content": _cands(5)}, query="科幻", top_k=5,
        llm_topn=settings.RERANK_LLM_TOPN,  # 与 graph._execute 传参方式一致
    )
    ranked = out["ranked"] if isinstance(out, dict) else out
    assert ranked, "topn=0 仍应返回融合/MMR 结果"


def test_rerank_calls_provider_when_topn_positive(monkeypatch):
    """反向守卫：显式开启（topn>0 + local）时必须真的走精排，防止 0 改过头。"""
    monkeypatch.setattr(settings, "RERANK_PROVIDER", "local")
    called = []

    def _fake_llm(head, query, top_k=None, positions=None):  # P2-7 起带 positions
        called.append(len(head))
        return head

    monkeypatch.setattr(rr_mod, "llm_rerank", _fake_llm)
    out = rr_mod.rerank(
        {"content": _cands(8)}, query="科幻", top_k=8, llm_topn=5,
    )
    assert called, "topn=5 + provider=local 应调用本地精排"


# ---------------------------------------------------------------------------
# P1-5：reflect 兜底成功 → error 必须清除
# ---------------------------------------------------------------------------

class _FakeStore:
    def get_prefs(self, uid):
        return {}

    def sediment_preferences(self, uid, **kw):
        return {}


def test_reflect_success_clears_error(monkeypatch):
    """execute 失败后 reflect 冷启动兜底成功：必须清 error，ranked 可用。"""
    board = _cands(6)
    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: _FakeStore())
    monkeypatch.setattr(graph_mod, "_feedback_profile_for", lambda uid: None)
    monkeypatch.setitem(graph_mod.RUNNERS, "high_quality_board",
                        lambda slots, prefs, top_k=20: board)
    # rerank 打桩：避免测试依赖真实融合/精排链路（本用例只关心 error 流转）
    monkeypatch.setattr(graph_mod, "rerank",
                        lambda recall, **kw: {"ranked": board[:5], "fusion_detail": {}})
    state = {"user_id": "u", "query": "q", "slots": {}, "ranked": [],
             "candidates": {}, "error": "召回失败: boom"}
    out = graph_mod._reflect(state)
    assert out.get("error") == "", "兜底成功未清除 error，generate 将走失败文案"
    assert len(out.get("ranked", [])) >= 5


def test_generate_uses_ranked_when_error_stale(monkeypatch):
    """双保险：error 残留但 ranked 非空 → 正常生成，不走失败文案。"""
    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: _FakeStore())
    monkeypatch.setattr(graph_mod, "_generate_rich", lambda *a, **k: ("正常回答", {}))
    monkeypatch.setattr(graph_mod, "chat_light", lambda *a, **k: None)
    state = {"user_id": "u", "intent": "recommend", "query": "q",
             "slots": {}, "candidates": {}, "ranked": _cands(5),
             "error": "召回失败: boom"}
    out = graph_mod._generate(state)
    assert out["answer"].startswith("正常回答")  # 末尾会附来源概况行
    assert "检索出了点问题" not in out["answer"]


def test_generate_shows_failure_when_error_and_no_ranked(monkeypatch):
    """反向守卫：error 且无任何候选 → 仍走失败文案（优雅降级不被改坏）。"""
    state = {"user_id": "u", "intent": "recommend", "query": "q",
             "slots": {}, "candidates": {}, "ranked": [],
             "error": "召回失败: boom"}
    out = graph_mod._generate(state)
    assert "检索出了点问题" in out["answer"]
