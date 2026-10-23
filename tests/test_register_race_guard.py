"""P0-4 注册竞态防护回归：同名注册不得覆盖他人账号。

曾经的缺陷：`MySQLUserStore.create` 使用
`INSERT ... ON DUPLICATE KEY UPDATE user_id/salt/pw_hash 全列覆盖`，
而注册是「查重 → 插入」两步非原子操作——两个并发同名注册会让后到者把
先到者的 user_id/salt/pw_hash 全部改写（先到者 token 立即失效、偏好/
会话按 user_id 全部成孤儿数据），等同账号接管。

修复后：create 为纯 INSERT，依赖 username 主键唯一约束；并发输家抛
IntegrityError，路由层转 409，先到者记录原样保留。

覆盖：
1. store 层：同名第二次 create 抛 IntegrityError，且原记录不被覆盖、
   原密码仍可登录（依赖 MySQL，离线自动 skip）；
2. 路由层：create 抛 IntegrityError → 409（不 500、不覆盖）。
"""
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

import agent.user_store as us_mod
from agent.auth_crypto import hash_password

try:  # api 导入会探业务库（强 MySQL），离线时跳走路由层用例
    import api.routes.auth as auth_mod
    import api.main as main_mod

    _API_IMPORT_ERR = None
except Exception as e:  # noqa: BLE001
    _API_IMPORT_ERR = e


@pytest.fixture
def store(mysql_ok):
    return us_mod.get_user_store()


def _cleanup(username: str) -> None:
    from config.settings import settings

    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
        with e.connect() as c:
            c.execute(text("DELETE FROM users WHERE username=:u"), {"u": username})
            c.commit()
        e.dispose()
    except Exception:  # noqa: BLE001 - 清理失败不影响断言结论
        pass


def test_duplicate_username_create_raises_and_keeps_original(store):
    """同名第二次 create 必须抛 IntegrityError，且第一条记录不被覆盖。"""
    uname = f"race_{uuid.uuid4().hex[:10]}"
    uid1 = uuid.uuid4().hex
    salt1, h1 = hash_password("secret123")  # 默认迭代次数与 verify 口径一致
    store.create(uid1, uname, salt1, h1)
    try:
        # 并发注册的「输家」：同用户名、不同 user_id/密码
        uid2 = uuid.uuid4().hex
        salt2, h2 = hash_password("other-pass")
        with pytest.raises(IntegrityError):
            store.create(uid2, uname, salt2, h2)
        # 先到者账号必须原样保留（user_id / pw_hash 均未被改写）
        rec = store.get_by_username(uname)
        assert rec is not None
        assert rec["user_id"] == uid1
        assert rec["pw_hash"] == h1
        # 原密码仍可登录；输家的密码不被接受
        assert store.verify(uname, "secret123") == uid1
        assert store.verify(uname, "other-pass") is None
    finally:
        _cleanup(uname)


@pytest.mark.skipif(_API_IMPORT_ERR is not None,
                    reason=f"api 导入失败（业务库不在线）: {_API_IMPORT_ERR}")
def test_register_integrity_error_returns_409(monkeypatch):
    """路由层：create 抛 IntegrityError（并发同名注册输家）→ 409，不 500。"""

    class _RacyStore:
        def get_by_username(self, username):
            return None  # 查重时尚未存在（并发窗口）

        def get(self, user_id):
            return None

        def create(self, *a, **k):
            raise IntegrityError("INSERT INTO users ...", None, None)

        def verify(self, *a, **k):
            return None

    monkeypatch.setattr(auth_mod, "get_user_store", lambda: _RacyStore())
    from fastapi.testclient import TestClient

    c = TestClient(main_mod.app)
    r = c.post("/auth/register", json={"username": "raceuser123", "password": "secret123"})
    assert r.status_code == 409, r.text
