"""G1 鉴权依赖：get_current_user 注入当前登录用户（user_id / username）。

设计要点（P0-1 起，整个鉴权功能由单一 env 变量 AUTH_ENABLED 总控）：
- AUTH_ENABLED=true（鉴权态）：必须携带合法 Bearer/Cookie token，验签/验期失败 → 401；
  成功则把 token 内的 user_id 注入，忽略请求体里的 user_id（防越权读取他人记忆）。
  该模式下启动校验强制 AUTH_SECRET 为强密钥（否则拒绝启动）。
- AUTH_ENABLED=false（调试兼容）：无有效 token 时按 query > body > state > default 取 user_id。
  注意：AUTH_SECRET 未配置时 verify_token 不采信任何 token（空密钥签名可离线伪造），
  故此模式下 body/query 传的 user_id 就是身份来源，仅限本地联调。
- 无论哪种模式，都把权威 user_id 写回 request.state.user_id，供日志/下游使用。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request

from agent.auth_crypto import verify_token
from config.settings import settings

logger = logging.getLogger("movie_agent.auth")


class CurrentUser:
    def __init__(self, user_id: str, username: Optional[str] = None):
        self.user_id = user_id
        self.username = username

    def dict(self) -> dict:
        return {"user_id": self.user_id, "username": self.username}


async def get_current_user(request: Request) -> CurrentUser:
    # 先解析 Bearer token（两种模式下一视同仁：合法 token 永远优先，便于 API 客户端）
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else None
    # 浏览器走 httpOnly Cookie（ma_token），JS 不可读，防 XSS 窃取
    if not token:
        token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    payload = verify_token(token) if token else None  # 无效/过期返回 None

    if payload is not None:
        request.state.user_id = payload["user_id"]
        return CurrentUser(user_id=payload["user_id"], username=payload.get("username"))

    # 无有效 token
    if settings.AUTH_ENABLED:
        # 生产鉴权：必须携带合法 token
        raise HTTPException(status_code=401, detail="未登录或缺少 token")

    # 调试兼容（AUTH_ENABLED=false）：query > body > state > default
    uid = request.query_params.get("user_id")
    if not uid:
        try:
            body = await request.body()
            data = json.loads(body) if body else {}
            uid = data.get("user_id") if isinstance(data, dict) else None
        except Exception:
            uid = None
    uid = uid or getattr(request.state, "user_id", None) or "default"
    request.state.user_id = uid
    return CurrentUser(user_id=uid)
