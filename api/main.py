"""P4.1 FastAPI 服务：对外暴露推荐 / 对话接口（开发期本地进程，无 Docker）。

- POST /recommend {query, user_id, top_k} -> 结构化候选列表 + 解说文案
- POST /chat     {query, user_id}          -> StreamingResponse 流式解说（逐句推送）
- GET  /health                            -> 健康检查

复用 agent.build_agent()，模块级只编译一次图；业务层（agent/retrieval/ranker）零改动。
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config.logging_setup import setup_logging
from agent.graph import build_agent
from retrieval.candidates import Candidate
from api.cache import get_cache, cache_key
from api.ratelimit import get_limiter, RateLimited
from agent.memory import get_memory_store
from agent.guardrail import check_guardrail
from config.settings import settings

setup_logging()  # 集中式结构化日志（JSON 行 + 滚动文件 + 专用 llm_calls.log）
logger_api = logging.getLogger("movie_agent.api")

app = FastAPI(title="影片推荐智能体 API", version="0.1.0")


@app.middleware("http")
async def log_http_requests(request: Request, call_next):
    """HTTP 访问日志（结构化）：方法/路径/用户/状态码/耗时；query 由 ACCESS_LOG_BODY 控制。
    统一生成 request_id（写入 request.state），供 recommend/chat 串联到云端 API 日志。"""
    t0 = time.time()
    rid = uuid.uuid4().hex[:12]
    request.state.request_id = rid
    uid = "-"
    query = None
    if request.method == "POST" and request.url.path in ("/recommend", "/chat", "/recommend/stream"):
        try:
            body = await request.body()  # Starlette 会缓存，端点仍可读取
            data = json.loads(body)
            uid = data.get("user_id", "-")
            query = data.get("query")
        except Exception:
            pass
    response = await call_next(request)
    latency_ms = int((time.time() - t0) * 1000)
    extra = {"event": "http_request", "method": request.method, "path": request.url.path,
             "status_code": response.status_code,
             "client": request.client.host if request.client else "-",
             "user_id": uid, "request_id": rid, "latency_ms": latency_ms}
    if settings.ACCESS_LOG_BODY and query is not None:
        extra["query"] = query
    logger_api.info("http_request", extra=extra)
    return response


logger_api.info(
    "服务启动", extra={"event": "startup", "env": settings.APP_ENV,
    "llm_provider": settings.LLM_PROVIDER, "memory_backend": get_memory_store().backend(),
    "cache_backend": get_cache().backend(), "guardrail": settings.GUARDRAIL_ENABLED,
    "ratelimit": settings.RATELIMIT_ENABLED, "log_body": settings.LLM_LOG_BODY},
)

# 模块级编译一次，所有请求复用（避免每次重新构图）
_AGENT = build_agent()

# 并发保护：模块级 _AGENT 与云端 LLM 客户端（OpenAI 兼容）并非为并发调用设计，
# 实测并发请求会把 worker 打挂（连接被重置）。开发期服务用一把锁把 Agent/LLM 调用
# 串行化（单飞），保证稳定性；代价是并发请求会排队，对 Demo 可接受。
_RUN_LOCK = threading.Lock()


def _candidate_to_dict(c: Candidate) -> dict:
    """候选 -> 可序列化 dict（过滤内部字段）。"""
    from agent.rag import _source_label
    return {
        "id": c.id,
        "source": c.source,
        "source_label": _source_label(c.source),   # 明显来源标记（豆瓣 / IMDb）
        "title": c.title,
        "year": c.year,
        "genres": c.genres,
        "country": c.country,
        "rating": c.rating,
        "rating_count": c.rating_count,
        "recall_source": c.recall_source,
        "score": round(c.score, 4),
    }


def _run(query: str, user_id: str, request_id: str = "") -> dict:
    """同步跑 Agent，返回完整 state（answer + ranked 候选）。

    护栏（P5 补充）：在锁之前先做内容安全判定——命中敏感/越域直接返回，
    不进入 Agent、不消耗云端额度。放行才进入串行化调用。
    """
    if not request_id:
        request_id = uuid.uuid4().hex[:12]

    g = check_guardrail(query)
    if g.action != "allow":
        logger_api.warning(
            "guardrail 拦截（不进 Agent、不耗额度）",
            extra={"event": "guardrail", "action": g.action, "category": g.category,
                   "user_id": user_id, "request_id": request_id, "query": query},
        )
        return {
            "answer": g.message,
            "intent": "guardrail",
            "items": [],
            "guardrail": {"action": g.action, "category": g.category, "reason": g.reason},
        }

    # 限流（P5 补充）：仅护栏放行、且非缓存命中的真实 LLM 调用才消耗令牌。
    # 放在锁之前——否则排队中的请求都占锁等待，限流失去意义。
    if settings.RATELIMIT_ENABLED:
        ok, retry_after = get_limiter().consume(user_id)
        if not ok:
            logger_api.warning(
                "rate_limited 限流触发",
                extra={"event": "rate_limited", "user_id": user_id,
                       "request_id": request_id, "retry_after": retry_after},
            )
            raise RateLimited(retry_after)

    with _RUN_LOCK:
        result = _AGENT.invoke({
            "user_id": user_id,
            "query": query,
            "request_id": request_id,
            "steps": [],
            "thinking": {},
        })
    ranked = result.get("ranked", []) or []
    return {
        "answer": result.get("answer", ""),
        "intent": result.get("intent", ""),
        "items": [_candidate_to_dict(c) for c in ranked],
        "sources": result.get("sources", {}),
        "steps": result.get("steps", []),
        "thinking": result.get("thinking", {}),
        "guardrail": {"action": "allow", "category": "ok", "reason": ""},
    }


class RecommendReq(BaseModel):
    query: str = Field(..., min_length=1, description="用户自然语言查询")
    user_id: str = "default"
    top_k: int = Field(default=10, ge=1, le=50)


class ChatReq(BaseModel):
    query: str = Field(..., min_length=1)
    user_id: str = "default"


@app.get("/health")
def health():
    # 暴露当前缓存后端（redis / local），便于运维与 P4.2 兜底观测
    return {
        "status": "ok",
        "service": "movie-agent-api",
        "version": app.version,
        "cache_backend": get_cache().backend(),
        "memory_backend": get_memory_store().backend(),
    }


@app.post("/recommend")
def recommend(req: RecommendReq, request: Request):
    # P4.2：缓存优先（Redis / 进程内兜底），命中直接返回
    cache = get_cache()
    ck = cache_key(req.query, req.user_id, str(req.top_k))
    hit = cache.get(ck)
    if hit is not None:
        out = dict(hit)
        out["cached"] = True
        out["cache_backend"] = cache.backend()
        logger_api.info(
            "recommend 缓存命中", extra={"event": "recommend", "user_id": req.user_id,
            "request_id": request.state.request_id, "cached": True,
            "cache_backend": cache.backend(), "intent": out.get("intent"),
            "count": out.get("count")},
        )
        return out

    request_id = request.state.request_id
    t0 = time.time()
    try:
        state = _run(req.query, req.user_id, request_id=request_id)
    except RateLimited as e:  # 限流触发：标准 429 + Retry-After
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试（rate limit）",
            headers={"Retry-After": str(max(1, int(e.retry_after)))},
        )
    except Exception as e:  # noqa: BLE001 - 接口层兜底，向上抛 500
        logger_api.exception(
            "recommend 失败", extra={"event": "api_error", "user_id": req.user_id,
            "request_id": request_id, "query": req.query},
        )
        raise HTTPException(status_code=500, detail=f"推荐失败: {e}")
    items = state["items"][: req.top_k]
    resp = {
        "query": req.query,
        "user_id": req.user_id,
        "request_id": request_id,
        "intent": state["intent"],
        "answer": state["answer"],
        "count": len(items),
        "items": items,
        "sources": state.get("sources", {}),
        "steps": state.get("steps", []),
        "thinking": state.get("thinking", {}),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "cached": False,
        "cache_backend": cache.backend(),
        "guardrail": state.get("guardrail", {"action": "allow", "category": "ok", "reason": ""}),
    }
    logger_api.info(
        "recommend 完成", extra={"event": "recommend", "user_id": req.user_id,
        "request_id": request_id, "cached": False, "cache_backend": cache.backend(),
        "intent": state["intent"], "count": len(items),
        "elapsed_ms": resp["elapsed_ms"],
        "guardrail_action": resp["guardrail"]["action"]},
    )
    cache.set(ck, resp)
    return resp


@app.post("/chat")
async def chat(req: ChatReq, request: Request):
    """流式对话：Agent 同步调用放到线程，避免阻塞事件循环；按句推送（伪流式体验）。"""
    request_id = request.state.request_id
    async def gen():
        try:
            state = await asyncio.to_thread(_run, req.query, req.user_id, request_id)
        except RateLimited as e:  # 流式接口无法回 429 状态码，推提示文本
            yield f"请求过于频繁，请 {max(1, int(e.retry_after))} 秒后再试。\n"
            return
        except Exception as e:  # noqa: BLE001
            logger_api.exception(
                "chat 失败", extra={"event": "api_error", "user_id": req.user_id,
                "request_id": request_id, "query": req.query},
            )
            yield f"抱歉，出错了：{e}\n"
            return
        answer = state.get("answer", "")
        # 按句切分，逐块推送（中文按 。！？\n 断句，避免生硬按字）
        for ch in re.split(r"(?<=[\n。！？])", answer):
            if ch:
                yield ch
                await asyncio.sleep(0.01)

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


def _sse(event: str, data: dict) -> str:
    """构造一条 SSE 事件（event: ...\\ndata: {...}\\n\\n）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/recommend/stream")
async def recommend_stream(req: RecommendReq, request: Request):
    """结构化流式推荐（SSE）：实时推送工具链步骤、大模型思考过程、答案 token，最后 done。

    事件协议（前端接入约定）：
      event: step     data: {order, node, kind, summary, detail, elapsed_ms, thinking?}
      event: thinking data: {content}            # 大模型深度思考过程 delta（generate 步骤）
      event: token    data: {content}            # 答案正文 token delta
      event: done     data: {intent, answer, items, sources, steps, thinking, elapsed_ms, request_id}
      event: error    data: {message}            # 护栏拦截 / 限流 / 运行异常

    与 /recommend 共用 Agent 与护栏/限流；缓存未启用（实时链路追踪）。并发仍走 _RUN_LOCK 单飞。
    """
    request_id = request.state.request_id
    t0 = time.time()

    # 护栏（锁前、不进 Agent）
    g = check_guardrail(req.query)
    if g.action != "allow":
        logger_api.warning("guardrail 拦截（stream）",
                           extra={"event": "guardrail", "action": g.action,
                                  "category": g.category, "user_id": req.user_id,
                                  "request_id": request_id, "query": req.query})

        async def _block():
            yield _sse("error", {"action": g.action, "category": g.category,
                                 "message": g.message})
        return StreamingResponse(_block(), media_type="text/event-stream")

    # 限流（锁前、仅真实 LLM 才扣令牌）
    if settings.RATELIMIT_ENABLED:
        ok, retry_after = get_limiter().consume(req.user_id)
        if not ok:
            logger_api.warning("rate_limited 限流触发（stream）",
                               extra={"event": "rate_limited", "user_id": req.user_id,
                                      "request_id": request_id, "retry_after": retry_after})

            async def _limited():
                yield _sse("error", {"action": "rate_limited",
                                     "message": f"请求过于频繁，请 {max(1, int(retry_after))} 秒后再试。"})
            return StreamingResponse(_limited(), media_type="text/event-stream")

    ev_q: "queue.Queue" = queue.Queue()
    init_state = {
        "user_id": req.user_id,
        "query": req.query,
        "request_id": request_id,
        "steps": [],
        "thinking": {},
        # 流式钩子：把步骤 / 思考 / token 增量实时推入事件队列（统一包装为 (type, data)）
        "_step_sink": lambda s: ev_q.put(("step", s)),
        "_token_sink": lambda t: ev_q.put(("token", t)),
        "_thinking_sink": lambda t: ev_q.put(("thinking", t)),
    }
    final: dict = {}

    def _run_agent():
        nonlocal final
        with _RUN_LOCK:
            try:
                final = _AGENT.invoke(init_state)
            except Exception as e:  # noqa: BLE001
                logger_api.exception("recommend/stream 运行异常",
                                     extra={"event": "api_error", "user_id": req.user_id,
                                            "request_id": request_id, "query": req.query})
                ev_q.put(("error", str(e)))

    th = threading.Thread(target=_run_agent, daemon=True)
    th.start()

    async def _gen():
        loop = asyncio.get_event_loop()
        while True:
            # 轮询事件队列（0.15s 超时），线程结束且队列空则收尾
            try:
                item = await loop.run_in_executor(None, lambda: ev_q.get(timeout=0.15))
            except queue.Empty:
                if not th.is_alive():
                    break
                continue
            if item is None:
                break
            etype, data = item
            if etype == "step":
                yield _sse("step", data)
            elif etype == "token":
                yield _sse("token", {"content": data})
            elif etype == "thinking":
                yield _sse("thinking", {"content": data})
            elif etype == "error":
                yield _sse("error", {"message": data})
                return
        th.join()
        ranked = final.get("ranked", []) or []
        items = [_candidate_to_dict(c) for c in ranked][: req.top_k]
        yield _sse("done", {
            "request_id": request_id,
            "intent": final.get("intent", ""),
            "answer": final.get("answer", ""),
            "items": items,
            "sources": final.get("sources", {}),
            "steps": final.get("steps", []),
            "thinking": final.get("thinking", {}),
            "elapsed_ms": int((time.time() - t0) * 1000),
        })

    return StreamingResponse(_gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
