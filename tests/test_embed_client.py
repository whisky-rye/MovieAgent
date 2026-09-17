"""嵌入客户端（vector/embed_client.py + vector/cloud_embed.py）单测。

验证：
- get_embed_client 按 EMBED_PROVIDER 切换 Ollama / 云端实现；
- OllamaEmbedClient 按 batch_size 分批（内部 _post 调用次数正确）；
- CloudEmbedClient batch_size 被夹到 20（百炼上限），且返回条数校验；
均用 monkeypatch 隔离真实网络。
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config.settings as cs
from vector.cloud_embed import CloudEmbedClient
from vector.embed_client import OllamaEmbedClient, get_embed_client


def test_factory_ollama(monkeypatch):
    monkeypatch.setattr(cs.settings, "EMBED_PROVIDER", "ollama")
    assert isinstance(get_embed_client(), OllamaEmbedClient)


def test_factory_cloud(monkeypatch):
    monkeypatch.setattr(cs.settings, "EMBED_PROVIDER", "cloud")
    monkeypatch.setattr(cs.settings, "CLOUD_EMBED_API_KEY", "dummy")
    monkeypatch.setattr(cs.settings, "CLOUD_EMBED_BASE_URL", "https://x")
    monkeypatch.setattr(cs.settings, "CLOUD_LLM_BASE_URL", "https://y")
    assert isinstance(get_embed_client(), CloudEmbedClient)


def test_ollama_batch_split(monkeypatch):
    c = OllamaEmbedClient(batch_size=2)
    calls = []

    def fake_post(texts):
        calls.append(list(texts))
        return {"embeddings": [[0.1] * 4 for _ in texts]}

    monkeypatch.setattr(c, "_post", fake_post)
    out = c.embed(["a", "b", "c"])
    assert len(out) == 3
    assert len(calls) == 2          # 2 + 1
    assert calls[0] == ["a", "b"] and calls[1] == ["c"]


def test_ollama_dim():
    assert OllamaEmbedClient().dim == cs.settings.EMBED_DIM


def test_cloud_batch_cap():
    c = CloudEmbedClient(base_url="https://x", api_key="x", batch_size=50, timeout=5)
    assert c.batch_size == 20        # 百炼单次批量上限 20


def test_cloud_embed_count_check(monkeypatch):
    c = CloudEmbedClient(base_url="https://x", api_key="x", batch_size=20, timeout=5)
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
    with patch("vector.cloud_embed.requests.post", return_value=fake):
        out = c.embed(["t1", "t2"])
    assert out == [[0.1, 0.2], [0.3, 0.4]]
