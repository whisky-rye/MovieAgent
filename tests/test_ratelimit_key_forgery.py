"""P2-16 回归：调试模式限流键按 IP（不可伪造）+ 内存令牌桶 LRU 淘汰。

修复前：
- AUTH_ENABLED=false 时 user_id 取自 query/body（deps 兼容兜底），_run 与
  stream 按 user_id 限流——轮换 user_id 即为每个请求开一个新令牌桶，限流
  完全被绕过（与 P0-1 叠加时等于无限流）；
- 内存令牌桶 _memory 无淘汰——按 IP/轮换 key 限流后恶意流量造出大量一次性
  key，字典无界增长（慢速内存泄漏）。

修复：限流键按模式生成（鉴权=user_id，token 派生不可伪造；调试=客户端 IP，
TCP 层属性无法在请求内伪造）；内存桶 OrderedDict + LRU 逐出（_MEMORY_MAX_KEYS）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import api.ratelimit as rl_mod
from config.settings import settings

# api.main 导入即探 MySQL（业务库），离线时跳过 _run 级验证
try:
    import api.main as m
    _API_OK = True
except Exception:  # noqa: BLE001
    _API_OK = False


def _req(client_ip="1.2.3.4"):
    from starlette.requests import Request
    scope = {"type": "http", "method": "POST", "path": "/recommend", "headers": []}
    if client_ip is not None:
        scope["client"] = (client_ip, 12345)
    return Request(scope)


# ---------- 限流键 ----------

def test_rl_key_auth_mode_uses_user_id(monkeypatch):
    """鉴权模式：user_id 来自 token 派生，按它限流（不可伪造）。"""
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    k = m._rate_limit_key(_req("9.9.9.9"), "alice")
    assert k == "user:alice"
    # 同一用户不同 IP 仍是同一桶（防同一账号多 IP 分散绕过）
    assert m._rate_limit_key(_req("8.8.8.8"), "alice") == "user:alice"


def test_rl_key_debug_mode_uses_ip(monkeypatch):
    """调试模式：按 IP 限流——body/query 里的 user_id 可任意轮换，不可作为键。"""
    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    assert m._rate_limit_key(_req("1.2.3.4"), "alice") == "ip:1.2.3.4"
    # 轮换 user_id 不换桶（修复前每个 user_id 一个新桶 = 无限流）
    assert m._rate_limit_key(_req("1.2.3.4"), "bob") == "ip:1.2.3.4"
    assert m._rate_limit_key(_req("1.2.3.4"), " Mallory ") == "ip:1.2.3.4"


def test_rl_key_debug_mode_no_client():
    """无 client 信息（极端场景）→ ip:unknown，仍不与 user_id 相关。"""
    old = settings.AUTH_ENABLED
    settings.AUTH_ENABLED = False
    try:
        assert m._rate_limit_key(_req(None), "alice") == "ip:unknown"
    finally:
        settings.AUTH_ENABLED = old


# ---------- 内存桶 LRU ----------

def _limiter(monkeypatch=None):
    """直连内存分支的 limiter。

    显式置 RATELIMIT_ENABLED=True：consume() 首行检查该开关，历史上 e2e conftest
    的普通赋值会把它泄漏成 False，使直连用例被「限流已关」短路（现已修 conftest，
    这里仍显式声明，自包含不依赖外部状态）。
    """
    settings.RATELIMIT_ENABLED = True
    old_backend = settings.RATELIMIT_BACKEND
    settings.RATELIMIT_BACKEND = "memory"  # 直走内存分支（Redis 不可用也不影响）
    try:
        return rl_mod.RateLimiter()
    finally:
        settings.RATELIMIT_BACKEND = old_backend


def test_memory_lru_evicts_least_recently_used(monkeypatch):
    monkeypatch.setattr(rl_mod, "_MEMORY_MAX_KEYS", 3)
    lim = _limiter(monkeypatch)
    for k in ("a", "b", "c"):
        lim.consume(k)
    assert list(lim._memory.keys()) == ["a", "b", "c"]
    lim.consume("a")          # 命中 a → 刷新时序（a 变为最新）
    lim.consume("d")          # 触发淘汰：逐出最久未用的 b
    assert list(lim._memory.keys()) == ["c", "a", "d"]
    assert len(lim._memory) == 3, "容量必须守住"


def test_memory_evicted_key_recreated(monkeypatch):
    """被逐出的 key 再次请求自然重建（语义无损，只是重新计流）。"""
    monkeypatch.setattr(rl_mod, "_MEMORY_MAX_KEYS", 2)
    lim = _limiter(monkeypatch)
    lim.consume("a")
    lim.consume("b")
    lim.consume("c")          # 逐出 a
    assert "a" not in lim._memory
    lim.consume("a")          # 重建（并逐出最久未用的 b）
    assert "a" in lim._memory and "b" not in lim._memory


def test_memory_capacity_default_is_bounded():
    """默认容量为有限值（防无界增长的常量必须存在且为正）。"""
    assert rl_mod._MEMORY_MAX_KEYS > 0


def test_memory_bucket_still_rate_limits(monkeypatch):
    """淘汰逻辑不破坏令牌桶本身：burst 耗尽后同 key 仍被限。"""
    monkeypatch.setattr(rl_mod, "_MEMORY_MAX_KEYS", 10)
    monkeypatch.setattr(settings, "RATELIMIT_RATE", 0.0)   # 不补充
    monkeypatch.setattr(settings, "RATELIMIT_BURST", 1)
    lim = _limiter(monkeypatch)
    assert lim.consume("k")[0] is True
    assert lim.consume("k")[0] is False   # burst=1 → 第二次被限


# ---------- _run 使用生成的键（需 MySQL 在线） ----------

@pytest.mark.skipif(not _API_OK, reason="api.main 需要 MySQL 在线（import 即探测）")
def test_run_consumes_rl_key(monkeypatch):
    """_run 用调用方生成的 rl_key 消耗令牌（调试模式 IP 键穿透到限流器）。"""
    seen = {}

    class _FakeLimiter:
        def consume(self, key, tokens=1.0):
            seen["key"] = key
            return True, 0.0

    class _FakeAgent:
        def invoke(self, state, config=None):
            return {"ranked": [], "answer": "ok", "intent": "recommend",
                    "sources": {}, "steps": [], "thinking": {}}

    monkeypatch.setattr(m, "get_limiter", lambda: _FakeLimiter())
    monkeypatch.setattr(m, "_AGENT", _FakeAgent())
    monkeypatch.setattr(settings, "RATELIMIT_ENABLED", True)
    m._run("推荐科幻片", "alice", request_id="p2_16_t1", rl_key="ip:1.2.3.4")
    assert seen["key"] == "ip:1.2.3.4", "调试模式的 IP 键应穿透到限流器"


@pytest.mark.skipif(not _API_OK, reason="api.main 需要 MySQL 在线（import 即探测）")
def test_run_rl_key_defaults_to_user(monkeypatch):
    """省略 rl_key（内部调用/旧测试）→ 退回 user:{user_id}，行为兼容。"""
    seen = {}

    class _FakeLimiter:
        def consume(self, key, tokens=1.0):
            seen["key"] = key
            return True, 0.0

    class _FakeAgent:
        def invoke(self, state, config=None):
            return {"ranked": [], "answer": "ok", "intent": "recommend",
                    "sources": {}, "steps": [], "thinking": {}}

    monkeypatch.setattr(m, "get_limiter", lambda: _FakeLimiter())
    monkeypatch.setattr(m, "_AGENT", _FakeAgent())
    monkeypatch.setattr(settings, "RATELIMIT_ENABLED", True)
    m._run("推荐科幻片", "alice", request_id="p2_16_t2")
    assert seen["key"] == "user:alice"
