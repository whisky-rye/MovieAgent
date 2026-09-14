"""Agent 编排（LangGraph 状态图）：意图 → 规划 → 工具 → 反思 → 生成。

为什么这条管线：
- 意图/生成用强/弱 LLM；规划/执行/反思走确定性逻辑，稳定可复现，避免本地小模型工具调用抖动。
- 多路召回在 execute 节点融合，reflect 节点做兜底，保证「永远有候选可说」。

可观测（v2.13）：
- 每个节点向 state.steps 追加一条「步骤」记录（reducer 累加），含节点名/类型/一句话摘要/
  结构化详情/耗时/该 LLM 步骤的思考过程，供「工具链可视化」对外输出。
- 大模型深度思考过程（reasoning_content）抽取进 state.thinking（{route, generate}），
  并随流式 sink 实时推送（/recommend/stream）。
"""
import json
import time
import re
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START, END

from agent.state import AgentState
from agent.prompt import SYSTEM_PROMPT, INTENT_PROMPT
from agent.tools import RUNNERS
from agent.memory import get_memory_store
from retrieval import content_recall
from ranker import rerank
from config.settings import settings
from llm.client import chat_light, strip_think
from agent.rag import (generate_explanation, build_source_meta, source_one_liner,
                       _generate_rich, _extract_thinking)


def _extract_json(text: str) -> dict | None:
    """从模型输出里抽出第一个 {..} JSON 块（容错 think 残留/多余文本）。"""
    m = re.search(r"\{[\s\S]*?\}", text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _append_step(state: AgentState, step: dict) -> None:
    """实时推送步骤事件（流式场景）；步骤本身由各节点返回经 reducer 累加。"""
    sink = state.get("_step_sink")
    if callable(sink):
        try:
            sink(step)
        except Exception:
            pass


def _new_step(state: AgentState, node: str, kind: str, summary: str,
              detail: dict | None = None, elapsed_ms: int = 0,
              thinking: str = "") -> dict:
    order = len(state.get("steps") or []) + 1
    return {
        "order": order,
        "node": node,
        "kind": kind,                 # llm | route | retrieval | rank | memory | generate
        "summary": summary,
        "detail": detail or {},
        "elapsed_ms": elapsed_ms,
        "thinking": thinking,
    }


def _parse_intent(state: AgentState) -> AgentState:
    t0 = time.time()
    uid = state.get("user_id", "default")
    query = state.get("query", "")
    llm = chat_light(user_id=uid, request_id=state.get("request_id", ""))
    slots = None
    thinking = ""
    try:
        resp = llm.invoke([SystemMessage(content=INTENT_PROMPT),
                           HumanMessage(content=query)])
        thinking = _extract_thinking(resp)
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
    elapsed = int((time.time() - t0) * 1000)
    step = _new_step(state, "parse_intent", "llm",
                     f"意图识别：{intent}", detail={"intent": intent, "slots": slots},
                     elapsed_ms=elapsed, thinking=thinking)
    _append_step(state, step)
    return {"intent": intent, "slots": slots,
            "messages": [HumanMessage(content=query)],
            "thinking": {"route": thinking}, "steps": [step]}


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
    t0 = time.time()
    intent = state["intent"]
    slots = state.get("slots", {})
    if intent == "similar":
        item = slots.get("item_hint") or slots.get("query")
        iid = _resolve_item_id(item) if item else None
        if iid:
            slots["item_id"] = iid
            plan = ["search_similar"]
        else:
            plan = ["search_by_text"]
    elif intent == "board":
        plan = ["high_quality_board"]
    elif intent == "preference":
        plan = []
    else:  # recommend / explain -> 内容召回为主
        plan = ["search_by_text"]
    elapsed = int((time.time() - t0) * 1000)
    if plan:
        step = _new_step(state, "plan", "route", f"规划工具链：{plan}",
                         detail={"plan": plan}, elapsed_ms=elapsed)
        _append_step(state, step)
        return {"slots": slots, "plan": plan, "steps": [step]}
    step = _new_step(state, "plan", "route", "规划工具链：无（偏好类，跳过召回）",
                     detail={"plan": []}, elapsed_ms=elapsed)
    _append_step(state, step)
    return {"slots": slots, "plan": plan, "steps": [step]}


def _execute(state: AgentState) -> AgentState:
    t0 = time.time()
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
        elapsed = int((time.time() - t0) * 1000)
        step = _new_step(state, "execute", "retrieval", "召回失败",
                         detail={"error": str(e)}, elapsed_ms=elapsed)
        _append_step(state, step)
        return {"error": f"召回失败: {e}", "ranked": [], "steps": [step]}
    elapsed = int((time.time() - t0) * 1000)
    recall_counts = {k: len(v) for k, v in recall.items()}
    step = _new_step(state, "execute", "retrieval",
                     f"多路召回 {sum(recall_counts.values())} 条 → 重排 {len(ranked)} 条",
                     detail={"plan": list(recall.keys()), "recall_counts": recall_counts,
                             "ranked_count": len(ranked),
                             "fusion_strategy": settings.FUSION_STRATEGY},
                     elapsed_ms=elapsed)
    _append_step(state, step)
    return {"candidates": recall, "ranked": ranked, "steps": [step]}


def _reflect(state: AgentState) -> AgentState:
    t0 = time.time()
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
        elapsed = int((time.time() - t0) * 1000)
        step = _new_step(state, "reflect", "retrieval",
                         f"候选不足，冷启动兜底补 {len(board)} 条 → 重排 {len(ranked)} 条",
                         detail={"fallback_count": len(board), "ranked_count": len(ranked)},
                         elapsed_ms=elapsed)
        _append_step(state, step)
        return {"ranked": ranked, "steps": [step]}
    except Exception:
        return {}


def _generate(state: AgentState) -> AgentState:
    t0 = time.time()
    uid = state.get("user_id", "default")
    intent = state["intent"]
    if intent == "preference":
        # 把意图里的类型写进记忆
        slots = state.get("slots", {})
        genres = slots.get("genres") or []
        if not genres:
            genres = re.findall(r"[\u4e00-\u9fa5]{2,3}", slots.get("query", ""))
        get_memory_store().add_pref(uid, genres=genres or None)
        elapsed = int((time.time() - t0) * 1000)
        step = _new_step(state, "generate", "memory", "记录用户偏好",
                         detail={"genres": genres or []}, elapsed_ms=elapsed)
        _append_step(state, step)
        return {"answer": f"已记下你的偏好（{genres or '未提取到具体类型'}），下次推荐会优先考虑。",
                "sources": {}, "steps": [step]}
    if state.get("error"):
        elapsed = int((time.time() - t0) * 1000)
        step = _new_step(state, "generate", "generate", "检索失败，返回兜底提示",
                         detail={"error": state["error"]}, elapsed_ms=elapsed)
        _append_step(state, step)
        return {"answer": f"抱歉，检索出了点问题：{state['error']}。可换个说法再试。",
                "sources": {}, "steps": [step]}
    ranked = state.get("ranked", [])
    prefs = get_memory_store().get_prefs(uid)
    token_sink = state.get("_token_sink")
    thinking_sink = state.get("_thinking_sink")
    answer, gen_thinking = _generate_rich(
        state.get("query", ""), ranked, prefs, uid,
        request_id=state.get("request_id", ""),
        token_sink=token_sink, thinking_sink=thinking_sink,
    )
    elapsed = int((time.time() - t0) * 1000)
    # 来源标记：对话末尾追加一句话来源概况；详细分源结构走 API sources 字段
    meta = build_source_meta(ranked)
    if meta.get("blocks"):
        answer = answer + source_one_liner(meta)
    # thinking 合并（route 步骤已在 state.thinking 中，此处补 generate）
    th = dict(state.get("thinking") or {})
    th["generate"] = gen_thinking
    step = _new_step(state, "generate", "generate",
                     f"生成推荐解说（基于 {len(ranked)} 部候选）",
                     detail={"candidate_count": len(ranked)}, elapsed_ms=elapsed,
                     thinking=gen_thinking)
    _append_step(state, step)
    return {"answer": answer, "sources": meta, "thinking": {"generate": gen_thinking},
            "steps": [step]}


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
