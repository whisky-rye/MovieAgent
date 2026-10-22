"""Q3/O20 跨源中英文名对齐去重：同一作品在豆瓣与 IMDb 双源各一份时，
通过 imdb_id 归并为单一结果，保留双源外链(douban_id/imdb_id)与异名(title_alt)。

不依赖 MySQL（纯 Candidate / fuse / rrf_fuse 单元验证）。
"""
import pytest

from retrieval.candidates import Candidate, work_key, merge_candidate, _richer
from ranker.rrf import rrf_fuse
from ranker.scorer import fuse


def _douban_incept():
    return Candidate(
        id="douban:100", source="douban", title="盗梦空间",
        year=2010, genres=["科幻", "悬疑"], rating=9.2, rating_count=200,
        douban_id="100", imdb_id="tt1375666", recall_source="content",
    )


def _imdb_incept():
    return Candidate(
        id="imdb:tt1375666", source="imdb", title="Inception",
        year=2010, genres=["Sci-Fi"], rating=8.8, rating_count=50,
        imdb_id="tt1375666", recall_source="relation",
    )


def test_work_key_collapses_same_imdb_id():
    d, i = _douban_incept(), _imdb_incept()
    assert work_key(d) == work_key(i) == "imdb:tt1375666"


def test_rrf_fuse_cross_source_dedup():
    ranked = rrf_fuse({"search_by_text": [_douban_incept(), _imdb_incept()]}, top_k=10)
    assert len(ranked) == 1
    c = ranked[0]
    # 代表候选：豆瓣（双源外链齐全、样本更多、中文 UI 优先）
    assert c.id == "douban:100"
    assert c.title == "盗梦空间"
    assert c.title_alt == "Inception"          # 异名对齐（中文名 ↔ 英文名）
    assert c.douban_id == "100"
    assert c.imdb_id == "tt1375666"
    assert set(c.recall_source.split("|")) == {"content", "relation"}


def test_fuse_cross_source_dedup():
    ranked = fuse({"content": [_douban_incept(), _imdb_incept()]}, top_k=10)
    assert len(ranked) == 1
    c = ranked[0]
    assert c.title_alt == "Inception"
    assert c.douban_id == "100" and c.imdb_id == "tt1375666"


@pytest.mark.parametrize("strategy", ["rrf", "score"])
def test_cross_source_explain_map_keyed_by_repr_id(strategy):
    """跨源合并后 explain_map 必须以代表候选 id 为键（前端按 id 取融合详情）。"""
    if strategy == "rrf":
        ranked, emap = rrf_fuse(
            {"search_by_text": [_douban_incept(), _imdb_incept()]}, explain=True)
    else:
        ranked, emap = fuse(
            {"content": [_douban_incept(), _imdb_incept()]}, explain=True)
    assert len(ranked) == 1
    assert ranked[0].id in emap
    assert emap[ranked[0].id]["rank"] == 1


def test_pure_same_source_not_merged():
    c1 = Candidate(id="douban:1", source="douban", title="A", rating=8,
                   rating_count=10, recall_source="content")
    c2 = Candidate(id="douban:2", source="douban", title="B", rating=7,
                   rating_count=5, recall_source="relation")
    ranked = rrf_fuse({"search_by_text": [c1], "high_quality_board": [c2]}, top_k=10)
    assert len(ranked) == 2
    assert {c.id for c in ranked} == {"douban:1", "douban:2"}


def test_merge_prefers_double_linked_candidate():
    # 两候选 imdb_id 相同；A 有双源 id、B 仅 imdb_id → A 应为代表
    a = Candidate(id="douban:5", source="douban", title="中文名", douban_id="5",
                  imdb_id="tt9", rating_count=100, recall_source="content")
    b = Candidate(id="imdb:tt9", source="imdb", title="English Name", imdb_id="tt9",
                  rating_count=10, recall_source="relation")
    merged = merge_candidate(a, b)
    assert merged.id == "douban:5"
    assert merged.douban_id == "5" and merged.imdb_id == "tt9"
    assert merged.title_alt == "English Name"


def test_merge_union_ids_when_single_side_has_them():
    # 代表仅 douban 侧有 imdb_id，c 仅 imdb 侧有 douban_id → 合并后双源齐全
    base = Candidate(id="douban:7", source="douban", title="片A", imdb_id="tt7",
                     recall_source="content")
    c = Candidate(id="imdb:tt7", source="imdb", title="Movie A", douban_id="7",
                  recall_source="relation")
    merged = merge_candidate(base, c)
    assert merged.douban_id == "7" and merged.imdb_id == "tt7"
    assert merged.title_alt == "Movie A"


def test_no_false_merge_when_imdb_ids_differ():
    """不同 imdb_id 的影片必须不合并（避免错配）。"""
    x = Candidate(id="imdb:tt1", source="imdb", title="X", imdb_id="tt1",
                  recall_source="content")
    y = Candidate(id="imdb:tt2", source="imdb", title="Y", imdb_id="tt2",
                  recall_source="relation")
    ranked = rrf_fuse({"search_by_text": [x, y]}, top_k=10)
    assert len(ranked) == 2


def test_richer_favors_double_linked():
    a = Candidate(id="douban:1", source="douban", title="a", douban_id="1", imdb_id="tt1")
    b = Candidate(id="imdb:tt1", source="imdb", title="b", imdb_id="tt1")
    assert _richer(a, b) is True
    assert _richer(b, a) is False
