"""P4.2 双后端缓存（api/cache.py）单测。

验证 DualCache 在「Redis 不可达」时自动回退进程内 TTL 缓存：
- use_redis=False / backend()=='local'；
- set/get 往返；
- 缺失 key 返回 None；
- TTL=0 立即过期；
- cache_key 对多段输入做确定性 md5 且带 recall: 前缀。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.cache import DualCache, cache_key


def _local_cache(ttl=300):
    # 连不上 Redis 的地址 -> 自动走本地兜底
    return DualCache(host="127.0.0.1", port=1, db=0, password=None, ttl=ttl, protocol=2)


def test_local_fallback_active():
    c = _local_cache()
    assert c.use_redis is False
    assert c.backend() == "local"


def test_set_get():
    c = _local_cache()
    c.set("k", {"a": 1})
    assert c.get("k") == {"a": 1}


def test_get_missing():
    c = _local_cache()
    assert c.get("missing") is None


def test_ttl_expiry():
    c = _local_cache(ttl=0)  # 立即可过期
    c.set("k", "v")
    assert c.get("k") is None


def test_cache_key_deterministic():
    k1 = cache_key("a", "b")
    k2 = cache_key("a", "b")
    assert k1 == k2
    assert k1.startswith("recall:")
    assert cache_key("a", "b") != cache_key("a", "c")
