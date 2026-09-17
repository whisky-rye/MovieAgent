"""LLM 故障转移（llm/failover.py）单测。

验证 FailoverChatModel 的降级与熔断逻辑（不触发真实网络）：
- 顺序降级：上一模型瞬时失败 -> 切到下一模型成功；
- 免费额度耗尽（FreeTierOnly / 403）-> 标记熔断并跳过，后续调用直接跳过该模型；
- 全部失败 -> 抛出异常（本地兜底未配置时不联网）；
- 配额识别助手 _is_quota_exhausted 多形态命中。
用 monkeypatch 替换 _build 与全局熔断集合，避免写盘/联网。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import llm.failover as fo


class FakeResult:
    def __init__(self, text):
        self.generations = [SimpleNamespace(message=SimpleNamespace(content=text))]
        self.llm_output = {}


class FakeModel:
    def __init__(self, text=None, exc=None):
        self._text, self._exc = text, exc

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self._exc:
            raise self._exc
        return FakeResult(self._text)


@pytest.fixture
def patch_build(monkeypatch):
    state = {}

    def builder(self, model):
        b = state.get(model, "ok")
        if b == "ok":
            return FakeModel(text=f"ok-{model}")
        if b == "transient":
            return FakeModel(exc=RuntimeError("connection timeout"))
        if b == "quota":
            return FakeModel(exc=RuntimeError("AllocationQuota.FreeTierOnly. quota used up"))
        return FakeModel(text=f"ok-{model}")

    monkeypatch.setattr(fo.FailoverChatModel, "_build", builder)
    monkeypatch.setattr(fo, "_EXHAUSTED", set())        # 隔离全局熔断集合
    monkeypatch.setattr(fo, "_save_exhausted", lambda s: None)  # 不写盘
    patch_build.state = state
    return state


def test_sequential_fallback(patch_build):
    patch_build.clear()
    patch_build.update({"m1": "transient", "m2": "ok"})
    fm = fo.FailoverChatModel(models=["m1", "m2"], tier="strong")
    res = fm._generate([SimpleNamespace(content="hi")])
    assert res.generations[0].message.content == "ok-m2"


def test_quota_circuit_breaks(patch_build):
    patch_build.clear()
    patch_build.update({"m1": "quota", "m2": "ok"})
    fm = fo.FailoverChatModel(models=["m1", "m2"], tier="strong")
    res = fm._generate([SimpleNamespace(content="hi")])
    assert res.generations[0].message.content == "ok-m2"
    assert fo.is_model_exhausted("m1") is True  # 已熔断


def test_all_fail_raises(patch_build):
    patch_build.clear()
    patch_build.update({"m1": "transient", "m2": "transient"})
    fm = fo.FailoverChatModel(models=["m1", "m2"], tier="strong")  # 无本地兜底 -> 直接抛
    with pytest.raises(Exception):
        fm._generate([SimpleNamespace(content="hi")])


def test_max_fallbacks_cap(patch_build):
    patch_build.clear()
    patch_build.update({"m1": "transient"})
    fm = fo.FailoverChatModel(models=["m1"], max_fallbacks=0, tier="strong")
    with pytest.raises(Exception):
        fm._generate([SimpleNamespace(content="hi")])


def test_quota_detection():
    assert fo._is_quota_exhausted(RuntimeError("AllocationQuota.FreeTierOnly. quota done"))
    e = RuntimeError("forbidden")
    e.status_code = 403
    assert fo._is_quota_exhausted(e)
    assert not fo._is_quota_exhausted(RuntimeError("connection timeout"))
