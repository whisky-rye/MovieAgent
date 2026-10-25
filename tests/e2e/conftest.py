"""后端 e2e 公共 fixture（O14 第二部分：e2e 补齐）。

设计要点：
- 强 MySQL fail-fast：app 在导入时即连 MySQL，不可达则服务拒绝启动。本套 e2e 因此
  只在 MySQL 在线 + 已 init 时运行；离线环境通过各模块 `pytestmark` 干净跳过（不崩收集）。
- /recommend、/recommend/stream 依赖 LangGraph Agent（需 ollama/云端），e2e 用 FakeAgent
  替换 build_agent，使推荐链路可在无外部 AI 服务时跑通，仅验证 HTTP 契约与响应结构。
- 精确/语义缓存、限流、海报异步调度在 e2e 中置为确定性，避免依赖 Redis/向量库/百炼。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import pytest
from sqlalchemy import create_engine, text

from config.settings import settings


def mysql_alive() -> bool:
    """探测 movie_agent 业务库是否可达（与 app 同口径，避免离线崩收集）。"""
    try:
        e = create_engine(settings.MOVIE_AGENT_MYSQL_DSN, pool_pre_ping=True)
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


class FakeAgent:
    """替代真实 LangGraph Agent，返回确定化结果，绕开 ollama/云端依赖。"""

    def __init__(self):
        self.calls = 0

    def invoke(self, state, config=None):
        self.calls += 1
        q = (state or {}).get("query", "")
        return {
            "ranked": [
                {
                    "id": "douban:1001",
                    "source": "douban",
                    "title": "测试影片A",
                    "year": 2020,
                    "genres": ["剧情"],
                    "country": "中国",
                    "rating": 8.5,
                    "rating_count": 1234,
                    "recall_source": "fusion",
                    "score": 0.91,
                    "douban_id": "1001",
                    "imdb_id": "",
                },
                {
                    "id": "imdb:tt1002",
                    "source": "imdb",
                    "title": "Test Movie B",
                    "year": 2019,
                    "genres": ["Action"],
                    "country": "美国",
                    "rating": 7.8,
                    "rating_count": 980,
                    "recall_source": "fusion",
                    "score": 0.87,
                    "douban_id": "",
                    "imdb_id": "tt1002",
                },
            ],
            "answer": f"为你推荐与「{q}」相关的影片。",
            "intent": "recommend",
            "sources": {"douban": 1, "imdb": 1},
            "steps": [{"order": 1, "node": "retrieve", "summary": "召回候选"}],
            "thinking": {},
        }


class _FakeSemCache:
    """语义缓存桩：永远不命中，避免 e2e 依赖向量库。"""

    def match(self, *a, **k):
        return None

    def index_query(self, *a, **k):
        return None


@pytest.fixture(scope="module")
def client():
    # 调试态放行：避免 e2e 强依赖登录 token（recommend/sessions 走 user_id 回退）
    # 注意：以下三项是**普通赋值**，会泄漏到本进程后续所有测试模块（pytest 按
    # 目录名字母序收集，e2e/ 先于 test_*.py）——历史上因此把
    # test_history_compress 的限流用例打成「必失败」。故在 teardown 显式恢复。
    _saved = (settings.AUTH_ENABLED, settings.RATELIMIT_ENABLED,
              settings.SEMANTIC_CACHE_ENABLED)
    settings.AUTH_ENABLED = False
    settings.RATELIMIT_ENABLED = False
    settings.SEMANTIC_CACHE_ENABLED = False

    # 用 FakeAgent 替换 build_agent，避免导入真实 Agent 时连 ollama/云端
    import agent.graph as ag

    ag.build_agent = lambda: FakeAgent()

    import api.main as m

    # 直接替换 _AGENT（模块全局），与 api.main 的导入/固化时机解耦，
    # 即使其他测试模块先 import api.main 把 _AGENT 用真实 Agent 固化也不受影响。
    m._AGENT = FakeAgent()
    # 推荐链路无关海报异步生成，置为 no-op 保持 e2e 确定性
    m.schedule_poster = lambda *a, **k: None
    # ranked 元素已是 dict，_candidate_to_dict 直接透传（仅验证 HTTP 契约）
    m._candidate_to_dict = lambda c: c
    # 语义缓存置桩，避免 e2e 依赖向量库
    m.get_sem_cache = lambda: _FakeSemCache()

    from fastapi.testclient import TestClient

    with TestClient(m.app) as c:
        yield c

    # 恢复被本 fixture 改过的全局 settings（防跨模块泄漏）
    settings.AUTH_ENABLED, settings.RATELIMIT_ENABLED, settings.SEMANTIC_CACHE_ENABLED = _saved
