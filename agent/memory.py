"""偏好记忆模块（G4）：落 `movie_agent.user_preferences` 表，**强 MySQL 单一后端**。

强约束（2026-09-17 起，应用户要求移除 file / redis / fallback 兜底）：
- 业务代码**严禁**包含任何建库/建表语句。表结构由 `db/schema/movie_agent.sql` 定义，
  需手动执行 `scripts/init_movie_agent_db.py` 初始化。
- **fail-fast**：初始化时 MySQL 不可达 / 表不存在 -> 直接抛 RuntimeError，服务拒绝启动
  （api/main.py 模块级调用 get_memory_store() 触发构造，异常即启动失败）。
- **运行期任何 MySQL 异常直接向上抛**，绝不静默降级到文件 / Redis——
  宁可报错可见，不可数据分叉（MySQL/file 双写不一致比短暂不可用更危险）。
- 单一 JSON 列 `data` 存整份 {user_id, preferences, history}，与 Agent（graph.py /
  tools.py 的 remember_preference）及前端 /memory 接口共用同一张表 = 真正的单一数据源。
"""
import json
import logging
from abc import ABC, abstractmethod
from typing import Optional

from sqlalchemy import create_engine, text

from config.settings import settings
from retrieval.candidates import normalize_genres

logger = logging.getLogger("movie_agent.memory")


class MemoryStore(ABC):
    """偏好记忆抽象：整份 {user_id, preferences, history} 的读写。"""

    @abstractmethod
    def load(self, user_id: str) -> dict: ...

    @abstractmethod
    def save(self, user_id: str, data: dict) -> None: ...

    def update(self, user_id: str, mutator):
        """原子读-改-写（P1-2）：单事务内 加锁读 → mutator 改 → 写回。

        背景：add_pref / sediment_preferences / push_history 早期都是
        load→改→save 三次独立连接操作；同一用户并发请求（多标签页、流式推荐与
        偏好沉淀同时打）会互相覆盖丢更新。默认实现退化为 load/mutator/save
        （无锁，供非 MySQL 后端）；MySQL 子类以 `SELECT ... FOR UPDATE` 覆盖
        为真原子。mutator 原地修改 data，返回值原样透传。
        """
        d = self.load(user_id)
        out = mutator(d)
        self.save(user_id, d)
        return out

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
        # P1-1：类型必须过词表闸门——非标准类型（正则切出的汉语片段 / LLM 幻觉）
        # 一旦入库就会作为内容召回过滤条件下推，把召回结果打空。
        clean = normalize_genres(genres)

        def _mut(d: dict) -> dict:
            prefs = d.setdefault("preferences", {})
            if clean:
                cur = prefs.setdefault("genres", [])
                for g in clean:
                    if g not in cur:
                        cur.append(g)
            if note:
                prefs["note"] = note
            return prefs

        return self.update(user_id, _mut)

    def push_history(self, user_id: str, item: dict) -> None:
        def _mut(d: dict) -> None:
            d.setdefault("history", []).append(item)

        self.update(user_id, _mut)

    def sediment_preferences(
        self, user_id: str, *,
        genres: list[str] | None = None,
        directors: list[str] | None = None,
        actors: list[str] | None = None,
        people: list[str] | None = None,
        note: str | None = None,
        history_item: dict | None = None,
    ) -> dict:
        """G4 自动沉淀：把本轮抽取到的结构化偏好合并进画像（去重），并可选追加一条历史。

        - genres 过词表闸门（P1-1）后去重合并；directors/actors/people 为人名/片名，
          自由文本不做词表校验；
        - note 覆盖写（latest wins）；
        - history_item 非 None 时追加到 history（避免无限膨胀由调用方控制频率）；
        - 经 self.update 单事务原子完成（P1-2），子类无需各自实现。
        """
        clean_genres = normalize_genres(genres)

        def _mut(d: dict) -> dict:
            prefs = d.setdefault("preferences", {})

            def _merge(key: str, vals) -> None:
                if not vals:
                    return
                cur = prefs.setdefault(key, [])
                for v in vals:
                    if v and v not in cur:
                        cur.append(v)

            _merge("genres", clean_genres)
            _merge("directors", directors)
            _merge("actors", actors)
            _merge("people", people)
            if note:
                prefs["note"] = note
            if history_item:
                d.setdefault("history", []).append(history_item)
            return prefs

        return self.update(user_id, _mut)


class MySQLMemoryStore(MemoryStore):
    """偏好记忆的强 MySQL 后端：落 `movie_agent.user_preferences` 表（与 imdb 语料库隔离）。

    - 构造时仅「探测」表是否存在 / 可读（SELECT 1 FROM user_preferences LIMIT 1）；
      不可达 / 表缺失 -> 直接抛 RuntimeError（fail-fast），服务拒绝启动，无 file 兜底。
    - load/save 直接读写 MySQL，任何异常向上抛（接口层转 500），不静默降级。
    - 单一 JSON 列 data 存整份 {user_id, preferences, history}。
    """

    def __init__(self):
        try:
            self._engine = create_engine(
                settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True, pool_recycle=3600
            )
            with self._engine.connect() as c:
                c.execute(text("SELECT 1 FROM user_preferences LIMIT 1"))
        except Exception as e:  # noqa: BLE001 - fail-fast：强 MySQL 模式下不启动服务
            raise RuntimeError(
                "偏好记忆存储初始化失败：movie_agent.user_preferences 不可达或表不存在。"
                "本服务为强 MySQL 模式（无 file 兜底），请先初始化业务库"
                "（.venv/Scripts/python.exe scripts/init_movie_agent_db.py）并确认 MySQL 在线。"
                f"原始错误：{e}"
            ) from e
        logger.info(
            "MySQLMemoryStore: 已启用 movie_agent.user_preferences 作为偏好记忆库"
            "（强 MySQL，无 file 兜底）"
        )

    def load(self, uid: str) -> dict:
        with self._engine.connect() as c:
            row = c.execute(
                text("SELECT data FROM user_preferences WHERE user_id=:uid"),
                {"uid": uid},
            ).fetchone()
        if row is None:
            return {"user_id": uid, "preferences": {}, "history": []}
        raw = row[0]
        return raw if isinstance(raw, dict) else json.loads(raw)

    def save(self, uid: str, data: dict) -> None:
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

    def backend(self) -> str:
        return "mysql"

    def update(self, user_id: str, mutator):
        """原子读-改-写（P1-2）：单事务 `SELECT ... FOR UPDATE` → mutator → upsert。

        user_id 是主键：行存在则锁该行直到事务提交；行不存在时 InnoDB 在 RR
        隔离下对主键空隙加 gap lock，两个并发「新用户首次写入」同样被串行化，
        不会各持一份旧数据互相覆盖。
        """
        with self._engine.begin() as c:
            row = c.execute(
                text("SELECT data FROM user_preferences WHERE user_id=:uid FOR UPDATE"),
                {"uid": user_id},
            ).fetchone()
            if row is None:
                d = {"user_id": user_id, "preferences": {}, "history": []}
            else:
                raw = row[0]
                d = raw if isinstance(raw, dict) else json.loads(raw)
            out = mutator(d)
            c.execute(
                text(
                    "INSERT INTO user_preferences (user_id, data, updated_at) "
                    "VALUES (:uid, :data, NOW()) "
                    "ON DUPLICATE KEY UPDATE data=:data, updated_at=NOW()"
                ),
                {"uid": user_id, "data": json.dumps(d, ensure_ascii=False)},
            )
        return out

    def health(self) -> bool:
        try:
            with self._engine.connect() as c:
                c.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False


_store: Optional[MySQLMemoryStore] = None


def get_memory_store() -> MySQLMemoryStore:
    """工厂：返回偏好记忆的强 MySQL 单例（无 file / redis / fallback 兜底）。

    MySQL 不可达 / 表不存在时直接抛 RuntimeError -> 服务启动失败（fail-fast）。
    """
    global _store
    if _store is None:
        _store = MySQLMemoryStore()
    return _store
