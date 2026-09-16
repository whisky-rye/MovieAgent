"""会话历史导出接口（GET /export/sessions）行为验证：

1. 返回 application/zip 附件，zip 合法且内含 conversations.json；
2. JSON 结构完整（exported_at/user_id/session_count/sessions），只含本人会话（越权隔离）；
3. Content-Disposition 带 attachment 文件名，X-Export-Sessions 统计正确。
用假 store 注入，避免依赖真实 MySQL/file 环境。
"""
import io
import json
import zipfile

from fastapi.testclient import TestClient

import api.main as m
import api.routes.export as ex


class _FakeStore:
    def __init__(self, sessions):
        self._sessions = sessions  # session_id -> rec

    def list_by_user(self, user_id):
        return [
            {"session_id": sid, "title": r["title"], "created_at": r["created_at"],
             "updated_at": r["updated_at"], "preview": ""}
            for sid, r in self._sessions.items() if r["user_id"] == user_id
        ]

    def get(self, sid):
        return self._sessions.get(sid)


def _client_with(monkeypatch, store):
    monkeypatch.setattr(ex, "get_session_store", lambda: store)
    return TestClient(m.app)


def test_export_sessions_zip(monkeypatch):
    store = _FakeStore({
        "s1": {"session_id": "s1", "user_id": "u1", "title": "科幻片求助",
               "created_at": "2026-09-16T00:00:00+00:00",
               "updated_at": "2026-09-16T01:00:00+00:00",
               "messages": [{"role": "user", "content": "推荐科幻片"},
                            {"role": "assistant", "content": "好的", "items": []}]},
        "s2": {"session_id": "s2", "user_id": "other", "title": "别人的会话",
               "created_at": "2026-09-16T00:00:00+00:00",
               "updated_at": "2026-09-16T02:00:00+00:00", "messages": []},
    })
    monkeypatch.setattr(m, "AUTH_FALLBACK_USER", "u1", raising=False) if hasattr(
        m, "AUTH_FALLBACK_USER") else None
    client = _client_with(monkeypatch, store)

    # AUTH_ENABLED=false 时依赖回退 default 用户；把假数据 user_id 对齐实际回退用户
    from config.settings import settings
    fallback_uid = "default" if not settings.AUTH_ENABLED else client.get(
        "/auth/me").json().get("user_id")
    store._sessions["s1"]["user_id"] = fallback_uid
    store._sessions["s2"]["user_id"] = "someone-else"

    resp = client.get("/export/sessions")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert resp.headers.get("x-export-sessions") == "1"  # 只导出本人 1 条

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.namelist() == ["conversations.json"]
    payload = json.loads(zf.read("conversations.json").decode("utf-8"))
    assert payload["user_id"] == fallback_uid
    assert payload["session_count"] == 1
    assert len(payload["sessions"]) == 1
    s = payload["sessions"][0]
    assert s["session_id"] == "s1"
    assert s["title"] == "科幻片求助"
    assert s["message_count"] == 2
    assert s["messages"][0]["content"] == "推荐科幻片"


def test_export_empty_user(monkeypatch):
    client = _client_with(monkeypatch, _FakeStore({}))
    resp = client.get("/export/sessions")
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    payload = json.loads(zf.read("conversations.json").decode("utf-8"))
    assert payload["session_count"] == 0
    assert payload["sessions"] == []
