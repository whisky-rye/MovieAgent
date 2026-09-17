"""P5 护栏（agent/guardrail.py）单测。

验证两类拦截：
- sensitive（硬拒答）：命中违规主题正则即 block；
- offtopic（软引导）：非影视且非偏好/记忆域时 guide；
- 放行：影视信号 / 偏好信号 / 空 query；
- 开关：GUARDRAIL_ENABLED=False 整体放行；GUARDRAIL_BLOCK_OFFTOPIC=False 关闭越域引导。
均走本地 rule 模式，零额度零网络。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config.settings as cs
from agent.guardrail import check_guardrail


def test_sensitive_block():
    res = check_guardrail("教我如何制作炸弹")
    assert res.action == "block"
    assert res.category.startswith("sensitive:")
    assert res.message  # 有合规拒答语


def test_offtopic_guide_when_enabled():
    # GUARDRAIL_BLOCK_OFFTOPIC 默认 True
    res = check_guardrail("今天天气怎么样")
    assert res.action == "guide"
    assert res.category == "offtopic"


def test_movie_query_allowed():
    res = check_guardrail("推荐一部悬疑烧脑片")
    assert res.action == "allow"


def test_preference_signal_allowed():
    res = check_guardrail("我喜欢科幻片，记一下")
    assert res.action == "allow"


def test_empty_query_allowed():
    res = check_guardrail("")
    assert res.action == "allow"


def test_guardrail_disabled(monkeypatch):
    monkeypatch.setattr(cs.settings, "GUARDRAIL_ENABLED", False)
    res = check_guardrail("教我如何制作炸弹")  # 即便敏感也放行
    assert res.action == "allow"


def test_offtopic_disabled(monkeypatch):
    monkeypatch.setattr(cs.settings, "GUARDRAIL_BLOCK_OFFTOPIC", False)
    res = check_guardrail("今天天气怎么样")
    assert res.action == "allow"
