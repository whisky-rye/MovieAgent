"""P0.2 数据库接入校验：连通性 + 数据规模 + 双源对齐缺口。

运行：source .venv/Scripts/activate && python scripts/check_db.py
"""
import asyncio
import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine

from config.settings import settings


async def main() -> None:
    dsn_safe = settings.MYSQL_DSN.replace(settings.IMDB_MYSQL_PASS, "***")
    print("[DSN]", dsn_safe)

    engine = create_async_engine(settings.MYSQL_DSN, pool_pre_ping=True)
    async with engine.connect() as conn:
        # 1) 表数量
        tbl_cnt = (
            await conn.execute(
                sqlalchemy.text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema=:db"
                ),
                {"db": settings.IMDB_MYSQL_DB},
            )
        ).scalar()
        print("[tables]", tbl_cnt)

        # 2) 主表行数
        for t in ("douban_main", "imdb_main"):
            try:
                n = (
                    await conn.execute(sqlalchemy.text(f"SELECT COUNT(*) FROM {t}"))
                ).scalar()
                print(f"[{t}]", n)
            except Exception as exc:  # noqa: BLE001
                print(f"[{t}] ERROR", exc)

        # 3) 双源对齐缺口
        try:
            res = await conn.execute(
                sqlalchemy.text("SELECT SUM(imdb_id IS NULL) FROM douban_main")
            )
            print("[douban_main.imdb_id_nulls]", res.scalar())
        except Exception as exc:  # noqa: BLE001
            print("[imdb_id_nulls] ERROR", exc)

        # 4) 类型/文本表抽样行数
        for t in ("douban_text", "imdb_text", "douban_genre_relate", "imdb_name_relate"):
            try:
                n = (
                    await conn.execute(sqlalchemy.text(f"SELECT COUNT(*) FROM {t}"))
                ).scalar()
                print(f"[{t}]", n)
            except Exception as exc:  # noqa: BLE001
                print(f"[{t}] ERROR", exc)

    await engine.dispose()
    print("[OK] 数据库接入校验完成")


if __name__ == "__main__":
    asyncio.run(main())
