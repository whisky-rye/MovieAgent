"""LLM 接入层统一出口。"""
from llm.client import chat_strong, chat_light
from llm.factory import get_strong, get_light

__all__ = ["chat_strong", "chat_light", "get_strong", "get_light"]
