"""MCP 查询层单测（用假客户端，不连真库）。

覆盖：
- 所有外部输入必须**参数化绑定**（不得拼接进 SQL 字符串）；
- 查询只打在本包 schema_map 登记的表上；
- 归一化输出字段正确（跨源统一结构、竖线多值标题清洗）；
- 入参校验与 limit 钳制；
- 客户端降级语义（异常 → None，绝不抛给业务层）。
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: E402
from mcp_prod import queries as Q  # noqa: E402
from mcp_prod import schema_map as sm  # noqa: E402
from mcp_prod.client import ProdChannel, _to_candidate  # noqa: E402
from mcp_prod.connection import McpError, McpRateLimited, McpReadOnlyViolation  # noqa: E402


class FakeClient:
    """假只读客户端：记录 SQL/参数，按需返回预置行。"""

    def __init__(self, rows_by_tool=None, error=None):
        self.calls = []
        self.rows_by_tool = rows_by_tool or {}
        self.error = error
        s = types.SimpleNamespace(**{**vars(settings)})
        s.MCP_MAX_LIMIT = 200
        s.MCP_DB_IMDB = "imdb"
        s.MCP_DB_DOUBAN = "imdb2"
        self.s = s

    def db_for_source(self, source):
        return "imdb" if source.startswith("imdb") else "imdb2"

    def query(self, db, sql, params=None, max_limit=None, tool="", allow_metadata=False):
        self.calls.append({"db": db, "sql": sql, "params": params,
                           "tool": tool, "allow_metadata": allow_metadata})
        if self.error:
            raise self.error
        rows = self.rows_by_tool.get(tool, [])
        return types.SimpleNamespace(rows=rows, row_count=len(rows),
                                     elapsed_ms=1, db=db, tables=[], truncated=False)

    def health(self):
        return {"configured": True, "circuit_open": False}

    @property
    def all_sql(self):
        return " ".join(c["sql"] for c in self.calls)

    @property
    def all_params(self):
        out = []
        for c in self.calls:
            if c["params"]:
                out.extend(list(c["params"]))
        return out


# ---------------------------------------------------------------------------
# search_movie
# ---------------------------------------------------------------------------

def test_search_movie_parameterized():
    c = FakeClient({"search_movie": [{"id": 1, "douban_id": "1292052",
                                      "title_text_cn": "肖申克的救赎",
                                      "release_date_year": 1994, "rating": 9.7,
                                      "rating_count": 100, "genres": "剧情|犯罪",
                                      "country": "美国", "language": "英语",
                                      "imdb_id": "tt0111161"}]})
    out = Q.search_movie("肖申克", source="douban", limit=3, client=c)
    assert out["count"] == 1
    assert c.calls[0]["params"][0] == "%肖申克%", "关键词必须参数化绑定"
    assert "肖申克" not in c.calls[0]["sql"], "关键词不得拼进 SQL 字符串"
    item = out["items"][0]
    assert item["title"] == "肖申克的救赎"
    assert item["year"] == 1994
    assert item["genres"] == ["剧情", "犯罪"]
    assert item["imdb_id"] == "tt0111161"


def test_search_movie_uses_only_main_table():
    c = FakeClient()
    Q.search_movie("x", source="douban", client=c)
    assert "douban_main" in c.calls[0]["sql"]
    assert "imdb_main" not in c.calls[0]["sql"]


def test_search_movie_limit_is_numeric_literal():
    """LIMIT 必须是数字字面量（护栏才能强制上限）。"""
    c = FakeClient()
    Q.search_movie("x", source="douban", limit=7, client=c)
    assert "LIMIT 7" in c.calls[0]["sql"]
    assert "LIMIT %s" not in c.calls[0]["sql"]


def test_search_movie_limit_clamped():
    c = FakeClient()
    Q.search_movie("x", source="douban", limit=99999, client=c)
    assert "LIMIT 200" in c.calls[0]["sql"]


def test_search_movie_empty_keyword_rejected():
    c = FakeClient()
    with pytest.raises(McpError):
        Q.search_movie("   ", source="douban", client=c)
    assert c.calls == [], "非法入参不应触发任何查询"


def test_search_movie_unknown_source_rejected():
    c = FakeClient()
    with pytest.raises(McpError):
        Q.search_movie("x", source="netflix", client=c)


def test_search_movie_injection_keyword_is_bound_not_executed():
    """注入型关键字仅作为绑定参数传递，不得改变 SQL 结构。"""
    c = FakeClient()
    Q.search_movie("'; DROP TABLE douban_main; --", source="douban", client=c)
    sql = c.calls[0]["sql"]
    assert "DROP" not in sql.upper()
    # 作为 LIKE 绑定参数整体传入（前后 % 为模糊匹配通配，非拼接）
    assert c.calls[0]["params"][0] == "%'; DROP TABLE douban_main; --%"
    assert ";" not in sql.rstrip(";"), "SQL 结构不得出现注入的分号"


# ---------------------------------------------------------------------------
# get_movie_detail
# ---------------------------------------------------------------------------

def test_get_movie_detail_not_found():
    c = FakeClient({"get_movie_detail": []})
    out = Q.get_movie_detail("douban", "1292052", client=c)
    assert out["found"] is False
    assert out["movie"] is None


def test_get_movie_detail_full_chain():
    c = FakeClient({
        "get_movie_detail": [{"id": 1, "douban_id": "1292052", "imdb_id": "tt0111161",
                              "title_text_cn": "肖申克的救赎", "release_date_year": 1994,
                              "rating": 9.7, "rating_count": 100, "genres": "剧情|犯罪",
                              "country": "美国", "language": "英语"}],
        "get_movie_detail.credits": [{"name_id": 1, "credits_type": "director",
                                       "name_cn": "弗兰克·德拉邦特", "name_en": "",
                                       "name": "Frank Darabont", "characters": ""}],
        "get_movie_detail.text": [{"description": "越狱", "plot": "p", "tags": "t",
                                    "hot_comments": "c", "reviews": "r"}],
        "get_movie_detail.dimension": [{"n": "剧情"}, {"n": "犯罪"}],
    })
    out = Q.get_movie_detail("douban", "1292052", client=c)
    assert out["found"] is True
    assert out["movie"]["title"] == "肖申克的救赎"
    assert out["credits"][0]["name"] == "弗兰克·德拉邦特"
    assert out["text"]["description"] == "越狱"
    assert out["genres"] == ["剧情", "犯罪"]


def test_get_movie_detail_can_skip_credits_and_text():
    c = FakeClient({"get_movie_detail": [{"id": 1, "douban_id": "1",
                                           "title_text_cn": "x"}]})
    out = Q.get_movie_detail("douban", "1", include_text=False,
                             include_credits=False, client=c)
    assert "credits" not in out and "text" not in out
    assert all("credits" not in cc["tool"] for cc in c.calls)


def test_get_movie_detail_empty_id_rejected():
    c = FakeClient()
    with pytest.raises(McpError):
        Q.get_movie_detail("douban", "", client=c)
    assert c.calls == []


# ---------------------------------------------------------------------------
# get_movie_relations
# ---------------------------------------------------------------------------

def test_get_movie_relations_batches_by_name_ids():
    c = FakeClient({
        "get_movie_relations.people": [{"nid": 10}, {"nid": 20}],
        "get_movie_relations": [{"k": "1292053", "shared": 2, "weight": 3.5,
                                 "nids": "10,20"}],
        "get_movie_relations.main": [{"id": 2, "douban_id": "1292053",
                                      "title_text_cn": "绿里奇迹",
                                      "release_date_year": 1999, "rating": 8.0,
                                      "rating_count": 50, "genres": "剧情",
                                      "country": "美国", "language": "英语",
                                      "imdb_id": ""}],
    })
    out = Q.get_movie_relations("douban", "1292052", limit=5, client=c)
    assert out["count"] == 1
    assert out["items"][0]["title"] == "绿里奇迹"
    assert out["items"][0]["shared_people"] == 2
    rel_sql = [cc["sql"] for cc in c.calls if cc["tool"] == "get_movie_relations"][0]
    assert "GROUP_CONCAT" in rel_sql.upper()
    assert "IN (%s, %s)" in rel_sql, "name_id 必须批量参数化"


def test_get_movie_relations_no_people_returns_empty():
    c = FakeClient({"get_movie_relations.people": []})
    out = Q.get_movie_relations("douban", "1", client=c)
    assert out["count"] == 0


# ---------------------------------------------------------------------------
# 人物 / 维度 / 统计 / 跨源
# ---------------------------------------------------------------------------

def test_search_person_prefers_matching_name_column():
    c = FakeClient({"search_person": [{"name_id": 1, "name_cn": "", "name_en": "Christopher Nolan",
                                        "name": "克里斯托弗·诺兰"}]})
    out = Q.search_person("诺兰", source="douban", client=c)
    assert out["items"][0]["name"] == "克里斯托弗·诺兰"


def test_get_person_movies():
    c = FakeClient({
        "get_person_movies": [{"k": "1", "role": "director"}],
        "get_person_movies.main": [{"id": 1, "douban_id": "1", "title_text_cn": "星际穿越",
                                    "release_date_year": 2014, "rating": 8.0,
                                    "rating_count": 10, "genres": "科幻",
                                    "country": "美国", "language": "英语", "imdb_id": ""}],
    })
    out = Q.get_person_movies("123", source="douban", client=c)
    assert out["count"] == 1
    assert out["items"][0]["title"] == "星际穿越"


def test_list_dimensions_genre_and_country():
    # 真实数据语义：douban_genre.genre_name 存中文名；douban_country.country_short_name 存中文名
    c = FakeClient({"list_dimensions": [{"genre_name": "剧情", "genre_cn_name": ""}]})
    out = Q.list_dimensions("genre", "douban", client=c)
    assert out["kind"] == "genre"
    assert out["items"][0]["name"] == "剧情"

    c2 = FakeClient({"list_dimensions": [{"country_name": "", "country_short_name": "美国",
                                          "country_cn_name": ""}]})
    out2 = Q.list_dimensions("country", "douban", client=c2)
    assert out2["items"][0]["name"] == "美国"


def test_list_dimensions_unknown_kind_rejected():
    c = FakeClient()
    with pytest.raises(McpError):
        Q.list_dimensions("planet", "douban", client=c)
    assert c.calls == []


def test_get_table_stats_uses_metadata_channel():
    c = FakeClient({"get_table_stats": [{"t": "douban_main", "n": 136500}]})
    out = Q.get_table_stats("douban", client=c)
    assert out["tables"][0]["table"] == "douban_main"
    assert c.calls[0]["allow_metadata"] is True
    assert "information_schema" in c.calls[0]["sql"]


def test_compare_cross_source_uses_registered_view():
    c = FakeClient({"compare_cross_source": [{
        "imdb_id": "tt0111161", "douban_id": "1292052", "imdb_title": "The Shawshank Redemption",
        "imdb_title_cn": "肖申克的救赎", "douban_title_cn": "肖申克的救赎",
        "imdb_rating": 9.3, "imdb_votes": 100, "douban_rating": 9.7, "douban_votes": 200,
        "release_date_year": 1994, "douban_genres": "剧情|犯罪"}]})
    out = Q.compare_cross_source("肖申克", client=c)
    assert out["count"] == 1
    assert out["items"][0]["imdb_id"] == "tt0111161"
    assert out["items"][0]["genres"] == ["剧情", "犯罪"]
    assert sm.CROSS_SOURCE_VIEW in c.calls[0]["sql"]


# ---------------------------------------------------------------------------
# schema_map
# ---------------------------------------------------------------------------

def test_schema_map_tables_are_prefixed_or_registered():
    for t in sm.all_tables():
        assert t.startswith(("imdb_", "douban_")) or t == sm.CROSS_SOURCE_VIEW, t


def test_schema_map_covers_real_tables():
    tables = set(sm.all_tables())
    for t in ("imdb_main", "douban_main", "imdb_name_relate", "douban_name_relate",
              "imdb_text", "douban_text", "imdb_genre", "douban_genre"):
        assert t in tables


def test_clean_title_handles_pipe_format():
    assert sm.clean_title("| 盗梦空间 |") == "盗梦空间"
    assert sm.clean_title("| 盗梦空间 |Inception|") == "盗梦空间"
    assert sm.clean_title("") == ""
    assert sm.clean_title(None) == ""


def test_normalize_main_unified_fields():
    spec = sm.MAIN_TABLES["douban"]
    row = {"id": 1, "douban_id": "1292052", "imdb_id": "tt0111161",
           "title_text_cn": "肖申克的救赎", "release_date_year": 1994,
           "rating": 9.7, "rating_count": 100, "genres": "剧情|犯罪",
           "country": "美国|", "language": "英语"}
    out = sm.normalize_main(row, spec)
    assert out["source"] == "douban"
    assert out["source_id"] == "1292052"
    assert out["imdb_id"] == "tt0111161"
    assert out["genres"] == ["剧情", "犯罪"]
    assert out["country"] == ["美国"]


def test_describe_schema_offline():
    d = Q.describe_schema()
    assert d["main_tables"]["douban"]["table"] == "douban_main"
    assert d["cross_source_view"] == sm.CROSS_SOURCE_VIEW
    assert "douban_main" in d["all_tables"]


# ---------------------------------------------------------------------------
# 客户端降级
# ---------------------------------------------------------------------------

def test_channel_disabled_returns_none():
    ch = ProdChannel(client=FakeClient())
    ch._enabled_override = False
    # 直接改 settings 语义：enabled 由 settings.MCP_ENABLED 决定
    import config.settings as cs
    old = cs.settings.MCP_ENABLED
    cs.settings.MCP_ENABLED = False
    try:
        assert ch.search_movie("x") is None
        assert ch.get_movie_detail("douban", "1") is None
        assert ch.get_movie_relations("douban", "1") is None
        assert ch.compare_cross_source() is None
    finally:
        cs.settings.MCP_ENABLED = old


def test_channel_degrades_on_error():
    import config.settings as cs
    old = cs.settings.MCP_ENABLED
    cs.settings.MCP_ENABLED = True
    try:
        ch = ProdChannel(client=FakeClient(error=McpRateLimited(3)))
        assert ch.search_movie("x") is None, "限流必须降级为 None，不抛异常"
    finally:
        cs.settings.MCP_ENABLED = old


def test_channel_degrades_on_readonly_violation():
    import config.settings as cs
    old = cs.settings.MCP_ENABLED
    cs.settings.MCP_ENABLED = True
    try:
        ch = ProdChannel(client=FakeClient(error=McpReadOnlyViolation("x")))
        assert ch.search_movie("x") is None
    finally:
        cs.settings.MCP_ENABLED = old


def test_channel_caches_results():
    import config.settings as cs
    old = cs.settings.MCP_ENABLED
    cs.settings.MCP_ENABLED = True
    try:
        c = FakeClient({"search_movie": []})
        ch = ProdChannel(client=c)
        ch.search_movie("x")
        ch.search_movie("x")
        assert len(c.calls) == 1, "相同查询应命中缓存（降低生产库压力）"
    finally:
        cs.settings.MCP_ENABLED = old


def test_to_candidate_maps_ids():
    douban = _to_candidate({"source": "douban", "id": 5, "douban_id": "1292052",
                             "imdb_id": "tt0111161", "title": "肖申克的救赎",
                             "year": 1994, "genres": ["剧情"], "country": ["美国"],
                             "rating": 9.7, "rating_count": 100})
    assert douban.id == "douban:5"
    assert douban.imdb_id == "tt0111161"
    assert douban.recall_source == "prod"
    imdb = _to_candidate({"source": "imdb", "imdb_id": "tt0111161", "title": "x"})
    assert imdb.id == "imdb:tt0111161"
    assert _to_candidate({"source": "unknown"}) is None
    assert _to_candidate(None) is None


def test_to_candidate_work_key_merges_with_local():
    """远端候选的 work_key 应与本地同片候选一致（跨源归并生效）。"""
    from retrieval.candidates import work_key
    remote = _to_candidate({"source": "douban", "id": 5, "douban_id": "1292052",
                             "imdb_id": "tt0111161", "title": "肖申克的救赎"})
    local = type(remote)(id="douban:999", source="douban", title="肖申克的救赎",
                         imdb_id="tt0111161")
    assert work_key(remote) == work_key(local) == "imdb:tt0111161"
