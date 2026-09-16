"""生成 300 条多样化的影片推荐语句（prompt suggestions），落盘 JSON 供前端导入。

组合维度：年代/档期 × 类型 × 评价词 × 国家/地区 × 导演 × 演员 × 风格流派 ×
观影心情/场合 × 参照影片（"类似XX的同风格影片"）等，多模板族随机组合 + 去重，
最终固定数量输出（打乱顺序）。

用法：python scripts/gen_prompt_suggestions.py
输出：frontend/src/data/suggestions.json  {"suggestions": ["...", ...]}
"""
from __future__ import annotations

import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "frontend" / "src" / "data" / "suggestions.json"
TARGET = 300

# —— 参数池 ——
DECADES = ["近10年", "近5年", "近20年", "2020年代", "2010年代", "90年代经典", "80年代经典", "21世纪以来"]
GENRES = ["科幻", "悬疑", "喜剧", "爱情", "动作", "恐怖", "战争", "犯罪", "动画", "奇幻",
          "剧情", "冒险", "惊悚", "西部", "谍战", "灾难", "武侠", "歌舞", "黑色幽默", "传记"]
QUALITIES = ["高分", "豆瓣8分以上", "冷门佳作", "经典必看", "口碑爆棚", "被低估的"]
COUNTRIES = ["美国", "韩国", "日本", "法国", "英国", "中国", "意大利", "西班牙", "印度", "德国", "北欧", "俄罗斯"]
DIRECTORS = ["诺兰", "维伦纽瓦", "宫崎骏", "王家卫", "李安", "昆汀·塔伦蒂诺", "斯皮尔伯格",
             "马丁·斯科塞斯", "希区柯克", "奉俊昊", "是枝裕和", "张艺谋", "姜文", "大卫·芬奇",
             "韦斯·安德森", "岩井俊二", "新海诚", "吉尔莫·德尔·托罗", "侯孝贤", "贾樟柯"]
ACTORS = ["莱昂纳多·迪卡普里奥", "汤姆·汉克斯", "周星驰", "葛优", "摩根·弗里曼", "奥黛丽·赫本",
          "梁朝伟", "张曼玉", "马东锡", "梅丽尔·斯特里普", "克里斯蒂安·贝尔", "巩俐"]
STYLES = ["赛博朋克", "黑色电影", "哥特风", "太空歌剧", "时间循环", "非线性叙事", "公路片",
          "怪兽片", "一镜到底", "意式西部"]
MOODS = ["轻松", "烧脑", "治愈", "催泪", "热血", "紧张刺激", "温馨合家欢", "深夜独自看"]
OCCASIONS = ["周末", "下雨天", "睡前", "圣诞节", "长途飞行", "假期宅家"]
REFERENCES = ["盗梦空间", "星际穿越", "肖申克的救赎", "教父", "千与千寻", "寄生虫", "让子弹飞",
              "布达佩斯大饭店", "银翼杀手2049", "花样年华", "楚门的世界", "流浪地球"]


def build_candidates(rnd: random.Random) -> list[str]:
    """多模板族笛卡尔采样，返回去重后的候选池。"""
    seen: set[str] = set()
    out: list[str] = []

    def add(s: str) -> None:
        if s not in seen:
            seen.add(s)
            out.append(s)

    # 1) 年代 + 类型 + 评价词
    for d in DECADES:
        for g in GENRES:
            add(f"{d}{rnd.choice(QUALITIES)}{g}片")
            add(f"推荐几部{d}的{rnd.choice(QUALITIES)}{g}电影")
    # 2) 国家 + 类型 + 评价
    for c in COUNTRIES:
        for g in GENRES:
            add(f"{c}{rnd.choice(QUALITIES)}{g}片")
            add(f"{c}导演拍的{g}电影有什么好看的")
    # 3) 导演 + 风味
    for d in DIRECTORS:
        add(f"{d}导演的{rnd.choice(MOODS)}电影")
        add(f"想看{d}导演的作品，从哪部开始")
        add(f"{d}导演早期和后期风格差别大的代表作")
    # 4) 演员
    for a in ACTORS:
        add(f"{a}主演的{rnd.choice(QUALITIES)}电影")
        add(f"{a}演过的{rnd.choice(GENRES)}片里哪部最好看")
    # 5) 风格流派
    for s in STYLES:
        add(f"推荐几部{s}风格的{rnd.choice(GENRES)}片")
        add(f"最有代表性的{s}电影")
    # 6) 心情 + 类型
    for m in MOODS:
        for g in ("喜剧", "爱情", "动画", "剧情", "奇幻"):
            add(f"{m}的{g}电影")
            add(f"想看点{m}的，推荐几部{g}片")
    # 7) 场合 + 类型/心情
    for o in OCCASIONS:
        add(f"适合{o}看的{rnd.choice(GENRES)}电影")
        add(f"{o}适合看的{rnd.choice(MOODS)}电影")
    # 8) 参照影片
    for r in REFERENCES:
        add(f"类似《{r}》的同风格影片推荐")
        add(f"看完《{r}》意犹未尽，还有什么类似的")
    # 9) 混合长句（年代×国家×类型 等）
    for c in COUNTRIES:
        for d in ("近10年", "近5年", "2010年代"):
            add(f"{d}{c}{rnd.choice(QUALITIES)}{rnd.choice(GENRES)}片")
    # 10) 其他句式
    add("今年最受欢迎的新片有哪些")
    add("影史排名前几的神作各是什么风格")
    add("推荐几部结局反转惊艳的电影")
    add("有哪些一口气看不完、值得慢慢品的电影")
    return out


def main() -> None:
    rnd = random.Random(20260916)  # 固定种子：可复现
    pool = build_candidates(rnd)
    rnd.shuffle(pool)
    if len(pool) < TARGET:
        raise SystemExit(f"候选池不足：{len(pool)} < {TARGET}")
    suggestions = pool[:TARGET]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"suggestions": suggestions}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"候选池 {len(pool)} 条 → 输出 {len(suggestions)} 条 → {OUT}")


if __name__ == "__main__":
    main()
