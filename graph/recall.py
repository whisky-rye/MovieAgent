"""知识图谱召回对外 API（F1/O12）。

把 ``KnowledgeGraphStore.traverse`` 的 GraphHit 转成召回层统一的 ``Candidate``，
``recall_source="graph"``、``graph_paths`` 携带可解释关系链。

设计：
- ``graph_recall(item_id, ...)``：以某作品为多跳种子，扩展「同导演/编剧/卡司」及可选
  类型/地区邻居，产出多样化、可解释的相似候选。
- ``graph_recall_by_entity(name, ...)``：以人物名（导演/演员）为中心，扩展其作品 +
  合作网络，支持「导齐鲁诺的片子」「小李子演过什么」这类实体中心召回。
- 均接受 ``store`` 注入，便于单测用假 fetcher 脱离 MySQL 验证图谱算法。
"""
from typing import Optional

from retrieval.candidates import Candidate, split_genres
from config.settings import settings
from graph.store import get_graph_store, KnowledgeGraphStore


def _hit_to_candidate(h) -> Candidate:
    return Candidate(
        id=h.item_id,
        source=h.source,
        title=h.title,
        year=h.year,
        genres=h.genres,
        country=h.country,
        rating=h.rating,
        rating_count=h.rating_count,
        recall_source="graph",
        score=round(h.score, 6),
        douban_id=h.douban_id,
        imdb_id=h.imdb_id,
        graph_paths=h.paths,
    )


def graph_recall(
    item_id: str,
    top_k: int = 20,
    store: Optional[KnowledgeGraphStore] = None,
    max_hops: Optional[int] = None,
    expand_genre: Optional[bool] = None,
    expand_country: Optional[bool] = None,
) -> list[Candidate]:
    """以某作品为多跳种子，做知识图谱扩展召回。

    item_id 形如 douban:{PK} 或 imdb:{imdb_id}。GRAPH_ENABLED=False 时返回空列表。
    """
    if not settings.GRAPH_ENABLED:
        return []
    if not item_id or ":" not in item_id:
        return []
    st = store or get_graph_store()
    hits = st.traverse(
        [item_id],
        max_hops=max_hops if max_hops is not None else settings.GRAPH_MAX_HOPS,
        top_k=top_k,
        expand_genre=expand_genre if expand_genre is not None else settings.GRAPH_EXPAND_GENRE,
        expand_country=expand_country if expand_country is not None else settings.GRAPH_EXPAND_COUNTRY,
    )
    return [_hit_to_candidate(h) for h in hits]


def graph_recall_by_entity(
    name: str,
    top_k: int = 20,
    store: Optional[KnowledgeGraphStore] = None,
    max_hops: Optional[int] = None,
    expand_genre: Optional[bool] = None,
    expand_country: Optional[bool] = None,
) -> list[Candidate]:
    """以人物名（导演/演员）为中心，扩展其作品 + 合作网络。"""
    if not settings.GRAPH_ENABLED:
        return []
    if not name or not name.strip():
        return []
    st = store or get_graph_store()
    pkeys = st.search_person(name.strip())
    if not pkeys:
        return []
    hits = st.traverse(
        pkeys,
        max_hops=max_hops if max_hops is not None else settings.GRAPH_MAX_HOPS,
        top_k=top_k,
        expand_genre=expand_genre if expand_genre is not None else settings.GRAPH_EXPAND_GENRE,
        expand_country=expand_country if expand_country is not None else settings.GRAPH_EXPAND_COUNTRY,
    )
    return [_hit_to_candidate(h) for h in hits]


def person_recall(
    name: str,
    top_k: int = 20,
    store: Optional[KnowledgeGraphStore] = None,
) -> list[Candidate]:
    """按导演/演员名召回其作品集（「找某人的作品」意图入口）。

    直接用 KnowledgeGraphStore.person_filmography（仅 fetcher、不依赖 GRAPH_ENABLED），
    产出 recall_source="person" 的候选，可无缝并入 RRF 融合。
    """
    if not name or not name.strip():
        return []
    st = store or get_graph_store()
    hits = st.person_filmography(name.strip(), top_k=top_k)
    out: list[Candidate] = []
    for h in hits:
        out.append(Candidate(
            id=h.item_id,
            source=h.source,
            title=h.title,
            year=h.year,
            genres=h.genres,
            country=h.country,
            rating=h.rating,
            rating_count=h.rating_count,
            recall_source="person",
            score=round(h.score, 6),
            douban_id=h.douban_id,
            imdb_id=h.imdb_id,
            graph_paths=h.paths,
        ))
    return out
