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
import logging
import time
import re
from datetime import datetime, timezone
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START, END

from agent.state import AgentState
from agent.prompt import SYSTEM_PROMPT, INTENT_PROMPT

logger = logging.getLogger("movie_agent.graph")
from agent.tools import RUNNERS
from agent.memory import get_memory_store
from agent.feedback_store import get_feedback_store
from retrieval import content_recall
from retrieval.candidates import normalize_genres
from ranker import rerank
from config.settings import settings
from llm.client import chat_light, strip_think
from agent.rag import (generate_explanation, build_source_meta, source_one_liner,
                       _generate_rich, _extract_thinking)
from ranker.rrf import _PLAN_TO_CANON


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


# 召回来源（规范名）→ 中文标签（用于融合可解释步骤的可读展示）。
# P2-13：补 graph/prod——similar 三路展开（P2-8）后 relation 为活键，
# 图谱多跳与生产库补充通道接入（P2-10）后 graph/prod 也会出现在 ranks 里。
_SRC_CN = {"content": "内容", "relation": "关系", "coldstart": "冷启动",
           "person": "人物", "graph": "图谱", "prod": "生产库"}


def _fmt_fusion_explain(fusion_detail: dict, ranked: list, n: int = 5) -> list[str]:
    """把 RRF/score 融合的 explain_map 转成人类可读的「各路排名来源」句子。

    例：『《星际穿越》综合#1：内容#3 · 关系#1 · 冷启动#— → RRF=0.83 +质量0.18 +偏好0.30 = 1.31』
    仅取融合后 top-N 展示，控制 steps 体积。
    """
    lines: list[str] = []
    if not fusion_detail:
        return lines
    for c in ranked[:n]:
        fd = fusion_detail.get(c.id)
        if not fd:
            continue
        ranks = fd.get("ranks", {})
        if ranks:
            parts = []
            for src_key, rk in ranks.items():
                canon = _PLAN_TO_CANON.get(src_key, src_key)
                parts.append(f"{_SRC_CN.get(canon, canon)}#{rk}")
            rank_line = " · ".join(parts)
        else:
            rank_line = "单路"
        strat = fd.get("strategy", "rrf")
        if strat == "rrf":
            contrib = (f"RRF={fd.get('rrf_norm','?')} "
                       f"+质量{fd.get('quality','?')} +偏好{fd.get('pref','?')}")
        else:
            bsrc = fd.get("base_by_src", {})
            bparts = " · ".join(f"{_SRC_CN.get(_PLAN_TO_CANON.get(s, s), s)}={v}"
                                for s, v in bsrc.items()) or "—"
            contrib = (f"归一分[{bparts}] "
                       f"+质量{fd.get('quality','?')} +偏好{fd.get('pref','?')}")
        lines.append(f"《{c.title}》综合#{fd.get('rank','?')}：{rank_line} → {contrib} = {fd.get('final','?')}")
    return lines


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
    except Exception as e:  # noqa: BLE001 - 解析失败走启发式，但必须留痕（P1-8）
        # 曾是裸 except：云端全熔断/网络中断时每请求静默走 heuristic，
        # 主链路故障在日志里毫无痕迹，排障只能看到最终答案异常
        logger.warning("意图解析 LLM 失败，走启发式兜底: %s", e)
        slots = None
    if not slots:
        slots = _heuristic(query)
    intent = slots.get("intent", "recommend")
    if intent not in ("recommend", "similar", "board", "explain", "preference", "by_person"):
        intent = "recommend"
    # by_person：人名兜底（LLM 误判为 recommend 时矫正）
    if intent == "recommend" and _looks_like_person_query(query):
        intent = "by_person"
    # 关键词矫正：即便 LLM 误判，命中偏好语也归为 preference
    if intent not in ("preference", "by_person") and ("记一下" in query or "记住" in query
                                    or ("我" in query and "喜欢" in query)
                                    or "我的偏好" in query or "偏好" in query):
        intent = "preference"
    # query 兜底：内容类/by_person 意图若解析出 query 为空，用原始输入
    # （by_person 的 query 同时作为人名兜底来源，见 _plan）
    if not slots.get("query") and intent in ("recommend", "similar", "board", "explain", "by_person"):
        slots["query"] = query
    elapsed = int((time.time() - t0) * 1000)
    step = _new_step(state, "parse_intent", "llm",
                     f"意图识别：{intent}", detail={"intent": intent, "slots": slots},
                     elapsed_ms=elapsed, thinking=thinking)
    _append_step(state, step)
    return {"intent": intent, "slots": slots,
            "messages": [HumanMessage(content=query)],
            "thinking": {"route": thinking}, "steps": [step]}


def _looks_like_person_query(query: str) -> bool:
    """启发式：是否像「按导演/演员找作品」类 query（诺兰的电影 / 周星驰演的 / X 执导）。"""
    if not query:
        return False
    return bool(re.search(r"(导演|演过|演的|执导|拍[过的]?|的作品|拍的电影|导的|编剧)", query)) \
        or bool(re.search(r"[\u4e00-\u9fa5]{2,4}(的电影|的作品|演的|拍的|导的)", query))


def _heuristic(query: str) -> dict:
    q = query.lower()
    if any(k in query for k in ("类似", "相似", "像", "like")):
        return {"intent": "similar", "query": query, "genres": [], "country": "", "min_year": 0, "item_hint": query}
    if _looks_like_person_query(query):
        return {"intent": "by_person", "query": query, "name": query, "genres": [], "country": "", "min_year": 0, "item_hint": ""}
    if any(k in q for k in ("高分", "榜单", "排行", "board", "top")):
        return {"intent": "board", "query": query, "genres": [], "country": "", "min_year": 0, "item_hint": ""}
    if "偏好" in query or ("记" in query and "喜欢" in query):
        return {"intent": "preference", "query": query, "genres": [], "country": "", "min_year": 0, "item_hint": ""}
    return {"intent": "recommend", "query": query, "genres": [], "country": "", "min_year": 0, "item_hint": ""}


def _resolve_item_id(title: str) -> str | None:
    try:
        cands = content_recall(query=title, top_k=1)
        return cands[0].id if cands else None
    except Exception as e:  # noqa: BLE001 - 定位失败退化为文本召回，但必须留痕（P1-8）
        # 曾是裸 except：向量库故障/超时会被静默改成文本召回，用户得到
        # 「不相似的相似推荐」且无迹可查
        logger.warning("作品定位失败（similar 退化为文本召回）: %s", e)
        return None


def _extract_prefs_llm(query: str) -> dict:
    """用轻量模型从用户原话抽取结构化偏好（类型/导演/演员/备注）。失败返回 {}。

    作为 G4 自动沉淀的「抽取器」：不依赖 LLM 主动调用 remember_preference 工具，
    每轮推荐后自动跑一次，把显式偏好稳定写入 user_preferences（与手动工具共用同一张表）。
    """
    if not query or not query.strip():
        return {}
    try:
        llm = chat_light(user_id="system", request_id="pref-extract")
        prompt = (
            "你是偏好抽取器。从用户这句话提取观影偏好，只输出 JSON（不要解释、不要<think>）。\n"
            '字段：genres(类型数组，如["科幻","悬疑"])、directors(导演名数组)、'
            'actors(演员名数组)、note(一句话备注，无则"")。\n'
            '示例：{"genres":["科幻"],"directors":["诺兰"],"actors":[],"note":"喜欢烧脑"}'
        )
        resp = llm.invoke([HumanMessage(content=prompt + "\n用户：" + query)])
        return _extract_json(strip_think(resp.content)) or {}
    except Exception as exc:  # 抽取失败不应阻断主流程
        logger.warning("偏好抽取失败（跳过自动沉淀）: %s", exc)
        return {}


def _sediment_preferences(state: AgentState) -> None:
    """G4 自动沉淀：每轮推荐后把结构化偏好写入 user_preferences（合并去重 + 追加历史）。

    - 类型偏好：优先取意图 slots.genres，再补 LLM 抽取的类型；
    - 人物偏好：by_person 意图时把人名写入 prefs.people；LLM 抽取到的导演/演员分别入表；
    - 历史：追加一条 {ts, intent, query, top_title} 便于后续回顾与画像演进；
    - 仅对真实登录用户（非 default/tool）生效，避免污染匿名画像；
    - 任何异常静默忽略，绝不阻断主流程。
    """
    uid = state.get("user_id", "default")
    if uid in ("default", "tool", "eval", ""):
        return
    intent = state.get("intent")
    query = state.get("query", "")
    slots = state.get("slots", {}) or {}
    ranked = state.get("ranked", []) or []

    genres = list(slots.get("genres") or [])
    llm_prefs = _extract_prefs_llm(query)
    genres += (llm_prefs.get("genres") or [])
    directors = llm_prefs.get("directors") or []
    actors = llm_prefs.get("actors") or []
    people = []
    if intent == "by_person":
        name = (slots.get("name") or query).strip()
        if name:
            people.append(name)
    note = llm_prefs.get("note") or None

    if not (genres or directors or actors or people or note):
        return

    top = ranked[0] if ranked else None
    history_item = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "intent": intent,
        "query": query,
        "top_title": getattr(top, "title", None),
    }
    try:
        get_memory_store().sediment_preferences(
            uid,
            genres=genres or None,
            directors=directors or None,
            actors=actors or None,
            people=people or None,
            note=note,
            history_item=history_item,
        )
    except Exception as exc:  # 沉淀失败不影响推荐
        logger.warning("偏好自动沉淀失败（忽略）: %s", exc)


def _plan(state: AgentState) -> AgentState:
    """按意图规划召回路（P2-10：六条 runner 的可达路径在此明示并接线）。

    可达路径（全链路只有本函数产生 plan，_execute 按 plan 调 RUNNERS）：
    - recommend / explain → search_by_text（MCP_ENABLED 时追加 search_prod
      生产库补充通道；关闭时该路不进 plan，避免空路噪音）
    - similar             → search_similar（P2-8 起展开为 content/relation/
      graph 三路——其中 graph 路即知识图谱多跳召回，GRAPH_ENABLED=false
      时该路为空，其余两路不受影响）
    - board               → high_quality_board
    - by_person           → search_by_person
    - preference          → 无（跳过召回，直接走记忆回答）
    search_graph 与 similar 的 graph 路同源（都是 graph_recall），经 similar
    展开可达，不单独进 plan（避免同一召回路双路双权重）。
    get_tools() 是预留的 tool-calling 入口（当前为规则式规划器，未启用
    tool-calling 循环，见 agent/tools.py）。
    """
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
    elif intent == "by_person":
        # 人名兜底：确保 runner 能拿到人名（LLM 可能填在 query 或 name；
        # 均空时用原始输入兜底，注意必须取 state 内的 query——本作用域无局部 query 变量）
        slots["name"] = slots.get("name") or slots.get("query") or state.get("query", "")
        plan = ["search_by_person"]
    else:  # recommend / explain -> 内容召回为主
        plan = ["search_by_text"]
        # P2-10：生产库只读补充通道按开关接入——此前 search_prod 在规划器
        # 不可达（六路召回的宣称名不副实），MCP_ENABLED=false 时该路恒空。
        if settings.MCP_ENABLED:
            plan.append("search_prod")
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


def _feedback_profile_for(uid: str) -> dict | None:
    """O11：拉取用户反馈画像（失败静默降级为 None，绝不阻断主链路）。"""
    if not settings.FEEDBACK_REWEIGHT_ENABLED:
        return None
    try:
        return get_feedback_store().get_profile(uid, half_life_days=settings.FEEDBACK_HALF_LIFE_DAYS)
    except Exception as e:  # noqa: BLE001 - 反馈存储异常不影响推荐
        logger.warning("拉取反馈画像失败（跳过调权）: %s", e)
        return None


def _execute(state: AgentState) -> AgentState:
    t0 = time.time()
    uid = state.get("user_id", "default")
    slots = dict(state.get("slots", {}))
    prefs = get_memory_store().get_prefs(uid)
    user_genres = prefs.get("genres")
    feedback_profile = _feedback_profile_for(uid)
    # P2-11：top_k 从 state 下传（/recommend 的 req.top_k，1~50）。此前 rerank
    # 硬编码 top_k=20，req.top_k>20 被静默截断——接口收 50 实际最多返 20。
    top_k = int(state.get("top_k") or 20)
    # 召回池不小于 20：融合/MMR/精排都需要足够候选才有挑选空间（与最终条数解耦）
    recall_k = max(20, top_k)
    recall: dict[str, list] = {}
    try:
        for name in state.get("plan", []):
            if name == "search_similar":
                # P2-8：similar 的三路（content/relation/graph）以原始列表展开进
                # recall，交外层 RRF 按各自权重统一融合——此前 runner 内层先做
                # score 预融合再当单路返回，relation/graph 权重只在内层生效、
                # 外层把整包当 content(1.0)，且 min-max 量级脆弱性在该路径复活。
                for src, lst in RUNNERS["search_similar"](
                    slots.get("item_id"), top_k=recall_k
                ).items():
                    if lst:  # 空路不建键（保持 recall_counts 干净）
                        recall.setdefault(src, []).extend(lst)
            else:
                # query 兜底：内容召回若 slots 无 query，用原始输入或偏好类型
                eff = slots
                if name == "search_by_text" and not slots.get("query"):
                    fb = state.get("query") or (" ".join(user_genres) if user_genres else "高分好看的作品")
                    eff = {**slots, "query": fb}
                recall[name] = RUNNERS[name](eff, prefs, top_k=recall_k)
        res = rerank(recall, diversify=settings.RERANK_DIVERSIFY,
                     llm_topn=settings.RERANK_LLM_TOPN, query=state.get("query"),
                     top_k=top_k, user_genres=user_genres,
                     feedback_profile=feedback_profile, explain=True)
        fusion_detail = res.get("fusion_detail") if isinstance(res, dict) else {}
        ranked = res["ranked"] if isinstance(res, dict) else res
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
                             "fusion_strategy": settings.FUSION_STRATEGY,
                             "pipeline": {"diversify": settings.RERANK_DIVERSIFY,
                                          "llm_topn": settings.RERANK_LLM_TOPN,
                                          "mmr_lambda": settings.MMR_LAMBDA,
                                          "fusion_weights": settings.RRF_WEIGHTS_DICT},
                             "fusion_explain": _fmt_fusion_explain(fusion_detail, ranked, n=5)},
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
        feedback_profile = _feedback_profile_for(state.get("user_id", "default"))
        res = rerank(merged, diversify=settings.RERANK_DIVERSIFY,
                     llm_topn=settings.RERANK_LLM_TOPN, query=state.get("query"),
                     top_k=20, user_genres=user_genres,
                     feedback_profile=feedback_profile, explain=True)
        fusion_detail = res.get("fusion_detail") if isinstance(res, dict) else {}
        ranked = res["ranked"] if isinstance(res, dict) else res
        elapsed = int((time.time() - t0) * 1000)
        step = _new_step(state, "reflect", "retrieval",
                         f"候选不足，冷启动兜底补 {len(board)} 条 → 重排 {len(ranked)} 条",
                         detail={"fallback_count": len(board), "ranked_count": len(ranked),
                                 "fusion_strategy": settings.FUSION_STRATEGY,
                                 "fusion_explain": _fmt_fusion_explain(fusion_detail, ranked, n=3)},
                         elapsed_ms=elapsed)
        _append_step(state, step)
        # P1-5：兜底成功必须清除 execute 阶段写入的 error——error 是无 reducer 的
        # 普通键，不清除则 generate 见 error 即返回「检索出了点问题」失败文案，
        # 把刚刚兜底回来的 ranked 整份丢弃（兜底链路实际不可用）。
        return {"ranked": ranked, "steps": [step], "error": ""}
    except Exception as e:  # noqa: BLE001 - 兜底失败不阻断，但必须留痕（P1-8）
        # 曾是裸 except return {}：候选不足时冷启动兜底再失败（向量库故障等），
        # 用户拿到「基于 0-4 部候选」的空洞回答，日志却毫无痕迹
        logger.warning("冷启动兜底失败（reflect 降级）: %s", e, exc_info=True)
        step = _new_step(state, "reflect", "retrieval", "冷启动兜底失败",
                         detail={"error": str(e)[:200]}, elapsed_ms=int((time.time() - t0) * 1000))
        _append_step(state, step)
        return {"steps": [step]}


def _generate(state: AgentState) -> AgentState:
    t0 = time.time()
    uid = state.get("user_id", "default")
    intent = state["intent"]
    if intent == "preference":
        # 把意图里的类型写进记忆（P1-1：仅接受 LLM 给出的类型并过词表闸门；
        # 曾用正则从原话切 2-3 字汉语片段当类型，「我喜欢周星驰的电影」会切出
        # ["我喜欢","周星驰","的电"] 落库并打空后续召回，已删除该兜底）
        slots = state.get("slots", {})
        genres = normalize_genres(slots.get("genres") or [])
        get_memory_store().add_pref(uid, genres=genres or None)
        elapsed = int((time.time() - t0) * 1000)
        step = _new_step(state, "generate", "memory", "记录用户偏好",
                         detail={"genres": genres or []}, elapsed_ms=elapsed)
        _append_step(state, step)
        return {"answer": f"已记下你的偏好（{genres or '未提取到具体类型'}），下次推荐会优先考虑。",
                "sources": {}, "steps": [step]}
    # P1-5 双保险：仅当「有错误且没有任何候选」才走失败文案；
    # reflect 冷启动兜底成功（ranked 非空）后即使 error 残留也正常生成
    if state.get("error") and not state.get("ranked"):
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
        history=state.get("history", "") or "",
    )
    # 第 3 层护栏：生成后闸门，拦截输出中的注入回显 / 策略泄漏 / 有害 how-to
    from agent.guardrail import check_output
    out_gate = check_output(answer)
    if out_gate.action == "block":
        logger.warning("输出护栏拦截（替换为安全文案）: %s", out_gate.reason)
        answer = out_gate.message
    elapsed = int((time.time() - t0) * 1000)
    # 来源标记：对话末尾追加一句话来源概况；详细分源结构走 API sources 字段
    meta = build_source_meta(ranked)
    if meta.get("blocks"):
        answer = answer + source_one_liner(meta)
    # G4 自动沉淀：每轮推荐后把结构化偏好写入 user_preferences（preference 意图已在上方单独写，避免重复）
    if intent != "preference":
        _sediment_preferences(state)
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
