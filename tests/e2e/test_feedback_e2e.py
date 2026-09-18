"""O11 反馈闭环 e2e：反馈采集接口 + 推荐结果自动曝光埋点（MySQL 门控）。

- POST /feedback：前端赞/踩/点落库（user_feedback），读回画像验证聚合正确。
- /recommend 完成后自动把结果记为 impression（位次即 rank），验证闭环「采集」侧。
- 不依赖 ollama/云端：复用 conftest 的 client 夹具；曝光测试用 Candidate 型假 Agent 替换。
"""
import uuid

import pytest
from sqlalchemy import create_engine, text

from config.settings import settings
from agent.feedback_store import get_feedback_store, SIGNAL_IMPRESSION


def _alive():
    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _alive(), reason="MySQL 不可用，跳过后端 e2e")


def _uid(prefix="e2e_fb"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_post_feedback_signals_persist(client):
    uid = _uid()
    try:
        # 赞
        r = client.post("/feedback", json={"item_id": "douban:1001", "signal": "like",
                                            "genres": ["剧情"], "user_id": uid})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "state": "like"}
        # 踩
        r = client.post("/feedback", json={"item_id": "douban:1002", "signal": "dislike",
                                            "genres": ["动作"], "user_id": uid})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "state": "dislike"}
        # 点
        r = client.post("/feedback", json={"item_id": "douban:1003", "signal": "click",
                                            "user_id": uid})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "state": "click"}

        prof = get_feedback_store().get_profile(uid)
        assert prof["item_boost"].get("douban:1001") is not None       # 赞 → 加分集
        assert prof["item_blacklist"].get("douban:1002") is not None   # 踩 → 黑名单
        # 类型亲和度仅来自显式赞踩
        assert "剧情" in prof["genre_affinity"] and prof["genre_affinity"]["剧情"] > 0
        assert "动作" in prof["genre_affinity"] and prof["genre_affinity"]["动作"] < 0
    finally:
        get_feedback_store().clear(uid)


def test_post_feedback_invalid_signal_400(client):
    uid = _uid()
    r = client.post("/feedback", json={"item_id": "douban:1", "signal": "favorite",
                                        "user_id": uid})
    assert r.status_code == 400
    assert "invalid signal" in r.json().get("detail", "")


def test_recommend_auto_logs_impressions(client, monkeypatch):
    """/recommend 完成后，结果逐条记为 impression（含 rank），闭环采集侧打通。"""
    from retrieval.candidates import Candidate
    import api.main as m

    class _CandAgent:
        def invoke(self, state, config=None):
            return {
                "ranked": [
                    Candidate(id="douban:9101", source="douban", title="X", genres=["剧情"], score=0.9),
                    Candidate(id="douban:9102", source="douban", title="Y", genres=["动作"], score=0.8),
                ],
                "answer": "a", "intent": "recommend", "sources": {}, "steps": [], "thinking": {},
            }

    monkeypatch.setattr(m, "_AGENT", _CandAgent())
    # 让 items 可序列化且可断言
    monkeypatch.setattr(m, "_candidate_to_dict",
                        lambda c: {"id": c.id, "genres": c.genres, "title": c.title,
                                   "source": c.source, "score": c.score})

    uid = _uid("e2e_imp")
    try:
        r = client.post("/recommend", json={"query": f"e2e推荐曝光测试_{uuid.uuid4().hex[:8]}",
                                             "user_id": uid, "top_k": 5})
        assert r.status_code == 200, r.text
        assert len(r.json()["items"]) == 2

        # 直接查表：两条 impression 已落库，且名次正确
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            rows = c.execute(
                text("SELECT `item_id`, `rank` FROM user_feedback "
                     "WHERE `user_id`=:u AND `signal`=:s ORDER BY `rank`"),
                {"u": uid, "s": SIGNAL_IMPRESSION},
            ).fetchall()
        assert len(rows) == 2
        assert {row[0] for row in rows} == {"douban:9101", "douban:9102"}
        ranks = {row[0]: row[1] for row in rows}
        assert ranks["douban:9101"] == 1 and ranks["douban:9102"] == 2
    finally:
        get_feedback_store().clear(uid)


def test_explicit_mutex_same_item(client):
    """同一作品先赞后踩（或先踩后赞）应互斥：只保留最后一条，画像/恢复态一致。"""
    uid = _uid()
    try:
        client.post("/feedback", json={"item_id": "douban:2001", "signal": "like",
                                       "genres": ["剧情"], "user_id": uid})
        r = client.post("/feedback", json={"item_id": "douban:2001", "signal": "dislike",
                                          "genres": ["剧情"], "user_id": uid})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "dislike"

        # 画像：只进黑名单，不进加分集（排序回灌不会「界面已切喜欢却按踩沉底」）
        prof = get_feedback_store().get_profile(uid)
        assert prof["item_blacklist"].get("douban:2001") is not None
        assert "douban:2001" not in prof["item_boost"]

        # GET /feedback/mine 恢复：仅一条，状态为 dislike
        r = client.get(f"/feedback/mine?user_id={uid}")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert items.get("douban:2001") == "dislike"
        assert len(items) == 1
    finally:
        get_feedback_store().clear(uid)


def test_cancel_clears_explicit(client):
    """再点一次 = 取消：POST cancel 后该作品在历史/画像中均消失。"""
    uid = _uid()
    try:
        client.post("/feedback", json={"item_id": "douban:3001", "signal": "like", "user_id": uid})
        r = client.post("/feedback", json={"item_id": "douban:3001", "signal": "cancel", "user_id": uid})
        assert r.status_code == 200, r.text
        assert r.json()["state"] is None

        items = client.get(f"/feedback/mine?user_id={uid}").json()["items"]
        assert items.get("douban:3001") is None
        assert "douban:3001" not in get_feedback_store().get_profile(uid)["item_boost"]
    finally:
        get_feedback_store().clear(uid)
