"""内容相似召回：把查询文本（或某作品）嵌入后，对向量库（Chroma/Qdrant/Milvus，按 VECTOR_BACKEND 切换）做向量检索。"""
from typing import Optional
from vector.backend import get_vector_backend
from vector.embed_client import get_embed_client
from retrieval.candidates import Candidate


def content_recall(
    query: Optional[str] = None,
    item_id: Optional[str] = None,
    top_k: int = 20,
    genres: Optional[list[str]] = None,
    country: Optional[str] = None,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    min_rating: Optional[float] = None,
) -> list[Candidate]:
    """向量检索召回。

    - query: 自然语言查询（如「太空科幻 孤独」），实时嵌入。
    - item_id: 以某作品找相似（取其在向量库中的嵌入，排除自身）。
    - 过滤：genres 走应用层（Chroma list-metadata 坑），其余走 Chroma 标量 where。
    返回按相似度(1-distance)降序的 Candidate 列表。
    """
    if not query and not item_id:
        raise ValueError("content_recall 需要 query 或 item_id 之一")

    backend = get_vector_backend()
    emb = get_embed_client()

    where: dict = {}
    if country:
        where["country"] = country
    yr: dict = {}
    if min_year is not None:
        yr["$gte"] = min_year
    if max_year is not None:
        yr["$lte"] = max_year
    if yr:
        where["year"] = yr
    if min_rating is not None:
        where["rating"] = {"$gte": min_rating}

    if query:
        vec = emb.embed_query(query)
    else:
        vecs = backend.get_embeddings([item_id])
        vec = vecs[0]

    rows = backend.query(vec, top_k=top_k, where=where or None, genres=genres)
    out: list[Candidate] = []
    for rid, doc, meta, dist in rows:
        if item_id and rid == item_id:
            continue
        out.append(Candidate(
            id=rid,
            source=meta.get("source", ""),
            title=meta.get("title", ""),
            year=meta.get("year", 0) or 0,
            genres=meta.get("genres_list", []) or [],
            country=meta.get("country", "") or "",
            rating=meta.get("rating", 0.0) or 0.0,
            rating_count=meta.get("rating_count", 0) or 0,
            doc=doc or "",
            recall_source="content",
            score=1.0 - dist,
        ))
    return out
