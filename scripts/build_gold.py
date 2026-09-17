"""O9/D3：基于真实语料生成 gold 标注（query -> 相关候选 id 列表）。

真值依据（透明、可复核）：
  语料关系表 douban_genre_relate 标注了每部影片的类型(genre)。我们把「某 query 的相关影片」
  定义为「被标注为目标类型(genre)且在语料中真实存在的影片」。这是一种基于元数据的
  人工式标注（标注者=按类型口径判定相关性），可解释、可复核、可扩展。

ID 对齐：评估对象（融合排序）产出的 Candidate.id = douban:{douban_main.id}，
  而 genre_relate 通过 douban_main.douban_id（豆瓣 subject id）关联，故经
  douban_main.douban_id 中转取出 douban_main.id 后再拼成 douban:{id}，保证与排序结果同口径。

运行：python scripts/build_gold.py            # 写出 data/eval_gold.json
      python scripts/build_gold.py --out x.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.session import fetch_all  # noqa: E402

# query -> 目标类型(genre_id)。genre_id 取自 douban_genre 表。
# 选取得分高、且在语料中确有标注的通用类型，确保 gold 非空、可解释。
QUERIES = [
    {"query": "太空科幻 孤独宇航员", "genres": [41], "label": "科幻",
     "note": "相关=语料标注为「科幻」的影片"},
    {"query": "悬疑烧脑 反转", "genres": [47], "label": "悬疑",
     "note": "相关=语料标注为「悬疑」的影片"},
    {"query": "治愈系 动画 宫崎骏风格", "genres": [42], "label": "动画",
     "note": "相关=语料标注为「动画」的影片"},
    {"query": "高分悬疑犯罪 推理", "genres": [47, 38], "intersect": True,
     "label": "悬疑+犯罪", "note": "相关=同时标注「悬疑」与「犯罪」的影片"},
    {"query": "黑色幽默 犯罪", "genres": [38], "label": "犯罪",
     "note": "相关=语料标注为「犯罪」的影片"},
]

GENRE_NAMES = {41: "科幻", 47: "悬疑", 42: "动画", 38: "犯罪"}


def _fetch_ids(genres: list[int], intersect: bool, limit: int) -> list[str]:
    """按类型抽取真实 douban_main.id，转成 douban:{id} 候选 id。"""
    if not intersect:
        gids = ",".join(str(g) for g in genres)
        sql = f"""
            SELECT m.id, m.title_text_cn, m.rating
            FROM douban_genre_relate g
            JOIN douban_main m ON m.douban_id = g.douban_id
            WHERE g.genre_id IN ({gids})
            ORDER BY m.rating DESC
            LIMIT {limit}
        """
        rows = fetch_all(sql)
    else:
        # 多类型交集：每个 genre 各做一次子查询，应用层求交集
        id_sets: list[set] = []
        for g in genres:
            rows = fetch_all(
                "SELECT m.id FROM douban_genre_relate g "
                "JOIN douban_main m ON m.douban_id = g.douban_id "
                "WHERE g.genre_id = :gid",
                {"gid": g},
            )
            id_sets.append({r["id"] for r in rows})
        common = set.intersection(*id_sets) if id_sets else set()
        if not common:
            return []
        placeholders = ",".join(str(i) for i in common)
        rows = fetch_all(
            f"SELECT id, title_text_cn, rating FROM douban_main "
            f"WHERE id IN ({placeholders}) ORDER BY rating DESC LIMIT {limit}"
        )
    return [f"douban:{r['id']}" for r in rows]


def build_gold(limit_per_query: int = 40) -> dict:
    out = {"meta": {
        "annotation_basis": "语料类型(genre)标注：相关=被标注为目标类型的真实影片",
        "id_scheme": "douban:{douban_main.id}，与排序结果 Candidate.id 同口径",
        "limit_per_query": limit_per_query,
    }, "queries": []}
    for q in QUERIES:
        ids = _fetch_ids(q["genres"], q.get("intersect", False), limit_per_query)
        out["queries"].append({
            "query": q["query"],
            "label": q["label"],
            "relevant": ids,
            "note": q["note"],
        })
    return out


def _main():
    ap = argparse.ArgumentParser(description="基于语料类型标注生成 gold")
    ap.add_argument("--out", default="data/eval_gold.json", help="输出路径")
    ap.add_argument("--limit", type=int, default=40, help="每 query 相关集上限")
    args = ap.parse_args()

    gold = build_gold(limit_per_query=args.limit)
    n_rel = sum(len(q["relevant"]) for q in gold["queries"])
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(gold, f, ensure_ascii=False, indent=2)
    print(f"已写出 {args.out}：{len(gold['queries'])} 条 query，共 {n_rel} 个相关标注")
    for q in gold["queries"]:
        print(f"  - {q['query']:<22} 相关 {len(q['relevant'])} 部（{q['label']}）")


if __name__ == "__main__":
    _main()
