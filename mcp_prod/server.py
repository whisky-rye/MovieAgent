"""MCP Server 装配（生产库只读通道）。

使用 MCP Python SDK 2.x 的 `MCPServer`（`mcp.server.mcpserver`；2.x 将 FastMCP
更名为 MCPServer）。对外暴露：

Tools（可调用能力）
  search_movie / get_movie_detail / get_movie_relations /
  search_person / get_person_movies / list_dimensions /
  get_table_stats / compare_cross_source

Resources（可读上下文）
  mcp://prod/schema/tables   表与字段映射总览
  mcp://prod/schema/tables/{source}  单源表清单
  mcp://prod/health          通道健康状态（配置/熔断/限流）

🔴 红线：所有工具底层都走 `mcp_prod/queries.py` → `connection.py` → `guard.py`，
   SQL 只读 + 表白名单 + 会话只读 + 限流 + 熔断，任何写操作不会触达远端。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from config.settings import settings
from mcp_prod import queries as Q
from mcp_prod.connection import (
    McpCircuitOpen,
    McpError,
    McpNotConfigured,
    McpRateLimited,
    McpReadOnlyViolation,
    get_prod_client,
)

logger = logging.getLogger("movie_agent.mcp")

INSTRUCTIONS = """\
生产库(IMDb/豆瓣)只读数据通道。

🔴 只读红线：本 Server 仅执行 SELECT。SQL 在触达数据库前经护栏校验
（语句白名单 / 表白名单 / 禁多语句 / 禁危险函数 / 强制 LIMIT），连接建立后
立即置为会话只读事务；任何写操作都会被拒绝且不会触达远端。

定位：作为「补充/校验通道」使用（长尾补齐、最新校验、运营即席查询），
主召回链路仍走本地库。工具响应含来源、条数与耗时，便于链路排查。
"""


#: 错误信封中的敏感信息脱敏模式（避免把凭据/令牌回显给 MCP 客户端）
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(pass(word|wd)?|secret|token|api[_-]?key|authorization)\s*[=:]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)


def _redact(text: str, limit: int = 300) -> str:
    """脱敏：抹掉疑似凭据/令牌/邮箱后再截断。"""
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out[:limit]


def _err(exc: Exception) -> dict:
    """统一错误响应（不含凭据/内部细节泄露）。"""
    if isinstance(exc, McpReadOnlyViolation):
        return {"ok": False, "error": "read_only_violation", "message": _redact(str(exc))}
    if isinstance(exc, McpRateLimited):
        return {"ok": False, "error": "rate_limited",
                "message": _redact(str(exc)), "retry_after": exc.retry_after}
    if isinstance(exc, McpCircuitOpen):
        return {"ok": False, "error": "circuit_open", "message": _redact(str(exc))}
    if isinstance(exc, McpNotConfigured):
        return {"ok": False, "error": "not_configured", "message": _redact(str(exc))}
    if isinstance(exc, McpError):
        return {"ok": False, "error": "mcp_error", "message": _redact(str(exc))}
    logger.exception("MCP 工具异常")
    return {"ok": False, "error": "internal_error", "message": _redact(str(exc))}


def _ok(payload: dict) -> dict:
    out = {"ok": True}
    out.update(payload)
    return out


def build_server(name: Optional[str] = None) -> MCPServer:
    """构建并装配 MCP Server（不启动，便于单测内联调用）。"""
    server = MCPServer(
        name=name or settings.MCP_SERVER_NAME,
        instructions=INSTRUCTIONS,
        version="1.0.0",
    )

    # ---------------- Tools ----------------

    @server.tool(
        name="search_movie",
        description="按标题关键词检索影片（豆瓣/IMDb 双源；中文名、英文名、原名任一命中即返回）。"
                    "返回归一化影片信息（标题/年份/评分/票数/类型/国家/跨源 id）。",
    )
    def search_movie(keyword: str, source: str = "douban", limit: int = 10) -> dict:
        """检索影片。source: douban | imdb。"""
        try:
            return _ok(Q.search_movie(keyword, source=source, limit=limit))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @server.tool(
        name="get_movie_detail",
        description="取单部影片的完整信息：主表字段 + 人物卡司(导演/编剧/演员) + 简介文本 + "
                    "类型/国家维度。用于长尾影片补齐与详情校验。",
    )
    def get_movie_detail(source: str, movie_id: str,
                         include_text: bool = True,
                         include_credits: bool = True) -> dict:
        """取单片全链路详情。movie_id: 豆瓣 douban_id 或 IMDb imdb_id。"""
        try:
            return _ok(Q.get_movie_detail(
                source, movie_id, include_text=include_text,
                include_credits=include_credits))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @server.tool(
        name="get_movie_relations",
        description="取同导演/编剧/卡司的关联作品（关系召回）。按共享人物权重排序，"
                    "适合「看这部的人还看了什么」类补充召回。",
    )
    def get_movie_relations(source: str, movie_id: str, limit: int = 20) -> dict:
        """取同剧组关联作品。"""
        try:
            return _ok(Q.get_movie_relations(source, movie_id, limit=limit))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @server.tool(
        name="search_person",
        description="按姓名检索人物（导演/编剧/演员维度）。返回 name_id 与多语姓名变体。",
    )
    def search_person(name: str, source: str = "douban", limit: int = 10) -> dict:
        """检索人物。"""
        try:
            return _ok(Q.search_person(name, source=source, limit=limit))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @server.tool(
        name="get_person_movies",
        description="取某人物（name_id）的作品集。人物中心召回的核心工具。",
    )
    def get_person_movies(name_id: str, source: str = "douban",
                          limit: int = 30) -> dict:
        """取人物作品集。"""
        try:
            return _ok(Q.get_person_movies(name_id, source=source, limit=limit))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @server.tool(
        name="list_dimensions",
        description="列出类型(genre)/国家(country)维度字典，便于把英文维度值映射为中文名。",
    )
    def list_dimensions(kind: str = "genre", source: str = "douban") -> dict:
        """列维度字典。kind: genre | country。"""
        try:
            return _ok(Q.list_dimensions(kind, source=source))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @server.tool(
        name="get_table_stats",
        description="查生产库各表规模概览（行数估算），用于了解数据分布与容量。",
    )
    def get_table_stats(source: str = "douban") -> dict:
        """表规模概览。"""
        try:
            return _ok(Q.get_table_stats(source))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @server.tool(
        name="compare_cross_source",
        description="跨源比对：用生产库官方视图口径对照豆瓣与 IMDb 的同片标题/评分/票数，"
                    "用于跨源映射与评分口径校准。",
    )
    def compare_cross_source(keyword: str = "", limit: int = 20) -> dict:
        """豆瓣↔IMDb 跨源比对。"""
        try:
            return _ok(Q.compare_cross_source(keyword, limit=limit))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------- Resources ----------------

    @server.resource(
        "mcp://prod/schema/tables",
        name="生产库表结构总览",
        description="只读通道登记的全部表/字段映射（离线可读，无需连库）。",
        mime_type="application/json",
    )
    def schema_tables() -> str:
        return json.dumps(Q.describe_schema(), ensure_ascii=False, indent=2)

    @server.resource(
        "mcp://prod/schema/tables/{source}",
        name="单源表结构",
        description="指定源(imdb/douban)的表与字段映射。",
        mime_type="application/json",
    )
    def schema_tables_by_source(source: str) -> str:
        data = Q.describe_schema()
        src = (source or "").lower()
        picked = {
            "source": src,
            "main_table": data["main_tables"].get(src),
            "relation_table": data["relation_tables"].get(src),
            "name_table": data["name_tables"].get(src),
            "text_table": data["text_tables"].get(src),
            "genre_dimension": data["genre_dimensions"].get(src),
            "country_dimension": data["country_dimensions"].get(src),
        }
        return json.dumps(picked, ensure_ascii=False, indent=2)

    @server.resource(
        "mcp://prod/health",
        name="只读通道健康状态",
        description="配置状态、熔断状态、连接池概况（不触发任何查询）。",
        mime_type="application/json",
    )
    def health() -> str:
        try:
            data = get_prod_client().health()
        except Exception as e:  # noqa: BLE001
            data = {"error": str(e)[:200]}
        data["read_only"] = True
        data["guard"] = "statement+table whitelist, session READ ONLY, rate limit, circuit breaker"
        return json.dumps(data, ensure_ascii=False, indent=2)

    return server


#: 全局 Server 单例（stdio 入口与外部客户端共用）
_SERVER: Optional[MCPServer] = None


def get_server() -> MCPServer:
    global _SERVER
    if _SERVER is None:
        _SERVER = build_server()
    return _SERVER


def main() -> None:
    """stdio 传输启动入口（供外部 MCP 客户端以子进程方式连接）。"""
    logging.basicConfig(
        level=getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    transport = (settings.MCP_TRANSPORT or "stdio").lower()
    if transport not in ("stdio", "sse", "streamable-http"):
        transport = "stdio"
    logger.info("MCP 只读服务器启动 transport=%s（只读红线：仅 SELECT）", transport)
    get_server().run(transport=transport)


if __name__ == "__main__":
    main()
