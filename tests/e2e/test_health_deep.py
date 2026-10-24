"""P1-11 e2e：深度健康检查（/health）。

覆盖：
- 正常形态：200 + status ok/degraded + 六项检查（mysql/cache/vector/ratelimit/
  alert_loop/llm_exhausted）齐备且带 healthy 字段；
- down 形态：关键依赖探测抛错 → 503 + down 列表包含该项；
- 告警循环活性：lifespan 启动后 alert_loop.healthy=True。

依赖 MySQL（import 即探测），离线自动 skip。
"""
import pytest
from sqlalchemy import create_engine, text

import api.main as m
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


def test_health_deep_structure(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert body["service"] == "movie-agent-api"
    checks = body["checks"]
    for key in ("mysql", "cache", "vector", "ratelimit", "alert_loop", "llm_exhausted"):
        assert key in checks and "healthy" in checks[key], f"缺少检查项: {key}"
    # 兼容旧扁平字段
    assert "cache_backend" in body and "memory_backend" in body
    # 告警循环经 lifespan 启动后应为健康
    assert checks["alert_loop"]["healthy"] is True
    assert checks["mysql"]["healthy"] is True


def test_health_returns_503_when_critical_dep_down(client, monkeypatch):
    """关键依赖（MySQL）探测失败 → 503 + down 含 mysql。"""

    class _BoomStore:
        def backend(self):
            return "mysql"

        def health(self):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(m, "get_memory_store", lambda: _BoomStore())
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "down"
    assert "mysql" in body["down"]
