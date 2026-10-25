"""P2-6 回归：排序确定性 tie-break + 反馈类型亲和度封顶。

修复前：
- 各处 `sorted(key=lambda c: c.score, reverse=True)` 同分时依赖输入顺序
  （融合字典序/召回返回序），跨请求不稳定——同分影片先后会飘；
- scorer._feedback_term 的类型亲和度累加无上限：一部片命中 5 个高亲和类型时
  该项可达 1.5，足以压倒质量分/融合信号（对比自述偏好分有 0.3 封顶）。

修复后：统一 sort_by_score（同分按 id 升序，全局唯一稳定键）；亲和度项
正负对称封顶 ±0.3（与自述偏好分上限一致）。
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import importlib

import pytest

import config.settings as cs
import ranker.scorer as sc_mod
from ranker.rrf import rrf_fuse
from ranker.scorer import _feedback_term, fuse as scorer_fuse
from retrieval.candidates import Candidate, sort_by_score

# importlib 取子模块（ranker/__init__ 把同名函数 rerank 重导出）
rr_mod = importlib.import_module("ranker.rerank")


def _c(i, score, genres=("剧情",)):
    return Candidate(id=f"douban:{i}", source="douban", title=f"片{i}",
                     year=2000, genres=list(genres), score=score)


@pytest.fixture(autouse=True)
def _reset_llm_singleton():
    """每个用例前清空精排客户端单例（跨文件共享的 _LLM 缓存会污染 monkeypatch）。"""
    import ranker.llm_rerank as _lr
    _lr._reset_llm()
    yield
    _lr._reset_llm()


# ---------- sort_by_score ----------

def test_sort_by_score_descending_with_id_tiebreak():
    out = sort_by_score([_c("b", 0.5), _c("a", 0.9), _c("c", 0.5), _c("d", 0.9)])
    assert [c.id for c in out] == ["douban:a", "douban:d", "douban:b", "douban:c"]


def test_sort_by_score_input_order_independent():
    base = [_c("a", 0.7), _c("b", 0.7), _c("c", 0.9), _c("d", 0.1)]
    rng = random.Random(42)
    for _ in range(20):
        shuffled = base[:]
        rng.shuffle(shuffled)
        assert [c.id for c in sort_by_score(shuffled)] == \
               ["douban:c", "douban:a", "douban:b", "douban:d"]


def test_sort_by_score_empty_and_single():
    assert sort_by_score([]) == []
    one = [_c("x", 0.5)]
    assert sort_by_score(one) == one


# ---------- 融合/精排各出口的 tie-break ----------

def test_rrf_fuse_tie_is_deterministic():
    """真实并列（双源等权同 rank → 同分）→ 按 id 升序，且与输入顺序无关。"""
    a, b = _c("a", 0.9), _c("z", 0.9)
    recall = {"content": [a], "relation": [b]}
    out = rrf_fuse(recall, weights={"content": 1.0, "relation": 1.0})
    assert [c.id for c in out] == ["douban:a", "douban:z"]
    recall2 = {"relation": [b], "content": [a]}  # 换输入顺序
    out2 = rrf_fuse(recall2, weights={"content": 1.0, "relation": 1.0})
    assert [c.id for c in out] == [c.id for c in out2]


def test_fuse_tie_is_deterministic():
    recall = {"content": [_c("z", 0.8), _c("a", 0.8)]}
    out = scorer_fuse(recall)
    assert [c.id for c in out] == ["douban:a", "douban:z"]


def test_rerank_pipeline_tie_is_deterministic(monkeypatch):
    """rerank 出口（score 融合路径）同分 → id 序，且与输入顺序无关。"""
    # settings 已在模块顶部导入

    monkeypatch.setattr(cs.settings, "FUSION_STRATEGY", "score")
    recall = {"content": [_c("z", 0.8), _c("a", 0.8)]}
    out = rr_mod.rerank(recall, strategy="score", top_k=10, llm_topn=0)
    assert [c.id for c in out] == ["douban:a", "douban:z"]
    recall2 = {"content": [_c("a", 0.8), _c("z", 0.8)]}
    out2 = rr_mod.rerank(recall2, strategy="score", top_k=10, llm_topn=0)
    assert [c.id for c in out] == [c.id for c in out2]


def test_llm_rerank_tie_is_deterministic(monkeypatch):
    """精排同分（假 LLM 全给 0.5）→ 按 id 升序。"""
    import ranker.llm_rerank as lr_mod

    class _FakeLLM:
        def invoke(self, prompt):
            class _R:
                content = "0.5"
            return _R()

    monkeypatch.setattr(lr_mod, "get_light", lambda: _FakeLLM())
    out = lr_mod.llm_rerank([_c("z", 0.8), _c("a", 0.8)], "q")
    assert [c.id for c in out] == ["douban:a", "douban:z"]


def test_cloud_rerank_tie_is_deterministic(monkeypatch):
    """云端精排同分 → 按 id 升序（fake 客户端全给 0.5）。"""
    import ranker.cloud_rerank as cr_mod

    class _FakeClient:
        def rerank(self, query, documents, top_n=None):
            return [(i, 0.5) for i in range(len(documents))]

        @staticmethod
        def _doc_text(c):
            return c.title

    monkeypatch.setattr(cr_mod, "_get_client", lambda: _FakeClient())
    out = cr_mod.cloud_rerank([_c("z", 0.8), _c("a", 0.8)], "q")
    assert [c.id for c in out] == ["douban:a", "douban:z"]


# ---------- 反馈类型亲和度封顶 ----------

def test_genre_affinity_capped_positive():
    """5 个高亲和类型：累加 5×1.0×0.3=1.5 → 封顶 0.3（此前无上限）。"""
    ga = {g: 1.0 for g in ("科幻", "悬疑", "喜剧", "动作", "爱情")}
    c = _c("a", 0.5, genres=tuple(ga))
    assert _feedback_term(c, {"genre_affinity": ga}) == pytest.approx(0.3)


def test_genre_affinity_capped_negative():
    """负向对称封顶：5 个厌恶类型 → -0.3（不能把片沉到底）。"""
    ga = {g: -1.0 for g in ("科幻", "悬疑", "喜剧", "动作", "爱情")}
    c = _c("a", 0.5, genres=tuple(ga))
    assert _feedback_term(c, {"genre_affinity": ga}) == pytest.approx(-0.3)


def test_genre_affinity_single_genre_unchanged():
    """单类型命中：0.3×1.0=0.3，恰在上限，行为不变。"""
    c = _c("a", 0.5, genres=("科幻",))
    assert _feedback_term(c, {"genre_affinity": {"科幻": 1.0}}) == pytest.approx(0.3)


def test_genre_affinity_two_genres_partial():
    """两个半亲和类型：0.3×(0.5+0.5)=0.3，未超上限不走封顶分支。"""
    c = _c("a", 0.5, genres=("科幻", "悬疑"))
    ga = {"科幻": 0.5, "悬疑": 0.5}
    assert _feedback_term(c, {"genre_affinity": ga}) == pytest.approx(0.3)


def test_blacklist_still_short_circuits():
    """黑名单优先语义不变：踩过直接返回强降权，不被亲和度加分抵消。"""
    c = _c("a", 0.5, genres=("科幻",))
    term = _feedback_term(c, {
        "item_blacklist": {"douban:a": -2.0},
        "genre_affinity": {"科幻": 1.0},
        "item_boost": {"douban:a": 0.6},
    })
    assert term == pytest.approx(-2.0)


def test_item_boost_still_additive():
    """item 加分与亲和度相加语义不变（未超上限时）。"""
    c = _c("a", 0.5, genres=("科幻",))
    term = _feedback_term(c, {
        "item_boost": {"douban:a": 0.6},
        "genre_affinity": {"科幻": 0.2},
    })
    assert term == pytest.approx(0.6 + 0.2 * sc_mod.GENRE_AFFINITY_SCALE)
