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


# 来源中文标记：用于对话一句话概括与 API 详细字段的「明显来源标记」
SOURCE_LABEL = {"douban": "豆瓣 Douban", "imdb": "IMDb"}


def _source_label(src: str) -> str:
    """候选 source 字段 -> 人类可读标记（豆瓣 / IMDb）。"""
    return SOURCE_LABEL.get(src, src or "未知来源")


def build_source_meta(ranked: list[Candidate], top_n: int = 20) -> dict:
    """统计推荐结果的来源分布，构建 API 详细字段。

    返回结构示例：
    {
      "summary": "豆瓣 8 部 · IMDb 2 部",
      "has_both": true,
      "blocks": [
        {"source": "douban", "label": "豆瓣 Douban", "count": 8,
         "movies": [{"title":..,"year":..,"genres":..,"country":..,"rating":..,"rating_count":..,"intro":..}]},
        {"source": "imdb", "label": "IMDb", "count": 2, "movies": [...]}
      ]
    }
    两个来源都出现时，按「豆瓣 / IMDb」顺序分块展示，块间明显标记，
    让人一眼区分每个作品来自哪个来源。
    """
    by_src: dict[str, list[Candidate]] = {}
    for c in ranked[:top_n]:
        by_src.setdefault(c.source, []).append(c)
    if not by_src:
        return {"summary": "", "has_both": False, "blocks": []}

    order = [s for s in ("douban", "imdb") if s in by_src]  # 固定展示顺序
    blocks = []
    parts = []
    for s in order:
        movies = []
        for c in by_src[s]:
            intro = (c.doc or "").strip().replace("\n", " ")
            if len(intro) > 120:
                intro = intro[:120] + "…"
            movies.append({
                "title": c.title,
                "year": c.year,
                "genres": c.genres,
                "country": c.country,
                "rating": c.rating,
                "rating_count": c.rating_count,
                "intro": intro,
            })
        blocks.append({
            "source": s,
            "label": _source_label(s),
            "count": len(movies),
            "movies": movies,
        })
        parts.append(f"{_source_label(s)} {len(movies)} 部")
    return {
        "summary": " · ".join(parts),
        "has_both": len(blocks) == 2,
        "blocks": blocks,
    }


def source_one_liner(meta: dict) -> str:
    """对话 answer 末尾追加的最简来源一句话。

    单来源：〔信息来源〕豆瓣（8 部）
    双来源：〔信息来源〕豆瓣（8 部）、IMDb（2 部）（各来源作品信息见返回 sources 字段）
    """
    if not meta or not meta.get("blocks"):
        return ""
    parts = [f"{b['label']}（{b['count']} 部）" for b in meta["blocks"]]
    line = "、".join(parts)
    if meta.get("has_both"):
        line += "（各来源作品信息已在返回结果的 sources 字段中分开展示）"
    return f"\n\n〔信息来源〕{line}"


def generate_explanation(query: str, ranked: list[Candidate],
                         prefs: dict, user_id: str, request_id: str = ""):
    """端到端：检索上下文 + 强档 LLM -> 解释性推荐语。

    空响应兜底：本地小模型偶发返回空/异常，此时用候选拼模板解说，
    保证「永远有可解释 answer」，不让前端拿到空白。
    """
    context = build_context(ranked)
    prefs_s = "，".join(prefs.get("genres", [])) or "（暂无）"
    prompt = EXPLAIN_PROMPT.format(query=query or "（未明确）",
                                   prefs=prefs_s, context=context)
    llm = chat_strong(user_id=user_id, request_id=request_id)
    try:
        resp = llm.invoke([SystemMessage(content="你是影片推荐解说员，中文作答。"),
                           HumanMessage(content=prompt)])
        text = strip_think(resp.content).strip()
    except Exception:
        text = ""
    if not text:
        lines = [f"根据你的需求「{query or '好看的片子'}」，结合已记录偏好（{prefs_s}），为你挑出以下作品："]
        for i, c in enumerate(ranked[:5], 1):
            g = "/".join(c.genres[:3]) if c.genres else "未知"
            r = f"{c.rating:.1f}" if c.rating else "未知"
            lines.append(f"{i}. 《{c.title}》({c.year}) 类型:{g} 评分:{r}")
        return "\n".join(lines)
    return text
