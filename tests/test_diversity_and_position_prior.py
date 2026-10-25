"""P2-7 回归：多样性代理升级（类型/年代/地区）+ 精排位置先验保住 MMR 成果。

修复前：
- MMR 相似度代理只有类型 Jaccard——年代/地区多样性无约束，结果可能是
  「同类型但同十年同产地」的一批片；
- rerank 的精排阶段按相关性重排 head，把 MMR 刚取得的多样性抹掉。

修复后：
- _diversity_distance = 0.6*类型Jaccard + 0.2*年代相近 + 0.2*地区相同
  （缺信息维度不惩罚）；mmr_diversify 走特征预计算 + 同一公式；
- rerank 在 MMR 后构建 {id: 名次} 交精排作位置先验（_with_position_prior）：
  相关性差距大于先验差时仍按相关性排序（精排有效性不变），接近持平时保持
  MMR 次序（多样性不被抹掉）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib

import pytest

import config.settings as cs
import ranker.llm_rerank as lr_mod
import ranker.scorer as sc_mod
from retrieval.candidates import Candidate

# importlib 取子模块（ranker/__init__ 把同名函数 rerank 重导出）
rr_mod = importlib.import_module("ranker.rerank")


@pytest.fixture(autouse=True)
def _reset_llm_singleton():
    """每个用例前清空精排客户端单例（否则第一次的假 LLM 被缓存复用）。"""
    lr_mod._reset_llm()
    yield
    lr_mod._reset_llm()


def _c(i, score, genres=("剧情",), year=0, country=""):
    return Candidate(id=f"douban:{i}", source="douban", title=f"片{i}",
                     year=year, genres=list(genres), country=country, score=score)


# ---------- 多样性距离 ----------

def test_distance_genre_only_when_meta_missing():
    """缺年份/地区时不惩罚：只有类型项。"""
    a, b = _c(1, 0.5, genres=("科幻", "悬疑")), _c(2, 0.5, genres=("科幻",))
    # Jaccard = 1/2 → 0.6*0.5 = 0.3
    assert sc_mod._diversity_distance(a, b) == pytest.approx(0.3)


def test_distance_full_overlap_is_one():
    a = _c(1, 0.5, genres=("科幻",), year=2000, country="美国")
    b = _c(2, 0.5, genres=("科幻",), year=2000, country="美国")
    assert sc_mod._diversity_distance(a, b) == pytest.approx(1.0)


def test_distance_disjoint_genres_same_era_country():
    """类型完全不同但同代同产地：0 + 0.2 + 0.2 = 0.4。"""
    a = _c(1, 0.5, genres=("科幻",), year=2000, country="美国")
    b = _c(2, 0.5, genres=("爱情",), year=2000, country="美国")
    assert sc_mod._diversity_distance(a, b) == pytest.approx(0.4)


def test_distance_year_linear_decay():
    a = _c(1, 0.5, genres=("科幻",), year=2000)
    near = _c(2, 0.5, genres=("科幻",), year=2015)   # 差 15 年 → 0.5
    far = _c(3, 0.5, genres=("科幻",), year=2030)    # 差 30 年 → 0
    assert sc_mod._diversity_distance(a, near) == pytest.approx(0.6 + 0.1)
    assert sc_mod._diversity_distance(a, far) == pytest.approx(0.6)


def test_distance_country_multi_value_overlap():
    """地区串多值：任一命中即算相同。"""
    a = _c(1, 0.5, genres=("科幻",), year=2000, country="中国/美国")
    b = _c(2, 0.5, genres=("科幻",), year=2000, country="美国")
    assert sc_mod._diversity_distance(a, b) == pytest.approx(1.0)
    c = _c(3, 0.5, genres=("科幻",), year=2000, country="日本")
    assert sc_mod._diversity_distance(a, c) == pytest.approx(0.8)


def test_distance_features_match_function():
    """预计算特征版与 Candidate 版同源（单一事实源）。"""
    a = _c(1, 0.5, genres=("科幻", "剧情"), year=1999, country="中国/美国")
    b = _c(2, 0.5, genres=("剧情",), year=2010, country="日本")
    assert sc_mod._sim_features(sc_mod._div_features(a), sc_mod._div_features(b)) == \
        pytest.approx(sc_mod._diversity_distance(a, b))


# ---------- 位置先验 ----------

def test_position_prior_shape():
    assert sc_mod._position_prior(1, 1) == 1.0     # 单候选
    assert sc_mod._position_prior(1, 5) == 1.0     # 第一名满分
    assert sc_mod._position_prior(5, 5) == 0.5     # 最后一名 0.5
    mid = [sc_mod._position_prior(p, 5) for p in range(1, 6)]
    assert mid == sorted(mid, reverse=True)        # 随名次单调衰减


def test_with_position_prior_no_positions_unchanged():
    assert sc_mod._with_position_prior(0.7, "douban:1", None) == 0.7
    assert sc_mod._with_position_prior(0.7, "douban:1", {}) == 0.7
    # 名次表里没有该候选 → 不加先验
    assert sc_mod._with_position_prior(0.7, "douban:x", {"douban:1": 1}) == 0.7


def test_with_position_prior_applied():
    out = sc_mod._with_position_prior(0.5, "douban:1", {"douban:1": 1, "douban:2": 2})
    assert out == pytest.approx(0.5 + 0.1 * 1.0)               # 第一名满额
    out2 = sc_mod._with_position_prior(0.5, "douban:2", {"douban:1": 1, "douban:2": 2})
    assert out2 == pytest.approx(0.5 + 0.1 * 0.5)              # 末名折半


# ---------- MMR 多样性：年代/地区约束生效 ----------

def test_mmr_diversifies_year_and_country():
    """同类型同分的一批片：第二个应选「年代/地区都远」的那部（旧代理会选相邻的）。"""
    cands = [
        _c(0, 1.0, genres=("科幻",), year=2000, country="美国"),
        _c(1, 1.0, genres=("科幻",), year=2001, country="美国"),  # 与 0 最近
        _c(2, 1.0, genres=("科幻",), year=2010, country="中国"),
        _c(3, 1.0, genres=("科幻",), year=2020, country="日本"),  # 与 0 最远
    ]
    out = sc_mod.mmr_diversify(cands, lambda_div=0.5, top_k=2)
    assert [c.id for c in out] == ["douban:0", "douban:3"], \
        "年代/地区多样性未生效（退回了仅类型代理）"


def test_mmr_genre_still_dominant():
    """类型维度仍是主信号：跨类型比跨年代「更不相似」。"""
    a = _c(1, 0.5, genres=("科幻",), year=2000, country="美国")
    same_genre_diff_era = _c(2, 0.5, genres=("科幻",), year=2030, country="日本")
    diff_genre_same_era = _c(3, 0.5, genres=("爱情",), year=2000, country="美国")
    d1 = sc_mod._diversity_distance(a, same_genre_diff_era)   # 0.6
    d2 = sc_mod._diversity_distance(a, diff_genre_same_era)   # 0.4
    assert d1 > d2


# ---------- 精排位置先验：rerank → llm_rerank / cloud_rerank ----------

class _FakeLLM:
    """按标题返回预设相关性分的假 LIGHT 模型。"""

    def __init__(self, table):
        self.table = table

    def invoke(self, prompt):
        for key, val in self.table.items():
            if f"影片：{key}" in prompt:
                return type("R", (), {"content": val})()
        return type("R", (), {"content": "0.5"})()


def test_llm_rerank_prior_preserves_mmr_order_on_near_tie(monkeypatch):
    """相关性接近时保持 MMR 次序（先验生效）；相关性差距大时仍按相关性。"""
    a, b = _c("a", 0.5, genres=("科幻",)), _c("b", 0.5, genres=("科幻",))
    positions = {"douban:a": 1, "douban:b": 2}
    # 近似平手：b 相关性只高一点（0.55 vs 0.5 → 融合差 0.015 < 先验差 0.0293）
    monkeypatch.setattr(lr_mod, "get_light",
                        lambda: _FakeLLM({"片a": "0.5", "片b": "0.55"}))
    out = lr_mod.llm_rerank([a, b], "q", positions=positions)
    assert [c.id for c in out] == ["douban:a", "douban:b"], "近似平手应保持 MMR 次序"
    # 差距真实：b 相关性 1.0（融合差 0.15 > 先验差 0.0293）→ 相关性胜出
    lr_mod._reset_llm()  # 换假 LLM 前先清单例（_get_llm 有缓存）
    monkeypatch.setattr(lr_mod, "get_light",
                        lambda: _FakeLLM({"片a": "0.5", "片b": "1.0"}))
    out2 = lr_mod.llm_rerank([a, b], "q", positions=positions)
    assert [c.id for c in out2] == ["douban:b", "douban:a"], "相关性差距大应仍按相关性"
    # 无 positions（未开 MMR）→ 旧行为：纯相关性
    lr_mod._reset_llm()
    out3 = lr_mod.llm_rerank([a, b], "q")
    assert [c.id for c in out3] == ["douban:b", "douban:a"]


def test_cloud_rerank_prior_preserves_mmr_order(monkeypatch):
    import ranker.cloud_rerank as cr_mod

    class _FakeClient:
        def rerank(self, query, documents, top_n=None):
            # 文档序即候选序：a→0.5, b→0.55
            return [(0, 0.5), (1, 0.55)]

        @staticmethod
        def _doc_text(c):
            return c.title

    monkeypatch.setattr(cr_mod, "_get_client", lambda: _FakeClient())
    a, b = _c("a", 0.5, genres=("科幻",)), _c("b", 0.5, genres=("科幻",))
    positions = {"douban:a": 1, "douban:b": 2}
    out = cr_mod.cloud_rerank([a, b], "q", positions=positions)
    assert [c.id for c in out] == ["douban:a", "douban:b"]


def test_rerank_passes_mmr_positions_to_reranker(monkeypatch):
    """rerank 在 MMR 之后把 {id: 名次} 交给精排实现（本地路）。"""
    captured = {}
    real = lr_mod.llm_rerank

    def _spy(cands, query, top_k=None, positions=None):
        captured["positions"] = positions
        return real(cands, query, top_k=top_k, positions=positions)

    monkeypatch.setattr(cs.settings, "RERANK_PROVIDER", "local")
    monkeypatch.setattr(rr_mod, "llm_rerank", _spy)
    monkeypatch.setattr(lr_mod, "get_light", lambda: _FakeLLM({}))
    cands = [_c(1, 0.9, genres=("科幻",)), _c(2, 0.8, genres=("科幻",)),
             _c(3, 0.7, genres=("科幻",))]
    rr_mod.rerank({"content": cands}, strategy="mmr", llm_topn=3, top_k=3, query="q")
    assert captured["positions"] == {"douban:1": 1, "douban:2": 2, "douban:3": 3}


def test_rerank_no_mmr_no_positions(monkeypatch):
    """未开 MMR（diversify=False）→ 不传 positions（行为与旧版一致）。"""
    captured = {}

    def _spy(cands, query, top_k=None, positions=None):
        captured["positions"] = positions
        return cands

    monkeypatch.setattr(cs.settings, "RERANK_PROVIDER", "local")
    monkeypatch.setattr(rr_mod, "llm_rerank", _spy)
    cands = [_c(1, 0.9), _c(2, 0.8)]
    rr_mod.rerank({"content": cands}, strategy="score", diversify=False,
                  llm_topn=2, top_k=2, query="q")
    assert captured["positions"] is None
