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
from langchain_core.messages import AIMessageChunk

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
    # marker 命中 → 熔断（不可逆的额度故障）
    assert fo._is_quota_exhausted(RuntimeError("AllocationQuota.FreeTierOnly. quota done"))
    assert fo._is_quota_exhausted(RuntimeError("error 403: FreeTierOnly"))
    # P0-5：裸 403（Key 失效 / 区域不支持 / 内容审核 / 网关）不是额度信号，
    # 不得再被当成不可逆计费故障永久熔断（曾因此误熔断，只能手动重置）
    e = RuntimeError("forbidden")
    e.status_code = 403
    assert not fo._is_quota_exhausted(e)
    e2 = RuntimeError("region not supported")
    e2.code = "403"
    assert not fo._is_quota_exhausted(e2)
    # 403 + 额度语义文本同时出现 → 仍判熔断
    assert fo._is_quota_exhausted(RuntimeError("HTTP 403: free tier quota exceeded"))
    assert not fo._is_quota_exhausted(RuntimeError("connection timeout"))
    assert not fo._is_quota_exhausted(RuntimeError("401 unauthorized"))


# ---------------------------------------------------------------------------
# 流式降级：P0-6 防「半截拼接」
# ---------------------------------------------------------------------------

class _FakeStreamModel:
    """流式假模型：按序产出 chunks；raise_after=N 表示产出 N 个 chunk 后抛异常
    （raise_after=0 表示未产出任何 chunk 即失败）。"""

    def __init__(self, chunks, raise_after=None):
        self._chunks = list(chunks)
        self._raise_after = raise_after

    def stream(self, messages):
        if self._raise_after == 0:
            raise RuntimeError("stream broke before any chunk")
        for i, c in enumerate(self._chunks):
            yield AIMessageChunk(content=c)
            if self._raise_after is not None and i + 1 >= self._raise_after:
                raise RuntimeError("stream broke midway")


class _AFakeStreamModel:
    """异步流式假模型：同 _FakeStreamModel。"""

    def __init__(self, chunks, raise_after=None):
        self._chunks = list(chunks)
        self._raise_after = raise_after

    async def astream(self, messages):
        if self._raise_after == 0:
            raise RuntimeError("stream broke before any chunk")
        for i, c in enumerate(self._chunks):
            yield AIMessageChunk(content=c)
            if self._raise_after is not None and i + 1 >= self._raise_after:
                raise RuntimeError("stream broke midway")


def test_stream_partial_failure_does_not_splice(monkeypatch):
    """P0-6：流式中途失败（已产出 chunk）→ 不切换模型、不追加全文，仅提示中断。"""
    state = {"m1": (["第一句。", "第二句。"], 2), "m2": (["模型B的完整回答。"], None)}

    def builder(self, model):
        chunks, raise_after = state.get(model, (["ok"], None))
        return _FakeStreamModel(chunks, raise_after)

    monkeypatch.setattr(fo.FailoverChatModel, "_build", builder)
    monkeypatch.setattr(fo, "_EXHAUSTED", set())
    monkeypatch.setattr(fo, "_save_exhausted", lambda s: None)
    fm = fo.FailoverChatModel(models=["m1", "m2"], max_fallbacks=4, tier="strong")
    chunks = [c.message.content for c in fm._stream([SimpleNamespace(content="hi")])]
    assert chunks == ["第一句。", "第二句。", "\n\n（模型响应中途中断，以上为部分内容）"]
    assert not any("模型B" in c for c in chunks)  # 绝不出现第二个模型的全文


def test_stream_early_failure_still_falls_back(monkeypatch):
    """未产出任何 chunk 即失败 → 保持原有降级行为（切到下一条模型）。"""
    state = {"m1": ([], 0), "m2": (["m2 的回答。"], None)}

    def builder(self, model):
        chunks, raise_after = state.get(model, (["ok"], None))
        return _FakeStreamModel(chunks, raise_after)

    monkeypatch.setattr(fo.FailoverChatModel, "_build", builder)
    monkeypatch.setattr(fo, "_EXHAUSTED", set())
    monkeypatch.setattr(fo, "_save_exhausted", lambda s: None)
    fm = fo.FailoverChatModel(models=["m1", "m2"], max_fallbacks=4, tier="strong")
    chunks = [c.message.content for c in fm._stream([SimpleNamespace(content="hi")])]
    assert chunks == ["m2 的回答。"]


def test_astream_partial_failure_does_not_splice(monkeypatch):
    """P0-6 异步路径：同上，半截拼接防护必须同步生效。"""
    import asyncio

    state = {"m1": (["异步第一句。", "异步第二句。"], 2), "m2": (["模型B全文。"], None)}

    def builder(self, model):
        chunks, raise_after = state.get(model, (["ok"], None))
        return _AFakeStreamModel(chunks, raise_after)

    monkeypatch.setattr(fo.FailoverChatModel, "_build", builder)
    monkeypatch.setattr(fo, "_EXHAUSTED", set())
    monkeypatch.setattr(fo, "_save_exhausted", lambda s: None)
    fm = fo.FailoverChatModel(models=["m1", "m2"], max_fallbacks=4, tier="strong")

    async def _collect():
        return [c.message.content async for c in fm._astream([SimpleNamespace(content="hi")])]

    chunks = asyncio.run(_collect())
    assert chunks == ["异步第一句。", "异步第二句。", "\n\n（模型响应中途中断，以上为部分内容）"]
    assert not any("模型B" in c for c in chunks)
