"""推荐链路 e2e：/recommend、/recommend/stream（FakeAgent 替换真实 Agent）。

验证 HTTP 契约与响应结构，不依赖 ollama/云端/向量库。同时覆盖护栏拦截与入参校验。
"""
import json
import uuid

import pytest
from sqlalchemy import create_engine, text

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


def _unique_query():
    return f"e2e推荐测试_{uuid.uuid4().hex[:8]}"


def _parse_sse_done(text: str) -> dict:
    """从 SSE 文本中提取最后一个 done 事件的数据体。"""
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if ev == "done" and data:
            return json.loads(data)
    raise AssertionError(f"未找到 done 事件: {text!r}")


def test_recommend_shape(client):
    q = _unique_query()
    r = client.post("/recommend", json={"query": q, "user_id": "e2e", "top_k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intent"] == "recommend"
    assert body["answer"]
    assert body["count"] == 2
    items = body["items"]
    assert len(items) == 2
    first = items[0]
    # _candidate_to_dict 透传的字段
    for k in ("id", "source", "title", "year", "genres", "rating", "score"):
        assert k in first, f"缺字段 {k}"
    assert {i["source"] for i in items} == {"douban", "imdb"}


def test_recommend_empty_query_422(client):
    r = client.post("/recommend", json={"query": "", "user_id": "e2e"})
    assert r.status_code == 422


def test_recommend_stream(client):
    q = _unique_query()
    r = client.post("/recommend/stream", json={"query": q, "user_id": "e2e", "top_k": 5})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    done = _parse_sse_done(r.text)
    assert done["intent"] == "recommend"
    assert len(done["items"]) == 2
    assert {i["source"] for i in done["items"]} == {"douban", "imdb"}


def test_recommend_guardrail_blocks(client, monkeypatch):
    import api.main as m

    class _Block:
        action = "block"
        category = "injection"
        message = "请求被护栏拦截"
        reason = "e2e"

    # 用记录型假 Agent 替换 _AGENT（模块全局，直接 setattr 生效），验证护栏短路
    class _SpyAgent:
        def __init__(self):
            self.calls = 0

        def invoke(self, state, config=None):
            self.calls += 1
            return {
                "ranked": [], "answer": "x", "intent": "recommend",
                "sources": {}, "steps": [], "thinking": {},
            }

    spy = _SpyAgent()
    monkeypatch.setattr(m, "_AGENT", spy)
    monkeypatch.setattr(m, "check_guardrail", lambda q: _Block())

    q = _unique_query()
    r = client.post("/recommend", json={"query": q, "user_id": "e2e"})
    assert r.status_code == 200, r.text
    body = r.json()
    # 护栏拦截：响应标记为 guardrail，且无任何推荐条目
    assert body["intent"] == "guardrail"
    assert body["items"] == []
    # 关键契约：护栏必须短路，绝不进入 Agent（不进 Agent、不耗额度）
    assert spy.calls == 0
