"""P1 建库脚本：双源语料 -> 嵌入（本地 Ollama bge-m3 / 云端 阿里百炼 qwen，按 EMBED_PROVIDER 切换） -> 写入向量库（按 VECTOR_BACKEND 切换）。

运行（项目根）：
  PYTHONPATH=. .venv/Scripts/python.exe scripts/build_vectors.py
  # 指定后端：VECTOR_BACKEND=qdrant PYTHONPATH=. .venv/Scripts/python.exe scripts/build_vectors.py
  # 指定嵌入源：EMBED_PROVIDER=cloud PYTHONPATH=. .venv/Scripts/python.exe scripts/build_vectors.py

耗时：~1134 条样本纯 CPU 嵌入约 3 分钟；全量 55 万约 23.5 小时（可分批/夜间/增量）。
向量结果缓存到 ./data/embed_cache.pkl，调参重试时跳过重新嵌入（模型/条数一致才命中）。
"""
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vector.embed_client import get_embed_client
from vector.backend import get_vector_backend
from etl.build_corpus import build_corpus
from config.settings import settings

CACHE_PATH = "./data/embed_cache.pkl"


def main():
    t0 = time.time()
    recs = build_corpus()
    print(f"[1/3] 语料构建完成：{len(recs)} 条 "
          f"（douban={sum(1 for r in recs if r['source']=='douban')}, "
          f"imdb={sum(1 for r in recs if r['source']=='imdb')}）")

    ids = [r["id"] for r in recs]
    docs = [r["doc"] for r in recs]
    metas = [r["meta"] for r in recs]

    client = get_embed_client()
    vectors = None
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "rb") as f:
            cached = pickle.load(f)
        if cached.get("n") == len(docs) and cached.get("model") == client.model:
            vectors = cached["vectors"]
            print(f"[2/3] 命中本地向量缓存，跳过嵌入（{len(vectors)} 向量）")
    if vectors is None:
        t_emb = time.time()
        print(f"[2/3] 调用嵌入（provider={settings.EMBED_PROVIDER}, 模型={client.model}, "
              f"批次={client.batch_size}）...")
        vectors = client.embed(docs)
        os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
        with open(CACHE_PATH, "wb") as f:
            pickle.dump({"n": len(docs), "model": client.model, "vectors": vectors}, f)
        print(f"      嵌入完成：{len(vectors)} 向量, dim={len(vectors[0])}, "
              f"用时 {time.time() - t_emb:.1f}s")

    backend = get_vector_backend()
    backend.upsert(ids, docs, metas, vectors)
    print(f"[3/3] 落库完成：{settings.VECTOR_BACKEND} 当前条数 = {backend.count()}")
    print(f"总耗时 {time.time() - t0:.1f}s。下一步运行 scripts/probe_recall.py 验证召回。")


if __name__ == "__main__":
    main()
