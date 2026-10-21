"""知识图谱召回层（F1/O12）。

对外暴露：
- graph_recall(item_id, top_k, store=None)：以作品为多跳种子的图谱扩展召回
- graph_recall_by_entity(name, top_k, store=None)：以人物为中心的实体召回
- KnowledgeGraphStore / MysqlGraphFetcher：可注入 fetcher 的遍历核心（便于单测）
"""
from graph.schema import (
    Relation,
    GraphHit,
    ROLE_TO_REL,
    REL_WEIGHT,
    REL_LABEL_CN,
)
from graph.store import (
    MysqlGraphFetcher,
    KnowledgeGraphStore,
    get_graph_store,
)
from graph.recall import graph_recall, graph_recall_by_entity

__all__ = [
    "Relation",
    "GraphHit",
    "ROLE_TO_REL",
    "REL_WEIGHT",
    "REL_LABEL_CN",
    "MysqlGraphFetcher",
    "KnowledgeGraphStore",
    "get_graph_store",
    "graph_recall",
    "graph_recall_by_entity",
]
