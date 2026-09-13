"""召回候选统一数据结构。召回层(retrieval/*)产出，排序层(ranker/*)消费。"""
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
    recall_source: str = ""     # content | relation | coldstart
    score: float = 0.0          # 召回/排序阶段综合分


def split_genres(s) -> list[str]:
    """把 '剧情|科幻/喜剧' 这类串拆成列表（覆盖豆瓣/IMDb 的 | / , 分隔）。"""
    return [g for g in re.split(r"[\|/,、\s]+", (s or "").strip()) if g]
