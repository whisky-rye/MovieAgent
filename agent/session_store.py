"""会话存储层（G3）：**强 MySQL**，唯一后端为 `movie_agent.sessions` 表（与 imdb 语料库隔离）。

强约束（2026-09-17 起，应用户要求移除 file 兜底）：
- 业务代码**不含任何建库/建表语句**；表结构见 `db/schema/movie_agent.sql`，需手动
  `scripts/init_movie_agent_db.py` 初始化。
- **fail-fast**：初始化时 MySQL 不可达 / 库或表不存在 -> 直接抛 RuntimeError，
  服务拒绝启动（api/main.py 模块级调用 get_session_store()，异常即启动失败）。
- **运行期任何 MySQL 异常直接向上抛**（由接口层转 500），绝不静默降级到文件——
  宁可报错可见，不可数据分叉（MySQL/file 双写不一致比短暂不可用更危险）。
- messages 内嵌为 JSON 列，结构：{session_id,user_id,title,created_at,updated_at,messages}。

会话归属：所有读取/删除前由 `assert_owner` 校验 `user_id`，纵深防御越权访问。
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, text

from config.settings import settings

logger = logging.getLogger("movie_agent.session")


class SessionStoreError(RuntimeError):
    """会话存储运行期错误（MySQL 读写失败等）；接口层据此返回 500。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except Exception:
        return str(v)


class MySQLSessionStore:
    """会话的强 MySQL 后端：落 movie_agent.sessions；不可用即启动失败，无任何兜底。"""

    def __init__(self, max_messages: int = 200):
        self._max = max_messages
        try:
            self._engine = create_engine(
                settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True, pool_recycle=3600
            )
            # 仅探测表是否存在 / 可读；不建库、不建表（DDL 见 db/schema/movie_agent.sql）
            with self._engine.connect() as c:
                c.execute(text("SELECT 1 FROM sessions LIMIT 1"))
        except Exception as e:  # noqa: BLE001 - fail-fast：强 MySQL 模式下不启动服务
            raise RuntimeError(
                "会话存储初始化失败：movie_agent.sessions 不可达或表不存在。"
                "本服务为强 MySQL 模式（无 file 兜底），请先初始化业务库"
                "（.venv/Scripts/python.exe scripts/init_movie_agent_db.py）并确认 MySQL 在线。"
                f"原始错误：{e}"
            ) from e
        logger.info("MySQLSessionStore: 已启用 movie_agent.sessions 作为会话存储（强 MySQL，无 file 兜底）")

    @staticmethod
    def _msgs(raw):
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return []

    def create(self, user_id: str, title: Optional[str] = None) -> str:
        title = title or "新会话"
        sid = uuid.uuid4().hex
        with self._engine.connect() as c:
            c.execute(
                text(
                    "INSERT INTO sessions (session_id, user_id, title, created_at, updated_at, messages) "
                    "VALUES (:sid, :uid, :title, NOW(), NOW(), :msgs)"
                ),
                {"sid": sid, "uid": user_id, "title": title,
                 "msgs": json.dumps([], ensure_ascii=False)},
            )
            c.commit()
        return sid

    def list_by_user(self, user_id: str) -> list:
        with self._engine.connect() as c:
            rows = c.execute(
                text(
                    "SELECT session_id, title, created_at, updated_at, messages "
                    "FROM sessions WHERE user_id=:uid ORDER BY updated_at DESC"
                ),
                {"uid": user_id},
            ).mappings().all()
        out = []
        for r in rows:
            msgs = self._msgs(r["messages"])
            preview = msgs[-1].get("content", "") if msgs else ""
            out.append({
                "session_id": r["session_id"], "title": r["title"],
                "created_at": _iso(r["created_at"]),
                "updated_at": _iso(r["updated_at"]),
                "preview": preview[:80],
            })
        return out

    def get(self, sid: str) -> Optional[dict]:
        with self._engine.connect() as c:
            r = c.execute(
                text(
                    "SELECT session_id, user_id, title, created_at, updated_at, messages "
                    "FROM sessions WHERE session_id=:sid"
                ),
                {"sid": sid},
            ).mappings().first()
        if r is None:
            return None
        return {
            "session_id": r["session_id"], "user_id": r["user_id"], "title": r["title"],
            "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
            "messages": self._msgs(r["messages"]),
        }

    def append_message(self, sid: str, msg: dict) -> None:
        with self._engine.begin() as c:
            r = c.execute(
                text("SELECT messages, title FROM sessions WHERE session_id=:sid"),
                {"sid": sid},
            ).mappings().first()
            if r is None:
                return
            msgs = self._msgs(r["messages"])
            msgs.append(msg)
            title = r["title"]
            # 自动命名：首条用户消息且仍为默认标题
            if msg.get("role") == "user" and title == "新会话":
                content = (msg.get("content") or "").strip()
                if content:
                    title = content[:20]
            if len(msgs) > self._max:
                msgs = msgs[-self._max:]
            c.execute(
                text(
                    "UPDATE sessions SET messages=:msgs, title=:title, updated_at=NOW() "
                    "WHERE session_id=:sid"
                ),
                {"msgs": json.dumps(msgs, ensure_ascii=False), "title": title, "sid": sid},
            )

    def rename(self, sid: str, title: str) -> None:
        with self._engine.connect() as c:
            c.execute(
                text("UPDATE sessions SET title=:title, updated_at=NOW() WHERE session_id=:sid"),
                {"title": title, "sid": sid},
            )
            c.commit()

    def delete(self, sid: str) -> None:
        with self._engine.connect() as c:
            c.execute(text("DELETE FROM sessions WHERE session_id=:sid"), {"sid": sid})
            c.commit()

    def assert_owner(self, sid: str, user_id: str) -> bool:
        rec = self.get(sid)
        return rec is not None and rec.get("user_id") == user_id

    def backend(self) -> str:
        return "mysql"


_session_store: Optional[MySQLSessionStore] = None


def get_session_store() -> MySQLSessionStore:
    """会话存储单例（强 MySQL）。

    MySQL 不可达 / 表不存在时直接抛 RuntimeError -> 服务启动失败（fail-fast）。
    """
    global _session_store
    if _session_store is None:
        _session_store = MySQLSessionStore(max_messages=settings.SESSION_MAX_MESSAGES)
    return _session_store
