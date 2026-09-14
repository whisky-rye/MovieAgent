"""Agent 编排层状态结构（LangGraph 在节点间传递的状态）。

设计要点：
- 字段用 TypedDict(total=False)，节点只更新自己关心的字段。
- messages 用 add_messages reducer 自动合并（LangGraph 约定）。
- candidates / ranked 存 retrieval.Candidate 对象列表（节点间共享，不序列化）。
"""
from typing import TypedDict, Annotated, Optional, Any
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from retrieval.candidates import Candidate


class AgentState(TypedDict, total=False):
    user_id: str
    request_id: str                # 单次请求链路 ID（用于云端 API 日志串联）
    query: str                      # 用户原始输入
    intent: str                     # recommend | similar | board | explain | preference
    slots: dict                     # 意图解析出的结构化槽位（query/genres/country/...）
    plan: list[str]                 # 规划要执行的工具名列表
    messages: Annotated[list[BaseMessage], add_messages]
    candidates: dict[str, list[Candidate]]   # 各召回来源 -> 候选
    ranked: list[Candidate]         # 融合+重排后的最终候选
    answer: str                     # 生成的解释性答案
    sources: dict                    # 来源元信息（build_source_meta 产出；含对话一句话 + API 详细分源）
    error: str                      # 出错信息（用于优雅降级）
