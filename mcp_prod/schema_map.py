"""生产库字段映射集中管理（防表结构漂移）。

调研报告 §4 风险项「表结构漂移」的缓解措施：所有表名/列名/关联键集中在此，
查询层只引用本模块常量，不散落字符串字面量。列名已于 2026-10-21 对生产实例
（MySQL 8.0.46）实拉 `information_schema.columns` 核对。

注意：生产库结构会随爬取任务演进（实测 2026-09→2026-10 期间新增了
`imdb_genre`/`imdb_country` 维度表与 `v_imdb_douban_compare` 跨源比对视图）。
新增查询前应先核对列名，再在此登记。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 源定义
# ---------------------------------------------------------------------------

SOURCE_IMDB = "imdb"
SOURCE_DOUBAN = "douban"


@dataclass(frozen=True)
class MainTableSpec:
    """影片主表结构。"""

    source: str
    table: str
    pk: str                 # 本地代理主键
    key: str                 # 业务关联键（imdb_id / douban_id）
    title_cols: tuple[str, ...]   # 标题列优先级（中文名优先展示）
    original_title_col: str = ""
    year_col: str = ""
    rating_col: str = ""
    votes_col: str = ""
    genres_col: str = ""
    country_col: str = ""
    language_col: str = ""
    imdb_id_col: str = ""    # 豆瓣主表里的 imdb_id（跨源映射键）


#: 影片主表（两源）
MAIN_TABLES: dict[str, MainTableSpec] = {
    SOURCE_IMDB: MainTableSpec(
        source=SOURCE_IMDB,
        table="imdb_main",
        pk="id",
        key="imdb_id",
        title_cols=("title_text_cn", "title_text"),
        original_title_col="title_text",
        year_col="release_date_year",
        rating_col="rate_agg",
        votes_col="rate_votes",
        genres_col="genres",
        country_col="country",
        language_col="language",
        imdb_id_col="imdb_id",
    ),
    SOURCE_DOUBAN: MainTableSpec(
        source=SOURCE_DOUBAN,
        table="douban_main",
        pk="id",
        key="douban_id",
        title_cols=("title_text_cn", "title_text", "original_title"),
        original_title_col="original_title",
        year_col="release_date_year",
        rating_col="rating",
        votes_col="rating_count",
        genres_col="genres",
        country_col="country",
        language_col="language",
        imdb_id_col="imdb_id",
    ),
}


@dataclass(frozen=True)
class RelationTableSpec:
    """影片↔人物 关联表结构。"""

    source: str
    table: str
    key_col: str            # 影片键列
    name_id_col: str
    name_cols: tuple[str, ...] = ()   # 冗余姓名列（豆瓣源有）
    role_col: str = "credits_type"
    character_col: str = "characters"


#: 影片↔人物 关联表
RELATION_TABLES: dict[str, RelationTableSpec] = {
    SOURCE_IMDB: RelationTableSpec(
        source=SOURCE_IMDB,
        table="imdb_name_relate",
        key_col="imdb_id",
        name_id_col="name_id",
        name_cols=("name",),
    ),
    SOURCE_DOUBAN: RelationTableSpec(
        source=SOURCE_DOUBAN,
        table="douban_name_relate",
        key_col="douban_id",
        name_id_col="name_id",
        name_cols=("name_cn", "name_en", "name"),
    ),
}


@dataclass(frozen=True)
class NameTableSpec:
    """人物维度表结构。"""

    source: str
    table: str
    name_id_col: str
    name_cols: tuple[str, ...]


#: 人物维度表
NAME_TABLES: dict[str, NameTableSpec] = {
    SOURCE_IMDB: NameTableSpec(
        source=SOURCE_IMDB, table="imdb_name", name_id_col="name_id",
        name_cols=("name",)),
    SOURCE_DOUBAN: NameTableSpec(
        source=SOURCE_DOUBAN, table="douban_name", name_id_col="name_id",
        name_cols=("name_cn", "name_en", "name")),
}


@dataclass(frozen=True)
class TextTableSpec:
    """剧情/简介文本表结构。"""

    source: str
    table: str
    key_col: str
    title_cols: tuple[str, ...]
    body_cols: tuple[str, ...]


#: 文本表
TEXT_TABLES: dict[str, TextTableSpec] = {
    SOURCE_IMDB: TextTableSpec(
        source=SOURCE_IMDB, table="imdb_text", key_col="imdb_id",
        title_cols=("title_text_cn", "title_text"),
        body_cols=("plot_text", "tagline", "plot_keywords", "akas_full", "akas")),
    SOURCE_DOUBAN: TextTableSpec(
        source=SOURCE_DOUBAN, table="douban_text", key_col="douban_id",
        title_cols=("title_text_cn", "title_text"),
        body_cols=("description", "plot", "tags", "hot_comments", "reviews")),
}


@dataclass(frozen=True)
class DimensionSpec:
    """类型/国家维度表结构（影片↔维度 + 维度字典）。"""

    source: str
    relate_table: str
    relate_key_col: str
    relate_dim_id_col: str
    relate_dim_name_col: str
    dim_table: str
    dim_id_col: str
    dim_name_cols: tuple[str, ...]


#: 类型维度
GENRE_DIMENSIONS: dict[str, DimensionSpec] = {
    SOURCE_IMDB: DimensionSpec(
        source=SOURCE_IMDB, relate_table="imdb_genre_relate",
        relate_key_col="imdb_id", relate_dim_id_col="genre_id",
        relate_dim_name_col="genre_name", dim_table="imdb_genre",
        dim_id_col="id", dim_name_cols=("genre_name", "genre_cn_name")),
    SOURCE_DOUBAN: DimensionSpec(
        source=SOURCE_DOUBAN, relate_table="douban_genre_relate",
        relate_key_col="douban_id", relate_dim_id_col="genre_id",
        relate_dim_name_col="genre_name", dim_table="douban_genre",
        dim_id_col="id", dim_name_cols=("genre_name", "genre_cn_name")),
}

#: 国家维度
COUNTRY_DIMENSIONS: dict[str, DimensionSpec] = {
    SOURCE_IMDB: DimensionSpec(
        source=SOURCE_IMDB, relate_table="imdb_country_relate",
        relate_key_col="imdb_id", relate_dim_id_col="country_id",
        relate_dim_name_col="country_name", dim_table="imdb_country",
        dim_id_col="id", dim_name_cols=("country_cn_name", "country_short_name",
                                        "country_name")),
    SOURCE_DOUBAN: DimensionSpec(
        source=SOURCE_DOUBAN, relate_table="douban_country_relate",
        relate_key_col="douban_id", relate_dim_id_col="country_id",
        relate_dim_name_col="country_name", dim_table="douban_country",
        dim_id_col="id", dim_name_cols=("country_cn_name", "country_short_name",
                                        "country_name")),
}

#: 跨源比对视图（imdb2 库）：官方口径的豆瓣↔IMDb 映射 + 双源评分对照
CROSS_SOURCE_VIEW = "v_imdb_douban_compare"

#: 跨源比对视图的列（实测）
CROSS_SOURCE_VIEW_COLUMNS = (
    "imdb_id", "imdb_title", "imdb_title_cn", "douban_title_cn",
    "imdb_rating", "imdb_votes", "meta_score",
    "douban_id", "douban_rating", "douban_votes",
    "release_date_year", "douban_genres",
)


def parse_pipe(value) -> list[str]:
    """解析 '|A|B|' 竖线分隔字段（生产库国家/类型/IMDb 标题字段格式）。"""
    if not value:
        return []
    return [p.strip() for p in str(value).split("|") if p.strip()]


def clean_title(value) -> str:
    """清洗标题：生产库 IMDb 侧标题为 '|中文名|英文名|' 竖线多值格式，取首个非空段。"""
    if not value:
        return ""
    parts = parse_pipe(value)
    return parts[0] if parts else str(value).strip()


def pick_title(row: dict, spec: MainTableSpec) -> str:
    """按优先级取展示标题（竖线多值已清洗）。"""
    for col in spec.title_cols:
        v = clean_title(row.get(col))
        if v:
            return v
    return ""


def normalize_main(row: dict, spec: MainTableSpec) -> dict:
    """把主表行归一为统一影片结构（跨源一致字段名）。"""
    return {
        "source": spec.source,
        "id": str(row.get(spec.pk) or ""),
        "source_id": str(row.get(spec.key) or ""),
        "imdb_id": str(row.get(spec.imdb_id_col) or ""),
        "douban_id": str(row.get("douban_id") or "") if spec.source == SOURCE_DOUBAN else "",
        "title": pick_title(row, spec),
        "original_title": clean_title(row.get(spec.original_title_col)) if spec.original_title_col else "",
        "year": int(row.get(spec.year_col) or 0),
        "rating": float(row.get(spec.rating_col) or 0.0),
        "rating_count": int(row.get(spec.votes_col) or 0),
        "genres": parse_pipe(row.get(spec.genres_col)),
        "country": parse_pipe(row.get(spec.country_col)),
        "language": parse_pipe(row.get(spec.language_col)),
    }


def normalize_person(row: dict, spec: NameTableSpec) -> str:
    """取人物显示名。"""
    for col in spec.name_cols:
        v = (row.get(col) or "").strip()
        if v:
            return v
    return ""


def all_tables() -> list[str]:
    """本模块登记的全部物理表名（供 schema 资源与白名单自检）。"""
    out: list[str] = []
    for spec in MAIN_TABLES.values():
        out.append(spec.table)
    for spec in RELATION_TABLES.values():
        out.append(spec.table)
    for spec in NAME_TABLES.values():
        out.append(spec.table)
    for spec in TEXT_TABLES.values():
        out.append(spec.table)
    for spec in GENRE_DIMENSIONS.values():
        out.extend([spec.relate_table, spec.dim_table])
    for spec in COUNTRY_DIMENSIONS.values():
        out.extend([spec.relate_table, spec.dim_table])
    out.append(CROSS_SOURCE_VIEW)
    return sorted(set(out))
