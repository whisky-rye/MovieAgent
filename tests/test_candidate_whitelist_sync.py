"""防回归：Candidate 字段在三个 dict 重建器中不得静默丢失（踩过的白名单坑）。

任何新增/删除 Candidate 字段，都必须同步到 ranker/scorer._as_dict 与
ranker/llm_rerank._cand_dict / ranker/cloud_rerank._cand_dict；本测试对「每个字段」做
round-trip 校验，避免再出现 douban_id/imdb_id 跨精排丢失这类问题。
"""
import pytest

from retrieval.candidates import Candidate
from ranker.scorer import _as_dict
from ranker.llm_rerank import _cand_dict as llm_cd
from ranker.cloud_rerank import _cand_dict as cloud_cd


def _full_candidate() -> Candidate:
    return Candidate(
        id="douban:42",
        source="douban",
        title="测试影片",
        year=2020,
        genres=["科幻", "悬疑"],
        country="美国",
        rating=8.5,
        rating_count=1234,
        doc="一段简介文本",
        recall_source="graph",
        douban_id="42",
        imdb_id="tt0099",
        graph_paths=["《A》 → 导演 诺兰 → 《B》"],
        title_alt="Inception",
    )


BUILDERS = {
    "scorer._as_dict": _as_dict,
    "llm_rerank._cand_dict": llm_cd,
    "cloud_rerank._cand_dict": cloud_cd,
}

# score 由重建器在融合/精排时重新计算，不经由 dict 透传，属有意排除；
# 其余业务数据字段（含新增的 graph_paths）必须在三处 dict 重建器中保持一致。
_EXCLUDED = {"score"}


@pytest.mark.parametrize("bname,builder", list(BUILDERS.items()))
def test_all_fields_present_in_builder(bname, builder):
    c = _full_candidate()
    d = builder(c)
    for fname in Candidate.__dataclass_fields__:
        if fname in _EXCLUDED:
            continue
        assert fname in d, f"{bname} 漏掉字段 {fname}"


@pytest.mark.parametrize("bname,builder", list(BUILDERS.items()))
def test_fields_preserved_through_rebuild(bname, builder):
    c = _full_candidate()
    d = builder(c)
    rebuilt = Candidate(**{**d, "score": c.score})
    for fname in Candidate.__dataclass_fields__:
        if fname in _EXCLUDED:
            continue
        assert getattr(rebuilt, fname) == getattr(c, fname), (
            f"{bname}: 字段 {fname} 经重建后丢失/改变"
        )


def test_builders_emit_same_field_set():
    c = _full_candidate()
    keysets = {name: set(builder(c).keys()) for name, builder in BUILDERS.items()}
    assert len({frozenset(k) for k in keysets.values()}) == 1, (
        f"三个 dict 重建器字段集合不一致: {keysets}"
    )
