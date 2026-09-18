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
             "rating", "rating_count", "doc", "recall_source",
             "douban_id", "imdb_id")}  # 外链 id 需要跨融合保留


def _quality_pref(c: Candidate, user_genres: list[str] | None) -> tuple[float, float]:
    """质量分(rating/10*0.2) 与 偏好分(与 user_genres 重叠，每条 +0.15 封顶 0.3)。"""
    quality = (c.rating / 10.0) * 0.2 if c.rating else 0.0
    pref = 0.0
    if user_genres and c.genres:
        overlap = len(set(user_genres) & set(c.genres))
        pref = min(overlap * 0.15, 0.3)
    return quality, pref


# 回灌强度（与 agent/feedback_store 对称；集中在此便于调参）
GENRE_AFFINITY_SCALE = 0.3


def _feedback_term(c: Candidate, profile: dict | None) -> float:
    """O11 反馈调权项：基于用户反馈画像对单个 candidate 加分/降权。

    - item 在黑名单（踩过）→ 强降权，直接返回（沉底）。
    - item 在加分集（赞过/点过）→ 加分。
    - 类型亲和度：与候选类型重叠度加权（叠加在自述 user_genres 偏好之上）。
    命中黑名单直接返回，避免被后续加分抵消。
    """
    if not profile:
        return 0.0
    aid = c.id
    black = profile.get("item_blacklist") or {}
    if aid in black:
        return float(black[aid])
    t = 0.0
    boost = profile.get("item_boost") or {}
    if aid in boost:
        t += float(boost[aid])
    ga = profile.get("genre_affinity") or {}
    if ga and c.genres:
        t += sum(ga.get(g, 0.0) for g in c.genres) * GENRE_AFFINITY_SCALE
    return t


def fuse(
    recall: dict[str, list[Candidate]],
    weights: dict | None = None,
    user_genres: list[str] | None = None,
    top_k: int | None = None,
    explain: bool = False,
    feedback_profile: dict | None = None,
) -> list[Candidate] | tuple[list[Candidate], dict]:
    """把多路召回按来源加权融合，叠加质量分与偏好分，返回按综合分降序的列表。

    - 每路召回分数先 min-max 归一化到 [0,1]，避免量级差异（相似度 vs 关联度 vs 榜单分）。
    - 质量分：rating/10 × 0.2（IMDb 0、或低分作品不爆雷）。
    - 偏好分：与 user_genres 重叠每个 +0.15，封顶 0.3。
    - 跨源同一作品(id)合并去重，取最高综合分并累加召回来源标记。

    explain=True 时返回 (ranked_list, explain_map)，explain_map[id] 含各路归一化分、
    质量/偏好分、来源集合与最终综合分，供 P4 步骤可视化「各路排名来源」。
    """
    w = weights or _DEFAULT_WEIGHTS
    norm: dict[str, dict[float, float]] = {}
    for src, cands in recall.items():
        norm[src] = _minmax([c.score for c in cands])

    merged: dict[str, Candidate] = {}
    emap: dict[str, dict] = {}
    for src, cands in recall.items():
        src_w = w.get(src, 1.0)
        for c in cands:
            base = src_w * norm[src].get(c.score, 0.5)
            quality, pref = _quality_pref(c, user_genres)
            total = base + quality + pref + _feedback_term(c, feedback_profile)
            if c.id in merged:
                merged[c.id].score = max(merged[c.id].score, total)
                if c.recall_source and c.recall_source not in merged[c.id].recall_source:
                    merged[c.id].recall_source = f"{merged[c.id].recall_source}|{c.recall_source}"
                em = emap[c.id]
                if src not in em["sources"]:
                    em["sources"].append(src)
                em["base_by_src"][src] = round(base, 4)
                if total > em["best_total"]:
                    em["best_total"] = total
            else:
                merged[c.id] = Candidate(**{**_as_dict(c), "score": total})
                emap[c.id] = {
                    "sources": [src] if src else [],
                    "base_by_src": {src: round(base, 4)} if src else {},
                    "quality": round(quality, 4),
                    "pref": round(pref, 4),
                    "best_total": total,
                    "strategy": "score",
                }

    ranked = sorted(merged.values(), key=lambda x: x.score, reverse=True)
    if top_k:
        ranked = ranked[:top_k]
    for i, c in enumerate(ranked, 1):
        em = emap.get(c.id)
        if em:
            em["rank"] = i
            em["final"] = round(c.score, 4)
    if explain:
        return ranked, emap
    return ranked


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
