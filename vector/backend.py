"""向量库抽象层 + Chroma 本地实现 + Qdrant 预留桩。

设计目标：业务代码（建库/召回）只依赖 VectorBackend 抽象接口，
上线期把 VECTOR_BACKEND=chroma 改成 qdrant、安装 qdrant-client 并填 QDRANT_PATH，
即可零代码改动切换到集群版（见 agent_architecture_and_stack.md 第 8 节）。

开发期：chromadb PersistentClient 进程内文件落盘，无需起服务、无 Docker。

关于 list-metadata 过滤的坑（已实测）：
  本机 chromadb==1.5.9 对 list-valued metadata 的 where 等值过滤（如 genres_list）不生效，
  而标量字段（country/year/rating/source）正常。因此类型这类 list 字段改在召回后用
  meta['genres_list'] 做应用层包含过滤（query 内部先按标量 where 召回 top_k*8 候选，再过滤）。
"""
from abc import ABC, abstractmethod
import chromadb
from config.settings import settings


class VectorBackend(ABC):
    @abstractmethod
    def upsert(self, ids, docs, metas, vectors): ...

    @abstractmethod
    def query(self, vector, top_k=10, where=None, genres=None):
        """向量相似检索。
        where: 标量 metadata 过滤（Chroma 原生，country/year/rating/source 等）。
        genres: list[str]，要求 meta['genres_list'] 含其一（应用层过滤，绕开 Chroma list where 坑）。
        返回 [(id, document, metadata, distance), ...]，按距离升序。
        """

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def get_embeddings(self, ids: list[str]) -> list[list[float]]:
        """按 id 取已存向量（供「以物品找相似」类召回）。"""


class ChromaBackend(VectorBackend):
    def __init__(self, path: str | None = None, collection: str = "movies"):
        self.path = path or settings.CHROMA_PATH
        self.client = chromadb.PersistentClient(path=self.path)
        self.collection = self.client.get_or_create_collection(
            name=collection, metadata={"hnsw:space": "cosine"}
        )

    def upsert(self, ids, docs, metas, vectors):
        self.collection.upsert(
            ids=ids, documents=docs, metadatas=metas, embeddings=vectors
        )

    def query(self, vector, top_k=10, where=None, genres=None):
        recall = max(top_k * 8, 32)  # 召回候选放大，供应用层 list 过滤后仍有足够 top_k
        r = self.collection.query(
            query_embeddings=[vector], n_results=recall, where=where
        )
        rows = list(zip(r["ids"][0], r["documents"][0], r["metadatas"][0], r["distances"][0]))
        if genres:
            want = set(genres)
            rows = [row for row in rows if want & set(row[2].get("genres_list", []) or [])]
        return rows[:top_k]

    def count(self) -> int:
        return self.collection.count()

    def get_embeddings(self, ids: list[str]) -> list[list[float]]:
        r = self.collection.get(ids=ids, include=["embeddings"])
        return r["embeddings"]


class QdrantBackend(VectorBackend):
    """预留桩：上线期实现（本地二进制 / 集群）。当前开发期未启用。"""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "QdrantBackend 为上线期预留；开发期请保持 VECTOR_BACKEND=chroma。"
            "启用时需 pip install qdrant-client 并本地直装/集群部署 Qdrant。"
        )

    def upsert(self, ids, docs, metas, vectors): raise NotImplementedError
    def query(self, vector, top_k=10, where=None, genres=None): raise NotImplementedError
    def count(self) -> int: raise NotImplementedError


def get_vector_backend() -> VectorBackend:
    """工厂：按 settings.VECTOR_BACKEND 选择实现（配置切换，业务零改动）。"""
    if settings.VECTOR_BACKEND == "chroma":
        return ChromaBackend()
    if settings.VECTOR_BACKEND == "qdrant":
        return QdrantBackend()
    raise ValueError(f"未知 VECTOR_BACKEND: {settings.VECTOR_BACKEND}")
