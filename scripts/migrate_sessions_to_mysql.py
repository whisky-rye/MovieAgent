"""一次性迁移：把旧 file 兜底时代的存量会话/共享映射导入 movie_agent 库（幂等）。

背景：2026-09-17 起会话与共享存储改为**强 MySQL**（不可用则服务拒绝启动，无 file 兜底），
`data/sessions/*.json` 与 `data/shared/*.json` 不再被读写。本脚本把其中尚不存在于
MySQL 的记录补进来，避免旧会话"消失"。

用法：.venv/Scripts/python.exe scripts/migrate_sessions_to_mysql.py [--purge]
- 默认只导入（session_id 已存在则跳过，不覆盖）；
- --purge：导入完成后把已成功迁移的源 json 文件移入 data/sessions/_migrated/ 归档目录
  （不直接删除，可人工复核后自行清理）。

注意：本脚本是 DDL 之外的显式数据迁移工具，普通业务代码仍然禁止建库建表。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from config.logging_setup import setup_logging
from config.settings import settings

setup_logging()
logger = logging.getLogger("movie_agent.migrate")

ARCHIVE_DIR = os.path.join("data", "sessions", "_migrated")


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning("跳过无法解析的文件 %s：%s", path, e)
        return None


def _to_dt(v: str | None) -> str | None:
    """ISO 8601（含时区）-> MySQL DATETIME 可接受的 'YYYY-MM-DD HH:MM:SS'（UTC 去时区）。"""
    if not v:
        return None
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return None


def migrate_sessions(engine, purge: bool = False) -> tuple[int, int]:
    """导入 data/sessions/*.json -> sessions 表；返回 (导入数, 跳过数)。"""
    root = "data/sessions"  # 配置项已随强 MySQL 改造移除，迁移固定读旧目录
    if not os.path.isdir(root):
        return 0, 0
    imported = skipped = 0
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".json"):
            continue
        rec = _load_json(os.path.join(root, fn))
        if not rec or not rec.get("session_id"):
            continue
        sid = rec["session_id"]
        msgs = json.dumps(rec.get("messages", []), ensure_ascii=False)
        with engine.begin() as c:
            exists = c.execute(
                text("SELECT 1 FROM sessions WHERE session_id=:sid"), {"sid": sid}
            ).first()
            if exists:
                skipped += 1
                continue
            c.execute(
                text(
                    "INSERT INTO sessions (session_id, user_id, title, created_at, updated_at, messages) "
                    "VALUES (:sid, :uid, :title, :ca, :ua, :msgs)"
                ),
                {
                    "sid": sid, "uid": rec.get("user_id", "default"),
                    "title": rec.get("title", "新会话"),
                    "ca": _to_dt(rec.get("created_at")), "ua": _to_dt(rec.get("updated_at")),
                    "msgs": msgs,
                },
            )
        imported += 1
        if purge:
            _archive(fn, root)
    return imported, skipped


def migrate_shared(engine, purge: bool = False) -> tuple[int, int]:
    """导入 data/shared/*.json -> shared_sessions 表；返回 (导入数, 跳过数)。"""
    root = "data/shared"
    if not os.path.isdir(root):
        return 0, 0
    imported = skipped = 0
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".json"):
            continue
        rec = _load_json(os.path.join(root, fn))
        if not rec or not rec.get("session_id") or not rec.get("share_token"):
            continue
        sid = rec["session_id"]
        with engine.begin() as c:
            exists = c.execute(
                text("SELECT 1 FROM shared_sessions WHERE session_id=:sid"), {"sid": sid}
            ).first()
            if exists:
                skipped += 1
                continue
            c.execute(
                text(
                    "INSERT INTO shared_sessions (session_id, owner_user_id, share_token, created_at) "
                    "VALUES (:sid, :uid, :tok, NOW())"
                ),
                {"sid": sid, "uid": rec.get("owner_user_id", "default"),
                 "tok": rec["share_token"]},
            )
        imported += 1
        if purge:
            _archive(fn, root)
    return imported, skipped


def _archive(fn: str, root: str) -> None:
    """把已迁移的源文件挪进归档目录（不删除，可复核）。"""
    if "_migrated" in root:
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    try:
        shutil.move(os.path.join(root, fn), os.path.join(ARCHIVE_DIR, fn))
    except Exception as e:  # noqa: BLE001
        logger.warning("归档 %s 失败（不影响导入结果）：%s", fn, e)


def main() -> int:
    ap = argparse.ArgumentParser(description="存量 file 会话/共享映射 -> movie_agent 库（幂等）")
    ap.add_argument("--purge", action="store_true", help="导入后把源 json 归档到 data/sessions/_migrated/")
    args = ap.parse_args()

    from sqlalchemy import create_engine

    engine = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
    si, ss = migrate_sessions(engine, purge=args.purge)
    hi, hs = migrate_shared(engine, purge=args.purge)
    logger.info("迁移完成：sessions 导入=%d 跳过=%d；shared_sessions 导入=%d 跳过=%d", si, ss, hi, hs)
    print(f"sessions: 导入 {si} 条, 跳过(已存在) {ss} 条")
    print(f"shared_sessions: 导入 {hi} 条, 跳过(已存在) {hs} 条")
    if not args.purge:
        print("（未加 --purge：源 json 保留在原目录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
