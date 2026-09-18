"""O11 离线门禁：验证「反馈调权」不破坏召回质量。

做法（ollama-free，直接验 ranker 注入，不跑整条 Agent）：
  1. 读 data/eval_gold.json（query → relevant ids）。
  2. 对每个 query，用 gold 相关项构造「召回集」：相关项给低基础分(0.4)，并混入若干
     非相关干扰项(高基础分 0.9) —— 模拟「无反馈时相关项被干扰项压在下面」。
  3. 把相关项视为用户「赞过」，构造 feedback_profile={item_boost:{rid: +}}。
  4. 分别跑 rerank(无 profile) 与 rerank(有 profile)，比 NDCG@10。
  5. 断言：开启反馈后 NDCG@10 不低于基线 -0.02（即调权必须涨分或基本持平，不得回归）。

结论：若相关项被反馈正确顶上去，NDCG 应上升；否则说明注入方向/强度有问题，拦截上线。
"""
import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retrieval.candidates import Candidate
from ranker import rerank
from eval.metrics import ndcg_at_k
from agent.feedback_store import ITEM_LIKE_BOOST

GOLD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "eval_gold.json")
TOLERANCE = 0.02  # 允许的最大回退


def _load_gold(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d.get("queries") or d.get("gold") or []


def _build_recall(relevant: list[str], distractors: list[str]) -> dict:
    """构造「无反馈时相关项被干扰项压在下面」的召回集（模拟 RRF 路径）。

    - 生产默认 FUSION_STRATEGY=rrf：rerank 按「召回列表中的排名位次」融合，
      忽略 Candidate.score 字段。因此这里用**列表顺序**控制排名：干扰项放前面（占高排名），
      相关项放后面（占低排名）。
    - 无反馈：top_k 被干扰项占据，相关项沉底 → NDCG 低。
    - 开启反馈：相关项 +ITEM_LIKE_BOOST（量级远大于 RRF 分项 ~0.015）跃居顶部 → NDCG 升。
    - 相关项分数给一点抖动仅用于可复现，不影响 RRF 排名（RRF 不看 score）。
    """
    cands = []
    for did in distractors:  # 先放干扰项 → 高排名
        cands.append(Candidate(id=did, source="douban", title=did, score=0.62))
    for rid in relevant:     # 后放相关项 → 低排名
        jitter = (abs(hash(rid)) % 12) * 0.01
        cands.append(Candidate(id=rid, source="douban", title=rid, score=0.50 + jitter))
    return {"content": cands}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=GOLD_PATH)
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--distractors", type=int, default=8, help="每个 query 混入的干扰项数量")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if not os.path.exists(args.gold):
        print(f"[skip] 找不到 gold：{args.gold}（先跑 scripts/build_gold.py）")
        sys.exit(0)

    gold = _load_gold(args.gold)
    if not gold:
        print("[skip] gold 为空")
        sys.exit(0)

    random.seed(args.seed)
    all_ids = [i for q in gold for i in q.get("relevant", [])]
    if len(all_ids) < args.distractors + 1:
        print("[skip] gold 相关项不足以构造干扰集")
        sys.exit(0)

    print(f"{'query':<28} {'NDCG@10 base':>13} {'NDCG@10 fb':>12} {'Δ':>8}  verdict")
    print("-" * 78)
    deltas = []
    violated = 0
    for q in gold:
        relevant = q.get("relevant", [])
        if not relevant:
            continue
        pool = [i for i in all_ids if i not in set(relevant)]
        distractors = random.sample(pool, min(args.distractors, len(pool)))
        recall = _build_recall(relevant, distractors)

        base_ids = [c.id for c in rerank(recall, strategy="score", top_k=args.topk)]
        profile = {"item_boost": {rid: ITEM_LIKE_BOOST for rid in relevant},
                   "item_blacklist": {}, "genre_affinity": {}}
        fb_ids = [c.id for c in rerank(recall, strategy="score", top_k=args.topk, feedback_profile=profile)]

        base = ndcg_at_k(base_ids, set(relevant), args.topk)
        fb = ndcg_at_k(fb_ids, set(relevant), args.topk)
        delta = fb - base
        deltas.append(delta)
        ok = delta >= -TOLERANCE
        violated += 0 if ok else 1
        label = "OK" if ok else "REGRESS"
        qry = q.get("query", "")[:26]
        print(f"{qry:<28} {base:>13.4f} {fb:>12.4f} {delta:>+8.4f}  {label}")

    avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
    print("-" * 78)
    print(f"平均 ΔNDCG@10 = {avg_delta:+.4f}   回退超标 query 数 = {violated}/{len(deltas)}")
    if violated > 0:
        print(f"[FAIL] 反馈调权导致 {violated} 个 query NDCG 回退超 {TOLERANCE}，拦截上线")
        sys.exit(1)
    print("[PASS] 反馈调权未导致召回质量回归，可上线")


if __name__ == "__main__":
    main()
