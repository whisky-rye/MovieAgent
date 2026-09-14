"""P4.1 FastAPI 服务：对外暴露推荐 / 对话接口（开发期本地进程，无 Docker）。

- POST /recommend {query, user_id, top_k} -> 结构化候选列表 + 解说文案
- POST /chat     {query, user_id}          -> StreamingResponse 流式解说（逐句推送）
- GET  /health                            -> 健康检查

复用 agent.build_agent()，模块级只编译一次图；业务层（agent/retrieval/ranker）零改动。
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.graph import build_agent
from retrieval.candidates import Candidate
from api.cache import get_cache, cache_key
from api.ratelimit import get_limiter, RateLimited
from agent.memory import get_memory_store
from config.settings import settings

app = FastAPI(title="影片推荐智能体 API", version="0.1.0")

# 模块级编译一次，所有请求复用（避免每次重新构图）
_AGENT = build_agent()

# 并发保护：模块级 _AGENT 与云端 LLM 客户端（OpenAI 兼容）并非为并发调用设计，
# 实测并发请求会把 worker 打挂（连接被重置）。开发期服务用一把锁把 Agent/LLM 调用
# 串行化（单飞），保证稳定性；代价是并发请求会排队，对 Demo 可接受。
_RUN_LOCK = threading.Lock()


def _candidate_to_dict(c: Candidate) -> dict:
    """候选 -> 可序列化 dict（过滤内部字段）。"""
    return {
        "id": c.id,
        "source": c.source,
        "title": c.title,
        "year": c.year,
        "genres": c.genres,
        "country": c.country,
        "rating": c.rating,
        "rating_count": c.rating_count,
        "recall_source": c.recall_source,
        "score": round(c.score, 4),
    }


def _run(query: str, user_id: str) -> dict:
    """同步跑 Agent，返回完整 state（answer + ranked 候选）。

    护栏（P5 补充）：在锁之前先做内容安全判定——命中敏感/越域直接返回，
    不进入 Agent、不消耗云端额度。放行才进入串行化调用。
    """
    from agent.guardrail import check_guardrail

    g = check_guardrail(query)
    if g.action != "allow":
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
            raise RateLimited(retry_after)

    with _RUN_LOCK:
        result = _AGENT.invoke({"user_id": user_id, "query": query})
    ranked = result.get("ranked", []) or []
    return {
        "answer": result.get("answer", ""),
        "intent": result.get("intent", ""),
        "items": [_candidate_to_dict(c) for c in ranked],
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
def recommend(req: RecommendReq):
    # P4.2：缓存优先（Redis / 进程内兜底），命中直接返回
    cache = get_cache()
    ck = cache_key(req.query, req.user_id, str(req.top_k))
    hit = cache.get(ck)
    if hit is not None:
        out = dict(hit)
        out["cached"] = True
        out["cache_backend"] = cache.backend()
        return out

    t0 = time.time()
    try:
        state = _run(req.query, req.user_id)
    except RateLimited as e:  # 限流触发：标准 429 + Retry-After
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试（rate limit）",
            headers={"Retry-After": str(max(1, int(e.retry_after)))},
        )
    except Exception as e:  # noqa: BLE001 - 接口层兜底，向上抛 500
        raise HTTPException(status_code=500, detail=f"推荐失败: {e}")
    items = state["items"][: req.top_k]
    resp = {
        "query": req.query,
        "user_id": req.user_id,
        "intent": state["intent"],
        "answer": state["answer"],
        "count": len(items),
        "items": items,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "cached": False,
        "cache_backend": cache.backend(),
        "guardrail": state.get("guardrail", {"action": "allow", "category": "ok", "reason": ""}),
    }
    cache.set(ck, resp)
    return resp


@app.post("/chat")
async def chat(req: ChatReq):
    """流式对话：Agent 同步调用放到线程，避免阻塞事件循环；按句推送（伪流式体验）。"""
    async def gen():
        try:
            state = await asyncio.to_thread(_run, req.query, req.user_id)
        except RateLimited as e:  # 流式接口无法回 429 状态码，推提示文本
            yield f"请求过于频繁，请 {max(1, int(e.retry_after))} 秒后再试。\n"
            return
        except Exception as e:  # noqa: BLE001
            yield f"抱歉，出错了：{e}\n"
            return
        answer = state.get("answer", "")
        # 按句切分，逐块推送（中文按 。！？\n 断句，避免生硬按字）
        for ch in re.split(r"(?<=[\n。！？])", answer):
            if ch:
                yield ch
                await asyncio.sleep(0.01)

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
