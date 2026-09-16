"""会话共享存储层（G 共享功能）：

把「会话 -> 共享令牌」的映射持久化，供公开只读查看（/share/{token}，无需登录）使用。

设计原则（与 agent/session_store.py、agent/memory.py 同构，绝不阻塞主链路）：
- 业务代码**不含任何建库/建表语句**；表结构见 `db/schema/movie_agent.sql`（`shared_sessions`），需手动
  `scripts/init_movie_agent_db.py` 初始化。
- 构造时仅「探测」表是否存在 / 可读（SELECT 1 FROM shared_sessions LIMIT 1）；不可达或表不存在则
  use_mysql=False，全程回退 `FileShareStore`，不抛异常、不阻塞主链路。
- 每次读写 MySQL 异常都会自动转 file 兜底。
- token 用 secrets.token_hex 生成（不可猜测），会话与令牌一一对应：一个会话至多一个有效令牌。
"""
import json
import logging
import os
import secrets
import threading
from typing import Optional

from sqlalchemy import create_engine, text

from config.settings import settings

logger = logging.getLogger("movie_agent.share")


def _new_token() -> str:
    return secrets.token_hex(16)  # 32 hex chars，密码学随机，不可猜测


class FileShareStore:
    """共享映射的本地文件兜底（与 MySQL 结构一致：session_id -> {token, owner, created_at}）。"""

    def __init__(self, root: str = "data/shared"):
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

    def create(self, session_id: str, owner_user_id: str) -> str:
        token = _new_token()
        self._write({
            "session_id": session_id, "owner_user_id": owner_user_id,
            "share_token": token,
        })
        return token

    def get_session_by_token(self, token: str) -> Optional[str]:
        # 文件兜底：线性扫描（共享条目通常很少，可接受）
        try:
            for fn in os.listdir(self.root):
                if not fn.endswith(".json"):
                    continue
                rec = self._read(fn[:-5])
                if rec and rec.get("share_token") == token:
                    return rec["session_id"]
        except Exception:  # noqa: BLE001
            return None
        return None

    def get_token_by_session(self, session_id: str) -> Optional[str]:
        rec = self._read(session_id)
        return rec.get("share_token") if rec else None

    def is_shared(self, session_id: str) -> bool:
        return self._read(session_id) is not None

    def delete(self, session_id: str) -> None:
        p = self._path(session_id)
        if os.path.exists(p):
            os.remove(p)


class MySQLShareStore:
    """共享映射的 MySQL 后端：落 movie_agent.shared_sessions；不可达自动回退 FileShareStore。"""

    def __init__(self, file_root: str = "data/shared"):
        self._file = FileShareStore(file_root)
        self.use_mysql = False
        self._engine = None
        try:
            self._engine = create_engine(
                settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True, pool_recycle=3600
            )
            with self._engine.connect() as c:
                c.execute(text("SELECT 1 FROM shared_sessions LIMIT 1"))
            self.use_mysql = True
            logger.info("MySQLShareStore: 已启用 movie_agent.shared_sessions 作为共享存储")
        except Exception as e:  # noqa: BLE001
            self.use_mysql = False
            self._engine = None
            logger.warning(
                "MySQLShareStore: movie_agent.shared_sessions 不可达或表不存在"
                "（请先执行 db/schema/movie_agent.sql 初始化），已回退 file 兜底：%s", e
            )

    def create(self, session_id: str, owner_user_id: str) -> str:
        token = _new_token()
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    # 幂等：已共享则先清掉旧令牌再写新的（一个会话一个令牌）
                    c.execute(
                        text("DELETE FROM shared_sessions WHERE session_id=:sid"),
                        {"sid": session_id},
                    )
                    c.execute(
                        text(
                            "INSERT INTO shared_sessions (session_id, owner_user_id, share_token, created_at) "
                            "VALUES (:sid, :uid, :tok, NOW())"
                        ),
                        {"sid": session_id, "uid": owner_user_id, "tok": token},
                    )
                    c.commit()
                return token
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLShareStore.create 失败，转 file：%s", e)
        return self._file.create(session_id, owner_user_id)

    def get_session_by_token(self, token: str) -> Optional[str]:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    r = c.execute(
                        text("SELECT session_id FROM shared_sessions WHERE share_token=:tok"),
                        {"tok": token},
                    ).first()
                if r is not None:
                    return r[0]
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLShareStore.get_session_by_token 失败，转 file：%s", e)
        return self._file.get_session_by_token(token)

    def get_token_by_session(self, session_id: str) -> Optional[str]:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    r = c.execute(
                        text("SELECT share_token FROM shared_sessions WHERE session_id=:sid"),
                        {"sid": session_id},
                    ).first()
                if r is not None:
                    return r[0]
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLShareStore.get_token_by_session 失败，转 file：%s", e)
        return self._file.get_token_by_session(session_id)

    def is_shared(self, session_id: str) -> bool:
        return self.get_token_by_session(session_id) is not None

    def delete(self, session_id: str) -> None:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    c.execute(
                        text("DELETE FROM shared_sessions WHERE session_id=:sid"),
                        {"sid": session_id},
                    )
                    c.commit()
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLShareStore.delete 失败，转 file：%s", e)
        return self._file.delete(session_id)

    def backend(self) -> str:
        return "mysql" if self.use_mysql else "mysql->file"


_share_store: Optional[MySQLShareStore] = None


def get_share_store() -> MySQLShareStore:
    """共享存储单例（MySQL 优先 + file 兜底）。"""
    global _share_store
    if _share_store is None:
        _share_store = MySQLShareStore(file_root=settings.SHARED_FILE_ROOT)
    return _share_store
