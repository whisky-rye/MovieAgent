"""手动初始化 movie_agent 业务库表结构（G1/G3/G4 新增表统一落这里）。

用法：
    .venv/Scripts/python.exe scripts/init_movie_agent_db.py

行为：
    1. 用「无库名」root DSN 建库（CREATE DATABASE IF NOT EXISTS movie_agent）。
    2. 切到 movie_agent，执行 db/schema/movie_agent.sql 内的全部 DDL。

说明：
    本脚本仅供本地 / 部署初始化使用；业务代码（agent/memory.py、agent/session_store.py、
    agent/user_store.py、agent/share_store.py）严禁包含任何建库建表语句，只做读写探测，
    异常则强 MySQL fail-fast（服务拒绝启动，无 file 兜底）。可重复执行（DDL 幂等）。
"""
import os
import sys

from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import settings  # noqa: E402

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "db", "schema", "movie_agent.sql",
)


def _split_sql(sql: str):
    """按 ; 切分语句，跳过 -- 注释行（本文件不含存储过程，足够）。"""
    out, buf = [], []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.strip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    if buf:
        stmt = "\n".join(buf).strip()
        if stmt:
            out.append(stmt)
    return out


def main():
    if not os.path.exists(SCHEMA_PATH):
        print(f"[err] 找不到 schema 文件：{SCHEMA_PATH}")
        sys.exit(1)

    # 1) 建库（无库名连接）
    root_eng = create_engine(settings.MOVIE_AGENT_MYSQL_ROOT_DSN, pool_pre_ping=True)
    with root_eng.connect() as c:
        c.execute(text("CREATE DATABASE IF NOT EXISTS `movie_agent` DEFAULT CHARACTER SET utf8mb4"))
        c.commit()
    print("[ok] 数据库 movie_agent 已就绪")

    # 2) 执行 schema DDL
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        sql = f.read()
    eng = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
    stmts = _split_sql(sql)
    with eng.connect() as c:
        for stmt in stmts:
            c.execute(text(stmt))
        c.commit()
    print(f"[ok] 已执行 {len(stmts)} 条 DDL（db/schema/movie_agent.sql）")
    print("[done] movie_agent 初始化完成")


if __name__ == "__main__":
    main()
