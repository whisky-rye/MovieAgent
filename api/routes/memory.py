"""G4 偏好记忆对外接口（G1 落地后，user_id 统一由 get_current_user 依赖注入，忽略请求体里的 user_id 防越权）。

- GET    /memory/preferences -> {user_id, preferences}
- PUT    /memory/preferences -> body {genres?, note?, preferences?} 合并更新后返回
- DELETE /memory/preferences -> 清空偏好（保留 history）
- GET    /memory/history     -> {user_id, history}

底层复用 agent.memory.get_memory_store()：G4 落地后该 store 指向 movie_agent.user_preferences 表，
因此前端读到的偏好与 Agent remember_preference 工具写入的偏好是同一份，天然打通（单一数据源）。
"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from agent.memory import get_memory_store
from api.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/memory", tags=["memory"])


class PrefPutReq(BaseModel):
    # 兼容旧契约保留该字段，但真实 user_id 一律取自鉴权依赖（auth.user_id），本字段被忽略
    user_id: str = Field(default="default", description="(已弃用) user_id 现由鉴权依赖注入")
    genres: Optional[List[str]] = Field(default=None, description="追加的偏好类型（去重合并）")
    note: Optional[str] = Field(default=None, description="偏好备注（覆盖写）")
    preferences: Optional[dict] = Field(
        default=None,
        description="任意额外键值，合并进 preferences（genres 去重合并，其余覆盖写）",
    )


def _merge_prefs(
    user_id: str,
    genres: Optional[List[str]],
    note: Optional[str],
    extra: Optional[dict],
) -> dict:
    """合并更新偏好：genres 去重追加，note/其余键值覆盖写，最后落库返回最新 prefs。"""
    store = get_memory_store()
    d = store.load(user_id)
    prefs = d.setdefault("preferences", {})
    if genres:
        cur = prefs.setdefault("genres", [])
        for g in genres:
            if g and g not in cur:
                cur.append(g)
    if note is not None:
        prefs["note"] = note
    if extra:
        for k, v in extra.items():
            if k == "genres" and isinstance(v, list):
                cur = prefs.setdefault("genres", [])
                for g in v:
                    if g and g not in cur:
                        cur.append(g)
            else:
                prefs[k] = v
    store.save(user_id, d)
    return prefs


@router.get("/preferences")
def get_preferences(auth: CurrentUser = Depends(get_current_user)):
    store = get_memory_store()
    return {"user_id": auth.user_id, "preferences": store.get_prefs(auth.user_id)}


@router.put("/preferences")
def put_preferences(req: PrefPutReq, auth: CurrentUser = Depends(get_current_user)):
    prefs = _merge_prefs(auth.user_id, req.genres, req.note, req.preferences)
    return {"user_id": auth.user_id, "preferences": prefs}


@router.delete("/preferences")
def delete_preferences(auth: CurrentUser = Depends(get_current_user)):
    store = get_memory_store()
    d = store.load(auth.user_id)
    d["preferences"] = {}
    store.save(auth.user_id, d)
    return {"user_id": auth.user_id, "preferences": {}}


@router.get("/history")
def get_history(auth: CurrentUser = Depends(get_current_user)):
    store = get_memory_store()
    return {"user_id": auth.user_id, "history": store.load(auth.user_id).get("history", [])}
