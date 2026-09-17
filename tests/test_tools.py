"""Agent 工具层（agent/tools.py）单测。

验证：
- RUNNERS 注册表含 3 个检索 runner 且均为可调用；
- get_tools 返回 5 个 LangChain 工具；
- _run_search_similar 融合 content + relation 召回并走 rerank；
- _run_search_by_text 把 slots / prefs 透传给 content_recall（含类型/国家/年份过滤）。
用 monkeypatch 隔离真实向量库 / 数据库。
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.tools as tools
from retrieval.candidates import Candidate


def test_runners_registry():
    assert set(tools.RUNNERS.keys()) == {"search_by_text", "search_similar", "high_quality_board"}
    for fn in tools.RUNNERS.values():
        assert callable(fn)


def test_get_tools_count():
    assert len(tools.get_tools()) == 5


def test_run_search_similar_fuses():
    sim = [Candidate(id="douban:1", source="douban", title="A", score=0.9)]
    rel = [Candidate(id="douban:2", source="douban", title="B", score=5.0)]
    fake_ranked = [Candidate(id="douban:1", source="douban", title="A", score=2.0)]
    with patch.object(tools, "content_recall", return_value=sim), \
         patch.object(tools, "relation_recall", return_value=rel), \
         patch.object(tools, "rerank", return_value=fake_ranked):
        out = tools._run_search_similar("douban:1", top_k=5)
    assert out is fake_ranked


def test_run_search_by_text_passes_slots():
    captured = {}

    def fake_content(**kw):
        captured.update(kw)
        return []

    with patch.object(tools, "content_recall", side_effect=fake_content):
        tools._run_search_by_text(
            {"query": "科幻", "genres": ["科幻"], "country": "美国", "min_year": 2000},
            {"genres": ["喜剧"]}, top_k=10,
        )
    assert captured["query"] == "科幻"
    assert captured["genres"] == ["科幻"]
    assert captured["country"] == "美国"
    assert captured["min_year"] == 2000
