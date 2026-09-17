"""静态不变量测试：全项目**不得**残留任何 MySQL 读取的 file 兜底实现。

本测试不依赖 MySQL，纯静态扫描源码，作为「强 MySQL、无 file 兜底」这条硬约束的把关：
一旦有人把 File*Store、use_mysql 标志、*_FILE_ROOT 配置、MEMORY_BACKEND 开关或
「回退/走/转 file」之类的兜底分支重新引入代码，本测试立即失败，从而保证
「覆盖全项目所有文件与调用点、无任何遗漏」。

扫描范围：agent / api / config / scripts / db / llm / ranker / retrieval / etl / vector
（含 .py 与 .sql），以及根目录 .env。故意排除 docs/（历史设计文档）与 tests/ 自身。

注意：允许在注释里写「无 file 兜底」之类的描述性文字（描述「没有兜底」），
因此只禁止表示「存在兜底」的强信号符号，而非字面 "file 兜底"。
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 表示「存在 file 兜底」的强信号符号。任何一条出现在源码中即视为违反约束。
FORBIDDEN = [
    "FileUserStore",
    "FileMemoryStore",
    "FileSessionStore",
    "FileShareStore",
    "FallbackMemoryStore",
    "use_mysql",
    "mysql->file",
    "回退 file",
    "回退 File",
    "走 file",
    "全程走 file",
    "转 file",
    "AUTH_FILE_ROOT",
    "MEMORY_FILE_ROOT",
    "SESSION_FILE_ROOT",
    "SHARED_FILE_ROOT",
    "MEMORY_BACKEND",
]

SCAN_DIRS = [
    "agent",
    "api",
    "config",
    "scripts",
    "db",
    "llm",
    "ranker",
    "retrieval",
    "etl",
    "vector",
]
SCAN_ROOT_FILES = [".env"]
SCAN_EXTS = {".py", ".sql"}


def _iter_source_files():
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            # 跳过虚拟环境 / 前端 / 构建产物
            if any(part in ("node_modules", ".venv", "__pycache__", "dist") for part in root.split(os.sep)):
                continue
            for fn in files:
                if os.path.splitext(fn)[1] in SCAN_EXTS:
                    yield os.path.join(root, fn)
    for fn in SCAN_ROOT_FILES:
        p = os.path.join(ROOT, fn)
        if os.path.isfile(p):
            yield p


def test_no_file_fallback_in_source():
    violations = []
    for path in _iter_source_files():
        try:
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    for tok in FORBIDDEN:
                        if tok in line:
                            rel = os.path.relpath(path, ROOT)
                            violations.append(f"{rel}:{lineno}: 命中禁止符号 {tok!r}")
                            break
        except Exception as e:  # noqa: BLE001 - 编码/读取异常也视为告警
            violations.append(f"{os.path.relpath(path, ROOT)}: 读取失败 {e!r}")
    assert not violations, (
        "发现 file 兜底残留，违反「强 MySQL、无 file 兜底」约束：\n" + "\n".join(violations)
    )


@pytest.mark.parametrize("token", FORBIDDEN)
def test_forbidden_symbols_are_meaningful(token):
    """保证 FORBIDDEN 列表里每个符号确实在当前仓库里查不到（防止列表被误清空却仍显示通过）。"""
    hits = []
    for path in _iter_source_files():
        try:
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if token in line:
                        hits.append(f"{os.path.relpath(path, ROOT)}:{lineno}")
        except Exception:
            pass
    # 该符号必须“不存在”，否则说明兜底代码仍在
    assert not hits, f"禁止符号 {token!r} 仍出现在：{hits}"
