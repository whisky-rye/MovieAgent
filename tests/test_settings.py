"""配置层（config/settings.py + vector/backend.py）单测。

验证关键派生属性 / 工厂：
- MYSQL_DSN（aiomysql）/ MOVIE_AGENT_MYSQL_DSN（pymysql, 库名 movie_agent）；
- RRF_WEIGHTS_DICT 解析；
- default_collection 随 EMBED_PROVIDER 切换（ollama→movies / cloud→movies_cloud）；
- 云端 LLM 链路属性可构造（lazy，不触发网络）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config.settings as cs
from vector.backend import default_collection


def test_mysql_dsn():
    assert cs.settings.MYSQL_DSN.startswith("mysql+aiomysql")


def test_movie_agent_dsn():
    d = cs.settings.MOVIE_AGENT_MYSQL_DSN
    assert d.startswith("mysql+pymysql") and "movie_agent" in d


def test_rrf_weights_dict():
    # graph 为知识图谱多跳召回权重（F1/O12），由 .env RRF_WEIGHTS 覆盖默认值
    assert cs.settings.RRF_WEIGHTS_DICT == {
        "content": 1.0, "relation": 0.8, "coldstart": 0.6, "graph": 0.7,
    }


def test_default_collection_ollama(monkeypatch):
    monkeypatch.setattr(cs.settings, "EMBED_PROVIDER", "ollama")
    assert default_collection() == "movies"


def test_default_collection_cloud(monkeypatch):
    monkeypatch.setattr(cs.settings, "EMBED_PROVIDER", "cloud")
    assert default_collection() == "movies_cloud"


def test_cloud_chains_present(monkeypatch):
    monkeypatch.setattr(cs.settings, "CLOUD_LLM_API_KEY", "dummy-for-construct")
    # 构造 lazy ChatOpenAI（不触发网络调用）
    assert cs.settings.cloud_strong_chain is not None
    assert cs.settings.cloud_light_chain is not None
