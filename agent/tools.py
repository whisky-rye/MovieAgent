"""Agent 工具集（LangChain 工具面）。

前若干个是对接召回层的「检索工具」，返回紧凑可读摘要；同时暴露 RUNNERS 注册表，
供 LangGraph 节点直接取 Candidate 列表做多路融合（避免字符串回解的脆弱）。
另含偏好记忆写入、RAG 解释，以及生产库只读通道（search_prod，默认关闭）。
"""
import logging

from langchain_core.tools import tool
from retrieval.candidates import Candidate
from retrieval import content_recall, relation_recall, coldstart_recall
from ranker import rerank
from agent.memory import get_memory_store
from graph.recall import graph_recall, graph_recall_by_entity, person_recall
from config.settings import settings

logger = logging.getLogger("movie_agent.tools")


def _fmt(cands: list[Candidate], label: str) -> str:
    if not cands:
        return f"[{label}] 无命中"
    lines = [f"[{label}] 命中 {len(cands)} 条："]
    for i, c in enumerate(cands[:10], 1):
        g = "/".join(c.genres[:3]) if c.genres else "-"
        r = f"{c.rating:.1f}" if c.rating else "?"
        lines.append(f"{i}. {c.title}({c.year}) [{g}] 评分{r} id={c.id}")
    return "\n".join(lines)


# ---- 检索工具（LangChain 工具面，供 LLM 工具调用 / 交互使用）----

@tool
def search_by_text(query: str, top_k: int = 10, genres: list = None,
                   country: str = None, min_year: int = 0, min_rating: float = 0.0) -> str:
    """根据自然语言描述（如『太空科幻 孤独』）做内容相似召回。可附带类型/国家/年份过滤。"""
    cands = content_recall(query=query, top_k=top_k, genres=genres or None,
                           country=country or None,
                           min_year=min_year or None,
                           min_rating=min_rating or None)
    return _fmt(cands, "内容相似")


@tool
def search_similar(item_id: str, top_k: int = 10) -> str:
    """找与某作品相似的影片（融合内容相似 + 同导演/卡司关系 + 知识图谱多跳扩展）。item_id 形如 douban:{PK} 或 imdb:{imdb_id}。"""
    cands = _run_search_similar(item_id, top_k)
    return _fmt(cands, "相似+关系+图谱")


@tool
def high_quality_board(top_k: int = 10, genres: list = None,
                       country: str = None, min_year: int = 0) -> str:
    """无明确偏好时的高分口碑榜单兜底（质量×热度）。可过滤类型/国家/年份。"""
    cands = coldstart_recall(top_k=top_k, genres=genres or None,
                             country=country or None, min_year=min_year or None)
    return _fmt(cands, "高分榜")


@tool
def search_graph(item_id: str, top_k: int = 10) -> str:
    """以某作品为种子做知识图谱多跳扩展（影片→导演/编剧/卡司→其作品，含同类型/地区）。
    item_id 形如 douban:{PK} 或 imdb:{imdb_id}。适合「这部导演还有哪些片子」「类似宇宙的其它作品」等探索。"""
    cands = graph_recall(item_id, top_k=top_k)
    return _fmt(cands, "知识图谱")


@tool
def search_by_person(name: str, top_k: int = 10) -> str:
    """按导演/演员查找其本人作品集（如「诺兰」「周星驰」）。返回该人署名的电影/剧集列表。"""
    cands = _run_search_by_person({"name": name, "query": name}, {}, top_k=top_k)
    return _fmt(cands, "人物作品集")


@tool
def search_prod(query: str, top_k: int = 10) -> str:
    """在生产库（豆瓣/IMDb 全量）按标题检索影片——补充/校验通道，用于长尾补齐。
    仅当 MCP_ENABLED=true 且远端可达时生效；🔴 只读，绝不写入生产库。"""
    if not settings.MCP_ENABLED:
        return "[生产库只读] 通道未启用（MCP_ENABLED=false）"
    cands = _run_search_prod({"query": query}, {}, top_k=top_k)
    return _fmt(cands, "生产库只读")


@tool
def remember_preference(user_id: str, genres: list = None, note: str = None) -> str:
    """记录用户偏好（喜欢的类型或备注），用于后续推荐加权。"""
    prefs = get_memory_store().add_pref(user_id, genres=genres, note=note)
    return f"已记录偏好：{prefs}"


@tool
def explain_recommendation(query: str, top_k: int = 6) -> str:
    """对一段需求做端到端推荐并生成解释（内容召回 + RAG 解说）。"""
    from agent.rag import generate_explanation
    cands = content_recall(query=query, top_k=top_k * 3)
    ranked = rerank(cands, strategy="mmr", top_k=top_k)
    return generate_explanation(query, ranked, prefs={}, user_id="tool")


# ---- RUNNERS：供 LangGraph 节点直接取 Candidate 列表做融合 ----

def _run_search_by_text(slots: dict, prefs: dict, top_k: int = 20) -> list[Candidate]:
    g = slots.get("genres") or prefs.get("genres")
    return content_recall(
        query=slots.get("query") or "",
        top_k=top_k,
        genres=g or None,
        country=slots.get("country") or None,
        min_year=slots.get("min_year") or None,
        min_rating=slots.get("min_rating") or None,
    )


def _run_search_similar(item_id: str, top_k: int = 20) -> list[Candidate]:
    sim = content_recall(item_id=item_id, top_k=top_k)
    rel = relation_recall(item_id=item_id, top_k=top_k)
    # F1/O12：知识图谱多跳扩展，与内容/关系召回一并融合，覆盖「同导演其它作品」「合作网络」等
    graph_cands = graph_recall(item_id, top_k=top_k)
    fused = rerank(
        {"content": sim, "relation": rel, "graph": graph_cands},
        strategy="score", top_k=top_k,
    )
    return fused


def _run_search_graph(item_id: str, top_k: int = 20) -> list[Candidate]:
    """知识图谱多跳召回（独立召回路，可直接并入 plan）。"""
    return graph_recall(item_id, top_k=top_k)


def _run_search_by_person(slots: dict, prefs: dict, top_k: int = 20) -> list[Candidate]:
    """按人物（导演/演员）召回其本人作品集。

    - 直接用 graph.person_recall（底层 KnowledgeGraphStore.person_filmography，
      仅走 fetcher，不依赖 GRAPH_ENABLED，即便图谱遍历关闭也能返回人物作品集）。
    - 异常不影响主链路，降级为空列表。
    """
    name = (slots.get("name") or slots.get("query") or "").strip()
    if not name:
        return []
    try:
        return person_recall(name, top_k=top_k)
    except Exception:  # noqa: BLE001 - 补充通道异常绝不阻断主链路
        logger.warning("按人物召回失败（忽略，降级本地召回）", exc_info=True)
        return []


def _run_search_prod(slots: dict, prefs: dict, top_k: int = 20) -> list[Candidate]:
    """生产库只读通道召回（补充/校验路，默认关闭）。

    - 仅在 `MCP_ENABLED=true` 且远端可达时产出候选；否则返回空列表（等价于该路不存在）。
    - 远端仅执行 SELECT（护栏 + 会话只读 + 限流 + 熔断），绝不写入。
    - 产出的 Candidate 携带 imdb_id/douban_id，可与本地召回做跨源归并（work_key）。
    """
    if not settings.MCP_ENABLED:
        return []
    query = (slots.get("query") or "").strip()
    if not query:
        return []
    try:
        from mcp_prod.client import get_prod_channel

        channel = get_prod_channel()
        if not channel.is_available():
            return []
        cands = channel.search_movie(query, source="douban", limit=top_k) or []
        # 双源各查一轮：豆瓣给中文名与评分，IMDb 给英文名与评分（跨源归并后只出一条）
        if top_k > len(cands):
            cands += (channel.search_movie(query, source="imdb",
                                           limit=top_k - len(cands)) or [])
        for c in cands:
            c.recall_source = "prod"
        return cands[:top_k]
    except Exception:  # noqa: BLE001 - 补充通道异常绝不阻断主链路
        logger.warning("生产库只读通道召回失败（忽略，降级本地召回）", exc_info=True)
        return []


def _run_board(slots: dict, prefs: dict, top_k: int = 20) -> list[Candidate]:
    g = slots.get("genres") or prefs.get("genres")
    return coldstart_recall(
        top_k=top_k,
        genres=g or None,
        country=slots.get("country") or None,
        min_year=slots.get("min_year") or None,
    )


RUNNERS = {
    "search_by_text": _run_search_by_text,
    "search_similar": _run_search_similar,
    "search_graph": _run_search_graph,
    "search_by_person": _run_search_by_person,
    "search_prod": _run_search_prod,
    "high_quality_board": _run_board,
}


def get_tools():
    """返回 LangChain 工具（供交互式 tool-calling 使用）。"""
    return [search_by_text, search_similar, search_graph, search_by_person,
            search_prod, high_quality_board, remember_preference,
            explain_recommendation]
