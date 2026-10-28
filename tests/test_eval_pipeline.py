"""O9/D3：融合排序评估对象 + 评估脚本的离线测试（不依赖真实 LLM/向量库）。

通过注入 fake agent / monkeypatch 排序函数，验证：
- run_fused_ranking 正确从 graph state['ranked'] 提取候选并按 top_k 裁剪；
- evaluate(fused) 用融合排序计算 NDCG/命中/多样；
- gold 加载与脚本模块可被正确导入。
"""
import importlib.util
import os

from retrieval.candidates import Candidate

# 以脚本方式载入 scripts/eval_run.py（与命令行运行同一份代码）
_SPEC = importlib.util.spec_from_file_location(
    "eval_run_script",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "eval_run.py"),
)
eval_run = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eval_run)


def _fake_agent(cands):
    class _A:
        def invoke(self, state):
            return {"ranked": cands}
    return _A()


def test_run_fused_ranking_extracts_and_slices():
    cands = [Candidate(id=f"douban:{i}", source="douban", title=f"t{i}") for i in range(7)]
    out = eval_run.run_fused_ranking("q", top_k=5, agent=_fake_agent(cands))
    assert [c.id for c in out] == [f"douban:{i}" for i in range(5)]


def test_run_fused_ranking_empty():
    assert eval_run.run_fused_ranking("q", agent=_fake_agent([])) == []


def test_evaluate_fused_mode_uses_ranking(monkeypatch):
    gold = {"queries": [
        {"query": "科幻", "relevant": ["douban:1", "douban:3"],
         "note": "相关=标注科幻"},
    ]}
    fake = [
        Candidate(id=f"douban:{i}", source="douban", title=f"t{i}",
                  genres=(["科幻"] if i % 2 else ["喜剧"]))
        for i in range(1, 5)  # douban:1 科幻, douban:2 喜剧, douban:3 科幻, douban:4 喜剧
    ]
    monkeypatch.setattr(eval_run, "run_fused_ranking", lambda q, top_k: fake)
    res = eval_run.evaluate(gold, top_k=10, mode="fused", ranking="agent")
    assert res["mode"] == "fused"
    pq = res["per_query"][0]
    # douban:1 在首位、douban:3 在第三位 → 命中且 NDCG>0
    assert pq["hit@5"] == 1.0
    assert pq["recall@10"] == 1.0
    assert pq["ndcg@5"] > 0.0
    # 类型两两不完全相同 → 多样性介于 (0,1)
    assert 0.0 < pq["diversity@10"] < 1.0


def test_evaluate_recall_mode_wired(monkeypatch):
    gold = {"queries": [
        {"query": "q", "relevant": ["douban:9"], "note": ""},
    ]}
    fake = [Candidate(id="douban:9", source="douban", title="x", genres=["a"])]
    monkeypatch.setattr(eval_run, "content_recall", lambda query, top_k: fake)
    res = eval_run.evaluate(gold, top_k=10, mode="recall")
    assert res["mode"] == "recall"
    assert res["per_query"][0]["hit@10"] == 1.0
