"""P0-1 鉴权单开关（AUTH_ENABLED）安全语义单测。

覆盖：
- config.settings.validate_auth_config 纯函数：开启+弱/空/短密钥 → 拒绝启动（RuntimeError）；
  关闭 → 返回告警文案；
- agent.auth_crypto.verify_token：AUTH_SECRET 未配置时不采信任何 token
  （空密钥签名可离线伪造，此为核心修复）；
- api.deps.get_current_user：调试态（AUTH_ENABLED=false）下伪造 token 不得冒充他人，
  body/query 的 user_id 才是身份来源；配置了密钥时合法 token 仍被采信（开发流不断）；
- api.routes.auth._cookie_secure：Secure 跟随总开关，显式 COOKIE_SECURE 可覆盖。

api 依赖导入放在 try 中：MySQL 离线时相关用例自动 skip（纯函数用例不受影响）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

import pytest

import config.settings as cs
from config.settings import validate_auth_config
from agent.auth_crypto import issue_token, verify_token

try:  # api 导入会探业务库（强 MySQL），离线时跳过 api 层用例
    import api.deps as deps_mod
    import api.routes.auth as auth_routes_mod
    from api.deps import get_current_user

    _API_IMPORT_ERR = None
except Exception as e:  # noqa: BLE001
    _API_IMPORT_ERR = e

_api_skip = pytest.mark.skipif(
    _API_IMPORT_ERR is not None, reason=f"api 导入失败（业务库不在线）: {_API_IMPORT_ERR}"
)


# ---------- validate_auth_config（纯函数） ----------

def test_validate_disabled_returns_warning():
    warn = validate_auth_config(False, "")
    assert warn and "AUTH_ENABLED=false" in warn


def test_validate_enabled_empty_secret_raises():
    with pytest.raises(RuntimeError, match="AUTH_SECRET"):
        validate_auth_config(True, "")


@pytest.mark.parametrize("weak", ["changeme", "secret", "password", "123456", "short"])
def test_validate_enabled_weak_or_short_secret_raises(weak):
    with pytest.raises(RuntimeError):
        validate_auth_config(True, weak)


def test_validate_enabled_strong_secret_ok():
    assert validate_auth_config(True, "a" * 32) is None


# ---------- verify_token：空密钥不采信（P0-1 核心） ----------

def test_verify_token_rejects_when_secret_unconfigured(monkeypatch):
    monkeypatch.setattr(cs.settings, "AUTH_SECRET", "")
    # 空密钥下签发的 token（等价于攻击者离线伪造）必须被拒绝
    tok = issue_token("victim", "alice")
    assert verify_token(tok) is None


def test_verify_token_ok_when_secret_configured(monkeypatch):
    monkeypatch.setattr(cs.settings, "AUTH_SECRET", "test-secret-0123456789")
    tok = issue_token("u1", "alice")
    assert verify_token(tok)["user_id"] == "u1"


# ---------- get_current_user：调试态伪造 token 不冒充（P0-1 核心） ----------

class _FakeRequest:
    def __init__(self, headers=None, cookies=None, query=None, body=b""):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query_params = query or {}
        self._body = body
        self.state = SimpleNamespace()

    async def body(self):
        return self._body


@_api_skip
def test_debug_mode_forged_token_not_trusted(monkeypatch):
    """AUTH_ENABLED=false 且未配置密钥：伪造 token 不得覆盖 body/query 的 user_id。"""
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(cs.settings, "AUTH_SECRET", "")
    forged = issue_token("victim-user", "alice")  # 空密钥签名，可被任意离线伪造
    req = _FakeRequest(
        headers={"authorization": f"Bearer {forged}"},
        query={"user_id": "debug-user"},
    )
    u = asyncio.run(get_current_user(req))
    assert u.user_id == "debug-user"  # 绝不等于 victim-user


@_api_skip
def test_debug_mode_valid_token_trusted_when_secret_set(monkeypatch):
    """调试态但配置了密钥：合法 token 仍被采信（保留带密钥的开发流）。"""
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", False)
    monkeypatch.setattr(cs.settings, "AUTH_SECRET", "test-secret-0123456789")
    tok = issue_token("real-user", "alice")
    req = _FakeRequest(headers={"authorization": f"Bearer {tok}"})
    u = asyncio.run(get_current_user(req))
    assert u.user_id == "real-user"


# ---------- Cookie Secure 派生 ----------

@_api_skip
def test_cookie_secure_follows_app_env(monkeypatch):
    """P2-18 新契约：Secure 跟随 APP_ENV（非 local 即 True），不再跟随 AUTH_ENABLED。

    旧契约（Secure 跟随 AUTH_ENABLED）会让调试态 token 明文传输，已废弃。
    """
    monkeypatch.setattr(cs.settings, "COOKIE_SECURE", None)
    monkeypatch.setattr(cs.settings, "APP_ENV", "local")
    assert auth_routes_mod._cookie_secure() is False   # 本地开发可 HTTP 联调
    monkeypatch.setattr(cs.settings, "APP_ENV", "production")
    assert auth_routes_mod._cookie_secure() is True    # 非 local 一律 Secure
    # AUTH_ENABLED 不再影响 Secure（新旧契约的分界）
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", False)
    assert auth_routes_mod._cookie_secure() is True
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(cs.settings, "APP_ENV", "local")
    assert auth_routes_mod._cookie_secure() is False


@_api_skip
def test_cookie_secure_explicit_override(monkeypatch):
    monkeypatch.setattr(cs.settings, "COOKIE_SECURE", True)
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", False)
    assert auth_routes_mod._cookie_secure() is True  # 显式覆盖优先
    monkeypatch.setattr(cs.settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", True)
    assert auth_routes_mod._cookie_secure() is False  # 内网 HTTP 联调可显式关闭
