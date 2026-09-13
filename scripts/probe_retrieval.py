"""P2 召回 + 排序验证探针：内容相似 / 关系 / 冷启动 三路召回 → 融合 / MMR 重排。"""
from retrieval import content_recall, relation_recall, coldstart_recall
from ranker import rerank


def _show(title: str, cands, n: int = 8):
    print(f"\n=== {title} (返回 {len(cands)}) ===")
    for i, c in enumerate(cands[:n], 1):
        print(f"  {i:2d}. [{c.recall_source:9s}] {c.title} ({c.year}) "
              f"评分={c.rating:.1f} 类型={','.join(c.genres[:3])} 综合={c.score:.3f}")


def main():
    print("=== P2 召回 + 排序探针 ===")

    # 1) 内容相似
    a = content_recall(query="太空科幻 孤独", top_k=10, genres=["科幻"])
    _show("① 内容相似：太空科幻", a)
    seed = a[0].id if a else None

    # 2) 关系召回（基于上面第一条作品找同剧组）
    b = relation_recall(seed, top_k=8) if seed else []
    if seed:
        _show(f"② 关系召回：基于《{a[0].title}》", b)

    # 3) 冷启动高分榜
    c = coldstart_recall(top_k=10, min_year=2000)
    _show("③ 冷启动：高分榜(>=2000)", c)

    # 4) 融合排序 + 偏好加权
    recall = {"content": a, "coldstart": c}
    if seed:
        recall["relation"] = b
    fused = rerank(recall, strategy="score", top_k=10, user_genres=["科幻"])
    _show("④ 融合(score) + 偏好[科幻]", fused)

    # 5) MMR 多样性重排
    mmr = rerank(recall, strategy="mmr", top_k=10, user_genres=["科幻"])
    _show("⑤ 融合 + MMR 多样性", mmr)

    print("\n=== 探针完成 ===")


if __name__ == "__main__":
    main()
