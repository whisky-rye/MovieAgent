#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP 只读服务器启动入口（stdio 传输）。

供外部 MCP 客户端以子进程方式连接本项目的生产库只读通道：

    {
      "mcpServers": {
        "movie-agent-prod-readonly": {
          "command": "./.venv/Scripts/python.exe",
          "args": ["./scripts/run_mcp_prod_server.py"],
          "env": {"MCP_DB_HOST": "...", "MCP_DB_USER": "...", "MCP_DB_PASSWORD": "..."}
        }
      }
    }

🔴 红线：本 Server 仅执行只读查询（SQL 护栏 + 会话 READ ONLY + 限流 + 熔断）。
凭据只从环境变量注入，**不要**写进本仓库。

用法：
    python scripts/run_mcp_prod_server.py                # stdio（默认）
    MCP_TRANSPORT=streamable-http python scripts/run_mcp_prod_server.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_prod.server import main  # noqa: E402

if __name__ == "__main__":
    main()
