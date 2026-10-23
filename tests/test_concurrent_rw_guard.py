"""P1-2 并发读-改-写守卫回归（真实 MySQL，离线自动 skip）。

三个历史丢更新/双写点（同一用户并发请求：多标签页、流式推荐与沉淀同时打）：
1. session append_message：读 messages→改→写回无行锁 → 并发丢消息；
2. memory sediment/add_pref：load→改→save 跨连接非事务 → 并发互相覆盖；
3. feedback 显式赞踩互斥：DELETE+INSERT 无串行化 → 同一 item 双显式行
   （读取侧 _aggregate 有"最新显式优先"兜底，但写入侧本应只有一条）。

修复：① append_message 加 `SELECT ... FOR UPDATE`；② memory 走
`MemoryStore.update()`（MySQL 子类单事务 FOR UPDATE + upsert）；
③ feedback 显式信号用 MySQL 命名锁 GET_LOCK 串行化（不改表结构）。

用 Barrier 让两线程同时进入临界区，放大竞争窗口；无锁时这些用例会失败。
"""
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text

from agent.feedback_store import SIGNAL_EXPLICIT_DISLIKE, SIGNAL_EXPLICIT_LIKE, get_feedback_store
from agent.memory import get_memory_store
from agent.session_store import get_session_store
from config.settings import settings


def _cleanup(uid: str) -> None:
    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
        with e.connect() as c:
            c.execute(text("DELETE FROM sessions WHERE user_id=:u"), {"u": uid})
            c.execute(text("DELETE FROM user_preferences WHERE user_id=:u"), {"u": uid})
            c.execute(text("DELETE FROM user_feedback WHERE user_id=:u"), {"u": uid})
            c.commit()
        e.dispose()
    except Exception:  # noqa: BLE001 - 清理失败不影响断言结论
        pass


def _run_parallel(fns):
    """两个任务经 Barrier 对齐后并行执行，放大竞争窗口。"""
    barrier = threading.Barrier(len(fns))

    def _wrap(fn):
        def _inner():
            barrier.wait(timeout=10)
            fn()
        return _inner

    ts = [threading.Thread(target=_wrap(fn)) for fn in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    return all(not t.is_alive() for t in ts)


def test_append_message_concurrent_no_lost_update(mysql_ok):
    store = get_session_store()
    uid = f"race_{uuid.uuid4().hex[:10]}"
    sid = store.create(uid, title="新会话")
    n = 5
    try:
        def worker(tag):
            for i in range(n):
                store.append_message(sid, {"role": "user", "content": f"{tag}-{i}"})

        assert _run_parallel([lambda: worker("t0"), lambda: worker("t1")])
        msgs = store.get(sid)["messages"]
        assert len(msgs) == 2 * n, f"并发追加丢消息：期望 {2 * n}，实际 {len(msgs)}"
    finally:
        _cleanup(uid)


def test_sediment_preferences_concurrent_no_lost_update(mysql_ok):
    store = get_memory_store()
    uid = f"race_{uuid.uuid4().hex[:10]}"
    try:
        assert _run_parallel([
            lambda: store.sediment_preferences(uid, genres=["科幻"]),
            lambda: store.sediment_preferences(uid, genres=["喜剧"]),
        ])
        prefs = store.get_prefs(uid)
        # 无锁时后写者整份覆盖先写者 → 只剩一个类型
        assert set(prefs.get("genres", [])) >= {"科幻", "喜剧"}, prefs
    finally:
        _cleanup(uid)


def test_feedback_explicit_mutex_concurrent(mysql_ok):
    store = get_feedback_store()
    uid = f"race_{uuid.uuid4().hex[:10]}"
    item = "douban:999888777"
    try:
        assert _run_parallel([
            lambda: store.record(uid, item, SIGNAL_EXPLICIT_LIKE),
            lambda: store.record(uid, item, SIGNAL_EXPLICIT_DISLIKE),
        ])
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
        with e.connect() as c:
            cnt = c.execute(
                text(
                    "SELECT COUNT(*) FROM user_feedback WHERE user_id=:u AND item_id=:i "
                    "AND `signal` IN (:s1,:s2)"
                ),
                {"u": uid, "i": item, "s1": SIGNAL_EXPLICIT_LIKE,
                 "s2": SIGNAL_EXPLICIT_DISLIKE},
            ).scalar()
        e.dispose()
        assert cnt == 1, f"显式赞踩互斥失效：同 item 存在 {cnt} 条显式信号"
    finally:
        _cleanup(uid)
