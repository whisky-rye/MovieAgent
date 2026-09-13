"""P1 召回探针：验证 Chroma 内容相似检索 + metadata 过滤是否可用。

运行（项目根）：
  PYTHONPATH=. .venv/Scripts/python.exe scripts/probe_recall.py
"""
from vector.embed_client import OllamaEmbedClient
from vector.backend import ChromaBackend


def show(title, rows):
    print(f"\n=== {title} ===")
    for i, (rid, doc, meta, dist) in enumerate(rows, 1):
        sim = 1 - dist  # cosine 距离 -> 相似度
        print(f"{i:2d}. [{meta.get('source'):>6}] {meta.get('title')} "
              f"| 年{meta.get('year')} 评分{meta.get('rating')} "
              f"{meta.get('country')} | sim={sim:.3f}")
        print(f"     {doc[:90]}...")


def main():
    client = OllamaEmbedClient()
    backend = ChromaBackend()
    print(f"[库] 当前条数 = {backend.count()}")

    q = "太空科幻 孤独 探索 人类未来"
    v = client.embed_query(q)
    print(f"\n>>> 查询：{q}")
    show("A. 内容相似（无过滤）", backend.query(v, top_k=8))
    show("B. 类型含『科幻』且国家=美国",
         backend.query(v, top_k=8, where={"country": "美国"}, genres=["科幻"]))
    show("C. 高评分(rating>=8)", backend.query(v, top_k=8, where={"rating": {"$gte": 8.0}}))

    q2 = "爱情 治愈 温暖 成长"
    v2 = client.embed_query(q2)
    print(f"\n>>> 查询：{q2}")
    show("D. 内容相似（无过滤）", backend.query(v2, top_k=8))

    q3 = "悬疑 反转 烧脑"
    v3 = client.embed_query(q3)
    print(f"\n>>> 查询：{q3}")
    show("E. 类型含『悬疑』", backend.query(v3, top_k=8, genres=["悬疑"]))


if __name__ == "__main__":
    main()
