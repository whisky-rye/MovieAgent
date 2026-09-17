"""会话共享存储（G-Share）强 MySQL 行为验证：

1. backend() 恒为 "mysql"。
2. create（一个会话至多一个令牌）/ get_session_by_token / get_token_by_session /
   is_shared / delete 全链路正确，令牌不可猜测。
3. MySQL 不可达（构造时探测失败）直接抛 RuntimeError（fail-fast），不静默回退 file。
依赖真实 MySQL（movie_agent.shared_sessions）；用唯一 session_id，结束后清理。
"""
import secrets
import uuid

import pytest

import agent.share_store as sh_mod
import config.settings as settings_mod


@pytest.fixture
def store(mysql_ok):
    return sh_mod.get_share_store()


def _cleanup(store, sid):
    try:
        store.delete(sid)
    except Exception:
        pass


def test_backend_is_mysql(store):
    assert store.backend() == "mysql"


def test_share_lifecycle(store):
    sid = f"sh_{uuid.uuid4().hex[:12]}"
    try:
        token = store.create(sid, "owner_uid")
        assert token and len(token) >= 16  # secrets.token_hex(16)
        assert store.is_shared(sid) is True
        assert store.get_token_by_session(sid) == token
        assert store.get_session_by_token(token) == sid
        # 不存在的令牌 -> None
        assert store.get_session_by_token(secrets.token_hex(16)) is None

        # 重新共享同一会话应轮换令牌（仍是单一有效令牌）
        token2 = store.create(sid, "owner_uid")
        assert token2 != token
        assert store.get_session_by_token(token) is None  # 旧令牌失效
        assert store.get_session_by_token(token2) == sid
    finally:
        _cleanup(store, sid)


def test_fail_fast_on_unreachable_mysql(monkeypatch):
    monkeypatch.setattr(
        settings_mod.Settings,
        "MOVIE_AGENT_MYSQL_DSN",
        property(lambda self: "mysql+pymysql://nouser:nopass@127.0.0.1:3399/none"),
    )
    with pytest.raises(RuntimeError):
        sh_mod.MySQLShareStore()
