"""可选：本地 LIGHT 模型 pointwise 相关性重排（开发期可用，验证探针默认不跑）。

P2-2：此前每次 llm_rerank 都 get_light() 新建客户端，且 for 循环逐条串行
invoke——cloud 不可用回退本地时一次请求 30 次串行 LLM 调用（每次还带客户端
构建开销）。现改为：
- 客户端进程级复用（_get_llm，双检锁；测试可用 _reset_llm 清空）；
- 候选打分线程池并发（LLM 调用为 IO bound，Ollama/云端均可并发吞吐），
  单条失败仍回退该候选融合分，不拖垮整批。
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from retrieval.candidates import Candidate, sort_by_score
from llm.factory import get_light
from ranker.scorer import _with_position_prior

_PROMPT = (
    "你是电影推荐的相关性评判。给用户查询与候选影片信息，"
    "只输出 0-1 之间的小数（越接近1越相关），不要任何解释。\n"
    "查询：{query}\n影片：{title}（{year}，类型：{genres}）\n相关性分数："
)

# 并发打分上限：精排只对前 RERANK_LLM_TOPN 名做（默认 30），8 路并发足够
_MAX_WORKERS = 8

_LLM = None
_LLM_LOCK = threading.Lock()


def _cand_dict(c: Candidate) -> dict:
    return {k: getattr(c, k) for k in
            ("id", "source", "title", "year", "genres", "country",
             "rating", "rating_count", "doc", "recall_source",
             "douban_id", "imdb_id", "graph_paths", "title_alt")}  # 外链 id / 异名 需要跨精排保留


def _get_llm():
    """精排客户端进程级复用（P2-2）。"""
    global _LLM
    with _LLM_LOCK:
        if _LLM is None:
            _LLM = get_light()
        return _LLM


def _reset_llm() -> None:
    """清空客户端单例（测试/配置热切换用）。"""
    global _LLM
    with _LLM_LOCK:
        _LLM = None


def _score_one(llm, query: str, c: Candidate) -> float:
    """单条候选打分；任何失败回退融合分（不拖垮整批）。"""
    try:
        txt = llm.invoke(_PROMPT.format(
            query=query, title=c.title, year=c.year,
            genres="/".join(c.genres),
        )).content
        digits = "".join(ch for ch in txt if ch.isdigit() or ch == ".")
        s = float(digits or "0")
        return max(0.0, min(1.0, s))
    except Exception:
        return c.score


def llm_rerank(
    cands: list[Candidate],
    query: str | None,
    top_k: int | None = None,
    positions: dict | None = None,
) -> list[Candidate]:
    """用本地 LIGHT 模型对每条候选打 0-1 相关性分，与融合分加权(0.7/0.3)后重排。

    P2-2：打分并发执行（见模块头）；结果按候选原位映射，排序语义不变。
    P2-7：positions（{id: MMR 名次}）作为位置先验叠加进融合分——精排是相关性
    「微调」而非重排：相关性差距大于先验差时仍按相关性排序，接近持平时保持
    MMR 已取得的多样性次序（见 ranker.scorer._with_position_prior）。
    """
    if not query:
        cands = sort_by_score(cands)
        return cands[:top_k] if top_k else cands
    llm = _get_llm()
    if len(cands) > 1:
        workers = min(_MAX_WORKERS, len(cands))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            scores = list(ex.map(lambda c: _score_one(llm, query, c), cands))
    else:
        scores = [_score_one(llm, query, c) for c in cands]
    scored = [
        Candidate(**{**_cand_dict(c),
                     "score": _with_position_prior(0.7 * c.score + 0.3 * s,
                                                   c.id, positions)})
        for c, s in zip(cands, scores)
    ]
    scored = sort_by_score(scored)
    return scored[:top_k] if top_k else scored
