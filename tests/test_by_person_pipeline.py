"""按导演/演员找作品（by_person）管线单测：意图解析 → 规划 → runner。

不依赖 MySQL / LLM：_heuristic / _plan 为纯函数；runner 用假 graph store 注入。
"""
import graph.recall as recall_mod
from agent import graph as graph_mod
from agent.tools import _run_search_by_person


def test_heuristic_person_query():
    s = graph_mod._heuristic("诺兰的电影")
    assert s["intent"] == "by_person"
    s2 = graph_mod._heuristic("周星驰演过的喜剧")
    assert s2["intent"] == "by_person"
    # 普通推荐不应被误判
    s3 = graph_mod._heuristic("太空科幻 孤独感")
    assert s3["intent"] == "recommend"


def test_plan_by_person():
    state = {"intent": "by_person", "query": "诺兰", "slots": {"query": "诺兰"}}
    out = graph_mod._plan(state)
    assert out["plan"] == ["search_by_person"]
    # 人名兜底：确保 runner 能拿到 name
    assert out["slots"].get("name") == "诺兰"


def test_parse_intent_corrects_person_query(monkeypatch):
    # LLM 误判为 recommend 时，关键词矫正应把它纠正为 by_person（验证白名单+矫正）
    class _FakeResp:
        content = '{"intent":"recommend","query":"诺兰的电影","genres":[],"country":"","min_year":0,"item_hint":""}'

    class _FakeLLM:
        def invoke(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(graph_mod, "chat_light", lambda *a, **k: _FakeLLM())
    out = graph_mod._parse_intent({"user_id": "u", "query": "诺兰的电影", "request_id": ""})
    assert out["intent"] == "by_person"


class _FakeHit:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeStore:
    def person_filmography(self, name, top_k=20):
        if name == "诺兰":
            return [_FakeHit(
                item_id="douban:1", source="douban", title="星际穿越", year=2014,
                genres=["科幻", "冒险"], country="美国", rating=9.4, rating_count=1000,
                douban_id="1", imdb_id="tt0816692", paths=["人物:诺兰 → 署名 → 《星际穿越》"],
                score=0.9,
            )]
        return []


def test_run_search_by_person_fake_store(monkeypatch):
    monkeypatch.setattr(recall_mod, "get_graph_store", lambda: _FakeStore())
    cands = _run_search_by_person({"name": "诺兰", "query": "诺兰"}, {}, top_k=5)
    assert len(cands) == 1
    c = cands[0]
    assert c.title == "星际穿越"
    assert c.recall_source == "person"
    assert c.id == "douban:1"


def test_run_search_by_person_empty_name(monkeypatch):
    monkeypatch.setattr(recall_mod, "get_graph_store", lambda: _FakeStore())
    assert _run_search_by_person({"name": "", "query": ""}, {}, top_k=5) == []


def test_sediment_preferences_wires_person(monkeypatch):
    """graph 层 _sediment_preferences 在 by_person 意图下，应把人名写入 people。"""
    class _FakeStore:
        def __init__(self):
            self.calls = []

        def load(self, uid):
            return {"user_id": uid, "preferences": {}, "history": []}

        def save(self, uid, data):
            self.last = data

        def sediment_preferences(self, uid, **kw):
            self.calls.append((uid, kw))

    fs = _FakeStore()
    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: fs)
    # chat_light 返回 None -> _extract_prefs_llm 内部 .invoke 抛错被捕获 -> 返回 {}（离线安全）
    monkeypatch.setattr(graph_mod, "chat_light", lambda *a, **k: None)

    state = {
        "user_id": "u7", "intent": "by_person", "query": "诺兰的电影",
        "slots": {"name": "诺兰", "query": "诺兰"}, "ranked": [],
    }
    graph_mod._sediment_preferences(state)
    assert fs.calls, "应当调用 sediment_preferences"
    kw = fs.calls[0][1]
    assert kw["people"] == ["诺兰"]
    assert kw["history_item"]["intent"] == "by_person"
