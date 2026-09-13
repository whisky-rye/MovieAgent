"""RAG 可解释输出：把召回候选拼成上下文，用强档 LLM 生成自然语言解说。"""
from langchain_core.messages import HumanMessage, SystemMessage
from retrieval.candidates import Candidate
from llm.client import chat_strong, strip_think
from agent.prompt import EXPLAIN_PROMPT


def build_context(ranked: list[Candidate], top_n: int = 8) -> str:
    """把候选拼成可喂给 LLM 的检索上下文（标题/年份/类型/评分/简介）。"""
    lines = []
    for i, c in enumerate(ranked[:top_n], 1):
        g = "/".join(c.genres[:3]) if c.genres else "-"
        r = f"{c.rating:.1f}" if c.rating else "未知"
        doc = (c.doc or "").strip().replace("\n", " ")[:240]
        lines.append(f"{i}. 《{c.title}》({c.year}) 类型:{g} 评分:{r} 简介:{doc}")
    return "\n".join(lines) if lines else "（无候选）"


def generate_explanation(query: str, ranked: list[Candidate],
                         prefs: dict, user_id: str) -> str:
    """端到端：检索上下文 + 强档 LLM -> 解释性推荐语。"""
    context = build_context(ranked)
    prefs_s = "，".join(prefs.get("genres", [])) or "（暂无）"
    prompt = EXPLAIN_PROMPT.format(query=query or "（未明确）",
                                   prefs=prefs_s, context=context)
    llm = chat_strong()
    resp = llm.invoke([SystemMessage(content="你是影片推荐解说员，中文作答。"),
                       HumanMessage(content=prompt)])
    return strip_think(resp.content)
