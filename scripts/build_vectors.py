"""P1 建库脚本：双源语料 -> 本地 Ollama bge-m3 嵌入 -> 写入 Chroma（纯本地）。

运行（项目根）：
  PYTHONPATH=. .venv/Scripts/python.exe scripts/build_vectors.py

耗时：~1134 条样本约 3 分钟（纯 CPU）；全量 55 万约 23.5 小时，可分批/夜间/增量。
向量结果缓存到 ./data/embed_cache.pkl，调参重试时跳过重新嵌入（模型/条数一致才命中）。
"""
import os
import pickle
import time
from vector.embed_client import OllamaEmbedClient
from vector.backend import ChromaBackend
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

    client = OllamaEmbedClient()
    vectors = None
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "rb") as f:
            cached = pickle.load(f)
        if cached.get("n") == len(docs) and cached.get("model") == client.model:
            vectors = cached["vectors"]
            print(f"[2/3] 命中本地向量缓存，跳过嵌入（{len(vectors)} 向量）")
    if vectors is None:
        t_emb = time.time()
        print(f"[2/3] 调用 Ollama 嵌入（模型={client.model}, 批次={client.batch_size}）...")
        vectors = client.embed(docs)
        os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
        with open(CACHE_PATH, "wb") as f:
            pickle.dump({"n": len(docs), "model": client.model, "vectors": vectors}, f)
        print(f"      嵌入完成：{len(vectors)} 向量, dim={len(vectors[0])}, "
              f"用时 {time.time() - t_emb:.1f}s")

    backend = ChromaBackend()
    backend.upsert(ids, docs, metas, vectors)
    print(f"[3/3] 落库完成：Chroma 当前条数 = {backend.count()} | 路径 = {settings.CHROMA_PATH}")
    print(f"总耗时 {time.time() - t0:.1f}s。下一步运行 scripts/probe_recall.py 验证召回。")


if __name__ == "__main__":
    main()
