"""O9/D3：离线评估脚本——以 NDCG / 命中率 / 多样性为核心指标量化推荐质量。

用法：
  # 用真实语料标注的 gold（data/eval_gold.json，基于类型标注）跑融合排序基线：
  python scripts/eval_run.py --gold data/eval_gold.json
  # 对照：仅用 content_recall 单路召回作为评估对象（旧口径）：
  python scripts/eval_run.py --gold data/eval_gold.json --mode recall
  # 把内置示例 gold（content_recall proxy，仅演示脚本可跑通）落成文件：
  python scripts/eval_run.py --write-sample data/eval_gold.sample.json

gold JSON 格式：
  {"queries":[{"query":"太空科幻","relevant":["douban:63","imdb:tt123"],
               "note":"相关判定依据（如：类型标注=科幻）"}, ...]}

评估对象（--mode）：
  - fused  （默认）：跑完整 Agent 图（内容+关系+冷启动 → 融合 → MMR → LLM 精排）的
            `ranked` 排序，端到端反映真实推荐质量；
  - recall ：仅 content_recall 单路向量召回排序（旧口径，用于对照）。

指标（eval/metrics.py，纯函数）：
  NDCG@k / Hit@k / Recall@k / Diversity@k。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List

# 允许以脚本方式直接运行（python scripts/eval_run.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval import metrics  # noqa: E402
from eval.pipeline import run_fused_ranking  # noqa: E402
from retrieval.content_recall import content_recall  # noqa: E402
from retrieval.candidates import Candidate  # noqa: E402
from ranker.rrf import rrf_fuse  # noqa: E402

EVAL_TOPK = 20
SAMPLE_TOPN = 10  # 内置示例 gold：取 content_recall 前 N 作为相关 proxy


def load_gold(path: str) -> Dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _pure_ranking(q: str, k: int) -> List[Candidate]:
    """纯排序函数（P2-23）：召回 -> RRF 融合，无 LLM、无状态写入、结果可复现。

    仅用确定性融合（RRF 基于召回排名，不看 score），不调用 LLM 精排，
    也不触发任何 memory 写入，适合 CI/离线评估。召回不可用时返回空列表。
    """
    recall: Dict[str, List[Candidate]] = {}
    try:
        content = content_recall(query=q, top_k=k)
        if content:
            recall["content"] = content
    except Exception:  # noqa: BLE001
        pass
    if recall:
        fused = rrf_fuse(recall, user_genres=None)
        return fused[:k]
    return []


def _ranking_fn(mode: str, ranking: str):
    """返回 (排序函数, 说明)。排序函数签名为 (query, top_k) -> List[Candidate]。

    mode:
      - fused ：（默认）融合排序评估对象
      - recall ：仅 content_recall 单路向量召回（旧口径对照）
    ranking（P2-23，仅 fused 生效）：
      - pure  ：（默认）注入纯排序函数，无 LLM/无副作用/可复现
      - agent ：跑完整 Agent 图（含 LLM 精排，需 LLM/向量库，有真实链路语义）
    """
    if mode == "recall":
        def fn(q: str, k: int) -> List[Candidate]:
            return content_recall(query=q, top_k=k)
        return fn, "content_recall 单路向量召回"
    if ranking == "agent":
        def fn(q: str, k: int) -> List[Candidate]:
            return run_fused_ranking(q, top_k=k)
        return fn, "Agent 图 融合+重排（端到端，含 LLM）"
    # 默认 pure：纯排序函数注入
    return _pure_ranking, "纯排序（召回+RRF 融合，无 LLM/无副作用）"


def evaluate(gold: Dict, top_k: int = EVAL_TOPK, mode: str = "fused",
              ranking: str = "pure") -> Dict:
    """对每个 query 跑评估对象，计算 NDCG/Hit/Recall/Diversity/MRR/MAP 并汇总均值。"""
    ranking_fn, src_desc = _ranking_fn(mode, ranking)
    per_query: List[Dict] = []
    for item in gold.get("queries", []):
        q = item["query"]
        relevant = item.get("relevant", [])
        try:
            cands = ranking_fn(q, top_k)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] query={q!r} 评估对象运行失败，跳过: {e}", file=sys.stderr)
            continue
        ranked_ids = [c.id for c in cands]
        div = metrics.diversity_at_k(cands, 10)
        per_query.append({
            "query": q,
            "note": item.get("note", ""),
            "intent": item.get("intent", ""),
            "ndcg@5": metrics.ndcg_at_k(ranked_ids, set(relevant), 5),
            "ndcg@10": metrics.ndcg_at_k(ranked_ids, set(relevant), 10),
            "hit@5": metrics.hit_rate_at_k(ranked_ids, set(relevant), 5),
            "hit@10": metrics.hit_rate_at_k(ranked_ids, set(relevant), 10),
            "recall@10": metrics.recall_at_k(ranked_ids, set(relevant), 10),
            "mrr@10": metrics.mrr_at_k(ranked_ids, set(relevant), 10),
            "map@10": metrics.map_at_k(ranked_ids, set(relevant), 10),
            "fp@10": metrics.first_relevant_position(ranked_ids, set(relevant), 10),
            # diversity 可能为 None（无类型信息），下游聚合时剔除
            "diversity@10": div,
        })

    n = len(per_query) or 1
    # P2-22：diversity 在无类型信息时返回 None，不能当作 1.0 计入均值，需剔除
    div_vals = [x["diversity@10"] for x in per_query if x["diversity@10"] is not None]
    agg = {
        "ndcg@5": sum(x["ndcg@5"] for x in per_query) / n,
        "ndcg@10": sum(x["ndcg@10"] for x in per_query) / n,
        "hit@5": sum(x["hit@5"] for x in per_query) / n,
        "hit@10": sum(x["hit@10"] for x in per_query) / n,
        "recall@10": sum(x["recall@10"] for x in per_query) / n,
        "mrr@10": sum(x["mrr@10"] for x in per_query) / n,
        "map@10": sum(x["map@10"] for x in per_query) / n,
        "fp@10": sum(x["fp@10"] for x in per_query) / n,
        "diversity@10": (sum(div_vals) / len(div_vals)) if div_vals else None,
        "n_queries": len(per_query),
        "n_diversity": len(div_vals),
    }
    return {"aggregate": agg, "per_query": per_query, "mode": mode, "mode_desc": src_desc}


def _main():
    ap = argparse.ArgumentParser(description="影片推荐离线评估（NDCG/命中/多样）")
    ap.add_argument("--gold", help="gold 标注 JSON 路径（query->相关id）")
    ap.add_argument("--topk", type=int, default=EVAL_TOPK, help="评估排序长度 k")
    ap.add_argument("--mode", choices=["fused", "recall"], default="fused",
                    help="评估对象：fused=融合排序(默认) / recall=content_recall 单路")
    ap.add_argument("--ranking", choices=["pure", "agent"], default="pure",
                    help="fused 模式排序来源：pure=纯排序函数(无LLM/无副作用，默认) / "
                         "agent=完整 Agent 图(含LLM精排，需 LLM+向量库)")
    ap.add_argument("--write-sample", help="把内置示例 gold 写成文件后退出")
    args = ap.parse_args()

    if args.write_sample:
        # 演示性 proxy gold（仅验证脚本可跑通，非真实标注）
        out = {"queries": []}
        for q in ["太空科幻 孤独宇航员", "诺兰导演 烧脑悬疑", "宫崎骏 动画 治愈",
                  "黑色幽默 犯罪 反转", "爱情 青春 校园"]:
            try:
                cands = content_recall(query=q, top_k=SAMPLE_TOPN)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] query={q!r} 召回失败，跳过: {e}", file=sys.stderr)
                continue
            out["queries"].append({"query": q, "relevant": [c.id for c in cands],
                                   "note": "proxy：content_recall 前 N（演示用）"})
        with open(args.write_sample, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"已写入示例 gold：{args.write_sample}（{len(out['queries'])} 条，proxy 仅供演示）")
        return

    if args.gold:
        gold = load_gold(args.gold)
        src = args.gold
    else:
        print("[warn] 未指定 --gold，使用内置 proxy 示例（非真实标注）")
        gold = {"queries": []}
        for q in ["太空科幻 孤独宇航员", "诺兰导演 烧脑悬疑", "宫崎骏 动画 治愈"]:
            try:
                cands = content_recall(query=q, top_k=SAMPLE_TOPN)
                gold["queries"].append({"query": q, "relevant": [c.id for c in cands],
                                        "note": "proxy"})
            except Exception:
                pass
        src = "内置 proxy 示例（非真实标注）"

    result = evaluate(gold, top_k=args.topk, mode=args.mode, ranking=args.ranking)
    print(f"评估来源：{src}")
    print(f"评估对象：{result['mode_desc']}")
    print(f"查询数：{result['aggregate']['n_queries']}  top_k={args.topk}")
    print("── 汇总（均值） ──")
    agg = result["aggregate"]
    for k, v in agg.items():
        if k == "n_queries":
            continue
        if k == "diversity@10" and v is None:
            print(f"  {k:<14}: N/A（无类型信息，已剔除 {agg['n_diversity']} 项之外）")
            continue
        print(f"  {k:<14}: {v:.4f}")
    print("── 逐查询 ──")
    for x in result["per_query"]:
        div = "N/A" if x["diversity@10"] is None else f"{x['diversity@10']:.3f}"
        print(
            f"  {x['query']:<16} "
            f"NDCG@10={x['ndcg@10']:.3f} Hit@10={x['hit@10']:.2f} "
            f"Recall@10={x['recall@10']:.2f} MRR@10={x['mrr@10']:.2f} "
            f"MAP@10={x['map@10']:.2f} FP@10={x['fp@10']} Div@10={div}"
        )


if __name__ == "__main__":
    _main()
