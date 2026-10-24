"""P1-4 e2e：监控端点（/metrics、/admin/*）访问控制。

修复前四个端点无任何鉴权依赖且默认开启——未认证者可读内部指标/告警/日志
链路摘要（错误率、峰值路径、熔断状态），为攻击者提供侦察信息。

修复后 router 级依赖 require_metrics_access：
- 配置 METRICS_ADMIN_TOKEN → 必须带匹配的 X-Admin-Token 头（HTML 看板支持
  ?token= 查询参数，恒定时间比较）；
- 未配置 → 退回用户鉴权（AUTH_ENABLED=true 需登录；调试态开放并有启动告警）。
"""
import uuid

import pytest

import api.main as m  # noqa: F401 - 确保 app 已装配
from config.settings import settings

_ADMIN_TOKEN = "test-metrics-token-0123456789"


def _alive():
    from sqlalchemy import create_engine, text

    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _alive(), reason="MySQL 不可用，跳过后端 e2e")


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setattr(settings, "METRICS_ADMIN_TOKEN", _ADMIN_TOKEN)
    return _ADMIN_TOKEN


# ---- 配置了 Admin Token：token 优先，与用户鉴权开关解耦 ----

def test_metrics_requires_admin_token(client, admin_token):
    r = client.get("/admin/stats")
    assert r.status_code == 401


def test_metrics_accepts_admin_token_header(client, admin_token):
    r = client.get("/admin/stats", headers={"X-Admin-Token": admin_token})
    assert r.status_code == 200


def test_metrics_rejects_wrong_admin_token(client, admin_token):
    r = client.get("/admin/stats", headers={"X-Admin-Token": "wrong-token"})
    assert r.status_code == 401


def test_dashboard_accepts_token_query_param(client, admin_token):
    """HTML 看板走浏览器直接打开，支持 ?token=（恒定时间比较同头路径）。"""
    r = client.get(f"/admin/dashboard?token={admin_token}")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_prometheus_endpoint_requires_admin_token(client, admin_token):
    assert client.get("/metrics").status_code == 401
    r = client.get("/metrics", headers={"X-Admin-Token": admin_token})
    assert r.status_code == 200


def test_alerts_endpoint_requires_admin_token(client, admin_token):
    assert client.get("/admin/alerts").status_code == 401
    r = client.get("/admin/alerts", headers={"X-Admin-Token": admin_token})
    assert r.status_code == 200


# ---- 未配置 token：退回用户鉴权（跟随 AUTH_ENABLED 总开关） ----

def test_metrics_open_in_debug_mode_without_token(client, monkeypatch):
    """未配 token + AUTH_ENABLED=false（调试态）：开放（启动有显式告警）。"""
    monkeypatch.setattr(settings, "METRICS_ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    assert client.get("/admin/stats").status_code == 200


def test_metrics_requires_login_when_auth_enabled(client, monkeypatch):
    """未配 token + AUTH_ENABLED=true：必须登录（生产形态）。"""
    monkeypatch.setattr(settings, "METRICS_ADMIN_TOKEN", "")
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_SECRET", "test-secret-0123456789abcdef")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    # 无 token → 401
    assert client.get("/admin/stats").status_code == 401
    # 注册拿 token → 200
    uname = f"e2e_metrics_{uuid.uuid4().hex[:10]}"
    r = client.post("/auth/register", json={"username": uname, "password": "secret123"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    r2 = client.get("/admin/stats", headers={"Authorization": f"Bearer {tok}"})
    assert r2.status_code == 200
    # 清理
    from sqlalchemy import create_engine, text

    e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
    with e.connect() as c:
        c.execute(text("DELETE FROM users WHERE username=:u"), {"u": uname})
        c.commit()
    e.dispose()
