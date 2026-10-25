"""token 吊销黑名单（P2-17）：logout 后服务端拒绝该 jti，直至 token 自然过期。

背景：AUTH_TOKEN_TTL 默认 7 天，此前 logout 只清 Cookie——服务端仍认已签发的
token，泄密 token 在剩余有效期内随时可用（用户点「退出登录」毫无意义）。现在
logout 把 token 的 jti 放进黑名单，verify_token 验签通过后先查黑名单。

后端：优先 Redis（多 worker 共享，SET key EX ttl 到期自动回收）；Redis 不可用
退进程内 dict（单 worker 有效；多 worker 部署需 Redis 才能全量吊销——不可用时
构造即记警告）。TTL 取 token 剩余寿命，过期项自然消失，无需后台清理。

API：revoke(jti, ttl_seconds) / is_revoked(jti) -> bool。best-effort：Redis
异常时退化内存并记日志，绝不抛异常阻断登录态校验主链路。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("movie_agent.token_blacklist")

_PREFIX = "auth:revoked:"
_MEM_MAX = 10000  # 进程内黑名单硬顶（防轮换 jti 打满内存）

_lock = threading.Lock()
_mem: dict[str, float] = {}   # jti -> 过期时间戳（time.time 基准）
_redis = None
_redis_tried = False


def _client():
    """懒加载 Redis 客户端（与 DualCache/限流同款模式）；不可用返回 None。"""
    global _redis, _redis_tried
    if _redis_tried:
        return _redis
    _redis_tried = True
    try:
        import redis

        from config.settings import settings

        c = redis.Redis.from_url(
            settings.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        c.ping()
        _redis = c
        logger.info("token 黑名单使用 Redis 后端（多 worker 共享吊销）")
    except Exception as e:  # noqa: BLE001 - 退化内存后端，不阻断主链路
        _redis = None
        logger.warning(
            "token 黑名单退进程内后端（Redis 不可用：%s）——多 worker 部署时 "
            "logout 仅本进程生效，需 Redis 才能全量吊销", e,
        )
    return _redis


def revoke(jti: str, ttl_seconds: float) -> None:
    """吊销 jti；ttl_seconds <= 0 视为已过期，跳过（无需吊销）。"""
    if not jti or ttl_seconds <= 0:
        return
    c = _client()
    if c is not None:
        try:
            c.set(_PREFIX + jti, "1", ex=max(1, int(ttl_seconds)))
            return
        except Exception as e:  # noqa: BLE001 - 退化内存
            logger.warning("token 黑名单 Redis 写入失败，改进程内：%s", e)
    now = time.time()
    with _lock:
        if len(_mem) >= _MEM_MAX and jti not in _mem:
            # 先批量清已过期的；仍满则逐出最早入库一项（字典保序）
            for k in [k for k, v in _mem.items() if v <= now]:
                _mem.pop(k, None)
            if len(_mem) >= _MEM_MAX:
                _mem.pop(next(iter(_mem)), None)
        _mem[jti] = now + ttl_seconds


def is_revoked(jti: str) -> bool:
    """jti 是否已被吊销（未过期）。任何后端异常都按「未吊销」放行并记日志——
    吊销是安全增强，不应因存储故障把全部用户踢下线。"""
    if not jti:
        return False
    c = _client()
    if c is not None:
        try:
            return bool(c.exists(_PREFIX + jti))
        except Exception as e:  # noqa: BLE001 - 退化内存查询
            logger.warning("token 黑名单 Redis 查询失败，改进程内：%s", e)
    now = time.time()
    with _lock:
        exp = _mem.get(jti)
        if exp is None:
            return False
        if exp <= now:
            _mem.pop(jti, None)
            return False
        return True


def _reset_for_test() -> None:
    """清空后端状态（测试隔离用）。"""
    global _redis, _redis_tried
    with _lock:
        _mem.clear()
    _redis = None
    _redis_tried = False
