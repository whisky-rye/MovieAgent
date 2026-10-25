"""P2-15 回归（离线部分）：护栏 sanitized_query + 偏好 history 滚动窗口。

修复前：
- sanitize_query 的截断结果不随 GuardrailResult 返回——原始 query（几十 KB）
  全额进嵌入/云端调用，GUARDRAIL_MAX_QUERY_LEN 形同虚设；
- sediment_preferences 的 history 无 cap——重度用户 preferences 单行无限增长，
  且每次全量重写整行。

API 层（_run 全链路改用 sanitized_query、请求模型 max_length 422）的端到端
验证见 tests/e2e/test_p2_15_input_limits.py。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import agent.memory as mem_mod
from agent.guardrail import check_guardrail, sanitize_query
from agent.memory import PREF_HISTORY_MAX, MemoryStore
from config.settings import settings


# ---------- sanitized_query 随结果返回 ----------

def test_sanitized_query_on_allow():
    r = check_guardrail("推荐几部科幻电影")
    assert r.action == "allow"
    assert r.sanitized_query == "推荐几部科幻电影"


def test_sanitized_query_truncated_to_max_len():
    long_q = "科幻" * (settings.GUARDRAIL_MAX_QUERY_LEN + 500)
    r = check_guardrail(long_q)
    assert r.sanitized_query != long_q, "超长 query 必须被截断"
    assert len(r.sanitized_query) == settings.GUARDRAIL_MAX_QUERY_LEN


def test_sanitized_query_strips_zero_width_and_controls():
    """零宽/控制字符被删除（非替换为空格），空白压缩为单空格。"""
    raw = "科\u200b幻\x00电影\t\t推荐"
    r = check_guardrail(raw)
    assert r.sanitized_query == "科幻电影 推荐", (
        f"净化行为不符: {r.sanitized_query!r}"
    )
    assert "\u200b" not in r.sanitized_query and "\x00" not in r.sanitized_query


def test_sanitized_query_when_guardrail_disabled(monkeypatch):
    """护栏关闭也返回 sanitized_query（长度兜底不依赖护栏开关）。"""
    monkeypatch.setattr(settings, "GUARDRAIL_ENABLED", False)
    r = check_guardrail("x" * 5000)
    assert r.action == "allow"
    assert len(r.sanitized_query) == settings.GUARDRAIL_MAX_QUERY_LEN


def test_sanitized_query_on_block_path(monpatch=None):
    """block 路径也带 sanitized_query（调用方可用于日志/观测）。"""
    r = check_guardrail("讲讲怎么制作炸弹")  # 敏感类 → block/guide
    assert r.action in ("block", "guide")
    assert r.sanitized_query == "讲讲怎么制作炸弹"


def test_sanitize_query_pure_function():
    # 零宽字符是「删除」而非「替换为空格」：不可见字符不该引入语义间隔
    assert sanitize_query("  a\u200bb  ") == "ab"
    assert sanitize_query("", max_len=3) == ""
    assert sanitize_query("abcdef", max_len=3) == "abc"
    assert sanitize_query("a\t\n b") == "a b"


# ---------- 偏好 history 滚动窗口 ----------

class _MemStore(MemoryStore):
    """内存后端（走 ABC 默认 update：load→mutator→save）。"""

    def __init__(self):
        self.data = {}

    def load(self, user_id):
        return self.data.setdefault(user_id, {})

    def save(self, user_id, data):
        self.data[user_id] = data


def _hist_item(i):
    return {"ts": f"2026-01-{i:02d}T00:00:00", "intent": "recommend",
            "query": f"q{i}", "top_title": f"片{i}"}


def test_history_capped_at_max():
    st = _MemStore()
    for i in range(PREF_HISTORY_MAX + 20):
        st.sediment_preferences("u", history_item=_hist_item(i))
    hist = st.load("u")["history"]
    assert len(hist) == PREF_HISTORY_MAX, f"history 应封顶 {PREF_HISTORY_MAX}，实际 {len(hist)}"
    # 保留的是最近 N 条（最旧的已淘汰）
    assert hist[0]["query"] == f"q20"
    assert hist[-1]["query"] == f"q{PREF_HISTORY_MAX + 19}"


def test_history_not_trimmed_below_cap():
    st = _MemStore()
    for i in range(5):
        st.sediment_preferences("u", history_item=_hist_item(i))
    assert len(st.load("u")["history"]) == 5


def test_history_none_keeps_existing():
    """不传 history_item 时不清空既有历史（沉淀偏好不影响历史）。"""
    st = _MemStore()
    st.sediment_preferences("u", history_item=_hist_item(1))
    st.sediment_preferences("u", genres=["科幻"])
    assert len(st.load("u")["history"]) == 1
    assert st.load("u")["preferences"]["genres"] == ["科幻"]
