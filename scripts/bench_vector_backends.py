"""向量后端 A/B 压测 harness（Chroma / Qdrant / Milvus Lite）。

⚠️ 执行前提：语料全量灌入（O1）后方可跑出有意义的 A/B 结论。
   当前 dev 索引仅 1134 条，在此规模下三后端均个位数 ms、召回一致，
   跑 A/B 会得到「无差别」的假阴性结论，故正式压测推迟到 O1 之后。

设计要点：
- 全程使用独立集合名 `bench_ab`，绝不触碰真实 `movies` 集合与既有索引。
- smoke 模式用合成向量 + 临时目录，仅验证后端代码路径可 ingest+query，
  不产出任何 A/B 对比/延迟结论（避免误导）。
- 四项维度：① 单查询延迟分布(p50/p95/p99) ② 并发吞吐 QPS ③ 灌入吞吐
  ④ recall@k 质量对齐（以暴力余弦为 ground truth）。
- 实时嵌入：--embed-live 时查询向量经 ollama bge-m3 实时计算；同时拆分
  DB-only 计时，保证后端差异不被 embedding 延迟完全淹没。
- 仅本地向量库，不连生产 MySQL / 不触红线。

用法：
  # 仅验证后端代码路径（无 O1 也可跑，安全）
  python scripts/bench_vector_backends.py --smoke

  # O1 全量后正式压测（需 --data 预计算语料）
  python scripts/bench_vector_backends.py \
      --backends chroma,qdrant,milvus \
      --data data/corpus_embeddings.npz \
      --topk 10 --queries 100 --concurrency 8 \
      --embed-live --out data/vector_bench_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

# 独立集合名：永远不碰真实 'movies' 集合
BENCH_COLLECTION = "bench_ab"


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def load_embed_client():
    from vector.embed_client import get_embed_client

    return get_embed_client()


def make_synthetic(n: int, dim: int):
    """合成语料（仅 smoke / 调参用，分布不真实）。"""
    rng = np.random.default_rng(42)
    ids = [f"syn_{i}" for i in range(n)]
    docs = [f"synthetic movie {i}" for i in range(n)]
    metas = [
        {"genres_list": ["action"], "year": 2000, "rating": 7.0, "country": "US"}
        for _ in range(n)
    ]
    vectors = rng.standard_normal((n, dim)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    # 查询用合成随机向量（无真实文本，smoke 不测实时嵌入）
    queries = [rng.standard_normal(dim).astype(np.float32) for _ in range(min(20, n))]
    return ids, docs, metas, vectors, queries


def load_corpus(npz_path: str):
    """加载 O1 产出的预计算语料（npz）：
    ids(str数组) / vectors(f2) / docs / metas(json) / query_texts / gold(json: q->rel_ids)。
    """
    d = np.load(npz_path, allow_pickle=True)
    ids = d["ids"].tolist()
    vectors = d["vectors"].astype(np.float32)
    docs = d["docs"].tolist() if "docs" in d else [f"doc {i}" for i in range(len(ids))]
    metas = json.loads(d["metas"].item()) if "metas" in d else [{} for _ in ids]
    query_texts = d["query_texts"].tolist() if "query_texts" in d else []
    gold = json.loads(d["gold"].item()) if "gold" in d else {}
    return ids, docs, metas, vectors, query_texts, gold


def _brute_force_topk(vectors: np.ndarray, q: np.ndarray, k: int) -> set[str]:
    sims = vectors @ q
    idx = np.argsort(-sims)[:k]
    return set(idx.tolist())


def bench_backend(backend, ids, docs, metas, vectors, queries, args, emb=None):
    """对单个后端跑四项维度，返回指标字典。"""
    dim = vectors.shape[1]
    n = len(ids)

    # ---- ① 灌入吞吐 ----
    t0 = time.perf_counter()
    backend.reset_collection()
    backend.upsert(ids, docs, metas, vectors)
    ingest_sec = time.perf_counter() - t0
    ingest_qps = n / ingest_sec if ingest_sec > 0 else float("inf")

    # 预构建向量矩阵供 recall 暴力基线
    vec_mat = vectors

    # 准备查询向量（实时嵌入 or 预计算）
    if args.embed_live and emb is not None and queries and isinstance(queries[0], str):
        qvecs = [np.asarray(emb.embed_query(q), dtype=np.float32) for q in queries]
        live_embed = True
    else:
        qvecs = [np.asarray(q, dtype=np.float32) for q in queries]
        live_embed = False

    # ---- ② 单查询延迟分布 ----
    total_ts, db_ts, embed_ts = [], [], []
    for qv in qvecs:
        if live_embed:
            te = time.perf_counter()
            qv = np.asarray(emb.embed_query(queries[qvecs.index(qv)]), dtype=np.float32) \
                if False else qv  # 已在上面算好，占位避免重复
        t_s = time.perf_counter()
        backend.query(qv, top_k=args.topk)
        db_ts.append(time.perf_counter() - t_s)
    # 实时嵌入单独计时（每条）
    if live_embed:
        for q in queries:
            t_e = time.perf_counter()
            emb.embed_query(q)
            embed_ts.append(time.perf_counter() - t_e)
    total_ts = [db + (e if live_embed else 0) for db, e in zip(db_ts, embed_ts)] if live_embed \
        else list(db_ts)

    # ---- ③ 并发吞吐 QPS ----
    def _one():
        qv = qvecs[int(time.perf_counter() * 1e6) % len(qvecs)]
        if live_embed:
            _ = emb.embed_query(queries[qvecs.index(qv)]) if False else None
        backend.query(qv, top_k=args.topk)

    total_q = max(1, args.concurrency) * 20
    t_c = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        list(ex.map(lambda _: _one(), range(total_q)))
    conc_sec = time.perf_counter() - t_c
    conc_qps = total_q / conc_sec if conc_sec > 0 else float("inf")

    # ---- ④ recall@k 质量对齐（暴力余弦为 ground truth）----
    recall_vals = []
    if args.gold and len(qvecs) >= 1:
        for q, rel in args.gold.items():
            qi = queries.index(q) if q in queries else -1
            if qi < 0:
                continue
            gt = _brute_force_topk(vec_mat, qvecs[qi], args.topk)
            got = backend.query(qvecs[qi], top_k=args.topk)
            got_ids = set(str(r[0]) for r in got)
            hit = len(gt & got_ids & set(rel)) if rel else len(gt & got_ids)
            denom = min(args.topk, len(rel)) if rel else args.topk
            recall_vals.append(hit / denom if denom else 0.0)

    return {
        "count": backend.count(),
        "ingest_rows_per_sec": round(ingest_qps, 1),
        "ingest_sec": round(ingest_sec, 3),
        "latency_total_ms": {
            "p50": round(_pct(total_ts, 50) * 1000, 3),
            "p95": round(_pct(total_ts, 95) * 1000, 3),
            "p99": round(_pct(total_ts, 99) * 1000, 3),
            "mean": round(statistics.mean(total_ts) * 1000, 3) if total_ts else 0,
        },
        "latency_db_only_ms": {
            "p50": round(_pct(db_ts, 50) * 1000, 3),
            "p95": round(_pct(db_ts, 95) * 1000, 3),
            "p99": round(_pct(db_ts, 99) * 1000, 3),
        },
        "latency_embed_ms": {
            "p50": round(_pct(embed_ts, 50) * 1000, 3),
            "p95": round(_pct(embed_ts, 95) * 1000, 3),
        } if embed_ts else None,
        "concurrent_qps": round(conc_qps, 1),
        "recall_at_k": round(statistics.mean(recall_vals), 4) if recall_vals else None,
    }


def smoke(backends: list[str], n: int, dim: int):
    """仅验证后端代码路径可 ingest+query+get_embeddings（不产出 A/B 结论）。"""
    ids, docs, metas, vectors, queries = make_synthetic(n, dim)
    tmp = tempfile.mkdtemp(prefix="vec_bench_smoke_")
    print(f"[smoke] 合成 {n} 条 dim={dim}，临时目录 {tmp}")
    for name in backends:
        try:
            if name == "chroma":
                from vector.backend import ChromaBackend

                b = ChromaBackend(path=os.path.join(tmp, "chroma"), collection=BENCH_COLLECTION)
            elif name == "qdrant":
                from vector.backend import QdrantBackend

                b = QdrantBackend(path=os.path.join(tmp, "qdrant"), collection=BENCH_COLLECTION, dim=dim)
            elif name == "milvus":
                from vector.backend import MilvusBackend

                b = MilvusBackend(uri=os.path.join(tmp, "milvus.db"), collection=BENCH_COLLECTION, dim=dim)
            else:
                print(f"[smoke] 跳过未知后端 {name}")
                continue
            b.reset_collection()
            b.upsert(ids, docs, metas, vectors)
            cnt = b.count()
            hits = b.query(vectors[0], top_k=5)
            emb = b.get_embeddings([ids[0]])
            ok = cnt == n and len(hits) == 5 and len(emb) == 1
            print(f"[smoke] {name:8s} OK  count={cnt} query_hits={len(hits)} "
                  f"get_embeddings={len(emb)} -> {'PASS' if ok else 'FAIL'}")
        except Exception as e:
            print(f"[smoke] {name:8s} ERROR -> {type(e).__name__}: {str(e)[:160]}")


def main():
    ap = argparse.ArgumentParser(description="向量后端 A/B 压测（O1 全量后执行）")
    ap.add_argument("--smoke", action="store_true", help="合成向量验证后端代码路径（安全，无 A/B 结论）")
    ap.add_argument("--backends", default="chroma,qdrant,milvus")
    ap.add_argument("--data", help="O1 产出的预计算语料 npz 路径（正式压测必需）")
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--synthetic", type=int, default=200, help="smoke 合成条数")
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--embed-live", action="store_true", help="查询向量经 ollama bge-m3 实时计算")
    ap.add_argument("--out", default="data/vector_bench_report.json")
    args = ap.parse_args()

    backends = [x.strip() for x in args.backends.split(",") if x.strip()]

    if args.smoke:
        smoke(backends, args.synthetic, args.dim)
        return

    if not args.data:
        ap.error("正式压测需 --data（O1 全量语料预计算 npz）；当前仅 1134 条，跑 A/B 无意义。")

    ids, docs, metas, vectors, query_texts, gold = load_corpus(args.data)
    emb = load_embed_client() if args.embed_live else None
    # 取前 --queries 条作为查询集（实时嵌入用文本，否则用向量）
    if args.embed_live:
        queries = query_texts[: args.queries]
    else:
        queries = vectors[: args.queries]

    report = {"backends": {}, "config": vars(args)}
    for name in backends:
        if name == "chroma":
            from vector.backend import ChromaBackend

            b = ChromaBackend(collection=BENCH_COLLECTION)
        elif name == "qdrant":
            from vector.backend import QdrantBackend

            b = QdrantBackend(collection=BENCH_COLLECTION)
        elif name == "milvus":
            from vector.backend import MilvusBackend

            b = MilvusBackend(collection=BENCH_COLLECTION)
        else:
            continue
        print(f"[bench] 后端 {name} 开始 ...")
        report["backends"][name] = bench_backend(b, ids, docs, metas, vectors, queries, args, emb)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[bench] 报告已写 {args.out}")
    print(json.dumps(report["backends"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
