"""RRF（Reciprocal Rank Fusion）融合：基于排名位次的召回融合。

相比 scorer.fuse 的 min-max 归一化「分数」融合，RRF 直接用各路召回的「排名位次」融合：
    score = Σ_src  w_src / (K + rank_src(item))
因此对三路量级天然免疫（向量相似度~[0,1]、同剧组累加无上界、质量×热度大数），
且对后续新增召回路（Neo4j/协同过滤等）零成本接入——只要能给出排名即可。

融合后再叠加与原 fuse 一致的质量分/偏好分，使融合信号与「高分/偏好优先」同日而语，
并最终仍过 MMR / 可选 LLM 精排（见 rerank）。v2.14 由 config.FUSION_STRATEGY 切换。
"""
from retrieval.candidates import Candidate, merge_candidate, sort_by_score, work_key
from ranker.scorer import _as_dict, _quality_pref, _feedback_term

# 顶层召回 dict 的 key（graph 规划名）映射到规范来源权重键，
# 以修正原 score 路径「顶层 key 全回退等权 1.0」的隐性失效。
# P2-8 起 search_similar 的三路以原始列表直接展开进顶层 recall（content/
# relation/graph 各自权重生效），不再需要 "similar"→"content" 的整包映射；
# 该映射保留仅为兼容历史调用方。
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


def _canon_key(k: str, mapping: dict) -> str:
    return mapping.get(k, k)


def rrf_fuse(
    recall: dict[str, list[Candidate]],
    weights: dict | None = None,
    k: int = 60,
    top_k: int | None = None,
    user_genres: list[str] | None = None,
    key_to_canon: dict | None = None,
    explain: bool = False,
    feedback_profile: dict | None = None,
) -> list[Candidate] | tuple[list[Candidate], dict]:
    """按排名位次做 RRF 融合，再叠加质量分/偏好分，返回综合分降序列表。

    - 每路召回按其自身顺序定 rank（1-based）；单路 RRF 分 = w_src / (k + rank)。
    - 跨源同 id 累加 RRF 分（多路印证 → 更高）。
    - 归一化到 [0,1]（除以最大 RRF 分）后与质量分/偏好分相加（与原 fuse 一致）。
    - recall_source 跨源合并标记（content|relation|coldstart...）。

    explain=True 时返回 (ranked_list, explain_map)：explain_map[id] 含各路排名位次(ranks)、
    RRF 原始/归一化分、质量/偏好分、来源集合，供 P4 步骤可视化「各路排名来源」。
    """
    w = weights or {}
    ktc = key_to_canon or _PLAN_TO_CANON

    def _eff(src_key: str) -> float:
        canon = _canon_key(src_key, ktc)
        return float(w.get(canon, w.get(src_key, 1.0)))

    rrf: dict[str, float] = {}
    sources: dict[str, list[str]] = {}
    best: dict[str, Candidate] = {}      # wk -> 代表候选
    wkid: dict[str, str] = {}            # wk -> 代表候选 id（跨源归并后可能变化）
    ranks: dict[str, dict[str, int]] = {}

    for src_key, cands in recall.items():
        w_src = _eff(src_key)
        for rank, c in enumerate(cands, start=1):
            if not c.id:
                continue
            wk = work_key(c)
            rrf[wk] = rrf.get(wk, 0.0) + w_src / (k + rank)
            r = ranks.setdefault(wk, {})
            r[src_key] = min(r.get(src_key, rank), rank) if src_key in r else rank
            if wk not in best:
                best[wk] = c
                wkid[wk] = c.id
            else:
                rep = best[wk]
                old_id = rep.id
                merge_candidate(rep, c)   # 合并双源外链 id / 异名，展示字段择优
                if rep.id != old_id:
                    wkid[wk] = rep.id
            rs = c.recall_source or ""
            if rs and rs not in sources.get(wk, []):
                sources.setdefault(wk, []).append(rs)

    if not rrf:
        return [] if not explain else ([], {})

    max_rrf = max(rrf.values())
    merged: dict[str, Candidate] = {}
    emap: dict[str, dict] = {}
    for wk, raw in rrf.items():
        base = (raw / max_rrf) if max_rrf else 0.0   # 归一化到 [0,1]
        cid = wkid.get(wk, wk)
        c = best[wk]
        quality, pref = _quality_pref(c, user_genres)
        d = _as_dict(c)
        d["score"] = base + quality + pref + _feedback_term(c, feedback_profile)
        d["recall_source"] = "|".join(sources.get(wk, [])) or (c.recall_source or "")
        merged[cid] = Candidate(**d)
        emap[cid] = {
            "sources": sources.get(wk, []),
            "ranks": ranks.get(wk, {}),
            "rrf_raw": round(raw, 6),
            "rrf_norm": round(base, 4),
            "quality": round(quality, 4),
            "pref": round(pref, 4),
            "final": round(d["score"], 4),
            "strategy": "rrf",
        }

    ranked = sort_by_score(list(merged.values()))
    if top_k:
        ranked = ranked[:top_k]
    for i, c in enumerate(ranked, 1):
        em = emap.get(c.id)
        if em:
            em["rank"] = i
    if explain:
        return ranked, emap
    return ranked
