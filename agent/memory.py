"""偏好记忆模块：开发期用本地文件(JSON)，预留 Redis 抽象。

为什么文件而非 Redis：开发期零依赖、可离线、便于 Debug；
上线期切 RedisMemoryStore（见架构文档第 8 节 D4），业务层只依赖 MemoryStore 抽象。
"""
import json
import os
import threading
from abc import ABC, abstractmethod
from typing import Optional


class MemoryStore(ABC):
    @abstractmethod
    def load(self, user_id: str) -> dict: ...

    @abstractmethod
    def save(self, user_id: str, data: dict) -> None: ...

    def get_prefs(self, user_id: str) -> dict:
        return self.load(user_id).get("preferences", {})

    def add_pref(self, user_id: str, genres: list[str] | None = None,
                 note: str | None = None) -> dict:
        d = self.load(user_id)
        prefs = d.setdefault("preferences", {})
        if genres:
            cur = prefs.setdefault("genres", [])
            for g in genres:
                if g and g not in cur:
                    cur.append(g)
        if note:
            prefs["note"] = note
        self.save(user_id, d)
        return prefs

    def push_history(self, user_id: str, item: dict) -> None:
        d = self.load(user_id)
        d.setdefault("history", []).append(item)
        self.save(user_id, d)


class FileMemoryStore(MemoryStore):
    def __init__(self, root: str = "data/memory"):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, uid: str) -> str:
        return os.path.join(self.root, f"{uid}.json")

    def load(self, uid: str) -> dict:
        p = self._path(uid)
        if not os.path.exists(p):
            return {"user_id": uid, "preferences": {}, "history": []}
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def save(self, uid: str, data: dict) -> None:
        with self._lock:
            with open(self._path(uid), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)


class RedisMemoryStore(MemoryStore):
    """上线期预留：Redis 存储（短/长期偏好 + 历史 + 限流）。开发期不启用。"""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "RedisMemoryStore 为上线期预留；开发期请使用 FileMemoryStore"
            "（见 agent_architecture_and_stack.md 第 8 节 D4）。"
        )

    def load(self, user_id: str) -> dict: raise NotImplementedError
    def save(self, user_id: str, data: dict) -> None: raise NotImplementedError


_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """工厂：开发期固定返回 FileMemoryStore（上线期可改 Redis）。"""
    global _store
    if _store is None:
        _store = FileMemoryStore()
    return _store
