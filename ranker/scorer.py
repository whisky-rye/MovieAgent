"""排序打分：多路召回融合 + 质量/偏好加权 + MMR 多样性。"""
from typing import Optional
from retrieval.candidates import Candidate, merge_candidate, sort_by_score, work_key

# 各召回来源的默认融合权重（内容相似最高，关系次之，冷启动兜底最低）
_DEFAULT_WEIGHTS = {"content": 1.0, "relation": 0.8, "coldstart": 0.6}

# 顶层召回 dict 的 key（graph 规划名）→ 规范来源权重键。
# P2-9：RRF 与 score 两条融合路径都必须做此规范化——此前只有 rrf_fuse 做，
# fuse（score 回退路径）直接用规划名查 RRF_WEIGHTS_DICT 全部落 1.0，
# 「向后兼容回退」实际把所有权重配置静默丢掉。
_PLAN_TO_CANON = {
    "search_by_text": "content",
    "search_similar": "content",   # 历史兼容：旧版 similar 为内层融合整包
    "similar": "content",          # 历史兼容：同上
    "search_graph": "graph",       # 知识图谱多跳召回（F1/O12）
    "search_by_person": "person",  # 按导演/演员找作品（实体中心召回）
    "search_prod": "prod",         # 生产库只读通道补充召回（E4/MCP，默认关闭）
    "high_quality_board": "coldstart",
    "coldstart_fallback": "coldstart",
}


def _canon_key(k: str, mapping: dict | None = None) -> str:
    """规划名 → 规范来源名（未映射的原样返回）。"""
    return (mapping or _PLAN_TO_CANON).get(k, k)


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
             "douban_id", "imdb_id", "graph_paths", "title_alt")}  # 外链 id / 异名 需要跨融合保留


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
# 反馈类型亲和度的总贡献上限（P2-6）：与自述偏好分（_quality_pref 的 pref
# 封顶 0.3）一致——多类型候选的亲和度累加此前无上限，一部片命中 5 个高亲和
# 类型时该项可达 1.5，足以压倒质量分/融合信号；封顶后反馈信号不越过用户
# 自述口径，且正负对称（过度踩的类型组合也不能把片沉到底）。
_GENRE_AFFINITY_MAX = 0.3


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
        raw = sum(ga.get(g, 0.0) for g in c.genres) * GENRE_AFFINITY_SCALE
        # P2-6：封顶（正负对称），见 _GENRE_AFFINITY_MAX 注释
        t += max(-_GENRE_AFFINITY_MAX, min(_GENRE_AFFINITY_MAX, raw))
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
    # P2-9：权重键与来源键都规范化到规范名（content/relation/coldstart/graph/
    # person/prod）——规划名（search_by_text/high_quality_board...）直接查
    # RRF_WEIGHTS_DICT 会全部落 1.0，score 回退路径的权重配置曾因此静默失效。
    w = {_canon_key(k): float(v) for k, v in (weights or _DEFAULT_WEIGHTS).items()}
    norm: dict[str, dict[float, float]] = {}
    for src, cands in recall.items():
        norm[src] = _minmax([c.score for c in cands])

    merged: dict[str, Candidate] = {}
    wkid: dict[str, str] = {}        # work_key -> 代表候选 id（跨源归并后可能变化）
    emap: dict[str, dict] = {}
    for src, cands in recall.items():
        src_w = w.get(_canon_key(src), 1.0)
        for c in cands:
            wk = work_key(c)
            base = src_w * norm[src].get(c.score, 0.5)
            quality, pref = _quality_pref(c, user_genres)
            total = base + quality + pref + _feedback_term(c, feedback_profile)
            if wk in merged:
                rep = merged[wk]
                # 跨源同作品：取最高综合分、合并来源标记、归并双源外链 id 与异名(title_alt)
                rep.score = max(rep.score, total)
                old_id = rep.id
                merge_candidate(rep, c)
                if rep.id != old_id:
                    em = emap.pop(old_id, None)
                    if em is not None:
                        emap[rep.id] = em
                wkid[wk] = rep.id
                if c.recall_source and c.recall_source not in rep.recall_source:
                    rep.recall_source = f"{rep.recall_source}|{c.recall_source}"
                em = emap[wkid[wk]]
                if src not in em["sources"]:
                    em["sources"].append(src)
                em["base_by_src"][src] = round(base, 4)
                if total > em["best_total"]:
                    em["best_total"] = total
            else:
                merged[wk] = Candidate(**{**_as_dict(c), "score": total})
                wkid[wk] = c.id
                emap[wkid[wk]] = {
                    "sources": [src] if src else [],
                    "base_by_src": {src: round(base, 4)} if src else {},
                    "quality": round(quality, 4),
                    "pref": round(pref, 4),
                    "best_total": total,
                    "strategy": "score",
                }

    ranked = sort_by_score(list(merged.values()))
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


# 多样性代理各维度权重：类型仍是主信号（原逻辑），年代/地区补足「同类型不同
# 代/不同产地」的多样性约束（P2-7）
_DIV_WEIGHTS = {"genre": 0.6, "year": 0.2, "country": 0.2}


def _div_features(c: Candidate) -> tuple:
    """候选的多样性特征（MMR 预计算用）：(类型集, 年份, 地区集)。"""
    cs = frozenset(
        x.strip() for x in (c.country or "").replace("|", "/").split("/") if x.strip()
    )
    return (frozenset(c.genres or ()), c.year or 0, cs)


def _sim_features(fa: tuple, fb: tuple) -> float:
    """特征级相似度（_diversity_distance 的预计算版，单一事实源）。"""
    ga, ya, ca = fa
    gb, yb, cb = fb
    s = 0.0
    if ga and gb:
        uni = len(ga | gb)
        s += _DIV_WEIGHTS["genre"] * (len(ga & gb) / uni if uni else 0.0)
    if ya and yb:
        s += _DIV_WEIGHTS["year"] * max(0.0, 1.0 - abs(ya - yb) / 30.0)
    if ca and cb and (ca & cb):
        s += _DIV_WEIGHTS["country"]
    return s


def _diversity_distance(a: Candidate, b: Candidate) -> float:
    """候选间相似度代理（MMR 的 sim 项）：类型/年代/地区三维归一加权。

    P2-7：原代理只有类型 Jaccard——年代/地区/卡司多样性无约束，结果可能是
    「同类型但同十年同产地」的一批片。缺信息维度不惩罚（返回 0，不假设不同），
    避免元数据稀疏的源被无谓推远。
    """
    return _sim_features(_div_features(a), _div_features(b))


# 位置先验（P2-7）：MMR 已把多样性/均衡性编码进名次，精排若纯按相关性重排会把
# 多样性成果抹掉。给按其名次加一个随名次指数衰减的小先验（第 1 名 1.0、最后
# 一名 0.5），使精排是「微调」而非「重排」：相关性差距大于先验差时仍按相关性，
# 接近持平时保持 MMR 次序。权重很小（_POSITION_PRIOR_W），只做并列保护。
_POSITION_PRIOR_W = 0.1


def _position_prior(pos: int, total: int) -> float:
    """MMR 名次先验：pos 为 1-based 名次，total 为总候选数；total<=1 恒 1.0。"""
    if total <= 1 or pos <= 1:
        return 1.0
    if pos >= total:
        return 0.5
    return 0.5 ** ((pos - 1) / (total - 1))


def _with_position_prior(base: float, cand_id: str, positions: dict | None) -> float:
    """给精排融合分叠加 MMR 位置先验（P2-7）。

    positions 为 {候选 id: 1-based MMR 名次}（rerank 在 MMR 之后构建）；缺失/
    空表示无上游次序信息（如未开 MMR），原样返回。先验差（首末名 0.05~0.1）
    远小于有实际意义的相关性差，只保护「相关性接近时保持 MMR 次序」。
    """
    if not positions:
        return base
    pos = positions.get(cand_id)
    if not pos:
        return base
    return base + _POSITION_PRIOR_W * _position_prior(pos, len(positions))


def mmr_diversify(
    cands: list[Candidate],
    lambda_div: float = 0.3,
    top_k: int | None = None,
) -> list[Candidate]:
    """MMR 多样性重排：相关性为主、相似度惩罚为辅，避免清一色同类型。

    实现要点：
    - P2-5：特征预计算 + 「已选标记」单遍扫描，替代 `remain.remove(best)`
      的每轮 O(n) 删除；选择语义严格保持（mmr 严格大于才换、并列取输入序
      在前者）；
    - P2-7：相似度代理从「仅类型 Jaccard」升级为类型/年代/地区三维加权
      （_diversity_distance）——年代/地区多样性从此有约束。
    """
    if not cands:
        return []
    feats = [_div_features(c) for c in cands]
    n = len(cands)
    limit = n if top_k is None else min(top_k, n)
    chosen = [False] * n
    selected: list[Candidate] = []
    sel_feats: list[tuple] = []   # 已选特征（含全零特征：对 max 贡献 0，无害）
    while len(selected) < limit:
        best_idx, best_score = -1, -1e9
        for i in range(n):
            if chosen[i]:
                continue
            sim = max((_sim_features(feats[i], f) for f in sel_feats), default=0.0)
            mmr = (1 - lambda_div) * cands[i].score - lambda_div * sim
            if mmr > best_score:
                best_idx, best_score = i, mmr
        if best_idx < 0:
            break
        chosen[best_idx] = True
        selected.append(cands[best_idx])
        sel_feats.append(feats[best_idx])
    return selected
