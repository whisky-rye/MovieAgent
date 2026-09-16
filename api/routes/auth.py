"""G1 鉴权路由：/auth/register /auth/login /auth/logout /auth/me。

- 注册：生成 user_id = uuid4().hex（稳定主键，前后端强一致）；密码经 pbkdf2_hmac 存 salt+pw_hash。
- 登录：校验密码 → 签发 token → 同时下发两类 Cookie：
    · ma_token（HttpOnly）：JWT，JS 无法读取，防 XSS 窃取；用于后端验签。
    · ma_csrf（可读）：随机值，前端读取后放入 X-CSRF-Token 头做双提交校验，防 CSRF。
- /auth/logout：清除两类 Cookie（前端调它即可退出，无需再管 localStorage）。
- /auth/me：回显当前登录用户（受保护）。
- 零新依赖：密码/ token 全部用 stdlib（见 agent/auth_crypto.py）。
"""
from __future__ import annotations

import os
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from agent.auth_crypto import hash_password, issue_token
from agent.user_store import get_user_store
from api.deps import CurrentUser, get_current_user
from config.settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterReq(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, description="登录用户名")
    password: str = Field(..., min_length=6, max_length=128, description="登录密码")


class LoginReq(BaseModel):
    username: str = Field(..., min_length=1, description="登录用户名")
    password: str = Field(..., min_length=1, description="登录密码")


def _set_auth_cookies(resp: Response, token: str) -> None:
    """下发 ma_token（HttpOnly）+ ma_csrf（可读）双 Cookie。

    - Secure 仅在非本地（生产/HTTPS）开启，本地 HTTP 下若设 Secure 会导致 Cookie 不被发送。
    - SameSite=Lax：允许前端(5173) ↔ 后端(8000) 同站跨端口携带，同时挡住跨站 CSRF 顶层的 POST。
    - 仍返回 JSON 中的 token，便于非浏览器 API 客户端（Bearer）使用；浏览器前端只依赖 Cookie。
    """
    secure = settings.COOKIE_SECURE or (settings.APP_ENV == "production")
    max_age = settings.AUTH_TOKEN_TTL
    resp.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )
    csrf = secrets.token_urlsafe(32)
    resp.set_cookie(
        key=settings.CSRF_COOKIE_NAME,
        value=csrf,
        max_age=max_age,
        httponly=False,  # 前端 JS 需读取以发起双提交校验
        secure=secure,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )


def _clear_auth_cookies(resp: Response) -> None:
    for name in (settings.AUTH_COOKIE_NAME, settings.CSRF_COOKIE_NAME):
        resp.delete_cookie(name, path="/")


@router.post("/register")
def register(req: RegisterReq, response: Response):
    store = get_user_store()
    if store.get_by_username(req.username) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user_id = uuid.uuid4().hex
    salt, pw_hash = hash_password(req.password, iterations=settings.AUTH_PBKDF2_ITERATIONS)
    if store.get(user_id) is not None:  # 极小概率 UUID 碰撞：重生成一次
        user_id = uuid.uuid4().hex
    store.create(user_id, req.username, salt, pw_hash)
    token = issue_token(user_id, req.username)
    _set_auth_cookies(response, token)
    return {"user_id": user_id, "username": req.username, "token": token}


@router.post("/login")
def login(req: LoginReq, response: Response):
    store = get_user_store()
    if store.get_by_username(req.username) is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    uid = store.verify(req.username, req.password)
    if uid is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = issue_token(uid, req.username)
    _set_auth_cookies(response, token)
    return {"user_id": uid, "username": req.username, "token": token}


@router.post("/logout")
def logout(response: Response):
    _clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
def me(auth: CurrentUser = Depends(get_current_user)):
    return auth.dict()
