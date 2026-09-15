"""冷启动召回：无用户行为时，用「质量×热度」榜单兜底（高分/高评分人数）。

仅用 douban_main（样本含真实评分）；imdb_main 样本评分缺失，暂不并入。
"""
from math import log10
from retrieval.candidates import Candidate, split_genres
from db.session import fetch_all


def _quality_score(rating, rating_count) -> float:
    """口碑(0-10) × log10(1+人数)，兼顾评分与人气。"""
    return float(rating or 0) * log10(1 + (rating_count or 0))


def coldstart_recall(
    top_k: int = 20,
    genres: list[str] | None = None,
    country: str | None = None,
    min_year: int | None = None,
) -> list[Candidate]:
    """高分榜召回，支持类型/国家/年份过滤。"""
    where = ["1=1"]
    params: dict = {}
    if country:
        where.append("country LIKE :c")
        params["c"] = f"%{country}%"
    if min_year is not None:
        where.append("release_date_year >= :y")
        params["y"] = min_year
    sql = (
        "SELECT id, douban_id, title_text_cn, original_title, release_date_year, "
        "rating, rating_count, genres, country "
        f"FROM douban_main WHERE {' AND '.join(where)} "
        "ORDER BY (rating * (1 + LOG10(1 + rating_count))) DESC LIMIT :lim"
    )
    params["lim"] = top_k * 4
    rows = fetch_all(sql, params)
    out: list[Candidate] = []
    for m in rows:
        title = (m["title_text_cn"] or m["original_title"] or "").strip()
        if not title:
            continue
        g = split_genres(m["genres"])
        if genres and not (set(genres) & set(g)):
            continue
        out.append(Candidate(
            id=f"douban:{m['id']}",
            source="douban",
            title=title,
            year=int(m["release_date_year"] or 0),
            genres=g,
            country=m["country"] or "",
            rating=float(m["rating"] or 0),
            rating_count=int(m["rating_count"] or 0),
            recall_source="coldstart",
            score=_quality_score(m["rating"], m["rating_count"]),
            douban_id=str(m.get("douban_id") or ""),
            imdb_id=str(m.get("imdb_id") or ""),
        ))
        if len(out) >= top_k:
            break
    return out
