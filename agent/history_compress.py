"""会话历史摘要压缩（Token 优化核心抓手）。

问题：多轮对话若把全部历史原文塞进生成 prompt，token 随轮次线性膨胀，
既贵又挤占上下文窗口，还可能冲淡当前轮需求。

策略（分层压缩）：
- 历史较短（先验消息数 <= HISTORY_THRESHOLD）：全部保留原文，不调用 LLM（零成本）。
- 超出阈值：把"较早的轮次"用轻量 LIGHT 模型压缩成 1~2 句摘要；"最近 KEEP_RECENT 轮"
  保留原文（细节不失真，便于连续指代/追问）。
- 摘要缓存：进程内按 session_id 缓存"已摘要覆盖的旧片段长度"，旧片段未增长时直接复用，
  避免每轮重复摘要（整体摘要调用次数 = O(轮次) 且每次文本很短）。
- 任何摘要失败都回退"原文拼接"，绝不因压缩而丢信息或报错。

接入点：api/main._run（及 /recommend/stream）按 session_id 取出先验消息 → build_history_context
→ 注入生成 prompt（见 agent/rag._generate_rich 的 {history} 占位）。
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("movie_agent.history")

HISTORY_THRESHOLD = 6   # 先验消息数 <= 此值：全部保留原文
KEEP_RECENT = 4         # 超出阈值后，最近 N 轮保留原文，更早的压缩为摘要

# 进程内摘要缓存：session_id -> (已覆盖的旧片段长度, 摘要文本)
_SUMMARY_CACHE: Dict[str, Tuple[int, str]] = {}
_SUMMARY_CACHE_LOCK = threading.Lock()


def _format_turns(turns: List[dict]) -> str:
    """把若干 (role, content) 消息拼成可读对话片段。"""
    lines: List[str] = []
    for m in turns:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        who = "用户" if role == "user" else ("助手" if role == "assistant" else (role or "?"))
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{who}：{content}")
    return "\n".join(lines)


def _summarize_old(old: List[dict]) -> str:
    """用轻量模型把较早轮次压成 1~2 句摘要；失败回退原文。"""
    try:
        from langchain_core.messages import HumanMessage
        from llm.client import chat_light, strip_think

        convo = _format_turns(old)
        if not convo:
            return ""
        prompt = (
            "请用 2-3 句中文概括以下对话要点（用户偏好、已推荐/讨论过的影片、"
            "尚未满足的需求、明确拒绝过的类型），不要列清单，不要编造新信息：\n\n"
            + convo
        )
        llm = chat_light(user_id="system", request_id="history-summary")
        resp = llm.invoke([HumanMessage(content=prompt)])
        s = strip_think(getattr(resp, "content", "")).strip()
        if s:
            return "前文对话概要：" + s
    except Exception as exc:  # 摘要失败不应阻断主流程
        logger.warning("history summary failed (fallback to plain): %s", exc)
    return _format_turns(old)


def _get_or_summarize(old: List[dict], session_id: Optional[str]) -> str:
    """带缓存的摘要：旧片段未增长则复用。"""
    key = session_id or "_nosession"
    old_len = len(old)
    with _SUMMARY_CACHE_LOCK:
        cached = _SUMMARY_CACHE.get(key)
    if cached is not None and cached[0] == old_len:
        return cached[1]
    text = _summarize_old(old)
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE[key] = (old_len, text)
    return text


def build_history_context(
    prior: List[dict], session_id: Optional[str] = None
) -> str:
    """把先验消息（当前轮之前的所有 (role,content)）压缩为紧凑上下文串。

    返回 "" 表示无历史（生成 prompt 中不插入历史块）。
    """
    if not prior:
        return ""
    if len(prior) <= HISTORY_THRESHOLD:
        return _format_turns(prior)
    recent = prior[-KEEP_RECENT:]
    old = prior[:-KEEP_RECENT]
    summary = _get_or_summarize(old, session_id)
    return f"{summary}\n\n最近几轮对话：\n" + _format_turns(recent)
