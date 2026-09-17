"""会话存储（G3）强 MySQL 行为验证：

1. backend() 恒为 "mysql"。
2. create / list_by_user / get / append_message / rename / delete 全链路正确。
3. append_message 自动命名首条用户消息、超长消息截断、归属校验 assert_owner。
4. MySQL 不可达（构造时探测失败）直接抛 RuntimeError（fail-fast），不静默回退 file。
依赖真实 MySQL（movie_agent.sessions）；用唯一 user_id 避免串扰，结束后清理。
"""
import uuid

import pytest

import agent.session_store as ss_mod
import config.settings as settings_mod


@pytest.fixture
def store(mysql_ok):
    return ss_mod.get_session_store()


def _cleanup(store, sid):
    try:
        store.delete(sid)
    except Exception:
        pass


def test_backend_is_mysql(store):
    assert store.backend() == "mysql"


def test_lifecycle_and_owner(store):
    uid = f"su_{uuid.uuid4().hex[:12]}"
    # 用默认标题（"新会话"）创建，才能触发「首条用户消息自动命名」
    sid = store.create(uid)
    try:
        assert store.assert_owner(sid, uid) is True
        assert store.assert_owner(sid, "other") is False

        # 首条用户消息触发自动命名（默认标题下）
        store.append_message(sid, {"role": "user", "content": "推荐科幻片"})
        rec = store.get(sid)
        assert rec["title"] == "推荐科幻片"
        assert len(rec["messages"]) == 1

        # 追加助手消息
        store.append_message(sid, {"role": "assistant", "content": "好的，来看看…", "items": []})
        rec = store.get(sid)
        assert len(rec["messages"]) == 2

        # 列表按 updated_at 倒序，且包含本会话
        listing = store.list_by_user(uid)
        assert any(s["session_id"] == sid for s in listing)
        mine = next(s for s in listing if s["session_id"] == sid)
        assert mine["title"] == "推荐科幻片"
        assert mine["preview"]  # 预览取最后一条消息内容

        # 改名
        store.rename(sid, "改名后的清单")
        assert store.get(sid)["title"] == "改名后的清单"
    finally:
        _cleanup(store, sid)


def test_append_message_truncates_over_max(store):
    uid = f"su_{uuid.uuid4().hex[:12]}"
    sid = store.create(uid, "长会话")
    try:
        for i in range(store._max + 5):
            store.append_message(sid, {"role": "user", "content": f"msg-{i}"})
        rec = store.get(sid)
        assert len(rec["messages"]) == store._max
    finally:
        _cleanup(store, sid)


def test_fail_fast_on_unreachable_mysql(monkeypatch):
    monkeypatch.setattr(
        settings_mod.Settings,
        "MOVIE_AGENT_MYSQL_DSN",
        property(lambda self: "mysql+pymysql://nouser:nopass@127.0.0.1:3399/none"),
    )
    with pytest.raises(RuntimeError):
        ss_mod.MySQLSessionStore()
