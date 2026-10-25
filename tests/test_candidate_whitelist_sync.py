"""防回归：Candidate 字段在 dict 重建器中不得静默丢失（踩过的白名单坑）。

任何新增/删除 Candidate 字段，都必须同步到 ranker/scorer._as_dict 与
ranker/llm_rerank._cand_dict / ranker/cloud_rerank._cand_dict；本测试对「每个字段」做
round-trip 校验，避免再出现 douban_id/imdb_id 跨精排丢失这类问题。

P2-12：api/main.py 的 _candidate_to_dict（API 序列化）/ _dict_to_candidate（语义
缓存回包复原）也纳入同一校验——graph_paths（可解释关系链）曾在这两处漏字段，
穿过 ranker 链后被静默丢弃，前端永远拿不到。api.main import 即探 MySQL，
离线环境（本地库不可达）自动跳过这两项，ranker 三项离线照常校验。
"""
import pytest

from retrieval.candidates import Candidate
from ranker.scorer import _as_dict
from ranker.llm_rerank import _cand_dict as llm_cd
from ranker.cloud_rerank import _cand_dict as cloud_cd

# P2-12：api.main 需要 MySQL 在线才能 import（import 时即 fail-fast 探测）
try:
    import api.main as _api_main
    _API_TO_DICT = _api_main._candidate_to_dict
    _API_FROM_DICT = _api_main._dict_to_candidate
    _API_OK = True
except Exception:  # noqa: BLE001 - 离线环境跳过 api 层校验（ranker 三项不受影响）
    _API_OK = False


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

# api/main.py 转换器的额外有意排除：doc 是大字段，API payload 有意不带
#（_dict_to_candidate 复原时置空——语义缓存复用候选只需元数据）。
_API_EXTRA_EXCLUDED = {"doc"}


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


# ---------- P2-12：api/main.py 序列化/回包转换（需 MySQL 在线） ----------

@pytest.mark.skipif(not _API_OK, reason="api.main 需要 MySQL 在线（import 即探测）")
def test_api_to_dict_has_all_fields():
    """API 序列化 dict 必须带全 Candidate 字段（ranker 键集减去 doc 后的超集）。

    doc 是有意排除：简介是大字段，API payload 不带（_dict_to_candidate 复原时置空，
    语义缓存复用候选只需元数据，doc 不影响解说生成）——与 ranker 侧排除 score 同理。
    """
    d = _API_TO_DICT(_full_candidate())
    ranker_keys = set(_as_dict(_full_candidate()).keys()) - _API_EXTRA_EXCLUDED
    assert ranker_keys <= set(d.keys()), (
        f"main._candidate_to_dict 缺少字段: {ranker_keys - set(d.keys())}"
    )


@pytest.mark.skipif(not _API_OK, reason="api.main 需要 MySQL 在线（import 即探测）")
def test_api_round_trip_preserves_graph_paths():
    """c → _candidate_to_dict → _dict_to_candidate 全字段保持（score/doc 除外）。"""
    c = _full_candidate()
    rebuilt = _API_FROM_DICT(_API_TO_DICT(c))
    for fname in Candidate.__dataclass_fields__:
        if fname in _EXCLUDED or fname in _API_EXTRA_EXCLUDED:
            continue
        assert getattr(rebuilt, fname) == getattr(c, fname), (
            f"API 序列化 round-trip: 字段 {fname} 丢失/改变"
        )
    # graph_paths 专项（P2-12 的问题字段）：非空时必须原样穿过两层转换
    assert rebuilt.graph_paths == c.graph_paths


@pytest.mark.skipif(not _API_OK, reason="api.main 需要 MySQL 在线（import 即探测）")
def test_api_dict_without_graph_paths_defaults_empty():
    """旧缓存payload（无 graph_paths 键）不得炸——默认空列表。"""
    d = _API_TO_DICT(_full_candidate())
    d.pop("graph_paths", None)
    rebuilt = _API_FROM_DICT(d)
    assert rebuilt.graph_paths == []
