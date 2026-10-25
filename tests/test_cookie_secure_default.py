"""P2-18 回归：Cookie Secure 默认 True（仅 local 环境关闭）。

修复前：COOKIE_SECURE=None 时跟随 AUTH_ENABLED——调试态（AUTH_ENABLED=false）
下 Secure 也为 false，token 在 HTTP 部署/混合内容场景可被网络层窃听（与 P0-1
叠加时风险放大）。

修复：未显式配置时，仅 APP_ENV=local 关闭 Secure，其余环境一律 True；
COOKIE_SECURE 显式值仍最高优先（内网 HTTP 联调可显式 false）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import api.routes.auth as auth_mod
from config.settings import settings


def _cookie_flags(resp) -> list[str]:
    """取响应里所有 Set-Cookie 头（Starlette Headers.getlist）。"""
    return resp.headers.getlist("set-cookie")


# ---------- _cookie_secure 判定矩阵 ----------

def test_default_local_env_not_secure(monkeypatch):
    """本地开发（APP_ENV=local）默认关闭——HTTP 联调可用。"""
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    monkeypatch.setattr(settings, "APP_ENV", "local")
    assert auth_mod._cookie_secure() is False


def test_default_production_secure(monkeypatch):
    """生产默认 True——token 仅 HTTPS 传输。"""
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    monkeypatch.setattr(settings, "APP_ENV", "production")
    assert auth_mod._cookie_secure() is True


def test_default_other_env_secure(monkeypatch):
    """非 local 的任意环境（staging/test/…）默认 True（白名单思维）。"""
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    for env in ("production", "staging", "test", ""):
        monkeypatch.setattr(settings, "APP_ENV", env)
        assert auth_mod._cookie_secure() is True, f"APP_ENV={env!r} 应强制 Secure"


def test_explicit_override_wins(monkeypatch):
    """显式 COOKIE_SECURE 最高优先（内网 HTTP 联调显式 false / 强制 true）。"""
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "APP_ENV", "production")
    assert auth_mod._cookie_secure() is False
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    monkeypatch.setattr(settings, "APP_ENV", "local")
    assert auth_mod._cookie_secure() is True


def test_debug_auth_mode_still_secure_in_nonlocal(monkeypatch):
    """核心场景：AUTH_ENABLED=false（调试）+ 非 local 环境 → 仍 Secure。

    修复前此处跟随 AUTH_ENABLED 返回 false，token 在 HTTP 部署下明文传输。
    """
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(settings, "APP_ENV", "production")
    assert auth_mod._cookie_secure() is True


# ---------- 实际下发的 Cookie 带 Secure 标志 ----------

def test_set_auth_cookies_emits_secure_in_production(monkeypatch):
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    monkeypatch.setattr(settings, "APP_ENV", "production")
    from fastapi import Response
    resp = Response()
    auth_mod._set_auth_cookies(resp, "fake.token.value")
    flags = _cookie_flags(resp)
    assert flags, "应下发 Set-Cookie"
    # 两个 Cookie（ma_token/ma_csrf）都必须带 Secure
    assert all("Secure" in f for f in flags), f"存在未加 Secure 的 Cookie: {flags}"
    assert any("HttpOnly" in f for f in flags), "ma_token 应保持 HttpOnly"


def test_set_auth_cookies_no_secure_in_local(monkeypatch):
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    monkeypatch.setattr(settings, "APP_ENV", "local")
    from fastapi import Response
    resp = Response()
    auth_mod._set_auth_cookies(resp, "fake.token.value")
    flags = _cookie_flags(resp)
    assert flags and all("Secure" not in f for f in flags), f"local 不应带 Secure: {flags}"
