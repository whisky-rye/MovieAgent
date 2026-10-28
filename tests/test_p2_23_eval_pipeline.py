"""P2-23 回归测试：评估改走纯 ranker 注入（无 Agent/无 LLM/无副作用）+ 沉淀跳过 eval 用户。"""
import importlib.util
import os

from retrieval.candidates import Candidate

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eval_run = _load_script("eval_run", "scripts/eval_run.py")


def test_run_fused_ranking_injects_pure_ranking_fn():
    from eval.pipeline import run_fused_ranking
    injected = [Candidate(id="douban:9", source="douban", title="x")]
    out = run_fused_ranking("q", top_k=5, ranking_fn=lambda q, k: injected)
    assert out is injected  # 直接返回注入结果，不构建 Agent / 不调 LLM
    assert [c.id for c in out] == ["douban:9"]


def test_graph_sedimentation_skips_eval_user(monkeypatch):
    import agent.graph as gmod
    calls = {"n": 0}

    def _fake(*a, **k):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(gmod, "_extract_prefs_llm", _fake)
    gmod._sediment_preferences({"user_id": "eval", "query": "x"})
    assert calls["n"] == 0  # eval 用户直接 return，不触发抽取
    gmod._sediment_preferences({"user_id": "real_user", "query": "x"})
    assert calls["n"] == 1  # 真实用户仍触发，跳过逻辑精准不误伤
