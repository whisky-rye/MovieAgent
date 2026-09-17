"""用户账户存储（G1）强 MySQL 行为验证：

1. backend() 恒为 "mysql"（绝不会是 "file" / "mysql->file"）。
2. create / get_by_username / get / verify 全链路读写正确；verify 密码哈希往返一致。
3. MySQL 不可达（构造时探测失败）直接抛 RuntimeError（fail-fast，拒绝启动），不静默回退 file。
依赖真实 MySQL（movie_agent.users）；用唯一 username/user_id 避免串扰。
"""
import uuid

import pytest

import agent.user_store as us_mod
import config.settings as settings_mod
from agent.auth_crypto import hash_password


@pytest.fixture
def store(mysql_ok):
    return us_mod.get_user_store()


def test_backend_is_mysql(store):
    assert store.backend() == "mysql"


def test_create_get_and_verify_roundtrip(store):
    username = f"ut_{uuid.uuid4().hex[:12]}"
    uid = f"uid_{uuid.uuid4().hex[:12]}"
    password = "Sup3rSecret!"
    salt, pw_hash = hash_password(password, iterations=settings_mod.settings.AUTH_PBKDF2_ITERATIONS)
    store.create(uid, username, salt, pw_hash)
    try:
        by_name = store.get_by_username(username)
        assert by_name is not None
        assert by_name["user_id"] == uid
        assert by_name["salt"] == salt
        assert by_name["pw_hash"] == pw_hash

        by_id = store.get(uid)
        assert by_id is not None
        assert by_id["username"] == username

        # verify 用同一密码应命中，错误密码应返回 None
        assert store.verify(username, password) == uid
        assert store.verify(username, "wrong-password") is None
    finally:
        # 清理：直接物理删除，避免唯一 username 残留影响后续运行
        with store._engine.connect() as c:
            c.execute(
                __import__("sqlalchemy").text("DELETE FROM users WHERE username=:u"),
                {"u": username},
            )
            c.commit()


def test_fail_fast_on_unreachable_mysql(monkeypatch):
    """构造时 MySQL 不可达 -> RuntimeError（强 MySQL，无 file 兜底）。"""
    monkeypatch.setattr(
        settings_mod.Settings,
        "MOVIE_AGENT_MYSQL_DSN",
        property(lambda self: "mysql+pymysql://nouser:nopass@127.0.0.1:3399/none"),
    )
    with pytest.raises(RuntimeError):
        us_mod.MySQLUserStore()
