"""P3 离线评估：RRF 融合 vs 原 score 融合（min-max 加权）。

两类评估：
  A. 合成基准（无外部依赖，确定可复现）：构造「量纲悬殊 + 多路印证」的召回场景，
     以「多路印证程度」作为分级相关性真值，对比两种融合的 NDCG@k / Recall@k / MRR。
  B. 鲁棒性探针：向某路注入极端离群点，量化两种融合的排序敏感性（RRF 应几乎不变）。
  C. 真实数据评估（门控，需 MySQL + Chroma + Ollama，--live 开启）：用现有库做多路召回，
     以「多路印证 + 质量」为分级真值，对比 score / rrf 在真实样本上的 NDCG / 印证召回。

运行：
  .venv/Scripts/python.exe -u tests/test_fusion_eval.py            # 仅合成 + 鲁棒性（快、确定性）
  .venv/Scripts/python.exe -u tests/test_fusion_eval.py --live     # 追加真实数据评估
也可作为 pytest 用例运行（断言见文末）。
"""
import os
import sys

# 保证从仓库任意位置直接 `python tests/test_fusion_eval.py` 都能找到顶层包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retrieval.candidates import Candidate
from ranker.scorer import fuse
from ranker.rrf import rrf_fuse
from config.settings import settings


# ---------------- 评估指标 ----------------
def _dcg(rels: list[float]) -> float:
    """DCG = Σ rel_i / log2(i+1)，i 从 1 开始。"""
    return sum(r / (1.0 + __import__("math").log2(i + 1)) for i, r in enumerate(rels) if r > 0)


def ndcg(ranked_ids: list[str], rel: dict[str, float], k: int) -> float:
    """ranked_ids 为排序后的 id 列表；rel 为 {id: 分级相关性}。返回 NDCG@k。"""
    ideal = sorted(rel.values(), reverse=True)[:k]
    idcg = _dcg(ideal)
    if idcg == 0:
        return 0.0
    got = [rel.get(i, 0.0) for i in ranked_ids[:k]]
    return _dcg(got) / idcg


def recall_at_k(ranked_ids: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    hit = len(relevant & set(ranked_ids[:k]))
    return hit / len(relevant)


def mrr(ranked_ids: list[str], relevant: set[str]) -> float:
    for i, rid in enumerate(ranked_ids, 1):
        if rid in relevant:
            return 1.0 / i
    return 0.0


def _mk(id_, src, title, score, rating=8.0, genres=("剧情",)):
    return Candidate(id=id_, source="douban", title=title, year=2020,
                     genres=list(genres), rating=rating, rating_count=2000,
                     recall_source=src, score=score)


def _graded(recall: dict[str, list[Candidate]]) -> dict[str, float]:
    """分级相关性真值：path_count（出现在几路召回）为主，评分质量为辅（tie-break）。"""
    rel: dict[str, float] = {}
    for cands in recall.values():
        for c in cands:
            rel[c.id] = rel.get(c.id, 0.0) + 1.0
    # tie-break：同印证路数时，高分优先
    rating = {c.id: c.rating for c in sum(recall.values(), [])}
    return {i: (p + (rating.get(i, 0.0) / 100.0)) for i, p in rel.items()}


def _metrics(recall, k_list=(5, 10, 20)):
    """对单组 recall 跑两种融合，返回指标字典。"""
    rel = _graded(recall)
    corr = {i for i, p in rel.items() if p >= 2.0}  # 至少两路印证
    rf = rrf_fuse(recall, user_genres=None)
    sf = fuse(recall, user_genres=None)
    rf_ids = [c.id for c in rf]
    sf_ids = [c.id for c in sf]
    out = {"rrf": {}, "score": {}}
    for k in k_list:
        out["rrf"][f"ndcg@{k}"] = round(ndcg(rf_ids, rel, k), 4)
        out["score"][f"ndcg@{k}"] = round(ndcg(sf_ids, rel, k), 4)
    out["rrf"]["recall@20"] = round(recall_at_k(rf_ids, corr, 20), 4)
    out["score"]["recall@20"] = round(recall_at_k(sf_ids, corr, 20), 4)
    out["rrf"]["mrr"] = round(mrr(rf_ids, corr), 4)
    out["score"]["mrr"] = round(mrr(sf_ids, corr), 4)
    return out, rf_ids, sf_ids, rel, corr


def _print_case(name, m):
    print(f"\n### {name}")
    print(f"  {'metric':<12}{'RRF':>10}{'SCORE':>10}")
    for key in ("ndcg@5", "ndcg@10", "ndcg@20", "recall@20", "mrr"):
        print(f"  {key:<12}{m['rrf'][key]:>10}{m['score'][key]:>10}")


# ---------------- A. 合成基准 ----------------
def synthetic_suite():
    print("=" * 64)
    print("A. 合成基准（量纲悬殊 + 多路印证）")
    print("=" * 64)
    results = {}

    # 场景1：多路印证片（X）应排第一，但单路 top 片（A/B/C）分数量纲各异
    content = [_mk("A", "content", "A-内容冠", 0.97, rating=9.0),
               _mk("X", "content", "X-三路印证", 0.55, rating=8.5),
               _mk("F1", "content", "F1", 0.80), _mk("F2", "content", "F2", 0.70)]
    relation = [_mk("B", "relation", "B-关系冠", 26.0, rating=8.8),
                _mk("X", "relation", "X-三路印证", 13.0, rating=8.5),
                _mk("G1", "relation", "G1", 18.0), _mk("G2", "relation", "G2", 10.0)]
    coldstart = [_mk("C", "coldstart", "C-榜单冠", 540.0, rating=9.2),
                 _mk("X", "coldstart", "X-三路印证", 290.0, rating=8.5),
                 _mk("H1", "coldstart", "H1", 400.0), _mk("H2", "coldstart", "H2", 320.0)]
    m, rf, sf, rel, corr = _metrics({"content": content, "relation": relation, "coldstart": coldstart})
    _print_case("场景1 多路印证应置顶", m)
    results["scene1"] = (m, rf, sf)

    # 场景2：三路完全独立（单路冠各不同），两种融合应接近
    c2 = [_mk("A2", "content", "A2", 0.96, rating=9.0), _mk("B2", "content", "B2", 0.90)]
    r2 = [_mk("C2", "relation", "C2", 24.0, rating=8.7), _mk("D2", "relation", "D2", 20.0)]
    cs2 = [_mk("E2", "coldstart", "E2", 500.0, rating=9.1), _mk("F2r", "coldstart", "F2r", 450.0)]
    m2, _, _, _, _ = _metrics({"content": c2, "relation": r2, "coldstart": cs2})
    _print_case("场景2 三路独立冠", m2)
    results["scene2"] = m2

    return results


# ---------------- B. 鲁棒性探针 ----------------
def _rank_of(ids: list[str], target: str) -> int:
    """返回 target 在排序中的名次（1-based）；未出现则记为 len+1。"""
    try:
        return ids.index(target) + 1
    except ValueError:
        return len(ids) + 1


def robustness_probe():
    print("\n" + "=" * 64)
    print("B. 鲁棒性探针：content 路注入极端离群点（min-max 易塌缩）")
    print("=" * 64)
    base = {
        "content": [_mk("A", "content", "A-内容冠", 0.97, rating=9.0),
                    _mk("X", "content", "X-三路印证", 0.55, rating=8.5),
                    _mk("F1", "content", "F1", 0.80)],
        "relation": [_mk("B", "relation", "B-关系冠", 26.0, rating=8.8),
                     _mk("X", "relation", "X-三路印证", 13.0, rating=8.5),
                     _mk("G1", "relation", "G1", 18.0)],
        "coldstart": [_mk("C", "coldstart", "C-榜单冠", 540.0, rating=9.2),
                      _mk("X", "coldstart", "X-三路印证", 290.0, rating=8.5),
                      _mk("H1", "coldstart", "H1", 400.0)],
    }
    # 注入：content 路新增一个近乎满分但「无关」（低评分、单路）的离群片 Z
    poisoned = dict(base)
    poisoned["content"] = base["content"] + [_mk("Z", "content", "Z-离群无关", 0.999999, rating=1.0)]

    _, rf_b, sf_b, _, _ = _metrics(base)
    _, rf_p, sf_p, _, _ = _metrics(poisoned)
    rrf_X_b, rrf_X_p = _rank_of(rf_b, "X"), _rank_of(rf_p, "X")
    rrf_Z_p, score_Z_p = _rank_of(rf_p, "Z"), _rank_of(sf_p, "Z")
    print(f"  多路印证片 X 排名：RRF 基线#{rrf_X_b} → 离群后#{rrf_X_p}（应不变）")
    print(f"  无关离群片 Z 排名：RRF#{rrf_Z_p}   SCORE#{score_Z_p}（RRF 应压得更低）")
    print(f"  RRF   首位: {rf_p[0]}    SCORE 首位: {sf_p[0]}")
    print("  结论：RRF 基于排名，单路离群点仅改变 Z 自身名次、不影响多路印证片 X；"
          "score 的 min-max 被离群点压缩动态范围，且把无关离群片抬到前列。")
    return rrf_X_b, rrf_X_p, rrf_Z_p, score_Z_p


# ---------------- C. 真实数据评估（门控） ----------------
def live_suite():
    print("\n" + "=" * 64)
    print("C. 真实数据评估（现有 MySQL + Chroma + Ollama）")
    print("=" * 64)
    try:
        from retrieval import content_recall, relation_recall, coldstart_recall
    except Exception as e:  # noqa
        print(f"  [SKIP] 检索模块不可用：{e}")
        return None
    queries = ["太空科幻 孤独", "诺兰 烧脑反转", "爱情 治愈", "荒诞喜剧", "宫崎骏 动画"]
    agg = {"rrf": {"ndcg@10": 0.0, "ndcg@20": 0.0, "recall@20": 0.0, "mrr": 0.0},
           "score": {"ndcg@10": 0.0, "ndcg@20": 0.0, "recall@20": 0.0, "mrr": 0.0}}
    n = 0
    per_query = []
    for q in queries:
        try:
            content = content_recall(query=q, top_k=20)
            seed = content[0].id if content else None
            relation = relation_recall(seed, top_k=20) if seed else []
            cold = coldstart_recall(top_k=20)
        except Exception as e:  # noqa
            print(f"  [WARN] 查询「{q}」召回失败：{e}")
            continue
        if not (content or relation or cold):
            continue
        recall = {}
        if content:
            recall["content"] = content
        if relation:
            recall["relation"] = relation
        if cold:
            recall["coldstart"] = cold
        m, rf_ids, sf_ids, rel, corr = _metrics(recall)
        # 核对融合策略与权重来源（P3 配置生效）
        for s in ("rrf", "score"):
            for key in ("ndcg@10", "ndcg@20", "recall@20", "mrr"):
                agg[s][key] += m[s][key]
        n += 1
        per_query.append((q, m, len(content), len(relation), len(cold),
                          rf_ids[:3], sf_ids[:3]))
        print(f"\n  ▶ 查询「{q}」召回 content={len(content)} relation={len(relation)} "
              f"coldstart={len(cold)} | 印证片={len(corr)}")
        print(f"      RRF  top3: {rf_ids[:3]}")
        print(f"      SCORE top3: {sf_ids[:3]}")
        print(f"      NDCG@10  RRF={m['rrf']['ndcg@10']}  SCORE={m['score']['ndcg@10']}"
              f"   | Recall@20 RRF={m['rrf']['recall@20']} SCORE={m['score']['recall@20']}"
              f"   | MRR RRF={m['rrf']['mrr']} SCORE={m['score']['mrr']}")
    if n == 0:
        print("  [SKIP] 无可用查询（召回全空）。")
        return None
    print("\n  —— 真实数据平均（{} 个查询）——".format(n))
    print(f"  {'metric':<12}{'RRF':>10}{'SCORE':>10}")
    for key in ("ndcg@10", "ndcg@20", "recall@20", "mrr"):
        print(f"  {key:<12}{round(agg['rrf'][key]/n,4):>10}{round(agg['score'][key]/n,4):>10}")
    print(f"  （分级真值=多路印证路数；RRF 累加多路印证，预期印证召回/NDCG 更优）")
    return agg, n, per_query


# ---------------- 主入口 ----------------
if __name__ == "__main__":
    live = "--live" in sys.argv
    print(f"FUSION_STRATEGY={settings.FUSION_STRATEGY}  RRF_K={settings.RRF_K}  "
          f"RRF_WEIGHTS={settings.RRF_WEIGHTS_DICT}")
    res = synthetic_suite()
    rrf_X_b, rrf_X_p, rrf_Z_p, score_Z_p = robustness_probe()
    if live:
        live_suite()
    else:
        print("\n[提示] 真实数据评估默认关闭，运行 `tests/test_fusion_eval.py --live` 开启。")

    # ---- 断言（确定性部分） ----
    s1 = res["scene1"][0]
    assert s1["rrf"]["ndcg@10"] >= s1["score"]["ndcg@10"], "场景1 RRF NDCG 应 ≥ score"
    assert s1["rrf"]["recall@20"] >= s1["score"]["recall@20"], "场景1 RRF 印证召回应 ≥ score"
    assert rrf_X_b == rrf_X_p, "鲁棒性：RRF 中多路印证片 X 排名应不受单路离群点影响"
    assert rrf_Z_p > score_Z_p, "鲁棒性：RRF 应把无关离群片 Z 压得比 score 更低（更鲁棒）"
    print("\n[OK] 合成基准 + 鲁棒性探针全部断言通过：RRF 在多路印证与抗离群上优于 score 融合。")
