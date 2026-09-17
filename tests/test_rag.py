"""RAG 可解释输出（agent/rag.py）单测。

验证：
- build_context 拼装检索上下文（含空候选占位）；
- build_source_meta 来源分块与顺序（豆瓣→IMDb）、has_both / summary；
- source_one_liner 对话末尾来源一句话；
- _extract_thinking / _chunk_thinking 从 AIMessage / AIMessageChunk 抽取推理过程；
- _fallback_explanation 空响应兜底模板。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage, AIMessageChunk

from agent.rag import (
    _extract_thinking,
    _fallback_explanation,
    _chunk_thinking,
    build_context,
    build_source_meta,
    source_one_liner,
)
from retrieval.candidates import Candidate


def _c(title, source="douban", year=2020, genres=None, rating=8.0, doc="简介文本"):
    return Candidate(
        id=f"{source}:1", source=source, title=title, year=year,
        genres=genres or ["剧情"], country="中国", rating=rating,
        rating_count=1000, doc=doc, recall_source="content", score=1.0,
    )


def test_build_context_basic():
    out = build_context([_c("测试片")], top_n=3)
    assert "测试片" in out and "8.0" in out


def test_build_context_empty():
    assert build_context([]) == "（无候选）"


def test_source_meta_summary_and_order():
    ranked = [_c("A", source="douban"), _c("B", source="imdb"), _c("C", source="douban")]
    meta = build_source_meta(ranked, top_n=20)
    assert meta["has_both"] is True
    assert meta["blocks"][0]["source"] == "douban"
    assert meta["blocks"][1]["source"] == "imdb"
    assert "·" in meta["summary"]


def test_source_meta_empty():
    assert build_source_meta([]) == {"summary": "", "has_both": False, "blocks": []}


def test_source_one_liner():
    meta = build_source_meta([_c("A"), _c("B", source="imdb")])
    line = source_one_liner(meta)
    assert line.startswith("\n\n〔信息来源〕")
    assert "豆瓣" in line and "IMDb" in line


def test_source_one_liner_empty():
    assert source_one_liner({}) == ""


def test_extract_thinking_from_additional_kwargs():
    msg = AIMessage(content="x", additional_kwargs={"reasoning_content": "我在思考"})
    assert _extract_thinking(msg) == "我在思考"


def test_extract_thinking_none():
    assert _extract_thinking(AIMessage(content="x")) == ""
    assert _extract_thinking(None) == ""


def test_chunk_thinking():
    chunk = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "delta"})
    assert _chunk_thinking(chunk) == "delta"
    assert _chunk_thinking(None) == ""


def test_fallback_explanation():
    out = _fallback_explanation("科幻片", [_c("A"), _c("B")], "科幻")
    assert "A" in out and "科幻" in out
