"""双源语料构建：从 douban_main/imdb_main（+ *_text）抽取作品，拼接可嵌入文档与过滤用 metadata。

通过 imdb_id 双源独立建索引（douban:*/imdb:* 两套 id），开发期足够；
后续如需双源融合召回，可在 metadata 增加 unified_key 做去重/聚合。

分页说明（修复「全量 SELECT 一次性 .all() 拉爆内存」问题）：
  - 数据库实际为 1:1 关系（经核查：douban_main=1072/douban_text=1072，imdb_main=62/imdb_text=60，
    仅 2 条 imdb_main 无 text 行，由 LEFT JOIN 正确处理）。故对每个主表分页拉取，
    分页边界精确、无错位。
  - P2-5：分页方式为**键集分页**（`WHERE <pk> > :last ORDER BY <pk> LIMIT :lim`）而非
    深 OFFSET——OFFSET 越深数据库要越过的行越多（55 万全量建库后半程每页都从头扫描），
    键集分页每页都从主键索引位置开始，成本恒定。
  - `build_corpus()` 仍返回全量列表（向后兼容探针脚本）；`_iter_douban/_iter_imdb/iter_corpus`
    为流式分页生成器，供建库脚本逐页消费，内存恒定受 settings.BUILD_PAGE_SIZE 约束
    （函数内读取，勿在导入期绑定默认值）。
"""
import re
from sqlalchemy import create_engine, text
from config.settings import settings

# 注意：分页大小一律在函数内读 settings.BUILD_PAGE_SIZE（P2-5）——模块级默认参数
# 会在导入期绑定，导致测试 monkeypatch settings 不生效。
_SYNC_DSN = settings.MYSQL_DSN.replace("mysql+aiomysql", "mysql+pymysql")


def _clip(s, n):
    return (s or "")[:n]


def _clean(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _split_genres(s):
    """把 '剧情|科幻/喜剧' 这类串拆成列表，供 Chroma list-metadata 过滤（覆盖豆瓣/IMDb 的 | / , 分隔）。"""
    return [g for g in re.split(r"[\|/,、\s]+", _clean(s)) if g]


# ---------- 豆瓣 ----------
def _row_to_douban_rec(r: dict) -> dict:
    title = _clean(r["title_text_cn"] or r["original_title"])
    if not title:
        return None
    parts = [f"片名：{title}"]
    if r["original_title"]:
        parts.append(f"原名：{_clean(r['original_title'])}")
    if r["release_date_year"]:
        parts.append(f"年份：{r['release_date_year']}")
    if r["genres"]:
        parts.append(f"类型：{_clean(r['genres'])}")
    if r["country"]:
        parts.append(f"国家：{_clean(r['country'])}")
    if r["director_name"]:
        parts.append(f"导演：{_clean(r['director_name'])}")
    if r["description"]:
        parts.append(f"简介：{_clip(r['description'], 800)}")
    if r["plot"]:
        parts.append(f"剧情：{_clip(r['plot'], 800)}")
    if r["tags"]:
        parts.append(f"标签：{_clip(r['tags'], 400)}")
    doc = " ".join(parts)[:1500]
    meta = {
        "source": "douban",
        "title": title,
        "year": _to_int(r["release_date_year"]),
        "rating": _to_float(r["rating"]),
        "rating_count": _to_int(r["rating_count"]),
        "country": _clean(r["country"]),
        "genres": _clean(r["genres"]),
        "genres_list": _split_genres(r["genres"]),
        "director": _clean(r["director_name"]),
    }
    return {
        "id": f"douban:{r['id']}",
        "source": "douban",
        "doc": doc,
        "meta": _clean_meta(meta),
    }


def _iter_douban(page_size: int | None = None):
    """流式分页生成器：每页最多 page_size 条 recs（list）。内存恒定，适合全量建库。

    P2-5：键集分页（WHERE id > :last ORDER BY id LIMIT）替代深 OFFSET——
    OFFSET 越深数据库要越过的行越多（55 万全量建库后半程每页都从头扫描），
    键集分页每页都从主键索引位置开始，成本恒定。
    page_size 省略时**函数内**取 settings.BUILD_PAGE_SIZE（模块级默认参数在
    导入期绑定，测试 monkeypatch settings 不生效）。
    """
    page_size = page_size or settings.BUILD_PAGE_SIZE
    eng = create_engine(_SYNC_DSN)
    sql = text(
        "SELECT m.id, m.title_text_cn, m.original_title, m.release_date_year, m.genres, m.country, "
        "m.rating, m.rating_count, m.director_name, "
        "t.description, t.plot, t.tags "
        "FROM (SELECT * FROM douban_main WHERE id > :last ORDER BY id LIMIT :lim) m "
        "LEFT JOIN douban_text t ON m.id = t.id"
    )
    last = 0
    while True:
        with eng.connect() as c:
            rows = c.execute(sql, {"lim": page_size, "last": last}).mappings().all()
        if not rows:
            return
        page = [rec for r in rows if (rec := _row_to_douban_rec(r)) is not None]
        yield page
        if len(rows) < page_size:
            return
        last = rows[-1]["id"]


def _build_douban(page_size: int | None = None) -> list[dict]:
    page_size = page_size or settings.BUILD_PAGE_SIZE
    recs = []
    for page in _iter_douban(page_size):
        recs.extend(page)
    return recs


# ---------- IMDb ----------
def _row_to_imdb_rec(r: dict) -> dict:
    title = _clean(r["title_text_cn"] or r["title_text"])
    if not title:
        return None
    parts = [f"片名：{title}"]
    if r["release_date_year"]:
        parts.append(f"年份：{r['release_date_year']}")
    if r["genres"]:
        parts.append(f"类型：{_clean(r['genres'])}")
    if r["country"]:
        parts.append(f"国家：{_clean(r['country'])}")
    if r["director_name"]:
        parts.append(f"导演：{_clean(r['director_name'])}")
    if r["plot_text"]:
        parts.append(f"剧情：{_clip(r['plot_text'], 800)}")
    if r["plot_keywords"]:
        parts.append(f"关键词：{_clip(r['plot_keywords'], 300)}")
    if r["tagline"]:
        parts.append(f"标语：{_clip(r['tagline'], 200)}")
    doc = " ".join(parts)[:1500]
    meta = {
        "source": "imdb",
        "title": title,
        "year": _to_int(r["release_date_year"]),
        "rating": float(r["rate_agg"] or 0.0),      # imdb_main.rate_agg（IMDb 加权评分）
        "rating_count": int(r["rate_votes"] or 0),  # imdb_main.rate_votes（评分人数）
        "country": _clean(r["country"]),
        "genres": _clean(r["genres"]),
        "genres_list": _split_genres(r["genres"]),
        "director": _clean(r["director_name"]),
    }
    return {
        "id": f"imdb:{r['imdb_id']}",
        "source": "imdb",
        "doc": doc,
        "meta": _clean_meta(meta),
    }


def _iter_imdb(page_size: int | None = None):
    """流式分页生成器（P2-5：键集分页，见 _iter_douban 说明；imdb 主键为字符串）。"""
    page_size = page_size or settings.BUILD_PAGE_SIZE
    eng = create_engine(_SYNC_DSN)
    sql = text(
        "SELECT m.imdb_id, m.title_text_cn, m.title_text, m.release_date_year, m.genres, m.country, "
        "m.director_name, m.rate_agg, m.rate_votes, "
        "t.plot_text, t.plot_keywords, t.tagline "
        "FROM (SELECT * FROM imdb_main WHERE imdb_id > :last ORDER BY imdb_id LIMIT :lim) m "
        "LEFT JOIN imdb_text t ON m.imdb_id = t.imdb_id"
    )
    last = ""
    while True:
        with eng.connect() as c:
            rows = c.execute(sql, {"lim": page_size, "last": last}).mappings().all()
        if not rows:
            return
        page = [rec for r in rows if (rec := _row_to_imdb_rec(r)) is not None]
        yield page
        if len(rows) < page_size:
            return
        last = rows[-1]["imdb_id"]


def _build_imdb(page_size: int | None = None) -> list[dict]:
    page_size = page_size or settings.BUILD_PAGE_SIZE
    recs = []
    for page in _iter_imdb(page_size):
        recs.extend(page)
    return recs


# ---------- 公共 ----------
def _clean_meta(meta: dict) -> dict:
    """Chroma 不允许空的 metadata list 值；空 genres_list 直接移除该键（缺失即不匹配，符合预期）。"""
    return {k: v for k, v in meta.items() if not (isinstance(v, list) and len(v) == 0)}


def iter_corpus(page_size: int | None = None):
    """流式分页生成器：依次产出豆瓣页、IMDb 页（每页 list[rec]）。建库脚本逐页消费，内存恒定。"""
    page_size = page_size or settings.BUILD_PAGE_SIZE
    yield from _iter_douban(page_size)
    yield from _iter_imdb(page_size)


def build_corpus() -> list[dict]:
    """返回统一语料：每条 {id, source, doc, meta}。向后兼容（探针脚本用），大批量建库请改用 iter_corpus。"""
    recs = []
    for page in iter_corpus():
        recs.extend(page)
    return recs
