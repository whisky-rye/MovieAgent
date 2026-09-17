"""O9/D3：融合排序评估对象——直接跑 Agent 图的「完整融合 + 重排」结果作为评估对象。

与仅用 content_recall（单一召回路径）不同，融合排序覆盖：
  内容向量召回 + 关系召回 + 冷启动兜底 → RRF/score 融合 → MMR 多样性 → LLM 精排
即端到端推荐链路的最终排序，最能反映真实推荐质量。

run_fused_ranking 返回 graph state 的 `ranked`（list[Candidate]），与线上 /recommend
产出的 items 同源。agent 可注入，便于离线单测（无需真实 LLM/向量库）。
"""
from typing import Callable, List, Optional

from retrieval.candidates import Candidate


def _build_agent():
    """延迟构建 Agent 图（避免模块导入即拉起重依赖）。"""
    from agent.graph import build_agent

    return build_agent()


# ranking_fn(query, top_k) -> List[Candidate]
RankingFn = Callable[[str, int], List[Candidate]]


def run_fused_ranking(
    query: str,
    top_k: int = 20,
    user_id: str = "eval",
    agent=None,
) -> List[Candidate]:
    """跑完整 Agent 图，返回融合重排后的候选列表（state['ranked']）。

    - agent=None 时自行 build_agent()；注入 agent 用于测试/复用已编译图。
    - 输入与 api/main.py 的 _run 一致（user_id/query/request_id/history/steps/thinking），
      但不走护栏/限流/历史压缩，保持纯评估语义。
    """
    app = agent or _build_agent()
    result = app.invoke(
        {
            "user_id": user_id,
            "query": query,
            "request_id": "",
            "history": "",
            "steps": [],
            "thinking": {},
        }
    )
    ranked = result.get("ranked", []) or []
    return ranked[:top_k]
