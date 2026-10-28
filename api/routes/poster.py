"""G-Poster 海报查询/下载路由。

- GET  /poster/{session_id}            ：返回该会话最新海报状态（pending/ready/error）+ 海报 url/download_url/模型等。
- GET  /poster/download/{session_id}   ：以文件流返回生成的海报（图片内联供 <img>，视频默认内联预览；
                                          ?download=1 时以附件形式触发浏览器下载）。
"""
from __future__ import annotations

import glob
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from agent.poster import get_poster_status
from api.deps import CurrentUser, get_current_user
from config.settings import settings

router = APIRouter(prefix="/poster", tags=["poster"])

# 依据扩展名推导 media_type（确保预览/下载时 Content-Type 正确，浏览器才不会把图片当二进制/异常文件）
_EXT_MEDIA = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "webm": "video/webm",
}


def _resolve_poster_path(session_id: str, info: dict) -> str | None:
    """定位海报本地文件：优先用 manifest.file；缺失或不在磁盘时按 {session_id}.* 回退扫描。

    目的：即便 manifest 记录与磁盘短暂脱节（如旧版时间戳命名残留），也能命中真实图片，
    绝不为 404 HTML 被浏览器当成图片保存（这正是"下载格式异常"现象的根因之一）。
    """
    data_dir = settings.POSTER_DATA_DIR
    file_rel = info.get("file")
    if file_rel:
        cand = os.path.join(data_dir, os.path.basename(file_rel))
        if os.path.isfile(cand):
            return cand
    # 回退：扫描 {session_id}.* 系列，取最新一份
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id))[:120]
    matches = sorted(
        glob.glob(os.path.join(data_dir, f"{safe}.*")),
        key=os.path.getmtime,
        reverse=True,
    )
    for m in matches:
        if os.path.isfile(m) and not m.endswith(".json"):
            return m
    return None


def _enforce_poster_access(info: dict, auth: CurrentUser) -> None:
    """鉴权 + 归属校验（P2-19）：未登录拒绝（401 由依赖注入负责）；

    生产态（AUTH_ENABLED=true）校验海报归属当前用户（manifest 内已落 user_id），
    非本人返回 403；调试态（AUTH_ENABLED=false）user_id 由客户端指定，不强制归属。
    """
    if not settings.AUTH_ENABLED:
        return
    owner = info.get("user_id")
    if owner is None or owner != auth.user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话的海报")


@router.get("/{session_id}")
def poster_status(
    session_id: str,
    auth: CurrentUser = Depends(get_current_user),
):
    """前端轮询：海报生成进度与可展示信息。"""
    info = get_poster_status(session_id)
    _enforce_poster_access(info, auth)
    return info


@router.get("/download/{session_id}")
def poster_download(
    session_id: str,
    request: Request,
    auth: CurrentUser = Depends(get_current_user),
):
    """返回生成的海报文件（png/mp4）。?download=1 强制附件下载。"""
    info = get_poster_status(session_id)
    _enforce_poster_access(info, auth)
    if info.get("status") != "ready":
        raise HTTPException(status_code=404, detail="海报尚未生成或不可用")
    path = _resolve_poster_path(session_id, info)
    if not path:
        raise HTTPException(status_code=404, detail="海报文件不存在")
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    media_type = _EXT_MEDIA.get(ext, info.get("media_type") or "application/octet-stream")
    as_attachment = request.query_params.get("download") == "1"
    return FileResponse(
        path,
        media_type=media_type,
        filename=os.path.basename(path),
        content_disposition_type="attachment" if as_attachment else "inline",
    )
