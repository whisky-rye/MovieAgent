"""P1 验证：RRF 融合 vs 原 score 融合，量级健壮性与开关切换。

不依赖 DB / Ollama / 向量库，仅校验排序层逻辑。
运行：.venv/Scripts/python.exe -u tests/test_rrf.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retrieval.candidates import Candidate, split_genres
from ranker.scorer import fuse, _quality_pref
from ranker.rrf import rrf_fuse
from ranker.rerank import rerank
from config.settings import settings


def _c(id_, src, title, score, rating=8.0, genres=("剧情", "科幻")):
    return Candidate(id=id_, source="douban", title=title, year=2020,
                     genres=list(genres), rating=rating, rating_count=1000,
                     recall_source=src, score=score)


# 三路召回，分数数量级悬殊（正是原 score 融合的痛点）：
#  content  ~[0,1]           relation 无上界(~10-25)      coldstart 大数(~300-520)
content = [_c("A", "content", "A-内容", 0.91), _c("B", "content", "B-内容", 0.88),
           _c("C", "content", "C-内容", 0.85)]
relation = [_c("B", "relation", "B-关系", 25.0), _c("D", "relation", "D-关系", 18.0),
            _c("E", "relation", "E-关系", 12.0)]
coldstart = [_c("C", "coldstart", "C-榜单", 520.0), _c("F", "coldstart", "F-榜单", 400.0),
             _c("G", "coldstart", "G-榜单", 300.0)]

recall = {"content": content, "relation": relation, "coldstart": coldstart}
# 模拟 graph 顶层 dict 的「规划名 key」（验证 _PLAN_TO_CANON 映射）
recall_plan_keys = {"search_by_text": content, "similar": relation, "high_quality_board": coldstart}


def _top(ranks, n=3):
    return [(c.id, round(c.score, 4), c.recall_source) for c in ranks[:n]]


print("=== 原 score 融合（min-max 加权） ===")
sf = fuse(recall, user_genres=["剧情"])
print("top3:", _top(sf))

print("\n=== RRF 融合（默认） ===")
rf = rrf_fuse(recall, user_genres=["剧情"])
print("top3:", _top(rf))

print("\n=== RRF（graph 规划名 key，验证 _PLAN_TO_CANON） ===")
rf2 = rrf_fuse(recall_plan_keys, user_genres=["剧情"])
print("top3:", _top(rf2))

print("\n=== rerank 开关切换 ===")
settings.FUSION_STRATEGY = "rrf"
r_rrf = rerank(recall, strategy="mmr", top_k=6, user_genres=["剧情"])
print("FUSION_STRATEGY=rrf  -> rerank top3:", _top(r_rrf))
settings.FUSION_STRATEGY = "score"
r_score = rerank(recall, strategy="mmr", top_k=6, user_genres=["剧情"])
print("FUSION_STRATEGY=score -> rerank top3:", _top(r_score))

# ---- 断言 ----
assert isinstance(rf, list) and len(rf) == 7, "RRF 应合并去重为 7 条"
assert all(rf[i].score >= rf[i + 1].score for i in range(len(rf) - 1)), "RRF 须降序"
# 跨源同 id 合并 recall_source 标记
by_id = {c.id: c for c in rf}
assert by_id["B"].recall_source == "content|relation", f"B 来源合并错误: {by_id['B'].recall_source}"
assert by_id["C"].recall_source == "content|coldstart", f"C 来源合并错误: {by_id['C'].recall_source}"
# rerank 两种策略都返回降序非空
for name, r in (("rrf", r_rrf), ("score", r_score)):
    assert r and all(r[i].score >= r[i + 1].score for i in range(len(r) - 1)), f"{name} 须降序非空"
# 质量/偏好分 helper
q, p = _quality_pref(_c("X", "content", "x", 0.5, rating=9.0, genres=("剧情", "科幻")), ["剧情", "科幻"])
assert abs(q - 0.18) < 1e-9 and p == 0.3, f"quality/pref 计算错误: {q},{p}"

# 还原默认（.env 为 rrf）
settings.FUSION_STRATEGY = "rrf"
print("\n[OK] 全部断言通过：RRF 跨源合并/降序/开关切换/质量偏好分均正确")
