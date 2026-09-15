"""云端重排序模型（阿里百炼 qwen3.7-text-rerank）接入测试。

不依赖网络的部分（URL 推导、blending 映射、rerank 管线 cloud 分支）用伪造 client 验证；
真实调用需加 --live（依赖 .env 的 CLOUD_LLM_API_KEY / 端点可达）。

运行：
  .venv/Scripts/python.exe -u tests/test_cloud_rerank.py            # 纯单测
  .venv/Scripts/python.exe -u tests/test_cloud_rerank.py --live    # 真实调用阿里百炼
"""
import sys

sys.path.insert(0, ".")

from retrieval.candidates import Candidate
from ranker.cloud_rerank import CloudRerankClient, cloud_rerank
from ranker.rerank import rerank
from config.settings import settings


def _mk(i: int, score: float) -> Candidate:
    return Candidate(
        id=f"m{i}", source="douban", title=f"片{i}", year=2020,
        genres=["剧情"], doc=f"简介{i}", score=score,
    )


def test_url_derivation():
    url = CloudRerankClient._derive_url()
    assert url.endswith("/api/v1/services/rerank/text-rerank/text-rerank"), url
    print("[OK] url derivation ->", url)


def test_cloud_rerank_mapping():
    # 伪造 rerank：相关性得分随下标递减（片0最相关）
    def fake(self, q, docs, top_n=None):
        return [(i, round(1.0 - i * 0.1, 3)) for i in range(len(docs))]

    orig = CloudRerankClient.rerank
    CloudRerankClient.rerank = fake
    try:
        cands = [_mk(0, 1.0), _mk(1, 0.9), _mk(2, 0.8)]
        out = cloud_rerank(cands, "太空科幻", top_k=3)
        assert len(out) == 3
        # 片0: 0.7*1.0 + 0.3*1.0 = 1.0；片2: 0.7*0.8 + 0.3*0.8 = 0.8
        assert abs(out[0].score - 1.0) < 1e-6, out[0].score
        assert abs(out[2].score - 0.8) < 1e-6, out[2].score
        print("[OK] cloud_rerank blending ->", [round(o.score, 3) for o in out])
    finally:
        CloudRerankClient.rerank = orig


def test_rerank_pipeline_switch():
    def fake(self, q, docs, top_n=None):
        return [(i, round(1.0 - i * 0.2, 3)) for i in range(len(docs))]

    orig = CloudRerankClient.rerank
    CloudRerankClient.rerank = fake
    try:
        recall = {"content": [_mk(0, 1.0)], "relation": [_mk(1, 0.9)]}
        # 显式 cloud provider + llm_topn>0 → 走新模型分支
        out = rerank(
            recall, strategy="mmr", top_k=5, query="太空科幻",
            llm_topn=2, rerank_provider="cloud",
        )
        assert len(out) == 2
        print("[OK] rerank(provider=cloud) top ids ->", [c.id for c in out])

        # 旧逻辑分支：provider=local 同样能跑通（不调用云端）
        out2 = rerank(
            recall, strategy="mmr", top_k=5, query="太空科幻",
            llm_topn=2, rerank_provider="local",
        )
        assert len(out2) == 2
        print("[OK] rerank(provider=local) top ids ->", [c.id for c in out2])
    finally:
        CloudRerankClient.rerank = orig


def live_test():
    print("\n=== LIVE: 真实调用阿里百炼 qwen3.7-text-rerank ===")
    cands = [_mk(0, 1.0), _mk(1, 0.9), _mk(2, 0.8)]
    out = cloud_rerank(cands, "太空科幻 孤独 人性挣扎", top_k=3)
    for o in out:
        print(f"   {o.id} | {o.title} | fused_score={o.score:.4f}")


if __name__ == "__main__":
    test_url_derivation()
    test_cloud_rerank_mapping()
    test_rerank_pipeline_switch()
    print("\n[OK] 单测全部通过（无网络）。")
    if "--live" in sys.argv:
        live_test()
