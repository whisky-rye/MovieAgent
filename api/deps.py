"""G1 鉴权依赖：get_current_user 注入当前登录用户（user_id / username）。

设计要点：
- AUTH_ENABLED=false（调试兼容）：优先取 query 参数 user_id，其次请求体 user_id，
  再退 request.state.user_id；不要求 token，现有 curl/前端联调不中断。
- AUTH_ENABLED=true（生产鉴权）：必须 `Authorization: Bearer <token>`，验签/验期失败 → 401；
  成功则把 token 内的 user_id 注入，忽略请求体里的 user_id（防越权读取他人记忆）。
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
    if not settings.AUTH_ENABLED:
        # 调试兼容：query > body > state，最终兜底 default
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

    # 生产鉴权：必须 Bearer token
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录或缺少 token")
    data = verify_token(auth[7:])
    if data is None:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    request.state.user_id = data["user_id"]
    return CurrentUser(user_id=data["user_id"], username=data.get("username"))
