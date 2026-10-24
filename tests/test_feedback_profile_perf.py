"""P2-3 回归：反馈聚合成本控制 + 曝光批量写入（真实 MySQL，离线 skip）。

修复前：
- get_profile 每请求全表扫描该用户全部反馈流水（曝光以 top_k 行/请求线性膨胀，
  聚合成本随之线性增长）；
- _log_impressions 每请求对 top_k 条候选逐条 record（每行一个独立事务），
  重复推荐同一 query 还会无限追加曝光行。

修复后：
- get_profile：隐式信号（click/impression）按时间窗过滤 + 行数硬顶（保留最新
  一批）；显式赞/踩不过窗（用户意图不静默过期）；
- record_impressions：单事务多值 INSERT + 同 (user, item, query) 去重。
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from agent import feedback_store as fs_mod
from agent.feedback_store import (
    SIGNAL_CLICK,
    SIGNAL_EXPLICIT_DISLIKE,
    SIGNAL_IMPRESSION,
    get_feedback_store,
)
from config.settings import settings


def _alive():
    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _alive(), reason="MySQL 不可用，跳过反馈存储测试")


@pytest.fixture
def store():
    return get_feedback_store()


def _cleanup(uid):
    e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
    with e.connect() as c:
        c.execute(text("DELETE FROM user_feedback WHERE user_id=:u"), {"u": uid})
        c.commit()
    e.dispose()


def _insert_raw(uid, item_id, signal, rank=None, query=None, created_at=None, genres=None):
    """直插一行（可指定 created_at，用于时间窗/行数上限测试）。"""
    e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
    with e.connect() as c:
        c.execute(
            text(
                "INSERT INTO user_feedback "
                "(`user_id`,`item_id`,`item_kind`,`signal`,`rank`,`query`,`genres`,`weight`,`created_at`) "
                "VALUES (:u,:i,'movie',:s,:r,:q,:g,1.0,:ts)"
            ),
            {"u": uid, "i": item_id, "s": signal, "r": rank, "q": query,
             "g": genres, "ts": created_at or datetime.now()},
        )
        c.commit()
    e.dispose()


# ---------- 批量曝光 + 去重 ----------

def test_record_impressions_batch_and_dedup(store):
    uid = f"imp_{uuid.uuid4().hex[:10]}"
    try:
        rows = [("douban:1", 1, ["剧情"]), ("douban:2", 2, None), ("douban:3", 3, None)]
        assert store.record_impressions(uid, "科幻片", rows) == 3
        # 同 query 重复推荐：去重后仍为 3 行（旧曝光先删后插）
        assert store.record_impressions(uid, "科幻片", rows) == 3
        # 不同 query：追加（上下文不同，位置偏差需分别校正）
        store.record_impressions(uid, "悬疑片", rows[:2])
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
        with e.connect() as c:
            n = c.execute(
                text("SELECT COUNT(*) FROM user_feedback WHERE user_id=:u AND `signal`=:s"),
                {"u": uid, "s": SIGNAL_IMPRESSION},
            ).scalar()
        e.dispose()
        assert n == 5, f"批量+去重行数异常: {n}"
    finally:
        _cleanup(uid)


def test_record_impressions_no_query_appends(store):
    """query 为空的曝光无上下文可去重，直接追加（语义不变）。"""
    uid = f"imp_{uuid.uuid4().hex[:10]}"
    try:
        store.record_impressions(uid, None, [("douban:9", 1, None)])
        store.record_impressions(uid, None, [("douban:9", 1, None)])
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
        with e.connect() as c:
            n = c.execute(
                text("SELECT COUNT(*) FROM user_feedback WHERE user_id=:u AND `signal`=:s"),
                {"u": uid, "s": SIGNAL_IMPRESSION},
            ).scalar()
        e.dispose()
        assert n == 2
    finally:
        _cleanup(uid)


def test_record_impressions_empty_noop(store):
    uid = f"imp_{uuid.uuid4().hex[:10]}"
    assert store.record_impressions(uid, "q", []) == 0


# ---------- get_profile 时间窗 / 行数上限 ----------

def test_get_profile_implicit_window_excludes_old(store):
    """隐式信号超窗（400 天 > 8×30 天）不参与聚合；窗内参与。"""
    uid = f"prof_{uuid.uuid4().hex[:10]}"
    try:
        now = datetime.now()
        _insert_raw(uid, "douban:old", SIGNAL_CLICK, created_at=now - timedelta(days=400))
        _insert_raw(uid, "douban:new", SIGNAL_CLICK, created_at=now - timedelta(days=1))
        prof = store.get_profile(uid, half_life_days=30)
        assert "douban:new" in prof["item_boost"]
        assert "douban:old" not in prof["item_boost"], "超窗隐式信号应被滤掉"
    finally:
        _cleanup(uid)


def test_get_profile_explicit_not_windowed(store):
    """显式赞/踩不过窗：400 天前的 dislike 仍进黑名单（用户意图不静默过期）。"""
    uid = f"prof_{uuid.uuid4().hex[:10]}"
    try:
        _insert_raw(uid, "douban:hated", SIGNAL_EXPLICIT_DISLIKE,
                    created_at=datetime.now() - timedelta(days=400))
        prof = store.get_profile(uid, half_life_days=30)
        assert "douban:hated" in prof["item_blacklist"]
    finally:
        _cleanup(uid)


def test_get_profile_limit_keeps_newest(store, monkeypatch):
    """行数硬顶：超限时保留最新一批（DESC+LIMIT 后反转回升序）。"""
    monkeypatch.setattr(fs_mod, "_PROFILE_MAX_ROWS", 3)
    uid = f"prof_{uuid.uuid4().hex[:10]}"
    try:
        base = datetime.now() - timedelta(days=10)
        for i in range(5):  # 5 条点击，时间递增（i 越大越新）
            _insert_raw(uid, f"douban:c{i}", SIGNAL_CLICK,
                        created_at=base + timedelta(hours=i))
        prof = store.get_profile(uid, half_life_days=30)
        boosted = set(prof["item_boost"])
        assert boosted == {"douban:c2", "douban:c3", "douban:c4"}, \
            f"行数上限应保留最新 3 条，实际: {boosted}"
    finally:
        _cleanup(uid)


# ---------- 纯函数 ----------

def test_implicit_window_days():
    assert fs_mod._implicit_window_days(30) == 240   # 8 个半衰期
    assert fs_mod._implicit_window_days(7) == 56
    assert fs_mod._implicit_window_days(0) == 240   # 不衰减时按 30 天兜底（不为 0）
    assert fs_mod._implicit_window_days(-1) == 240
