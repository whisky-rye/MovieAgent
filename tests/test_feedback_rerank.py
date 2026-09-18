"""O11 排序注入单测：_feedback_term 在 fuse / rrf_fuse 中正确加分/降权（不依赖 MySQL）。

覆盖：
- 黑名单（踩过）直接强降权、沉底，且不被加分抵消；
- 赞过/点过 item_boost 按 ITEM_LIKE_BOOST 叠加；
- 类型亲和度按 GENRE_AFFINITY_SCALE 加权；
- profile=None / 空画像返回 0（不影响原排序）；
- 默认 RRF 策略下同样生效（融合按位次、回灌项叠加在综合分上）。
"""
import pytest

from retrieval.candidates import Candidate
from ranker.scorer import fuse, _feedback_term, GENRE_AFFINITY_SCALE
from ranker.rrf import rrf_fuse
from agent.feedback_store import (
    ITEM_LIKE_BOOST,
    ITEM_DISLIKE_PENALTY,
    ITEM_CLICK_BOOST,
)


def _mk(iid, score, rating=8.0, genres=None):
    return Candidate(
        id=iid, source="douban", title=iid, year=2020,
        genres=genres or [], rating=rating, score=score,
    )


# —— 1. _feedback_term 纯函数 ——
def test_term_none_profile_returns_zero():
    c = _mk("douban:A", 0.9)
    assert _feedback_term(c, None) == 0.0
    assert _feedback_term(c, {}) == 0.0


def test_term_blacklist_overrides_boost():
    c = _mk("douban:A", 0.9)
    prof = {"item_blacklist": {"douban:A": ITEM_DISLIKE_PENALTY},
            "item_boost": {"douban:A": ITEM_LIKE_BOOST},
            "genre_affinity": {}}
    # 命中黑名单直接返回惩罚，不受 boost 影响
    assert _feedback_term(c, prof) == ITEM_DISLIKE_PENALTY


def test_term_like_boost_added():
    c = _mk("douban:A", 0.9)
    prof = {"item_blacklist": {}, "item_boost": {"douban:A": ITEM_LIKE_BOOST},
            "genre_affinity": {}}
    assert _feedback_term(c, prof) == ITEM_LIKE_BOOST


def test_term_genre_affinity_scaled():
    c = _mk("douban:A", 0.9, genres=["剧情", "科幻"])
    prof = {"item_blacklist": {}, "item_boost": {},
            "genre_affinity": {"剧情": 0.5, "科幻": -0.4}}
    # (0.5 + (-0.4)) * scale
    assert _feedback_term(c, prof) == pytest.approx((0.5 - 0.4) * GENRE_AFFINITY_SCALE)


def test_term_click_weaker_than_like_kept_consistent():
    # 仅校验导入常量一致性（小于赞的分值），实际聚合在 feedback_store 单测
    assert ITEM_CLICK_BOOST < ITEM_LIKE_BOOST


# —— 2. fuse（score 策略）注入 ——
def _score_recall():
    # 单一 content 路：A 低分位、B 高位；rating 差异小
    return {"content": [_mk("douban:A", 0.5, rating=8.0),
                        _mk("douban:B", 0.9, rating=6.0)]}


def test_fuse_blacklist_sinks_item_below_unboosted():
    ranked, emap = fuse(_score_recall(), explain=True)
    base_first = ranked[0].id  # 默认 B 在前（高分位）
    assert base_first == "douban:B"

    prof = {"item_blacklist": {"douban:B": ITEM_DISLIKE_PENALTY},
            "item_boost": {}, "genre_affinity": {}}
    ranked2, emap2 = fuse(_score_recall(), feedback_profile=prof, explain=True)
    # 踩过的 B 被强降权，沉到末位
    assert ranked2[-1].id == "douban:B"
    assert emap2["douban:B"]["final"] < emap2["douban:A"]["final"]


def test_fuse_like_boost_increases_final_by_exact_amount():
    _, emap0 = fuse(_score_recall(), explain=True)
    prof = {"item_blacklist": {}, "item_boost": {"douban:A": ITEM_LIKE_BOOST},
            "genre_affinity": {}}
    _, emap1 = fuse(_score_recall(), feedback_profile=prof, explain=True)
    # A 的终分因点赞精确到 +ITEM_LIKE_BOOST（无黑名单、无亲和度干扰）
    delta = emap1["douban:A"]["final"] - emap0["douban:A"]["final"]
    assert delta == pytest.approx(ITEM_LIKE_BOOST)


# —— 3. rrf_fuse（默认 RRF 策略）注入 ——
def _rrf_recall():
    # 单路按位次：A rank1、B rank2
    return {"content": [_mk("douban:A", 0.9, rating=8.0),
                        _mk("douban:B", 0.8, rating=6.0)]}


def test_rrf_blacklist_sinks_item():
    _, emap0 = rrf_fuse(_rrf_recall(), explain=True)
    prof = {"item_blacklist": {"douban:B": ITEM_DISLIKE_PENALTY},
            "item_boost": {}, "genre_affinity": {}}
    ranked, emap1 = rrf_fuse(_rrf_recall(), feedback_profile=prof, explain=True)
    assert ranked[-1].id == "douban:B"
    # 终分 = 原终分 + 惩罚
    assert emap1["douban:B"]["final"] == pytest.approx(emap0["douban:B"]["final"] + ITEM_DISLIKE_PENALTY)


def test_rrf_like_boost_increases_final_by_exact_amount():
    _, emap0 = rrf_fuse(_rrf_recall(), explain=True)
    prof = {"item_blacklist": {}, "item_boost": {"douban:A": ITEM_LIKE_BOOST},
            "genre_affinity": {}}
    _, emap1 = rrf_fuse(_rrf_recall(), feedback_profile=prof, explain=True)
    delta = emap1["douban:A"]["final"] - emap0["douban:A"]["final"]
    assert delta == pytest.approx(ITEM_LIKE_BOOST)
