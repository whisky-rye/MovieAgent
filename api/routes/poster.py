"""G-Poster 海报查询/下载路由。

- GET  /poster/{session_id}            ：返回该会话最新海报状态（pending/ready/error）+ 海报 url/download_url/模型等。
- GET  /poster/download/{session_id}   ：以文件流返回生成的海报（图片内联供 <img>，视频默认内联预览；
                                          ?download=1 时以附件形式触发浏览器下载）。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from agent.poster import get_poster_status
from config.settings import settings

router = APIRouter(prefix="/poster", tags=["poster"])


@router.get("/{session_id}")
def poster_status(session_id: str):
    """前端轮询：海报生成进度与可展示信息。"""
    return get_poster_status(session_id)


@router.get("/download/{session_id}")
def poster_download(session_id: str, request: Request):
    """返回生成的海报文件（png/mp4）。?download=1 强制附件下载。"""
    info = get_poster_status(session_id)
    file_rel = info.get("file")
    if not file_rel or info.get("status") != "ready":
        raise HTTPException(status_code=404, detail="海报尚未生成或不可用")
    path = os.path.join(settings.POSTER_DATA_DIR, os.path.basename(file_rel))
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="海报文件不存在")
    media_type = info.get("media_type") or ("video/mp4" if path.endswith(".mp4") else "image/png")
    as_attachment = request.query_params.get("download") == "1"
    return FileResponse(
        path,
        media_type=media_type,
        filename=file_rel,
        content_disposition_type="attachment" if as_attachment else "inline",
    )
