"""可选：本地 LIGHT 模型 pointwise 相关性重排（开发期可用，验证探针默认不跑）。"""
from retrieval.candidates import Candidate
from llm.factory import get_light

_PROMPT = (
    "你是电影推荐的相关性评判。给用户查询与候选影片信息，"
    "只输出 0-1 之间的小数（越接近1越相关），不要任何解释。\n"
    "查询：{query}\n影片：{title}（{year}，类型：{genres}）\n相关性分数："
)


def _cand_dict(c: Candidate) -> dict:
    return {k: getattr(c, k) for k in
            ("id", "source", "title", "year", "genres", "country",
             "rating", "rating_count", "doc", "recall_source",
             "douban_id", "imdb_id", "graph_paths")}  # 外链 id 需要跨精排保留


def llm_rerank(
    cands: list[Candidate],
    query: str | None,
    top_k: int | None = None,
) -> list[Candidate]:
    """用本地 LIGHT 模型对每条候选打 0-1 相关性分，与融合分加权(0.7/0.3)后重排。"""
    if not query:
        cands = sorted(cands, key=lambda c: c.score, reverse=True)
        return cands[:top_k] if top_k else cands
    llm = get_light()
    scored: list[Candidate] = []
    for c in cands:
        try:
            txt = llm.invoke(_PROMPT.format(
                query=query, title=c.title, year=c.year,
                genres="/".join(c.genres),
            )).content
            digits = "".join(ch for ch in txt if ch.isdigit() or ch == ".")
            s = float(digits or "0")
            s = max(0.0, min(1.0, s))
        except Exception:
            s = c.score
        nc = Candidate(**{**_cand_dict(c), "score": 0.7 * c.score + 0.3 * s})
        scored.append(nc)
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:top_k] if top_k else scored
