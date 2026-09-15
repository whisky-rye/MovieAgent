"""关系召回：基于「同导演/同编剧/同卡司」找相关作品。

开发期走 MySQL 关系表（douban_name_relate / imdb_name_relate），
生产期可无缝换成 Neo4j 图查询（接口不变，见架构文档第 5 节）。
"""
from retrieval.candidates import Candidate, split_genres
from db.session import fetch_all

# 不同职位的关联权重（导演权重最高，编剧次之，卡司/其他最低）
_ROLE_WEIGHT = {
    "director": 2.5, "导演": 2.5,
    "writer": 1.5, "编剧": 1.5,
    "cast": 1.0, "主演": 1.0, "actor": 1.0,
}
_DEF_WEIGHT = 1.0


def relation_recall(item_id: str, top_k: int = 20) -> list[Candidate]:
    """以某作品找「同剧组」相关作品。item_id 形如 douban:{PK} 或 imdb:{imdb_id}。"""
    if item_id.startswith("douban:"):
        return _relation_douban(item_id, top_k)
    if item_id.startswith("imdb:"):
        return _relation_imdb(item_id, top_k)
    raise ValueError("item_id 须以 douban: 或 imdb: 开头")


def _relation_douban(item_id: str, top_k: int) -> list[Candidate]:
    pk = int(item_id.split(":", 1)[1])
    seed = fetch_all("SELECT douban_id FROM douban_main WHERE id=:pk", {"pk": pk})
    if not seed:
        return []
    seed_did = seed[0]["douban_id"]
    people = fetch_all(
        "SELECT name_id, credits_type FROM douban_name_relate WHERE douban_id=:d",
        {"d": seed_did},
    )
    if not people:
        return []
    ids = [p["name_id"] for p in people]
    rows = fetch_all(
        "SELECT n.douban_id AS did, "
        "SUM(CASE n.credits_type WHEN 'director' THEN 2.5 WHEN 'writer' THEN 1.5 ELSE 1.0 END) AS shared "
        "FROM douban_name_relate n "
        "WHERE n.name_id IN :ids AND n.douban_id <> :seed "
        "GROUP BY n.douban_id ORDER BY shared DESC LIMIT :lim",
        {"ids": ids, "seed": seed_did, "lim": top_k * 4},
    )
    dids = [r["did"] for r in rows]
    if not dids:
        return []
    mains = fetch_all(
        "SELECT id, douban_id, title_text_cn, original_title, release_date_year, "
        "rating, rating_count, genres, country "
        "FROM douban_main WHERE douban_id IN :dids",
        {"dids": dids},
    )
    mm = {m["douban_id"]: m for m in mains}
    out: list[Candidate] = []
    for r in rows:
        m = mm.get(r["did"])
        if not m:
            continue
        title = (m["title_text_cn"] or m["original_title"] or "").strip()
        if not title:
            continue
        out.append(Candidate(
            id=f"douban:{m['id']}",
            source="douban",
            title=title,
            year=int(m["release_date_year"] or 0),
            genres=split_genres(m["genres"]),
            country=m["country"] or "",
            rating=float(m["rating"] or 0),
            rating_count=int(m["rating_count"] or 0),
            recall_source="relation",
            score=float(r["shared"]),
            douban_id=str(m.get("douban_id") or ""),
        ))
    return out[:top_k]


def _relation_imdb(item_id: str, top_k: int) -> list[Candidate]:
    imdb_id = item_id.split(":", 1)[1]
    people = fetch_all(
        "SELECT name_id, credits_type FROM imdb_name_relate WHERE imdb_id=:d",
        {"d": imdb_id},
    )
    if not people:
        return []
    ids = [p["name_id"] for p in people]
    rows = fetch_all(
        "SELECT n.imdb_id AS iid, "
        "SUM(CASE n.credits_type WHEN 'director' THEN 2.5 WHEN 'writer' THEN 1.5 ELSE 1.0 END) AS shared "
        "FROM imdb_name_relate n "
        "WHERE n.name_id IN :ids AND n.imdb_id <> :seed "
        "GROUP BY n.imdb_id ORDER BY shared DESC LIMIT :lim",
        {"ids": ids, "seed": imdb_id, "lim": top_k * 4},
    )
    iids = [r["iid"] for r in rows]
    if not iids:
        return []
    mains = fetch_all(
        "SELECT imdb_id, title_text_cn, title_text, release_date_year, "
        "rate_agg, rate_votes, genres, country "
        "FROM imdb_main WHERE imdb_id IN :iids",
        {"iids": iids},
    )
    mm = {m["imdb_id"]: m for m in mains}
    out: list[Candidate] = []
    for r in rows:
        m = mm.get(r["iid"])
        if not m:
            continue
        title = (m["title_text_cn"] or m["title_text"] or "").strip()
        if not title:
            continue
        out.append(Candidate(
            id=f"imdb:{m['imdb_id']}",
            source="imdb",
            title=title,
            year=int(m["release_date_year"] or 0),
            genres=split_genres(m["genres"]),
            country=m["country"] or "",
            rating=float(m["rate_agg"] or 0),
            rating_count=int(m["rate_votes"] or 0),
            recall_source="relation",
            score=float(r["shared"]),
            imdb_id=str(m.get("imdb_id") or ""),
        ))
    return out[:top_k]
