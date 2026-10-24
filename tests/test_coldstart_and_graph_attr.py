"""P2-4 回归：冷启动榜单 TTL 缓存 + 图谱类型扩展走维度关联表。

修复前：
- coldstart_recall 的 ORDER BY (rating*(1+LOG10(1+rating_count))) 是计算表达式
  无索引，每请求全表扫描 + filesort；
- graph _attr_relations 类型扩展对 douban_main/imdb_main 各做一次
  `genres LIKE '%name%'` 全表扫描（且子串误匹配「动画」→「动画电影」），
  豆瓣路径还逐行回查主键（40 行 = 40 次往返）。

修复后：榜单按查询键 TTL 缓存（每 TTL 至多一次扫描，SQL 语义不变）；
类型扩展走 {src}_genre_relate + {src}_genre 等值 JOIN（key 直接由 JOIN
主键构造），维度表缺失自动回退 LIKE；地区维度维持 LIKE（名称口径不一致）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib

import pytest

import config.settings as cs
from graph.store import MysqlGraphFetcher

# importlib 取子模块（retrieval/__init__ 把同名函数重导出，「from retrieval
# import coldstart_recall」会绑定到函数而非模块）
csr = importlib.import_module("retrieval.coldstart_recall")

_BOARD_ROWS = [
    {"id": 1, "douban_id": "1", "imdb_id": "tt1", "title_text_cn": "甲片",
     "original_title": None, "release_date_year": 2001, "rating": 9.0,
     "rating_count": 100, "genres": "剧情/喜剧", "country": "中国"},
    {"id": 2, "douban_id": "2", "imdb_id": "", "title_text_cn": "乙片",
     "original_title": None, "release_date_year": 2002, "rating": 8.0,
     "rating_count": 50, "genres": "动作", "country": "美国"},
]


@pytest.fixture
def board_fetch(monkeypatch):
    """受控榜单行 + 调用计数；每个用例前清缓存。"""
    csr._BOARD_CACHE.clear()
    state = {"n": 0, "sql": ""}

    def _fetch(sql, params):
        state["n"] += 1
        state["sql"] = sql
        state["params"] = dict(params or {})
        return _BOARD_ROWS

    monkeypatch.setattr(csr, "fetch_all", _fetch)
    yield state
    csr._BOARD_CACHE.clear()


# ---------- 冷启动：TTL 缓存 ----------

def test_board_cached_within_ttl(board_fetch, monkeypatch):
    monkeypatch.setattr(cs.settings, "COLDSTART_BOARD_TTL_S", 300)
    a = csr.coldstart_recall(top_k=5)
    b = csr.coldstart_recall(top_k=5)
    assert board_fetch["n"] == 1, "TTL 内第二次应命中缓存（不再扫表）"
    assert [c.id for c in a] == [c.id for c in b]  # 结果一致


def test_board_ttl_zero_disables_cache(board_fetch, monkeypatch):
    monkeypatch.setattr(cs.settings, "COLDSTART_BOARD_TTL_S", 0)
    csr.coldstart_recall(top_k=5)
    csr.coldstart_recall(top_k=5)
    assert board_fetch["n"] == 2, "TTL=0 应关闭缓存（每请求都查）"


def test_board_cache_key_includes_sql_filters(board_fetch, monkeypatch):
    """缓存键 = SQL 实际过滤条件（country/min_year/limit）；genres 是 Python 侧
    过滤、不影响 SQL，故不进键（同键命中后仍会按各自 genres 过滤，语义正确）。"""
    monkeypatch.setattr(cs.settings, "COLDSTART_BOARD_TTL_S", 300)
    csr.coldstart_recall(top_k=5)
    csr.coldstart_recall(top_k=5, genres=["动作"])     # 同键 → 命中缓存
    csr.coldstart_recall(top_k=5, country="中国")      # country 进 SQL → 新键
    csr.coldstart_recall(top_k=9)                      # limit 进 SQL → 新键
    assert board_fetch["n"] == 3


def test_board_sql_contract_unchanged(board_fetch):
    """SQL 契约不变：同一计算表达式 ORDER BY + LIMIT 参数化（缓存不改变语义）。"""
    csr.coldstart_recall(top_k=7)
    assert "ORDER BY (rating * (1 + LOG10(1 + rating_count))) DESC" in board_fetch["sql"]
    assert board_fetch["params"]["lim"] == 28  # top_k * 4
    assert "imdb_id" in board_fetch["sql"]      #  latent 修复：SELECT 补 imdb_id


def test_board_genre_filter_still_applied(board_fetch):
    """Python 侧类型过滤保持（语义与旧实现一致）。"""
    out = csr.coldstart_recall(top_k=5, genres=["动作"])
    assert [c.title for c in out] == ["乙片"]


def test_board_carries_imdb_id(board_fetch):
    """豆瓣候选带 imdb_id（P1-6 归并键）——此前 SELECT 漏列导致恒为空。"""
    out = csr.coldstart_recall(top_k=5)
    by_id = {c.id: c for c in out}
    assert by_id["douban:1"].imdb_id == "tt1"
    assert by_id["douban:2"].imdb_id == ""


# ---------- 图谱：类型扩展走关联表 ----------

def _make_fetcher(fetch_impl):
    f = object.__new__(MysqlGraphFetcher)  # 绕过 __init__ 建连探测
    f._fetch = fetch_impl
    return f


def test_genre_attr_uses_relate_tables():
    """类型扩展走 genre_relate 等值 JOIN；影片 key 由 JOIN 主键直接构造。"""
    seen = []

    def _fetch(sql, params):
        seen.append(sql)
        if "genre_relate" in sql:
            # 豆瓣行给 pk、imdb 行给 iid（各自源取各自键）
            return ([{"pk": 10, "iid": None}] if "douban_genre_relate" in sql
                    else [{"pk": None, "iid": "tt1"}])
        return []

    f = _make_fetcher(_fetch)
    rels = f._attr_relations("genre:科幻", "genre")
    assert {r.obj for r in rels} == {"douban:10", "imdb:tt1"}
    assert len(seen) == 2 and all("genre_relate" in s for s in seen)
    assert "LIKE" not in seen[0], "类型扩展不应再 LIKE 全表扫描"


def test_genre_attr_falls_back_to_like_on_missing_tables():
    """维度表缺失（开发库未建）→ 自动回退 LIKE，图谱不塌。"""
    calls = {"relate": 0, "like": 0}

    def _fetch(sql, params):
        if "genre_relate" in sql:
            calls["relate"] += 1
            raise RuntimeError("Table 'douban_genre_relate' doesn't exist")
        calls["like"] += 1
        return ([{"pk": 11, "iid": None}] if calls["like"] == 1
                else [{"pk": None, "iid": "tt2"}])

    f = _make_fetcher(_fetch)
    rels = f._attr_relations("genre:科幻", "genre")
    assert {r.obj for r in rels} == {"douban:11", "imdb:tt2"}
    assert calls["relate"] == 2 and calls["like"] == 2


def test_country_attr_still_uses_like():
    """地区维度维持 LIKE（dim 短名与 main.country 管道串口径不一致，等值会丢边）。"""
    seen = []

    def _fetch(sql, params):
        seen.append(sql)
        if "SELECT id FROM douban_main WHERE douban_id" in sql:
            return [{"id": 77}]
        return [{"sid": 5, "rating": 8.0, "rate_agg": 0}]

    f = _make_fetcher(_fetch)
    rels = f._attr_relations("country:中国", "country")
    assert rels and rels[0].obj == "douban:77"
    assert rels[0].pred == "from_country"
    assert any("country LIKE" in s for s in seen)


def test_genre_attr_exact_match_no_substring():
    """等值匹配消除子串误扩：'动画' 不再命中 '动画电影'（由 SQL 形态保证）。"""
    seen = []

    def _fetch(sql, params):
        seen.append((sql, dict(params or {})))
        return []

    f = _make_fetcher(_fetch)
    f._attr_relations("genre:动画", "genre")
    assert seen and seen[0][1].get("name") == "动画"  # 参数是精确类型名而非 %..%
