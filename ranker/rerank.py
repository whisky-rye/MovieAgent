"""重排：融合（RRF / 原 score）→ 可选 MMR 多样性 → 可选 LLM pointwise 精排。

P2（二级 pipeline）：融合之后的重排链可组合为
    [融合] → [质量/偏好 boost（融合内已完成）] → [MMR 多样性] → [LLM 精排（仅前 N）]
其中 MMR 与 LLM 精排均为可选阶段，由显式参数或兼容旧用法的 strategy 决定。

P3（配置化）：RRF 权重 / MMR 开关与强度 / LLM 精排名额均来自 config.settings，
无需改业务代码即可调参。

P4（可解释）：explain=True 时返回 {"ranked": [...], "fusion_detail": {...}}，
fusion_detail 含每片「各路排名来源与贡献」，供 graph 步骤可视化。
"""
import logging
from typing import Optional
from retrieval.candidates import Candidate
from config.settings import settings
from ranker.scorer import fuse, mmr_diversify
from ranker.rrf import rrf_fuse
from ranker.llm_rerank import llm_rerank
from ranker.cloud_rerank import cloud_rerank

logger = logging.getLogger("movie_agent.ranker.rerank")


def rerank(
    recall: dict[str, list[Candidate]] | list[Candidate],
    strategy: str = "score",
    top_k: int | None = None,
    user_genres: list[str] | None = None,
    lambda_div: float | None = None,
    query: Optional[str] = None,
    feedback_profile: dict | None = None,
    # —— P2 二级 pipeline 显式开关（优先级高于 strategy 兼容映射）——
    diversify: bool | None = None,
    llm_topn: int | None = None,
    # —— 精排实现选择（新接入点）：local=原本地 LIGHT 精排（旧逻辑）| cloud=阿里百炼 qwen3.7-text-rerank ——
    rerank_provider: str | None = None,
    # —— P4 可解释 ——
    explain: bool = False,
) -> list[Candidate] | dict:
    """统一重排入口。

    strategy 兼容旧用法（决定二级 pipeline 的默认行为）：
      'score'     : 仅融合（默认，最快，无需 LLM）
      'mmr'      : 融合 + MMR 多样性重排          （等价 diversify=True）
      'llm'      : 融合 + LLM pointwise 精排（对前 top_k 做）
      'mmr+llm'  : 融合 + MMR + LLM 精排           （完整二级 pipeline）

    融合策略由 config.FUSION_STRATEGY 控制（'rrf' 默认 / 'score' 回退）。

    显式参数 diversify / llm_topn 一旦给出（非 None）即覆盖 strategy 的默认映射，
    便于 graph 直接按配置驱动：diversify=settings.RERANK_DIVERSIFY，
    llm_topn=settings.RERANK_LLM_TOPN。

    explain=True 返回 dict {"ranked": list, "fusion_detail": dict}；
    否则返回 list[Candidate]（向后兼容）。
    """
    lambda_div = settings.MMR_LAMBDA if lambda_div is None else lambda_div

    cands: list[Candidate] = []
    fusion_detail: dict = {}
    if isinstance(recall, dict):
        if settings.FUSION_STRATEGY == "rrf":
            if explain:
                cands, fusion_detail = rrf_fuse(
                    recall, user_genres=user_genres, k=settings.RRF_K,
                    weights=settings.RRF_WEIGHTS_DICT, explain=True,
                    feedback_profile=feedback_profile,
                )
            else:
                cands = rrf_fuse(
                    recall, user_genres=user_genres, k=settings.RRF_K,
                    weights=settings.RRF_WEIGHTS_DICT,
                    feedback_profile=feedback_profile,
                )
        else:
            if explain:
                cands, fusion_detail = fuse(
                    recall, user_genres=user_genres,
                    weights=settings.RRF_WEIGHTS_DICT, explain=True,
                    feedback_profile=feedback_profile,
                )
            else:
                cands = fuse(
                    recall, user_genres=user_genres,
                    weights=settings.RRF_WEIGHTS_DICT,
                    feedback_profile=feedback_profile,
                )
    else:
        cands = recall

    # 二级 pipeline 开关解析：显式参数优先，否则由 strategy 映射
    if diversify is None:
        diversify = strategy in ("mmr", "mmr+llm")
    if llm_topn is None:
        llm_topn = top_k if strategy in ("llm", "mmr+llm") else 0

    # 阶段一：MMR 多样性重排（避免清一色同类型）
    if diversify:
        cands = mmr_diversify(cands, lambda_div=lambda_div, top_k=top_k)

    # 阶段二：可选精排（仅对前 llm_topn 做，控成本）
    #   RERANK_PROVIDER=cloud → 阿里百炼 qwen3.7-text-rerank 重排序模型（新接入点）
    #   RERANK_PROVIDER=local  → 本地 LIGHT pointwise 精排（旧逻辑，默认）
    #   云端调用失败时自动回退旧逻辑，保证请求不中断、可观测。
    if llm_topn and llm_topn > 0:
        if query:
            head = cands[:llm_topn]
            tail = cands[llm_topn:]
            provider = rerank_provider or settings.RERANK_PROVIDER
            if provider == "cloud":
                try:
                    cands = cloud_rerank(head, query, top_k=llm_topn) + tail
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "云端重排序(cloud)失败，回退本地 LIGHT 旧逻辑: %s", e
                    )
                    cands = llm_rerank(head, query, top_k=llm_topn) + tail
            else:
                cands = llm_rerank(head, query, top_k=llm_topn) + tail
        else:
            # 无 query 时精排退化为按融合分排序（与 llm_rerank 内部一致）
            cands = sorted(cands, key=lambda x: x.score, reverse=True)

    # 最终按 top_k 收口（MMR 已尊重 top_k；此处为 LLM 阶段后的兜底裁剪）
    if top_k:
        cands = cands[:top_k]

    if explain:
        return {"ranked": cands, "fusion_detail": fusion_detail}
    return cands
