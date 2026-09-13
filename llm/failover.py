"""云端模型顺序降级 + 免费额度熔断。

设计目标（用户要求）：
- 阿里云百炼提供了多种模型，按给定顺序 `settings.CLOUD_LLM_MODEL_LIST` 逐一尝试；
- 上一个模型请求失败 / 超时 / 中断等错误时，自动触发下一个模型，单次调用最多触发 4 次降级；
- 命中阿里云「免费额度用完即停」(HTTP 403 + AllocationQuota.FreeTierOnly.) 时，
  永久标记该模型为「熔断」，写入 `log/exhausted_models.json`，后续调用直接跳过，
  避免产生免费额度以外的费用；
- 其它错误（超时 / 网络中断 / 服务不可用）视为瞬时失败，触发下一个模型；
- 全部尝试均失败则抛出最后一个错误，由上层兜底。

运行期日志（自动追加，不入仓）：
- `log/model_failover.log`   人类可读的逐次尝试 / 熔断 / 放弃记录
- `log/exhausted_models.json` 已熔断模型持久化（跨进程/重启保留，避免反复踩坑）

重置：设置环境变量 `CLOUD_LLM_RESET_QUOTA=1` 后启动即清空熔断记录。
"""
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from config.settings import settings

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


class FailoverChatModel(BaseChatModel):
    """云端模型顺序降级包装器：对业务层透明，实现 BaseChatModel 接口。

    业务层照常调用 `.invoke([messages])` / `.invoke("字符串")`，由本类在内部
    按 `models` 顺序逐个尝试，遇到失败/熔断自动切换，最多触发 `max_fallbacks` 次降级。
    """

    models: List[str]
    max_fallbacks: int = 4          # 单次调用最多触发的降级次数（primary + 4 fallback）
    temperature: float = 0.3
    extra_body: Dict[str, Any] = {}
    request_timeout: int = 180

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "failover"

    def _build(self, model: str) -> ChatOpenAI:
        return ChatOpenAI(
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
            candidates = list(self.models)
        cap = self.max_fallbacks + 1          # primary + N 次降级
        tried = 0
        last_err = None
        for model in candidates:
            tried += 1
            try:
                result = self._try_one(model, messages, stop, run_manager, kwargs, async_mode)
                _log(f"OK model={model}")
                return result
            except Exception as e:
                if _is_quota_exhausted(e):
                    _EXHAUSTED.add(model)
                    _save_exhausted(_EXHAUSTED)
                    _log(f"QUOTA_EXHAUSTED model={model} -> 已标记熔断并持久化，后续跳过。reason={_safe(e)}")
                else:
                    _log(f"TRANSIENT_FAIL model={model} err={_safe(e)}")
                last_err = e
                if tried >= cap:
                    _log(f"MAX_FALLBACKS_EXCEEDED 已触发 {tried - 1} 次降级，放弃")
                    break
        raise last_err or RuntimeError("FailoverChatModel: 无可用模型（全部熔断或失败）")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._run_failover(messages, stop, run_manager, kwargs, async_mode=False)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return await self._run_failover(messages, stop, run_manager, kwargs, async_mode=True)
