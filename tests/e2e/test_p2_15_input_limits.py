"""P2-15 e2e：sanitized_query 驱动全链路 + 请求模型长度上限 422。

修复前：sanitize_query 的截断结果不返回——原始 query（几十 KB）全额进嵌入/
云端调用；请求模型无 max_length——title/item_id/note 等可写入任意长文本。
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


class _CaptureAgent:
    """捕获 agent 收到的 state.query 的假 agent。"""

    def invoke(self, state, config=None):
        self.seen_query = (state or {}).get("query")
        return {"ranked": [], "answer": "ok", "intent": "recommend",
                "sources": {}, "steps": [], "thinking": {}}


@pytest.fixture
def cap_agent():
    ag = _CaptureAgent()
    old = m._AGENT
    m._AGENT = ag
    yield ag
    m._AGENT = old


def test_run_passes_sanitized_query_to_agent(cap_agent):
    """_run 放行后，agent 收到的是净化+截断后的 query（不是原始超长/含零宽输入）。"""
    raw = "科\u200b幻" + "x" * (settings.GUARDRAIL_MAX_QUERY_LEN + 100)
    m._run(raw, "u", request_id="p2_15_test")
    seen = cap_agent.seen_query
    assert seen is not None
    assert "\u200b" not in seen, "零宽字符应被净化"
    assert len(seen) <= settings.GUARDRAIL_MAX_QUERY_LEN, (
        f"query 未被截断到上限: len={len(seen)}"
    )
    assert seen.startswith("科幻"), "净化不应改变语义前缀"


def test_recommend_rejects_oversized_query(client):
    """超长 query 在边缘 422（max_length=GUARDRAIL_MAX_QUERY_LEN）。"""
    r = client.post("/recommend",
                    json={"query": "x" * (settings.GUARDRAIL_MAX_QUERY_LEN + 1)})
    assert r.status_code == 422


def test_recommend_accepts_query_at_limit(client, cap_agent):
    """恰好等于上限的 query 放行（并被护栏截断逻辑原样通过）。"""
    q = "科幻" * (settings.GUARDRAIL_MAX_QUERY_LEN // 2)
    r = client.post("/recommend", json={"query": q})
    assert r.status_code == 200
    assert cap_agent.seen_query == q


def test_chat_rejects_oversized_query(client):
    r = client.post("/chat",
                    json={"query": "x" * (settings.GUARDRAIL_MAX_QUERY_LEN + 1)})
    assert r.status_code == 422


def test_session_title_rejects_oversized(client):
    r = client.post("/sessions", json={"title": "t" * 101})
    assert r.status_code == 422
    r2 = client.patch("/sessions/does-not-exist-xyz", json={"title": "t" * 101})
    assert r2.status_code == 422


def test_feedback_rejects_oversized_fields(client):
    r = client.post("/feedback",
                    json={"item_id": "d" * 65, "signal": "like"})
    assert r.status_code == 422
    r2 = client.post("/feedback",
                     json={"item_id": "douban:1", "signal": "like",
                           "query": "q" * 256})
    assert r2.status_code == 422


def test_feedback_accepts_normal_fields(client):
    r = client.post("/feedback",
                    json={"item_id": "douban:1", "signal": "click",
                          "query": "推荐科幻片"})
    assert r.status_code == 200
