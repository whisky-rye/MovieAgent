"""P2-17 回归：token 服务端吊销（jti 黑名单）+ 登录/注册按 IP+用户名限流。

修复前：
- AUTH_TOKEN_TTL 默认 7 天，logout 只清 Cookie——服务端仍认已签发 token，
  泄密 token 在剩余有效期内随时可用；
- /auth/login|register 无限流——可被无限暴力尝试/用户名枚举。

修复：issue_token 带 jti；verify_token 验签验期后查黑名单；logout 按剩余寿命
吊销当前 token 的 jti；auth 路由独立令牌桶（rate=1/6、burst=10）按 IP+用户名限流。
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import agent.token_blacklist as tb
from agent.auth_crypto import issue_token, verify_token
from agent.token_blacklist import is_revoked, revoke
from config.settings import settings


@pytest.fixture(autouse=True)
def _reset_blacklist():
    tb._reset_for_test()
    yield
    tb._reset_for_test()


def _decode_payload(token: str) -> dict:
    payload = token.split(".")[1]
    pad = "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload + pad))


# ---------- 黑名单本体 ----------

def test_revoke_and_query():
    revoke("jti-1", 3600)
    assert is_revoked("jti-1") is True
    assert is_revoked("jti-unknown") is False


def test_revoke_nonpositive_ttl_is_noop():
    revoke("jti-2", 0)
    revoke("jti-2", -5)
    assert is_revoked("jti-2") is False


def test_revoke_expires(monkeypatch):
    revoke("jti-3", 0.05)
    assert is_revoked("jti-3") is True
    time.sleep(0.08)
    assert is_revoked("jti-3") is False  # 过期自动失效（内存项同时被清）


def test_revoke_empty_jti_noop():
    revoke("", 3600)
    assert is_revoked("") is False


def test_memory_cap_evicts(monkeypatch):
    monkeypatch.setattr(tb, "_MEM_MAX", 3)
    for i in range(6):
        revoke(f"jti-cap-{i}", 3600)
    assert len(tb._mem) <= 3
    assert is_revoked("jti-cap-0") is False   # 最早的被逐出


# ---------- token：jti + 吊销生效 ----------

@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_SECRET", "test-secret-0123456789abcdef")


def test_token_has_unique_jti(secret):
    t1 = issue_token("u1", "alice")
    t2 = issue_token("u1", "alice")
    p1, p2 = _decode_payload(t1), _decode_payload(t2)
    assert p1["jti"] and p2["jti"] and p1["jti"] != p2["jti"], "同用户两次签发的 jti 必须不同"
    assert verify_token(t1)["user_id"] == "u1"


def test_revoked_token_rejected(secret):
    """核心安全属性：签名有效、未过期的 token，被 logout 吊销后 verify_token 必须拒。"""
    token = issue_token("u1", "alice")
    assert verify_token(token) is not None
    jti = _decode_payload(token)["jti"]
    revoke(jti, 3600)
    assert verify_token(token) is None, "已吊销 token 必须被拒"


def test_revocation_scoped_to_single_token(secret):
    """吊销一个 token 不影响同一用户的其它 token（按 jti 粒度）。"""
    t1 = issue_token("u1", "alice")
    t2 = issue_token("u1", "alice")
    revoke(_decode_payload(t1)["jti"], 3600)
    assert verify_token(t1) is None
    assert verify_token(t2) is not None, "其它 token 不应被连带吊销"


def test_expired_token_rejected_regardless(secret):
    token = issue_token("u1", "alice", ttl=-10)
    assert verify_token(token) is None


def test_revoke_with_remaining_ttl(secret):
    """按剩余寿命吊销：TTL 取 token 剩余有效期（黑名单项先于 token 过期，无缝衔接）。"""
    token = issue_token("u1", "alice", ttl=30)
    jti = _decode_payload(token)["jti"]
    payload = _decode_payload(token)
    remaining = payload["exp"] - time.time()
    assert remaining > 1  # 30s TTL：即便测试基础设施慢也不会误判为已过期
    revoke(jti, remaining)
    assert verify_token(token) is None


# ---------- logout 路由吊销 ----------

def _make_request(headers=None, cookies=None):
    from starlette.requests import Request
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": "POST", "path": "/auth/logout",
        "headers": raw_headers,
    }
    req = Request(scope)
    if cookies:
        req._cookies = cookies  # Starlette 从 headers 解析；直接注入供测试
    return req


def test_logout_revokes_current_token(secret):
    """logout 后，原 token 立即可用性为零（服务端吊销）。"""
    import api.routes.auth as auth_mod

    token = issue_token("u1", "alice")
    req = _make_request(headers={"Authorization": f"Bearer {token}"})
    out = auth_mod.logout(req, __import__("fastapi").Response())
    assert out == {"ok": True}
    assert verify_token(token) is None, "logout 必须吊销当前 token"


def test_logout_without_token_is_noop(secret):
    """未登录/无 token 的 logout 不炸、不吊销任何东西。"""
    import api.routes.auth as auth_mod

    req = _make_request()
    out = auth_mod.logout(req, __import__("fastapi").Response())
    assert out == {"ok": True}


# ---------- auth 路由限流 ----------

def _rl_request(ip="9.9.9.9"):
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/auth/login",
                    "headers": [], "client": (ip, 1234)})


def test_auth_rate_limit_triggers_after_burst(monkeypatch):
    """同一 IP+用户名超过突发上限后 429（暴力爆破被挡）。"""
    import api.routes.auth as auth_mod
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "RATELIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATELIMIT_BACKEND", "memory")
    monkeypatch.setattr(auth_mod, "_auth_limiter", None)  # 重置单例
    monkeypatch.setattr(auth_mod, "_AUTH_RL_BURST", 2.0)  # 注入后需重建实例
    # 直接构造小容量限流器（绕过模块常量读取时机）
    from api.ratelimit import RateLimiter
    monkeypatch.setattr(auth_mod, "_auth_limiter",
                        RateLimiter(rate=0.0, burst=2.0))
    req = _rl_request()
    auth_mod._auth_rate_limit(req, "alice")   # 1
    auth_mod._auth_rate_limit(req, "alice")   # 2（burst 用尽）
    with pytest.raises(HTTPException) as ei:
        auth_mod._auth_rate_limit(req, "alice")  # 3 → 429
    assert ei.value.status_code == 429
    assert "Retry-After" in ei.value.headers


def test_auth_rate_limit_per_username(monkeypatch):
    """限流键含用户名：换用户名不继承他人额度（各自 10 次突发）。"""
    import api.routes.auth as auth_mod
    from api.ratelimit import RateLimiter

    monkeypatch.setattr(settings, "RATELIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATELIMIT_BACKEND", "memory")
    monkeypatch.setattr(auth_mod, "_auth_limiter", RateLimiter(rate=0.0, burst=1.0))
    req = _rl_request()
    auth_mod._auth_rate_limit(req, "alice")       # alice 用尽
    auth_mod._auth_rate_limit(req, "bob")         # bob 独立额度 → 放行
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        auth_mod._auth_rate_limit(req, "bob")     # bob 第二次 → 429


def test_auth_rate_limit_disabled_never_blocks(monkeypatch):
    import api.routes.auth as auth_mod

    monkeypatch.setattr(settings, "RATELIMIT_ENABLED", False)
    monkeypatch.setattr(auth_mod, "_auth_limiter", None)
    for _ in range(20):
        auth_mod._auth_rate_limit(_rl_request(), "alice")  # 不抛即通过
