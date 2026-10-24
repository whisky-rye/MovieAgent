"""冷启动召回：无用户行为时，用「质量×热度」榜单兜底（高分/高评分人数）。

仅用 douban_main（样本含真实评分）；imdb_main 样本评分缺失，暂不并入。

P2-4：质量榜 SQL 的 ORDER BY 是计算表达式（rating*(1+LOG10(1+rating_count))），
无索引可走——全量灌入后每请求全表扫描 + filesort，延迟显著。榜单只随语料变化，
故按查询键（country/min_year/limit）做进程内 TTL 缓存：每 TTL 至多一次扫描
（多 worker 各自缓存，替代每请求一次）。过滤条件下推与 Python 侧类型过滤的
语义与旧实现完全一致（同一 SQL 同一批行）。
"""
import threading
import time
from math import log10
from retrieval.candidates import Candidate, split_genres
from db.session import fetch_all
from config.settings import settings

# 榜单缓存容器 + 锁；TTL 调用时读 settings（勿在导入期绑定）
_BOARD_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_BOARD_LOCK = threading.Lock()
_BOARD_CACHE_MAX = 64  # 过滤组合有限，防无限增长


def _quality_score(rating, rating_count) -> float:
    """口碑(0-10) × log10(1+人数)，兼顾评分与人气。"""
    return float(rating or 0) * log10(1 + (rating_count or 0))


def _board_rows(country: str | None, min_year: int | None, limit: int) -> list[dict]:
    """质量榜原始行（带 TTL 缓存）；SQL 与旧实现一致。"""
    ttl = settings.COLDSTART_BOARD_TTL_S  # 调用时读取（热切换/测试可调）
    key = (country, min_year, limit)
    now = time.monotonic()
    with _BOARD_LOCK:
        hit = _BOARD_CACHE.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    where = ["1=1"]
    params: dict = {}
    if country:
        where.append("country LIKE :c")
        params["c"] = f"%{country}%"
    if min_year is not None:
        where.append("release_date_year >= :y")
        params["y"] = min_year
    sql = (
        "SELECT id, douban_id, imdb_id, title_text_cn, original_title, release_date_year, "
        "rating, rating_count, genres, country "
        f"FROM douban_main WHERE {' AND '.join(where)} "
        "ORDER BY (rating * (1 + LOG10(1 + rating_count))) DESC LIMIT :lim"
    )
    params["lim"] = limit
    rows = fetch_all(sql, params)
    with _BOARD_LOCK:
        if len(_BOARD_CACHE) >= _BOARD_CACHE_MAX:
            _BOARD_CACHE.clear()
        _BOARD_CACHE[key] = (now + ttl, rows)
    return rows


def coldstart_recall(
    top_k: int = 20,
    genres: list[str] | None = None,
    country: str | None = None,
    min_year: int | None = None,
) -> list[Candidate]:
    """高分榜召回，支持类型/国家/年份过滤。"""
    rows = _board_rows(country, min_year, top_k * 4)
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
