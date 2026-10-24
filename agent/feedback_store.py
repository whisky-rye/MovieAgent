"""用户反馈闭环存储（强 MySQL；仅 movie_agent.user_feedback 表，DDL 见 db/schema/movie_agent.sql）。

- 构造时仅探测表是否可达（SELECT 1 FROM user_feedback LIMIT 1）；不可达/表缺失 → 抛 RuntimeError
  （fail-fast，与项目「强 MySQL 无 file 兜底」一致）。
- record / get_profile / clear 直接读写 MySQL，异常向上抛（接口层转 500），不静默降级。
- get_profile 把原始流水聚合成「反馈画像」：类型亲和度 / item 黑名单 / item 加分，带时间衰减。
"""
from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import Engine, bindparam, create_engine, text

from config.settings import settings

logger = logging.getLogger("movie_agent.feedback_store")

# —— 信号类型 ——
SIGNAL_EXPLICIT_LIKE = "explicit_like"
SIGNAL_EXPLICIT_DISLIKE = "explicit_dislike"
SIGNAL_CLICK = "click"
SIGNAL_IMPRESSION = "impression"
_VALID_SIGNALS = {
    SIGNAL_EXPLICIT_LIKE,
    SIGNAL_EXPLICIT_DISLIKE,
    SIGNAL_CLICK,
    SIGNAL_IMPRESSION,
}

# —— 聚合基础权重（叠加时间衰减） ——
_SIGNAL_WEIGHT = {
    SIGNAL_EXPLICIT_LIKE: 1.0,
    SIGNAL_EXPLICIT_DISLIKE: -1.0,
    SIGNAL_CLICK: 0.3,
    SIGNAL_IMPRESSION: -0.05,  # 已曝光未点：弱负，防反复推已读腻的
}

# —— 回灌排序时的强度（与 ranker/scorer._feedback_term 对称，避免分散配置） ——
ITEM_LIKE_BOOST = 0.6
ITEM_CLICK_BOOST = 0.3
ITEM_DISLIKE_PENALTY = -2.0
GENRE_AFFINITY_SCALE = 0.3

# 显式赞/踩：界面上一件作品只能二选一，存储侧同 item 只保留最新一条（见 record）
_EXPLICIT_SIGNALS = (SIGNAL_EXPLICIT_LIKE, SIGNAL_EXPLICIT_DISLIKE)

# —— P2-3：聚合成本控制（get_profile 不再每请求全表扫描用户流水）——
# 隐式信号（click/impression）时间窗：按半衰期折算，窗口外的隐式信号衰减已近零
# （_IMPLICIT_WINDOW_HALFLIVES 个半衰期 → 残留 <0.4%），不参与聚合；
# 显式赞/踩是用户意图，**不过窗**（record 已保证同 item 至多一条，量级很小）。
_IMPLICIT_WINDOW_HALFLIVES = 8
# 聚合行数硬顶：超限时保留**最新**一批（DESC + LIMIT 后反转回升序交 _aggregate）
_PROFILE_MAX_ROWS = 5000


def _implicit_window_days(half_life_days: int) -> int:
    """隐式信号聚合窗口（天）；半衰期 ≤0（不衰减）时按 30 天兜底，避免窗口为 0 把信号全滤掉。"""
    hl = half_life_days if half_life_days and half_life_days > 0 else 30
    return int(hl * _IMPLICIT_WINDOW_HALFLIVES)

# 存储信号 → 前端语义（GET /feedback/mine 用）
SIGNAL_TO_FRONT = {
    SIGNAL_EXPLICIT_LIKE: "like",
    SIGNAL_EXPLICIT_DISLIKE: "dislike",
}


class FeedbackStore(ABC):
    @abstractmethod
    def record(
        self, user_id: str, item_id: str, signal: str,
        item_kind: str = "movie", rank: Optional[int] = None,
        query: Optional[str] = None, genres: Optional[list[str]] = None,
        weight: float = 1.0,
    ) -> None: ...

    @abstractmethod
    def get_profile(self, user_id: str, half_life_days: int = 30) -> dict: ...

    @abstractmethod
    def get_explicit_map(self, user_id: str) -> dict: ...

    @abstractmethod
    def clear_item(self, user_id: str, item_id: str) -> None: ...

    @abstractmethod
    def clear(self, user_id: str) -> None: ...

    def record_impressions(
        self, user_id: str, query: str | None,
        rows: list[tuple[str, int, list[str] | None]],
    ) -> int:
        """批量记曝光（P2-3）。默认实现逐条 record；MySQL 子类覆写为单事务批量+去重。"""
        for item_id, rank, genres in rows:
            self.record(user_id, item_id, SIGNAL_IMPRESSION,
                        genres=genres, rank=rank, query=query)
        return len(rows)


class MySQLFeedbackStore(FeedbackStore):
    """强 MySQL 反馈存储：落 movie_agent.user_feedback。"""

    def __init__(self, engine: Optional[Engine] = None):
        try:
            self._engine = engine or create_engine(
                settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True, pool_recycle=3600
            )
            with self._engine.connect() as c:
                c.execute(text("SELECT 1 FROM user_feedback LIMIT 1"))
        except Exception as e:  # noqa: BLE001 - fail-fast：强 MySQL 模式不启动服务
            raise RuntimeError(
                "反馈存储初始化失败：movie_agent.user_feedback 不可达或表不存在。"
                "本服务为强 MySQL 模式（无 file 兜底），请先初始化业务库"
                "（.venv/Scripts/python.exe scripts/init_movie_agent_db.py）并确认 MySQL 在线。"
                f"原始错误：{e}"
            ) from e
        logger.info("MySQLFeedbackStore: 已启用 movie_agent.user_feedback 作为反馈库（强 MySQL）")

    def record(
        self, user_id: str, item_id: str, signal: str,
        item_kind: str = "movie", rank: Optional[int] = None,
        query: Optional[str] = None, genres: Optional[list[str]] = None,
        weight: float = 1.0,
    ) -> None:
        if signal not in _VALID_SIGNALS:
            raise ValueError(f"invalid feedback signal: {signal}")
        genres_str = ",".join(genres) if genres else None
        with self._engine.begin() as c:
            # 显式赞/踩互斥：同一 user+item 只保留「最新一条」显式信号。
            # 若两条并存（先踩后赞），_feedback_term 的黑名单分支优先命中，会导致
            # 界面已切成「喜欢」而排序仍按「踩」处理 —— 故写入前先清掉旧显式记录。
            # 隐式信号（click/impression）保持流水语义，不参与互斥。
            #
            # P1-2：DELETE+INSERT 两步在并发下会双写（两个请求都看到"无旧记录"），
            # 用 MySQL 命名锁把「删+插」串行化：跨连接/多 worker 生效，锁名取
            # user+item 的 md5（定长无碰撞）；会话结束（提交/回滚/连接回收）自动
            # 释放，无需改表结构。隐式信号高频流水，不加锁。
            locked = signal in _EXPLICIT_SIGNALS
            lock_name = ""
            if locked:
                lock_name = "fbx:" + hashlib.md5(
                    f"{user_id}:{item_id}".encode("utf-8")
                ).hexdigest()
                got = c.execute(
                    text("SELECT GET_LOCK(:k, 3)"), {"k": lock_name}
                ).scalar()
                if not got:
                    # 拿锁超时：不阻断反馈链路（读取侧 _aggregate 仍有"最新显式
                    # 信号优先"去重兜底），仅告警留痕
                    logger.warning(
                        "GET_LOCK 超时，反馈互斥降级为 best-effort",
                        extra={"event": "feedback_lock_timeout",
                               "user_id": user_id, "item_id": item_id},
                    )
            try:
                if locked:
                    c.execute(
                        text(
                            "DELETE FROM user_feedback WHERE `user_id`=:uid AND `item_id`=:iid "
                            "AND `signal` IN (:s1,:s2)"
                        ),
                        {
                            "uid": user_id, "iid": item_id,
                            "s1": SIGNAL_EXPLICIT_LIKE, "s2": SIGNAL_EXPLICIT_DISLIKE,
                        },
                    )
                c.execute(
                    text(
                        "INSERT INTO user_feedback "
                        "(`user_id`,`item_id`,`item_kind`,`signal`,`rank`,`query`,`genres`,`weight`) "
                        "VALUES (:uid,:iid,:kind,:sig,:rank,:q,:g,:w)"
                    ),
                    {
                        "uid": user_id, "iid": item_id, "kind": item_kind, "sig": signal,
                        "rank": rank, "q": query, "g": genres_str, "w": float(weight),
                    },
                )
            finally:
                if locked and lock_name:
                    c.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": lock_name})

    def record_impressions(
        self, user_id: str, query: str | None,
        rows: list[tuple[str, int, list[str] | None]],
    ) -> int:
        """批量记曝光（P2-3）：单事务多值 INSERT，替代每请求 N 次独立 record。

        rows: [(item_id, rank, genres), ...]
        - 去重：同 (user, item, query) 的旧曝光先删后插——重复推荐同一 query 只保留
          最新一批（位置偏差校正用最新名次），避免 user_feedback 以 top_k 行/请求
          线性膨胀；query 为空的曝光（无上下文）不去重、直接追加。
        - GET_LOCK(user, query) 串行化，防并发同 query 双写（与显式互斥同款思路）。
        """
        if not rows:
            return 0
        lock_name = ""
        if query:
            lock_name = "fbi:" + hashlib.md5(
                f"{user_id}:{query}".encode("utf-8")
            ).hexdigest()
        with self._engine.begin() as c:
            if lock_name:
                got = c.execute(
                    text("SELECT GET_LOCK(:k, 3)"), {"k": lock_name}
                ).scalar()
                if not got:
                    logger.warning(
                        "曝光互斥 GET_LOCK 超时，降级 best-effort",
                        extra={"event": "impression_lock_timeout", "user_id": user_id},
                    )
            try:
                if query:
                    # 同 query 旧曝光去重（ expanding IN ）
                    c.execute(
                        text(
                            "DELETE FROM user_feedback WHERE `user_id`=:uid AND `signal`=:sig "
                            "AND `query`=:q AND `item_id` IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"uid": user_id, "sig": SIGNAL_IMPRESSION,
                         "q": query, "ids": [r[0] for r in rows]},
                    )
                params = [
                    {"uid": user_id, "iid": item_id, "kind": "movie",
                     "sig": SIGNAL_IMPRESSION, "rank": rank, "q": query,
                     "g": ",".join(genres) if genres else None, "w": 1.0}
                    for item_id, rank, genres in rows
                ]
                # executemany：pymysql 会打成单条多值 INSERT（一次往返）
                c.execute(
                    text(
                        "INSERT INTO user_feedback "
                        "(`user_id`,`item_id`,`item_kind`,`signal`,`rank`,`query`,`genres`,`weight`) "
                        "VALUES (:uid,:iid,:kind,:sig,:rank,:q,:g,:w)"
                    ),
                    params,
                )
            finally:
                if lock_name:
                    c.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": lock_name})
        return len(params)

    def get_profile(self, user_id: str, half_life_days: int = 30) -> dict:
        """聚合反馈画像：类型亲和度 / item 黑名单 / item 加分（带时间衰减）。

        返回结构（直接喂给 ranker/scorer._feedback_term）：
            {"genre_affinity": {genre: [-1,1]}, "item_boost": {id: +}, "item_blacklist": {id: -}}

        P2-3：隐式信号按时间窗过滤 + 行数硬顶（曝光以 top_k 行/请求线性膨胀，
        此前每请求全表扫描该用户全部流水，聚合成本随之线性增长）；显式赞踩不过窗。
        取最新一批（created_at DESC + LIMIT）后反转回升序交 _aggregate（越靠后越新）。
        """
        since = datetime.now() - timedelta(days=_implicit_window_days(half_life_days))
        with self._engine.connect() as c:
            rows = c.execute(
                text(
                    "SELECT `signal`, `genres`, `item_id`, `weight`, `created_at` "
                    "FROM user_feedback WHERE `user_id`=:uid AND ("
                    "  `signal` IN (:s1,:s2) OR `created_at` >= :since"
                    ") ORDER BY `created_at` DESC, `id` DESC LIMIT :lim"
                ),
                {"uid": user_id, "s1": SIGNAL_EXPLICIT_LIKE,
                 "s2": SIGNAL_EXPLICIT_DISLIKE, "since": since,
                 "lim": _PROFILE_MAX_ROWS},
            ).fetchall()
        return _aggregate(list(reversed(rows)), datetime.now(), half_life_days)

    def get_explicit_map(self, user_id: str) -> dict:
        """返回 {item_id: "like"|"dislike"}：用户显式操作过的作品及其**当前**状态。

        供前端刷新 / 重进会话时恢复按钮选中态与视觉标记（GET /feedback/mine）。
        record 已保证同一 item 至多一条显式记录；这里仍按 created_at,id 升序覆盖，
        使「最后写入者胜出」在历史脏数据（旧版本曾 append-only）下也成立。
        """
        with self._engine.connect() as c:
            rows = c.execute(
                text(
                    "SELECT `item_id`, `signal` FROM user_feedback "
                    "WHERE `user_id`=:uid AND `signal` IN (:s1,:s2) "
                    "ORDER BY `created_at` ASC, `id` ASC"
                ),
                {
                    "uid": user_id,
                    "s1": SIGNAL_EXPLICIT_LIKE, "s2": SIGNAL_EXPLICIT_DISLIKE,
                },
            ).fetchall()
        out: dict[str, str] = {}
        for item_id, sig in rows:
            front = SIGNAL_TO_FRONT.get(sig)
            if front:
                out[item_id] = front
        return out

    def clear_item(self, user_id: str, item_id: str) -> None:
        """撤销某作品的显式赞/踩（前端「再点一次取消」）。隐式信号不受影响。"""
        with self._engine.begin() as c:
            c.execute(
                text(
                    "DELETE FROM user_feedback WHERE `user_id`=:uid AND `item_id`=:iid "
                    "AND `signal` IN (:s1,:s2)"
                ),
                {
                    "uid": user_id, "iid": item_id,
                    "s1": SIGNAL_EXPLICIT_LIKE, "s2": SIGNAL_EXPLICIT_DISLIKE,
                },
            )

    def clear(self, user_id: str) -> None:
        with self._engine.begin() as c:
            c.execute(text("DELETE FROM user_feedback WHERE user_id=:uid"), {"uid": user_id})


def _aggregate(rows, now: datetime | None = None, half_life_days: int = 30) -> dict:
    """纯函数：把反馈流水聚合成画像（抽离以便离线单测，不依赖 MySQL/驱动）。

    rows: 可迭代的 (signal, genres, item_id, weight, created_at) 元组，须按
          created_at,id 升序（越靠后越新）。
    now 省略时取当前时间（离线单测可显式传入以固定衰减基准）。

    信号权重见模块级 ``_SIGNAL_WEIGHT``（现已真正生效）：
      - explicit_like / explicit_dislike：显式赞踩 → 加分 / 黑名单（强降权）
      - click：点击 → 弱加分
      - impression：已曝光未点 → 弱负（``_SIGNAL_WEIGHT[SIGNAL_IMPRESSION]``），
        仅对「未显式赞/踩」的作品降权，防反复推已读腻的；
        不会抹掉显式赞/踩带来的正向分。
    """
    if now is None:
        now = datetime.now()
    rows = list(rows)

    # 显式赞/踩互斥兜底：同一 item 只认「最新一条」显式信号（行序越后越新）。
    # record 已在写入侧做互斥（同 item 只留一条），这里再兜一层历史脏数据，
    # 保证「前端显示的状态」与「排序回灌用的画像」口径完全一致。
    latest_explicit: dict[str, int] = {}
    for pos, (signal, _g, item_id, _w, _ts) in enumerate(rows):
        if signal in _EXPLICIT_SIGNALS:
            latest_explicit[item_id] = pos
    explicit_ids = set(latest_explicit.keys())

    genre_aff: dict[str, float] = {}
    pos_boost: dict[str, float] = {}   # 显式赞/点击 正向加分
    imp_pen: dict[str, float] = {}     # 曝光未点 弱负（仅对未显式互动的作品）
    item_black: dict[str, float] = {}

    for pos, (signal, genres, item_id, weight, created_at) in enumerate(rows):
        if signal in _EXPLICIT_SIGNALS and latest_explicit.get(item_id) != pos:
            continue  # 该 item 存在更新的显式信号，跳过这条旧记录
        age_days = max(0, (now - (created_at or now)).days)
        decay = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
        w = float(weight or 1.0) * decay

        if signal == SIGNAL_EXPLICIT_DISLIKE:
            item_black[item_id] = ITEM_DISLIKE_PENALTY
        elif signal == SIGNAL_EXPLICIT_LIKE:
            pos_boost[item_id] = max(pos_boost.get(item_id, 0.0), ITEM_LIKE_BOOST)
        elif signal == SIGNAL_CLICK:
            pos_boost[item_id] = max(pos_boost.get(item_id, 0.0), ITEM_CLICK_BOOST)
        elif signal == SIGNAL_IMPRESSION:
            # 已曝光未点：弱负，防反复推；仅对「未显式赞/踩」的作品生效，
            # 因此不会把用户已赞/点过的作品也一起压下去。
            if item_id not in explicit_ids:
                imp_pen[item_id] = (
                    imp_pen.get(item_id, 0.0)
                    + _SIGNAL_WEIGHT[SIGNAL_IMPRESSION] * w
                )
        # 注：impression 不计入类型亲和度（曝光行通常无 genres，且不表达偏好）

        # 类型亲和度：仅显式赞踩携带 genres 才有意义
        if genres and signal in (SIGNAL_EXPLICIT_LIKE, SIGNAL_EXPLICIT_DISLIKE):
            sgn = 1.0 if signal == SIGNAL_EXPLICIT_LIKE else -1.0
            for g in genres.split(","):
                g = g.strip()
                if g:
                    genre_aff[g] = max(-1.0, min(1.0, genre_aff.get(g, 0.0) + sgn * w))

    # 合并正向加分与曝光弱负（曝光负分只降权未互动作品，不影响已赞/点）
    item_boost: dict[str, float] = {}
    for item_id in set(pos_boost) | set(imp_pen):
        item_boost[item_id] = pos_boost.get(item_id, 0.0) + imp_pen.get(item_id, 0.0)

    return {
        "genre_affinity": genre_aff,
        "item_boost": item_boost,
        "item_blacklist": item_black,
    }


_store: Optional[FeedbackStore] = None


def get_feedback_store() -> FeedbackStore:
    """懒构造单例（首次调用才连 MySQL，避免 import 期 fail-fast 阻断无反馈路径）。"""
    global _store
    if _store is None:
        _store = MySQLFeedbackStore()
    return _store
