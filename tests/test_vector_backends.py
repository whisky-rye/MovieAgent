"""向量库多后端（vector/backend.py）单测。

验证抽象层在三种本地嵌入式后端（Chroma / Qdrant 本地 path / Milvus Lite 单文件）
下的统一行为：upsert / count / query(最近邻在前、distance 升序) / get_embeddings /
exists。均使用临时目录，不依赖 MySQL / Ollama / 网络。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vector.backend import ChromaBackend, MilvusBackend, QdrantBackend

DIM = 4


def _vecs(n):
    return [[float((i * 7 + j) % 5) for j in range(DIM)] for i in range(n)]


def _ids(n):
    return [f"douban:{i}" for i in range(n)]


def _docs(n):
    return [f"doc{i}" for i in range(n)]


def _metas(n):
    return [{
        "source": "douban", "title": f"t{i}", "year": 2000 + i,
        "genres_list": ["剧情"], "country": "中国", "rating": 8.0,
        "rating_count": 100, "director": "d",
    } for i in range(n)]


def _exercise(b):
    ids, docs, metas, vecs = _ids(5), _docs(5), _metas(5), _vecs(5)
    b.upsert(ids, docs, metas, vecs)
    assert b.count() == 5
    rows = b.query(vecs[0], top_k=3)
    assert len(rows) <= 3
    assert rows[0][0] == ids[0]        # 最近邻是自身
    assert abs(rows[0][3]) < 1e-4       # 自身 distance ≈ 0
    embs = b.get_embeddings([ids[0]])
    assert len(embs) == 1 and len(embs[0]) == DIM
    assert b.exists(ids) == set(ids)
    assert b.exists(["nope:999"]) == set()


def test_chroma(tmp_path):
    b = ChromaBackend(path=str(tmp_path / "chroma"), collection="test_movies")
    _exercise(b)


def test_qdrant(tmp_path):
    b = QdrantBackend(path=str(tmp_path / "qdrant"), collection="test_movies", dim=DIM)
    _exercise(b)


def test_milvus(tmp_path):
    b = MilvusBackend(uri=str(tmp_path / "milvus.db"), collection="test_movies", dim=DIM)
    _exercise(b)
