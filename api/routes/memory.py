"""G4 偏好记忆对外接口（G1 鉴权落地前，临时用 body/query 透传 user_id，与 /recommend 同口径）。

- GET    /memory/preferences?user_id=   -> {user_id, preferences}
- PUT    /memory/preferences           -> body {user_id, genres?, note?, preferences?} 合并更新后返回
- DELETE /memory/preferences?user_id=   -> 清空偏好（保留 history）
- GET    /memory/history?user_id=       -> {user_id, history}

底层复用 agent.memory.get_memory_store()：G4 落地后该 store 指向 movie_agent.user_preferences 表，
因此前端读到的偏好与 Agent remember_preference 工具写入的偏好是同一份，天然打通（单一数据源）。
G1 完成后，把 user_id 来源从「请求体/查询参数」切换为 Depends(get_current_user) 即可，路由契约不变。
"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agent.memory import get_memory_store

router = APIRouter(prefix="/memory", tags=["memory"])


class PrefPutReq(BaseModel):
    user_id: str = Field(
        default="default",
        description="用户 UUID（G1 落地前暂由前端透传，与 /recommend 同口径）",
    )
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
def get_preferences(user_id: str = "default"):
    store = get_memory_store()
    return {"user_id": user_id, "preferences": store.get_prefs(user_id)}


@router.put("/preferences")
def put_preferences(req: PrefPutReq):
    prefs = _merge_prefs(req.user_id, req.genres, req.note, req.preferences)
    return {"user_id": req.user_id, "preferences": prefs}


@router.delete("/preferences")
def delete_preferences(user_id: str = "default"):
    store = get_memory_store()
    d = store.load(user_id)
    d["preferences"] = {}
    store.save(user_id, d)
    return {"user_id": user_id, "preferences": {}}


@router.get("/history")
def get_history(user_id: str = "default"):
    store = get_memory_store()
    return {"user_id": user_id, "history": store.load(user_id).get("history", [])}
