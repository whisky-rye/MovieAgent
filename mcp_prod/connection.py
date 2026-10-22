"""生产库只读连接层（MCP 通道）。

🔴 红线：连接建立后立即执行 `SET SESSION TRANSACTION READ ONLY`（MySQL 8 会话级只读，
已在目标实例 8.0.46 实测生效），并叠加应用层护栏（mcp_prod/guard.py）双保险。

工程要点：
- **凭据隔离**：仅从 settings/env 注入，无默认值；审计日志绝不打印凭据。
- **连接池**：按库各一个线程安全小池（避免频繁建连打爆生产库）；连接归还前做健康检查。
- **限流**：令牌桶（每分钟 QPM + 突发），超限抛 `McpRateLimited`。
- **熔断**：连续失败达阈值即打开熔断，冷却期内快速失败（fail-fast），避免持续冲击
  生产库；成功一次即恢复（半开）。
- **审计**：每次查询记录 tool/库表/行数/耗时，参数只记长度不记明文（防敏感信息落盘）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import pymysql

from config.settings import settings
from mcp_prod.guard import ReadOnlyViolation, assert_readonly

logger = logging.getLogger("movie_agent.mcp")


class McpError(RuntimeError):
    """MCP 通道通用异常（对外统一为可读错误）。"""


class McpNotConfigured(McpError):
    """未配置远端只读凭据（视为通道不可用，调用方应降级）。"""


class McpRateLimited(McpError):
    """触发限流（防打爆生产库）。"""

    def __init__(self, retry_after: float = 1.0):
        self.retry_after = retry_after
        super().__init__(f"MCP 查询限流，请 {retry_after:.1f}s 后重试")


class McpCircuitOpen(McpError):
    """熔断已打开（快速失败，避免持续冲击生产库）。"""


class McpReadOnlyViolation(McpError):
    """SQL 未通过只读护栏（绝不触达数据库连接）。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"只读护栏拦截：{reason}")


# ---------------------------------------------------------------------------
# 令牌桶限流
# ---------------------------------------------------------------------------

class TokenBucket:
    """线程安全令牌桶（按 QPM + 突发容量）。"""

    def __init__(self, rate_per_min: float, burst: float):
        self.rate_per_sec = max(rate_per_min, 0.0) / 60.0
        self.capacity = max(float(burst), 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: float = 1.0) -> tuple[bool, float]:
        """尝试取令牌，返回 (是否成功, 建议等待秒数)。"""
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last
            if elapsed > 0:
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_sec)
                self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True, 0.0
            deficit = tokens - self._tokens
            wait = deficit / self.rate_per_sec if self.rate_per_sec > 0 else 60.0
            return False, wait

    def reset(self) -> None:
        with self._lock:
            self._tokens = self.capacity
            self._last = time.monotonic()


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------

@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float = 0.0
    is_open: bool = False


class CircuitBreaker:
    """简单熔断器：连续失败 N 次打开，冷却后半开，成功即恢复。"""

    def __init__(self, threshold: int, cooldown: int):
        self.threshold = max(1, int(threshold))
        self.cooldown = max(1, int(cooldown))
        self._state = _CircuitState()
        self._lock = threading.Lock()

    def _is_open(self) -> bool:
        with self._lock:
            if not self._state.is_open:
                return False
            if time.monotonic() - self._state.opened_at >= self.cooldown:
                # 冷却结束 → 半开，放行一次探测
                self._state.is_open = False
                self._state.failures = 0
                return False
            return True

    def check(self) -> None:
        if self._is_open():
            with self._lock:
                remain = self.cooldown - (time.monotonic() - self._state.opened_at)
            raise McpCircuitOpen(f"熔断中（连续失败达阈值），{max(0, remain):.0f}s 后恢复")

    def on_success(self) -> None:
        with self._lock:
            self._state.failures = 0
            self._state.is_open = False

    def on_failure(self) -> None:
        with self._lock:
            self._state.failures += 1
            if self._state.failures >= self.threshold and not self._state.is_open:
                self._state.is_open = True
                self._state.opened_at = time.monotonic()
                logger.error("MCP 通道熔断打开：连续失败 %s 次", self._state.failures,
                             extra={"event": "mcp_circuit_open"})

    @property
    def is_open(self) -> bool:
        return self._is_open()


# ---------------------------------------------------------------------------
# 只读连接池
# ---------------------------------------------------------------------------

class ReadOnlyPool:
    """按库维护的只读连接池（pymysql 同步驱动 + 线程安全借还）。"""

    def __init__(self, db: str, size: int, connect_fn):
        self.db = db
        self.size = max(1, int(size))
        self._connect = connect_fn
        self._idle: deque = deque()
        self._lock = threading.Lock()
        self._created = 0

    def _new_conn(self):
        conn = self._connect(self.db)
        self._created += 1
        return conn

    def acquire(self, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._idle:
                    conn = self._idle.popleft()
                    self._created -= 1
                    break
                if self._created < self.size:
                    self._created += 1
                    conn = None
                    break
            if time.monotonic() >= deadline:
                raise McpError("获取只读连接超时（连接池耗尽）")
            time.sleep(0.05)
        if conn is None:
            try:
                conn = self._new_conn()
            except Exception:
                # 建连失败必须归还已预占的槽位，否则池容量被"幽灵连接"占满
                with self._lock:
                    self._created = max(0, self._created - 1)
                raise
        try:
            conn.ping()
        except Exception:  # noqa: BLE001 - 连接失效则重建
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn = self._new_conn()
            except Exception:
                with self._lock:
                    self._created = max(0, self._created - 1)
                raise
        return conn

    def release(self, conn, broken: bool = False) -> None:
        try:
            if broken:
                conn.close()
                with self._lock:
                    self._created = max(0, self._created - 1)
                return
            with self._lock:
                if len(self._idle) < self.size:
                    self._idle.append(conn)
                    return
            conn.close()
            with self._lock:
                self._created = max(0, self._created - 1)
        except Exception:  # noqa: BLE001
            try:
                conn.close()
            except Exception:
                pass

    def close_all(self) -> None:
        with self._lock:
            while self._idle:
                try:
                    self._idle.popleft().close()
                except Exception:
                    pass
            self._created = 0


# ---------------------------------------------------------------------------
# 只读客户端
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    """只读查询结果。"""

    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    elapsed_ms: int = 0
    db: str = ""
    tables: list[str] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "row_count": self.row_count,
            "elapsed_ms": self.elapsed_ms,
            "db": self.db,
            "tables": self.tables,
            "truncated": self.truncated,
        }


class ProdReadOnlyClient:
    """生产库只读客户端（唯一允许触达远端连接的类）。

    所有查询必须经 `guard.assert_readonly`；调用方应使用参数化占位符。
    """

    def __init__(self, settings_obj=None):
        self.s = settings_obj or settings
        self._pools: dict[str, ReadOnlyPool] = {}
        self._pool_lock = threading.Lock()
        self._bucket = TokenBucket(self.s.MCP_RATE_LIMIT_QPM, self.s.MCP_RATE_LIMIT_BURST)
        self._breaker = CircuitBreaker(
            self.s.MCP_CIRCUIT_FAIL_THRESHOLD, self.s.MCP_CIRCUIT_COOLDOWN)

    # —— 配置 ——
    @property
    def configured(self) -> bool:
        return bool(self.s.MCP_DB_HOST and self.s.MCP_DB_USER and self.s.MCP_DB_PASSWORD)

    def _require_config(self) -> None:
        if not self.configured:
            raise McpNotConfigured(
                "未配置生产库只读凭据（MCP_DB_HOST/MCP_DB_USER/MCP_DB_PASSWORD）")

    def db_for_source(self, source: str) -> str:
        """source(imdb/douban) → 库名。"""
        s = (source or "douban").lower()
        if s.startswith("imdb"):
            return self.s.MCP_DB_IMDB
        if s.startswith("douban"):
            return self.s.MCP_DB_DOUBAN
        raise McpError(f"未知数据源：{source}")

    # —— 连接 ——
    def _connect(self, db: str):
        """建立只读连接并立即置为会话只读。"""
        self._require_config()
        conn = pymysql.connect(
            host=self.s.MCP_DB_HOST,
            port=int(self.s.MCP_DB_PORT),
            user=self.s.MCP_DB_USER,
            password=self.s.MCP_DB_PASSWORD,
            database=db,
            charset="utf8mb4",
            connect_timeout=int(self.s.MCP_CONNECT_TIMEOUT),
            read_timeout=int(self.s.MCP_QUERY_TIMEOUT),
            write_timeout=int(self.s.MCP_QUERY_TIMEOUT),
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
        with conn.cursor() as cur:
            # 🔴 会话级只读（双保险）：MySQL 8 支持 SET SESSION TRANSACTION READ ONLY
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            # 单语句执行超时（毫秒），防慢查询长期占用生产库连接
            try:
                cur.execute("SET SESSION max_execution_time=%s", (int(self.s.MCP_QUERY_TIMEOUT) * 1000,))
            except Exception:  # noqa: BLE001 - 旧版本不支持则忽略
                pass
        return conn

    def _pool(self, db: str) -> ReadOnlyPool:
        with self._pool_lock:
            pool = self._pools.get(db)
            if pool is None:
                pool = ReadOnlyPool(db, self.s.MCP_POOL_SIZE, self._connect)
                self._pools[db] = pool
            return pool

    # —— 查询 ——
    def query(
        self,
        db: str,
        sql: str,
        params: Optional[tuple | dict] = None,
        max_limit: Optional[int] = None,
        tool: str = "",
        allow_metadata: bool = False,
    ) -> QueryResult:
        """执行只读查询。

        顺序：护栏校验（最内层闸门）→ 限流 → 熔断检查 → 执行 → 审计。
        护栏不通过时**绝不建立/借用连接**（连"尝试"都不触达远端）。
        `allow_metadata=True` 仅放行受控的 information_schema 只读元数据查询。
        """
        limit = max_limit or int(self.s.MCP_MAX_LIMIT)
        # ① 护栏（红线闸门）——失败即抛，且不触达数据库
        try:
            safe_sql = assert_readonly(
                sql, max_limit=limit, allow_metadata=allow_metadata,
                allowed_schemas=(self.s.MCP_DB_IMDB, self.s.MCP_DB_DOUBAN),
            )
        except ReadOnlyViolation as e:
            metrics_incr("mcp_guard_rejected_total", 1, {"tool": tool or "-"})
            logger.error("MCP 只读护栏拦截", extra={
                "event": "mcp_guard_rejected", "tool": tool or "-",
                "reason": str(e)[:200]})
            raise McpReadOnlyViolation(str(e)) from e

        # ② 限流
        ok, wait = self._bucket.consume()
        if not ok:
            metrics_incr("mcp_rate_limited_total", 1, {"tool": tool or "-"})
            raise McpRateLimited(wait)

        # ③ 熔断
        self._breaker.check()

        pool = self._pool(db)
        conn = None
        t0 = time.time()
        try:
            conn = pool.acquire()
            with conn.cursor() as cur:
                cur.execute(safe_sql, params or ())
                rows = list(cur.fetchall() or [])
            elapsed_ms = int((time.time() - t0) * 1000)
            self._breaker.on_success()
            result = QueryResult(
                rows=rows,
                row_count=len(rows),
                elapsed_ms=elapsed_ms,
                db=db,
                truncated=len(rows) >= limit,
            )
            self._audit(tool, db, result, ok=True)
            metrics_incr("mcp_queries_total", 1, {"tool": tool or "-", "status": "ok"})
            metrics_observe("mcp_query_duration_ms", elapsed_ms, {"tool": tool or "-"})
            return result
        except Exception as e:  # noqa: BLE001
            # 未配置凭据属配置问题（非数据库故障）→ 不计入熔断
            if not isinstance(e, McpNotConfigured):
                self._breaker.on_failure()
            self._audit(tool, db, None, ok=False, error=str(e))
            metrics_incr("mcp_queries_total", 1, {"tool": tool or "-", "status": "error"})
            logger.warning("MCP 查询失败：%s", str(e)[:200],
                           extra={"event": "mcp_query_error", "tool": tool or "-"})
            raise
        finally:
            if conn is not None:
                broken = False
                try:
                    if conn.open is False:
                        broken = True
                except Exception:  # noqa: BLE001
                    broken = True
                pool.release(conn, broken=broken)

    def _audit(self, tool: str, db: str, result: Optional[QueryResult],
               ok: bool, error: str = "") -> None:
        """审计日志（不记录凭据、不记录参数明文）。"""
        if not self.s.MCP_AUDIT_LOG:
            return
        extra = {
            "event": "mcp_query",
            "tool": tool or "-",
            "db": db,
            "status": "ok" if ok else "error",
        }
        if result is not None:
            extra["row_count"] = result.row_count
            extra["elapsed_ms"] = result.elapsed_ms
        if error:
            extra["err"] = error[:200]
        logger.info("MCP 查询 %s db=%s rows=%s lat=%sms",
                    tool or "-", db,
                    result.row_count if result else 0,
                    result.elapsed_ms if result else 0,
                    extra=extra)

    # —— 运维 ——
    def health(self) -> dict:
        """健康检查（不触发查询，仅报告配置/熔断/限流状态）。"""
        return {
            "configured": self.configured,
            "enabled": bool(self.s.MCP_ENABLED),
            "circuit_open": self._breaker.is_open,
            "host": self.s.MCP_DB_HOST or "-",
            "pools": {db: p.size for db, p in self._pools.items()},
        }

    def close(self) -> None:
        for p in self._pools.values():
            p.close_all()


# —— 指标埋点（可选依赖，observability 缺失时静默降级）——
def _metrics():
    try:
        from observability.metrics import metrics
        return metrics
    except Exception:  # noqa: BLE001
        return None


def metrics_incr(name: str, n: float = 1, tags: dict | None = None) -> None:
    m = _metrics()
    if m is not None:
        m.incr(name, n, tags)


def metrics_observe(name: str, v: float, tags: dict | None = None) -> None:
    m = _metrics()
    if m is not None:
        m.observe(name, v, tags)


# —— 全局单例 ——
_CLIENT: Optional[ProdReadOnlyClient] = None
_CLIENT_LOCK = threading.Lock()


def get_prod_client() -> ProdReadOnlyClient:
    """全局只读客户端单例。"""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = ProdReadOnlyClient()
    return _CLIENT


def reset_prod_client() -> None:
    """重置单例（测试用）。"""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
        _CLIENT = None
