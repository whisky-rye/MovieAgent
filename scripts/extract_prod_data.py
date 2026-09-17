#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生产库只读全链路数据提取 + 验证脚本（IMDb / Douban）。

⚠️ 红线约束：本脚本对生产库 **仅执行 SELECT**，严禁任何写操作（UPDATE/DELETE/INSERT/DDL）。
   连接使用独立的 PROD_* 环境变量，默认指向只读生产实例，绝不连本地语料库。

用法：
    python scripts/extract_prod_data.py                 # 提取 2000 条全链路并落盘 fixtures
    python scripts/extract_prod_data.py --limit 500     # 自定义条数
    python scripts/extract_prod_data.py --verify        # 提取后追加「实时外键完整性」校验
    EXTRACT_LIMIT=1000 python scripts/extract_prod_data.py

产物（默认写入 data/，已被 .gitignore 忽略，属大型生成物不入库）：
    data/prod_imdb_fullchain.json      # imdb 库 2000 条全链路
    data/prod_douban_fullchain.json    # imdb2(douban) 库 2000 条全链路
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager

import pymysql

# ---------------------------------------------------------------------------
# 连接配置（PROD 只读）。默认值即用户提供的生产实例凭据；生产环境应通过环境变量注入，
# 且强烈建议改用「仅 SELECT」权限的专用只读账号，避免 root 直连。
# ---------------------------------------------------------------------------
HOST = os.getenv("PROD_DB_HOST", "<PROD_DB_HOST>")
PORT = int(os.getenv("PROD_DB_PORT", "3306"))
USER = os.getenv("PROD_DB_USER", "root")
PASS = os.getenv("PROD_DB_PASS", "!@$G3ad4gjD%3u5$^@75io7%$&67SJ")

IMDB_DB = os.getenv("PROD_IMDB_DB", "imdb")    # 仅读 imdb_* 表
DOUBAN_DB = os.getenv("PROD_DOUBAN_DB", "imdb2")  # 仅读 douban_* 表

DATA_DIR = os.path.abspath(
    os.getenv("EXTRACT_OUT", os.path.join(os.path.dirname(__file__), "..", "data"))
)


def _connect(db: str):
    """建立只读连接。仅用于 SELECT，不做任何写操作。"""
    return pymysql.connect(
        host=HOST, port=PORT, user=USER, password=PASS, database=db,
        charset="utf8mb4", connect_timeout=15, read_timeout=180, write_timeout=180,
        cursorclass=pymysql.cursors.DictCursor,
    )


@contextmanager
def ro_conn(db: str):
    conn = _connect(db)
    try:
        yield conn
    finally:
        conn.close()


def parse_pipe(value) -> list[str]:
    """解析 '|A|B|' 形式的竖线分隔字段。"""
    if not value:
        return []
    return [p for p in str(value).split("|") if p.strip()]


def fetch_related(conn, table: str, id_col: str, ids: list) -> dict:
    """按主键列表批量拉取关联表全部行，返回 {id: [row, ...]}。"""
    if not ids:
        return {}
    sql = f"SELECT * FROM `{table}` WHERE `{id_col}` IN %(ids)s"
    with conn.cursor() as cur:
        cur.execute(sql, {"ids": tuple(ids)})
        rows = cur.fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r[id_col], []).append(r)
    return out


def extract_imdb(conn, limit: int) -> list:
    """IMDb 全链路：imdb_main -> name_relate/company_relate/text/prepare + 维度解析。

    说明：imdb 库缺少独立的国家/类别维度表，故 country/genres/language 直接从
    imdb_main 的竖线分隔字段解析（用户已确认此兜底方式）。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM imdb_main ORDER BY id LIMIT %s", (limit,))
        mains = cur.fetchall()
    ids = [m["imdb_id"] for m in mains]

    names = fetch_related(conn, "imdb_name_relate", "imdb_id", ids)
    companies = fetch_related(conn, "imdb_company_relate", "imdb_id", ids)
    texts = fetch_related(conn, "imdb_text", "imdb_id", ids)
    prepares = fetch_related(conn, "imdb_prepare", "imdb_id", ids)

    # 解析 name_id -> imdb_name（维度表）
    name_ids = {r["name_id"] for rows in names.values() for r in rows}
    name_map: dict = {}
    if name_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name_id, name, primary_image FROM imdb_name WHERE name_id IN %(ids)s",
                {"ids": tuple(name_ids)},
            )
            for r in cur.fetchall():
                name_map[r["name_id"]] = r

    chains = []
    for m in mains:
        iid = m["imdb_id"]
        nm = names.get(iid, [])
        chains.append({
            "main": m,
            "names": nm,
            "companies": companies.get(iid, []),
            "text": (texts.get(iid) or [None])[0],
            "prepare": (prepares.get(iid) or [None])[0],
            "parsed": {
                "genres": parse_pipe(m.get("genres")),
                "country": parse_pipe(m.get("country")),
                "language": parse_pipe(m.get("language")),
            },
            "resolved": {n["name_id"]: name_map.get(n["name_id"]) for n in nm},
        })
    return chains


def extract_douban(conn, limit: int) -> list:
    """Douban 全链路：douban_main -> genre_relate/country_relate/name_relate/text/prepare。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM douban_main ORDER BY id LIMIT %s", (limit,))
        mains = cur.fetchall()
    ids = [m["douban_id"] for m in mains]

    genres = fetch_related(conn, "douban_genre_relate", "douban_id", ids)
    countries = fetch_related(conn, "douban_country_relate", "douban_id", ids)
    names = fetch_related(conn, "douban_name_relate", "douban_id", ids)
    texts = fetch_related(conn, "douban_text", "douban_id", ids)
    prepares = fetch_related(conn, "douban_prepare", "douban_id", ids)

    gid = {r["genre_id"] for rows in genres.values() for r in rows}
    cid = {r["country_id"] for rows in countries.values() for r in rows}
    nid = {r["name_id"] for rows in names.values() for r in rows}

    genre_map, country_map, name_map = {}, {}, {}
    if gid:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, genre_name, genre_cn_name FROM douban_genre WHERE id IN %(ids)s",
                {"ids": tuple(gid)},
            )
            for r in cur.fetchall():
                genre_map[r["id"]] = r
    if cid:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, country_name, country_short_name, country_cn_name "
                "FROM douban_country WHERE id IN %(ids)s",
                {"ids": tuple(cid)},
            )
            for r in cur.fetchall():
                country_map[r["id"]] = r
    if nid:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name_id, name, name_cn, name_en, primary_image "
                "FROM douban_name WHERE name_id IN %(ids)s",
                {"ids": tuple(nid)},
            )
            for r in cur.fetchall():
                name_map[r["name_id"]] = r

    chains = []
    for m in mains:
        did = m["douban_id"]
        g = genres.get(did, [])
        c = countries.get(did, [])
        n = names.get(did, [])
        chains.append({
            "main": m,
            "genres": g,
            "countries": c,
            "names": n,
            "text": (texts.get(did) or [None])[0],
            "prepare": (prepares.get(did) or [None])[0],
            "resolved": {
                "genres": {x["genre_id"]: genre_map.get(x["genre_id"]) for x in g},
                "countries": {x["country_id"]: country_map.get(x["country_id"]) for x in c},
                "names": {x["name_id"]: name_map.get(x["name_id"]) for x in n},
            },
        })
    return chains


def verify_chains_imdb(chains: list) -> dict:
    orphan_names = sum(1 for ch in chains for nid, v in ch["resolved"].items() if v is None)
    total_names = sum(len(ch["names"]) for ch in chains)
    missing_text = sum(1 for ch in chains if ch["text"] is None)
    missing_prepare = sum(1 for ch in chains if ch["prepare"] is None)
    no_parsed_country = sum(1 for ch in chains if not ch["parsed"]["country"])
    no_parsed_genre = sum(1 for ch in chains if not ch["parsed"]["genres"])
    return {
        "records": len(chains),
        "total_name_relate": total_names,
        "orphan_name_dim": orphan_names,
        "missing_text": missing_text,
        "missing_prepare": missing_prepare,
        "no_parsed_country": no_parsed_country,
        "no_parsed_genre": no_parsed_genre,
    }


def verify_chains_douban(chains: list) -> dict:
    orphan_g = sum(1 for ch in chains for k, v in ch["resolved"]["genres"].items() if v is None)
    orphan_c = sum(1 for ch in chains for k, v in ch["resolved"]["countries"].items() if v is None)
    orphan_n = sum(1 for ch in chains for k, v in ch["resolved"]["names"].items() if v is None)
    total_g = sum(len(ch["genres"]) for ch in chains)
    total_c = sum(len(ch["countries"]) for ch in chains)
    total_n = sum(len(ch["names"]) for ch in chains)
    missing_text = sum(1 for ch in chains if ch["text"] is None)
    return {
        "records": len(chains),
        "total_genre_relate": total_g,
        "total_country_relate": total_c,
        "total_name_relate": total_n,
        "orphan_genre_dim": orphan_g,
        "orphan_country_dim": orphan_c,
        "orphan_name_dim": orphan_n,
        "missing_text": missing_text,
    }


def verify_live(conn, db_kind: str, chains: list, limit: int) -> dict:
    """实时外键完整性校验：以 SQL LEFT JOIN 直接核对孤儿记录（权威校验）。"""
    if db_kind == "imdb":
        id_col, rel, dim, dim_pk = "imdb_id", "imdb_name_relate", "imdb_name", "name_id"
    else:
        id_col, rel, dim, dim_pk = "douban_id", "douban_genre_relate", "douban_genre", "id"
    ids = [(ch["main"]["imdb_id"] if db_kind == "imdb" else ch["main"]["douban_id"]) for ch in chains]
    sql = (f"SELECT COUNT(*) AS c FROM `{rel}` r LEFT JOIN `{dim}` d "
           f"ON r.{('genre_id' if db_kind=='douban' else 'name_id')} = d.{dim_pk} "
           f"WHERE d.{dim_pk} IS NULL AND r.{id_col} IN %(ids)s")
    with conn.cursor() as cur:
        cur.execute(sql, {"ids": tuple(ids)})
        orphans = list(cur.fetchone().values())[0]
    return {"live_orphans": orphans}


def main():
    limit = int(os.getenv("EXTRACT_LIMIT", os.getenv("limit", "2000")))
    do_verify = "--verify" in sys.argv
    # 支持 --limit N
    for a in sys.argv[1:]:
        if a.startswith("--limit"):
            limit = int(a.split("=", 1)[1]) if "=" in a else None
    if limit is None and "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        limit = int(sys.argv[i + 1])

    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"[extract] limit={limit} target={DATA_DIR}")

    with ro_conn(IMDB_DB) as c:
        imdb_chains = extract_imdb(c, limit)
    with ro_conn(DOUBAN_DB) as c:
        douban_chains = extract_douban(c, limit)

    imdb_path = os.path.join(DATA_DIR, "prod_imdb_fullchain.json")
    douban_path = os.path.join(DATA_DIR, "prod_douban_fullchain.json")
    with open(imdb_path, "w", encoding="utf-8") as f:
        json.dump({"source": "imdb", "limit": limit, "chains": imdb_chains}, f, ensure_ascii=False)
    with open(douban_path, "w", encoding="utf-8") as f:
        json.dump({"source": "imdb2(douban)", "limit": limit, "chains": douban_chains}, f, ensure_ascii=False)

    vi = verify_chains_imdb(imdb_chains)
    vd = verify_chains_douban(douban_chains)
    print(f"[extract] IMDb  -> {imdb_path}")
    print(f"          {json.dumps(vi, ensure_ascii=False)}")
    print(f"[extract] Douban-> {douban_path}")
    print(f"          {json.dumps(vd, ensure_ascii=False)}")

    if do_verify:
        with ro_conn(IMDB_DB) as c:
            liv = verify_live(c, "imdb", imdb_chains, limit)
        with ro_conn(DOUBAN_DB) as c:
            ldv = verify_live(c, "douban", douban_chains, limit)
        print(f"[verify] IMDb  live orphan name_relate: {liv['live_orphans']}")
        print(f"[verify] Douban live orphan genre_relate: {ldv['live_orphans']}")
        # 实时校验零孤儿方为通过
        ok = liv["live_orphans"] == 0 and ldv["live_orphans"] == 0
        print(f"[verify] RESULT: {'PASS' if ok else 'FAIL'}")

    print("[done] 仅执行 SELECT，未对生产库做任何写操作。")


if __name__ == "__main__":
    main()
