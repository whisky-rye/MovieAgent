"""P1-1 偏好类型闸门回归：垃圾类型绝不入库。

背景：曾用正则 `[\u4e00-\u9fa5]{2,3}` 从用户原话切"类型」，「我喜欢周星驰的电影」
会切出 ["我喜欢","周星驰","的电"] 落库；preferences.genres 会作为内容召回过滤条件下推
（vector backend 按类型集合求交），垃圾类型匹配不到任何影片 → 召回空结果、
推荐质量静默劣化，且垃圾偏好永久留存。

修复：retrieval.candidates.normalize_genres 词表闸门，所有写入路径
（add_pref / sediment_preferences / graph 偏好意图）统一过闸。

覆盖（离线，不需要 MySQL）：
1. normalize_genres 纯函数：标准类型保留、垃圾丢弃、去重保序、None 安全；
2. MemoryStore.add_pref / sediment_preferences 写入前过滤（ABC 默认路径）；
3. graph._generate 偏好意图不再正则切字，仅落词表内类型。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import graph as graph_mod
from agent.memory import MemoryStore
from retrieval.candidates import KNOWN_GENRES, normalize_genres


# ---------- 1) 纯函数 ----------

def test_normalize_genres_keeps_standard_and_drops_garbage():
    out = normalize_genres(["科幻", "我喜欢", "周星驰", "的电", "悬疑", ""])
    assert out == ["科幻", "悬疑"]


def test_normalize_genres_dedup_and_order():
    out = normalize_genres(["喜剧", "科幻", "喜剧", " 科幻 "])
    assert out == ["喜剧", "科幻"]


def test_normalize_genres_none_and_empty_safe():
    assert normalize_genres(None) == []
    assert normalize_genres([]) == []


def test_known_genres_covers_common_types():
    for g in ("剧情", "喜剧", "科幻", "爱情", "动画", "纪录片"):
        assert g in KNOWN_GENRES


# ---------- 2) 写入路径过滤（内存假 store，走 ABC 默认 update） ----------

class _MemStore(MemoryStore):
    """最小内存实现：验证 ABC 层（add_pref/sediment）的闸门与原子 update 契约。"""

    def __init__(self):
        self.rows = {}

    def load(self, user_id):
        return self.rows.get(user_id) or {"user_id": user_id, "preferences": {}, "history": []}

    def save(self, user_id, data):
        self.rows[user_id] = data


def test_add_pref_filters_garbage():
    st = _MemStore()
    st.add_pref("u1", genres=["我喜欢", "周星驰", "的电", "喜剧"])
    assert st.get_prefs("u1")["genres"] == ["喜剧"]


def test_add_pref_all_garbage_writes_nothing():
    st = _MemStore()
    st.add_pref("u1", genres=["我喜欢", "周星驰"])
    assert st.get_prefs("u1").get("genres") in (None, [])


def test_sediment_filters_garbage_but_keeps_people():
    st = _MemStore()
    st.sediment_preferences("u1", genres=["我喜欢", "科幻"], people=["诺兰"])
    prefs = st.get_prefs("u1")
    assert prefs["genres"] == ["科幻"]
    assert prefs["people"] == ["诺兰"]  # 人名自由文本不过词表


# ---------- 3) graph 偏好意图：不再正则切字 ----------

class _FakeStore:
    def __init__(self):
        self.calls = []

    def get_prefs(self, uid):
        return {}

    def add_pref(self, uid, genres=None, note=None):
        self.calls.append({"genres": genres, "note": note})
        return {"genres": genres or []}


def test_generate_preference_intent_drops_regex_garbage(monkeypatch):
    fs = _FakeStore()
    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: fs)
    state = {
        "user_id": "u1", "intent": "preference",
        "query": "我喜欢周星驰的电影",
        "slots": {"query": "我喜欢周星驰的电影",
                  "genres": ["我喜欢", "周星驰", "的电", "喜剧"]},
    }
    out = graph_mod._generate(state)
    # 仅词表内的「喜剧」入库；正则片段绝不出现
    assert fs.calls and fs.calls[0]["genres"] == ["喜剧"]
    assert "喜剧" in out["answer"]
    assert "周星驰" not in out["answer"]  # 人名不进 genres 过滤链


def test_generate_preference_intent_no_genres_no_garbage(monkeypatch):
    """LLM 未给出类型时不再正则兜底——写 None（无偏好可记），而非垃圾。"""
    fs = _FakeStore()
    monkeypatch.setattr(graph_mod, "get_memory_store", lambda: fs)
    state = {
        "user_id": "u1", "intent": "preference",
        "query": "记住我喜欢周星驰的电影",
        "slots": {"query": "记住我喜欢周星驰的电影", "genres": []},
    }
    out = graph_mod._generate(state)
    assert fs.calls and fs.calls[0]["genres"] is None
    assert "未提取到具体类型" in out["answer"]
