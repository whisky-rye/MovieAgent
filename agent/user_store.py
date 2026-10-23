"""G1 用户账户存储：落 `movie_agent.users` 表，强 MySQL 单一后端。

强约束（2026-09-17 起，应用户要求移除 file 兜底）：
- 业务代码**不含任何建库/建表语句**；表结构见 `db/schema/movie_agent.sql`，需手动
  `scripts/init_movie_agent_db.py` 初始化。
- **fail-fast**：初始化时 MySQL 不可达 / 库或表不存在 -> 直接抛 RuntimeError，
  服务拒绝启动（api/routes/auth.py 经 get_user_store() 触发构造，异常即启动失败）。
- **运行期任何 MySQL 异常直接向上抛**（由接口层转 500），绝不静默降级到文件——
  宁可报错可见，不可数据分叉（MySQL/file 双写不一致比短暂不可用更危险）。
- 与 MySQLSessionStore / MySQLShareStore 同规则，全项目统一为「强 MySQL、无 file 兜底」。
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from sqlalchemy import create_engine, text

from agent.auth_crypto import hash_password
from config.settings import settings

logger = logging.getLogger("movie_agent.user_store")


class UserStoreError(RuntimeError):
    """用户存储运行期错误（MySQL 读写失败等）；接口层据此返回 500。"""


class MySQLUserStore:
    """用户账户的强 MySQL 后端：落 movie_agent.users；不可用即启动失败，无任何兜底。"""

    def __init__(self):
        try:
            self._engine = create_engine(
                settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True, pool_recycle=3600
            )
            # 仅探测表是否存在 / 可读；不建库、不建表（DDL 见 db/schema/movie_agent.sql）
            with self._engine.connect() as c:
                c.execute(text("SELECT 1 FROM users LIMIT 1"))
        except Exception as e:  # noqa: BLE001 - fail-fast：强 MySQL 模式下不启动服务
            raise RuntimeError(
                "用户存储初始化失败：movie_agent.users 不可达或表不存在。"
                "本服务为强 MySQL 模式（无 file 兜底），请先初始化业务库"
                "（.venv/Scripts/python.exe scripts/init_movie_agent_db.py）并确认 MySQL 在线。"
                f"原始错误：{e}"
            ) from e
        logger.info("MySQLUserStore: 已启用 movie_agent.users 作为用户存储（强 MySQL，无 file 兜底）")

    def create(self, user_id: str, username: str, salt: str, pw_hash: str) -> None:
        """新建用户（纯 INSERT，绝不覆盖）。

        username 为主键（唯一约束）：并发同名注册时后到者由数据库抛
        IntegrityError（路由层转 409），**绝不再静默覆盖他人账号**——
        此前 `ON DUPLICATE KEY UPDATE user_id/salt/pw_hash 全列覆盖` 会让
        并发注册的赢家把输家的 user_id/salt/pw_hash 改写掉，导致其 token
        失效、偏好/会话全部成孤儿数据（等同账号接管）。
        """
        with self._engine.connect() as c:
            c.execute(
                text(
                    "INSERT INTO users (username, user_id, salt, pw_hash, created_at) "
                    "VALUES (:u, :uid, :s, :h, NOW())"
                ),
                {"u": username, "uid": user_id, "s": salt, "h": pw_hash},
            )
            c.commit()

    def get_by_username(self, username: str) -> Optional[dict]:
        with self._engine.connect() as c:
            row = c.execute(
                text("SELECT user_id, salt, pw_hash FROM users WHERE username=:u"),
                {"u": username},
            ).fetchone()
        if row is None:
            return None
        return {"user_id": row[0], "salt": row[1], "pw_hash": row[2]}

    def verify(self, username: str, password: str) -> Optional[str]:
        rec = self.get_by_username(username)
        if not rec:
            return None
        _, h = hash_password(password, rec["salt"], settings.AUTH_PBKDF2_ITERATIONS)
        if hmac.compare_digest(h, rec["pw_hash"]):
            return rec["user_id"]
        return None

    def get(self, user_id: str) -> Optional[dict]:
        with self._engine.connect() as c:
            row = c.execute(
                text("SELECT username, salt, pw_hash FROM users WHERE user_id=:uid"),
                {"uid": user_id},
            ).fetchone()
        if row is None:
            return None
        return {"username": row[0], "salt": row[1], "pw_hash": row[2]}

    def backend(self) -> str:
        return "mysql"


_store: Optional[MySQLUserStore] = None


def get_user_store() -> MySQLUserStore:
    """用户存储单例（强 MySQL）。

    MySQL 不可达 / 表不存在时直接抛 RuntimeError -> 服务启动失败（fail-fast）。
    """
    global _store
    if _store is None:
        _store = MySQLUserStore()
    return _store
