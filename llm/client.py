"""LLM 客户端封装：本地优先（Ollama），云端可切换（OpenAI 兼容）。

开关：settings.LLM_PROVIDER = "ollama" | "cloud"
- ollama：本地 Ollama（端口 11434），强制 think=False 关闭思维链，提速且输出干净
- cloud ：OpenAI 兼容网关（七牛/硅基流动/阿里百炼/火山方舟等），改 base_url+api_key 即切换，业务层零改

注意：仅生成调用走此处；嵌入走 vector/embed_client（直连 /api/embed 绕过端口坑）。
"""
import json
from langchain_ollama import ChatOllama
from config.settings import settings
from llm.failover import FailoverChatModel

_PROVIDER = (settings.LLM_PROVIDER or "ollama").lower()


def _is_cloud() -> bool:
    return _PROVIDER == "cloud"


def _cloud_extra_body() -> dict:
    """解析供应商特有参数（JSON 字符串），如 {"think": false}。无效则空 dict。"""
    raw = (settings.CLOUD_LLM_EXTRA_BODY or "").strip()
    if not raw:
        return {}
    try:
        body = json.loads(raw)
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def chat_strong(**kwargs):
    """强档：解释/对话/工具调用。

    本地 qwen3.5 / deepseek-r1 默认输出思维链（<think>...</think>），
    会导致 JSON 解析偏移、自然语言答案被 strip_think 清空、且 CPU 推理极慢。
    ollama 模式强制 think=False（Ollama 0.5+ 支持）；云端模式 reasoning 在
    reasoning_content，content 已是最终答案，strip_think 幂等清洗即可。
    """
    if _is_cloud():
        return FailoverChatModel(
            models=settings.cloud_model_chain,
            max_fallbacks=settings.CLOUD_LLM_MAX_FALLBACKS,
            temperature=0.3,
            extra_body=_cloud_extra_body(),
            **kwargs,
        )
    return ChatOllama(
        model=settings.LLM_STRONG,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=0.3,
        think=False,
        request_timeout=180,
        **kwargs,
    )


def chat_light(**kwargs):
    """弱档：意图路由/重排。

    ollama 模式同样关闭思维链（见 chat_strong 说明），意图解析毫秒级。
    """
    if _is_cloud():
        return FailoverChatModel(
            models=settings.cloud_model_chain,
            max_fallbacks=settings.CLOUD_LLM_MAX_FALLBACKS,
            temperature=0.1,
            extra_body=_cloud_extra_body(),
            **kwargs,
        )
    return ChatOllama(
        model=settings.LLM_LIGHT,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=0.1,
        think=False,
        request_timeout=180,
        **kwargs,
    )


def strip_think(text: str) -> str:
    """清洗推理模型（deepseek-r1 / qwen3 等）常见的 <think>...</think> 标签。

    对云端 API（reasoning 在 reasoning_content，content 已是最终答案）幂等，安全通用。
    """
    import re
    return re.sub(r"<think>[\s\S]*?</think>", "", text or "").strip()
