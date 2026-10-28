"""P2-22 回归测试：gold 覆盖度 + 多样性虚高修复 + 新增 MRR/MAP/位置偏差。"""
import importlib.util
import os

from retrieval.candidates import Candidate

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build_gold_mod = _load_script("build_gold", "scripts/build_gold.py")
eval_run = _load_script("eval_run", "scripts/eval_run.py")


def test_build_gold_synthetic_coverage():
    gold = build_gold_mod.build_gold(limit_per_query=10, synthetic=True)
    qs = gold["queries"]
    assert len(qs) >= 50, f"gold 应 ≥50 条，实际 {len(qs)}"
    intents = {q["intent"] for q in qs}
    expected = {"genre_douban", "genre_imdb", "similar", "by_person",
                "board_douban", "board_imdb"}
    assert expected.issubset(intents), f"意图覆盖不全：{intents}"
    all_ids = [i for q in qs for i in q["relevant"]]
    assert any(i.startswith("douban:") for i in all_ids)
    assert any(i.startswith("imdb:") for i in all_ids)
    assert all("intent" in q for q in qs)


def test_metrics_mrr_map_fp_present():
    from eval import metrics
    assert metrics.mrr_at_k(["a", "b"], {"a"}, 10) == 1.0
    assert metrics.map_at_k(["a", "x", "b"], {"a", "b"}, 10) > 0.0
    assert metrics.first_relevant_position(["x", "a"], {"a"}, 10) == 2


def test_diversity_none_excluded_from_mean():
    """diversity 无信息时返回 None，eval_run 聚合须剔除而非当 1.0。"""
    gold = {"queries": [
        {"query": "q1", "relevant": ["douban:1"], "intent": "genre_douban", "note": ""},
        {"query": "q2", "relevant": ["douban:2"], "intent": "genre_douban", "note": ""},
    ]}
    fake = [
        Candidate(id="douban:1", source="douban", title="t1", genres=[]),
        Candidate(id="douban:2", source="douban", title="t2", genres=[]),
    ]
    orig = eval_run.run_fused_ranking
    eval_run.run_fused_ranking = lambda q, top_k=20, user_id="eval", agent=None, ranking_fn=None: (
        [fake[0]] if q == "q1" else [fake[1]]
    )
    try:
        res = eval_run.evaluate(gold, top_k=10, mode="fused", ranking="agent")
    finally:
        eval_run.run_fused_ranking = orig
    agg = res["aggregate"]
    assert agg["diversity@10"] is None, f"diversity 应剔除为 None，实际 {agg['diversity@10']}"
    assert agg["n_diversity"] == 0
    for key in ("mrr@10", "map@10", "fp@10"):
        assert key in agg
