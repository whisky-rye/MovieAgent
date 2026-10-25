"""P2-11 e2e：/recommend 的 top_k 端到端真实生效（>20 不再被静默截断）。

修复前 rerank 硬编码 top_k=20：POST /recommend {"top_k": 30} 实际最多返 20 条。
修复后 top_k 下传 agent state，_execute 按它收口——30 条候选的 fake agent
应原样返回 30 条。
"""
import uuid

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


class _ManyItemsAgent:
    """返回 N 条候选的假 agent（N 由 state.top_k 决定，验证下传是否生效）。"""

    def invoke(self, state, config=None):
        n = int((state or {}).get("top_k") or 20)
        self.seen_top_k = n
        ranked = [
            {
                "id": f"douban:{i}", "source": "douban", "title": f"片{i}",
                "year": 2000, "genres": ["剧情"], "country": "中国",
                "rating": 8.0, "rating_count": 100, "recall_source": "fusion",
                "score": round(1.0 - i * 0.001, 4), "douban_id": str(i), "imdb_id": "",
            }
            for i in range(n)
        ]
        return {"ranked": ranked, "answer": "ok", "intent": "recommend",
                "sources": {}, "steps": [], "thinking": {}}


@pytest.fixture
def many_agent():
    ag = _ManyItemsAgent()
    old = m._AGENT
    m._AGENT = ag
    yield ag
    m._AGENT = old


def test_recommend_top_k_30_returns_30(client, many_agent):
    """top_k=30 → 30 条（修复前最多 20）。"""
    r = client.post("/recommend", json={"query": "推荐点科幻片", "top_k": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 30
    assert many_agent.seen_top_k == 30, "top_k 未下传进 agent state"


def test_recommend_top_k_20_boundary(client, many_agent):
    """边界 20 行为不变。"""
    r = client.post("/recommend", json={"query": "推荐点科幻片", "top_k": 20})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 20
    assert many_agent.seen_top_k == 20


def test_recommend_default_top_k(client, many_agent):
    """不传 top_k → 默认 10（RecommendReq 默认值）。"""
    r = client.post("/recommend", json={"query": "推荐点科幻片"})
    assert r.status_code == 200
    assert many_agent.seen_top_k == 10
    assert len(r.json()["items"]) == 10


def test_recommend_rejects_top_k_over_50(client, many_agent):
    """上限 50 仍由 pydantic 把关（422）。"""
    r = client.post("/recommend", json={"query": "推荐点科幻片", "top_k": 51})
    assert r.status_code == 422


def test_chat_uses_default_top_k(client):
    """/chat 无 top_k 入参 → agent state 默认 20（历史行为）。"""
    seen = {}

    class _CapAgent:
        def invoke(self, state, config=None):
            seen["top_k"] = (state or {}).get("top_k")
            return {"ranked": [], "answer": "好的", "intent": "recommend",
                    "sources": {}, "steps": [], "thinking": {}}

    old = m._AGENT
    m._AGENT = _CapAgent()
    try:
        r = client.post("/chat", json={"query": "推荐部科幻片"})
        assert r.status_code == 200
        assert seen["top_k"] == 20
    finally:
        m._AGENT = old
