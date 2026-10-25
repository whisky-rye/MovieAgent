"""LLM 工厂：统一返回 light 实例，业务层不硬编码模型名与 provider。

注意：只有 get_light 有真实消费者（意图路由/重排/偏好抽取）；强档解释生成
走 llm.client.chat_strong 直调（rag/generate_explanation），此处不再保留
无消费者的 get_strong 包装（P2-13 死代码清理）。
"""
from langchain_core.language_models.chat_models import BaseChatModel
from llm.client import chat_light


def get_light(**kw) -> BaseChatModel:
    return chat_light(**kw)
