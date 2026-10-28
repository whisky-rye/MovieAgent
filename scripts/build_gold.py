"""O9/D3：基于真实语料生成 gold 标注（query -> 相关候选 id 列表）。

真值依据（透明、可复核）：
  语料关系表 douban_genre_relate / imdb_genre_relate 标注了每部影片的类型(genre)。
  我们把「某 query 的相关影片」定义为「被标注为目标类型(genre)且在语料中真实存在的影片」。
  这是一种基于元数据的标注（标注者=按类型口径判定相关性），可解释、可复核、可扩展。

ID 对齐：评估对象（融合排序）产出的 Candidate.id = douban:{douban_main.id} 或 imdb:{imdb_main.id}，
  而 genre_relate 通过 douban_main.douban_id / imdb_main.imdb_id 关联，故取出的 id 直接拼成
  douban:{id} / imdb:{id}，保证与排序结果同口径。

意图覆盖（P2-22）：gold 不再只有「豆瓣 genre」一种，扩至 ≥50 条，覆盖
  genre_douban / genre_imdb / similar / by_person / board_douban / board_imdb 六类意图，
  且同时含 IMDb 侧与豆瓣侧样本，可评估图谱/prod/精排在不同意图与双源上的增量。

离线兜底（P2-22）：当语料库不可达（如 CI 无 MySQL）时自动回退到「确定性合成 gold」，
  保证 gold 文件非空、门禁始终有真实资产可校验（合成 id 与真实 id 同口径，
  仅标注本身为合成，用于跑通链路与覆盖度，不用于判断绝对质量）。

运行：
  python scripts/build_gold.py                 # 写出 data/eval_gold.json（语料可达则用真实标注）
  python scripts/build_gold.py --synthetic     # 强制合成 gold（CI/离线）
  python scripts/build_gold.py --out x.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.session import fetch_all  # noqa: E402

# 类型 id -> 中文名（取自 douban_genre / imdb_genre 表；两库 genre_id 口径一致处复用）
GENRE_NAMES = {
    41: "科幻", 47: "悬疑", 42: "动画", 38: "犯罪", 39: "喜剧", 40: "爱情",
    43: "剧情", 44: "动作", 45: "冒险", 46: "奇幻", 48: "恐怖", 49: "战争",
    50: "纪录", 51: "音乐", 52: "西部", 53: "历史", 54: "同性", 55: "武侠",
}

# 统一查询清单：覆盖六类意图、双源。kind 决定取数方式；genres 为目标类型（IMDb 同表复用）。
# 每条带 intent 字段，供评估侧按意图分桶统计。
QUERIES = [
    # ---- 豆瓣类型（genre_douban） ----
    {"query": "太空科幻 孤独宇航员", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [41], "label": "科幻", "note": "相关=语料标注为「科幻」的影片"},
    {"query": "悬疑烧脑 反转", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [47], "label": "悬疑", "note": "相关=语料标注为「悬疑」的影片"},
    {"query": "治愈系 动画 宫崎骏风格", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [42], "label": "动画", "note": "相关=语料标注为「动画」的影片"},
    {"query": "高分悬疑犯罪 推理", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [47, 38], "intersect": True, "label": "悬疑+犯罪", "note": "相关=同时标注「悬疑」与「犯罪」"},
    {"query": "黑色幽默 犯罪", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [38], "label": "犯罪", "note": "相关=语料标注为「犯罪」的影片"},
    {"query": "轻松喜剧 解压", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [39], "label": "喜剧", "note": "相关=语料标注为「喜剧」的影片"},
    {"query": "爱情 青春 校园", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [40], "label": "爱情", "note": "相关=语料标注为「爱情」的影片"},
    {"query": "硬核动作 飙车", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [44], "label": "动作", "note": "相关=语料标注为「动作」的影片"},
    {"query": "奇幻冒险 史诗", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [46, 45], "intersect": True, "label": "奇幻+冒险", "note": "相关=同时标注「奇幻」与「冒险」"},
    {"query": "恐怖 惊悚 心理", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [48], "label": "恐怖", "note": "相关=语料标注为「恐怖」的影片"},

    # ---- IMDb 类型（genre_imdb） ----
    {"query": "sci-fi space opera", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [41], "label": "Sci-Fi", "note": "相关=IMDb 标注为「Sci-Fi」的影片"},
    {"query": "imdb thriller mind-bending", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [47], "label": "Thriller", "note": "相关=IMDb 标注为「Thriller」的影片"},
    {"query": "imdb animation family", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [42], "label": "Animation", "note": "相关=IMDb 标注为「Animation」的影片"},
    {"query": "imdb crime drama", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [38, 43], "intersect": True, "label": "Crime+Drama", "note": "相关=同时标注「Crime」与「Drama」"},
    {"query": "imdb comedy romcom", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [39, 40], "intersect": True, "label": "Comedy+Romance", "note": "相关=同时标注「Comedy」与「Romance」"},
    {"query": "imdb action war", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [44, 49], "intersect": True, "label": "Action+War", "note": "相关=同时标注「Action」与「War」"},
    {"query": "imdb fantasy adventure", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [46], "label": "Fantasy", "note": "相关=IMDb 标注为「Fantasy」的影片"},
    {"query": "imdb horror", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [48], "label": "Horror", "note": "相关=IMDb 标注为「Horror」的影片"},
    {"query": "imdb documentary", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [50], "label": "Documentary", "note": "相关=IMDb 标注为「Documentary」的影片"},
    {"query": "imdb history biography", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [53], "label": "History", "note": "相关=IMDb 标注为「History」的影片"},

    # ---- 相似片（similar）：以多类型交集代理「与某片同类」 ----
    {"query": "和我喜欢的科幻悬疑类似的", "intent": "similar", "kind": "douban_genre",
     "genres": [41, 47], "intersect": True, "label": "科幻∩悬疑", "note": "相似=同属科幻且悬疑的影片"},
    {"query": "类似 动画 奇幻 合家欢", "intent": "similar", "kind": "douban_genre",
     "genres": [42, 46], "intersect": True, "label": "动画∩奇幻", "note": "相似=同属动画且奇幻的影片"},
    {"query": "similar sci-fi thriller", "intent": "similar", "kind": "imdb_genre",
     "genres": [41, 47], "intersect": True, "label": "Sci-Fi∩Thriller", "note": "similar=IMDb 同属 Sci-Fi 且 Thriller"},
    {"query": "类似 爱情 喜剧 浪漫", "intent": "similar", "kind": "douban_genre",
     "genres": [40, 39], "intersect": True, "label": "爱情∩喜剧", "note": "相似=同属爱情且喜剧的影片"},
    {"query": "similar action adventure", "intent": "similar", "kind": "imdb_genre",
     "genres": [44, 45], "intersect": True, "label": "Action∩Adventure", "note": "similar=IMDb 同属 Action 且 Adventure"},
    {"query": "类似 犯罪 剧情 黑色", "intent": "similar", "kind": "douban_genre",
     "genres": [38, 43], "intersect": True, "label": "犯罪∩剧情", "note": "相似=同属犯罪且剧情的影片"},

    # ---- 人物相关（by_person）：以人物典型类型交集代理「该人物作品」 ----
    {"query": "诺兰 烧脑反转", "intent": "by_person", "kind": "douban_genre",
     "genres": [47, 46], "intersect": True, "label": "诺兰式", "note": "人物相关=悬疑+科幻风格影片（代理）"},
    {"query": "宫崎骏 动画", "intent": "by_person", "kind": "douban_genre",
     "genres": [42], "label": "宫崎骏式", "note": "人物相关=动画影片（代理）"},
    {"query": "Christopher Nolan films", "intent": "by_person", "kind": "imdb_genre",
     "genres": [47, 41], "intersect": True, "label": "Nolan-style", "note": "person=IMDb thriller+sci-fi (proxy)"},
    {"query": "昆汀 暴力美学 犯罪", "intent": "by_person", "kind": "douban_genre",
     "genres": [38, 39], "intersect": True, "label": "昆汀式", "note": "人物相关=犯罪+喜剧风格影片（代理）"},
    {"query": "Spielberg adventure", "intent": "by_person", "kind": "imdb_genre",
     "genres": [45, 42], "intersect": True, "label": "Spielberg-style", "note": "person=IMDb adventure+animation (proxy)"},
    {"query": "是枝裕和 剧情 家庭", "intent": "by_person", "kind": "douban_genre",
     "genres": [43, 40], "intersect": True, "label": "是枝式", "note": "人物相关=剧情+爱情风格影片（代理）"},

    # ---- 榜单（board）：高分 Top-N ----
    {"query": "必看高分神作 豆瓣", "intent": "board_douban", "kind": "board_douban",
     "label": "豆瓣高分", "note": "榜单=豆瓣评分 Top-N"},
    {"query": "IMDb top rated", "intent": "board_imdb", "kind": "board_imdb",
     "label": "IMDb Top", "note": "榜单=IMDb 评分 Top-N"},
    {"query": "年度口碑佳作 豆瓣", "intent": "board_douban", "kind": "board_douban",
     "label": "豆瓣口碑", "note": "榜单=豆瓣高分（第二桶）"},
    {"query": "IMDb top crime", "intent": "board_imdb", "kind": "imdb_genre",
     "genres": [38], "label": "IMDb Crime Top", "note": "榜单=IMDb 犯罪高分"},
    {"query": "经典科幻榜单 豆瓣", "intent": "board_douban", "kind": "douban_genre",
     "genres": [41], "label": "豆瓣科幻榜", "note": "榜单=豆瓣科幻高分"},

    # ---- 补充覆盖（确保 ≥50 条，意图/双源均衡） ----
    {"query": "war film 战争片", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [49], "label": "War", "note": "相关=IMDb 标注为「War」的影片"},
    {"query": "imdb western 西部", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [52], "label": "Western", "note": "相关=IMDb 标注为「Western」的影片"},
    {"query": "imdb music musical", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [51], "label": "Music", "note": "相关=IMDb 标注为「Music」的影片"},
    {"query": "imdb history drama", "intent": "genre_imdb", "kind": "imdb_genre",
     "genres": [53, 43], "intersect": True, "label": "History+Drama", "note": "相关=同时标注「History」与「Drama」"},
    {"query": "武侠 功夫 动作", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [55, 44], "intersect": True, "label": "武侠+动作", "note": "相关=同时标注「武侠」与「动作」"},
    {"query": "战争 历史 史诗", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [49, 53], "intersect": True, "label": "战争+历史", "note": "相关=同时标注「战争」与「历史」"},
    {"query": "纪录 真实 自然", "intent": "genre_douban", "kind": "douban_genre",
     "genres": [50], "label": "纪录", "note": "相关=语料标注为「纪录」的影片"},
    {"query": "similar comedy animation", "intent": "similar", "kind": "imdb_genre",
     "genres": [39, 42], "intersect": True, "label": "Comedy∩Animation", "note": "similar=IMDb 同属 Comedy 且 Animation"},
    {"query": "similar drama crime", "intent": "similar", "kind": "douban_genre",
     "genres": [43, 38], "intersect": True, "label": "剧情∩犯罪", "note": "相似=同属剧情且犯罪的影片"},
    {"query": "similar fantasy family", "intent": "similar", "kind": "douban_genre",
     "genres": [46, 40], "intersect": True, "label": "奇幻∩爱情", "note": "相似=同属奇幻且爱情的影片"},
    {"query": "王家卫 文艺 爱情", "intent": "by_person", "kind": "douban_genre",
     "genres": [40, 43], "intersect": True, "label": "王家卫式", "note": "人物相关=爱情+剧情风格影片（代理）"},
    {"query": "Tim Burton gothic", "intent": "by_person", "kind": "imdb_genre",
     "genres": [48, 46], "intersect": True, "label": "Burton-style", "note": "person=IMDb horror+fantasy (proxy)"},
    {"query": "IMDb top comedy", "intent": "board_imdb", "kind": "imdb_genre",
     "genres": [39], "label": "IMDb Comedy Top", "note": "榜单=IMDb 喜剧高分"},
    {"query": "IMDb top sci-fi", "intent": "board_imdb", "kind": "imdb_genre",
     "genres": [41], "label": "IMDb Sci-Fi Top", "note": "榜单=IMDb 科幻高分"},
    {"query": "华语剧情口碑 豆瓣", "intent": "board_douban", "kind": "douban_genre",
     "genres": [43], "label": "豆瓣剧情榜", "note": "榜单=豆瓣剧情高分"},
]


def _fetch_ids_douban(genres: list[int], intersect: bool, limit: int) -> list[str]:
    """按豆瓣类型抽取真实 douban_main.id，转成 douban:{id}。"""
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


def _fetch_ids_imdb(genres: list[int], intersect: bool, limit: int) -> list[str]:
    """按 IMDb 类型抽取真实 imdb_main.id，转成 imdb:{id}。"""
    if not intersect:
        gids = ",".join(str(g) for g in genres)
        sql = f"""
            SELECT m.id, m.title_text_en, m.rating
            FROM imdb_genre_relate g
            JOIN imdb_main m ON m.imdb_id = g.imdb_id
            WHERE g.genre_id IN ({gids})
            ORDER BY m.rating DESC
            LIMIT {limit}
        """
        rows = fetch_all(sql)
    else:
        id_sets: list[set] = []
        for g in genres:
            rows = fetch_all(
                "SELECT m.id FROM imdb_genre_relate g "
                "JOIN imdb_main m ON m.imdb_id = g.imdb_id "
                "WHERE g.genre_id = :gid",
                {"gid": g},
            )
            id_sets.append({r["id"] for r in rows})
        common = set.intersection(*id_sets) if id_sets else set()
        if not common:
            return []
        placeholders = ",".join(str(i) for i in common)
        rows = fetch_all(
            f"SELECT id, title_text_en, rating FROM imdb_main "
            f"WHERE id IN ({placeholders}) ORDER BY rating DESC LIMIT {limit}"
        )
    return [f"imdb:{r['id']}" for r in rows]


def _fetch_board_douban(limit: int) -> list[str]:
    rows = fetch_all(
        "SELECT id FROM douban_main ORDER BY rating DESC LIMIT :lim", {"lim": limit}
    )
    return [f"douban:{r['id']}" for r in rows]


def _fetch_board_imdb(limit: int) -> list[str]:
    rows = fetch_all(
        "SELECT id FROM imdb_main ORDER BY rating DESC LIMIT :lim", {"lim": limit}
    )
    return [f"imdb:{r['id']}" for r in rows]


def _fetch_ids(q: dict, limit: int) -> list[str]:
    """按 kind 分发到对应的取数函数。"""
    kind = q.get("kind", "douban_genre")
    genres = q.get("genres", [])
    intersect = q.get("intersect", False)
    if kind == "imdb_genre":
        return _fetch_ids_imdb(genres, intersect, limit)
    if kind == "board_douban":
        return _fetch_board_douban(limit)
    if kind == "board_imdb":
        return _fetch_board_imdb(limit)
    # 默认 douban_genre（含 similar/by_person 以豆瓣类型交集代理）
    return _fetch_ids_douban(genres, intersect, limit)


def _synthetic_gold(limit_per_query: int) -> dict:
    """确定性合成 gold：无语料时保证 gold 非空、覆盖六类意图与双源。

    合成 id 与真实 id 同口径（douban:{n} / imdb:tt{n}），仅标注本身为合成，
    用于跑通评估链路与覆盖度统计，不用于判断绝对质量。
    """
    out = {"meta": {
        "annotation_basis": "合成（语料不可达时的离线兜底）：按意图确定性分配 id，"
                            "仅保证覆盖度与链路可跑通",
        "id_scheme": "douban:{douban_main.id} / imdb:{imdb_main.id}，与排序结果同口径",
        "synthetic": True,
        "limit_per_query": limit_per_query,
    }, "queries": []}
    for idx, q in enumerate(QUERIES):
        intent = q["intent"]
        if intent in ("genre_imdb", "board_imdb"):
            prefix = "imdb:tt"
            base = 1000 + idx * 13
        else:
            prefix = "douban:"
            base = 2000 + idx * 17
        ids = [f"{prefix}{base + j}" for j in range(limit_per_query)]
        out["queries"].append({
            "query": q["query"],
            "intent": intent,
            "label": q.get("label", ""),
            "relevant": ids,
            "note": q.get("note", "") + " [synthetic]",
        })
    return out


def build_gold(limit_per_query: int = 40, synthetic: bool = False) -> dict:
    if synthetic:
        return _synthetic_gold(limit_per_query)

    out = {"meta": {
        "annotation_basis": "语料类型(genre)标注：相关=被标注为目标类型的真实影片",
        "id_scheme": "douban:{douban_main.id} / imdb:{imdb_main.id}，与排序结果同口径",
        "limit_per_query": limit_per_query,
    }, "queries": []}
    for q in QUERIES:
        try:
            ids = _fetch_ids(q, limit_per_query)
        except Exception as exc:  # 单条查询失败不应中断整体；记录为空并在 meta 标注
            print(f"[warn] query={q['query']!r} 取数失败，跳过: {exc}", file=sys.stderr)
            ids = []
        out["queries"].append({
            "query": q["query"],
            "intent": q["intent"],
            "label": q.get("label", ""),
            "relevant": ids,
            "note": q.get("note", ""),
        })

    total_rel = sum(len(q["relevant"]) for q in out["queries"])
    # 语料不可达（total_rel==0）时回退合成，保证门禁有资产
    if total_rel == 0:
        print("[warn] 语料库不可达或类型表为空，回退确定性合成 gold（覆盖度用）", file=sys.stderr)
        return _synthetic_gold(limit_per_query)
    out["meta"]["total_relevant"] = total_rel
    return out


def _main():
    ap = argparse.ArgumentParser(description="基于语料类型标注生成 gold")
    ap.add_argument("--out", default="data/eval_gold.json", help="输出路径")
    ap.add_argument("--limit", type=int, default=40, help="每 query 相关集上限")
    ap.add_argument("--synthetic", action="store_true", help="强制合成 gold（CI/离线）")
    args = ap.parse_args()

    gold = build_gold(limit_per_query=args.limit, synthetic=args.synthetic)
    n_rel = sum(len(q["relevant"]) for q in gold["queries"])
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(gold, f, ensure_ascii=False, indent=2)
    n_intents = len({q["intent"] for q in gold["queries"]})
    print(f"已写出 {args.out}：{len(gold['queries'])} 条 query（{n_intents} 类意图），共 {n_rel} 个相关标注"
          + (" [synthetic]" if gold["meta"].get("synthetic") else ""))
    for q in gold["queries"]:
        print(f"  - {q['query']:<26} [{q['intent']:<13}] 相关 {len(q['relevant'])} 部（{q['label']}）")


if __name__ == "__main__":
    _main()
