"""G1 鉴权路由：/auth/register /auth/login /auth/logout /auth/me。

- 注册：生成 user_id = uuid4().hex（稳定主键，前后端强一致）；密码经 pbkdf2_hmac 存 salt+pw_hash。
- 登录：校验密码 → 签发 token → 同时下发两类 Cookie：
    · ma_token（HttpOnly）：JWT，JS 无法读取，防 XSS 窃取；用于后端验签。
    · ma_csrf（可读）：随机值，前端读取后放入 X-CSRF-Token 头做双提交校验，防 CSRF。
- /auth/logout：清除两类 Cookie + **服务端吊销当前 token 的 jti**（P2-17）——
  此前只清 Cookie，已签发 token 在 7 天 TTL 内仍然有效，泄密 token 无法作废。
- 登录/注册按 IP+用户名限流（P2-17）：防暴力爆破与用户名枚举。
- /auth/me：回显当前登录用户（受保护）。
- 零新依赖：密码/ token 全部用 stdlib（见 agent/auth_crypto.py）。
"""
from __future__ import annotations

import os
import secrets
import threading
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from agent.auth_crypto import hash_password, issue_token, verify_token
from agent.token_blacklist import revoke
from agent.user_store import get_user_store
from api.ratelimit import RateLimiter
from config.settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])

# 登录/注册独立限流器（P2-17）：与 LLM 入口的限流器隔离——按「错误尝试」场景
# 设定容量/速率（突发 10 次，之后约每 6 秒 1 次）。正常用户输错几次密码不会被
# 误伤；脚本爆破/用户名枚举在十余次后必须等待。懒创建 + 锁（双检）。
_AUTH_RL_RATE = 1.0 / 6.0   # 每秒 1/6 个令牌 ≈ 每 6 秒 1 次
_AUTH_RL_BURST = 10.0       # 连续突发上限
_auth_limiter: RateLimiter | None = None
_auth_limiter_lock = threading.Lock()


def _auth_rate_limit(request: Request, username: str) -> None:
    """按 IP+用户名消耗 1 令牌；超限抛 429（带 Retry-After）。RATELIMIT_ENABLED=false 时跳过。"""
    global _auth_limiter
    if not settings.RATELIMIT_ENABLED:
        return
    with _auth_limiter_lock:
        if _auth_limiter is None:
            _auth_limiter = RateLimiter(rate=_AUTH_RL_RATE, burst=_AUTH_RL_BURST)
    ip = request.client.host if request.client else "unknown"
    ok, retry_after = _auth_limiter.consume(f"auth:{ip}:{(username or '').lower()}")
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )


def _current_token(request: Request) -> str | None:
    """从请求取当前 token（Bearer 优先，Cookie 兜底）——与 /auth/me 同口径。"""
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else None
    return token or request.cookies.get(settings.AUTH_COOKIE_NAME)


def _revoke_current_token(request: Request) -> None:
    """吊销当前 token 的 jti（P2-17）：TTL 取剩余寿命，过期后黑名单项自然消失。"""
    payload = verify_token(_current_token(request) or "")
    if not payload:
        return  # 无有效 token（如 Cookie 已清/未登录）→ 无需吊销
    import time as _time
    ttl = float(payload.get("exp", 0)) - _time.time()
    revoke(str(payload.get("jti") or ""), ttl)


class RegisterReq(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, description="登录用户名")
    password: str = Field(..., min_length=6, max_length=128, description="登录密码")


class LoginReq(BaseModel):
    username: str = Field(..., min_length=1, description="登录用户名")
    password: str = Field(..., min_length=1, description="登录密码")


def _cookie_secure() -> bool:
    """Cookie Secure 标志：跟随鉴权总开关 AUTH_ENABLED（true → 仅 HTTPS 传输）。

    - COOKIE_SECURE 显式配置（true/false）优先，可覆盖（如内网 HTTP 联调需显式 false）；
    - 未配置（None）时 = AUTH_ENABLED：鉴权开启即强制 Secure，杜绝 token 明文传输。
    """
    if settings.COOKIE_SECURE is not None:
        return settings.COOKIE_SECURE
    return settings.AUTH_ENABLED


def _set_auth_cookies(resp: Response, token: str) -> None:
    """下发 ma_token（HttpOnly）+ ma_csrf（可读）双 Cookie。

    - Secure 跟随鉴权总开关（见 _cookie_secure）：AUTH_ENABLED=true 时默认仅 HTTPS，
      杜绝 token 明文传输；本地 HTTP 联调可显式 COOKIE_SECURE=false 覆盖。
    - SameSite=Lax：允许前端(5173) ↔ 后端(8000) 同站跨端口携带，同时挡住跨站 CSRF 顶层的 POST。
    - 仍返回 JSON 中的 token，便于非浏览器 API 客户端（Bearer）使用；浏览器前端只依赖 Cookie。
    """
    secure = _cookie_secure()
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
def register(req: RegisterReq, request: Request, response: Response):
    # P2-17：按 IP+用户名限流，防批量注册/用户名枚举
    _auth_rate_limit(request, req.username)
    store = get_user_store()
    if store.get_by_username(req.username) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user_id = uuid.uuid4().hex
    salt, pw_hash = hash_password(req.password, iterations=settings.AUTH_PBKDF2_ITERATIONS)
    if store.get(user_id) is not None:  # 极小概率 UUID 碰撞：重生成一次
        user_id = uuid.uuid4().hex
    try:
        store.create(user_id, req.username, salt, pw_hash)
    except IntegrityError:
        # 并发同名注册：查重与插入两步非原子，输家由 username 主键唯一约束拦住。
        # 必须返回 409，绝不能覆盖前者账号（P0-4：曾经的 ON DUPLICATE KEY UPDATE
        # 会把前者 user_id/salt/pw_hash 全部改写，等同账号接管）。
        raise HTTPException(status_code=409, detail="用户名已存在")
    token = issue_token(user_id, req.username)
    _set_auth_cookies(response, token)
    return {"user_id": user_id, "username": req.username, "token": token}


@router.post("/login")
def login(req: LoginReq, request: Request, response: Response):
    # P2-17：按 IP+用户名限流，防暴力爆破（失败也计次——在查询前消耗）
    _auth_rate_limit(request, req.username)
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
def logout(request: Request, response: Response):
    # P2-17：清 Cookie 之外，服务端吊销当前 token 的 jti——否则已签发 token 在
    # 7 天 TTL 内仍然有效（泄密 token 无法作废，「退出登录」名不副实）。
    _revoke_current_token(request)
    _clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    # /auth/me 语义是"回显当前登录用户"，必须凭真实 token（Bearer 或 Cookie），
    # 不走 get_current_user 的调试兜底（AUTH_ENABLED=false 时无 token 也会 200 + default，
    # 导致退出登录后刷新页面被误判为已登录、直接进首页——已修复）。
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else None
    if not token:
        token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    payload = verify_token(token) if token else None
    if payload is None:
        raise HTTPException(status_code=401, detail="未登录")
    return {"user_id": payload["user_id"], "username": payload.get("username")}
