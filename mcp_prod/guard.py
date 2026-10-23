"""生产库只读护栏（红线核心）。

🔴 红线：远端生产库 `imdb` / `imdb2` **仅允许 SELECT**。本模块是所有 SQL 触达
数据库连接之前的最后一道闸门，任何写入/元数据/越权语句必须在**进入连接之前**被拒绝
（连"尝试"都不发到远端）。

纵深防御（从外到内）：
1. 语义校验：只读语句白名单 + 表前缀白名单 + 危险子句/函数黑名单。
2. 语法加固：剥离注释后重新解析（防 `SE/**/LECT`、换行注入）；拒绝多语句（`;`）。
3. 表引用全覆盖：FROM/JOIN/STRAIGHT_JOIN + 逗号隐式连接 + 反引号/库名限定，
   另对系统库限定引用做独立兜底扫描（不依赖单一提取路径）。
4. 强制 LIMIT：自动追加并钳制上限，杜绝全表拉取打爆生产库。
5. 参数化：外部输入一律走 `%s` 占位符绑定（由 queries 层保证），护栏拒绝字面量拼接
   出的高危形态。

本模块为**纯函数、零依赖**（不 import 数据库驱动），可完整离线单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mcp_prod import schema_map as sm

# ---------------------------------------------------------------------------
# 白名单 / 黑名单定义
# ---------------------------------------------------------------------------

#: 允许的只读语句起始关键字（WITH 最终也必须是 SELECT，由 _is_select_only 递归校验）
ALLOWED_START_KEYWORDS = ("SELECT", "WITH")

#: 允许访问的表前缀（生产库 imdb_* / douban_*）
ALLOWED_TABLE_PREFIXES = ("imdb_", "douban_")

#: 额外允许的已登记对象（如跨源比对视图 v_imdb_douban_compare —— 非前缀命名，
#: 但已在 schema_map 中登记为合法只读对象）。以 schema_map 为唯一真源，避免
#: 护栏与查询层各维护一份白名单而漂移。
ALLOWED_REGISTERED_TABLES = frozenset(sm.all_tables())

#: 允许显式限定的库（schema）——防止用 `other_db.imdb_main` 形式跨库读取同实例其它库
ALLOWED_SCHEMAS = ("imdb", "imdb2")

#: 禁止访问的库/模式（防元数据探测、越权、系统表读取）
FORBIDDEN_SCHEMAS = (
    "information_schema", "mysql", "performance_schema", "sys",
)

#: 元数据只读通道：允许读取的 information_schema 对象（仅结构元数据，不含业务数据）
METADATA_ALLOWED_OBJECTS = (
    "tables", "columns", "statistics", "key_column_usage",
    "referential_constraints", "table_constraints", "views",
)

#: 元数据通道必须携带的库过滤条件（防止全实例元数据扫描）
_METADATA_SCHEMA_FILTER = re.compile(
    r"\btable_schema\s*(=|in)\s*", re.I
)

#: 元数据过滤值字面量（table_schema = 'imdb' / table_schema IN ('imdb','imdb2')）
_SCHEMA_VALUE_EQ = re.compile(r"\btable_schema\s*=\s*'([^']*)'", re.I)
_SCHEMA_VALUE_IN = re.compile(r"\btable_schema\s+in\s*\(([^)]*)\)", re.I)
_SCHEMA_VALUE_IN_ITEM = re.compile(r"'([^']*)'")

#: 任意位置的「库名.对象」限定引用（兜底扫描）：即便表提取被绕过，
#: 对系统库的限定引用也必须在此拦截（纵深防御，不依赖单一提取路径）。
_ANY_SCHEMA_QUALIFIED = re.compile(
    r"\b(`?[A-Za-z_][\w$]*`?)\s*\.\s*(`?[A-Za-z_][\w$]*`?)", re.I
)

#: 任何通道都永远禁止引用的系统库（allow_metadata 也不放行）
_ALWAYS_FORBIDDEN_SCHEMAS = ("mysql", "performance_schema", "sys")

#: 禁止出现的语句关键字（覆盖写操作、事务控制、权限变更、落盘等）
#: 整词匹配（\b...\b），避免 'truncated' 之类普通标识符误伤。
FORBIDDEN_KEYWORDS = (
    # 写数据
    "INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "UPSERT",
    # DDL
    "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME",
    # 权限 / 事务 / 会话控制
    "GRANT", "REVOKE", "COMMIT", "ROLLBACK", "SAVEPOINT",
    "LOCK", "UNLOCK", "CALL", "DO", "HANDLER", "PREPARE", "EXECUTE",
    "DEALLOCATE", "SET", "USE", "RESET", "FLUSH", "KILL", "SHUTDOWN",
    # 落盘 / 文件 / 复制
    "OUTFILE", "DUMPFILE", "LOAD", "INTO", "DUMP", "REPLICATION", "SLAVE",
    "FOR", "SHARE",
)

#: 禁止出现的函数（读文件、探测、延时放大）
FORBIDDEN_FUNCTIONS = (
    "LOAD_FILE", "SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK",
    "MASTER_POS_WAIT", "SOURCE_POS_WAIT",
)

#: 需要整词/整短语匹配的禁用子句（与 FORBIDDEN_KEYWORDS 分开，便于静态扫描豁免）
FORBIDDEN_PHRASES = (
    "INTO OUTFILE", "INTO DUMPFILE", "INTO @",
    "FOR UPDATE", "LOCK IN SHARE MODE",
    "LOAD DATA", "LOAD XML",
    "COMMENT", "BEGIN", "START",
)
_READ_ONLY_CLAUSES = (
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE",
    "BETWEEN", "EXISTS", "CASE", "WHEN", "THEN", "ELSE", "END", "DISTINCT",
    "GROUP", "BY", "ORDER", "HAVING", "LIMIT", "OFFSET", "AS", "ON", "JOIN",
    "INNER", "LEFT", "RIGHT", "OUTER", "CROSS", "UNION", "ALL", "ASC", "DESC",
    "COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "CONCAT", "SUBSTRING",
    "TRIM", "LOWER", "UPPER", "YEAR", "MONTH", "DAY", "NOW", "CURDATE",
    "INTERVAL", "CAST", "CONVERT", "CHAR", "SIGNED", "UNSIGNED", "INTEGER",
    "DECIMAL", "FLOAT", "DOUBLE", "VARCHAR", "TEXT", "DATE", "DATETIME",
    "WITH", "RECURSIVE", "VALUES", "TABLE", "OVER", "PARTITION", "ROW",
    "RANK", "DENSE_RANK", "ROW_NUMBER", "UNBOUNDED", "PRECEDING", "FOLLOWING",
    "CURRENT", "ROW", "TRUE", "FALSE", "UNKNOWN", "JSON", "EXTRACT",
)

#: 注释语法
_LINE_COMMENT = re.compile(r"(?m)(--|#)[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)

#: 表名提取（FROM / JOIN / STRAIGHT_JOIN 之后的标识符，支持 `db`.`table` 与反引号）。
#: straight_join 必须显式列出：`\bjoin\b` 因前导下划线不匹配 `straight_join`。
_TABLE_REF = re.compile(
    r"\b(?:from|straight_join|join)\s+(`?[A-Za-z_][\w$]*`?)(?:\s*\.\s*(`?[A-Za-z_][\w$]*`?))?",
    re.I,
)

#: FROM 列表中的别名/终止符：逗号续扫时，别名位置遇到这些关键字即停止
#: （防把 `WHERE a, b` / `GROUP BY a, b` / `ORDER BY a, b` 中的列名当成表）。
_FROM_LIST_TERMINATORS = frozenset({
    "on", "using", "where", "group", "order", "having", "limit", "offset",
    "join", "straight_join", "inner", "left", "right", "cross", "natural",
    "union", "except", "intersect", "into", "for", "lock", "procedure",
    "as",  # AS 后必跟别名而非表；裸 AS 不续扫
})

#: 标识符（用于关键字扫描）
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

#: LIMIT 上限（硬顶，防止一次拉爆生产库）
DEFAULT_MAX_LIMIT = 500
HARD_MAX_LIMIT = 2000


class ReadOnlyViolation(ValueError):
    """SQL 未通过只读护栏（禁止触达数据库连接）。"""


@dataclass
class GuardDecision:
    """护栏裁决结果（可审计）。"""

    allowed: bool
    sql: str = ""
    reason: str = ""
    tables: list[str] = field(default_factory=list)
    rewritten_sql: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def _strip_comments(sql: str) -> str:
    """剥离注释（防注释绕过：SE/**/LECT、SELECT 1 -- \\n DROP TABLE x）。

    引号感知：字符串字面量内的 `--`/`#`/`/* */` 不视为注释（避免破坏含特殊字符的查询）。
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        # 行注释
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "#":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        # 块注释
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip())


def _unquote(name: str) -> str:
    return (name or "").strip().strip("`").strip()


#: CTE 名称提取（WITH name AS ( ... )）——CTE 名不是物理表，不应参与表白名单校验
_CTE_NAME = re.compile(
    r"\b(?:with\s+(?:recursive\s+)?|,)\s*(`?[A-Za-z_][\w$]*`?)\s+as\s*\(", re.I
)


def _extract_cte_names(sql: str) -> set[str]:
    """精确提取 WITH 子句中声明的 CTE 名称（小写集合）。

    精确实现（括号配对扫描）：只有真正出现在 `WITH name AS ( ... )` 头部的标识符
    才是 CTE 名。这样既避免把真实表名误判为 CTE（放宽白名单），又不会误伤合法 CTE。
    安全性：即便某名被识别为 CTE，其 CTE 体内的真实表仍逐个走表白名单校验。
    """
    names: set[str] = set()
    m = re.match(r"\s*WITH\s+(?:RECURSIVE\s+)?", sql, re.I)
    if not m:
        return names
    i = m.end()
    n = len(sql)
    while i < n:
        mm = re.match(r"\s*(`?[A-Za-z_][\w$]*`?)\s+AS\s*\(", sql[i:], re.I)
        if not mm:
            break
        name = _unquote(mm.group(1)).lower()
        j = i + mm.end()      # 指向 '(' 之后
        depth = 1
        while j < n and depth > 0:
            ch = sql[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            j += 1
        names.add(name)
        i = j
        m2 = re.match(r"\s*,", sql[i:])
        if not m2:
            break
        i += m2.end()
    return names


def _extract_tables(sql: str) -> list[str]:
    """提取 SQL 引用到的表（含库名前缀），去重保序。

    覆盖三类引用形态：
    1. `FROM` / `JOIN` / `STRAIGHT_JOIN` 之后的表（主正则 `_TABLE_REF`）；
    2. 逗号隐式连接：`FROM a, b` / `FROM a AS x, b y`（从每个匹配处向后续扫，
       别名位置遇 `_FROM_LIST_TERMINATORS` 关键字即停，避免把 WHERE/GROUP BY/
       ORDER BY 后的列名误当表）；
    3. 库名限定 `db.table` 与反引号标识符。
    """
    out: list[str] = []

    def _add(first: str, second: str) -> None:
        full = f"{first}.{second}" if second else first
        if full and full not in out:
            out.append(full)

    for m in _TABLE_REF.finditer(sql):
        _add(_unquote(m.group(1)), _unquote(m.group(2) or ""))
        # 逗号连接续扫：<table> [AS] alias , <table> ...
        pos = m.end()
        n = len(sql)
        while pos < n:
            cm = re.match(r"\s*,", sql[pos:])
            if not cm:
                # 无立即逗号：尝试跳过别名（AS alias / alias）后再找逗号
                am = re.match(r"\s*(?:as\s+)?(`?[A-Za-z_][\w$]*`?)", sql[pos:], re.I)
                if not am or _unquote(am.group(1)).lower() in _FROM_LIST_TERMINATORS:
                    break
                pos += am.end()
                cm = re.match(r"\s*,", sql[pos:])
                if not cm:
                    break
            pos += cm.end()
            tm = re.match(
                r"\s*(`?[A-Za-z_][\w$]*`?)(?:\s*\.\s*(`?[A-Za-z_][\w$]*`?))?",
                sql[pos:], re.I,
            )
            if not tm:
                break
            _add(_unquote(tm.group(1)), _unquote(tm.group(2) or ""))
            pos += tm.end()
    return out


def _mask_literals(sql: str) -> str:
    """把字符串字面量与反引号标识符内容替换为空格（保留长度）。

    关键字/函数扫描必须在「代码视图」上进行，否则：
    - 误报：影片名含 'update'/'comment' 等字样的**字面量**会被当成危险关键字；
    - 漏报：把关键字藏在字面量里再配合拼接（虽不多见）也不应被当作安全。
    表名提取仍在**原文**上做（需要真实标识符）。
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"', "`"):
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == "\\" and quote != "`":
                    j += 2
                    continue
                if sql[j] == quote:
                    break
                j += 1
            # 保留引号本身，内容替换为空格
            out.append(ch)
            out.append(" " * max(0, min(j, n - 1) - i - 1))
            out.append(quote if j < n else "")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_statements(sql: str) -> list[str]:
    """按顶层分号切分语句。

    引号感知：字符串字面量（'...' / "..."）与反引号标识符内的分号不算语句分隔符，
    避免 `WHERE title LIKE '%;%'` 被误判为多语句（fail-closed 误伤）。
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:      # 转义字符
                buf.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:
                # 双引号标识符外的 ' 表示字符串结束
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def _has_forbidden_keyword(sql: str) -> str | None:
    """扫描禁用关键字（整词匹配，避免 'truncated' 之类误伤）。"""
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", sql, re.I):
            return kw
    for phrase in FORBIDDEN_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", sql, re.I):
            return phrase
    return None


def _has_forbidden_function(sql: str) -> str | None:
    for fn in FORBIDDEN_FUNCTIONS:
        if re.search(rf"\b{re.escape(fn)}\s*\(", sql, re.I):
            return fn
    return None


def _is_select_only(sql: str) -> bool:
    """确认语句确为只读查询。

    - 必须以 SELECT 或 WITH 起始；
    - WITH 子句内的最终语句也必须是 SELECT（CTE 不能包裹写操作）。
    """
    s = sql.strip()
    if not s:
        return False
    if s.upper().startswith("WITH"):
        # WITH ... SELECT：取最后一个顶层关键字必须是 SELECT
        # 简单可靠做法：CTE 之后不允许出现 INSERT/UPDATE/DELETE（已被黑名单覆盖），
        # 且必须包含 SELECT
        return re.search(r"\bselect\b", s, re.I) is not None
    return s.upper().startswith("SELECT")


def _clamp_limit(sql: str, max_limit: int) -> str:
    """强制 LIMIT：不存在则追加；存在则钳制到上限。

    安全要求：LIMIT 必须是**数字字面量**（禁止 `LIMIT %s` / `LIMIT ?`）——
    否则护栏无法验证/钳制实际行数上限，会给「全表拉取」留下缺口。
    调用方必须在应用层先校验并把 limit 拼成整数。
    """
    max_limit = max(1, min(int(max_limit), HARD_MAX_LIMIT))
    if re.search(r"\blimit\s+(%s|\?|:)", sql, re.I):
        raise ReadOnlyViolation(
            "LIMIT 必须为数字字面量（禁止参数化 LIMIT），以便护栏强制结果集上限")
    # LIMIT offset, count 形式：钳制 count
    m2 = re.search(r"\blimit\s+(\d+)\s*,\s*(\d+)", sql, re.I)
    if m2:
        if int(m2.group(2)) > max_limit:
            return sql[:m2.start(2)] + str(max_limit) + sql[m2.end(2):]
        return sql
    m = re.search(r"\blimit\s+(\d+)", sql, re.I)
    if m:
        cur = int(m.group(1))
        if cur > max_limit:
            return sql[:m.start(1)] + str(max_limit) + sql[m.end(1):]
        return sql
    return f"{sql} LIMIT {max_limit}"


def _check_metadata_sql(sql: str, allowed_schemas: tuple = ALLOWED_SCHEMAS) -> list[str]:
    """校验元数据只读通道的 SQL：仅允许 information_schema 的只读元数据对象，
    且必须带 table_schema 过滤（防全实例元数据扫描）；过滤**字面量值**必须在
    白名单库内（参数化占位符无法静态校验，由内部调用方保证绑定白名单值）。
    返回引用的表名。
    """
    tables = _extract_tables(sql)
    for t in tables:
        parts = t.split(".")
        schema = parts[0].lower()
        obj = parts[-1].lower() if len(parts) > 1 else ""
        if schema != "information_schema":
            raise ReadOnlyViolation(
                f"元数据通道仅允许 information_schema，收到：{t}")
        if obj not in METADATA_ALLOWED_OBJECTS:
            raise ReadOnlyViolation(
                f"元数据通道仅允许只读元数据对象 {METADATA_ALLOWED_OBJECTS}，收到：{t}")
    if not _METADATA_SCHEMA_FILTER.search(sql):
        raise ReadOnlyViolation(
            "元数据通道必须带 table_schema 过滤条件（防全实例元数据扫描）")
    # 过滤值校验：字面量库名必须在白名单内（防 table_schema='mysql' 探测系统库）
    allowed = tuple(s.lower() for s in (allowed_schemas or ALLOWED_SCHEMAS))
    values: list[str] = []
    for m in _SCHEMA_VALUE_EQ.finditer(sql):
        values.append(m.group(1))
    m_in = _SCHEMA_VALUE_IN.search(sql)
    if m_in:
        values.extend(_SCHEMA_VALUE_IN_ITEM.findall(m_in.group(1)))
    for v in values:
        if v.strip().lower() not in allowed:
            raise ReadOnlyViolation(
                f"元数据过滤值仅允许库 {allowed}，收到：{v}")
    return tables


def check_sql(sql: str, max_limit: int = DEFAULT_MAX_LIMIT,
              allow_metadata: bool = False,
              allowed_schemas: Optional[tuple] = None) -> GuardDecision:
    """只读护栏主入口：对 SQL 做完整裁决。

    - `allow_metadata=False`（默认）：完全禁止 information_schema。
    - `allow_metadata=True`：放行**受控**元数据读取（仅 information_schema 的只读
      元数据对象 + 必须带 table_schema 过滤）。仅供内部 schema 资源/表规模统计使用，
      外部输入不得走此路径。
    - `allowed_schemas`：允许显式限定的库名（默认 imdb/imdb2）；连接层传入实际配置值。

    通过 → 返回 allowed=True（rewritten_sql 为钳制 LIMIT 后的语句）；
    拒绝 → 抛 ReadOnlyViolation（**绝不返回可执行 SQL**）。
    """
    if not sql or not str(sql).strip():
        raise ReadOnlyViolation("SQL 为空，拒绝执行")

    raw = str(sql)
    normalized = _normalize(raw)

    # 1) 剥注释后再校验（防注释绕过）
    stripped = _normalize(_strip_comments(raw))

    # 2) 多语句拒绝（`SELECT 1; DROP TABLE x`）
    statements = _split_statements(stripped)
    if len(statements) > 1:
        raise ReadOnlyViolation(
            f"拒绝多语句执行（检测到 {len(statements)} 条语句）；只允许单条只读查询")

    # 3) 语句类型白名单
    if not _is_select_only(stripped):
        raise ReadOnlyViolation(
            "拒绝非只读语句：仅允许 SELECT / WITH ... SELECT")

    # 4) 禁用关键字（在「代码视图」上扫描，忽略字面量内容）
    masked = _mask_literals(stripped)
    bad = _has_forbidden_keyword(masked)
    if bad:
        raise ReadOnlyViolation(f"拒绝包含禁用关键字的语句：{bad}")

    # 5) 禁用函数
    bad_fn = _has_forbidden_function(masked)
    if bad_fn:
        raise ReadOnlyViolation(f"拒绝包含禁用函数的语句：{bad_fn}")

    # 5.5) 兜底扫描：任何「库名.对象」限定引用中的系统库一律拒绝。
    #      不依赖 _TABLE_REF 提取路径——即便逗号连接/STRAIGHT_JOIN 等形态漏扫，
    #      对 mysql/performance_schema/sys 的限定引用也在此拦截（纵深防御）。
    #      在掩码视图上扫描：字符串字面量内容不算表引用（防影片名误伤）；
    #      反引号限定引用（`mysql`.`user`）由 _TABLE_REF（支持反引号）覆盖。
    for m in _ANY_SCHEMA_QUALIFIED.finditer(masked):
        sch = _unquote(m.group(1)).lower()
        if sch in _ALWAYS_FORBIDDEN_SCHEMAS:
            raise ReadOnlyViolation(f"拒绝访问受限模式：{sch}")
        if sch == "information_schema" and not allow_metadata:
            raise ReadOnlyViolation("拒绝访问受限模式：information_schema")

    # 6) 表白名单（CTE 名不是物理表，跳过）
    schemas = tuple(s.lower() for s in (allowed_schemas or ALLOWED_SCHEMAS))
    cte_names = _extract_cte_names(stripped)
    tables = _extract_tables(stripped)
    is_meta = any(t.split(".")[0].lower() == "information_schema" for t in tables)
    if is_meta:
        if not allow_metadata:
            raise ReadOnlyViolation("拒绝访问受限模式：information_schema")
        tables = _check_metadata_sql(stripped, schemas)
    else:
        for t in tables:
            parts = t.split(".")
            schema = parts[0].lower() if len(parts) > 1 else ""
            name = parts[-1]
            if schema and schema in FORBIDDEN_SCHEMAS:
                raise ReadOnlyViolation(f"拒绝访问受限模式：{schema}")
            if schema and schema not in schemas:
                raise ReadOnlyViolation(
                    f"拒绝访问库 {schema}：仅允许 {schemas}")
            if not schema and name.lower() in cte_names:
                continue  # CTE 别名，合法
            if name in ALLOWED_REGISTERED_TABLES:
                continue  # schema_map 已登记的合法对象（如跨源比对视图）
            if not any(name.lower().startswith(p) for p in ALLOWED_TABLE_PREFIXES):
                raise ReadOnlyViolation(
                    f"拒绝访问表 {t}：仅允许前缀 {ALLOWED_TABLE_PREFIXES} "
                    f"或 schema_map 已登记对象")

    # 7) 强制 LIMIT
    rewritten = _clamp_limit(stripped, max_limit)

    return GuardDecision(
        allowed=True,
        sql=raw,
        reason="ok",
        tables=tables,
        rewritten_sql=rewritten,
    )


def assert_readonly(sql: str, max_limit: int = DEFAULT_MAX_LIMIT,
                    allow_metadata: bool = False,
                    allowed_schemas: Optional[tuple] = None) -> str:
    """便捷函数：校验通过返回可执行 SQL（已钳制 LIMIT），否则抛 ReadOnlyViolation。"""
    return check_sql(sql, max_limit=max_limit, allow_metadata=allow_metadata,
                     allowed_schemas=allowed_schemas).rewritten_sql


def is_readonly(sql: str) -> bool:
    """布尔判定（不抛异常），供静态检查/测试使用。"""
    try:
        check_sql(sql)
        return True
    except ReadOnlyViolation:
        return False
