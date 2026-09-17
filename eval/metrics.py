"""O9/D3：离线评估指标体系（纯函数，可单测，无外部依赖）。

提供以 NDCG、命中率、多样性为核心的推荐质量指标：
- ndcg_at_k        : 归一化折损累计增益（考虑排序位置，graded relevance 友好）
- hit_rate_at_k    : 命中率（top-k 内是否出现任意相关项，二元，按 query 平均）
- recall_at_k      : 召回率（相关项中被召回的比例）
- diversity_at_k   : 列表内多样性（1 - 平均两两类型 Jaccard 重叠，越高越多样）

`ranked_ids` 为按推荐顺序排列的候选 id 列表；`relevant` 为相关 id 集合
（或 {id: 相关度} 字典以支持 graded relevance）。
"""
import math
from typing import Dict, Iterable, List, Set, Union


def _as_set(relevant) -> Set[str]:
    if isinstance(relevant, dict):
        return set(relevant.keys())
    return set(relevant or [])


def _relevance(ranked_ids: List[str], relevant, k: int) -> List[float]:
    """取 top-k 每位的关联度（graded 或二元）。"""
    graded = isinstance(relevant, dict)
    rel_set = _as_set(relevant)
    out: List[float] = []
    for i in ranked_ids[:k]:
        if graded:
            out.append(float(relevant.get(i, 0.0)))
        else:
            out.append(1.0 if i in rel_set else 0.0)
    return out


def dcg_at_k(rels: List[float], k: int) -> float:
    """DCG@k：位置折扣累计增益（log2 折扣，位置从 1 起）。"""
    s = 0.0
    for i, r in enumerate(rels[:k], 1):
        s += (2 ** r - 1) / math.log2(i + 1)
    return s


def ndcg_at_k(ranked_ids: List[str], relevant: Union[Set[str], Dict[str, float]],
              k: int) -> float:
    """NDCG@k：DCG / IDCG。无相关项时返回 0.0（避免除零）。"""
    rels = _relevance(ranked_ids, relevant, k)
    dcg = dcg_at_k(rels, k)
    if isinstance(relevant, dict):
        ideal = sorted(relevant.values(), reverse=True)[:k]
    else:
        n_rel = min(k, len(_as_set(relevant)))
        # 理想列表按 k 长度用 0 补齐：超出相关项数量的位置贡献为 0
        ideal = [1.0] * n_rel + [0.0] * (k - n_rel)
    idcg = dcg_at_k(ideal, k)
    if idcg <= 0.0:
        return 0.0
    return dcg / idcg


def hit_rate_at_k(ranked_ids: List[str], relevant: Iterable[str], k: int) -> float:
    """命中率（二元）：top-k 内出现任意相关项记 1.0，否则 0.0。"""
    rel_set = _as_set(relevant)
    if not rel_set:
        return 0.0
    return 1.0 if rel_set & set(ranked_ids[:k]) else 0.0


def recall_at_k(ranked_ids: List[str], relevant: Iterable[str], k: int) -> float:
    """召回率：top-k 内相关项数量 / 相关项总数。"""
    rel_set = _as_set(relevant)
    if not rel_set:
        return 0.0
    return len(rel_set & set(ranked_ids[:k])) / len(rel_set)


def _genres_of(item) -> List[str]:
    """候选可能是 dict（含 genres）或 Candidate 对象。"""
    if isinstance(item, dict):
        g = item.get("genres") or []
    else:
        g = getattr(item, "genres", None) or []
    return list(g)


def diversity_at_k(items: List, k: int, key: str = "genres") -> float:
    """列表内多样性：1 - 平均两两类型 Jaccard 重叠（越高越多样，[0,1]）。

    - 不足 2 项或无类型信息时返回 1.0（视为无重复、最多样）。
    - 单对重叠 = |A∩B| / |A∪B|（两者皆空按 0 重叠计）。
    """
    sub = items[:k]
    if len(sub) < 2:
        return 1.0
    sets = [set(_genres_of(it)) for it in sub]
    if all(len(s) == 0 for s in sets):
        return 1.0
    overlap_sum = 0.0
    pairs = 0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            a, b = sets[i], sets[j]
            union = a | b
            ov = (len(a & b) / len(union)) if union else 0.0
            overlap_sum += ov
            pairs += 1
    if pairs == 0:
        return 1.0
    return 1.0 - (overlap_sum / pairs)
