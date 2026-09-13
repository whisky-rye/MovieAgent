"""scorer 纯函数单元测试（无外部依赖，可离线跑）。"""
from retrieval.candidates import Candidate
from ranker.scorer import fuse, mmr_diversify


def _mk(i: int, score: float, genres: list[str], src: str = "content") -> Candidate:
    return Candidate(id=f"t{i}", source="douban", title=f"片{i}",
                     genres=genres, recall_source=src, score=score)


def test_fuse_merges_and_ranks():
    recall = {
        "content": [_mk(1, 0.9, ["科幻"]), _mk(2, 0.4, ["喜剧"])],
        "coldstart": [_mk(2, 0.8, ["喜剧"]), _mk(3, 0.2, ["爱情"])],
    }
    out = fuse(recall, top_k=5)
    ids = [c.id for c in out]
    assert "t2" in ids            # 跨源合并去重
    assert out[0].score >= out[-1].score   # 降序


def test_fuse_user_preference_boost():
    recall = {"content": [_mk(1, 0.5, ["科幻", "悬疑"]), _mk(2, 0.5, ["喜剧"])]}
    out = fuse(recall, user_genres=["科幻"])
    top = out[0]
    assert top.id == "t1"         # 偏好命中科幻被加权


def test_mmr_diversifies():
    cands = [
        _mk(1, 1.0, ["科幻", "悬疑"]),
        _mk(2, 0.99, ["科幻", "悬疑"]),
        _mk(3, 0.5, ["喜剧"]),
    ]
    out = mmr_diversify(cands, lambda_div=0.5, top_k=3)
    assert out[0].id in ("t1", "t2")
    assert "t3" in [c.id for c in out]   # 差异大的喜剧被纳入
