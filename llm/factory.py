"""LLM 工厂：统一返回 strong/light 实例，业务层不硬编码模型名。"""
from langchain_ollama import ChatOllama
from llm.client import chat_strong, chat_light


def get_strong(**kw) -> ChatOllama:
    return chat_strong(**kw)


def get_light(**kw) -> ChatOllama:
    return chat_light(**kw)
