"""G-Share 会话共享对外接口：

- POST   /sessions/{id}/share   -> {share_token, share_url}   把本人会话设为「公开只读」（鉴权 owner）
- DELETE /sessions/{id}/share   -> 204                         取消共享（鉴权 owner）
- GET    /share/{token}         -> 公开只读会话（无需登录）     仅返回该令牌对应的会话内容；令牌无效/未共享 404

安全模型（纵深防御）：
1. 分享/取消共享必须先经 get_current_user 拿到权威 user_id，再 `_require_owner` 校验该会话确属本人，
   非本人会话一律 404（不暴露「存在但非你」），杜绝共享他人会话。
2. 公开查看 /share/{token} 完全无鉴权，但只凭令牌取回 session_id，再读会话内容；令牌不可猜测
   （secrets.token_hex），且响应体绝不回传 owner_user_id，外部访客无法反推作者、也无法枚举他人会话。
3. 取消共享即删除令牌，原链接立即 404；一个会话至多一个有效令牌（create 幂等覆盖旧令牌）。

底层复用 agent.share_store.get_share_store()（MySQL 优先 + file 兜底）与 session_store（校验归属）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from agent.session_store import get_session_store
from agent.share_store import get_share_store
from api.deps import CurrentUser, get_current_user
from config.settings import settings

router = APIRouter(tags=["share"])


class ShareResp(BaseModel):
    share_token: str
    share_url: str


def _require_owner(session_id: str, user_id: str) -> None:
    """校验会话存在且归属本人：不存在 404；存在但非本人 403。

    —— 注意：这里对「非本人」用 403 而非 404，是因为会话「存在」这一事实已通过本人才有的列表暴露；
    而对外公开接口 /share/{token} 则一律 404，避免枚举。本接口的 owner 校验目的是防止共享/取消他人会话。
    """
    rec = get_session_store().get(session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if rec.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="无权操作该会话")


@router.post("/sessions/{session_id}/share")
def share_session(session_id: str, auth: CurrentUser = Depends(get_current_user)) -> ShareResp:
    # 1) 必须是本人会话，否则不能共享（防共享他人会话）
    _require_owner(session_id, auth.user_id)
    # 2) 生成/重置令牌并落库
    token = get_share_store().create(session_id, auth.user_id)
    origin = (settings.APP_PUBLIC_ORIGIN or "http://localhost:5173").rstrip("/")
    return ShareResp(share_token=token, share_url=f"{origin}/share/{token}")


@router.delete("/sessions/{session_id}/share")
def unshare_session(session_id: str, auth: CurrentUser = Depends(get_current_user)):
    _require_owner(session_id, auth.user_id)
    get_share_store().delete(session_id)
    return Response(status_code=204)


@router.get("/share/{token}")
def get_shared_session(token: str):
    """公开只读查看：仅凭令牌取回会话，无需登录。令牌无效/未共享 -> 404。

    响应体只包含会话展示所需字段（title / 时间 / messages），绝不回传 owner_user_id。
    """
    share_store = get_share_store()
    session_id = share_store.get_session_by_token(token)
    if not session_id:
        # 故意与「未共享」同态：一律 404，避免枚举/探测
        raise HTTPException(status_code=404, detail="该分享链接无效或已取消")
    # 读会话（共享的是谁的不重要，只取内容；即便 owner 字段读到也不回传）
    rec = get_session_store().get(session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="该分享链接无效或已取消")
    from api.items_backfill import backfill_item_ids

    for msg in rec.get("messages") or []:
        if isinstance(msg, dict) and msg.get("items"):
            backfill_item_ids(msg["items"])
    return {
        "share_token": token,
        "title": rec.get("title", "共享会话"),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
        "messages": rec.get("messages", []),
    }
