"""G4 偏好自动沉淀（sediment_preferences）单测：合并去重 + 历史追加。

不依赖 MySQL：用内存版 MemoryStore 子类验证合并逻辑（抽象基类方法复用 load/save）。
"""
from agent.memory import MemoryStore


class _DictStore(MemoryStore):
    def __init__(self):
        self._d = {}

    def load(self, uid):
        return self._d.get(uid, {"user_id": uid, "preferences": {}, "history": []})

    def save(self, uid, data):
        self._d[uid] = data


def test_sediment_merges_and_dedup():
    st = _DictStore()
    st.sediment_preferences("u1", genres=["科幻", "科幻"], directors=["诺兰"], people=["诺兰"])
    st.sediment_preferences(
        "u1", genres=["悬疑"], directors=["诺兰"], history_item={"query": "诺兰的电影"}
    )
    d = st.load("u1")
    assert d["preferences"]["genres"] == ["科幻", "悬疑"], d["preferences"]
    assert d["preferences"]["directors"] == ["诺兰"]
    assert d["preferences"]["people"] == ["诺兰"]
    assert len(d["history"]) == 1


def test_sediment_note_overwrite_and_no_history_when_none():
    st = _DictStore()
    st.sediment_preferences("u2", note="喜欢烧脑")
    st.sediment_preferences("u2", genres=["喜剧"], note="轻松向")
    d = st.load("u2")
    assert d["preferences"]["note"] == "轻松向"          # 覆盖写
    assert d["preferences"]["genres"] == ["喜剧"]
    assert d["history"] == []                             # 未传 history_item 不追加


def test_sediment_empty_is_noop():
    st = _DictStore()
    st.sediment_preferences("u3")  # 全 None
    d = st.load("u3")
    assert d["preferences"] == {}
    assert d["history"] == []
