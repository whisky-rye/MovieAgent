"""向量库抽象层 + 多后端实现（Chroma / Qdrant / Milvus Lite 本地嵌入式）。

设计目标：业务代码（建库/召回）只依赖 VectorBackend 抽象接口，
上线期把 VECTOR_BACKEND 从 chroma 改成 qdrant / milvus、安装对应客户端、填路径，
即可零代码改动切换存储（见 整体架构与技术选型方案.md 第 2.2 / 第 8 节）。

开发期：三者均本地嵌入式、无服务、无 Docker（Chroma PersistentClient / Qdrant 本地 path /
Milvus Lite 单文件）。生产再切集群版。

关于 list-metadata 过滤的坑：
  - Chroma 1.5.9 对 list-valued metadata 的 where 等值过滤不生效，类型这类 list 字段
    统一在召回后用 meta['genres_list'] 做应用层包含过滤（query 内部先按标量 where 召回
    top_k*8 候选，再过滤）。
  - Qdrant 原生支持 list payload 的 MatchAny 过滤，可直接在 query 内完成。
  - Milvus 动态字段对 array 过滤支持不稳，统一在应用层做 genres 包含过滤（与 Chroma 一致）。
各后端统一返回 (id, document, metadata, distance) 升序，distance 越低越相似（与 Chroma 一致）。
"""
import hashlib
from abc import ABC, abstractmethod

import chromadb
from config.settings import settings


# ---------- 集合命名：provider 感知，避免不同嵌入维度/分布互相污染 ----------
def default_collection() -> str:
    """本地 ollama(bge-m3) 沿用历史集合名 'movies'（已存 1134 条，向后兼容）；
    云端嵌入维度/分布不同，单独集合 'movies_cloud' 隔离，避免混维导致检索错乱。"""
    return "movies" if settings.EMBED_PROVIDER == "ollama" else "movies_cloud"


def _stable_uint64(key: str) -> int:
    """把字符串 id 稳定映射为 Qdrant 所需的 uint64 点 id（md5，避免 Python hash 随机化）。"""
    return int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % (2 ** 63 - 1)


class VectorBackend(ABC):
    @abstractmethod
    def upsert(self, ids, docs, metas, vectors): ...

    @abstractmethod
    def query(self, vector, top_k=10, where=None, genres=None):
        """向量相似检索。
        where: 标量 metadata 过滤（country/year/rating 等）。
        genres: list[str]，要求 meta['genres_list'] 含其一（应用层过滤，绕开 list where 坑）。
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
        self.name = collection  # 集合名（字符串）；注意 self.collection 是 Collection 对象，二者不同
        self.client = chromadb.PersistentClient(path=self.path)
        self.collection = self.client.get_or_create_collection(
            name=collection, metadata={"hnsw:space": "cosine"}
        )

    def upsert(self, ids, docs, metas, vectors):
        self.collection.upsert(
            ids=ids, documents=docs, metadatas=metas, embeddings=vectors
        )

    def reset_collection(self):
        """清空并重建集合（Chroma 默认无 reset_collection，建库脚本 --reset 用）。"""
        try:
            self.client.delete_collection(self.name)
        except Exception:
            pass
        self.collection = self.client.get_or_create_collection(
            name=self.name, metadata={"hnsw:space": "cosine"}
        )

    def exists(self, ids: list[str]) -> set:
        """返回已存在于集合中的 id 子集（供增量续建跳过已索引项）。"""
        if not ids:
            return set()
        res = self.collection.get(ids=ids, include=[])
        return set(res.get("ids") or [])

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
    """Qdrant 本地嵌入式（qdrant-client 本地直装，path 模式无需起服务/Docker）。

    生产可切集群：把 __init__ 的 QdrantClient(path=...) 换成 QdrantClient(host=, port=) 即可。
    """

    def __init__(self, path: str | None = None, collection: str | None = None,
                 dim: int | None = None):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams
        self.path = path or settings.QDRANT_PATH
        self.collection = collection or default_collection()
        self.dim = dim or settings.EMBED_DIM
        # 本地嵌入式：进程内、文件落盘，无网络端口
        self.client = QdrantClient(path=self.path)
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
            )

    def upsert(self, ids, docs, metas, vectors):
        from qdrant_client.models import PointStruct
        pts = []
        for id_, doc, meta, vec in zip(ids, docs, metas, vectors):
            payload = {k: v for k, v in meta.items() if v is not None}
            payload["_doc"] = doc
            payload["_id"] = id_  # 还原原始字符串 id（Qdrant 点 id 为 uint64 哈希）
            pts.append(PointStruct(id=_stable_uint64(id_), vector=vec, payload=payload))
        # 分批写入，避免超大批次 OOM / 超时（每批 256）
        for i in range(0, len(pts), 256):
            self.client.upsert(self.collection, pts[i:i + 256])

    def query(self, vector, top_k=10, where=None, genres=None):
        from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny, Range
        recall = max(top_k * 8, 32)
        must = []
        if where:
            for k, v in where.items():
                if isinstance(v, dict):  # 范围：{$gte, $lte}
                    rng = {}
                    if "$gte" in v:
                        rng["gte"] = v["$gte"]
                    if "$lte" in v:
                        rng["lte"] = v["$lte"]
                    if rng:
                        must.append(FieldCondition(key=k, range=Range(**rng)))
                else:
                    must.append(FieldCondition(key=k, match=MatchValue(value=v)))
        if genres:
            must.append(FieldCondition(key="genres_list", match=MatchAny(any=genres)))
        filt = Filter(must=must) if must else None
        # 本地嵌入式 Qdrant 用 query_points API（非 server 端的 search）
        resp = self.client.query_points(
            self.collection, query=vector, limit=recall, query_filter=filt,
            with_payload=True,
        )
        # Qdrant cosine 返回 score=相似度（越高越相似），统一转 distance（越低越相似）
        rows = []
        for h in resp.points:
            pid = h.payload.get("_id", "")
            doc = h.payload.get("_doc", "")
            meta = {k: v for k, v in h.payload.items() if k not in ("_id", "_doc")}
            rows.append((pid, doc, meta, 1.0 - h.score))
        return rows[:top_k]

    def count(self) -> int:
        return self.client.count(self.collection).count

    def get_embeddings(self, ids: list[str]) -> list[list[float]]:
        pts = self.client.retrieve(
            self.collection, ids=[_stable_uint64(i) for i in ids], with_vectors=True
        )
        return [p.vector for p in pts]

    def reset_collection(self):
        """清空并重建集合（同一 client 实例，避免嵌入式模式多实例锁冲突）。"""
        from qdrant_client.models import Distance, VectorParams
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )

    def exists(self, ids: list[str]) -> set:
        """返回已存在于集合中的 id 子集（供增量续建跳过已索引项）。"""
        if not ids:
            return set()
        out = set()
        for i in range(0, len(ids), 256):
            batch = ids[i:i + 256]
            pts = self.client.retrieve(
                self.collection,
                ids=[_stable_uint64(x) for x in batch],
                with_vectors=False,
            )
            for p in pts:
                pid = (p.payload or {}).get("_id")
                if pid:
                    out.add(pid)
        return out


class MilvusBackend(VectorBackend):
    """Milvus Lite 单文件嵌入式（milvus-lite + pymilvus，无需起服务/Docker）。

    uri 指向本地 .db 文件（MilvusClient 在 milvus-lite 安装时自动进入 lite 模式）。
    生产可切集群：把 uri 换成 "http://host:19530" 即可（同一 MilvusClient 接口）。
    """

    def __init__(self, uri: str | None = None, collection: str | None = None,
                 dim: int | None = None):
        from pymilvus import MilvusClient, DataType, FieldSchema, CollectionSchema
        self.uri = uri or settings.MILVUS_PATH
        self.collection = collection or default_collection()
        self.dim = dim or settings.EMBED_DIM
        self.client = MilvusClient(uri=self.uri)
        if not self.client.has_collection(self.collection):
            schema = CollectionSchema(
                fields=[
                    FieldSchema(name="pk", dtype=DataType.VARCHAR,
                                is_primary=True, max_length=256),
                    FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.dim),
                ],
                enable_dynamic_field=True,  # 其余 meta 字段以动态字段存储
            )
            self.client.create_collection(
                self.collection, schema=schema, metric_type="COSINE"
            )

    def upsert(self, ids, docs, metas, vectors):
        data = []
        for id_, doc, meta, vec in zip(ids, docs, metas, vectors):
            row = {k: v for k, v in meta.items() if v is not None}
            row["pk"] = id_
            row["vector"] = vec
            row["_doc"] = doc
            data.append(row)
        for i in range(0, len(data), 256):
            self.client.insert(self.collection, data[i:i + 256], progress_bar=False)
        self.client.flush(self.collection)

    @staticmethod
    def _build_expr(where: dict | None) -> str | None:
        if not where:
            return None
        parts = []
        for k, v in where.items():
            if isinstance(v, dict):  # 范围
                if "$gte" in v:
                    parts.append(f"{k} >= {v['$gte']}")
                if "$lte" in v:
                    parts.append(f"{k} <= {v['$lte']}")
            else:
                if isinstance(v, str):
                    parts.append(f'{k} == "{v}"')
                else:
                    parts.append(f"{k} == {v}")
        return " and ".join(parts) if parts else None

    def query(self, vector, top_k=10, where=None, genres=None):
        recall = max(top_k * 8, 32)
        expr = self._build_expr(where)
        res = self.client.search(
            self.collection, data=[vector], limit=recall, filter=expr,
            output_fields=["*"],
        )
        rows = []
        for hit in res[0]:
            ent = hit["entity"]
            pid = ent.pop("pk", "")
            doc = ent.pop("_doc", "")
            ent.pop("vector", None)
            # Milvus COSINE 返回 distance=1-相似度（越低越相似）；统一转 distance 升序
            dist = 1.0 - hit["distance"]
            rows.append((pid, doc, ent, dist))
        if genres:
            want = set(genres)
            rows = [r for r in rows if want & set(r[2].get("genres_list", []) or [])]
        return rows[:top_k]

    def count(self) -> int:
        return self.client.get_collection_stats(self.collection)["row_count"]

    def get_embeddings(self, ids: list[str]) -> list[list[float]]:
        res = self.client.get(self.collection, ids=ids, with_vector=True)
        return [r["vector"] for r in res]

    def reset_collection(self):
        """清空并重建集合（同一 client 实例，避免嵌入式模式多实例锁冲突）。"""
        from pymilvus import DataType, FieldSchema, CollectionSchema
        if self.client.has_collection(self.collection):
            self.client.drop_collection(self.collection)
        schema = CollectionSchema(
            fields=[
                FieldSchema(name="pk", dtype=DataType.VARCHAR,
                            is_primary=True, max_length=256),
                FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.dim),
            ],
            enable_dynamic_field=True,
        )
        self.client.create_collection(
            self.collection, schema=schema, metric_type="COSINE"
        )

    def exists(self, ids: list[str]) -> set:
        """返回已存在于集合中的 id 子集（供增量续建跳过已索引项）。"""
        if not ids:
            return set()
        out = set()
        for i in range(0, len(ids), 256):
            batch = ids[i:i + 256]
            res = self.client.get(self.collection, ids=batch, with_vector=False)
            for e in res or []:
                pk = (e or {}).get("pk")
                if pk:
                    out.add(pk)
        return out


def get_vector_backend() -> VectorBackend:
    """工厂：按 settings.VECTOR_BACKEND 选择实现（配置切换，业务零改动）。"""
    if settings.VECTOR_BACKEND == "chroma":
        return ChromaBackend()
    if settings.VECTOR_BACKEND == "qdrant":
        return QdrantBackend()
    if settings.VECTOR_BACKEND == "milvus":
        return MilvusBackend()
    raise ValueError(f"未知 VECTOR_BACKEND: {settings.VECTOR_BACKEND}")
