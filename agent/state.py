"""Agent 编排层状态结构（LangGraph 在节点间传递的状态）。

设计要点：
- 字段用 TypedDict(total=False)，节点只更新自己关心的字段。
- messages 用 add_messages reducer 自动合并（LangGraph 约定）。
- candidates / ranked 存 retrieval.Candidate 对象列表（节点间共享，不序列化）。
- steps / thinking 用 reducer 累加（节点各自返回增量，LangGraph 自动合并），
  用于「工具链可视化」与「大模型深度思考过程」对外输出。
- _step_sink / _token_sink / _thinking_sink 为流式透传钩子（callable），仅在
  /recommend/stream 等流式场景注入；非流式接口不传，节点读到 None 即跳过。
"""
import operator
from typing import TypedDict, Annotated, Optional, Any, Callable
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from retrieval.candidates import Candidate


def _merge_thinking(a: dict, b: dict) -> dict:
    """thinking 字段 reducer：跨节点深合并（route/generate 各写各的键）。"""
    out = dict(a or {})
    out.update(b or {})
    return out


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
    # ---- 可观测：工具链步骤 + 大模型深度思考（v2.13） ----
    steps: Annotated[list, operator.add]      # 各节点执行步骤（reducer 累加）
    thinking: Annotated[dict, _merge_thinking]  # 各 LLM 步骤的推理过程 {route, generate}
    # ---- 流式透传钩子（可选；非流式场景为 None） ----
    _step_sink: Any                 # callable(step_dict)  实时推送步骤事件
    _token_sink: Any                # callable(text)       实时推送答案 token
    _thinking_sink: Any             # callable(text)       实时推送思考过程 delta
