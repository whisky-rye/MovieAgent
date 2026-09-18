"""O11 用户反馈闭环：反馈采集 + 状态查询接口。

- POST /feedback：前端赞踩/点击落库（user_feedback）；`cancel` 表示撤销该作品的赞/踩。
- GET  /feedback/mine：返回当前用户显式操作过的作品及状态，供前端刷新/重进会话后恢复。
- 鉴权：复用 get_current_user（AUTH_ENABLED=false 时退 default，与推荐一致）。
- 失败仅影响个性化，不影响业务；接口层异常由 FastAPI 转 4xx/5xx。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import CurrentUser, get_current_user
from agent.feedback_store import (
    get_feedback_store,
    SIGNAL_EXPLICIT_LIKE,
    SIGNAL_EXPLICIT_DISLIKE,
    SIGNAL_CLICK,
)

logger = logging.getLogger("movie_agent.feedback")

router = APIRouter()

# 前端信号 → 存储信号
_SIGNAL_MAP = {
    "like": SIGNAL_EXPLICIT_LIKE,
    "dislike": SIGNAL_EXPLICIT_DISLIKE,
    "click": SIGNAL_CLICK,
}

# 「撤销赞/踩」：不走 _SIGNAL_MAP（语义是删除而非追加）
SIGNAL_CANCEL = "cancel"


class FeedbackReq(BaseModel):
    item_id: str
    signal: str  # like | dislike | click | cancel
    genres: list[str] | None = None
    rank: int | None = None
    query: str | None = None


@router.post("/feedback")
def post_feedback(req: FeedbackReq, auth: CurrentUser = Depends(get_current_user)):
    if req.signal == SIGNAL_CANCEL:
        get_feedback_store().clear_item(auth.user_id, req.item_id)
        return {"ok": True, "state": None}

    sig = _SIGNAL_MAP.get(req.signal)
    if sig is None:
        raise HTTPException(status_code=400, detail=f"invalid signal: {req.signal}")
    get_feedback_store().record(
        auth.user_id, req.item_id, sig,
        genres=req.genres, rank=req.rank, query=req.query,
    )
    return {"ok": True, "state": req.signal}


@router.get("/feedback/mine")
def get_my_feedback(auth: CurrentUser = Depends(get_current_user)):
    """当前用户已赞/已踩的作品（{item_id: "like"|"dislike"}），用于页面状态恢复。"""
    return {"items": get_feedback_store().get_explicit_map(auth.user_id)}
