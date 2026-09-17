"""G1 鉴权路由（api/routes/auth.py）集成测试。

走真实 FastAPI app + 真实 MySQL（movie_agent.users，强 MySQL）。
覆盖：注册/登录/me/登出全链路；错误密码 401；重复注册 409；退出后 /auth/me 必须 401（红线）。
每个用例用唯一 username 并在结束后物理清理，避免串扰。
"""
import os
import sys
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as cs
import api.main as m
import pytest

client = TestClient(m.app)


@pytest.fixture(scope="module", autouse=True)
def require_mysql():
    try:
        e = create_engine(cs.settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MySQL 不可用，跳过鉴权路由测试：{exc}")


def _cleanup(uname):
    try:
        e = create_engine(cs.settings.MOVIE_AGENT_MYSQL_DSN)
        with e.connect() as c:
            c.execute(text("DELETE FROM users WHERE username=:u"), {"u": uname})
            c.commit()
    except Exception:
        pass


def test_register_login_me_logout():
    uname = f"auth_t_{uuid.uuid4().hex[:10]}"
    try:
        r = client.post("/auth/register", json={"username": uname, "password": "secret123"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["username"] == uname and body["token"]
        assert cs.settings.AUTH_COOKIE_NAME in r.cookies

        # 凭 Cookie 回显当前用户
        r2 = client.get("/auth/me")
        assert r2.status_code == 200 and r2.json()["username"] == uname

        # 登出清除 Cookie
        assert client.post("/auth/logout").status_code == 200

        # 红线：登出后再访问 /auth/me 必须 401（不凭空判定已登录）
        assert client.get("/auth/me").status_code == 401
    finally:
        _cleanup(uname)


def test_login_wrong_password():
    uname = f"auth_t_{uuid.uuid4().hex[:10]}"
    try:
        client.post("/auth/register", json={"username": uname, "password": "secret123"})
        r = client.post("/auth/login", json={"username": uname, "password": "wrongpass"})
        assert r.status_code == 401
    finally:
        _cleanup(uname)


def test_duplicate_register():
    uname = f"auth_t_{uuid.uuid4().hex[:10]}"
    try:
        client.post("/auth/register", json={"username": uname, "password": "secret123"})
        r = client.post("/auth/register", json={"username": uname, "password": "secret123"})
        assert r.status_code == 409
    finally:
        _cleanup(uname)


def test_login_then_me_with_bearer():
    uname = f"auth_t_{uuid.uuid4().hex[:10]}"
    try:
        client.post("/auth/register", json={"username": uname, "password": "secret123"})
        r = client.post("/auth/login", json={"username": uname, "password": "secret123"})
        assert r.status_code == 200
        tok = r.json()["token"]
        r2 = client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert r2.status_code == 200 and r2.json()["username"] == uname
    finally:
        _cleanup(uname)
