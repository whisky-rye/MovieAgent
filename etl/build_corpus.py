"""双源语料构建：从 douban_main/imdb_main（+ *_text）抽取作品，拼接可嵌入文档与过滤用 metadata。

通过 imdb_id 双源独立建索引（douban:*/imdb:* 两套 id），开发期足够；
后续如需双源融合召回，可在 metadata 增加 unified_key 做去重/聚合。
"""
import re
from sqlalchemy import create_engine, text
from config.settings import settings

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


def _build_douban():
    recs = []
    eng = create_engine(_SYNC_DSN)
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT m.id, m.title_text_cn, m.original_title, m.release_date_year, m.genres, m.country, "
            "m.rating, m.rating_count, m.director_name, "
            "t.description, t.plot, t.tags "
            "FROM douban_main m LEFT JOIN douban_text t ON m.id = t.id"
        )).mappings().all()
    for r in rows:
        title = _clean(r["title_text_cn"] or r["original_title"])
        if not title:
            continue
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
        recs.append({
            "id": f"douban:{r['id']}",
            "source": "douban",
            "doc": doc,
            "meta": {
                "source": "douban",
                "title": title,
                "year": _to_int(r["release_date_year"]),
                "rating": _to_float(r["rating"]),
                "rating_count": _to_int(r["rating_count"]),
                "country": _clean(r["country"]),
                "genres": _clean(r["genres"]),
                "genres_list": _split_genres(r["genres"]),
                "director": _clean(r["director_name"]),
            },
        })
    return recs


def _build_imdb():
    recs = []
    eng = create_engine(_SYNC_DSN)
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT m.imdb_id, m.title_text_cn, m.title_text, m.release_date_year, m.genres, m.country, "
            "m.director_name, m.rate_agg, m.rate_votes, "
            "t.plot_text, t.plot_keywords, t.tagline "
            "FROM imdb_main m LEFT JOIN imdb_text t ON m.imdb_id = t.imdb_id"
        )).mappings().all()
    for r in rows:
        title = _clean(r["title_text_cn"] or r["title_text"])
        if not title:
            continue
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
        recs.append({
            "id": f"imdb:{r['imdb_id']}",
            "source": "imdb",
            "doc": doc,
            "meta": {
                "source": "imdb",
                "title": title,
                "year": _to_int(r["release_date_year"]),
                "rating": float(r["rate_agg"] or 0.0),      # imdb_main.rate_agg（IMDb 加权评分）
                "rating_count": int(r["rate_votes"] or 0),  # imdb_main.rate_votes（评分人数）
                "country": _clean(r["country"]),
                "genres": _clean(r["genres"]),
                "genres_list": _split_genres(r["genres"]),
                "director": _clean(r["director_name"]),
            },
        })
    return recs


def _clean_meta(meta: dict) -> dict:
    """Chroma 不允许空的 metadata list 值；空 genres_list 直接移除该键（缺失即不匹配，符合预期）。"""
    return {k: v for k, v in meta.items() if not (isinstance(v, list) and len(v) == 0)}


def build_corpus() -> list[dict]:
    """返回统一语料：每条 {id, source, doc, meta}。"""
    recs = _build_douban() + _build_imdb()
    for r in recs:
        r["meta"] = _clean_meta(r["meta"])
    return recs
