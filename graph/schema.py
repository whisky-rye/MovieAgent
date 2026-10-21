"""知识图谱数据类型与关系定义（F1/O12）。

实体（Entity）按 key 前缀区分来源：
- 影片：与 Candidate.id 一致 —— ``douban:{PK}`` / ``imdb:{imdb_id}``
- 人物：``douban_name:{name_id}`` / ``imdb_name:{name_id}``
- 类型：``genre:{name}``（跨源共享，扩展时查两表）
- 国家/地区：``country:{name}``

关系（Relation）为有向边 ``subj --pred--> obj``，带权重。
"""
from dataclasses import dataclass, field
from typing import Optional


# ---- 实体类型 ----
ENTITY_MOVIE = "movie"
ENTITY_PERSON = "person"
ENTITY_GENRE = "genre"
ENTITY_COUNTRY = "country"


# ---- 关系类型 ----
REL_DIRECTED_BY = "directed_by"
REL_WRITTEN_BY = "written_by"
REL_ACTED_BY = "acted_by"
REL_HAS_GENRE = "has_genre"
REL_FROM_COUNTRY = "from_country"


# 职位（credits_type）→（关系, 权重）。导演权重最高，编剧次之，卡司/其他最低。
ROLE_TO_REL = {
    "director": (REL_DIRECTED_BY, 2.5),
    "导演": (REL_DIRECTED_BY, 2.5),
    "writer": (REL_WRITTEN_BY, 1.5),
    "编剧": (REL_WRITTEN_BY, 1.5),
    "cast": (REL_ACTED_BY, 1.0),
    "主演": (REL_ACTED_BY, 1.0),
    "actor": (REL_ACTED_BY, 1.0),
}

# 关系 → 基础权重（用于图遍历打分）
REL_WEIGHT = {
    REL_DIRECTED_BY: 2.5,
    REL_WRITTEN_BY: 1.5,
    REL_ACTED_BY: 1.0,
    REL_HAS_GENRE: 0.8,
    REL_FROM_COUNTRY: 0.5,
}

# 关系 → 中文标签（路径可读性）
REL_LABEL_CN = {
    REL_DIRECTED_BY: "导演",
    REL_WRITTEN_BY: "编剧",
    REL_ACTED_BY: "主演",
    REL_HAS_GENRE: "类型",
    REL_FROM_COUNTRY: "地区",
}

# 职位 → 中文标签
ROLE_LABEL_CN = {
    "director": "导演",
    "writer": "编剧",
    "cast": "主演",
    "actor": "主演",
    "导演": "导演",
    "编剧": "编剧",
    "主演": "主演",
}


@dataclass
class Relation:
    """有向关系边：subj --pred(role)--> obj，带权重。"""

    subj: str
    pred: str
    obj: str
    weight: float
    role: str = ""  # credits_type（director/writer/cast），仅人物边有意义


@dataclass
class GraphHit:
    """图谱遍历命中（最终转成 Candidate）。"""

    item_id: str  # douban:{PK} / imdb:{imdb_id}
    source: str
    title: str
    year: int
    genres: list
    country: str
    rating: float
    rating_count: int
    score: float  # 图谱路径综合分（含质量偏置）
    douban_id: str = ""
    imdb_id: str = ""
    paths: list[str] = field(default_factory=list)  # 人类可读路径解释


def parse_entity_kind(key: str) -> str:
    """根据 key 前缀判断实体类型。"""
    if key.startswith("douban:") or key.startswith("imdb:"):
        return ENTITY_MOVIE
    if key.startswith("douban_name:") or key.startswith("imdb_name:"):
        return ENTITY_PERSON
    if key.startswith("genre:"):
        return ENTITY_GENRE
    if key.startswith("country:"):
        return ENTITY_COUNTRY
    return "unknown"


def person_key(source: str, name_id) -> str:
    return f"{source}_name:{name_id}"


def genre_key(name: str) -> str:
    return f"genre:{name}"


def country_key(name: str) -> str:
    return f"country:{name}"
