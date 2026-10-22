"""MCP 接入包：生产库**只读**数据通道。

🔴 红线：远端生产库 `imdb` / `imdb2` **仅允许 SELECT**。本包所有 SQL 必须先通过
`mcp_prod/guard.py`（语句/表前缀白名单 + 禁多语句 + 禁危险函数），连接建立后立即
`SET SESSION TRANSACTION READ ONLY`，并叠加限流与熔断。**任何写操作都不会触达远端。**

模块职责：
- `guard`     只读护栏（纯函数，红线闸门）
- `connection`只读连接池 + 限流 + 熔断 + 审计日志
- `schema_map`字段映射集中管理（防表结构漂移）
- `queries`   查询层（参数化，只读工具实现）
- `server`    MCP Server 装配（MCPServer + tools + resources）
- `client`    项目侧消费封装（短 TTL 缓存 + 降级）
"""
from mcp_prod.guard import (  # noqa: F401
    DEFAULT_MAX_LIMIT,
    GuardDecision,
    ReadOnlyViolation,
    assert_readonly,
    check_sql,
    is_readonly,
)

__all__ = [
    "DEFAULT_MAX_LIMIT",
    "GuardDecision",
    "ReadOnlyViolation",
    "assert_readonly",
    "check_sql",
    "is_readonly",
]
