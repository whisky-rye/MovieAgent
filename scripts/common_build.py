"""建库公共流程：流式分页构建语料 -> 分批嵌入 -> 分批落库。

被 scripts/build_vectors.py、init_qdrant.py、init_milvus.py 共用，统一以下能力，
避免三套脚本各写一遍且行为漂移：

  - 流式分页（iter_corpus）：每批只从 MySQL 拉 page_size 行，内存恒定，500k 全量也不会 OOM。
  - 分批嵌入+分批 upsert：单批向量不长期驻留内存；对 Chroma 也按页提交，避免一次性 upsert 巨量数据。
  - 增量续建（--incremental）：跳过集合里已存在的 id，重跑只补新数据/改动的文档，不再全量重嵌入。
  - 云端并发嵌入（--concurrency）：cloud 嵌入是网络 IO 密集，多线程可大幅提速；本地 Ollama 单线程即可。
  - --reset 清空集合后重建；--limit 只处理前 N 条（快速验证/基准）。

不再使用旧的全局 embed_cache.pkl：500k 时该 pickle 高达 ~2GB，反而拖垮内存与加载；
增量续建（幂等 upsert + exists 跳过）已能覆盖「重跑不重复嵌入」的需求。
"""
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from config.settings import settings
from etl.build_corpus import iter_corpus
from vector.embed_client import get_embed_client


def _embed_parallel(client, docs: list[str], concurrency: int) -> list[list[float]]:
    """批量嵌入；concurrency>1 时按片切分给线程池并行（云端场景收益大）。"""
    if not docs:
        return []
    if concurrency <= 1 or len(docs) == 1:
        return client.embed(docs)
    n = len(docs)
    shard = max(1, math.ceil(n / concurrency))
    shards = [docs[i:i + shard] for i in range(0, n, shard)]
    results = [None] * len(shards)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(client.embed, s): i for i, s in enumerate(shards)}
        for f in as_completed(futs):
            results[futs[f]] = f.result()
    merged: list[list[float]] = []
    for r in results:
        merged.extend(r)
    return merged


def stream_build(
    backend,
    *,
    page_size: int | None = None,
    limit: int = 0,
    reset: bool = False,
    incremental: bool = False,
    concurrency: int | None = None,
    verbose: bool = True,
) -> int:
    """流式建库主流程。返回本次实际新写入的向量条数。

    backend      : VectorBackend 实例（Chroma/Qdrant/Milvus 均可，接口一致）。
    page_size    : 每页从 MySQL 拉取的行数；None 取 settings.BUILD_PAGE_SIZE。
    limit        : 只处理语料前 N 条（0=全部）。
    reset        : 先清空集合再重建（相当于全量重刷）。
    incremental  : 跳过集合里已存在的 id（增量续建，不重复嵌入）。
    concurrency  : 嵌入并发线程数；None 取 settings.EMBED_CONCURRENCY。
    """
    page_size = page_size or settings.BUILD_PAGE_SIZE
    if concurrency is None:
        concurrency = settings.EMBED_CONCURRENCY
    client = get_embed_client()

    if reset:
        if verbose:
            print(f"    --reset：清空并重建集合 {backend.collection}")
        backend.reset_collection()

    processed = 0
    written = 0
    t0 = time.time()
    if verbose:
        print(f"[1/3] 开始建库：backend={settings.VECTOR_BACKEND}, embed={client.model}, "
              f"page_size={page_size}, concurrency={concurrency}, "
              f"incremental={incremental}, limit={limit or '全部'}")

    for page in iter_corpus(page_size=page_size):
        if limit and processed + len(page) > limit:
            page = page[: max(0, limit - processed)]
        if not page:
            break

        page_len = len(page)  # 本页从语料检出的条数（含将被增量跳过的）
        ids = [r["id"] for r in page]
        if incremental:
            existing = backend.exists(ids)
            if existing:
                page = [r for r in page if r["id"] not in existing]
                ids = [r["id"] for r in page]

        processed += page_len  # 已检出的语料条数（无论是否跳过），--limit 以语料量为准
        if not ids:
            if verbose:
                print(f"    本页 {page_len} 条已存在，跳过（累计已处理 {processed}）")
            if limit and processed >= limit:
                break
            continue

        docs = [r["doc"] for r in page]
        metas = [r["meta"] for r in page]
        vectors = _embed_parallel(client, docs, concurrency)
        backend.upsert(ids, docs, metas, vectors)
        written += len(ids)

        if verbose:
            print(f"    已处理 {processed} 条，本批写入 {len(ids)}，累计落库 {written}，"
                  f"用时 {time.time() - t0:.1f}s")
        if limit and processed >= limit:
            break

    if verbose:
        print(f"[3/3] 建库完成：本次写入 {written} 条，集合当前总数 = {backend.count()}，"
              f"总耗时 {time.time() - t0:.1f}s")
    return written
