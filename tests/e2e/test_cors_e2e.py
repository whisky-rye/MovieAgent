"""CORS e2e：预检响应头 + 生产环境未授权源拦截（cors_enforce 403）。

调试态下 cors_enforce 不拦截（仅中间件加头）；生产态下未授权 Origin 必须 403。
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


def test_preflight_returns_cors_headers(client):
    # CORS 中间件对白名单内源（前端 dev server）回声 access-control-allow-origin
    origin = "http://localhost:5173"
    r = client.get("/health", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


def test_production_rejects_unauthorized_origin(client, monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    # cors_enforce 内部按逗号分隔字符串解析白名单，必须是字符串
    monkeypatch.setattr(settings, "CORS_ORIGINS", "https://good.example")

    # 未授权源 → 403
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403

    # 授权源 → 200
    r = client.get("/health", headers={"Origin": "https://good.example"})
    assert r.status_code == 200
