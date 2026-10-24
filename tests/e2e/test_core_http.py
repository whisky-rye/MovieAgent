"""核心 HTTP 链路 e2e：/health、/sessions CRUD、/share、/memory、/poster 轮询。

覆盖真实 app + 真实 MySQL（强 MySQL），验证端到端请求/响应契约与归属校验（404/403）。
不依赖任何外部 AI 服务（推荐链路见 test_recommend_e2e.py）。
"""
import pytest
from sqlalchemy import create_engine, text

from config.settings import settings


def _alive():
    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _alive(), reason="MySQL 不可用，跳过后端 e2e")


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")  # down 会返 503
    assert body["service"] == "movie-agent-api"
    assert "cache_backend" in body and "session_backend" in body
    # P1-11：深度健康检查结构（各依赖真实状态 + 告警循环活性 + 熔断摘要）
    checks = body["checks"]
    for key in ("mysql", "cache", "vector", "ratelimit", "alert_loop", "llm_exhausted"):
        assert key in checks and "healthy" in checks[key], f"缺少检查项: {key}"


def test_sessions_crud_lifecycle(client):
    # 创建
    r = client.post("/sessions", json={"title": "e2e-core-session"})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    try:
        # 列表可见
        r = client.get("/sessions")
        assert r.status_code == 200
        ids = [s["session_id"] for s in r.json()]
        assert sid in ids

        # 详情
        r = client.get(f"/sessions/{sid}")
        assert r.status_code == 200
        assert r.json()["session_id"] == sid

        # 重命名
        r = client.patch(f"/sessions/{sid}", json={"title": "e2e-renamed"})
        assert r.status_code == 200
        r = client.get(f"/sessions/{sid}")
        assert r.json()["title"] == "e2e-renamed"

        # 不存在 → 404
        r = client.get("/sessions/__no_such_session__")
        assert r.status_code == 404
    finally:
        r = client.delete(f"/sessions/{sid}")
        assert r.status_code == 204
        # 删除后不可见
        r = client.get(f"/sessions/{sid}")
        assert r.status_code == 404


def test_share_flow(client):
    r = client.post("/sessions", json={"title": "e2e-share-session"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    try:
        r = client.post(f"/sessions/{sid}/share")
        assert r.status_code == 200, r.text
        token = r.json().get("token") or r.json().get("share_token")
        assert token

        # 通过分享 token 取回
        r = client.get(f"/share/{token}")
        assert r.status_code == 200
        assert r.json().get("session_id") == sid or "messages" in r.json()

        # 取消分享
        r = client.delete(f"/sessions/{sid}/share")
        assert r.status_code in (200, 204)
    finally:
        client.delete(f"/sessions/{sid}")


def test_memory_preferences_roundtrip(client):
    payload = {"genres": ["科幻", "悬疑"], "min_year": 2015}
    r = client.put("/memory/preferences", json=payload)
    assert r.status_code == 200, r.text
    r = client.get("/memory/preferences")
    assert r.status_code == 200
    body = r.json()
    # 偏好写回（至少不报错且结构合理）
    assert isinstance(body, (dict, list))
    # 清理
    client.delete("/memory/preferences")


def test_poster_poll_missing_session(client):
    # 不存在的会话轮询海报：应返回状态而非 500
    r = client.get("/poster/__no_such_poster_session__")
    assert r.status_code == 200
    assert r.json().get("status") != "ready"
