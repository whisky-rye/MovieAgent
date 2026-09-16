"""会话共享（G-Share）接口行为验证：

1. 分享本人会话 -> 200 返回令牌+链接；公开 GET /share/{token} 可读且不含 owner；
   会话列表带 shared/share_token 标记；取消共享后原链接 404。
2. 不能共享他人会话（owner 校验 403）。
3. 无效 / 未共享令牌一律 404（避免枚举）。
依赖真实 app + 真实存储（MySQL 在线则落库，否则 file 兜底），结束后清理测试数据。
"""
from fastapi.testclient import TestClient

import api.main as m
import agent.session_store as ss_mod
import agent.share_store as sh_mod

client = TestClient(m.app)


def _new_session(uid: str, title: str = "测试会话") -> str:
    r = client.post("/sessions", json={"title": title, "user_id": uid})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _cleanup(sid: str, uid: str) -> None:
    try:
        client.delete(f"/sessions/{sid}?user_id={uid}")
    except Exception:
        pass
    try:
        sh_mod.get_share_store().delete(sid)
    except Exception:
        pass


def test_share_flow_and_public_view():
    uid = "share_test_user"
    sid = _new_session(uid, "我的观影清单")
    try:
        # 1) 共享本人会话
        r = client.post(f"/sessions/{sid}/share?user_id={uid}")
        assert r.status_code == 200, r.text
        body = r.json()
        token = body["share_token"]
        assert token and body["share_url"].endswith(f"/share/{token}")

        # 2) 公开只读查看（无鉴权）：可读内容但不暴露 owner
        pub = client.get(f"/share/{token}")
        assert pub.status_code == 200, pub.text
        pub_json = pub.json()
        assert pub_json["title"] == "我的观影清单"
        assert "owner_user_id" not in pub_json
        assert "user_id" not in pub_json

        # 3) 会话列表带 shared 标记
        lst = client.get(f"/sessions?user_id={uid}").json()
        item = next(s for s in lst if s["session_id"] == sid)
        assert item["shared"] is True
        assert item["share_token"] == token

        # 4) 取消共享后原链接失效
        d = client.delete(f"/sessions/{sid}/share?user_id={uid}")
        assert d.status_code == 204
        assert client.get(f"/share/{token}").status_code == 404
    finally:
        _cleanup(sid, uid)


def test_cannot_share_others_session():
    alice = "alice"
    bob = "bob"
    sid_a = _new_session(alice, "Alice 的私密会话")
    sid_b = _new_session(bob, "Bob 的会话")
    try:
        # bob 试图共享 alice 的会话 -> 存在但非本人 -> 403
        r = client.post(f"/sessions/{sid_a}/share?user_id={bob}")
        assert r.status_code == 403, r.text
        # alice 的会话并未被共享
        assert client.get(f"/sessions?user_id={alice}").json()[0]["shared"] is False
    finally:
        _cleanup(sid_a, alice)
        _cleanup(sid_b, bob)


def test_invalid_token_404():
    # 不存在的令牌 / 未共享令牌均 404（同态，避免枚举探测）
    assert client.get("/share/nonexistent-token-xyz").status_code == 404
    sid = _new_session("u_no_share", "未共享会话")
    try:
        assert client.get(f"/share/{sid}").status_code == 404
    finally:
        _cleanup(sid, "u_no_share")
