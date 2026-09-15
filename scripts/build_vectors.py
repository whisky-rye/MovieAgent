"""P1 建库脚本：双源语料（流式分页）-> 嵌入（本地 Ollama bge-m3 / 云端 阿里百炼 qwen）-> 写入向量库（按 VECTOR_BACKEND 切换）。

运行（项目根）：
  PYTHONPATH=. .venv/Scripts/python.exe scripts/build_vectors.py
  # 指定后端：VECTOR_BACKEND=qdrant PYTHONPATH=. .venv/Scripts/python.exe scripts/build_vectors.py
  # 指定嵌入源：EMBED_PROVIDER=cloud PYTHONPATH=. .venv/Scripts/python.exe scripts/build_vectors.py
  # 快速验证：--limit 200 --page-size 50
  # 全量重刷：--reset
  # 增量续建（只补新数据）：--incremental
  # 云端提速：--concurrency 8

对比旧版优化（应对 500k 全量「爆炸」）：
  - 流式分页：每批只从 MySQL 拉 page_size 行（默认 5000），内存恒定，不再一次性 SELECT .all() 拉爆；
  - 分批嵌入+分批 upsert：单批向量不长期驻留，Chroma 也按页提交，不再一次性 upsert 全量；
  - 增量续建：exists 跳过已索引 id，重跑只补新数据，不再全量重嵌入；
  - 云端并发嵌入：--concurrency 多线程，网络 IO 密集场景显著提速；
  - 去掉全局 embed_cache.pkl（500k 时 ~2GB，反成内存负担；增量续建已覆盖其用途）。

耗时参考：~1134 条样本纯 CPU 嵌入约 3 分钟；全量 55 万建议 --incremental 分批/夜间/并发跑。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from vector.backend import get_vector_backend
from scripts.common_build import stream_build


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page-size", type=int, default=settings.BUILD_PAGE_SIZE,
                    help="每批从 MySQL 拉取的行数（默认 settings.BUILD_PAGE_SIZE=5000）")
    ap.add_argument("--limit", type=int, default=0, help="只处理语料前 N 条（0=全部）")
    ap.add_argument("--reset", action="store_true", help="先清空集合再重建（全量重刷）")
    ap.add_argument("--incremental", action="store_true",
                    help="增量续建：跳过集合里已存在的 id，只补新数据")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="嵌入并发线程数（cloud 建议 4~8；默认 settings.EMBED_CONCURRENCY）")
    args = ap.parse_args()

    backend = get_vector_backend()
    stream_build(
        backend,
        page_size=args.page_size,
        limit=args.limit,
        reset=args.reset,
        incremental=args.incremental,
        concurrency=args.concurrency,
    )
    print("下一步运行 scripts/probe_recall.py 验证召回。")


if __name__ == "__main__":
    main()
