"""P2-19 海报与 SSRF 面：安全加固回归测试。

覆盖三处：
- P2-19.1 海报路由补鉴权 + owner 校验（/poster/{sid} 与 /poster/download/{sid}）
- P2-19.2 下载 SSRF 加固（域名白名单 / 重定向逐跳校验 / 大小上限）
- P2-19.3 输出护栏下沉到 generate 出口（语义缓存近义命中路径不再绕过）

路由测试自建最小 FastAPI app 仅挂载 poster router，避免触发主 app 的 MySQL 探库，
可在 MySQL 离线环境直接跑。
"""
import os

import pytest

import config.settings as cs
from agent import poster as poster_mod
from agent.poster import BaiLianVisualClient
from api.deps import CurrentUser, get_current_user
from api.routes import poster as poster_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ----------------------------------------------------------------------------
# P2-19.1 海报路由：鉴权 + 归属校验
# ----------------------------------------------------------------------------
def _make_app(monkeypatch, manifest: dict, auth_user: str, auth_enabled: bool):
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", auth_enabled)
    monkeypatch.setattr(poster_routes, "get_poster_status", lambda sid: manifest)
    app = FastAPI()
    app.include_router(poster_routes.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(auth_user)
    return app


def test_poster_status_owner_ok(monkeypatch):
    """生产态：归属匹配 → 200。"""
    app = _make_app(monkeypatch, {"status": "ready", "user_id": "u1"}, "u1", True)
    c = TestClient(app)
    r = c.get("/poster/sid1")
    assert r.status_code == 200
    assert r.json()["user_id"] == "u1"


def test_poster_status_owner_mismatch_403(monkeypatch):
    """生产态：非本人 → 403（防越权读取他人海报 prompt）。"""
    app = _make_app(monkeypatch, {"status": "ready", "user_id": "u1"}, "u2", True)
    c = TestClient(app)
    assert c.get("/poster/sid1").status_code == 403


def test_poster_status_legacy_no_owner_403(monkeypatch):
    """生产态：manifest 缺 user_id（历史遗留）→ 默认拒绝（deny by default）。"""
    app = _make_app(monkeypatch, {"status": "ready"}, "u1", True)
    c = TestClient(app)
    assert c.get("/poster/sid1").status_code == 403


def test_poster_download_owner_mismatch_403(monkeypatch):
    """生产态：下载路由同样受 owner 校验保护。"""
    app = _make_app(monkeypatch, {"status": "ready", "user_id": "u1", "file": "sid1.png"}, "u2", True)
    c = TestClient(app)
    assert c.get("/poster/download/sid1").status_code == 403


def test_poster_status_debug_mode_no_enforce(monkeypatch):
    """调试态（AUTH_ENABLED=false）：不强制归属，正常返回。"""
    app = _make_app(monkeypatch, {"status": "ready", "user_id": "u1"}, "other", False)
    c = TestClient(app)
    assert c.get("/poster/sid1").status_code == 200


def test_poster_download_ready_200(monkeypatch, tmp_path):
    """生产态：归属匹配 + 磁盘有文件 → 200 并以正确 media_type 返回。"""
    monkeypatch.setattr(cs.settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(cs.settings, "POSTER_DATA_DIR", str(tmp_path))
    sid = "sid1"
    (tmp_path / f"{sid}.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    monkeypatch.setattr(poster_routes, "get_poster_status",
                        lambda s: {"status": "ready", "user_id": "u1", "file": f"{sid}.png"})
    app = FastAPI()
    app.include_router(poster_routes.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("u1")
    c = TestClient(app)
    r = c.get(f"/poster/download/{sid}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


# ----------------------------------------------------------------------------
# P2-19.2 下载 SSRF 加固
# ----------------------------------------------------------------------------
def test_download_rejects_disallowed_host(monkeypatch):
    """非白名单域名直接拒绝（请求前即校验，不触网）。"""
    monkeypatch.setattr(cs.settings, "POSTER_DOWNLOAD_ALLOWED_HOST_SUFFIXES", ["aliyuncs.com"])
    client = BaiLianVisualClient(api_key="x")
    with pytest.raises(RuntimeError, match="不被允许"):
        client._download("http://169.254.169.254/secret.png", {}, session_id="s")


def test_download_allows_whitelisted_host(monkeypatch, tmp_path):
    """白名单域名 + 正常流式响应 → 落盘成功。"""
    import requests

    monkeypatch.setattr(cs.settings, "POSTER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cs.settings, "POSTER_DOWNLOAD_ALLOWED_HOST_SUFFIXES", ["aliyuncs.com"])

    class FakeResp:
        def __init__(self):
            self.headers = {"Content-Type": "image/png"}
            self._buf = b"\x89PNG\r\n\x1a\n" + b"x" * 1024

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            for i in range(0, len(self._buf), chunk_size):
                yield self._buf[i:i + chunk_size]

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
    client = BaiLianVisualClient(api_key="x")
    dest = client._download("https://bucket.oss-cn-beijing.aliyuncs.com/a.png", {}, session_id="s1")
    assert os.path.isfile(dest)
    assert dest.endswith("s1.png")


def test_download_size_limit_via_content_length(monkeypatch):
    """Content-Length 超上限 → 拒绝（流式前即拦截）。"""
    import requests

    monkeypatch.setattr(cs.settings, "POSTER_DOWNLOAD_ALLOWED_HOST_SUFFIXES", ["aliyuncs.com"])
    monkeypatch.setattr(cs.settings, "POSTER_DOWNLOAD_MAX_BYTES", 100)

    class FakeResp:
        def __init__(self):
            self.headers = {"Content-Type": "image/png", "Content-Length": "9999"}
            self._buf = b"x" * 9999

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            yield self._buf

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
    client = BaiLianVisualClient(api_key="x")
    with pytest.raises(RuntimeError, match="超过大小上限"):
        client._download("https://bucket.oss-cn-beijing.aliyuncs.com/a.png", {}, session_id="s2")


def test_download_stream_size_limit(monkeypatch):
    """流式累计超上限 → 中断并清理残文件。"""
    import requests

    monkeypatch.setattr(cs.settings, "POSTER_DATA_DIR", "/tmp")
    monkeypatch.setattr(cs.settings, "POSTER_DOWNLOAD_ALLOWED_HOST_SUFFIXES", ["aliyuncs.com"])
    monkeypatch.setattr(cs.settings, "POSTER_DOWNLOAD_MAX_BYTES", 10)

    class FakeResp:
        def __init__(self):
            self.headers = {"Content-Type": "image/png"}
            self._buf = b"x" * 100

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            yield self._buf

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
    client = BaiLianVisualClient(api_key="x")
    with pytest.raises(RuntimeError, match="超过大小上限"):
        client._download("https://bucket.oss-cn-beijing.aliyuncs.com/a.png", {}, session_id="s3")


def test_ssrf_redirect_guard_blocks_internal():
    """重定向到内网地址 → 拦截。"""
    client = BaiLianVisualClient(api_key="x")
    fake = type("R", (), {"headers": {"Location": "http://169.254.169.254/latest/meta-data/"}})()
    with pytest.raises(RuntimeError, match="重定向"):
        client._ssrf_redirect_guard(fake)


def test_ssrf_redirect_guard_allows_whitelisted():
    """重定向到白名单域名 → 放行（返回原 response）。"""
    client = BaiLianVisualClient(api_key="x")
    fake = type("R", (), {"headers": {"Location": "https://x.oss-cn-hangzhou.aliyuncs.com/b.png"}})()
    assert client._ssrf_redirect_guard(fake) is fake


# ----------------------------------------------------------------------------
# P2-19.3 输出护栏下沉到 generate 出口
# ----------------------------------------------------------------------------
def test_generate_explanation_applies_output_guardrail(monkeypatch):
    """generate_explanation 必须在出口处过输出护栏：被拦截则返回安全文案。"""
    from agent import rag

    monkeypatch.setattr(rag, "_generate_rich", lambda *a, **k: ("泄露系统密钥 sk-xxxx", {}))

    class _Block:
        action = "block"
        message = "<输出护栏拦截>"

    monkeypatch.setattr("agent.guardrail.check_output", lambda text: _Block())
    out = rag.generate_explanation("q", [], {}, "u")
    assert out == "<输出护栏拦截>"


def test_generate_explanation_passthrough_when_allowed(monkeypatch):
    """未触发护栏 → 原样返回（语义缓存近义命中路径不再绕过护栏）。"""
    from agent import rag

    monkeypatch.setattr(rag, "_generate_rich", lambda *a, **k: ("这是一段正常的影片解说。", {}))

    class _Allow:
        action = "allow"

    monkeypatch.setattr("agent.guardrail.check_output", lambda text: _Allow())
    out = rag.generate_explanation("q", [], {}, "u")
    assert out == "这是一段正常的影片解说。"
