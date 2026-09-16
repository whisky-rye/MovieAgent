"""G3 会话管理对外接口（G1 落地后，user_id 统一由 get_current_user 依赖注入，忽略请求体里的 user_id 防越权）。

- POST   /sessions            -> {session_id}                 新建会话
- GET    /sessions            -> [{session_id,title,updated_at,preview}]  列出当前用户会话
- GET    /sessions/{id}       -> {session_id,user_id,title,created_at,updated_at,messages}  取完整历史
- PATCH  /sessions/{id}       -> {session_id,title}           改名
- DELETE /sessions/{id}       -> 204                          删除

归属校验：所有读取/删除前校验 session.user_id == 当前 user_id，否则 403（纵深防御）。
消息落库不暴露独立接口——由 /recommend/stream 的 done 事件后内部调用 SessionStore.append_message，
保证「对话即落库」且前后端单一写入点，避免双写不一致。

底层复用 agent.session_store.get_session_store()（MySQL 优先 + file 兜底）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from agent.session_store import get_session_store
from agent.share_store import get_share_store
from api.deps import CurrentUser, get_current_user
from api.items_backfill import backfill_item_ids

router = APIRouter(prefix="/sessions", tags=["sessions"])

# 共享状态装饰：把「是否共享 / 共享令牌」附加到会话对象上，供前端展示「已共享」徽标与复制链接。
_share_store = get_share_store()


def _decorate_share(session: dict) -> dict:
    token = _share_store.get_token_by_session(session.get("session_id"))
    session["shared"] = bool(token)
    session["share_token"] = token
    return session


class SessionCreateReq(BaseModel):
    # 兼容旧契约保留该字段，但真实 user_id 一律取自鉴权依赖（auth.user_id），本字段被忽略
    user_id: str = Field(default="default", description="(已弃用) user_id 现由鉴权依赖注入")
    title: Optional[str] = Field(default=None, description="会话标题（可选，默认'新会话'）")


class SessionRenameReq(BaseModel):
    title: str = Field(..., min_length=1, description="新标题")


@router.post("")
def create_session(req: SessionCreateReq, auth: CurrentUser = Depends(get_current_user)):
    sid = get_session_store().create(auth.user_id, req.title)
    return {"session_id": sid}


@router.get("")
def list_sessions(auth: CurrentUser = Depends(get_current_user)):
    items = get_session_store().list_by_user(auth.user_id)
    return [_decorate_share(s) for s in items]


def _require_owner(session_id: str, user_id: str) -> dict:
    """取会话并校验归属：不存在 404；存在但非本人 403。"""
    store = get_session_store()
    rec = store.get(session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if rec.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return rec


@router.get("/{session_id}")
def get_session(session_id: str, auth: CurrentUser = Depends(get_current_user)):
    rec = _require_owner(session_id, auth.user_id)
    # 历史消息里旧推荐卡片可能缺外部 id：按 item.id 回填，保证详情页外链可用
    for msg in rec.get("messages") or []:
        if isinstance(msg, dict) and msg.get("items"):
            backfill_item_ids(msg["items"])
    return _decorate_share(rec)


@router.patch("/{session_id}")
def rename_session(
    session_id: str, req: SessionRenameReq, auth: CurrentUser = Depends(get_current_user)
):
    _require_owner(session_id, auth.user_id)
    get_session_store().rename(session_id, req.title)
    return {"session_id": session_id, "title": req.title}


@router.delete("/{session_id}")
def delete_session(session_id: str, auth: CurrentUser = Depends(get_current_user)):
    _require_owner(session_id, auth.user_id)
    get_session_store().delete(session_id)
    return Response(status_code=204)
