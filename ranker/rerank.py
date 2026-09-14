"""重排：融合分排序 / MMR 多样性 / 可选 LLM pointwise 重排。"""
from typing import Optional
from retrieval.candidates import Candidate
from config.settings import settings
from ranker.scorer import fuse, mmr_diversify
from ranker.rrf import rrf_fuse
from ranker.llm_rerank import llm_rerank


def rerank(
    recall: dict[str, list[Candidate]] | list[Candidate],
    strategy: str = "score",
    top_k: int | None = None,
    user_genres: list[str] | None = None,
    lambda_div: float = 0.3,
    query: Optional[str] = None,
) -> list[Candidate]:
    """统一重排入口。

    strategy:
      'score' : 多路召回融合分降序（默认，最快，无需 LLM）
      'mmr'   : 在 'score' 融合基础上做 MMR 多样性重排
      'llm'   : 额外用本地 LIGHT 模型对 query 做 pointwise 相关性打分再排序（需 query）

    融合策略由 config.FUSION_STRATEGY 控制：
      'rrf'  （默认）：基于排名位次的 RRF 融合，免疫各路分数量级差异
      'score'        ：原 min-max 归一化加权融合（向后兼容回退）
    """
    cands: list[Candidate]
    if isinstance(recall, dict):
        if settings.FUSION_STRATEGY == "rrf":
            cands = rrf_fuse(recall, user_genres=user_genres, k=settings.RRF_K)
        else:
            cands = fuse(recall, user_genres=user_genres)
    else:
        cands = recall

    if strategy == "mmr":
        return mmr_diversify(cands, lambda_div=lambda_div, top_k=top_k)
    if strategy == "llm":
        return llm_rerank(cands, query, top_k=top_k)

    cands = sorted(cands, key=lambda c: c.score, reverse=True)
    return cands[:top_k] if top_k else cands
