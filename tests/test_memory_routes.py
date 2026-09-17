"""G4 偏好记忆路由（api/routes/memory.py）集成测试。

走真实 FastAPI app + 真实 MySQL（movie_agent.user_preferences，强 MySQL）。
覆盖：PUT 合并更新偏好（genres 去重、note 覆盖、remove_genres 剔除）/ GET / DELETE；
GET /history 初值为空列表。单一数据源：与 remember_preference 工具共用同一张表。
每个用例用唯一 user_id 并在结束后清理。
"""
import os
import sys
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as cs
import api.main as m
import pytest

client = TestClient(m.app)


@pytest.fixture(scope="module", autouse=True)
def require_mysql():
    try:
        e = create_engine(cs.settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MySQL 不可用，跳过记忆路由测试：{exc}")


def _cleanup(uid):
    try:
        e = create_engine(cs.settings.MOVIE_AGENT_MYSQL_DSN)
        with e.connect() as c:
            c.execute(text("DELETE FROM user_preferences WHERE user_id=:u"), {"u": uid})
            c.commit()
    except Exception:
        pass


def test_preferences_crud():
    uid = f"mem_t_{uuid.uuid4().hex[:10]}"
    base = f"/memory/preferences?user_id={uid}"
    try:
        # 添加两类偏好
        r = client.put(base, json={"genres": ["科幻", "悬疑"]})
        assert r.status_code == 200, r.text
        prefs = r.json()["preferences"]
        assert "科幻" in prefs["genres"] and "悬疑" in prefs["genres"]

        # GET 读回
        r2 = client.get(base)
        assert r2.status_code == 200
        assert "科幻" in r2.json()["preferences"]["genres"]

        # 合并更新：同偏好去重 + 追加 note
        r3 = client.put(base, json={"genres": ["科幻"], "note": "喜欢烧脑"})
        assert "烧脑" in r3.json()["preferences"]["note"]
        assert r3.json()["preferences"]["genres"].count("科幻") == 1  # 去重

        # DELETE 清空偏好（保留 history）
        r4 = client.delete(base)
        assert r4.status_code == 200 and r4.json()["preferences"] == {}
    finally:
        _cleanup(uid)


def test_preferences_remove_genres():
    uid = f"mem_t_{uuid.uuid4().hex[:10]}"
    base = f"/memory/preferences?user_id={uid}"
    try:
        client.put(base, json={"genres": ["科幻", "悬疑", "喜剧"]})
        r = client.put(base, json={"remove_genres": ["悬疑"]})
        assert "悬疑" not in r.json()["preferences"]["genres"]
        assert "科幻" in r.json()["preferences"]["genres"]
    finally:
        _cleanup(uid)


def test_history_empty():
    uid = f"mem_t_{uuid.uuid4().hex[:10]}"
    try:
        r = client.get(f"/memory/history?user_id={uid}")
        assert r.status_code == 200
        assert r.json()["history"] == []
    finally:
        _cleanup(uid)
