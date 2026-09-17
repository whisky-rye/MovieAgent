"""O7/E5：CORS 生产白名单中间件测试。

- APP_ENV=production：带 Origin 且不在 CORS_ORIGINS -> 403；在白名单内 -> 200。
- 非 production：不强制，任何 Origin -> 200（开发期宽松）。
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    api_main = importlib.import_module("api.main")
    # 关闭启动期安全校验（测试仅验证 CORS 拦截，与生产鉴权解耦）
    monkeypatch.setattr(api_main, "validate_security_config", lambda: None)
    monkeypatch.setattr(api_main.settings, "APP_ENV", "production")
    monkeypatch.setattr(api_main.settings, "CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setattr(api_main.settings, "CORS_ALLOW_METHODS", "GET,POST,OPTIONS")
    monkeypatch.setattr(
        api_main.settings, "CORS_ALLOW_HEADERS",
        "Content-Type,Authorization,X-CSRF-Token,X-Requested-With",
    )
    return TestClient(api_main.app)


def test_production_blocks_disallowed_origin(client):
    r = client.get("/health", headers={"Origin": "https://evil.com"})
    assert r.status_code == 403


def test_production_allows_whitelisted_origin(client):
    r = client.get("/health", headers={"Origin": "https://app.example.com"})
    assert r.status_code == 200


def test_production_no_origin_header_passes(client):
    r = client.get("/health")
    assert r.status_code == 200


def test_non_production_not_enforced(monkeypatch):
    api_main = importlib.import_module("api.main")
    monkeypatch.setattr(api_main, "validate_security_config", lambda: None)
    monkeypatch.setattr(api_main.settings, "APP_ENV", "local")
    monkeypatch.setattr(api_main.settings, "CORS_ORIGINS", "https://app.example.com")
    c = TestClient(api_main.app)
    r = c.get("/health", headers={"Origin": "https://evil.com"})
    # 非生产：不强制白名单，请求正常通过（由 CORSMiddleware 回声处理跨域）
    assert r.status_code == 200
