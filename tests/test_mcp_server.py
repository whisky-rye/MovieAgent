"""MCP Server 协议层单测（工具/资源注册与调用，注入假客户端，不连真库）。

覆盖：
- 8 个只读工具全部注册、描述非空；
- schema / health 资源注册（含模板资源）；
- 工具调用返回统一信封 {ok: true, ...}；
- 底层异常 → 统一错误信封 {ok: false, error: ...}，且**不泄露凭据**；
- 写操作类工具不存在（协议面也不得暴露写能力）。
"""
import asyncio
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_prod import server as S  # noqa: E402
from mcp_prod.connection import McpError, McpRateLimited, McpReadOnlyViolation  # noqa: E402

from tests.test_mcp_queries import FakeClient  # noqa: E402

EXPECTED_TOOLS = {
    "search_movie", "get_movie_detail", "get_movie_relations", "search_person",
    "get_person_movies", "list_dimensions", "get_table_stats", "compare_cross_source",
}


def _run(coro):
    """在独立事件循环中执行协程（避免复用已关闭的 loop）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _payload(result):
    """从 CallToolResult 提取 JSON 文本。"""
    text = result.content[0].text
    return json.loads(text)


def _resource(result):
    """从 read_resource 结果（list[ReadResourceContents]）提取文本。"""
    if isinstance(result, list):
        return result[0].content
    return result.content


@pytest.fixture
def patched(monkeypatch):
    """把查询层换成假客户端（避免连真库）。"""
    fake = FakeClient({"search_movie": [], "get_movie_detail": [],
                       "get_table_stats": [{"t": "douban_main", "n": 100}]})
    monkeypatch.setattr(S.Q, "search_movie",
                        lambda *a, **k: {"count": 0, "items": [], "source": "douban"})
    monkeypatch.setattr(S.Q, "get_movie_detail", lambda *a, **k: {"found": False})
    monkeypatch.setattr(S.Q, "get_table_stats", lambda *a, **k: {"tables": []})
    return fake


def test_all_expected_tools_registered():
    srv = S.build_server()
    tools = _run(srv.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names, f"缺少工具: {EXPECTED_TOOLS - names}"


def test_no_write_capability_exposed():
    """协议面不得出现任何写/删能力。"""
    srv = S.build_server()
    tools = _run(srv.list_tools())
    banned = ("insert", "update", "delete", "drop", "truncate", "alter",
              "create", "write", "execute", "grant", "replace")
    for t in tools:
        low = (t.name + " " + (t.description or "")).lower()
        for b in banned:
            assert b not in low, f"工具 {t.name} 的名称/描述含写操作词 {b}"


def test_tool_descriptions_present():
    srv = S.build_server()
    for t in _run(srv.list_tools()):
        assert t.description and len(t.description) > 10, f"{t.name} 缺少描述"


def test_resources_registered():
    srv = S.build_server()
    uris = {str(r.uri) for r in _run(srv.list_resources())}
    assert "mcp://prod/schema/tables" in uris
    assert "mcp://prod/health" in uris
    templates = {t.uri_template for t in _run(srv.list_resource_templates())}
    assert any("schema/tables/{source}" in t for t in templates), \
        f"缺少单源 schema 模板资源，实际: {templates}"


def test_schema_resource_offline():
    srv = S.build_server()
    data = json.loads(_resource(_run(srv.read_resource("mcp://prod/schema/tables"))))
    assert data["main_tables"]["douban"]["table"] == "douban_main"
    assert "douban_main" in data["all_tables"]


def test_schema_resource_by_source():
    srv = S.build_server()
    data = json.loads(_resource(_run(srv.read_resource("mcp://prod/schema/tables/douban"))))
    assert data["source"] == "douban"
    assert data["main_table"]["table"] == "douban_main"
    assert data["relation_table"]["table"] == "douban_name_relate"


def test_health_resource_reports_readonly():
    srv = S.build_server()
    data = json.loads(_resource(_run(srv.read_resource("mcp://prod/health"))))
    assert data["read_only"] is True
    assert "READ ONLY" in data["guard"]


def test_tool_call_success_envelope(patched):
    srv = S.build_server()
    payload = _payload(_run(srv.call_tool("search_movie",
                                          {"keyword": "x", "source": "douban"})))
    assert payload["ok"] is True
    assert "count" in payload


def test_tool_call_error_envelope(monkeypatch):
    def _boom(*a, **k):
        raise McpReadOnlyViolation("拒绝非只读语句")
    monkeypatch.setattr(S.Q, "search_movie", _boom)
    srv = S.build_server()
    payload = _payload(_run(srv.call_tool("search_movie", {"keyword": "x"})))
    assert payload["ok"] is False
    assert payload["error"] == "read_only_violation"


def test_tool_call_rate_limited_envelope(monkeypatch):
    def _boom(*a, **k):
        raise McpRateLimited(2.5)
    monkeypatch.setattr(S.Q, "search_movie", _boom)
    srv = S.build_server()
    payload = _payload(_run(srv.call_tool("search_movie", {"keyword": "x"})))
    assert payload["error"] == "rate_limited"
    assert payload["retry_after"] == 2.5


def test_tool_call_generic_error_envelope(monkeypatch):
    def _boom(*a, **k):
        raise ValueError("内部异常")
    monkeypatch.setattr(S.Q, "search_movie", _boom)
    srv = S.build_server()
    payload = _payload(_run(srv.call_tool("search_movie", {"keyword": "x"})))
    assert payload["ok"] is False
    assert payload["error"] == "internal_error"


def test_error_envelope_never_leaks_credentials(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("failed for user root password=SUPER_SECRET")
    monkeypatch.setattr(S.Q, "search_movie", _boom)
    srv = S.build_server()
    payload = _payload(_run(srv.call_tool("search_movie", {"keyword": "x"})))
    assert "SUPER_SECRET" not in json.dumps(payload, ensure_ascii=False)


def test_server_instructions_mention_readonly():
    srv = S.build_server()
    assert "只读" in (srv.instructions or "")
    assert "SELECT" in (srv.instructions or "")


def test_build_server_is_isolated_instances():
    a = S.build_server("a")
    b = S.build_server("b")
    assert a.name != b.name


def test_get_server_singleton():
    assert S.get_server() is S.get_server()
