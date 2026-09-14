"""Agent 编排（LangGraph 状态图）：意图 → 规划 → 工具 → 反思 → 生成。

为什么这条管线：
- 意图/生成用强/弱 LLM；规划/执行/反思走确定性逻辑，稳定可复现，避免本地小模型工具调用抖动。
- 多路召回在 execute 节点融合，reflect 节点做兜底，保证「永远有候选可说」。
"""
import json
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START, END

from agent.state import AgentState
from agent.prompt import SYSTEM_PROMPT, INTENT_PROMPT
from agent.tools import RUNNERS
from agent.memory import get_memory_store
from retrieval import content_recall
from ranker import rerank
from llm.client import chat_light, strip_think
from agent.rag import generate_explanation, build_source_meta, source_one_liner


def _extract_json(text: str) -> dict | None:
    """从模型输出里抽出第一个 {..} JSON 块（容错 think 残留/多余文本）。"""
    import re
    m = re.search(r"\{[\s\S]*?\}", text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _parse_intent(state: AgentState) -> AgentState:
    uid = state.get("user_id", "default")
    query = state.get("query", "")
    llm = chat_light(user_id=uid, request_id=state.get("request_id", ""))
    slots = None
    try:
        resp = llm.invoke([SystemMessage(content=INTENT_PROMPT),
                           HumanMessage(content=query)])
        slots = _extract_json(strip_think(resp.content))
    except Exception:
        slots = None
    if not slots:
        slots = _heuristic(query)
    intent = slots.get("intent", "recommend")
    if intent not in ("recommend", "similar", "board", "explain", "preference"):
        intent = "recommend"
    # 关键词矫正：即便 LLM 误判，命中偏好语也归为 preference
    if intent != "preference" and ("记一下" in query or "记住" in query
                                    or ("我" in query and "喜欢" in query)
                                    or "我的偏好" in query or "偏好" in query):
        intent = "preference"
    # query 兜底：内容类意图若解析出 query 为空，用原始输入
    if not slots.get("query") and intent in ("recommend", "similar", "board", "explain"):
        slots["query"] = query
    return {"intent": intent, "slots": slots,
            "messages": [HumanMessage(content=query)]}


def _heuristic(query: str) -> dict:
    q = query.lower()
    if any(k in query for k in ("类似", "相似", "像", "like")):
        return {"intent": "similar", "query": query, "genres": [], "country": "", "min_year": 0, "item_hint": query}
    if any(k in q for k in ("高分", "榜单", "排行", "board", "top")):
        return {"intent": "board", "query": query, "genres": [], "country": "", "min_year": 0, "item_hint": ""}
    if "偏好" in query or ("记" in query and "喜欢" in query):
        return {"intent": "preference", "query": query, "genres": [], "country": "", "min_year": 0, "item_hint": ""}
    return {"intent": "recommend", "query": query, "genres": [], "country": "", "min_year": 0, "item_hint": ""}


def _resolve_item_id(title: str) -> str | None:
    try:
        cands = content_recall(query=title, top_k=1)
        return cands[0].id if cands else None
    except Exception:
        return None


def _plan(state: AgentState) -> AgentState:
    intent = state["intent"]
    slots = state.get("slots", {})
    if intent == "similar":
        item = slots.get("item_hint") or slots.get("query")
        iid = _resolve_item_id(item) if item else None
        if iid:
            slots["item_id"] = iid
        return {"slots": slots, "plan": ["search_similar"] if iid else ["search_by_text"]}
    if intent == "board":
        return {"plan": ["high_quality_board"]}
    if intent == "preference":
        return {"plan": []}
    # recommend / explain -> 内容召回为主
    return {"plan": ["search_by_text"]}


def _execute(state: AgentState) -> AgentState:
    uid = state.get("user_id", "default")
    slots = dict(state.get("slots", {}))
    prefs = get_memory_store().get_prefs(uid)
    user_genres = prefs.get("genres")
    recall: dict[str, list] = {}
    try:
        for name in state.get("plan", []):
            if name == "search_similar":
                recall["similar"] = RUNNERS["search_similar"](slots.get("item_id"), top_k=20)
            else:
                # query 兜底：内容召回若 slots 无 query，用原始输入或偏好类型
                eff = slots
                if name == "search_by_text" and not slots.get("query"):
                    fb = state.get("query") or (" ".join(user_genres) if user_genres else "高分好看的作品")
                    eff = {**slots, "query": fb}
                recall[name] = RUNNERS[name](eff, prefs, top_k=20)
        ranked = rerank(recall, strategy="mmr", top_k=20, user_genres=user_genres)
    except Exception as e:
        return {"error": f"召回失败: {e}", "ranked": []}
    return {"candidates": recall, "ranked": ranked}


def _reflect(state: AgentState) -> AgentState:
    ranked = state.get("ranked", [])
    if len(ranked) >= 5:
        return {}
    # 兜底：补高分榜，再融合重排
    try:
        board = RUNNERS["high_quality_board"](state.get("slots", {}),
                                               get_memory_store().get_prefs(state.get("user_id", "default")),
                                               top_k=20)
        merged = state.get("candidates", {})
        merged["coldstart_fallback"] = board
        user_genres = get_memory_store().get_prefs(state.get("user_id", "default")).get("genres")
        ranked = rerank(merged, strategy="mmr", top_k=20, user_genres=user_genres)
    except Exception:
        pass
    return {"ranked": ranked}


def _generate(state: AgentState) -> AgentState:
    uid = state.get("user_id", "default")
    intent = state["intent"]
    if intent == "preference":
        # 把意图里的类型写进记忆
        slots = state.get("slots", {})
        genres = slots.get("genres") or []
        if not genres:
            import re
            genres = re.findall(r"[\u4e00-\u9fa5]{2,3}", slots.get("query", ""))
        get_memory_store().add_pref(uid, genres=genres or None)
        return {"answer": f"已记下你的偏好（{genres or '未提取到具体类型'}），下次推荐会优先考虑。",
                "sources": {}}
    if state.get("error"):
        return {"answer": f"抱歉，检索出了点问题：{state['error']}。可换个说法再试。",
                "sources": {}}
    ranked = state.get("ranked", [])
    prefs = get_memory_store().get_prefs(uid)
    answer = generate_explanation(state.get("query", ""), ranked, prefs, uid,
                                  request_id=state.get("request_id", ""))
    # 来源标记：对话末尾追加一句话来源概况；详细分源结构走 API sources 字段
    meta = build_source_meta(ranked)
    if meta.get("blocks"):
        answer = answer + source_one_liner(meta)
    return {"answer": answer, "sources": meta}


def build_agent():
    """编译并返回 LangGraph 应用（开发期本地运行，无需 Redis/服务）。"""
    g = StateGraph(AgentState)
    g.add_node("parse_intent", _parse_intent)
    g.add_node("plan", _plan)
    g.add_node("execute", _execute)
    g.add_node("reflect", _reflect)
    g.add_node("generate", _generate)
    g.add_edge(START, "parse_intent")
    g.add_edge("parse_intent", "plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "reflect")
    g.add_edge("reflect", "generate")
    g.add_edge("generate", END)
    return g.compile()


def run_agent(query: str, user_id: str = "default") -> str:
    """便捷入口：一句话拿到推荐解说。"""
    app = build_agent()
    result = app.invoke({"user_id": user_id, "query": query})
    return result.get("answer", "")
