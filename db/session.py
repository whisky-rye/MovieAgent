"""数据访问层：MySQL 引擎与查询助手（开发期同步驱动 pymysql）。

为什么同步：召回/排序链路是请求内的短查询，同步足够简单可靠；
异步引擎(sqlalchemy.ext.asyncio) 仅在上线期高并发接口层需要。
"""
from contextlib import contextmanager
from sqlalchemy import create_engine, text
from config.settings import settings

_SYNC_DSN = settings.MYSQL_DSN.replace("mysql+aiomysql", "mysql+pymysql")

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(_SYNC_DSN, pool_pre_ping=True, pool_recycle=3600)
    return _engine


@contextmanager
def session():
    eng = get_engine()
    conn = eng.connect()
    try:
        yield conn
        conn.commit()  # 读为主，commit 无害
    finally:
        conn.close()


def fetch_all(sql: str, params: dict | None = None) -> list[dict]:
    """执行 SELECT，返回 dict 列表。IN 列表用命名绑定 `col IN :ids` + {"ids": [...]} 即可（SQLAlchemy 自动展开）。"""
    with session() as c:
        rows = c.execute(text(sql), params or {}).mappings().all()
        return [dict(r) for r in rows]
