"""排序打分：多路召回融合 + 质量/偏好加权 + MMR 多样性。"""
from typing import Optional
from retrieval.candidates import Candidate

# 各召回来源的默认融合权重（内容相似最高，关系次之，冷启动兜底最低）
_DEFAULT_WEIGHTS = {"content": 1.0, "relation": 0.8, "coldstart": 0.6}


def _minmax(vals: list[float]) -> dict[float, float]:
    if not vals:
        return {}
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return {v: 0.5 for v in set(vals)}
    return {v: (v - lo) / (hi - lo) for v in set(vals)}


def _as_dict(c: Candidate) -> dict:
    return {k: getattr(c, k) for k in
            ("id", "source", "title", "year", "genres", "country",
             "rating", "rating_count", "doc", "recall_source")}


def fuse(
    recall: dict[str, list[Candidate]],
    weights: dict | None = None,
    user_genres: list[str] | None = None,
    top_k: int | None = None,
) -> list[Candidate]:
    """把多路召回按来源加权融合，叠加质量分与偏好分，返回按综合分降序的列表。

    - 每路召回分数先 min-max 归一化到 [0,1]，避免量级差异（相似度 vs 关联度 vs 榜单分）。
    - 质量分：rating/10 × 0.2（IMDb 0、或低分作品不爆雷）。
    - 偏好分：与 user_genres 重叠每个 +0.15，封顶 0.3。
    - 跨源同一作品(id)合并去重，取最高综合分并累加召回来源标记。
    """
    w = weights or _DEFAULT_WEIGHTS
    norm: dict[str, dict[float, float]] = {}
    for src, cands in recall.items():
        norm[src] = _minmax([c.score for c in cands])

    merged: dict[str, Candidate] = {}
    for src, cands in recall.items():
        src_w = w.get(src, 1.0)
        for c in cands:
            base = src_w * norm[src].get(c.score, 0.5)
            quality = (c.rating / 10.0) * 0.2 if c.rating else 0.0
            pref = 0.0
            if user_genres and c.genres:
                overlap = len(set(user_genres) & set(c.genres))
                pref = min(overlap * 0.15, 0.3)
            total = base + quality + pref
            if c.id in merged:
                merged[c.id].score = max(merged[c.id].score, total)
                if c.recall_source not in merged[c.id].recall_source:
                    merged[c.id].recall_source = f"{merged[c.id].recall_source}|{c.recall_source}"
            else:
                merged[c.id] = Candidate(**{**_as_dict(c), "score": total})

    ranked = sorted(merged.values(), key=lambda x: x.score, reverse=True)
    return ranked[:top_k] if top_k else ranked


def _genre_overlap(a: Candidate, b: Candidate) -> float:
    if not a.genres or not b.genres:
        return 0.0
    inter = len(set(a.genres) & set(b.genres))
    uni = len(set(a.genres) | set(b.genres))
    return inter / uni if uni else 0.0


def mmr_diversify(
    cands: list[Candidate],
    lambda_div: float = 0.3,
    top_k: int | None = None,
) -> list[Candidate]:
    """以类型重叠作为距离代理的 MMR 重排，提升结果多样性（避免清一色同类型）。"""
    if not cands:
        return []
    selected: list[Candidate] = []
    remain = list(cands)
    while remain and (top_k is None or len(selected) < top_k):
        best, best_score = None, -1e9
        for c in remain:
            sim = max((_genre_overlap(c, s) for s in selected), default=0.0)
            mmr = (1 - lambda_div) * c.score - lambda_div * sim
            if mmr > best_score:
                best, best_score = c, mmr
        selected.append(best)
        remain.remove(best)
    return selected
