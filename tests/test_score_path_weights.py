"""P2-9 回归：score 融合回退路径的 RRF_WEIGHTS 不再静默失效。

修复前：rerank 的 score 分支把规划名 key（search_by_text / search_graph /
high_quality_board...）的 recall 直接交给 fuse，而 fuse 用 `w.get(src, 1.0)`
查的是规范权重表（content/relation/coldstart/graph/person/prod）——规划名
全部查无，权重静默回退 1.0。文档宣称 score 为「向后兼容回退用」，实际回退时
所有权重配置丢失（与 RRF 路径行为不一致）。

修复后：规划名→规范名的映射与规范化助手下沉到 scorer（_PLAN_TO_CANON /
_canon_key，rrf 再导出保持兼容），fuse 对权重键与来源键都做规范化。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib

import pytest

import config.settings as cs
from ranker.rrf import _PLAN_TO_CANON, _canon_key, rrf_fuse
from ranker.scorer import fuse
from retrieval.candidates import Candidate

# importlib 取子模块（ranker/__init__ 把同名函数 rerank 重导出）
rr_mod = importlib.import_module("ranker.rerank")


def _c(i, score=0.9):
    # rating 保持 0：质量/偏好/反馈项皆为 0，最终分 = 源权重 * min-max 归一化分
    return Candidate(id=f"douban:{i}", source="douban", title=f"片{i}",
                     year=0, genres=[], score=score)


# ---------- 规范化助手 ----------

def test_canon_key_mapping():
    assert _canon_key("search_by_text") == "content"
    assert _canon_key("search_graph") == "graph"
    assert _canon_key("high_quality_board") == "coldstart"
    assert _canon_key("coldstart_fallback") == "coldstart"
    assert _canon_key("search_by_person") == "person"
    assert _canon_key("search_prod") == "prod"
    # 规范名与未知名原样返回
    assert _canon_key("content") == "content"
    assert _canon_key("whatever") == "whatever"
    # 自定义映射优先
    assert _canon_key("x", {"x": "y"}) == "y"


def test_plan_to_canon_reexported_from_rrf():
    """agent/graph.py 既有用法 `from ranker.rrf import _PLAN_TO_CANON` 仍有效。"""
    assert _PLAN_TO_CANON["search_by_text"] == "content"


# ---------- fuse：权重真实生效 ----------

def test_fuse_applies_weights_to_plan_keys():
    """规划名 key + canon 权重表 → 按权重递减（修复前全部落 1.0 并列）。"""
    recall = {"search_by_text": [_c(1)], "search_graph": [_c(2)],
              "high_quality_board": [_c(3)]}
    weights = {"content": 1.0, "graph": 0.5, "coldstart": 0.1}
    out = fuse(recall, weights=weights)
    scores = {c.id: c.score for c in out}
    # 单候选源的 min-max 归一化恒 0.5 → 最终分 = 权重 * 0.5
    assert scores["douban:1"] == pytest.approx(0.5)
    assert scores["douban:2"] == pytest.approx(0.25)
    assert scores["douban:3"] == pytest.approx(0.05)
    assert [c.id for c in out] == ["douban:1", "douban:2", "douban:3"]


def test_fuse_plan_keys_distinguishable_from_tie():
    """id 序与权重序相反时，结果必须跟权重序（修复前会是 id 序）。"""
    recall = {"search_by_text": [_c(9)], "search_graph": [_c(1)]}
    weights = {"content": 1.0, "graph": 0.7}
    out = fuse(recall, weights=weights)
    assert [c.id for c in out] == ["douban:9", "douban:1"]


def test_fuse_canon_keys_still_work():
    """规范名 key（content/relation/graph）行为不变（恒等映射）。"""
    recall = {"content": [_c(1)], "relation": [_c(2)]}
    out = fuse(recall, weights={"content": 1.0, "relation": 0.8})
    scores = {c.id: c.score for c in out}
    assert scores["douban:1"] == pytest.approx(0.5)
    assert scores["douban:2"] == pytest.approx(0.4)


def test_fuse_weights_dict_may_use_plan_names():
    """权重表本身用规划名也兼容（双向规范化）。"""
    recall = {"content": [_c(1)], "graph": [_c(2)]}
    out = fuse(recall, weights={"search_by_text": 1.0, "search_graph": 0.5})
    scores = {c.id: c.score for c in out}
    assert scores["douban:1"] == pytest.approx(0.5)
    assert scores["douban:2"] == pytest.approx(0.25)


# ---------- rerank score 回退路径端到端 ----------

def test_rerank_score_path_applies_settings_weights(monkeypatch):
    """FUSION_STRATEGY=score 回退路径：settings.RRF_WEIGHTS_DICT 真实生效。"""
    monkeypatch.setattr(cs.settings, "FUSION_STRATEGY", "score")
    # id 刻意与权重序相反：修复前并列按 id → [1, 9]；修复后 content(1.0) > graph(0.7) → [9, 1]
    recall = {"search_by_text": [_c(9)], "search_graph": [_c(1)]}
    out = rr_mod.rerank(recall, strategy="score", top_k=5, llm_topn=0)
    assert [c.id for c in out] == ["douban:9", "douban:1"]


def test_rerank_rrf_path_unchanged(monkeypatch):
    """RRF 主路径不受影响（权重本就生效）。"""
    monkeypatch.setattr(cs.settings, "FUSION_STRATEGY", "rrf")
    recall = {"search_by_text": [_c(9)], "search_graph": [_c(1)]}
    out = rr_mod.rerank(recall, strategy="rrf", top_k=5, llm_topn=0)
    assert [c.id for c in out] == ["douban:9", "douban:1"]


def test_rrf_fuse_still_normalizes_plan_keys():
    """rrf_fuse 的规划名归一（原有行为）保持。"""
    recall = {"search_by_text": [_c(9)], "search_graph": [_c(1)]}
    out = rrf_fuse(recall, weights=cs.settings.RRF_WEIGHTS_DICT, k=60)
    assert [c.id for c in out] == ["douban:9", "douban:1"]
