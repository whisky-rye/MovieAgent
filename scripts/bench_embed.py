"""本地 Ollama bge-m3 向量化耗时基准（调研用）。

模拟 P1 建库时的语料拼接：标题/原名/年份/类型/国家/导演 + 简介/剧情/标签（裁剪），
测量 embed_documents 真实耗时，并外推到全量规模，用于判断是否走云端 API。
"""
import time
import json
import urllib.request
from sqlalchemy import create_engine, text
from config.settings import settings

EMBED_URL = settings.OLLAMA_BASE_URL.rstrip("/") + "/api/embed"
engine = create_engine(settings.MYSQL_DSN.replace("mysql+aiomysql", "mysql+pymysql"))


def embed_http(texts):
    """直连 Ollama /api/embed，绕过 ollama 客户端库的 tokenize 子调用坑；服务端自动截断超长。"""
    payload = json.dumps({"model": settings.EMBED_MODEL, "input": texts, "truncate": True}).encode()
    req = urllib.request.Request(
        EMBED_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


def _clip(s, n):
    return (s or "")[:n]


def build_douban():
    out = []
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT m.title_text_cn, m.original_title, m.release_date_year, m.genres, m.country, "
            "m.rating, m.rating_count, m.director_name, t.description, t.plot, t.tags "
            "FROM douban_main m LEFT JOIN douban_text t ON m.id=t.id"
        )).mappings().all()
    for r in rows:
        p = []
        if r["title_text_cn"]: p.append(f"片名：{r['title_text_cn']}")
        if r["original_title"]: p.append(f"原名：{r['original_title']}")
        if r["release_date_year"]: p.append(f"年份：{r['release_date_year']}")
        if r["genres"]: p.append(f"类型：{r['genres']}")
        if r["country"]: p.append(f"国家：{r['country']}")
        if r["director_name"]: p.append(f"导演：{r['director_name']}")
        if r["description"]: p.append(f"简介：{_clip(r['description'], 800)}")
        if r["plot"]: p.append(f"剧情：{_clip(r['plot'], 800)}")
        if r["tags"]: p.append(f"标签：{_clip(r['tags'], 400)}")
        doc = " ".join(p)
        if doc.strip():
            out.append(doc[:1500])
    return out


def build_imdb():
    out = []
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT m.title_text_cn, m.title_text, m.release_date_year, m.genres, m.country, "
            "m.director_name, t.plot_text, t.plot_keywords, t.tagline "
            "FROM imdb_main m LEFT JOIN imdb_text t ON m.imdb_id=t.imdb_id"
        )).mappings().all()
    for r in rows:
        p = []
        name = r["title_text_cn"] or r["title_text"]
        if name: p.append(f"片名：{name}")
        if r["release_date_year"]: p.append(f"年份：{r['release_date_year']}")
        if r["genres"]: p.append(f"类型：{r['genres']}")
        if r["country"]: p.append(f"国家：{r['country']}")
        if r["director_name"]: p.append(f"导演：{r['director_name']}")
        if r["plot_text"]: p.append(f"剧情：{_clip(r['plot_text'], 800)}")
        if r["plot_keywords"]: p.append(f"关键词：{_clip(r['plot_keywords'], 300)}")
        if r["tagline"]: p.append(f"标语：{_clip(r['tagline'], 200)}")
        doc = " ".join(p)
        if doc.strip():
            out.append(doc[:1500])
    return out


def main():
    docs = build_douban() + build_imdb()
    print(f"[语料] 总文档数 = {len(docs)}")

    M = settings.EMBED_MODEL

    # 1) 单条热延迟（模拟线上实时查询的嵌入开销，模型已常驻）
    t0 = time.time()
    r1 = embed_http([docs[0][:200]])
    q_lat = time.time() - t0
    print(f"[单条 embed 热延迟] {q_lat*1000:.0f} ms，dim = {len(r1['embeddings'][0])}")

    # 2) warmup（Ollama 首次加载模型权重可能慢）
    t0 = time.time()
    embed_http(docs[:20])
    warm = time.time() - t0
    print(f"[warmup 前20条] {warm:.2f}s ({warm/20*1000:.0f} ms/条)")

    # 3) 全量嵌入计时（分批，贴近真实建库；Ollama 单次 embed 有总 token 上限）
    BATCH = 64
    t0 = time.time()
    n_ok = 0
    for i in range(0, len(docs), BATCH):
        r = embed_http(docs[i : i + BATCH])
        n_ok += len(r["embeddings"])
    total = time.time() - t0
    per = total / len(docs)
    print(f"[全量 {len(docs)} 条 / 批{BATCH}] 成功={n_ok} 总耗时 = {total:.2f}s | 平均 = {per*1000:.0f} ms/条 "
          f"| 吞吐 = {len(docs)/total:.1f} 条/s")

    # 4) 外推（豆瓣全量~35万 + IMDb~20万，去重后约 50-55 万）
    print("[外推]")
    for label, N in [("样本现状 ~1134", len(docs)), ("全量 ~5万", 50000), ("全量 ~55万", 550000)]:
        est = per * N
        print(f"  {label:14s}: 约 {est:7.1f}s = {est/60:6.1f} 分钟")


if __name__ == "__main__":
    main()
