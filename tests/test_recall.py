"""多路召回（retrieval/coldstart_recall.py, relation_recall.py, content_recall.py）单测。

- coldstart_recall：依赖真实 MySQL（movie_agent.douban_main），验证返回列表且按质量分降序；
  _quality_score 单调性。
- relation_recall：用受控 fetch_all 验证「同剧组」组装 Candidate 与角色加权（导演>卡司）。
- content_recall：用 monkeypatch 替换向量库/嵌入客户端，验证召回合并与「以物品找相似」时排除自身。
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import importlib

# 用 importlib 直接取子模块对象，避免 retrieval/__init__.py 把同名函数重导出后
# 「import retrieval.xxx as xxx」绑定到函数而非模块。
csr = importlib.import_module("retrieval.coldstart_recall")
rrr = importlib.import_module("retrieval.relation_recall")
ccr = importlib.import_module("retrieval.content_recall")
from retrieval.candidates import Candidate


# ---------- coldstart（真实 MySQL） ----------

def test_coldstart_returns_sorted(mysql_ok):
    out = csr.coldstart_recall(top_k=5)
    assert isinstance(out, list) and len(out) <= 5
    if len(out) >= 2:
        assert out[0].score >= out[1].score  # SQL 已按质量分降序


def test_coldstart_quality_score():
    assert csr._quality_score(9.0, 100000) > csr._quality_score(5.0, 10)


# ---------- relation（受控 fetch） ----------

def _fake_fetch(sql, params):
    if "credits_type" in sql and "SUM(" not in sql:
        return [{"name_id": "p1", "credits_type": "director"}, {"name_id": "p2", "credits_type": "cast"}]
    if "SUM(" in sql:
        return [{"did": "999", "shared": 2.5}, {"did": "888", "shared": 1.0}]
    if "douban_main WHERE douban_id" in sql:
        return [
            {"id": 10, "douban_id": "999", "title_text_cn": "导演片", "original_title": None,
             "release_date_year": 2001, "rating": 8.0, "rating_count": 100, "genres": "剧情", "country": "中国"},
            {"id": 11, "douban_id": "888", "title_text_cn": "演员片", "original_title": None,
             "release_date_year": 2002, "rating": 7.0, "rating_count": 50, "genres": "喜剧", "country": "中国"},
        ]
    if "SELECT douban_id FROM douban_main WHERE id" in sql:
        return [{"douban_id": "555"}]  # seed
    return []


def test_relation_recall_role_weighting(monkeypatch):
    monkeypatch.setattr(rrr, "fetch_all", _fake_fetch)
    out = rrr.relation_recall("douban:1", top_k=10)
    assert all(isinstance(c, Candidate) for c in out)
    d = next(c for c in out if c.douban_id == "999")
    c = next(c for c in out if c.douban_id == "888")
    assert d.score > c.score  # 导演权重高于卡司


# ---------- content（受控向量库/嵌入） ----------

class FakeBackend:
    def __init__(self):
        self._emb = {"douban:1": [0.1, 0.2]}

    def query(self, vector, top_k, where, genres):
        return [
            ("douban:1", "doc", {"source": "douban", "title": "A", "year": 2020,
             "genres_list": ["剧情"], "country": "中国", "rating": 8.0, "rating_count": 100,
             "director": "d", "douban_id": "11", "imdb_id": "tt1"}, 0.1),
            ("douban:2", "doc2", {"source": "douban", "title": "B", "year": 2021,
             "genres_list": ["喜剧"], "country": "中国", "rating": 7.0, "rating_count": 50,
             "director": "e", "douban_id": "22", "imdb_id": "tt2"}, 0.2),
        ]

    def get_embeddings(self, ids):
        return [self._emb.get(i, [0.0, 0.0]) for i in ids]


class FakeEmb:
    def embed_query(self, text):
        return [0.1, 0.2]

    def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


def test_content_recall_merge(monkeypatch):
    monkeypatch.setattr(ccr, "get_vector_backend", lambda: FakeBackend())
    monkeypatch.setattr(ccr, "get_embed_client", lambda: FakeEmb())
    # douban_id/imdb_id 由 content_recall 按主键回查补全（与线上一致）
    monkeypatch.setattr(
        ccr, "fetch_all",
        lambda sql, params: [{"id": 1, "douban_id": "11", "imdb_id": "tt1"}],
    )
    out = ccr.content_recall(query="科幻", top_k=5)
    assert out and out[0].id == "douban:1"
    assert out[0].title == "A" and out[0].year == 2020
    assert out[0].douban_id == "11" and out[0].imdb_id == "tt1"


def test_content_recall_excludes_self(monkeypatch):
    monkeypatch.setattr(ccr, "get_vector_backend", lambda: FakeBackend())
    monkeypatch.setattr(ccr, "get_embed_client", lambda: FakeEmb())
    out = ccr.content_recall(item_id="douban:1", top_k=5)
    assert all(c.id != "douban:1" for c in out)  # 排除自身
