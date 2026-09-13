"""本地 Ollama LLM 客户端封装（强/弱两档）。

开发期走本地 Ollama（settings 已锁端口 11434，含真实模型）。
注意：仅生成调用走 ChatOllama；嵌入走 vector/embed_client（直连 /api/embed 绕过端口坑）。
"""
from langchain_ollama import ChatOllama
from config.settings import settings


def chat_strong(**kwargs) -> ChatOllama:
    """强档：解释/对话/工具调用（默认 qwen3.5:9b）。"""
    return ChatOllama(
        model=settings.LLM_STRONG,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=0.3,
        **kwargs,
    )


def chat_light(**kwargs) -> ChatOllama:
    """弱档：意图路由/重排（较小模型，默认 deepseek-r1:7b）。"""
    return ChatOllama(
        model=settings.LLM_LIGHT,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=0.1,
        **kwargs,
    )
