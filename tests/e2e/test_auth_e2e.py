"""鉴权链路 e2e：注册/登录/me/登出，以及登出后 /auth/me 必须 401 的红线。

验证红线需在 AUTH_ENABLED=true 下进行（调试态会回退 default 用户），故用例内强制开启。
"""
import uuid

import pytest
from sqlalchemy import create_engine, text

from config.settings import settings


def _alive():
    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _alive(), reason="MySQL 不可用，跳过后端 e2e")


def _cleanup(uname):
    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
        with e.connect() as c:
            c.execute(text("DELETE FROM users WHERE username=:u"), {"u": uname})
            c.commit()
    except Exception:  # noqa: BLE001
        pass


def test_register_login_me_logout_redline(client, monkeypatch):
    # 红线验证需要生产鉴权态
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    uname = f"e2e_auth_{uuid.uuid4().hex[:10]}"
    try:
        r = client.post("/auth/register", json={"username": uname, "password": "secret123"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["username"] == uname and body["token"]
        assert settings.AUTH_COOKIE_NAME in r.cookies

        r = client.get("/auth/me")
        assert r.status_code == 200 and r.json()["username"] == uname

        assert client.post("/auth/logout").status_code == 200

        # 红线：登出后再访问 /auth/me 必须 401（不凭空判定已登录）
        assert client.get("/auth/me").status_code == 401
    finally:
        _cleanup(uname)


def test_login_wrong_password(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    uname = f"e2e_auth_{uuid.uuid4().hex[:10]}"
    try:
        client.post("/auth/register", json={"username": uname, "password": "secret123"})
        r = client.post("/auth/login", json={"username": uname, "password": "wrongpass"})
        assert r.status_code == 401
    finally:
        _cleanup(uname)


def test_duplicate_register(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    uname = f"e2e_auth_{uuid.uuid4().hex[:10]}"
    try:
        client.post("/auth/register", json={"username": uname, "password": "secret123"})
        r = client.post("/auth/register", json={"username": uname, "password": "secret123"})
        assert r.status_code == 409
    finally:
        _cleanup(uname)
