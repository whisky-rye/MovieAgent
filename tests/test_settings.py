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
    # graph=知识图谱多跳召回(F1/O12)；prod=生产库只读通道补充召回(MCP，默认关闭)
    assert cs.settings.RRF_WEIGHTS_DICT == {
        "content": 1.0, "relation": 0.8, "coldstart": 0.6, "graph": 0.7, "prod": 0.5,
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


# ---- P2-20：IMDB MySQL 弱默认凭证移除 + fail-fast ----
def test_mysql_weak_default_removed():
    """代码默认值不再为 root/root（弱默认）；实际值由 .env 覆盖。"""
    from config.settings import Settings

    assert Settings.model_fields["IMDB_MYSQL_USER"].default == ""
    assert Settings.model_fields["IMDB_MYSQL_PASS"].default == ""


def test_validate_mysql_config_raises_when_missing(monkeypatch):
    """缺凭证且未配置远端覆盖 → 启动 fail-fast。"""
    monkeypatch.setattr(cs.settings, "IMDB_MYSQL_USER", "")
    monkeypatch.setattr(cs.settings, "IMDB_MYSQL_PASS", "")
    monkeypatch.setattr(cs.settings, "MOVIE_AGENT_MYSQL_DSN_OVERRIDE", "")
    with pytest.raises(RuntimeError, match="不能为空"):
        cs.settings.validate_mysql_config()


def test_validate_mysql_config_ok_with_override(monkeypatch):
    """业务库走远端 DSN 覆盖 + 语料库凭证齐 → 通过。"""
    monkeypatch.setattr(cs.settings, "IMDB_MYSQL_USER", "u")
    monkeypatch.setattr(cs.settings, "IMDB_MYSQL_PASS", "p")
    monkeypatch.setattr(cs.settings, "MOVIE_AGENT_MYSQL_DSN_OVERRIDE",
                        "mysql+pymysql://u:p@47.1.2.3:3306/movie_agent")
    # 不应抛错
    cs.settings.validate_mysql_config()


def test_validate_mysql_config_override_still_requires_corpus_creds(monkeypatch):
    """即便业务库走远端覆盖，语料库凭证缺失仍 fail-fast。"""
    monkeypatch.setattr(cs.settings, "IMDB_MYSQL_USER", "")
    monkeypatch.setattr(cs.settings, "IMDB_MYSQL_PASS", "")
    monkeypatch.setattr(cs.settings, "MOVIE_AGENT_MYSQL_DSN_OVERRIDE",
                        "mysql+pymysql://u:p@47.1.2.3:3306/movie_agent")
    with pytest.raises(RuntimeError):
        cs.settings.validate_mysql_config()
