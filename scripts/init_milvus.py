"""Milvus Lite 向量库初始化脚本（单文件嵌入式，无需起服务/Docker）。

从双源 MySQL 语料 -> 嵌入（本地 Ollama bge-m3 / 云端 阿里百炼 qwen，按 EMBED_PROVIDER 切换）
-> 写入 Milvus Lite 单文件（默认 ./data/milvus_lite.db，milvus-lite 自动进入 lite 模式）。

运行（项目根，需先 pip install pymilvus milvus-lite）：
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py --benchmark
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py --reset
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py --limit 200

说明：
  - 向量缓存 ./data/embed_cache.pkl 命中则跳过嵌入。
  - Milvus 主键为字符串 pk，重复 insert 同 pk 会报重复键；--reset 先 drop 集合再建。
  - 集合名由 EMBED_PROVIDER 决定：ollama->'movies'，cloud->'movies_cloud'（隔离混维）。
"""
import argparse
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from etl.build_corpus import build_corpus
from vector.backend import MilvusBackend
from vector.embed_client import get_embed_client


CACHE_PATH = "./data/embed_cache.pkl"


def _load_or_embed(client, docs):
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "rb") as f:
            cached = pickle.load(f)
        if cached.get("n") == len(docs) and cached.get("model") == client.model:
            print(f"[2/3] 命中本地向量缓存，跳过嵌入（{len(cached['vectors'])} 向量）")
            return cached["vectors"]
    t_emb = time.time()
    print(f"[2/3] 嵌入中（provider={settings.EMBED_PROVIDER}, 模型={client.model}, "
          f"批次={client.batch_size}）...")
    vecs = client.embed(docs)
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    with open(CACHE_PATH, "wb") as f:
        pickle.dump({"n": len(docs), "model": client.model, "vectors": vecs}, f)
    print(f"      嵌入完成：{len(vecs)} 向量, dim={len(vecs[0])}, "
          f"用时 {time.time() - t_emb:.1f}s")
    return vecs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", action="store_true", help="建库后额外测量一次查询延迟")
    ap.add_argument("--reset", action="store_true", help="先删除集合再重建")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    args = ap.parse_args()

    t0 = time.time()
    recs = build_corpus()
    if args.limit:
        recs = recs[: args.limit]
    print(f"[1/3] 语料构建完成：{len(recs)} 条")

    ids = [r["id"] for r in recs]
    docs = [r["doc"] for r in recs]
    metas = [r["meta"] for r in recs]

    client = get_embed_client()
    vectors = _load_or_embed(client, docs)

    backend = MilvusBackend()
    if args.reset:
        print(f"     --reset：清空并重建集合 {backend.collection}")
        backend.reset_collection()

    t_up = time.time()
    backend.upsert(ids, docs, metas, vectors)
    dt = time.time() - t_up
    n = backend.count()
    print(f"[3/3] Milvus 落库完成：集合={backend.collection}, 当前条数={n}, "
          f"写入耗时={dt:.2f}s, 吞吐≈{len(ids) / dt:.1f} 向量/秒")
    print(f"总耗时 {time.time() - t0:.1f}s")

    if args.benchmark and docs:
        q = client.embed_query(docs[0][:50])
        tb = time.time()
        hits = backend.query(q, top_k=10)
        ql = time.time() - tb
        print(f"    查询延迟≈{ql * 1000:.1f}ms, 命中 {len(hits)} 条, "
              f"top1={hits[0][0] if hits else 'NA'}")

    try:
        backend.client.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
