"""P2-2 回归：向量库客户端进程级单例（按 backend+collection）。

修复前 content_recall 每请求 `get_vector_backend()` 每次 new；嵌入式模式下
反复开同一存储路径有对象 churn / 元数据往返 / 与 semcache 单例（同 path）的
文件锁竞争风险。修复后按 (backend, collection) 单例复用（配置变更自动重建）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import vector.backend as vb_mod
from config.settings import settings


class _FakeBackend:
    def __init__(self, collection):
        self.collection = collection

    def count(self):
        return 0


@pytest.fixture
def fake_backends(monkeypatch):
    """把三个后端类换成计数假实现，并清空单例表。"""
    built = {"chroma": 0, "qdrant": 0, "milvus": 0}

    def _make(kind):
        def _ctor(collection=None):
            built[kind] += 1
            return _FakeBackend(collection)
        return _ctor

    monkeypatch.setattr(vb_mod, "ChromaBackend", _make("chroma"))
    monkeypatch.setattr(vb_mod, "QdrantBackend", _make("qdrant"))
    monkeypatch.setattr(vb_mod, "MilvusBackend", _make("milvus"))
    vb_mod._reset_vector_backends()
    yield built
    vb_mod._reset_vector_backends()


def test_same_collection_reuses_instance(fake_backends, monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "chroma")
    a = vb_mod.get_vector_backend()
    b = vb_mod.get_vector_backend()
    assert a is b, "同 (backend, collection) 应复用同一实例"
    assert fake_backends["chroma"] == 1, "不应重复构造"


def test_different_collection_different_instance(fake_backends, monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "chroma")
    a = vb_mod.get_vector_backend()                 # 默认影片集合
    b = vb_mod.get_vector_backend("query_cache")    # 语义缓存集合
    assert a is not b
    assert fake_backends["chroma"] == 2
    assert b.collection == "query_cache"


def test_backend_switch_rebuilds(fake_backends, monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "chroma")
    a = vb_mod.get_vector_backend()
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "qdrant")
    b = vb_mod.get_vector_backend()
    assert a is not b
    assert fake_backends["qdrant"] == 1


def test_concurrent_first_use_single_construction(fake_backends, monkeypatch):
    """并发首次取用：锁保护下只构造一次（双检锁定）。"""
    import threading

    monkeypatch.setattr(settings, "VECTOR_BACKEND", "chroma")
    barrier = threading.Barrier(8)
    got = []

    def _worker():
        barrier.wait()
        got.append(vb_mod.get_vector_backend())

    ts = [threading.Thread(target=_worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len({id(g) for g in got}) == 1
    assert fake_backends["chroma"] == 1


def test_unknown_backend_still_raises(fake_backends, monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "unknown-db")
    with pytest.raises(ValueError):
        vb_mod.get_vector_backend()


def test_reset_clears_singletons(fake_backends, monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "chroma")
    vb_mod.get_vector_backend()
    vb_mod._reset_vector_backends()
    vb_mod.get_vector_backend()
    assert fake_backends["chroma"] == 2
