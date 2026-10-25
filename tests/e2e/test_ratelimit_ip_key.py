"""P2-16 e2e：调试模式下轮换 user_id 绕不过令牌桶（按 IP 限流）。

修复前：AUTH_ENABLED=false 时 _run 按 body 里的 user_id 限流——每个新 user_id
都是一个新令牌桶，攻击者轮换 user_id 即完全绕过限流。修复后调试模式按客户端 IP
限流：同一 IP 下换 user_id 仍打同一个桶。

注意：精确缓存在限流之前——同 (query,user_id) 的重复请求会命中缓存不消耗令牌，
故本模块用 no-op 缓存隔离，确保每个请求都走到限流判定。
"""
import pytest

import api.ratelimit as rl_mod
from config.settings import settings


class _NoCache:
    """禁缓存：get 恒未命中、set 丢弃（隔离限流判定）。"""

    def backend(self):
        return "noop"

    def get(self, *a, **k):
        return None

    def set(self, *a, **k):
        return None


@pytest.fixture
def tight_limiter(monkeypatch):
    """极小容量内存限流器 + no-op 缓存 + 重置单例（用例后恢复）。"""
    monkeypatch.setattr(settings, "RATELIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATELIMIT_BACKEND", "memory")
    monkeypatch.setattr(settings, "RATELIMIT_RATE", 0.0)   # 不补充令牌
    monkeypatch.setattr(settings, "RATELIMIT_BURST", 1)    # 桶容量 1

    import api.main as m
    monkeypatch.setattr(m, "get_cache", lambda: _NoCache())

    old_limiter, old_cache = rl_mod._limiter, m.get_cache
    rl_mod._limiter = None
    yield
    rl_mod._limiter = old_limiter


def test_rotating_user_id_cannot_bypass_limit(client, tight_limiter):
    """同 IP 轮换 user_id：第一个 200，第二个 429（修复前两个都 200）。"""
    r1 = client.post("/recommend", json={"query": "推荐点科幻片", "user_id": "u1"})
    assert r1.status_code == 200, r1.text
    r2 = client.post("/recommend", json={"query": "推荐点科幻片", "user_id": "u2"})
    assert r2.status_code == 429, "轮换 user_id 不应拿到新令牌桶（应按 IP 限流）"
    assert "Retry-After" in r2.headers


def test_same_user_still_limited(client, tight_limiter):
    """同一 user_id 连续请求同样被限（回归：限流本身仍生效）。"""
    r1 = client.post("/recommend", json={"query": "推荐点科幻片", "user_id": "u1"})
    assert r1.status_code == 200
    r2 = client.post("/recommend", json={"query": "推荐点科幻片", "user_id": "u1"})
    assert r2.status_code == 429


def test_chat_shares_ip_bucket_with_recommend(client, tight_limiter):
    """/chat 与 /recommend 共用同一 IP 桶（同一真实 LLM 入口）。"""
    r1 = client.post("/recommend", json={"query": "推荐点科幻片", "user_id": "u1"})
    assert r1.status_code == 200
    r2 = client.post("/chat", json={"query": "推荐部科幻片", "user_id": "u2"})
    assert r2.status_code == 200  # /chat 是 SSE 文本响应，限流以提示文本呈现
    assert "频繁" in r2.text, f"应收到限流提示，实际: {r2.text!r}"
