"""🔴 红线静态守护：整个 mcp_prod 包**不得出现任何写操作**。

思路（比字符串 grep 更严格）：用 AST 解析每个模块，找出所有字符串常量，
凡是以写操作关键字（INSERT/UPDATE/DELETE/DROP/…）开头的一律视为违规；
guard.py 中作为**黑名单常量**存在的关键字集合予以豁免（它们是"要拦截的对象"，
不是"要执行的语句"）。

同时守护：
- 连接层只能通过 `assert_readonly` 取得可执行 SQL（不得绕过护栏直接 execute）；
- 包内不得出现 `pymysql.connect` 之外的其它驱动直连（如 sqlalchemy.create_engine）；
- 不得出现写库 API（to_csv/to_sql/INSERT INTO 等）。
"""
import ast
import re
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PKG_DIR = Path(__file__).resolve().parents[1] / "mcp_prod"

#: 写操作关键字（字符串字面量以此开头即视为违规）
WRITE_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "RENAME", "GRANT", "REVOKE", "COMMIT", "ROLLBACK",
    "LOAD DATA", "INTO OUTFILE", "INTO DUMPFILE", "CALL ", "DO ",
)

#: guard.py 中合法的「黑名单常量」变量名（这些字符串是拦截对象，不是执行语句）
GUARD_EXEMPT_NAMES = ("FORBIDDEN_KEYWORDS", "FORBIDDEN_FUNCTIONS", "FORBIDDEN_PHRASES",
                       "ALLOWED_START_KEYWORDS", "FORBIDDEN_SCHEMAS",
                       "METADATA_ALLOWED_OBJECTS", "ALLOWED_TABLE_PREFIXES",
                       "ALLOWED_REGISTERED_TABLES", "ALLOWED_SCHEMAS")


def _py_files():
    return sorted(PKG_DIR.glob("*.py"))


def _guard_constant_strings(tree: ast.AST) -> set[str]:
    """收集 guard.py 中黑名单常量里的字符串（豁免集合）。"""
    exempt: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                name = getattr(tgt, "id", None)
                if name in GUARD_EXEMPT_NAMES and isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
                # 收集元组内字符串
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            exempt.add(elt.value)
    return exempt


def _looks_like_write(sql: str) -> bool:
    """整词/整短语匹配（与 guard 的判定语义一致，避免 'truncated' 误伤）。"""
    head = sql.strip().upper()
    for kw in WRITE_KEYWORDS:
        if re.match(rf"^{re.escape(kw)}\b", head):
            return True
    return False


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.name)
def test_no_write_statement_literals(path):
    """任何以写操作关键字开头的 SQL 字面量都是红线违规。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    exempt = _guard_constant_strings(tree) if path.name == "guard.py" else set()
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value.strip()
            if s in exempt:
                continue
            if _looks_like_write(s):
                offenders.append((node.lineno, s[:80]))
    assert not offenders, f"{path.name} 含写操作 SQL 字面量: {offenders}"


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.name)
def test_no_write_keyword_in_fstring_sql(path):
    """f-string 拼出的 SQL 不得以写操作开头。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            # 取 f-string 的字面量片段拼接后的静态前缀
            prefix = "".join(v.value for v in node.values
                             if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if _looks_like_write(prefix):
                pytest.fail(f"{path.name}:{node.lineno} f-string SQL 疑似写操作: {prefix[:80]}")


def test_connection_layer_always_uses_guard():
    """连接层必须先过护栏再执行（不得直接 execute 原始 SQL）。"""
    src = (PKG_DIR / "connection.py").read_text(encoding="utf-8")
    assert "assert_readonly" in src, "连接层必须调用只读护栏"
    # 唯一可执行 SQL 的来源是 safe_sql（护栏返回），不得出现 execute(sql) 形式
    assert "cur.execute(safe_sql" in src
    assert "cur.execute(sql" not in src, "不得直接执行未过护栏的 SQL"


def test_no_bulk_write_apis():
    """不得出现写库/导出 API。"""
    banned = ("to_sql(", "to_csv(", "to_excel(", "write_csv(", "INSERT INTO ",
              "LOAD DATA INFILE", "SELECT ... INTO OUTFILE")
    for path in _py_files():
        src = path.read_text(encoding="utf-8")
        for b in banned:
            # guard.py 的黑名单常量里含 "LOAD DATA" 等字样（片段匹配），跳过
            if path.name == "guard.py" and b in ("LOAD DATA INFILE",):
                continue
            assert b not in src, f"{path.name} 含疑似写库 API: {b}"


def test_only_pymysql_driver():
    """只允许 pymysql 直连，不引入其它写能力驱动。"""
    for path in _py_files():
        src = path.read_text(encoding="utf-8")
        assert "sqlalchemy" not in src.lower(), f"{path.name} 不应引入 sqlalchemy"
        assert "sqlite3" not in src.lower(), f"{path.name} 不应引入 sqlite3"


def test_guard_module_has_no_db_driver():
    """护栏必须是纯函数模块（零数据库依赖）。"""
    src = (PKG_DIR / "guard.py").read_text(encoding="utf-8")
    for bad in ("import pymysql", "pymysql.connect", "sqlalchemy", "sqlite3"):
        assert bad not in src, f"guard.py 不应依赖 {bad}"


def test_package_files_present():
    """包结构完整性。"""
    for name in ("__init__.py", "guard.py", "connection.py", "schema_map.py",
                 "queries.py", "server.py", "client.py"):
        assert (PKG_DIR / name).exists(), f"缺少模块 {name}"


def test_settings_have_no_default_credentials():
    """settings 中凭据字段必须**无默认值**（禁止把密码写进代码）。

    注意：检查的是「类字段默认值」，不是运行时值（运行时值来自 .env/环境变量，
    那是允许的——.env 已被 .gitignore 忽略）。
    """
    from config.settings import Settings

    for field in ("MCP_DB_PASSWORD", "MCP_DB_USER", "MCP_DB_HOST"):
        default = Settings.model_fields[field].default
        assert default == "", f"{field} 不得有默认值（当前={default!r}）"


def test_extract_script_has_no_hardcoded_password():
    """历史提取脚本中的硬编码密码必须迁出到 env（凭据不落库）。"""
    p = Path(__file__).resolve().parents[1] / "scripts" / "extract_prod_data.py"
    src = p.read_text(encoding="utf-8")
    assert "PROD_DB_PASS" in src
    # 不得出现形如 PASS = "....." 的硬编码字面量
    import re
    m = re.search(r'PASS\s*=\s*os\.getenv\([^)]*,\s*"([^"]+)"\)', src)
    assert m is None or not m.group(1), \
        "extract_prod_data.py 仍有硬编码密码默认值，应仅从环境变量读取"
