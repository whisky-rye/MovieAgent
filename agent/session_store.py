"""会话存储层（G3）：落 `movie_agent.sessions` 表（与 imdb 语料库隔离）。

设计原则（与 agent/memory.py 的 MySQLMemoryStore 同构、与 FallbackMemoryStore「绝不阻塞主链路」一致）：
- 业务代码**不含任何建库/建表语句**；表结构见 `db/schema/movie_agent.sql`，需手动
  `scripts/init_movie_agent_db.py` 初始化。
- 构造时仅「探测」表是否存在 / 可读（SELECT 1 FROM sessions LIMIT 1）；不可达或表不存在则
  use_mysql=False，全程回退 `FileSessionStore`，不抛异常、不阻塞主链路。
- 每次读写的 MySQL 异常都会自动转 file 兜底。
- messages 内嵌为 JSON 列，file 兜底时结构完全一致（{session_id,user_id,title,created_at,updated_at,messages}）。

会话归属：所有读取/删除前由 `assert_owner` 校验 `user_id`，纵深防御越权访问。
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, text

from config.settings import settings

logger = logging.getLogger("movie_agent.session")


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


class FileSessionStore:
    """会话的本地文件兜底（与 MySQL 结构完全一致）。"""

    def __init__(self, root: str = "data/sessions"):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, sid: str) -> str:
        return os.path.join(self.root, f"{sid}.json")

    def _read(self, sid: str) -> Optional[dict]:
        p = self._path(sid)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return None

    def _write(self, rec: dict) -> None:
        with self._lock:
            with open(self._path(rec["session_id"]), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)

    def create(self, user_id: str, title: Optional[str] = None) -> str:
        sid = uuid.uuid4().hex
        rec = {
            "session_id": sid, "user_id": user_id,
            "title": title or "新会话", "created_at": _now_iso(),
            "updated_at": _now_iso(), "messages": [],
        }
        self._write(rec)
        return sid

    def list_by_user(self, user_id: str) -> list:
        out = []
        try:
            names = os.listdir(self.root)
        except Exception:  # noqa: BLE001
            return out
        for fn in names:
            if not fn.endswith(".json"):
                continue
            rec = self._read(fn[:-5])
            if rec and rec.get("user_id") == user_id:
                msgs = rec.get("messages", [])
                preview = msgs[-1].get("content", "") if msgs else ""
                out.append({
                    "session_id": rec["session_id"], "title": rec.get("title", "新会话"),
                    "created_at": rec.get("created_at"), "updated_at": rec.get("updated_at"),
                    "preview": preview[:80],
                })
        out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return out

    def get(self, sid: str) -> Optional[dict]:
        return self._read(sid)

    def append_message(self, sid: str, msg: dict) -> None:
        rec = self._read(sid)
        if rec is None:
            return
        msgs = rec.setdefault("messages", [])
        msgs.append(msg)
        # 自动命名：首条用户消息且仍为默认标题
        if msg.get("role") == "user" and rec.get("title") == "新会话":
            content = (msg.get("content") or "").strip()
            if content:
                rec["title"] = content[:20]
        if len(msgs) > settings.SESSION_MAX_MESSAGES:
            msgs = msgs[-settings.SESSION_MAX_MESSAGES:]
            rec["messages"] = msgs
        rec["updated_at"] = _now_iso()
        self._write(rec)

    def rename(self, sid: str, title: str) -> None:
        rec = self._read(sid)
        if rec is None:
            return
        rec["title"] = title
        rec["updated_at"] = _now_iso()
        self._write(rec)

    def delete(self, sid: str) -> None:
        p = self._path(sid)
        if os.path.exists(p):
            os.remove(p)

    def assert_owner(self, sid: str, user_id: str) -> bool:
        rec = self._read(sid)
        return rec is not None and rec.get("user_id") == user_id


class MySQLSessionStore:
    """会话的 MySQL 后端：落 movie_agent.sessions；不可达自动回退 FileSessionStore。"""

    def __init__(self, file_root: str = "data/sessions", max_messages: int = 200):
        self._file = FileSessionStore(file_root)
        self.use_mysql = False
        self._engine = None
        self._max = max_messages
        try:
            # 仅探测表是否存在 / 可读；不建库、不建表（DDL 见 db/schema/movie_agent.sql）
            self._engine = create_engine(
                settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True, pool_recycle=3600
            )
            with self._engine.connect() as c:
                c.execute(text("SELECT 1 FROM sessions LIMIT 1"))
            self.use_mysql = True
            logger.info("MySQLSessionStore: 已启用 movie_agent.sessions 作为会话存储")
        except Exception as e:  # noqa: BLE001
            self.use_mysql = False
            self._engine = None
            logger.warning(
                "MySQLSessionStore: movie_agent.sessions 不可达或表不存在"
                "（请先执行 db/schema/movie_agent.sql 初始化），已回退 file 兜底：%s", e
            )

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
        if self.use_mysql and self._engine is not None:
            sid = uuid.uuid4().hex
            try:
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
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLSessionStore.create 失败，转 file：%s", e)
        return self._file.create(user_id, title)

    def list_by_user(self, user_id: str) -> list:
        if self.use_mysql and self._engine is not None:
            try:
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
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLSessionStore.list_by_user 失败，转 file：%s", e)
        return self._file.list_by_user(user_id)

    def get(self, sid: str) -> Optional[dict]:
        if self.use_mysql and self._engine is not None:
            try:
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
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLSessionStore.get 失败，转 file：%s", e)
        return self._file.get(sid)

    def append_message(self, sid: str, msg: dict) -> None:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    r = c.execute(
                        text("SELECT messages, title FROM sessions WHERE session_id=:sid"),
                        {"sid": sid},
                    ).mappings().first()
                    if r is None:
                        return
                    msgs = self._msgs(r["messages"])
                    msgs.append(msg)
                    title = r["title"]
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
                    c.commit()
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLSessionStore.append_message 失败，转 file：%s", e)
        return self._file.append_message(sid, msg)

    def rename(self, sid: str, title: str) -> None:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    c.execute(
                        text("UPDATE sessions SET title=:title, updated_at=NOW() WHERE session_id=:sid"),
                        {"title": title, "sid": sid},
                    )
                    c.commit()
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLSessionStore.rename 失败，转 file：%s", e)
        return self._file.rename(sid, title)

    def delete(self, sid: str) -> None:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    c.execute(text("DELETE FROM sessions WHERE session_id=:sid"), {"sid": sid})
                    c.commit()
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLSessionStore.delete 失败，转 file：%s", e)
        return self._file.delete(sid)

    def assert_owner(self, sid: str, user_id: str) -> bool:
        rec = self.get(sid)
        return rec is not None and rec.get("user_id") == user_id

    def backend(self) -> str:
        return "mysql" if self.use_mysql else "mysql->file"


_session_store: Optional[MySQLSessionStore] = None


def get_session_store() -> MySQLSessionStore:
    """会话存储单例（MySQL 优先 + file 兜底）。"""
    global _session_store
    if _session_store is None:
        _session_store = MySQLSessionStore(
            file_root=settings.SESSION_FILE_ROOT,
            max_messages=settings.SESSION_MAX_MESSAGES,
        )
    return _session_store
