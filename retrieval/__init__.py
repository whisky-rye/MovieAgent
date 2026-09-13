"""召回层统一出口。"""
from retrieval.candidates import Candidate, split_genres
from retrieval.content_recall import content_recall
from retrieval.relation_recall import relation_recall
from retrieval.coldstart_recall import coldstart_recall

__all__ = [
    "Candidate", "split_genres",
    "content_recall", "relation_recall", "coldstart_recall",
]
