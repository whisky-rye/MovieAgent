"""O11 反馈聚合逻辑单测（离线，直接测纯函数 _aggregate，不依赖 MySQL）。"""
from datetime import datetime, timedelta

from agent.feedback_store import (
    _aggregate,
    ITEM_LIKE_BOOST,
    ITEM_CLICK_BOOST,
    ITEM_DISLIKE_PENALTY,
)


def _row(signal, genres, item_id, weight=1.0, age_days=0):
    return (signal, genres, item_id, weight, datetime.now() - timedelta(days=age_days))


def test_like_boosts_item():
    p = _aggregate([_row("explicit_like", "科幻", "douban:1")])
    assert p["item_boost"].get("douban:1") == ITEM_LIKE_BOOST
    assert p["genre_affinity"].get("科幻") == 1.0  # 全新类型，无衰减


def test_dislike_blacklists_and_penalizes():
    p = _aggregate([_row("explicit_dislike", "恐怖", "douban:9")])
    assert p["item_blacklist"].get("douban:9") == ITEM_DISLIKE_PENALTY
    assert p["genre_affinity"].get("恐怖") == -1.0
    assert "douban:9" not in p["item_boost"]


def test_click_is_weaker_than_like():
    p = _aggregate([_row("click", None, "douban:5")])
    assert p["item_boost"].get("douban:5") == ITEM_CLICK_BOOST
    assert ITEM_CLICK_BOOST < ITEM_LIKE_BOOST


def test_genre_affinity_clamped_and_accumulates():
    # 多次赞同一类型：累加但每一步封顶 1.0（不会到 2.0）
    p = _aggregate([
        _row("explicit_like", "科幻", "a"),
        _row("explicit_like", "科幻", "b"),
    ])
    assert p["genre_affinity"]["科幻"] == 1.0
    # 一次踩把已封顶的 +1 拉回 0（逐步 clamp：1 - 1 = 0）
    p2 = _aggregate([
        _row("explicit_like", "科幻", "a"),
        _row("explicit_dislike", "科幻", "c"),
    ])
    assert p2["genre_affinity"]["科幻"] == 0.0


def test_time_decay_lowers_old_feedback():
    fresh = _aggregate([_row("explicit_like", "科幻", "a", age_days=0)])
    old = _aggregate([_row("explicit_like", "科幻", "a", age_days=30)])  # 半衰期30d → 0.5
    assert fresh["genre_affinity"]["科幻"] == 1.0
    assert old["genre_affinity"]["科幻"] == 0.5


def test_empty_rows_returns_empty_profile():
    p = _aggregate([])
    assert p == {"genre_affinity": {}, "item_boost": {}, "item_blacklist": {}}


# —— 显式赞/踩互斥（同一作品不能同时是喜欢与不喜欢） ——

def test_later_dislike_overrides_earlier_like():
    """先赞后踩：该作品应只算「踩」——进黑名单、不进加分集。"""
    p = _aggregate([
        _row("explicit_like", "剧情", "douban:7", age_days=1),
        _row("explicit_dislike", "剧情", "douban:7", age_days=0),
    ])
    assert p["item_blacklist"].get("douban:7") == ITEM_DISLIKE_PENALTY
    assert "douban:7" not in p["item_boost"]


def test_later_like_overrides_earlier_dislike():
    """先踩后赞（历史脏数据）：该作品应只算「赞」——进加分集、不进黑名单。"""
    p = _aggregate([
        _row("explicit_dislike", "剧情", "douban:8", age_days=1),
        _row("explicit_like", "剧情", "douban:8", age_days=0),
    ])
    assert p["item_boost"].get("douban:8") == ITEM_LIKE_BOOST
    assert "douban:8" not in p["item_blacklist"]


def test_mutex_does_not_affect_other_items():
    """互斥只作用于同 item：不同作品的赞/踩各自独立生效。"""
    p = _aggregate([
        _row("explicit_like", None, "a"),
        _row("explicit_dislike", None, "a"),  # a 最终为踩
        _row("explicit_like", None, "b"),      # b 不受影响
    ])
    assert p["item_blacklist"].get("a") == ITEM_DISLIKE_PENALTY
    assert "a" not in p["item_boost"]
    assert p["item_boost"].get("b") == ITEM_LIKE_BOOST
