"""G1 鉴权依赖注入（api/deps.py）单测。

验证 get_current_user 的解析优先级：
- Bearer token 优先；
- 无 Bearer 时回退 httpOnly Cookie（ma_token）；
- AUTH_ENABLED=False 调试兼容：无 token 时按 query > body > state > default 取 user_id；
- AUTH_ENABLED=True：无合法 token 必须 401。
用 FakeRequest 模拟 ASGI Request，避免起服务。
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

import pytest

import api.deps as deps_mod
import config.settings as cs
from api.deps import get_current_user


class FakeRequest:
    def __init__(self, headers=None, cookies=None, query=None, body=b""):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query_params = query or {}
        self._body = body
        self.state = SimpleNamespace()

    async def body(self):
        return self._body


def test_bearer_token(monkeypatch):
    monkeypatch.setattr(deps_mod, "verify_token", lambda t: {"user_id": "u1", "username": "alice"})
    req = FakeRequest(headers={"authorization": "Bearer tok"})
    u = asyncio.run(get_current_user(req))
    assert u.user_id == "u1" and u.username == "alice"


def test_cookie_token(monkeypatch):
    monkeypatch.setattr(deps_mod, "verify_token", lambda t: {"user_id": "u2", "username": "bob"})
    req = FakeRequest(cookies={cs.settings.AUTH_COOKIE_NAME: "tok"})
    u = asyncio.run(get_current_user(req))
    assert u.user_id == "u2"


def test_debug_fallback_query(monkeypatch):
    monkeypatch.setattr(deps_mod, "verify_token", lambda t: None)
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", False)
    req = FakeRequest(query={"user_id": "debuguser"})
    u = asyncio.run(get_current_user(req))
    assert u.user_id == "debuguser"


def test_auth_enabled_requires_token(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(deps_mod, "verify_token", lambda t: None)
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", True)
    req = FakeRequest()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_current_user(req))
    assert exc.value.status_code == 401
