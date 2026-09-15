"""G1 鉴权路由：/auth/register /auth/login /auth/me。

- 注册：生成 user_id = uuid4().hex（稳定主键，前后端强一致）；密码经 pbkdf2_hmac 存 salt+pw_hash。
- 登录：校验密码 → 签发 token。
- /auth/me：回显当前登录用户（受保护，需 Bearer token 或调试态 user_id）。
- 零新依赖：密码/ token 全部用 stdlib（见 agent/auth_crypto.py）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
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


@router.post("/register")
def register(req: RegisterReq):
    store = get_user_store()
    if store.get_by_username(req.username) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user_id = uuid.uuid4().hex
    salt, pw_hash = hash_password(req.password, iterations=settings.AUTH_PBKDF2_ITERATIONS)
    # 极小概率 UUID 碰撞：重生成一次
    if store.get(user_id) is not None:
        user_id = uuid.uuid4().hex
    store.create(user_id, req.username, salt, pw_hash)
    token = issue_token(user_id, req.username)
    return {"user_id": user_id, "username": req.username, "token": token}


@router.post("/login")
def login(req: LoginReq):
    store = get_user_store()
    if store.get_by_username(req.username) is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    uid = store.verify(req.username, req.password)
    if uid is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = issue_token(uid, req.username)
    return {"user_id": uid, "username": req.username, "token": token}


@router.get("/me")
def me(auth: CurrentUser = Depends(get_current_user)):
    return auth.dict()
