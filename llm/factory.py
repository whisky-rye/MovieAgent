"""LLM 工厂：统一返回 strong/light 实例，业务层不硬编码模型名与 provider。"""
from langchain_core.language_models.chat_models import BaseChatModel
from llm.client import chat_strong, chat_light


def get_strong(**kw) -> BaseChatModel:
    return chat_strong(**kw)


def get_light(**kw) -> BaseChatModel:
    return chat_light(**kw)
