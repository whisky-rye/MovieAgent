"""P1-3 e2e：/chat 接入 session_id（历史上下文 + 落库）。

修复前 /chat 不传 session_id：_run 拿不到历史（每轮都是单轮）、done 后不落库，
"流式对话"实际是单轮玩具。修复后与 /recommend/stream 对齐：
- ChatReq.session_id 透传 _run（多轮历史连贯）；
- 流式结束后经 _persist_session 落库（归属校验通过才写）。

依赖 MySQL（会话/落库）+ FakeAgent（conftest 提供），离线自动 skip。
"""
import uuid

import pytest

import api.main as m
from config.settings import settings


def _alive():
    from sqlalchemy import create_engine, text

    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _alive(), reason="MySQL 不可用，跳过后端 e2e")


class _CapturingAgent:
    """记录每次 invoke 的 state，返回确定化结果（绕开真实 Agent）。"""

    def __init__(self):
        self.seen = []

    def invoke(self, state, config=None):
        self.seen.append(dict(state or {}))
        return {
            "ranked": [],
            "answer": f"收到：{(state or {}).get('query', '')}",
            "intent": "recommend",
            "sources": {},
            "steps": [],
            "thinking": {},
        }


@pytest.fixture
def agent():
    ag = _CapturingAgent()
    old = m._AGENT
    m._AGENT = ag
    yield ag
    m._AGENT = old


def _new_session(client):
    r = client.post("/sessions", json={})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def test_chat_with_session_persists_round(client, agent):
    """/chat 带 session_id → 流式结束后落库（user + assistant 两条）。"""
    sid = _new_session(client)
    r = client.post("/chat", json={"query": "推荐点科幻片", "session_id": sid})
    assert r.status_code == 200
    assert "收到：推荐点科幻片" in r.text

    rec = client.get(f"/sessions/{sid}").json()
    roles = [msg["role"] for msg in rec["messages"]]
    assert roles == ["user", "assistant"], f"落库消息异常: {rec['messages']}"
    assert rec["messages"][0]["content"] == "推荐点科幻片"
    assert "收到：推荐点科幻片" in rec["messages"][1]["content"]


def test_chat_second_round_sees_history(client, agent):
    """/chat 第二轮应带上首轮历史（session_id 透传 _run → _build_history）。"""
    sid = _new_session(client)
    client.post("/chat", json={"query": "推荐点科幻片", "session_id": sid})
    client.post("/chat", json={"query": "有没有类似的悬疑片", "session_id": sid})
    assert len(agent.seen) == 2
    assert "推荐点科幻片" in (agent.seen[-1].get("history") or ""), \
        f"第二轮未携带历史: {agent.seen[-1].get('history')!r}"


def test_chat_without_session_not_persisted(client, agent):
    """/chat 不带 session_id → 临时对话：不报错、不落库、无历史。"""
    r = client.post("/chat", json={"query": "推荐一部科幻片"})
    assert r.status_code == 200
    assert "收到：推荐一部科幻片" in r.text
    assert agent.seen[-1].get("history") in (None, "")


def test_chat_rejects_foreign_session(client, agent):
    """/chat 带他人 session_id → 归属校验失败，不落库（不 500）。"""
    from sqlalchemy import create_engine, text

    other_uid = f"e2e_chat_{uuid.uuid4().hex[:10]}"
    sid = _new_session(client)
    # 把会话 owner 改成别人（直连业务库模拟越权）
    e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN)
    with e.connect() as c:
        c.execute(text("UPDATE sessions SET user_id=:u WHERE session_id=:s"),
                  {"u": other_uid, "s": sid})
        c.commit()
    try:
        r = client.post("/chat", json={"query": "推荐部科幻片", "session_id": sid})
        assert r.status_code == 200  # 流式本身不失败
        # 归属校验失败：跳过落库（直连库验证 messages 仍为空）
        with e.connect() as c:
            msgs = c.execute(
                text("SELECT messages FROM sessions WHERE session_id=:s"), {"s": sid}
            ).scalar()
        assert not msgs or msgs in ("[]", "null"), f"他人会话被写入: {msgs!r}"
        # 前端侧同样读不到（owner 不是当前用户）
        assert client.get(f"/sessions/{sid}").status_code == 403
    finally:
        with e.connect() as c:
            c.execute(text("DELETE FROM sessions WHERE user_id=:u"), {"u": other_uid})
            c.commit()
        e.dispose()
