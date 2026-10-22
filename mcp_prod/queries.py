"""生产库只读查询层（MCP 工具实现）。

🔴 所有 SQL 均经 `mcp_prod/guard.py` 校验后才触达连接（连接层再叠加会话只读）；
   外部输入**一律走参数化占位符**，不做字符串拼接。

工具（对齐调研报告 §2 的设想）：
- `search_movie`        双源标题模糊检索（中文名/英文名/原名）
- `get_movie_detail`    单片全链路（主表 + 人物 + 文本 + 类型/国家维度）
- `get_movie_relations` 同导演/编剧/卡司的关联作品
- `search_person`       人物维度检索
- `get_person_movies`   人物作品集
- `list_dimensions`     类型/国家维度字典
- `get_table_stats`     表规模概览（运营即席）
- `compare_cross_source` 跨源比对（豆瓣↔IMDb 官方视图口径）
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from mcp_prod import schema_map as sm
from mcp_prod.connection import (
    McpError,
    ProdReadOnlyClient,
    get_prod_client,
)

logger = logging.getLogger("movie_agent.mcp")

#: 职位权重（关系召回用）
ROLE_WEIGHT = {"director": 2.5, "writer": 1.5, "producer": 1.2, "cast": 1.0}


def _client(client: Optional[ProdReadOnlyClient]) -> ProdReadOnlyClient:
    return client or get_prod_client()


def _db(source: str, client: ProdReadOnlyClient) -> str:
    return client.db_for_source(source)


def _norm_source(source: str) -> str:
    s = (source or "").lower().strip()
    if s.startswith("imdb"):
        return sm.SOURCE_IMDB
    if s.startswith("douban"):
        return sm.SOURCE_DOUBAN
    raise McpError(f"未知数据源：{source}（仅支持 imdb / douban）")


# ---------------------------------------------------------------------------
# 1. 标题检索
# ---------------------------------------------------------------------------

def search_movie(
    keyword: str,
    source: str = "douban",
    limit: int = 10,
    client: Optional[ProdReadOnlyClient] = None,
) -> dict:
    """按标题关键词检索影片（中文名/英文名/原名，任一命中即返回）。

    - `keyword` 走参数化 LIKE 绑定，不拼接到 SQL 字符串。
    - 标题匹配用 `LIKE %kw%`，以参数 `%{kw}%` 传入。
    """
    c = _client(client)
    src = _norm_source(source)
    spec = sm.MAIN_TABLES[src]
    kw = (keyword or "").strip()
    if not kw:
        raise McpError("keyword 不能为空")
    limit = max(1, min(int(limit), c.s.MCP_MAX_LIMIT))

    title_exprs = []
    params: list = []
    for i, col in enumerate(spec.title_cols):
        title_exprs.append(f"{col} LIKE %s")
        params.append(f"%{kw}%")
    # 豆瓣：别名表 original_title 亦为标题候选
    if src == sm.SOURCE_DOUBAN and "original_title" not in spec.title_cols:
        title_exprs.append("original_title LIKE %s")
        params.append(f"%{kw}%")

    sql = (
        f"SELECT * FROM {spec.table} WHERE ({' OR '.join(title_exprs)}) "
        f"ORDER BY {spec.votes_col} DESC LIMIT {limit}"
    )
    res = c.query(_db(src, c), sql, tuple(params), max_limit=limit, tool="search_movie")
    items = [sm.normalize_main(r, spec) for r in res.rows]
    return {
        "source": src,
        "keyword": kw,
        "count": len(items),
        "elapsed_ms": res.elapsed_ms,
        "items": items,
    }


# ---------------------------------------------------------------------------
# 2. 单片全链路
# ---------------------------------------------------------------------------

def get_movie_detail(
    source: str,
    movie_id: str,
    include_text: bool = True,
    include_credits: bool = True,
    client: Optional[ProdReadOnlyClient] = None,
) -> dict:
    """取单片全链路：主表 + 人物卡司 + 简介文本 + 类型/国家维度。"""
    c = _client(client)
    src = _norm_source(source)
    spec = sm.MAIN_TABLES[src]
    key = (movie_id or "").strip()
    if not key:
        raise McpError("movie_id 不能为空")
    db = _db(src, c)

    # 主表
    res = c.query(db, f"SELECT * FROM {spec.table} WHERE {spec.key} = %s LIMIT 1",
                  (key,), max_limit=1, tool="get_movie_detail")
    if not res.rows:
        return {"source": src, "source_id": key, "found": False, "movie": None}
    row = res.rows[0]
    movie = sm.normalize_main(row, spec)

    out: dict[str, Any] = {
        "source": src,
        "source_id": key,
        "found": True,
        "movie": movie,
    }

    # 人物卡司
    if include_credits:
        rspec = sm.RELATION_TABLES[src]
        cres = c.query(
            db,
            f"SELECT {rspec.name_id_col}, {rspec.role_col}, "
            + ", ".join(rspec.name_cols)
            + f" FROM {rspec.table} WHERE {rspec.key_col} = %s LIMIT 200",
            (key,), max_limit=200, tool="get_movie_detail.credits")
        credits = []
        for r in cres.rows:
            name = ""
            for col in rspec.name_cols:
                if (r.get(col) or "").strip():
                    name = str(r[col]).strip()
                    break
            if not name:
                name = str(r.get(rspec.name_id_col) or "")
            credits.append({
                "name": name,
                "name_id": str(r.get(rspec.name_id_col) or ""),
                "role": str(r.get(rspec.role_col) or ""),
                "characters": str(r.get("characters") or ""),
            })
        out["credits"] = credits

    # 简介文本
    if include_text:
        tspec = sm.TEXT_TABLES[src]
        body_cols = ", ".join(tspec.body_cols)
        tres = c.query(
            db,
            f"SELECT {body_cols} FROM {tspec.table} WHERE {tspec.key_col} = %s LIMIT 1",
            (key,), max_limit=1, tool="get_movie_detail.text")
        if tres.rows:
            trow = tres.rows[0]
            out["text"] = {
                col: str(trow.get(col) or "")[:2000]
                for col in tspec.body_cols if trow.get(col)
            }

    # 类型 / 国家维度
    genres = _dimension_names(c, db, sm.GENRE_DIMENSIONS[src], key)
    countries = _dimension_names(c, db, sm.COUNTRY_DIMENSIONS[src], key)
    if genres:
        out["genres"] = genres
    if countries:
        out["countries"] = countries
    return out


def _dimension_names(c: ProdReadOnlyClient, db: str, spec: sm.DimensionSpec,
                     key: str) -> list[str]:
    """取影片的类型/国家名（关联表直接带名称，缺失时回退维度表）。"""
    res = c.query(
        db,
        f"SELECT DISTINCT {spec.relate_dim_name_col} AS n FROM {spec.relate_table} "
        f"WHERE {spec.relate_key_col} = %s LIMIT 50",
        (key,), max_limit=50, tool="get_movie_detail.dimension")
    names = [str(r["n"]).strip() for r in res.rows if r.get("n")]
    if names:
        return names
    # 回退：维度字典表按 id 反查
    rel = c.query(
        db,
        f"SELECT DISTINCT {spec.relate_dim_id_col} AS did FROM {spec.relate_table} "
        f"WHERE {spec.relate_key_col} = %s LIMIT 50",
        (key,), max_limit=50, tool="get_movie_detail.dimension_rel")
    ids = [r["did"] for r in rel.rows if r.get("did") is not None]
    if not ids:
        return []
    placeholders = ", ".join(["%s"] * len(ids))
    dim_cols = ", ".join(spec.dim_name_cols)
    dres = c.query(
        db,
        f"SELECT {dim_cols} FROM {spec.dim_table} WHERE {spec.dim_id_col} IN "
        f"({placeholders}) LIMIT 50",
        tuple(ids), max_limit=50, tool="get_movie_detail.dimension_dict")
    out: list[str] = []
    for r in dres.rows:
        for col in spec.dim_name_cols:
            v = (r.get(col) or "").strip()
            if v:
                out.append(v)
                break
    return out


# ---------------------------------------------------------------------------
# 3. 同剧组关联作品
# ---------------------------------------------------------------------------

def get_movie_relations(
    source: str,
    movie_id: str,
    limit: int = 20,
    client: Optional[ProdReadOnlyClient] = None,
) -> dict:
    """取同导演/编剧/卡司的其他作品（关系召回，MCP 补充通道的核心价值）。"""
    c = _client(client)
    src = _norm_source(source)
    rspec = sm.RELATION_TABLES[src]
    mspec = sm.MAIN_TABLES[src]
    key = (movie_id or "").strip()
    if not key:
        raise McpError("movie_id 不能为空")
    limit = max(1, min(int(limit), c.s.MCP_MAX_LIMIT))
    db = _db(src, c)

    # 取该片的人物 name_id
    pres = c.query(
        db,
        f"SELECT DISTINCT {rspec.name_id_col} AS nid FROM {rspec.table} "
        f"WHERE {rspec.key_col} = %s LIMIT 100",
        (key,), max_limit=100, tool="get_movie_relations.people")
    nids = [r["nid"] for r in pres.rows if r.get("nid") is not None]
    if not nids:
        return {"source": src, "source_id": key, "count": 0, "items": []}

    placeholders = ", ".join(["%s"] * len(nids))
    # 共享人物的作品，按共享权重排序
    sql = (
        f"SELECT n.{rspec.key_col} AS k, "
        f"COUNT(DISTINCT n.{rspec.name_id_col}) AS shared, "
        f"SUM(CASE n.{rspec.role_col} WHEN 'director' THEN 2.5 "
        f"WHEN 'writer' THEN 1.5 WHEN 'producer' THEN 1.2 ELSE 1.0 END) AS weight, "
        f"GROUP_CONCAT(DISTINCT n.{rspec.name_id_col}) AS nids "
        f"FROM {rspec.table} n "
        f"WHERE n.{rspec.name_id_col} IN ({placeholders}) AND n.{rspec.key_col} <> %s "
        f"GROUP BY n.{rspec.key_col} ORDER BY weight DESC LIMIT {limit}"
    )
    params = list(nids) + [key]
    res = c.query(db, sql, tuple(params), max_limit=limit, tool="get_movie_relations")

    keys = [r["k"] for r in res.rows if r.get("k")]
    if not keys:
        return {"source": src, "source_id": key, "count": 0, "items": []}

    # 批量回查主表
    kph = ", ".join(["%s"] * len(keys))
    mres = c.query(
        db,
        f"SELECT * FROM {mspec.table} WHERE {mspec.key} IN ({kph}) LIMIT {limit}",
        tuple(keys), max_limit=limit, tool="get_movie_relations.main")
    mmap = {str(r.get(mspec.key)): r for r in mres.rows}

    items = []
    for r in res.rows:
        k = str(r.get("k"))
        m = mmap.get(k)
        if not m:
            continue
        item = sm.normalize_main(m, mspec)
        item["shared_people"] = int(r.get("shared") or 0)
        item["relation_weight"] = float(r.get("weight") or 0.0)
        items.append(item)
    return {"source": src, "source_id": key, "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# 4. 人物检索与作品集
# ---------------------------------------------------------------------------

def search_person(
    name: str,
    source: str = "douban",
    limit: int = 10,
    client: Optional[ProdReadOnlyClient] = None,
) -> dict:
    """按姓名检索人物。"""
    c = _client(client)
    src = _norm_source(source)
    nspec = sm.NAME_TABLES[src]
    nm = (name or "").strip()
    if not nm:
        raise McpError("name 不能为空")
    limit = max(1, min(int(limit), c.s.MCP_MAX_LIMIT))
    exprs, params = [], []
    for col in nspec.name_cols:
        exprs.append(f"{col} LIKE %s")
        params.append(f"%{nm}%")
    sql = (f"SELECT * FROM {nspec.table} WHERE {' OR '.join(exprs)} "
           f"LIMIT {limit}")
    res = c.query(_db(src, c), sql, tuple(params), max_limit=limit, tool="search_person")
    items = []
    for r in res.rows:
        # 优先返回「命中关键词」的那一列（否则中文库会返回英文名）
        name = ""
        for col in nspec.name_cols:
            v = (r.get(col) or "").strip()
            if v and nm in v:
                name = v
                break
        if not name:
            name = sm.normalize_person(r, nspec)
        items.append({
            "source": src,
            "name_id": str(r.get(nspec.name_id_col) or ""),
            "name": name,
            "name_variants": {c: (r.get(c) or "") for c in nspec.name_cols},
        })
    return {"source": src, "keyword": nm, "count": len(items), "items": items}


def get_person_movies(
    name_id: str,
    source: str = "douban",
    limit: int = 30,
    client: Optional[ProdReadOnlyClient] = None,
) -> dict:
    """取某人物（name_id）的作品集。"""
    c = _client(client)
    src = _norm_source(source)
    rspec = sm.RELATION_TABLES[src]
    mspec = sm.MAIN_TABLES[src]
    nid = (name_id or "").strip()
    if not nid:
        raise McpError("name_id 不能为空")
    limit = max(1, min(int(limit), c.s.MCP_MAX_LIMIT))
    db = _db(src, c)

    rres = c.query(
        db,
        f"SELECT DISTINCT {rspec.key_col} AS k, {rspec.role_col} AS role "
        f"FROM {rspec.table} WHERE {rspec.name_id_col} = %s LIMIT {limit}",
        (nid,), max_limit=limit, tool="get_person_movies")
    keys = [str(r["k"]) for r in rres.rows if r.get("k")]
    if not keys:
        return {"source": src, "name_id": nid, "count": 0, "items": []}
    kph = ", ".join(["%s"] * len(keys))
    mres = c.query(
        db,
        f"SELECT * FROM {mspec.table} WHERE {mspec.key} IN ({kph}) LIMIT {limit}",
        tuple(keys), max_limit=limit, tool="get_person_movies.main")
    items = [sm.normalize_main(r, mspec) for r in mres.rows]
    return {"source": src, "name_id": nid, "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# 5. 维度字典
# ---------------------------------------------------------------------------

def list_dimensions(
    kind: str = "genre",
    source: str = "douban",
    client: Optional[ProdReadOnlyClient] = None,
) -> dict:
    """列类型/国家维度字典。"""
    c = _client(client)
    src = _norm_source(source)
    k = (kind or "genre").lower().strip()
    if k in ("genre", "genres", "type"):
        spec = sm.GENRE_DIMENSIONS[src]
    elif k in ("country", "countries", "region"):
        spec = sm.COUNTRY_DIMENSIONS[src]
    else:
        raise McpError(f"未知维度类型：{kind}（仅支持 genre / country）")
    dim_cols = ", ".join(spec.dim_name_cols)
    res = c.query(_db(src, c),
                  f"SELECT {dim_cols} FROM {spec.dim_table} LIMIT 500",
                  max_limit=500, tool="list_dimensions")
    items = []
    for r in res.rows:
        label = ""
        for col in spec.dim_name_cols:
            if (r.get(col) or "").strip():
                label = str(r[col]).strip()
                break
        if label:
            items.append({"name": label, "raw": {k2: r.get(k2) for k2 in spec.dim_name_cols}})
    return {"source": src, "kind": k, "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# 6. 表规模概览
# ---------------------------------------------------------------------------

def get_table_stats(
    source: str = "douban",
    client: Optional[ProdReadOnlyClient] = None,
) -> dict:
    """表规模概览（走 information_schema 只读元数据通道）。

    说明：information_schema 默认被护栏禁止；此处显式开启受控元数据通道
    （仅 tables 元数据对象 + 必须带 table_schema 过滤），SQL 由本模块硬编码，
    外部输入仅作为绑定参数传入。
    """
    c = _client(client)
    src = _norm_source(source)
    db = _db(src, c)
    sql = (
        "SELECT table_name AS t, table_rows AS n FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name LIKE %s ORDER BY table_rows DESC LIMIT 100"
    )
    res = c.query(db, sql, (db, f"{'imdb' if src == sm.SOURCE_IMDB else 'douban'}_%"),
                  max_limit=100, tool="get_table_stats", allow_metadata=True)
    return {
        "source": src,
        "db": db,
        "count": len(res.rows),
        "tables": [{"table": r.get("t"), "rows_est": int(r.get("n") or 0)} for r in res.rows],
    }


# ---------------------------------------------------------------------------
# 7. 跨源比对（豆瓣↔IMDb）
# ---------------------------------------------------------------------------

def compare_cross_source(
    keyword: str = "",
    limit: int = 20,
    client: Optional[ProdReadOnlyClient] = None,
) -> dict:
    """跨源比对：用生产库官方视图口径对照豆瓣与 IMDb 的同片评分/票数/标题。

    数据源：`imdb2.v_imdb_douban_compare`（由 imdb2.imdb_main JOIN douban_main
    ON imdb_id 生成），是生产侧对跨源映射的官方口径。
    """
    c = _client(client)
    limit = max(1, min(int(limit), c.s.MCP_MAX_LIMIT))
    cols = ", ".join(sm.CROSS_SOURCE_VIEW_COLUMNS)
    kw = (keyword or "").strip()
    if kw:
        sql = (f"SELECT {cols} FROM {sm.CROSS_SOURCE_VIEW} "
               f"WHERE imdb_title LIKE %s OR douban_title_cn LIKE %s "
               f"OR imdb_title_cn LIKE %s LIMIT {limit}")
        params = (f"%{kw}%", f"%{kw}%", f"%{kw}%")
    else:
        sql = f"SELECT {cols} FROM {sm.CROSS_SOURCE_VIEW} LIMIT {limit}"
        params = ()
    res = c.query(c.s.MCP_DB_DOUBAN, sql, params, max_limit=limit,
                  tool="compare_cross_source")
    items = []
    for r in res.rows:
        items.append({
            "imdb_id": str(r.get("imdb_id") or ""),
            "douban_id": str(r.get("douban_id") or ""),
            "imdb_title": str(r.get("imdb_title") or ""),
            "imdb_title_cn": str(r.get("imdb_title_cn") or ""),
            "douban_title_cn": str(r.get("douban_title_cn") or ""),
            "imdb_rating": float(r.get("imdb_rating") or 0.0),
            "imdb_votes": int(r.get("imdb_votes") or 0),
            "douban_rating": float(r.get("douban_rating") or 0.0),
            "douban_votes": int(r.get("douban_votes") or 0),
            "year": int(r.get("release_date_year") or 0),
            "genres": sm.parse_pipe(r.get("douban_genres")),
        })
    return {"count": len(items), "keyword": kw, "items": items}


# ---------------------------------------------------------------------------
# 8. Schema 元数据（供 MCP resource 使用）
# ---------------------------------------------------------------------------

def describe_schema() -> dict:
    """本通道登记的表/字段映射（离线可读，无需连库）。"""
    return {
        "main_tables": {
            src: {
                "table": spec.table,
                "key": spec.key,
                "title_cols": list(spec.title_cols),
                "rating_col": spec.rating_col,
                "votes_col": spec.votes_col,
            } for src, spec in sm.MAIN_TABLES.items()
        },
        "relation_tables": {
            src: {"table": spec.table, "key_col": spec.key_col,
                  "name_id_col": spec.name_id_col}
            for src, spec in sm.RELATION_TABLES.items()
        },
        "name_tables": {src: spec.table for src, spec in sm.NAME_TABLES.items()},
        "text_tables": {src: spec.table for src, spec in sm.TEXT_TABLES.items()},
        "genre_dimensions": {
            src: {"relate": spec.relate_table, "dict": spec.dim_table}
            for src, spec in sm.GENRE_DIMENSIONS.items()
        },
        "country_dimensions": {
            src: {"relate": spec.relate_table, "dict": spec.dim_table}
            for src, spec in sm.COUNTRY_DIMENSIONS.items()
        },
        "cross_source_view": sm.CROSS_SOURCE_VIEW,
        "cross_source_view_columns": list(sm.CROSS_SOURCE_VIEW_COLUMNS),
        "all_tables": sm.all_tables(),
    }
