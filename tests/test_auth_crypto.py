"""G1 鉴权密码学原语（agent/auth_crypto.py）单测。

纯 stdlib 实现，不依赖 MySQL / Ollama / Redis，验证：
- pbkdf2 密码哈希的「同密码同 salt 一致、随机 salt 不同」性质；
- base64url 编解码无填充往返；
- JWT 签发/验签往返、密钥篡改/过期/篡改签名/畸形均判 None。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config.settings as cs
from agent.auth_crypto import (
    _b64url_decode,
    _b64url_encode,
    hash_password,
    issue_token,
    verify_token,
)


def test_hash_password_roundtrip_with_same_salt():
    salt, h1 = hash_password("secret", iterations=1000)
    assert salt and h1
    _, h2 = hash_password("secret", salt=salt, iterations=1000)
    assert h1 == h2  # 同密码 + 同 salt => 同哈希


def test_hash_password_random_salt_differs():
    _, a = hash_password("secret", iterations=1000)
    _, b = hash_password("secret", iterations=1000)
    assert a != b  # 随机 salt => 即便同密码哈希也不同


def test_b64url_roundtrip():
    data = b"hello world+/="
    enc = _b64url_encode(data)
    assert "=" not in enc
    assert _b64url_decode(enc) == data


def test_issue_verify_roundtrip(monkeypatch):
    monkeypatch.setattr(cs.settings, "AUTH_SECRET", "test-secret")
    tok = issue_token("u1", "alice")
    assert tok.count(".") == 2  # header.payload.signature
    payload = verify_token(tok)
    assert payload["user_id"] == "u1"
    assert payload["username"] == "alice"
    assert payload["exp"] > payload["iat"]


def test_verify_wrong_secret(monkeypatch):
    monkeypatch.setattr(cs.settings, "AUTH_SECRET", "secret-A")
    tok = issue_token("u1", "alice")
    monkeypatch.setattr(cs.settings, "AUTH_SECRET", "secret-B")
    assert verify_token(tok) is None  # 密钥被篡改 => 验签失败


def test_verify_tampered_signature(monkeypatch):
    monkeypatch.setattr(cs.settings, "AUTH_SECRET", "s")
    tok = issue_token("u1", "alice")
    h, p, _ = tok.split(".")
    assert verify_token(f"{h}.{p}.deadbeef") is None


def test_verify_expired(monkeypatch):
    monkeypatch.setattr(cs.settings, "AUTH_SECRET", "s")
    tok = issue_token("u1", "alice", ttl=-10)  # 签发即过期
    assert verify_token(tok) is None


def test_verify_malformed():
    assert verify_token("not-a-token") is None
    assert verify_token("") is None
