"""MCP 只读护栏单测（红线第一道闸门）。

覆盖：
- 写操作/DDL/权限/事务控制语句必须被拒；
- 多语句注入、注释绕过、危险函数、越权表/库必须被拒；
- 合法只读查询（含 CTE、派生表、竖线字段、参数占位符）必须放行；
- LIMIT 必须被强制钳制；参数化 LIMIT 必须被拒（护栏需能验证行数上限）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_prod.guard import (  # noqa: E402
    DEFAULT_MAX_LIMIT,
    HARD_MAX_LIMIT,
    ReadOnlyViolation,
    assert_readonly,
    check_sql,
    is_readonly,
)


# ---------------------------------------------------------------------------
# 必须被拒绝的语句
# ---------------------------------------------------------------------------

WRITE_STATEMENTS = [
    "DELETE FROM imdb_main",
    "UPDATE imdb_main SET title='x' WHERE id=1",
    "INSERT INTO imdb_main (id) VALUES (1)",
    "REPLACE INTO imdb_main VALUES (1)",
    "TRUNCATE TABLE imdb_main",
    "ALTER TABLE imdb_main ADD COLUMN x INT",
    "DROP TABLE imdb_main",
    "CREATE TABLE t (id INT)",
    "RENAME TABLE imdb_main TO x",
    "GRANT SELECT ON imdb.* TO 'u'@'%'",
    "REVOKE SELECT ON imdb.* FROM 'u'@'%'",
    "COMMIT",
    "ROLLBACK",
    "START TRANSACTION",
    "LOCK TABLES imdb_main WRITE",
    "UNLOCK TABLES",
    "CALL some_procedure()",
    "DO 1",
    "PREPARE s FROM 'SELECT 1'",
    "EXECUTE s",
    "DEALLOCATE PREPARE s",
    "SET GLOBAL read_only = 0",
    "USE mysql",
    "FLUSH PRIVILEGES",
    "KILL 12345",
    "LOAD DATA INFILE '/etc/passwd' INTO TABLE imdb_main",
    "LOAD XML INFILE '/tmp/x' INTO TABLE imdb_main",
    "HANDLER imdb_main OPEN",
]


@pytest.mark.parametrize("sql", WRITE_STATEMENTS)
def test_write_statements_rejected(sql):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE imdb_main",
    "SELECT * FROM imdb_main; DELETE FROM imdb_main",
    "SELECT * FROM imdb_main; UPDATE imdb_main SET x=1",
    "SELECT * FROM imdb_main --\n; DROP TABLE douban_main",
    "SELECT * FROM imdb_main /* c */; /* x */ DROP TABLE douban_main",
])
def test_multi_statement_rejected(sql):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql)


@pytest.mark.parametrize("sql", [
    "SE/**/LECT * FROM imdb_main",
    "SEL/**/ECT * FROM imdb_main",
])
def test_comment_bypass_rejected(sql):
    """注释绕过必须失效（剥离注释后不再是合法只读语句 → 拒绝）。"""
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql)


def test_comment_between_tokens_is_allowed():
    """注释夹在合法 token 之间：剥离后仍是合法只读查询 → 放行。"""
    assert is_readonly("SELECT/**/1 FROM imdb_main") is True


@pytest.mark.parametrize("sql", [
    "SELECT * FROM imdb_main -- trailing",
    "SELECT * FROM imdb_main # trailing",
    "SELECT * FROM imdb_main /* mid */ WHERE imdb_id = %s",
])
def test_trailing_comments_stripped_and_allowed(sql):
    """正常注释被剥离后查询仍合法（剥离不影响语义）。"""
    assert is_readonly(sql) is True


@pytest.mark.parametrize("sql", [
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT SLEEP(10)",
    "SELECT BENCHMARK(1000000, MD5('a'))",
    "SELECT GET_LOCK('x', 10)",
])
def test_dangerous_functions_rejected(sql):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM imdb_main INTO OUTFILE '/tmp/x'",
    "SELECT * FROM imdb_main INTO DUMPFILE '/tmp/x'",
    "SELECT 1 INTO @var",
])
def test_into_outfile_rejected(sql):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM mysql.user",
    "SELECT * FROM performance_schema.accounts",
    "SELECT * FROM sys.host_summary",
])
def test_forbidden_schemas_rejected(sql):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM secret_table",
    "SELECT * FROM users",
    "SELECT * FROM sys_config",
    "SELECT * FROM imdb.main",
    "SELECT * FROM other_db.imdb_main",
])
def test_non_whitelisted_tables_rejected(sql):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM imdb_main FOR UPDATE",
    "SELECT * FROM imdb_main LOCK IN SHARE MODE",
])
def test_locking_clauses_rejected(sql):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql)


def test_empty_sql_rejected():
    with pytest.raises(ReadOnlyViolation):
        check_sql("")
    with pytest.raises(ReadOnlyViolation):
        check_sql("   ")


# ---------------------------------------------------------------------------
# 必须放行的只读语句
# ---------------------------------------------------------------------------

LEGIT_QUERIES = [
    "SELECT * FROM imdb_main WHERE imdb_id = %s",
    "SELECT id, title_text_cn FROM douban_main WHERE title_text_cn LIKE %s LIMIT 10",
    "SELECT * FROM imdb_main ORDER BY rate_agg DESC LIMIT 20",
    "SELECT COUNT(*) FROM imdb_name_relate",
    "SELECT * FROM douban_genre_relate WHERE douban_id IN %s",
    "SELECT * FROM v_imdb_douban_compare LIMIT 10",
    "SELECT * FROM imdb.imdb_main LIMIT 1",
    "SELECT a.* FROM imdb_main a JOIN imdb_name_relate b ON a.imdb_id=b.imdb_id",
    "SELECT * FROM imdb_main WHERE title = 'a;b'",
    "SELECT * FROM imdb_main WHERE note = 'has -- dash'",
    "SELECT * FROM imdb_main WHERE note = '/* not comment */'",
    "WITH t AS (SELECT imdb_id FROM imdb_main LIMIT 5) SELECT * FROM t",
    "WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS y) SELECT * FROM a JOIN b",
    "WITH RECURSIVE r AS (SELECT imdb_id FROM imdb_main LIMIT 3) SELECT * FROM r",
    "SELECT * FROM (SELECT imdb_id FROM imdb_main) x",
    "SELECT * FROM (SELECT imdb_id FROM imdb_main LIMIT 3) AS y",
    "SELECT DISTINCT genre_name FROM douban_genre_relate",
    "SELECT * FROM imdb_main WHERE release_date_year >= %s",
    "SELECT imdb_title, douban_title_cn FROM v_imdb_douban_compare WHERE imdb_title LIKE %s",
]


@pytest.mark.parametrize("sql", LEGIT_QUERIES)
def test_legit_queries_allowed(sql):
    d = check_sql(sql)
    assert d.allowed is True
    assert d.reason == "ok"


def test_cte_name_not_treated_as_table():
    """CTE 别名不应被当作物理表拦截。"""
    d = check_sql("WITH t AS (SELECT imdb_id FROM imdb_main) SELECT * FROM t")
    assert d.allowed is True


def test_cte_does_not_exempt_real_tables():
    """CTE 体内的真实表仍需过表白名单。"""
    with pytest.raises(ReadOnlyViolation):
        check_sql("WITH t AS (SELECT 1) SELECT * FROM secret_table")


def test_cte_wrapping_write_rejected():
    with pytest.raises(ReadOnlyViolation):
        check_sql("WITH t AS (SELECT imdb_id FROM imdb_main) DELETE FROM t")


# ---------------------------------------------------------------------------
# LIMIT 强制
# ---------------------------------------------------------------------------

def test_limit_appended_when_missing():
    out = check_sql("SELECT * FROM imdb_main").rewritten_sql
    assert f"LIMIT {DEFAULT_MAX_LIMIT}" in out


def test_limit_clamped_when_too_large():
    out = check_sql("SELECT * FROM imdb_main LIMIT 99999").rewritten_sql
    assert f"LIMIT {DEFAULT_MAX_LIMIT}" in out


def test_limit_kept_when_within_cap():
    out = check_sql("SELECT * FROM imdb_main LIMIT 10").rewritten_sql
    assert "LIMIT 10" in out


def test_limit_offset_count_form_clamped():
    out = check_sql("SELECT * FROM imdb_main LIMIT 5, 99999").rewritten_sql
    assert f"LIMIT 5, {DEFAULT_MAX_LIMIT}" in out


def test_hard_max_limit_enforced():
    out = check_sql("SELECT * FROM imdb_main", max_limit=999999).rewritten_sql
    assert f"LIMIT {HARD_MAX_LIMIT}" in out


def test_parameterized_limit_rejected():
    """参数化 LIMIT 无法被护栏验证行数上限 → 必须拒绝。"""
    with pytest.raises(ReadOnlyViolation):
        check_sql("SELECT * FROM imdb_main LIMIT %s")


# ---------------------------------------------------------------------------
# 元数据只读通道（受控放行）
# ---------------------------------------------------------------------------

def test_metadata_blocked_by_default():
    with pytest.raises(ReadOnlyViolation):
        check_sql("SELECT table_name FROM information_schema.tables WHERE table_schema='imdb'")


def test_metadata_allowed_with_optin_and_schema_filter():
    sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = %s"
    d = check_sql(sql, allow_metadata=True)
    assert d.allowed is True


def test_metadata_requires_schema_filter():
    sql = "SELECT table_name FROM information_schema.tables"
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql, allow_metadata=True)


def test_metadata_only_readonly_objects():
    sql = ("SELECT * FROM information_schema.user_privileges WHERE table_schema = %s")
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql, allow_metadata=True)


def test_metadata_cannot_access_business_tables():
    sql = ("SELECT * FROM information_schema.tables t JOIN imdb_main m "
           "ON t.table_name = m.imdb_id WHERE t.table_schema = %s")
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql, allow_metadata=True)


# ---------------------------------------------------------------------------
# 表引用提取盲区回归（P0-3）：逗号连接 / STRAIGHT_JOIN / 系统库限定引用 /
# 元数据过滤值校验
# ---------------------------------------------------------------------------

def test_comma_join_forbidden_table_rejected():
    """逗号隐式连接不得成为表白名单盲区（曾实测可被绕过）。"""
    for sql in (
        "SELECT id FROM imdb_main, mysql.user LIMIT 1",
        "SELECT id FROM imdb_main, movie_agent.users LIMIT 1",
        "SELECT id FROM imdb_main m, mysql.user u LIMIT 1",
    ):
        with pytest.raises(ReadOnlyViolation):
            check_sql(sql)


def test_straight_join_forbidden_table_rejected():
    """STRAIGHT_JOIN 曾是 _TABLE_REF 盲区（\bjoin\b 不匹配 straight_join）。"""
    with pytest.raises(ReadOnlyViolation):
        check_sql("SELECT id FROM imdb_main STRAIGHT_JOIN mysql.user ON 1=1 LIMIT 1")


def test_schema_qualified_system_reference_rejected_even_without_from():
    """兜底扫描：任何位置的系统库限定引用都被拒（不依赖表提取路径）。"""
    with pytest.raises(ReadOnlyViolation):
        check_sql("SELECT id FROM imdb_main WHERE x = mysql.user LIMIT 1")


def test_literal_containing_system_schema_not_false_positive():
    """影片名等字面量内容不算表引用，不得误伤（兜底扫描走掩码视图）。"""
    d = check_sql("SELECT title FROM imdb_main WHERE title = 'mysql.user' LIMIT 10")
    assert d.allowed is True


def test_legit_comma_join_allowed():
    """合法逗号连接必须放行，且两张表都应被提取到。"""
    d = check_sql("SELECT a.id, b.title FROM imdb_main a, douban_main b "
                  "WHERE a.imdb_id = b.imdb_id LIMIT 10")
    assert d.allowed is True
    assert set(d.tables) == {"imdb_main", "douban_main"}


def test_legit_group_by_columns_not_treated_as_tables():
    """GROUP BY / ORDER BY 后的列名不得被逗号续扫误当成表（终止符守护）。"""
    sql = ("SELECT title, year FROM imdb_main "
           "GROUP BY title, year ORDER BY year, title LIMIT 10")
    d = check_sql(sql)
    assert d.allowed is True
    assert d.tables == ["imdb_main"]


def test_metadata_literal_schema_value_validated():
    """元数据过滤字面量值必须是白名单库（曾实测 table_schema='mysql' 被放行）。"""
    with pytest.raises(ReadOnlyViolation):
        check_sql("SELECT table_name FROM information_schema.tables "
                  "WHERE table_schema = 'mysql' LIMIT 1", allow_metadata=True)


def test_metadata_literal_schema_value_allowed():
    d = check_sql("SELECT table_name FROM information_schema.tables "
                  "WHERE table_schema = 'imdb' LIMIT 1", allow_metadata=True)
    assert d.allowed is True


def test_metadata_in_list_values_validated():
    ok = ("SELECT table_name FROM information_schema.tables "
          "WHERE table_schema IN ('imdb','imdb2') LIMIT 1")
    assert check_sql(ok, allow_metadata=True).allowed is True
    bad = ("SELECT table_name FROM information_schema.tables "
           "WHERE table_schema IN ('imdb','mysql') LIMIT 1")
    with pytest.raises(ReadOnlyViolation):
        check_sql(bad, allow_metadata=True)


# ---------------------------------------------------------------------------
# 辅助接口
# ---------------------------------------------------------------------------

def test_assert_readonly_returns_executable_sql():
    out = assert_readonly("SELECT * FROM imdb_main WHERE imdb_id = %s", max_limit=50)
    assert out.startswith("SELECT")
    assert "LIMIT 50" in out


def test_is_readonly_boolean():
    assert is_readonly("SELECT * FROM imdb_main") is True
    assert is_readonly("DELETE FROM imdb_main") is False


def test_decision_reports_tables():
    d = check_sql("SELECT * FROM imdb_main JOIN douban_main ON 1=1")
    assert "imdb_main" in d.tables and "douban_main" in d.tables


def test_guard_is_pure_no_db_import():
    """护栏必须零数据库依赖（可离线单测）。"""
    import mcp_prod.guard as g
    src = open(g.__file__, encoding="utf-8").read()
    for bad in ("import pymysql", "pymysql.connect", "sqlalchemy"):
        assert bad not in src
