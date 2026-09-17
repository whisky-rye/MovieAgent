"""数据导出接口（仿 DeepSeek「导出所有历史对话」）：

- GET /export/sessions -> application/zip 附件下载
  把当前用户的全部会话整理为一份结构化 JSON（conversations.json），
  在内存中压缩为 zip 包后以附件流返回，前端拿到 blob 触发浏览器下载。

设计要点：
- user_id 一律取自鉴权依赖（get_current_user），与 /sessions 同源，天然只导出本人数据。
- 全程内存（io.BytesIO + zipfile），不落临时文件、无清理负担；zip 内固定单文件 conversations.json，
  UTF-8 ensure_ascii=False，中文原文可读。
- GET 请求：CSRF 中间件天然豁免；鉴权走 httpOnly Cookie（浏览器）或 Bearer（API 客户端）均可。
- 会话很多时也是一次 list + N 次 get 的 O(N) 读；store 为强 MySQL（无 file 兜底）。
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from agent.session_store import get_session_store
from api.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/export", tags=["export"])

# zip 内的 JSON 文件名（固定，便于解析）
JSON_NAME = "conversations.json"


@router.get("/sessions")
def export_sessions(auth: CurrentUser = Depends(get_current_user)):
    """导出当前用户全部会话为 zip（内含 conversations.json）。"""
    store = get_session_store()
    metas = store.list_by_user(auth.user_id)  # 已按 updated_at 倒序

    sessions = []
    for meta in metas:
        rec = store.get(meta["session_id"])
        # 归属防御：list 与 get 之间理论上无竞态，但导出属敏感数据，再校验一次
        if rec is None or rec.get("user_id") != auth.user_id:
            continue
        sessions.append({
            "session_id": rec.get("session_id"),
            "title": rec.get("title", "新会话"),
            "created_at": rec.get("created_at"),
            "updated_at": rec.get("updated_at"),
            "message_count": len(rec.get("messages") or []),
            "messages": rec.get("messages") or [],
        })

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user_id": auth.user_id,
        "session_count": len(sessions),
        "sessions": sessions,
    }
    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(JSON_NAME, json_bytes)
    zip_bytes = buf.getvalue()

    # 文件名：ASCII 回退名 + RFC 5987 UTF-8 名（双写兼容各浏览器）
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ascii_name = f"chat_export_{stamp}.zip"
    utf8_name = quote(f"对话导出_{stamp}.zip")
    disposition = (
        f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"
    )

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": disposition,
            "X-Export-Sessions": str(len(sessions)),
        },
    )
