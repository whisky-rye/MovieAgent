"""G1 鉴权密码学原语：stdlib only（不引 PyJWT / passlib / bcrypt，避开 Windows C 扩展构建坑）。

- hash_password：pbkdf2_hmac('sha256') + 随机 salt（注册/校验用）。
- issue_token / verify_token：HMAC-SHA256 签名的极简 JWT（header.payload.signature，base64url）。
  不依赖第三方 JWT 库，token 自包含 {user_id, username, iat, exp}，后端无状态校验。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from config.settings import settings


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def hash_password(
    password: str, salt: Optional[str] = None, iterations: int = 100000
) -> tuple[str, str]:
    """返回 (salt_hex, pw_hash_hex)。salt 为 None 时自动生成随机 salt。"""
    if salt is None:
        salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
    )
    return salt, dk.hex()


def issue_token(user_id: str, username: str, ttl: Optional[int] = None) -> str:
    """签发 token：base64url(header).base64url(payload).hmac_sig。"""
    ttl = ttl or settings.AUTH_TOKEN_TTL
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    now = int(time.time())
    payload = _b64url_encode(
        json.dumps(
            {"user_id": user_id, "username": username, "iat": now, "exp": now + ttl}
        ).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("utf-8")
    sig = hmac.new(
        settings.AUTH_SECRET.encode("utf-8"), signing_input, hashlib.sha256
    ).hexdigest()
    return f"{header}.{payload}.{sig}"


def verify_token(token: str) -> Optional[dict]:
    """验签 + 验期；任何失败返回 None（由依赖层转 401）。"""
    try:
        header, payload, sig = token.split(".")
    except Exception:
        return None
    signing_input = f"{header}.{payload}".encode("utf-8")
    expected = hmac.new(
        settings.AUTH_SECRET.encode("utf-8"), signing_input, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(_b64url_decode(payload))
    except Exception:
        return None
    if data.get("exp", 0) < int(time.time()):
        return None
    return data
