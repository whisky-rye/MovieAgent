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
import secrets
import time
from typing import Optional

from agent.token_blacklist import is_revoked
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
    """签发 token：base64url(header).base64url(payload).hmac_sig。

    P2-17：payload 带 jti（随机唯一 ID）——logout 时按 jti 吊销，使服务端能
    在 token 自然过期前拒绝它（此前 7 天 TTL 内泄密 token 无法作废）。
    """
    ttl = ttl or settings.AUTH_TOKEN_TTL
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    now = int(time.time())
    payload = _b64url_encode(
        json.dumps(
            {"user_id": user_id, "username": username, "iat": now, "exp": now + ttl,
             "jti": secrets.token_hex(8)}
        ).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("utf-8")
    sig = hmac.new(
        settings.AUTH_SECRET.encode("utf-8"), signing_input, hashlib.sha256
    ).hexdigest()
    return f"{header}.{payload}.{sig}"


def verify_token(token: str) -> Optional[dict]:
    """验签 + 验期 + 查吊销黑名单；任何失败返回 None（由依赖层转 401）。

    安全红线（P0-1）：AUTH_SECRET 未配置（空/空白）时**不采信任何 token**——
    空密钥下 HMAC 签名可被任意离线伪造，此时若仍接受 token 等同于允许冒充任意用户。
    （合法部署由启动校验保证：AUTH_ENABLED=true 时 AUTH_SECRET 必为强密钥。）
    P2-17：验签验期通过后查 jti 黑名单——logout 吊销的 token 在此被拒。
    """
    if not token:
        return None
    secret = settings.AUTH_SECRET
    if not secret or not secret.strip():
        return None
    try:
        header, payload, sig = token.split(".")
    except Exception:
        return None
    signing_input = f"{header}.{payload}".encode("utf-8")
    expected = hmac.new(
        secret.encode("utf-8"), signing_input, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(_b64url_decode(payload))
    except Exception:
        return None
    if data.get("exp", 0) < int(time.time()):
        return None
    if is_revoked(str(data.get("jti") or "")):
        return None
    return data
