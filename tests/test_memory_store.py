"""偏好记忆存储（G4）强 MySQL 行为验证：

1. backend() 恒为 "mysql"（无 "file" / "redis" / "mysql->file"）。
2. add_pref / get_prefs / push_history / load 全链路读写正确（单一 JSON 列 data）。
3. MySQL 不可达（构造时探测失败）直接抛 RuntimeError（fail-fast，拒绝启动），不静默回退 file/Redis。
依赖真实 MySQL（movie_agent.user_preferences）；用唯一 user_id 避免串扰。
"""
import uuid

import pytest

import agent.memory as mem_mod
import config.settings as settings_mod


@pytest.fixture
def store(mysql_ok):
    return mem_mod.get_memory_store()


def test_backend_is_mysql(store):
    assert store.backend() == "mysql"


def test_add_and_get_prefs(store):
    uid = f"mem_{uuid.uuid4().hex[:12]}"
    prefs = store.add_pref(uid, genres=["科幻", "悬疑"], note="喜欢烧脑")
    assert prefs["genres"] == ["科幻", "悬疑"]
    assert prefs["note"] == "喜欢烧脑"
    got = store.get_prefs(uid)
    assert got["genres"] == ["科幻", "悬疑"]
    assert got["note"] == "喜欢烧脑"


def test_push_and_load_history(store):
    uid = f"mem_{uuid.uuid4().hex[:12]}"
    store.push_history(uid, {"query": "推荐科幻", "ts": 1})
    data = store.load(uid)
    assert any(h.get("query") == "推荐科幻" for h in data.get("history", []))
    # 再次 load 应能看到已持久化的偏好与历史
    assert store.get_prefs(uid).get("history") or data.get("history")


def test_fail_fast_on_unreachable_mysql(monkeypatch):
    """构造时 MySQL 不可达 -> RuntimeError（强 MySQL，无 file 兜底）。"""
    monkeypatch.setattr(
        settings_mod.Settings,
        "MOVIE_AGENT_MYSQL_DSN",
        property(lambda self: "mysql+pymysql://nouser:nopass@127.0.0.1:3399/none"),
    )
    with pytest.raises(RuntimeError):
        mem_mod.MySQLMemoryStore()
