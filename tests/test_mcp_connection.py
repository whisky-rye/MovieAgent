"""MCP 只读连接层单测（红线第二道闸门 + 稳定性）。

覆盖：
- 建连后**必须**执行 `SET SESSION TRANSACTION READ ONLY`（会话只读）；
- 护栏拒绝时**绝不建立/借用连接**（写操作连"尝试"都不触达远端）；
- 限流（令牌桶）与熔断（连续失败 → fail-fast → 冷却恢复）行为；
- 审计日志**不泄露凭据**；
- 连接池借还与健康检查。
"""
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: E402
import mcp_prod.connection as conn_mod  # noqa: E402
from mcp_prod.connection import (  # noqa: E402
    CircuitBreaker,
    McpCircuitOpen,
    McpRateLimited,
    McpReadOnlyViolation,
    ProdReadOnlyClient,
    ReadOnlyPool,
    TokenBucket,
)


# ---------------------------------------------------------------------------
# 假 pymysql（记录执行的语句，不连真库）
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if self._conn.fail_on and self._conn.fail_on in sql:
            raise RuntimeError(self._conn.fail_on)
        self._rows = list(self._conn.rows_to_return)
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConn:
    def __init__(self, db, rows_to_return=None, fail_on=None):
        self.db = db
        self.executed = []
        self.rows_to_return = rows_to_return or []
        self.fail_on = fail_on
        self.closed = False
        self.open = True

    def cursor(self):
        return FakeCursor(self)

    def ping(self, reconnect=True):
        return True

    def close(self):
        self.closed = True
        self.open = False


class FakePymysql:
    """替换 mcp_prod.connection.pymysql 的假模块。"""

    def __init__(self, rows_to_return=None, fail_on=None):
        self.connections = []
        self.rows_to_return = rows_to_return or []
        self.fail_on = fail_on
        self.cursors = types.SimpleNamespace(DictCursor=dict)

    def connect(self, **kwargs):
        conn = FakeConn(kwargs.get("database"), self.rows_to_return, self.fail_on)
        conn.kwargs = kwargs
        self.connections.append(conn)
        return conn


@pytest.fixture
def fake_pymysql(monkeypatch):
    fake = FakePymysql()
    monkeypatch.setattr(conn_mod, "pymysql", fake)
    return fake


def _client(fake=None, **over):
    """构造使用假 pymysql 的只读客户端。"""
    s = types.SimpleNamespace(**{**vars(settings)})
    for k, v in over.items():
        setattr(s, k, v)
    s.MCP_DB_HOST = s.MCP_DB_HOST or "127.0.0.1"
    s.MCP_DB_USER = s.MCP_DB_USER or "ro"
    s.MCP_DB_PASSWORD = s.MCP_DB_PASSWORD or "pw"
    c = ProdReadOnlyClient(settings_obj=s)
    return c


# ---------------------------------------------------------------------------
# 会话只读
# ---------------------------------------------------------------------------

def test_session_read_only_set_on_connect(fake_pymysql):
    c = _client()
    c.query("imdb2", "SELECT * FROM douban_main WHERE douban_id = %s", ("1",))
    conn = fake_pymysql.connections[0]
    stmts = [s for s, _ in conn.executed]
    assert any("TRANSACTION READ ONLY" in s.upper() for s in stmts), \
        f"建连后必须设置会话只读，实际: {stmts}"


def test_connection_uses_configured_db_and_credentials(fake_pymysql):
    c = _client()
    c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    kw = fake_pymysql.connections[0].kwargs
    assert kw["database"] == "imdb2"
    assert kw["user"] == c.s.MCP_DB_USER
    assert kw["password"] == c.s.MCP_DB_PASSWORD
    assert kw["autocommit"] is True


def test_not_configured_raises(fake_pymysql):
    c = _client()
    c.s.MCP_DB_HOST = ""
    c.s.MCP_DB_USER = ""
    c.s.MCP_DB_PASSWORD = ""
    with pytest.raises(conn_mod.McpNotConfigured):
        c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    assert fake_pymysql.connections == [], "未配置时不得建立任何连接"


def test_not_configured_does_not_open_circuit(fake_pymysql):
    """配置问题不应被计入熔断（否则误报熔断、掩盖真实问题）。"""
    c = _client()
    c.s.MCP_DB_HOST = ""
    for _ in range(5):
        with pytest.raises(conn_mod.McpNotConfigured):
            c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    assert c.health()["circuit_open"] is False


# ---------------------------------------------------------------------------
# 护栏拒绝 → 绝不触达连接
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "DELETE FROM douban_main",
    "UPDATE douban_main SET title='x'",
    "DROP TABLE douban_main",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM other_db.imdb_main",
    "SELECT 1; DROP TABLE douban_main",
])
def test_guard_violation_never_touches_connection(fake_pymysql, sql):
    """🔴 核心红线测试：写操作被护栏拦下时，连接根本没被建立。"""
    c = _client()
    with pytest.raises(McpReadOnlyViolation):
        c.query("imdb2", sql)
    assert fake_pymysql.connections == [], "护栏拒绝后不得建立任何数据库连接"


def test_guard_violation_does_not_count_as_failure(fake_pymysql):
    """护栏拒绝不应触发熔断（不是数据库故障）。"""
    c = _client()
    for _ in range(5):
        with pytest.raises(McpReadOnlyViolation):
            c.query("imdb2", "DELETE FROM douban_main")
    assert not c.health()["circuit_open"]


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------

def test_token_bucket_consume_and_refill():
    b = TokenBucket(rate_per_min=60, burst=2)
    assert b.consume()[0] is True
    assert b.consume()[0] is True
    ok, wait = b.consume()
    assert ok is False and wait > 0


def test_rate_limit_raises():
    c = _client(MCP_RATE_LIMIT_QPM=1, MCP_RATE_LIMIT_BURST=1)
    c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    with pytest.raises(McpRateLimited):
        c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")


def test_rate_limit_violation_not_reported_as_db_error(fake_pymysql):
    c = _client(MCP_RATE_LIMIT_QPM=1, MCP_RATE_LIMIT_BURST=1)
    c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    with pytest.raises(McpRateLimited):
        c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    assert not c.health()["circuit_open"]


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------

def test_circuit_breaker_opens_after_threshold():
    cb = CircuitBreaker(threshold=3, cooldown=60)
    for _ in range(3):
        cb.on_failure()
    assert cb.is_open is True
    with pytest.raises(McpCircuitOpen):
        cb.check()


def test_circuit_breaker_resets_on_success():
    cb = CircuitBreaker(threshold=3, cooldown=60)
    cb.on_failure()
    cb.on_success()
    cb.on_failure()
    cb.on_failure()
    assert cb.is_open is False


def test_circuit_breaker_half_open_after_cooldown():
    cb = CircuitBreaker(threshold=1, cooldown=1)
    cb.on_failure()
    assert cb.is_open is True
    time.sleep(1.1)
    cb.check()   # 冷却结束放行探测
    assert cb.is_open is False


def test_query_failures_open_circuit(fake_pymysql):
    fake_pymysql.fail_on = "SELECT * FROM douban_main"
    c = _client(MCP_CIRCUIT_FAIL_THRESHOLD=2, MCP_CIRCUIT_COOLDOWN=60)
    for _ in range(2):
        with pytest.raises(Exception):
            c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    assert c.health()["circuit_open"] is True
    with pytest.raises(McpCircuitOpen):
        c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")


# ---------------------------------------------------------------------------
# 审计日志不泄露凭据
# ---------------------------------------------------------------------------

def test_audit_log_contains_no_credentials(fake_pymysql, caplog):
    c = _client()
    c.s.MCP_DB_PASSWORD = "SUPER_SECRET_PW"
    with caplog.at_level("INFO", logger="movie_agent.mcp"):
        c.query("imdb2", "SELECT * FROM douban_main WHERE douban_id = %s", ("1",),
                tool="t")
    text = caplog.text
    assert "SUPER_SECRET_PW" not in text
    assert c.s.MCP_DB_PASSWORD not in text


def test_audit_log_records_tool_and_rowcount(fake_pymysql, caplog):
    fake_pymysql.rows_to_return = [{"a": 1}, {"a": 2}]
    c = _client()
    with caplog.at_level("INFO", logger="movie_agent.mcp"):
        c.query("imdb2", "SELECT * FROM douban_main LIMIT 2", tool="search_movie")
    assert "search_movie" in caplog.text


# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------

def test_pool_reuses_connections(fake_pymysql):
    c = _client()
    for _ in range(3):
        c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    assert len(fake_pymysql.connections) == 1, "同库应复用连接"


def test_pool_separates_by_db(fake_pymysql):
    c = _client()
    c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    c.query("imdb", "SELECT * FROM imdb_main LIMIT 1")
    assert len(fake_pymysql.connections) == 2


def test_pool_release_and_close(fake_pymysql):
    c = _client()
    c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    c.close()
    assert fake_pymysql.connections[0].closed is True


def test_query_result_shape(fake_pymysql):
    fake_pymysql.rows_to_return = [{"a": 1}]
    c = _client()
    r = c.query("imdb2", "SELECT * FROM douban_main LIMIT 1")
    d = r.to_dict()
    assert d["row_count"] == 1
    assert d["rows"] == [{"a": 1}]
    assert "elapsed_ms" in d and "db" in d


def test_health_reports_config(fake_pymysql):
    c = _client()
    h = c.health()
    assert h["configured"] is True
    assert "circuit_open" in h and "pools" in h
