"""云端模型顺序降级 + 免费额度熔断。

设计目标（用户要求）：
- 阿里云百炼提供了多种模型，按给定顺序（强档/弱档各自独立的 `settings.CLOUD_LLM_*_MODEL_LIST`）逐一尝试；
- 上一个模型请求失败 / 超时 / 中断等错误时，自动触发下一个模型，单次调用最多触发 4 次降级；
- 命中阿里云「免费额度用完即停」(HTTP 403 + AllocationQuota.FreeTierOnly.) 时，
  永久标记该模型为「熔断」，写入 `log/exhausted_models.json`，后续调用直接跳过，
  避免产生免费额度以外的费用；
- 其它错误（超时 / 网络中断 / 服务不可用）视为瞬时失败，触发下一个模型；
- 本档全部尝试均失败后：若 `APP_ENV=local` 且配置了 `local_fallback_model`，
  回退本地 Ollama（deepseek）兜底；若 `APP_ENV=production` 则直接抛出云端最后一个错误。

运行期日志（自动追加，不入仓）：
- `log/model_failover.log`   人类可读的逐次尝试 / 熔断 / 放弃记录
- `log/exhausted_models.json` 已熔断模型持久化（跨进程/重启保留，避免反复踩坑）

重置：设置环境变量 `CLOUD_LLM_RESET_QUOTA=1` 后启动即清空熔断记录。
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from config.settings import settings

logger = logging.getLogger("movie_agent.llm")

_LOG_DIR = Path(__file__).resolve().parents[1] / "log"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_EXHAUSTED_FILE = _LOG_DIR / "exhausted_models.json"
_FAILOVER_LOG = _LOG_DIR / "model_failover.log"


def _safe(err: Exception) -> str:
    return str(err).replace("\n", " ").strip()[:400]


def _log(line: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_FAILOVER_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
    except Exception:
        pass


def _load_exhausted() -> set:
    # 重置开关：启动即清空熔断记录（用于免费额度被平台重置后手动恢复）
    if os.environ.get("CLOUD_LLM_RESET_QUOTA") == "1":
        try:
            _EXHAUSTED_FILE.write_text("[]", encoding="utf-8")
        except Exception:
            pass
        return set()
    if _EXHAUSTED_FILE.exists():
        try:
            return set(json.loads(_EXHAUSTED_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_exhausted(s: set) -> None:
    try:
        _EXHAUSTED_FILE.write_text(
            json.dumps(sorted(s), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _extract_usage(result) -> dict:
    """从 LangChain ChatResult.llm_output 提取 token 用量（兼容 OpenAI/Anthropic 风格）。

    返回 prompt_tokens / completion_tokens / total_tokens / cached_tokens。
    cached_tokens 优先取 prompt_tokens_details.cached_tokens（OpenAI 提示缓存命中计数）。
    """
    try:
        llm_output = getattr(result, "llm_output", None) or {}
        u = llm_output.get("token_usage", {}) or {}
    except Exception:
        u = {}
    prompt = u.get("prompt_tokens")
    completion = u.get("completion_tokens")
    total = u.get("total_tokens")
    cached = 0
    details = u.get("prompt_tokens_details") or {}
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or 0
    if not cached:
        cached = u.get("cached_tokens") or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached,
    }


def _last_human_text(messages) -> str:
    """取最后一条 HumanMessage 的文本（作为「输入词」）。"""
    try:
        for m in reversed(messages or []):
            cls = m.__class__.__name__
            if getattr(m, "type", None) == "human" or cls == "HumanMessage":
                c = m.content
                if isinstance(c, list):
                    c = " ".join(str(x) for x in c)
                return str(c)
    except Exception:
        pass
    return ""


def _output_text(result) -> str:
    """取生成结果的文本（作为「输出词」）。"""
    try:
        gens = getattr(result, "generations", None) or []
        if gens:
            return str(gens[0].message.content)
    except Exception:
        pass
    return ""


def _log_llm_call(model, provider, tier, messages, result, latency_ms,
                  user_id="", request_id=""):
    """结构化记录一次云端/本地 LLM 调用：输入/输出 token、缓存 token、耗时、正文（受开关）。

    始终记录 token 计数与字符数；仅当 settings.LLM_LOG_BODY=True 时附带输入/输出原文，
    满足「请求体/响应体可配置开关」——开则记录、关则不具体记录。
    """
    usage = _extract_usage(result)
    in_text = _last_human_text(messages)
    out_text = _output_text(result)
    extra = {
        "event": "llm_call",
        "provider": provider,
        "tier": tier,
        "model": model,
        "user_id": user_id or "-",
        "request_id": request_id or "-",
        "input_tokens": usage["prompt_tokens"],
        "output_tokens": usage["completion_tokens"],
        "cached_tokens": usage["cached_tokens"],
        "cache_read": (usage["cached_tokens"] or 0) > 0,
        "total_tokens": usage["total_tokens"],
        "latency_ms": latency_ms,
        "input_chars": len(in_text),
        "output_chars": len(out_text),
    }
    if settings.LLM_LOG_BODY:
        extra["input_text"] = in_text
        extra["output_text"] = out_text
    logger.info(
        "llm_call provider=%s tier=%s model=%s in=%s out=%s cached=%s total=%s lat=%sms",
        provider, tier, model, usage["prompt_tokens"], usage["completion_tokens"],
        usage["cached_tokens"], usage["total_tokens"], latency_ms,
        extra=extra,
    )


_EXHAUSTED: set = _load_exhausted()


def is_model_exhausted(model: str) -> bool:
    """对外查询：某模型是否已被标记为免费额度耗尽（熔断）。"""
    return model in _EXHAUSTED


def reset_exhausted() -> None:
    """手动清空熔断记录（免费额度被平台重置后调用）。"""
    global _EXHAUSTED
    _EXHAUSTED = set()
    _save_exhausted(_EXHAUSTED)
    _log("RESET 已清空免费额度熔断记录")


def _is_quota_exhausted(err: Exception) -> bool:
    """识别阿里云「免费额度用完即停」：403 + AllocationQuota.FreeTierOnly.

    官方说明：免费额度用尽后平台自动停止服务，返回 403 错误
    AllocationQuota.FreeTierOnly.。此类错误不可逆（额度已尽），应永久熔断。
    """
    msg = _safe(err)
    for marker in ("AllocationQuota.FreeTierOnly", "FreeTierOnly"):
        if marker in msg:
            return True
    for attr in ("status_code", "http_status", "code"):
        v = getattr(err, attr, None)
        if v in (403, "403"):
            return True
    low = msg.lower()
    if "403" in msg and ("quota" in low or "freetier" in low):
        return True
    return False


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI 子类：把第三方网关（DashScope/阿里百炼等）返回的 reasoning_content
    补回 additional_kwargs。

    langchain-openai 1.x 默认只认官方 OpenAI，会丢弃 reasoning_content（见其文档
    warning）。但 DashScope 的 Qwen3 推理模型确实在 message.reasoning_content 返回
    思考过程。本子类在 _create_chat_result 里把它附加到 additional_kwargs——
    由于流式 chunk 也经 _get_generation_chunk_from_completion 复用该字段，
    因此「非流式 + 流式」两条路径都能拿到思考过程（供 /recommend 的 thinking 字段
    与 /recommend/stream 的 thinking 事件使用）。
    """

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        try:
            if isinstance(response, dict):
                choices = response.get("choices") or []
            else:
                choices = getattr(response, "choices", None) or []
            for gen, ch in zip(result.generations, choices):
                rc = self._extract_reasoning(ch)
                if rc:
                    gen.message.additional_kwargs["reasoning_content"] = rc
        except Exception:
            pass
        return result

    @staticmethod
    def _extract_reasoning(ch) -> str:
        """从 choices[i] 抽取 reasoning_content（兼容完整 message 与流式 delta）。"""
        if isinstance(ch, dict):
            m = ch.get("message") or ch.get("delta") or {}
            if isinstance(m, dict):
                return m.get("reasoning_content") or ""
            return getattr(m, "reasoning_content", None) or ""
        for attr in ("delta", "message"):
            obj = getattr(ch, attr, None)
            if obj is not None:
                rc = getattr(obj, "reasoning_content", None)
                if rc:
                    return rc
        return ""

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class,
                                           base_generation_info=None):
        """流式：把 delta.reasoning_content 补回 AIMessageChunk.additional_kwargs。

        langchain-openai 的流式路径走 _convert_chunk_to_generation_chunk（而非
        _create_chat_result），默认丢弃 reasoning_content；此处复用 _extract_reasoning
        思路，从 choices[0].delta 提取后挂到 chunk 上，使 /recommend/stream 的
        thinking 事件能拿到思考过程 delta。
        """
        gen = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info or {})
        if gen is None:
            return None
        try:
            choices = chunk.get("choices") if isinstance(chunk, dict) \
                else getattr(chunk, "choices", None) or []
            if choices:
                delta = choices[0].get("delta") if isinstance(choices[0], dict) \
                    else getattr(choices[0], "delta", None)
                rc = None
                if isinstance(delta, dict):
                    rc = delta.get("reasoning_content")
                else:
                    rc = getattr(delta, "reasoning_content", None)
                if rc and isinstance(gen.message, AIMessageChunk):
                    gen.message.additional_kwargs["reasoning_content"] = rc
        except Exception:
            pass
        return gen


class FailoverChatModel(BaseChatModel):
    """云端模型顺序降级包装器：对业务层透明，实现 BaseChatModel 接口。

    业务层照常调用 `.invoke([messages])` / `.invoke("字符串")`，由本类在内部
    按 `models` 顺序逐个尝试，遇到失败/熔断自动切换，最多触发 `max_fallbacks` 次降级。

    本地兜底：本档全部云端模型失败后，若 `local_fallback_model` 非空且 `settings.APP_ENV
    =='local'`，自动回退本地 Ollama（deepseek）生成；生产环境（`APP_ENV='production'`）
    不做本地兜底，直接抛出云端最后一个错误。
    """

    models: List[str]
    max_fallbacks: int = 4          # 单次调用最多触发的降级次数（primary + 4 fallback）
    temperature: float = 0.3
    extra_body: Dict[str, Any] = {}
    request_timeout: int = 180
    local_fallback_model: str = ""  # 本地兜底模型（Ollama）；空 = 不做本地兜底
    tier: str = "strong"            # strong（解释/对话）| light（意图/重排），用于日志区分
    user_id: str = ""               # 透传请求用户，便于按用户维度排查
    request_id: str = ""            # 透传请求 ID，便于链路串联

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "failover"

    def _build(self, model: str) -> ChatOpenAI:
        return ReasoningChatOpenAI(
            model=model,
            base_url=settings.CLOUD_LLM_BASE_URL,
            api_key=settings.CLOUD_LLM_API_KEY,
            temperature=self.temperature,
            request_timeout=self.request_timeout,
            max_retries=0,            # 重试/降级由本类统一控制
            extra_body=self.extra_body or None,
        )

    def _try_one(self, model: str, messages, stop, run_manager, kwargs, async_mode: bool):
        _log(f"TRY model={model} (async={async_mode})")
        llm = self._build(model)
        if async_mode:
            return llm._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        return llm._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _run_failover(self, messages, stop, run_manager, kwargs, async_mode: bool):
        candidates = [m for m in self.models if m not in _EXHAUSTED]
        if not candidates:
            _log("ALL_EXHAUSTED 全部模型已熔断，回退到完整列表兜底")
            logger.error("ALL_EXHAUSTED 全部模型已熔断，回退完整列表",
                         extra={"event": "all_exhausted", "tier": self.tier})
            candidates = list(self.models)
        cap = self.max_fallbacks + 1          # primary + N 次降级
        tried = 0
        last_err = None
        for model in candidates:
            tried += 1
            try:
                t0 = time.time()
                result = self._try_one(model, messages, stop, run_manager, kwargs, async_mode)
                latency_ms = int((time.time() - t0) * 1000)
                _log(f"OK model={model} lat={latency_ms}ms")
                _log_llm_call(model, "cloud", self.tier, messages, result, latency_ms,
                              self.user_id, self.request_id)
                return result
            except Exception as e:
                if _is_quota_exhausted(e):
                    _EXHAUSTED.add(model)
                    _save_exhausted(_EXHAUSTED)
                    _log(f"QUOTA_EXHAUSTED model={model} -> 已标记熔断并持久化，后续跳过。reason={_safe(e)}")
                    logger.error("QUOTA_EXHAUSTED 模型免费额度耗尽已熔断",
                                 extra={"event": "quota_exhausted", "model": model,
                                        "tier": self.tier, "reason": _safe(e)})
                else:
                    _log(f"TRANSIENT_FAIL model={model} err={_safe(e)}")
                    logger.warning("TRANSIENT_FAIL 模型瞬时失败触发降级",
                                   extra={"event": "model_fail", "model": model,
                                          "tier": self.tier, "err": _safe(e)})
                last_err = e
                if tried >= cap:
                    _log(f"MAX_FALLBACKS_EXCEEDED 已触发 {tried - 1} 次降级，放弃")
                    logger.error("MAX_FALLBACKS_EXCEEDED 降级次数用尽，放弃",
                                 extra={"event": "max_fallbacks", "tier": self.tier, "tried": tried})
                    break
        # 云端本档全部失败：按环境决定是否回退本地 Ollama
        if self.local_fallback_model and settings.APP_ENV.lower() == "local":
            try:
                _log(f"LOCAL_FALLBACK 云端全失败，回退本地 Ollama model={self.local_fallback_model}")
                logger.warning("LOCAL_FALLBACK 云端全失败，回退本地 Ollama",
                               extra={"event": "local_fallback", "model": self.local_fallback_model,
                                      "tier": self.tier})
                t0 = time.time()
                result = self._try_local(messages, stop, run_manager, kwargs, async_mode)
                latency_ms = int((time.time() - t0) * 1000)
                _log_llm_call(self.local_fallback_model, "ollama", self.tier, messages,
                              result, latency_ms, self.user_id, self.request_id)
                return result
            except Exception as le:
                _log(f"LOCAL_FALLBACK_FAIL model={self.local_fallback_model} err={_safe(le)}")
                logger.error("LOCAL_FALLBACK_FAIL 本地兜底也失败",
                             extra={"event": "local_fallback_fail", "model": self.local_fallback_model,
                                    "tier": self.tier, "err": _safe(le)})
                last_err = le  # 本地兜底也失败：以本地错误向上抛（云端错误已记录）
        raise last_err or RuntimeError("FailoverChatModel: 无可用模型（云端与本地兜底均失败）")

    def _try_local(self, messages, stop, run_manager, kwargs, async_mode: bool):
        """本地 Ollama 兜底：仅本地环境、云端全失败后被调用。think=False 提速且输出干净。"""
        from langchain_ollama import ChatOllama

        llm = ChatOllama(
            model=self.local_fallback_model,
            base_url=settings.OLLAMA_BASE_URL,
            temperature=self.temperature,
            think=False,
            request_timeout=180,
        )
        if async_mode:
            return llm._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        return llm._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._run_failover(messages, stop, run_manager, kwargs, async_mode=False)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return await self._run_failover(messages, stop, run_manager, kwargs, async_mode=True)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """同步流式降级：逐 chunk 透传首个可用模型；中途异常切下一模型，
        末次仍失败则回退非流式产出单个 chunk，保证「至少有一段回复」。"""
        candidates = [m for m in self.models if m not in _EXHAUSTED] or list(self.models)
        cap = self.max_fallbacks + 1          # primary + N 次降级
        tried = 0
        for model in candidates:
            tried += 1
            try:
                _log(f"TRY_STREAM model={model}")
                t0 = time.time()
                llm = self._build(model)
                for chunk in llm.stream(messages):
                    yield ChatGenerationChunk(message=chunk)
                _log(f"OK_STREAM model={model} lat={int((time.time() - t0) * 1000)}ms")
                return
            except Exception as e:
                if _is_quota_exhausted(e):
                    _EXHAUSTED.add(model)
                    _save_exhausted(_EXHAUSTED)
                    _log(f"QUOTA_EXHAUSTED model={model} -> 已标记熔断并持久化。reason={_safe(e)}")
                    logger.error("QUOTA_EXHAUSTED 模型免费额度耗尽已熔断",
                                 extra={"event": "quota_exhausted", "model": model,
                                        "tier": self.tier, "reason": _safe(e)})
                else:
                    _log(f"TRANSIENT_FAIL_STREAM model={model} err={_safe(e)}")
                    logger.warning("TRANSIENT_FAIL_STREAM 流式模型瞬时失败触发降级",
                                   extra={"event": "model_fail", "model": model,
                                          "tier": self.tier, "err": _safe(e)})
                if tried >= cap:
                    try:
                        res = self._run_failover(messages, stop, run_manager, kwargs, async_mode=False)
                        msg = res.generations[0].message if hasattr(res, "generations") else res
                        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content))
                    except Exception as le:
                        logger.error("STREAM_FALLBACK_FAIL 流式兜底也失败",
                                     extra={"event": "stream_fallback_fail", "tier": self.tier,
                                            "err": _safe(le)})
                    return

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        """异步流式降级：同 _stream，但使用 astream。"""
        candidates = [m for m in self.models if m not in _EXHAUSTED] or list(self.models)
        cap = self.max_fallbacks + 1
        tried = 0
        for model in candidates:
            tried += 1
            try:
                _log(f"TRY_ASTREAM model={model}")
                t0 = time.time()
                llm = self._build(model)
                async for chunk in llm.astream(messages):
                    yield ChatGenerationChunk(message=chunk)
                _log(f"OK_ASTREAM model={model} lat={int((time.time() - t0) * 1000)}ms")
                return
            except Exception as e:
                if _is_quota_exhausted(e):
                    _EXHAUSTED.add(model)
                    _save_exhausted(_EXHAUSTED)
                    _log(f"QUOTA_EXHAUSTED model={model} -> 已标记熔断并持久化。reason={_safe(e)}")
                    logger.error("QUOTA_EXHAUSTED 模型免费额度耗尽已熔断",
                                 extra={"event": "quota_exhausted", "model": model,
                                        "tier": self.tier, "reason": _safe(e)})
                else:
                    _log(f"TRANSIENT_FAIL_ASTREAM model={model} err={_safe(e)}")
                    logger.warning("TRANSIENT_FAIL_ASTREAM 流式模型瞬时失败触发降级",
                                   extra={"event": "model_fail", "model": model,
                                          "tier": self.tier, "err": _safe(e)})
                if tried >= cap:
                    try:
                        res = await self._run_failover(messages, stop, run_manager, kwargs, async_mode=True)
                        msg = res.generations[0].message if hasattr(res, "generations") else res
                        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content))
                    except Exception as le:
                        logger.error("STREAM_FALLBACK_FAIL 流式兜底也失败",
                                     extra={"event": "stream_fallback_fail", "tier": self.tier,
                                            "err": _safe(le)})
                    return
