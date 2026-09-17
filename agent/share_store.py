"""会话共享存储层（G 共享功能）：**强 MySQL**，唯一后端为 `movie_agent.shared_sessions` 表。

把「会话 -> 共享令牌」的映射持久化，供公开只读查看（/share/{token}，无需登录）使用。

强约束（2026-09-17 起，应用户要求移除 file 兜底，与 agent/session_store.py 同规则）：
- 业务代码**不含任何建库/建表语句**；表结构见 `db/schema/movie_agent.sql`（`shared_sessions`）。
- **fail-fast**：初始化时 MySQL 不可达 / 表不存在 -> 直接抛 RuntimeError，服务拒绝启动。
- **运行期任何 MySQL 异常直接向上抛**，绝不静默降级到文件。
- token 用 secrets.token_hex 生成（不可猜测），会话与令牌一一对应：一个会话至多一个有效令牌。
"""
import logging
import secrets
from typing import Optional

from sqlalchemy import create_engine, text

from config.settings import settings

logger = logging.getLogger("movie_agent.share")


class ShareStoreError(RuntimeError):
    """共享存储运行期错误（MySQL 读写失败等）；接口层据此返回 500。"""


def _new_token() -> str:
    return secrets.token_hex(16)  # 32 hex chars，密码学随机，不可猜测


class MySQLShareStore:
    """共享映射的强 MySQL 后端：落 movie_agent.shared_sessions；不可用即启动失败，无任何兜底。"""

    def __init__(self):
        try:
            self._engine = create_engine(
                settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True, pool_recycle=3600
            )
            with self._engine.connect() as c:
                c.execute(text("SELECT 1 FROM shared_sessions LIMIT 1"))
        except Exception as e:  # noqa: BLE001 - fail-fast：强 MySQL 模式下不启动服务
            raise RuntimeError(
                "共享存储初始化失败：movie_agent.shared_sessions 不可达或表不存在。"
                "本服务为强 MySQL 模式（无 file 兜底），请先初始化业务库"
                "（.venv/Scripts/python.exe scripts/init_movie_agent_db.py）并确认 MySQL 在线。"
                f"原始错误：{e}"
            ) from e
        logger.info("MySQLShareStore: 已启用 movie_agent.shared_sessions 作为共享存储（强 MySQL，无 file 兜底）")

    def create(self, session_id: str, owner_user_id: str) -> str:
        token = _new_token()
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

    def get_session_by_token(self, token: str) -> Optional[str]:
        with self._engine.connect() as c:
            r = c.execute(
                text("SELECT session_id FROM shared_sessions WHERE share_token=:tok"),
                {"tok": token},
            ).first()
        return r[0] if r is not None else None

    def get_token_by_session(self, session_id: str) -> Optional[str]:
        with self._engine.connect() as c:
            r = c.execute(
                text("SELECT share_token FROM shared_sessions WHERE session_id=:sid"),
                {"sid": session_id},
            ).first()
        return r[0] if r is not None else None

    def is_shared(self, session_id: str) -> bool:
        return self.get_token_by_session(session_id) is not None

    def delete(self, session_id: str) -> None:
        with self._engine.connect() as c:
            c.execute(
                text("DELETE FROM shared_sessions WHERE session_id=:sid"),
                {"sid": session_id},
            )
            c.commit()

    def backend(self) -> str:
        return "mysql"


_share_store: Optional[MySQLShareStore] = None


def get_share_store() -> MySQLShareStore:
    """共享存储单例（强 MySQL；不可用直接抛错 -> 服务启动失败）。"""
    global _share_store
    if _share_store is None:
        _share_store = MySQLShareStore()
    return _share_store
