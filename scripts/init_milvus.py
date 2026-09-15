"""Milvus Lite 向量库初始化脚本（单文件嵌入式，无需起服务/Docker）。

从双源 MySQL 语料（流式分页）-> 嵌入（本地 Ollama bge-m3 / 云端 阿里百炼 qwen，按 EMBED_PROVIDER 切换）
-> 写入 Milvus Lite 单文件（默认 ./data/milvus_lite.db，milvus-lite 自动进入 lite 模式）。

运行（项目根，需先 pip install pymilvus milvus-lite）：
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py --benchmark
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py --reset
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py --limit 200
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py --incremental
  PYTHONPATH=. .venv/Scripts/python.exe scripts/init_milvus.py --concurrency 8

说明：
  - 集合名由 EMBED_PROVIDER 决定：ollama->'movies'，cloud->'movies_cloud'（隔离混维）。
  - 已改为流式分页建库（见 scripts/common_build.stream_build）：每批只拉 page_size 行、分批嵌入分批落库，
    500k 全量也不会一次拉爆内存；--incremental 跳过已索引 id，重跑只补新数据。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from vector.backend import MilvusBackend
from vector.embed_client import get_embed_client
from scripts.common_build import stream_build


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", action="store_true", help="建库后额外测量一次查询延迟")
    ap.add_argument("--reset", action="store_true", help="先删除集合再重建")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    ap.add_argument("--page-size", type=int, default=settings.BUILD_PAGE_SIZE,
                    help="每批从 MySQL 拉取的行数（默认 5000）")
    ap.add_argument("--incremental", action="store_true", help="增量续建：跳过已索引 id")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="嵌入并发线程数（cloud 建议 4~8）")
    args = ap.parse_args()

    backend = MilvusBackend()
    stream_build(
        backend,
        page_size=args.page_size,
        limit=args.limit,
        reset=args.reset,
        incremental=args.incremental,
        concurrency=args.concurrency,
    )

    if args.benchmark:
        client = get_embed_client()
        q = client.embed_query("科幻 太空 孤独")
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
