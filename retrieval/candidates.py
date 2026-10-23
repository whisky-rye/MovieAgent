"""召回候选统一数据结构。召回层(retrieval/*)产出，排序层(ranker/*)消费。

跨源对齐（Q3/O20）：同一部影片在豆瓣(douban)与 IMDb(imdb) 两源可能各存一份，
通过 `imdb_id`（双源唯一键）归并为同一「作品」(work)。归并后候选同时携带
`douban_id` 与 `imdb_id`（双源外链），并以 `title_alt` 保留另一侧异名
（中文名 ↔ 英文名对齐），避免结果里出现重复条目、且前端可同时从两侧取详情。
"""
import re
from dataclasses import dataclass, field


@dataclass
class Candidate:
    id: str                     # 与向量库一致：douban:{PK} / imdb:{imdb_id}
    source: str                 # 'douban' | 'imdb'
    title: str
    year: int = 0
    genres: list[str] = field(default_factory=list)
    country: str = ""
    rating: float = 0.0
    rating_count: int = 0
    doc: str = ""
    recall_source: str = ""     # content | relation | coldstart | graph
    score: float = 0.0          # 召回/排序阶段综合分
    douban_id: str = ""         # 豆瓣 subject id（用于拼 detail 链接，≠ 主键 id）
    imdb_id: str = ""           # IMDb title id（形如 tt1234567，用于拼 detail 链接）
    graph_paths: list[str] = field(default_factory=list)  # 知识图谱关系链解释（可解释召回）
    title_alt: str = ""         # 异名（跨源对齐）：如豆瓣中文名 ↔ IMDb 英文名（默认空）


def work_key(c: "Candidate") -> str:
    """跨源作品归并键：优先 imdb_id（双源唯一主键），其次 douban_id，最后回退自身 id。

    同片在 douban 与 imdb 两源各自一份时，imdb_id 一致 → 归并为同一 work，
    从而在融合/去重阶段只出现一条结果（携带两侧外链与异名）。
    """
    if c.imdb_id:
        return f"imdb:{c.imdb_id}"
    if c.douban_id:
        return f"douban:{c.douban_id}"
    return c.id


def _richer(a: "Candidate", b: "Candidate") -> bool:
    """判断 a 是否更适合作为「代表候选」（决定 id/标题等展示字段来源）。

    优先级：双源外链齐全 > 单源外链 > 无外链；其次评分样本量（更可信）；
    其次中文源(douban)优先（中文 UI）；最后标题长度（更完整）。
    """
    sa = 2 if (a.douban_id and a.imdb_id) else (1 if (a.douban_id or a.imdb_id) else 0)
    sb = 2 if (b.douban_id and b.imdb_id) else (1 if (b.douban_id or b.imdb_id) else 0)
    if sa != sb:
        return sa > sb
    if a.rating_count != b.rating_count:
        return a.rating_count > b.rating_count
    if (a.source or "") != (b.source or ""):
        return (a.source or "") == "douban"
    return len(a.title or "") >= len(b.title or "")


def merge_candidate(base: "Candidate", c: "Candidate") -> "Candidate":
    """把候选 c 归并进代表候选 base（原地修改，返回 base）。

    - 合并双源外链 id（不丢任何一侧）；
    - 异名保留：把与 base.title 不同的另一侧标题存入 title_alt；
    - 展示字段择优（_richer 决定 id/标题/评分等来源）。
    用于 fuse / rrf_fuse 的跨源同作品合并。
    """
    # 1) 合并跨源外链 id（任一侧有则补齐）
    if c.douban_id and not base.douban_id:
        base.douban_id = c.douban_id
    if c.imdb_id and not base.imdb_id:
        base.imdb_id = c.imdb_id
    # 2) 异名保留：c 标题与 base 不同且 base 尚无 title_alt 时记入
    if c.title and c.title != base.title and not base.title_alt:
        base.title_alt = c.title
    # 3) 展示字段择优（可能改变 base.id）
    if _richer(c, base):
        base.title_alt = base.title_alt or c.title_alt or ""
        base.id = c.id
        base.source = c.source
        base.title = c.title
        base.year = c.year
        base.genres = list(c.genres) if c.genres else base.genres
        base.country = c.country
        base.rating = c.rating
        base.rating_count = c.rating_count
        base.doc = c.doc or base.doc
        base.graph_paths = list(c.graph_paths) if c.graph_paths else base.graph_paths
    return base


def split_genres(s) -> list[str]:
    """把 '剧情|科幻/喜剧' 这类串拆成列表（覆盖豆瓣/IMDb 的 | / , 分隔）。"""
    return [g for g in re.split(r"[\|/,、\s]+", (s or "").strip()) if g]


#: 标准影片类型词表（中文，对齐豆瓣/IMDb 常见类型）。
#: 只作「偏好写入校验闸门」用：偏好里的 genres 会作为内容召回过滤条件下推
#: （vector backend 按类型集合求交），非标准类型串匹配不到任何影片。
KNOWN_GENRES = frozenset({
    "剧情", "喜剧", "动作", "爱情", "科幻", "悬疑", "惊悚", "恐怖", "战争",
    "犯罪", "奇幻", "冒险", "动画", "动画电影", "纪录片", "短片", "传记",
    "历史", "古装", "武侠", "家庭", "儿童", "音乐", "歌舞", "运动", "西部",
    "黑色电影", "独立电影", "灾难", "情色", "同性", "脱口秀", "戏曲", "舞台艺术",
})


def normalize_genres(values) -> list[str]:
    """偏好类型校验闸门（P1-1）：去空白/去重，仅保留 KNOWN_GENRES 内标准类型。

    背景：曾用正则 `[\u4e00-\u9fa0]{2,3}` 从用户原话切"类型"，「我喜欢周星驰的电影」
    会切出 ["我喜欢","周星驰","的电"] 落库，此后该用户每次内容召回都带这些垃圾
    类型过滤 → 召回空结果、推荐质量静默劣化，且垃圾偏好永久留存。
    现在任何写入路径（add_pref / sediment_preferences / 偏好意图）都必须过本闸门，
    非词表条目（正则片段、LLM 幻觉、中英混合）一律丢弃。保序去重。
    """
    out: list[str] = []
    for v in values or []:
        if not v:
            continue
        g = str(v).strip()
        if g in KNOWN_GENRES and g not in out:
            out.append(g)
    return out
