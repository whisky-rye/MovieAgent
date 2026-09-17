"""LangGraph 编排状态（agent/state.py）单测。

验证：
- _merge_thinking 跨节点深合并（新增键 / 同键覆盖 / None 容错）；
- AgentState 作为 total=False 的 TypedDict 可作为普通 dict 使用；
- steps 使用的 operator.add reducer 的累加语义。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import operator

from agent.state import AgentState, _merge_thinking


def test_merge_thinking_new_keys():
    assert _merge_thinking({"route": "x"}, {"generate": "y"}) == {"route": "x", "generate": "y"}


def test_merge_thinking_override():
    assert _merge_thinking({"route": "a"}, {"route": "b"}) == {"route": "b"}


def test_merge_thinking_none_safe():
    assert _merge_thinking(None, {"k": 1}) == {"k": 1}
    assert _merge_thinking({"k": 1}, None) == {"k": 1}


def test_agent_state_is_typeddict():
    st: AgentState = {"query": "推荐科幻", "intent": "recommend"}
    assert st["query"] == "推荐科幻"
    assert st.get("intent") == "recommend"


def test_steps_reducer_add():
    assert operator.add([1, 2], [3]) == [1, 2, 3]
