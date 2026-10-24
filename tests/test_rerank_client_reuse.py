"""P2-2 回归：精排客户端复用 + 并发打分 + requests.Session。

修复前：
- llm_rerank 每次 get_light() 新建客户端，且 for 循环逐条串行 invoke——
  cloud 不可用回退本地时一次请求 30 次串行 LLM（每次还带客户端构建开销）；
- cloud_rerank 每次新建 CloudRerankClient（端点推导 + Key 校验），且直连
  requests.post 无 Session——每请求新建 TCP 连接。

修复后：客户端进程级复用（配置变更自动重建）+ 线程池并发打分 + Session 连接池。
打分/融合语义（0.7 融合分 + 0.3 相关性分、单条失败回退融合分）必须保持不变。
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import ranker.cloud_rerank as cr_mod
import ranker.llm_rerank as lr_mod
from retrieval.candidates import Candidate


def _cands(n=6):
    return [
        Candidate(id=f"douban:{i}", source="douban", title=f"片{i}", year=2000 + i,
                  genres=["剧情"], score=0.5 + 0.01 * i)
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _reset_singletons():
    lr_mod._reset_llm()
    cr_mod._reset_client()
    yield
    lr_mod._reset_llm()
    cr_mod._reset_client()


# ---------- llm_rerank：客户端复用 ----------

def test_llm_rerank_reuses_client(monkeypatch):
    built = {"n": 0}

    class _FakeLLM:
        def invoke(self, prompt):
            class _R:
                content = "0.8"
            return _R()

    def _fake_get_light():
        built["n"] += 1
        return _FakeLLM()

    monkeypatch.setattr(lr_mod, "get_light", _fake_get_light)
    lr_mod.llm_rerank(_cands(4), "科幻")
    lr_mod.llm_rerank(_cands(4), "科幻")
    assert built["n"] == 1, "客户端应进程级复用（每次请求不重建）"


def test_llm_rerank_scores_concurrently(monkeypatch):
    """并发打分：假 LLM 记录最大并发度，6 条候选应出现 >1 的并发。"""
    lock = threading.Lock()
    state = {"cur": 0, "max": 0}

    class _FakeLLM:
        def invoke(self, prompt):
            with lock:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            import time
            time.sleep(0.03)
            with lock:
                state["cur"] -= 1

            class _R:
                content = "0.5"
            return _R()

    monkeypatch.setattr(lr_mod, "get_light", lambda: _FakeLLM())
    out = lr_mod.llm_rerank(_cands(6), "科幻")
    assert len(out) == 6
    assert state["max"] > 1, f"打分未并发（最大并发 {state['max']}）"


def test_llm_rerank_score_mapping_and_fusion(monkeypatch):
    """打分语义：按 title 返回相关性分，融合分 = 0.7*原分 + 0.3*相关性分。"""

    class _FakeLLM:
        def invoke(self, prompt):
            class _R:
                # 对「片3」给 0.9，其余 0.1
                content = "0.9" if "影片：片3" in prompt else "0.1"
            return _R()

    monkeypatch.setattr(lr_mod, "get_light", lambda: _FakeLLM())
    cands = _cands(4)  # score: 0.50/0.51/0.52/0.53（片0..片3）
    out = lr_mod.llm_rerank(cands, "科幻")
    by_title = {c.title: c for c in out}
    assert by_title["片3"].score == pytest.approx(0.7 * 0.53 + 0.3 * 0.9)
    assert by_title["片0"].score == pytest.approx(0.7 * 0.50 + 0.3 * 0.1)
    assert out[0].title == "片3"  # 高相关性分应排到最前


def test_llm_rerank_single_failure_falls_back(monkeypatch):
    """单条打分失败 → 该候选回退融合分（0.7*原分+0.3*原分），不拖垮整批。"""

    class _FakeLLM:
        def invoke(self, prompt):
            if "影片：片1" in prompt:
                raise RuntimeError("llm timeout")

            class _R:
                content = "1.0"
            return _R()

    monkeypatch.setattr(lr_mod, "get_light", lambda: _FakeLLM())
    out = lr_mod.llm_rerank(_cands(3), "科幻")  # 片0/1/2 score=0.50/0.51/0.52
    by_title = {c.title: c for c in out}
    assert by_title["片1"].score == pytest.approx(0.51)  # 原分即兜底分
    assert by_title["片2"].score == pytest.approx(0.7 * 0.52 + 0.3 * 1.0)


def test_llm_rerank_no_query_sorts_by_score(monkeypatch):
    """无 query：不走 LLM，按融合分排序（语义不变）。"""
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("无 query 不应构造 LLM")

    monkeypatch.setattr(lr_mod, "get_light", _boom)
    out = lr_mod.llm_rerank(_cands(3), None)
    assert called["n"] == 0
    assert [c.score for c in out] == sorted([c.score for c in out], reverse=True)


# ---------- cloud_rerank：客户端复用 + Session ----------

def test_cloud_rerank_reuses_client(monkeypatch):
    built = {"n": 0}

    class _FakeClient:
        def __init__(self, *a, **k):
            built["n"] += 1

        @staticmethod
        def _doc_text(c):
            return c.title

        def rerank(self, query, documents, top_n=None):
            return [(i, 0.9) for i in range(len(documents))]

    monkeypatch.setattr(cr_mod, "CloudRerankClient", _FakeClient)
    out1 = cr_mod.cloud_rerank(_cands(3), "科幻")
    out2 = cr_mod.cloud_rerank(_cands(3), "科幻")
    assert built["n"] == 1, "客户端应进程级复用"
    assert len(out1) == len(out2) == 3


def test_cloud_client_uses_session(monkeypatch):
    """客户端内部用 requests.Session（连接池 keep-alive）发请求。"""
    posted = {}

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"output": {"results": [
                {"index": 1, "relevance_score": 0.8},
                {"index": 0, "relevance_score": 0.6},
            ]}}

    class _FakeSession:
        def post(self, url, data=None, headers=None, timeout=None):
            posted.update({"url": url, "headers": headers, "timeout": timeout})
            return _FakeResp()

    monkeypatch.setattr(cr_mod.requests, "Session", lambda: _FakeSession())
    client = cr_mod.CloudRerankClient(
        base_url="https://example.com/services/rerank/text-rerank/text-rerank",
        api_key="test-key",
    )
    pairs = client.rerank("科幻", ["docA", "docB"])
    assert pairs == [(1, 0.8), (0, 0.6)]
    assert posted["url"].endswith("/text-rerank")
    assert posted["headers"]["Authorization"] == "Bearer test-key"
    assert posted["timeout"] == client.timeout


def test_cloud_client_rebuilds_on_config_change(monkeypatch):
    """配置变更（模型/Key/端点）→ 客户端自动重建，不用重启。"""
    built = {"n": 0}

    class _FakeClient:
        def __init__(self, *a, **k):
            built["n"] += 1

        @staticmethod
        def _doc_text(c):
            return c.title

        def rerank(self, query, documents, top_n=None):
            return [(i, 0.5) for i in range(len(documents))]

    monkeypatch.setattr(cr_mod, "CloudRerankClient", _FakeClient)
    cr_mod.cloud_rerank(_cands(2), "q")
    assert built["n"] == 1
    monkeypatch.setattr(cr_mod.settings, "CLOUD_RERANK_MODEL", "qwen3.7-text-rerank-v2")
    cr_mod.cloud_rerank(_cands(2), "q")
    assert built["n"] == 2, "模型变更应重建客户端"
