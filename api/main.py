"""P4.1 FastAPI 服务：对外暴露推荐 / 对话接口（开发期本地进程，无 Docker）。

- POST /recommend {query, user_id, top_k} -> 结构化候选列表 + 解说文案
- POST /chat     {query, user_id}          -> StreamingResponse 流式解说（逐句推送）
- GET  /health                            -> 健康检查

复用 agent.build_agent()，模块级只编译一次图；业务层（agent/retrieval/ranker）零改动。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import queue
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from api.routes.memory import router as memory_router  # G4：偏好记忆接口
from api.routes.sessions import router as sessions_router  # G3：会话管理接口
from api.routes.auth import router as auth_router  # G1：登录/注册/me
from api.routes.export import router as export_router  # 会话历史导出（zip）
from api.routes.share import router as share_router  # G-Share：会话共享 / 公开只读查看
from api.routes.poster import router as poster_router  # G-Poster：Top3 宣传海报查询/下载
from api.deps import get_current_user, CurrentUser  # G1：鉴权依赖（注入 user_id）
from agent.session_store import get_session_store  # G3：会话落库
from pydantic import BaseModel, Field

from config.logging_setup import setup_logging
from config.langsmith_setup import setup_langsmith, run_metadata
from agent.graph import build_agent
from agent.history_compress import build_history_context
from retrieval.candidates import Candidate
from api.cache import get_cache, cache_key
from api.ratelimit import get_limiter, RateLimited
from agent.memory import get_memory_store
from agent.guardrail import check_guardrail
from agent.poster import schedule_poster  # G-Poster：对话完成后异步生成 Top3 宣传海报
from config.settings import settings

setup_logging()  # 集中式结构化日志（JSON 行 + 滚动文件 + 专用 llm_calls.log）
_LS_ENABLED = setup_langsmith()  # P4.4：LangSmith 全链路追踪（按开关写入环境变量；缺 Key 自动退化关闭）
logger_api = logging.getLogger("movie_agent.api")


def validate_security_config() -> None:
    """生产环境安全红线校验（启动即 fail-fast，避免带病上线）。

    - APP_ENV=production 必须 AUTH_ENABLED=true（否则任何人可越权读取他人记忆）；
    - APP_ENV=production 必须配置 AUTH_SECRET 且非默认值（否则 token 签名可伪造）；
    - 非生产：AUTH_ENABLED=true 但缺 Secret 仅告警（便于本地调试）。
    """
    if settings.APP_ENV != "production":
        if settings.AUTH_ENABLED and not settings.AUTH_SECRET:
            logger_api.warning(
                "鉴权已启用（AUTH_ENABLED=true）但 AUTH_SECRET 为空，token 签名不安全！"
                "请在 .env 配置 AUTH_SECRET 后再启用鉴权。"
            )
        return
    # 生产环境：强制红线
    if not settings.AUTH_ENABLED:
        raise RuntimeError(
            "安全校验失败：APP_ENV=production 但 AUTH_ENABLED=false。"
            "生产环境必须开启鉴权（AUTH_ENABLED=true），否则存在越权风险。"
        )
    _weak = {"", "changeme", "change_me", "secret", "default", "your-secret-here"}
    if not settings.AUTH_SECRET or settings.AUTH_SECRET.strip() in _weak:
        raise RuntimeError(
            "安全校验失败：APP_ENV=production 但 AUTH_SECRET 未设置或仍为默认值。"
            "生产环境必须配置强随机密钥（建议 >=32 字节），否则 token 可被伪造。"
        )
    logger_api.info("生产安全校验通过：鉴权已强制、AUTH_SECRET 已配置。")


@asynccontextmanager
async def lifespan(app):  # noqa: ANN201
    validate_security_config()  # 启动即校验，不合规直接失败（fail-fast）
    yield


app = FastAPI(title="影片推荐智能体 API", version="0.1.0", lifespan=lifespan)

# G2 CORS：允许前端 dev server（默认 http://localhost:5173）跨域访问本 API。
# 开发期也可在 vite.config.ts 配 server.proxy 把 /api 代理到 :8000 实现零跨域；
# 此处中间件同时服务「前端独立域名 / 生产」场景，并自动处理 /recommend/stream 的 OPTIONS 预检。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # 导出接口：允许前端跨域读取附件名与导出统计
    expose_headers=["Content-Disposition", "X-Export-Sessions"],
)


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    """CSRF 双提交校验（仅保护基于 Cookie 的浏览器会话）。

    规则：
    - 带 `Authorization: Bearer` 的请求视为 API 客户端，跳过（不依赖 Cookie，无 CSRF 风险）；
    - 非安全方法（POST/PUT/DELETE/PATCH）且非 Bearer 会话：必须携带与 ma_csrf Cookie
      一致的 X-CSRF-Token 头，否则 403；
    - 豁免 /auth/*（登录/注册/登出是建立会话的入口，且非破坏性）；OPTIONS 预检直接放行。
    """
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return await call_next(request)
    if request.url.path.startswith("/auth/"):
        return await call_next(request)
    # 调试兼容模式（AUTH_ENABLED=false）：无真实 Cookie 鉴权会话，CSRF 无意义且会阻断
    # 「免登录」Demo 流程，故跳过校验；生产环境（AUTH_ENABLED=true，由启动校验强制）才启用。
    if not settings.AUTH_ENABLED:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return await call_next(request)  # API 客户端：跳过 CSRF
    expected = request.cookies.get(settings.CSRF_COOKIE_NAME)
    submitted = request.headers.get(settings.CSRF_HEADER_NAME)
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        logger_api.warning(
            "CSRF 校验失败",
            extra={"event": "csrf_blocked", "path": request.url.path,
                   "client": request.client.host if request.client else "-"},
        )
        return JSONResponse(
            status_code=403,
            content={"detail": "CSRF 校验失败：缺少或不匹配的 CSRF Token"},
        )
    return await call_next(request)

# G4 路由：偏好记忆对外接口（/memory/preferences 等）
app.include_router(memory_router)
# G3 路由：会话管理对外接口（/sessions 等）
app.include_router(sessions_router)
# G1 路由：登录/注册/me（无需鉴权即可访问，用于拿 token）
app.include_router(auth_router)
# 会话历史导出（GET /export/sessions -> zip 附件）
app.include_router(export_router)
# G-Share 会话共享：分享/取消（鉴权）/ 公开只读查看 /share/{token}（无鉴权）
app.include_router(share_router)
# G-Poster：Top3 宣传海报查询/下载（GET，CSRF 中间件天然豁免）
app.include_router(poster_router)


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
    # 初始化 request.state.user_id（供鉴权依赖/日志使用；鉴权模式下会被 token 内的权威值覆盖）
    if not hasattr(request.state, "user_id"):
        request.state.user_id = "default"
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
    "session_backend": get_session_store().backend(),
    "cache_backend": get_cache().backend(), "guardrail": settings.GUARDRAIL_ENABLED,
    "ratelimit": settings.RATELIMIT_ENABLED, "log_body": settings.LLM_LOG_BODY,
    "langsmith_tracing": _LS_ENABLED,
    "langsmith_project": settings.LANGSMITH_PROJECT or "-"},
)

if settings.AUTH_ENABLED and not settings.AUTH_SECRET:
    logger_api.warning(
        "鉴权已启用（AUTH_ENABLED=true）但 AUTH_SECRET 为空，token 签名不安全！"
        "请在 .env 配置 AUTH_SECRET 后再启用鉴权。"
    )

# 模块级编译一次：LangGraph 编译后的图对象是不可变的，对并发 `.invoke()` 安全
# （每次调用传入/返回独立 state，不在图对象上保留可变状态），故可跨请求复用，避免重复编译开销。
# 「每请求客户端」在业务层落地：llm/client.py 的 chat_strong/chat_light 每次调用都新建
# FailoverChatModel / ChatOllama（各自内含独立 httpx 客户端），DB 走 SQLAlchemy 连接池
# （每次请求从池取独立连接）——均不共享可变客户端状态，天然支持并发调用。
_AGENT = build_agent()

# 并发控制（P0 多 worker + 每请求客户端）：
# 不再用「全局串行锁」把全部请求单飞成 1 并发，改为「有界信号量」——允许至多
# MAX_CONCURRENT_RUNS 个 Agent/LLM 调用在单 worker 内真正并行，其余排队；
# 配合多 worker 部署，理论并发 ≈ workers × MAX_CONCURRENT_RUNS，P99 大幅下降。
# 取不到槽（过载）超 RUN_SLOT_TIMEOUT 秒则抛 _Overloaded，由端点返回 503。
_RUN_SEM = threading.Semaphore(settings.MAX_CONCURRENT_RUNS)


class _Overloaded(Exception):
    """单 worker 并发槽耗尽（过载）；端点据此返回 503，而非无限阻塞占用连接。"""


@contextmanager
def _run_slot():
    """获取一个并发执行槽；超时（RUN_SLOT_TIMEOUT）视为过载，抛出 _Overloaded。"""
    acquired = _RUN_SEM.acquire(timeout=settings.RUN_SLOT_TIMEOUT)
    if not acquired:
        raise _Overloaded()
    try:
        yield
    finally:
        _RUN_SEM.release()


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
        "douban_id": c.douban_id or "",
        "imdb_id": c.imdb_id or "",
    }


def _build_history(session_id: Optional[str]) -> str:
    """按 session_id 取出当前轮之前的会话消息，压缩为上下文串（多轮连贯 + 省 token）。

    失败仅返回 ""（不阻断主流程）；session_id 为空直接返回 ""。
    """
    if not session_id:
        return ""
    try:
        store = get_session_store()
        rec = store.get(session_id)
        if not rec:
            return ""
        msgs = rec.get("messages", []) or []
        prior = [m for m in msgs if isinstance(m, dict)]
        return build_history_context(prior, session_id)
    except Exception as e:  # noqa: BLE001
        logger_api.warning(
            "会话历史压缩失败（跳过历史上下文）：%s",
            e, extra={"event": "history_compress_error", "session_id": session_id},
        )
        return ""


def _run(query: str, user_id: str, request_id: str = "", session_id: Optional[str] = None) -> dict:
    """同步跑 Agent，返回完整 state（answer + ranked 候选）。

    护栏（P5 补充）：在锁之前先做内容安全判定——命中敏感/越域直接返回，
    不进入 Agent、不消耗云端额度。放行才进入串行化调用。
    """
    if not request_id:
        request_id = uuid.uuid4().hex[:12]

    # 历史上下文压缩（多轮连贯 + token 省流）；仅当存在会话时加载
    history = _build_history(session_id)

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

    with _run_slot():
        result = _AGENT.invoke(
            {
                "user_id": user_id,
                "query": query,
                "request_id": request_id,
                "history": history,
                "steps": [],
                "thinking": {},
            },
            config={"metadata": run_metadata(user_id, request_id)},
        )
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
    session_id: Optional[str] = Field(
        default=None, description="会话 ID（多会话管理；空=临时对话不落库）"
    )


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
    "session_backend": get_session_store().backend(),
    }


@app.post("/recommend")
def recommend(
    req: RecommendReq,
    request: Request,
    auth: CurrentUser = Depends(get_current_user),
):
    # P4.2：缓存优先（Redis / 进程内兜底），命中直接返回
    cache = get_cache()
    ck = cache_key(req.query, auth.user_id, str(req.top_k))
    hit = cache.get(ck)
    if hit is not None:
        out = dict(hit)
        out["cached"] = True
        out["cache_backend"] = cache.backend()
        logger_api.info(
            "recommend 缓存命中", extra={"event": "recommend", "user_id": auth.user_id,
            "request_id": request.state.request_id, "cached": True,
            "cache_backend": cache.backend(), "intent": out.get("intent"),
            "count": out.get("count")},
        )
        return out

    request_id = request.state.request_id
    t0 = time.time()
    try:
        state = _run(req.query, auth.user_id, request_id=request_id, session_id=req.session_id)
    except RateLimited as e:  # 限流触发：标准 429 + Retry-After
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试（rate limit）",
            headers={"Retry-After": str(max(1, int(e.retry_after)))},
        )
    except _Overloaded:  # 并发槽耗尽：过载 503
        logger_api.warning(
            "recommend 过载（并发槽耗尽）", extra={"event": "overloaded", "user_id": auth.user_id,
            "request_id": request_id, "concurrency": settings.MAX_CONCURRENT_RUNS},
        )
        raise HTTPException(status_code=503, detail="服务繁忙，请稍后再试（并发超限）")
    except Exception as e:  # noqa: BLE001 - 接口层兜底，向上抛 500
        logger_api.exception(
            "recommend 失败", extra={"event": "api_error", "user_id": auth.user_id,
            "request_id": request_id, "query": req.query},
        )
        raise HTTPException(status_code=500, detail=f"推荐失败: {e}")
    items = state["items"][: req.top_k]
    resp = {
        "query": req.query,
        "user_id": auth.user_id,
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
        "recommend 完成", extra={"event": "recommend", "user_id": auth.user_id,
        "request_id": request_id, "cached": False, "cache_backend": cache.backend(),
        "intent": state["intent"], "count": len(items),
        "elapsed_ms": resp["elapsed_ms"],
        "guardrail_action": resp["guardrail"]["action"]},
    )
    cache.set(ck, resp)
    return resp


@app.post("/chat")
async def chat(
    req: ChatReq,
    request: Request,
    auth: CurrentUser = Depends(get_current_user),
):
    """流式对话：Agent 同步调用放到线程，避免阻塞事件循环；按句推送（伪流式体验）。"""
    request_id = request.state.request_id
    async def gen():
        try:
            state = await asyncio.to_thread(_run, req.query, auth.user_id, request_id)
        except RateLimited as e:  # 流式接口无法回 429 状态码，推提示文本
            yield f"请求过于频繁，请 {max(1, int(e.retry_after))} 秒后再试。\n"
            return
        except _Overloaded:  # 并发槽耗尽：过载提示
            yield "服务繁忙，请稍后再试（并发超限）。\n"
            return
        except Exception as e:  # noqa: BLE001
            logger_api.exception(
                "chat 失败", extra={"event": "api_error", "user_id": auth.user_id,
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


def _persist_session(session_id: str, user_id: str, query: str, done_payload: dict) -> None:
    """G3 落库：把本轮 (user, assistant) 消息写入会话（带归属校验）。

    失败仅记 warning，绝不阻塞已返回的流式结果；存储为强 MySQL，异常会在此显式告警（无 file 兜底）。
    """
    try:
        store = get_session_store()
        if not store.assert_owner(session_id, user_id):
            logger_api.warning(
                "会话归属校验失败，跳过落库",
                extra={"event": "session_skip", "session_id": session_id, "user_id": user_id},
            )
            return
        now = datetime.now(timezone.utc).isoformat()
        store.append_message(
            session_id,
            {"role": "user", "content": query, "created_at": now},
        )
        store.append_message(
            session_id,
            {
                "role": "assistant",
                "content": done_payload.get("answer", ""),
                "items": done_payload.get("items", []),
                "steps": done_payload.get("steps", []),
                "thinking": done_payload.get("thinking", {}),
                "created_at": now,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger_api.warning("会话落库失败（不影响已返回的流式结果）：%s", e)


@app.post("/recommend/stream")
async def recommend_stream(
    req: RecommendReq,
    request: Request,
    auth: CurrentUser = Depends(get_current_user),
):
    """结构化流式推荐（SSE）：实时推送工具链步骤、大模型思考过程、答案 token，最后 done。

    事件协议（前端接入约定）：
      event: step     data: {order, node, kind, summary, detail, elapsed_ms, thinking?}
      event: thinking data: {content}            # 大模型深度思考过程 delta（generate 步骤）
      event: token    data: {content}            # 答案正文 token delta
      event: done     data: {intent, answer, items, sources, steps, thinking, elapsed_ms, request_id}
      event: error    data: {message}            # 护栏拦截 / 限流 / 运行异常

    与 /recommend 共用 Agent 与护栏/限流；缓存未启用（实时链路追踪）。并发走有界信号量
    _RUN_SEM（MAX_CONCURRENT_RUNS 真正并行，超出排队，过载 503），支持多 worker 部署。
    """
    request_id = request.state.request_id
    t0 = time.time()

    # 护栏（锁前、不进 Agent）
    g = check_guardrail(req.query)
    if g.action != "allow":
        logger_api.warning("guardrail 拦截（stream）",
                           extra={"event": "guardrail", "action": g.action,
                                  "category": g.category, "user_id": auth.user_id,
                                  "request_id": request_id, "query": req.query})

        async def _block():
            yield _sse("error", {"action": g.action, "category": g.category,
                                 "message": g.message})
        return StreamingResponse(_block(), media_type="text/event-stream")

    # 限流（锁前、仅真实 LLM 才扣令牌）
    if settings.RATELIMIT_ENABLED:
        ok, retry_after = get_limiter().consume(auth.user_id)
        if not ok:
            logger_api.warning("rate_limited 限流触发（stream）",
                               extra={"event": "rate_limited", "user_id": auth.user_id,
                                      "request_id": request_id, "retry_after": retry_after})

            async def _limited():
                yield _sse("error", {"action": "rate_limited",
                                     "message": f"请求过于频繁，请 {max(1, int(retry_after))} 秒后再试。"})
            return StreamingResponse(_limited(), media_type="text/event-stream")

    ev_q: "queue.Queue" = queue.Queue()
    init_state = {
        "user_id": auth.user_id,
        "query": req.query,
        "request_id": request_id,
        "history": _build_history(req.session_id),
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
        try:
            with _run_slot():
                final = _AGENT.invoke(
                    init_state,
                    config={"metadata": run_metadata(auth.user_id, request_id)},
                )
        except _Overloaded:
            logger_api.warning("recommend/stream 过载（并发槽耗尽）",
                               extra={"event": "overloaded", "user_id": auth.user_id,
                                      "request_id": request_id, "concurrency": settings.MAX_CONCURRENT_RUNS})
            ev_q.put(("error", "服务繁忙，请稍后再试（并发超限）。"))
        except Exception as e:  # noqa: BLE001
            logger_api.exception("recommend/stream 运行异常",
                                 extra={"event": "api_error", "user_id": auth.user_id,
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
        done_payload = {
            "request_id": request_id,
            "intent": final.get("intent", ""),
            "answer": final.get("answer", ""),
            "items": items,
            "sources": final.get("sources", {}),
            "steps": final.get("steps", []),
            "thinking": final.get("thinking", {}),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
        yield _sse("done", done_payload)
        # G3：done 后落会话（带 session_id 且归属校验通过才写；异步线程不阻塞流式返回）
        if req.session_id:
            await asyncio.to_thread(
                _persist_session, req.session_id, auth.user_id, req.query, done_payload
            )
            # G-Poster：异步（守护线程）生成 Top3 宣传海报，状态写入 manifest 供前端轮询
            try:
                schedule_poster(
                    req.session_id, auth.user_id, request_id,
                    done_payload.get("items", []), req.query,
                )
            except Exception as e:
                logger_api.warning("海报调度失败（不影响主链路）：%s", e)

    return StreamingResponse(_gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import os

    import uvicorn

    # 多 worker 部署（P0 多 worker + 每请求客户端）：
    # 单 worker 内由 _RUN_SEM 控制有界并发；跨 worker 的共享状态（限流/缓存/记忆/会话）
    # 走 Redis / MySQL 后端（不可达自动回退），因此多进程安全。
    # 命令行 `--workers N` 优先；否则读取 .env 的 UVICORN_WORKERS（默认 1）。
    workers = int(os.environ.get("UVICORN_WORKERS", "") or settings.UVICORN_WORKERS or 1)
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=workers)
