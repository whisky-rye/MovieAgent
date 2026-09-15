"""偏好记忆模块：支持 file / redis / fallback 三种后端。

设计（与 api/cache.py 的 DualCache 优先级思路一致）：
- FileMemoryStore   ：开发期零依赖、可离线、便于 Debug 的本地 JSON 文件存储。
- RedisMemoryStore  ：上线期 Redis 存储（长/短期偏好 + 历史），单 key + JSON 持久化。
- FallbackMemoryStore：组合后端——Redis 优先，Redis 连接/读写异常时自动回退 FileMemoryStore，
                       绝不阻塞主推荐链路（与 DualCache 的兜底语义对齐）。
- 工厂 get_memory_store() 按 settings.MEMORY_BACKEND 选择后端：
  fallback（默认）= Redis 优先 + file 兜底；redis = 仅 Redis；file = 仅文件。
"""
import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Optional

from redis import Redis
from sqlalchemy import create_engine, text

from config.settings import settings

logger = logging.getLogger("movie_agent.memory")


class MemoryStore(ABC):
    @abstractmethod
    def load(self, user_id: str) -> dict: ...

    @abstractmethod
    def save(self, user_id: str, data: dict) -> None: ...

    # 后端标识 / 健康探测（便于 /health 观测；子类可覆盖）
    def backend(self) -> str:
        return "abstract"

    def health(self) -> bool:
        return True

    def get_prefs(self, user_id: str) -> dict:
        return self.load(user_id).get("preferences", {})

    def add_pref(
        self,
        user_id: str,
        genres: list[str] | None = None,
        note: str | None = None,
    ) -> dict:
        d = self.load(user_id)
        prefs = d.setdefault("preferences", {})
        if genres:
            cur = prefs.setdefault("genres", [])
            for g in genres:
                if g and g not in cur:
                    cur.append(g)
        if note:
            prefs["note"] = note
        self.save(user_id, d)
        return prefs

    def push_history(self, user_id: str, item: dict) -> None:
        d = self.load(user_id)
        d.setdefault("history", []).append(item)
        self.save(user_id, d)


class FileMemoryStore(MemoryStore):
    def __init__(self, root: str = "data/memory"):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, uid: str) -> str:
        return os.path.join(self.root, f"{uid}.json")

    def load(self, uid: str) -> dict:
        p = self._path(uid)
        if not os.path.exists(p):
            return {"user_id": uid, "preferences": {}, "history": []}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001 - 文件损坏则回退默认结构
            logger.warning("FileMemoryStore: 读取 %s 失败，回退默认：%s", p, e)
            return {"user_id": uid, "preferences": {}, "history": []}

    def save(self, uid: str, data: dict) -> None:
        with self._lock:
            with open(self._path(uid), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    def backend(self) -> str:
        return "file"

    def health(self) -> bool:
        return os.path.isdir(self.root)


class RedisMemoryStore(MemoryStore):
    """Redis 存储：以 mem:{user_id} 为 key 持久化整份 JSON（无 TTL，长存）。

    连接参数复用 settings 的 REDIS_*，protocol=2 兼容老版本 Redis 3.x。
    构造时 ping，连不上由调用方（FallbackMemoryStore/工厂）决定兜底。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 0,
        password: str = "",
        protocol: int = 2,
        prefix: str = "mem:",
    ):
        self._prefix = prefix
        self._lock = threading.Lock()
        self._r = Redis(
            host=host,
            port=port,
            db=db,
            password=password or None,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            protocol=protocol,
        )
        self._r.ping()  # 不可达则抛异常，交给上层兜底

    def _key(self, uid: str) -> str:
        return f"{self._prefix}{uid}"

    def load(self, uid: str) -> dict:
        raw = self._r.get(self._key(uid))
        if raw is None:
            return {"user_id": uid, "preferences": {}, "history": []}
        return json.loads(raw)

    def save(self, uid: str, data: dict) -> None:
        with self._lock:
            self._r.set(self._key(uid), json.dumps(data, ensure_ascii=False))

    def backend(self) -> str:
        return "redis"

    def health(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:  # noqa: BLE001
            return False


class FallbackMemoryStore(MemoryStore):
    """组合后端：Redis 优先，Redis 不可达/读写异常时回退 FileMemoryStore。

    - 启动 ping Redis；不可达则 use_redis=False，全程走 file（与 DualCache 一致）。
    - 每次 load/save 若 Redis 抛异常，自动回退 file，绝不阻塞主链路。
    - load 在 Redis 返回空默认结构时额外回退 file，兼容「原 file 存量数据」的迁移期。
    """

    def __init__(
        self,
        file_root: str = "data/memory",
        host: str = "127.0.0.1",
        port: int = 6379,
        db: int = 0,
        password: str = "",
        protocol: int = 2,
    ):
        self._file = FileMemoryStore(file_root)
        self.use_redis = False
        self._redis: Optional[RedisMemoryStore] = None
        try:
            self._redis = RedisMemoryStore(
                host=host, port=port, db=db, password=password, protocol=protocol
            )
            self.use_redis = True
            logger.info("FallbackMemoryStore: 使用 Redis 优先，file 兜底（%s:%s/db%s）",
                        host, port, db)
        except Exception as e:  # noqa: BLE001 - 任何连接/协议错误都走 file 兜底
            self._redis = None
            self.use_redis = False
            logger.warning("FallbackMemoryStore: Redis 不可用，全部走 file 兜底：%s", e)

    def _non_empty(self, d: dict) -> bool:
        return bool(d.get("preferences") or d.get("history"))

    def load(self, uid: str) -> dict:
        if self.use_redis and self._redis is not None:
            try:
                d = self._redis.load(uid)
                if self._non_empty(d):
                    return d
                # Redis 为空（迁移期/新用户）：回退 file，兼容既有 file 存量
                file_d = self._file.load(uid)
                if self._non_empty(file_d):
                    return file_d
                return d
            except Exception as e:  # noqa: BLE001
                logger.warning("FallbackMemoryStore: Redis load 失败，转 file：%s", e)
        return self._file.load(uid)

    def save(self, uid: str, data: dict) -> None:
        if self.use_redis and self._redis is not None:
            try:
                self._redis.save(uid, data)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("FallbackMemoryStore: Redis save 失败，转 file：%s", e)
        self._file.save(uid, data)

    def backend(self) -> str:
        return "redis" if self.use_redis else "file"

    def health(self) -> bool:
        if self.use_redis and self._redis is not None:
            return self._redis.health()
        return self._file.health()


class MySQLMemoryStore(MemoryStore):
    """偏好记忆的 MySQL 后端：落 `movie_agent.user_preferences` 表（与 imdb 语料库隔离）。

    设计要点（与既有 FallbackMemoryStore「绝不阻塞主链路」原则一致）：
    - 业务代码**严禁**包含任何建库/建表语句。表结构由独立文件 `db/schema/movie_agent.sql` 定义，
      需手动执行 `scripts/init_movie_agent_db.py` 初始化。
    - 构造时仅「探测」表是否存在 / 可读（SELECT 1 FROM user_preferences LIMIT 1）；
      表不存在或不可达则 use_mysql=False，全程回退 FileMemoryStore，不抛异常、不阻塞主推荐链路。
    - load/save 每次调用若 MySQL 抛异常，自动转 file 兜底。
    - 单一 JSON 字段 data 存整份 {user_id, preferences, history}，与 FallbackMemoryStore 结构完全一致，
      因此 Agent（graph.py / tools.py 的 remember_preference）与前端 /memory 接口共用同一张表 = 真正打通。
    """

    def __init__(
        self,
        file_root: str = "data/memory",
        mysql_db: str = "movie_agent",
    ):
        self._file = FileMemoryStore(file_root)
        self.use_mysql = False
        self._engine = None
        try:
            # 仅探测表是否存在 / 可读；不建库、不建表（DDL 见 db/schema/movie_agent.sql）
            self._engine = create_engine(
                settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True, pool_recycle=3600
            )
            with self._engine.connect() as c:
                c.execute(text("SELECT 1 FROM user_preferences LIMIT 1"))
            self.use_mysql = True
            logger.info("MySQLMemoryStore: 已启用 movie_agent.user_preferences 作为偏好记忆库")
        except Exception as e:  # noqa: BLE001
            self.use_mysql = False
            self._engine = None
            logger.warning(
                "MySQLMemoryStore: movie_agent.user_preferences 不可达或表不存在"
                "（请先执行 db/schema/movie_agent.sql 初始化），已回退 file 兜底：%s",
                e,
            )

    def load(self, uid: str) -> dict:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    row = c.execute(
                        text("SELECT data FROM user_preferences WHERE user_id=:uid"),
                        {"uid": uid},
                    ).fetchone()
                if row is not None:
                    raw = row[0]
                    return raw if isinstance(raw, dict) else json.loads(raw)
                return {"user_id": uid, "preferences": {}, "history": []}
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLMemoryStore: load 失败，转 file：%s", e)
        return self._file.load(uid)

    def save(self, uid: str, data: dict) -> None:
        if self.use_mysql and self._engine is not None:
            try:
                with self._engine.connect() as c:
                    c.execute(
                        text(
                            "INSERT INTO user_preferences (user_id, data, updated_at) "
                            "VALUES (:uid, :data, NOW()) "
                            "ON DUPLICATE KEY UPDATE data=:data, updated_at=NOW()"
                        ),
                        {"uid": uid, "data": json.dumps(data, ensure_ascii=False)},
                    )
                    c.commit()
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("MySQLMemoryStore: save 失败，转 file：%s", e)
        self._file.save(uid, data)

    def backend(self) -> str:
        return "mysql" if self.use_mysql else "mysql->file"


def _make_store() -> MemoryStore:
    """按 settings.MEMORY_BACKEND 构建后端单例。"""
    backend = (settings.MEMORY_BACKEND or "fallback").strip().lower()
    if backend == "mysql":
        # 落 movie_agent.user_preferences 表；连不上自动回退 file（见 MySQLMemoryStore）
        return MySQLMemoryStore(
            file_root=settings.MEMORY_FILE_ROOT,
            mysql_db=settings.MOVIE_AGENT_MYSQL_DB,
        )
    if backend == "file":
        return FileMemoryStore(settings.MEMORY_FILE_ROOT)
    if backend == "redis":
        return RedisMemoryStore(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD,
        )
    # fallback（默认）：Redis 优先 + file 兜底
    return FallbackMemoryStore(
        file_root=settings.MEMORY_FILE_ROOT,
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD,
    )


_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """工厂：返回记忆后端单例（按 settings.MEMORY_BACKEND 选择）。"""
    global _store
    if _store is None:
        _store = _make_store()
    return _store
