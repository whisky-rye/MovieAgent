"""G1 用户账户存储：落 `movie_agent.users` 表，连不上自动回退 file 兜底。

与 MySQLMemoryStore 同构（业务代码严禁 DDL，仅探测表存在性；不可达回退 file）：
- MySQLUserStore：优先落 movie_agent.users；构造时仅探测表可读，失败回退 FileUserStore。
- FileUserStore：本地 data/users/users.json（调试/兜底，与 MySQL 结构一致）。
- get_user_store()：返回单例，调用方无需感知后端。
"""
from __future__ import annotations

import json
import hmac
import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Optional

from sqlalchemy import create_engine, text

from agent.auth_crypto import hash_password
from config.settings import settings

logger = logging.getLogger("movie_agent.user_store")


class UserStore(ABC):
    @abstractmethod
    def create(self, user_id: str, username: str, salt: str, pw_hash: str) -> None: ...

    @abstractmethod
    def get_by_username(self, username: str) -> Optional[dict]: ...

    @abstractmethod
    def verify(self, username: str, password: str) -> Optional[str]: ...

    @abstractmethod
    def get(self, user_id: str) -> Optional[dict]: ...

    def backend(self) -> str:
        return "abstract"


class FileUserStore(UserStore):
    """file 兜底：单个 users.json 存 {username: {user_id, salt, pw_hash}}。"""

    def __init__(self, root: str = "data/users"):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._path = os.path.join(root, "users.json")
        self._lock = threading.Lock()

    def _load_all(self) -> dict:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001 - 文件损坏则当作空
            logger.warning("FileUserStore: 读取 %s 失败，回退空：%s", self._path, e)
            return {}

    def _save_all(self, d: dict) -> None:
        with self._lock:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)

    def create(self, user_id: str, username: str, salt: str, pw_hash: str) -> None:
        d = self._load_all()
        d[username] = {"user_id": user_id, "salt": salt, "pw_hash": pw_hash}
        self._save_all(d)

    def get_by_username(self, username: str) -> Optional[dict]:
        return self._load_all().get(username)

    def verify(self, username: str, password: str) -> Optional[str]:
        rec = self.get_by_username(username)
        if not rec:
            return None
        _, h = hash_password(password, rec["salt"], settings.AUTH_PBKDF2_ITERATIONS)
        if hmac.compare_digest(h, rec["pw_hash"]):
            return rec["user_id"]
        return None

    def get(self, user_id: str) -> Optional[dict]:
        for rec in self._load_all().values():
            if rec.get("user_id") == user_id:
                return rec
        return None

    def backend(self) -> str:
        return "file"


class MySQLUserStore(UserStore):
    """MySQL 用户存储：落 movie_agent.users。构造仅探测表可读，失败回退 file。"""

    def __init__(self, file_root: str = "data/users", mysql_db: str = "movie_agent"):
        self._file = FileUserStore(file_root)
        self.use_mysql = False
        self._engine = None
        try:
            self._engine = create_engine(
                settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True, pool_recycle=3600
            )
            with self._engine.connect() as c:
                c.execute(text("SELECT 1 FROM users LIMIT 1"))
            self.use_mysql = True
            logger.info("MySQLUserStore: 已启用 movie_agent.users 作为用户存储")
        except Exception as e:  # noqa: BLE001
            self.use_mysql = False
            self._engine = None
            logger.warning(
                "MySQLUserStore: movie_agent.users 不可达或表不存在"
                "（请先执行 db/schema/movie_agent.sql 初始化），已回退 file 兜底：%s",
                e,
            )

    def create(self, user_id: str, username: str, salt: str, pw_hash: str) -> None:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    c.execute(
                        text(
                            "INSERT INTO users (username, user_id, salt, pw_hash, created_at) "
                            "VALUES (:u, :uid, :s, :h, NOW()) "
                            "ON DUPLICATE KEY UPDATE user_id=:uid, salt=:s, pw_hash=:h, "
                            "updated_at=NOW()"
                        ),
                        {"u": username, "uid": user_id, "s": salt, "h": pw_hash},
                    )
                    c.commit()
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLUserStore: create 失败，转 file：%s", e)
        self._file.create(user_id, username, salt, pw_hash)

    def get_by_username(self, username: str) -> Optional[dict]:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    row = c.execute(
                        text(
                            "SELECT user_id, salt, pw_hash FROM users WHERE username=:u"
                        ),
                        {"u": username},
                    ).fetchone()
                if row is not None:
                    return {"user_id": row[0], "salt": row[1], "pw_hash": row[2]}
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLUserStore: get_by_username 失败，转 file：%s", e)
        return self._file.get_by_username(username)

    def verify(self, username: str, password: str) -> Optional[str]:
        rec = self.get_by_username(username)
        if not rec:
            return None
        _, h = hash_password(password, rec["salt"], settings.AUTH_PBKDF2_ITERATIONS)
        if hmac.compare_digest(h, rec["pw_hash"]):
            return rec["user_id"]
        return None

    def get(self, user_id: str) -> Optional[dict]:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    row = c.execute(
                        text(
                            "SELECT username, salt, pw_hash FROM users WHERE user_id=:uid"
                        ),
                        {"uid": user_id},
                    ).fetchone()
                if row is not None:
                    return {"username": row[0], "salt": row[1], "pw_hash": row[2]}
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLUserStore: get 失败，转 file：%s", e)
        return self._file.get(user_id)

    def backend(self) -> str:
        return "mysql" if self.use_mysql else "mysql->file"


_store: UserStore | None = None


def get_user_store() -> UserStore:
    """返回用户存储单例（MySQL 优先 + file 兜底）。"""
    global _store
    if _store is None:
        _store = MySQLUserStore(file_root=settings.AUTH_FILE_ROOT)
    return _store
