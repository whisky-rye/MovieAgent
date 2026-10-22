"""缺口 3：impression 曝光信号必须进入反馈画像（此前被 _aggregate 完全忽略）。

纯函数 _aggregate 离线单测，不依赖 MySQL/驱动。
核心断言：
  - impression 单独出现 → 该 item 的 item_boost 为弱负（已曝光未点→降权）
  - impression 多次累计 → 负分随曝光次数线性叠加（防反复推）
  - impression 不影响显式赞/踩：赞过的作品仍为正向加分，曝光负分被跳过
  - impression 不污染类型亲和度（曝光行无 genres 语义）
  - 真正读取模块级 _SIGNAL_WEIGHT（此前定义却从未被使用）
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent.feedback_store import (
    SIGNAL_EXPLICIT_DISLIKE,
    SIGNAL_EXPLICIT_LIKE,
    SIGNAL_IMPRESSION,
    _SIGNAL_WEIGHT,
    _aggregate,
)


def _row(signal, item_id, created_at, genres=None, weight=1.0):
    return (signal, genres, item_id, weight, created_at)


def test_impression_alone_is_weak_negative():
    now = datetime(2026, 1, 1)
    rows = [_row(SIGNAL_IMPRESSION, "m1", now)]
    prof = _aggregate(rows, now=now)
    # 一次曝光 → 弱负，权重来自 _SIGNAL_WEIGHT
    assert prof["item_boost"]["m1"] == pytest.approx(_SIGNAL_WEIGHT[SIGNAL_IMPRESSION])
    assert prof["item_boost"]["m1"] < 0
    assert prof["genre_affinity"] == {}
    assert prof["item_blacklist"] == {}


def test_impression_accumulates_over_many_shows():
    now = datetime(2026, 1, 1)
    rows = [_row(SIGNAL_IMPRESSION, "m2", now) for _ in range(10)]
    prof = _aggregate(rows, now=now)
    # 10 次曝光累计弱负 → 防反复推
    assert prof["item_boost"]["m2"] == pytest.approx(_SIGNAL_WEIGHT[SIGNAL_IMPRESSION] * 10)
    assert prof["item_boost"]["m2"] < -0.4


def test_impression_does_not_cancel_explicit_like():
    t0 = datetime(2026, 1, 1)
    t1 = t0 + timedelta(hours=1)
    # 先曝光、后显式赞（用户最终点了喜欢）
    rows = [
        _row(SIGNAL_IMPRESSION, "m3", t0),
        _row(SIGNAL_EXPLICIT_LIKE, "m3", t1, genres="科幻"),
    ]
    prof = _aggregate(rows, now=t1)
    # 曝光负分不得抹掉显式赞的正向加分
    assert prof["item_boost"]["m3"] > 0
    # 类型亲和度由显式赞贡献，score>0
    assert prof["genre_affinity"].get("科幻", 0) > 0


def test_impression_skipped_when_explicit_dislike_exists():
    t0 = datetime(2026, 1, 1)
    t1 = t0 + timedelta(hours=1)
    rows = [
        _row(SIGNAL_IMPRESSION, "m4", t0),
        _row(SIGNAL_EXPLICIT_DISLIKE, "m4", t1, genres="恐怖"),
    ]
    prof = _aggregate(rows, now=t1)
    # 踩过的作品进黑名单强降权；曝光负分被跳过（不另起一条 item_boost）
    assert prof["item_blacklist"]["m4"] < 0
    assert "m4" not in prof["item_boost"]


def test_impression_not_in_genre_affinity():
    now = datetime(2026, 1, 1)
    # 即便曝光行带 genres（异常数据），也不应进入类型亲和度
    rows = [_row(SIGNAL_IMPRESSION, "m5", now, genres="动作")]
    prof = _aggregate(rows, now=now)
    assert prof["genre_affinity"] == {}


def test_time_decay_applied_to_impression():
    now = datetime(2026, 1, 1)
    old = now - timedelta(days=30)  # 一个半衰期
    rows = [_row(SIGNAL_IMPRESSION, "m6", old)]
    prof = _aggregate(rows, now=now, half_life_days=30)
    # 经过一个半衰期 → 负分减半
    assert prof["item_boost"]["m6"] == pytest.approx(_SIGNAL_WEIGHT[SIGNAL_IMPRESSION] * 0.5)
