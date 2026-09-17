"""历史会话外链回填（api/items_backfill.py）单测。

验证：
- imdb:{imdb_id} 直接解析补充 imdb_id（零查询）；
- douban:{PK} 已从 meta 带出 douban_id/imdb_id 时原样保留；
- 豆瓣需按主键回查时，fetch_all 正常路径补全；
- 数据库不可用时静默降级（返回原列表，不抛）。
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.items_backfill as ib


def test_imdb_id_parsed():
    items = [{"id": "imdb:tt123", "title": "X"}]
    out = ib.backfill_item_ids(items)
    assert out[0]["imdb_id"] == "tt123"


def test_douban_id_passthrough_if_present():
    items = [{"id": "douban:5", "douban_id": "777", "imdb_id": "tt9"}]
    out = ib.backfill_item_ids(items)
    assert out[0]["douban_id"] == "777" and out[0]["imdb_id"] == "tt9"


def test_douban_backfill_via_fetch(monkeypatch):
    items = [{"id": "douban:123", "title": "Y"}]
    monkeypatch.setattr(
        ib, "fetch_all",
        lambda sql, params: [{"id": 123, "douban_id": "456", "imdb_id": "tt789"}],
    )
    out = ib.backfill_item_ids(items)
    assert out[0]["douban_id"] == "456" and out[0]["imdb_id"] == "tt789"


def test_db_error_degrades(monkeypatch):
    items = [{"id": "douban:123", "title": "Y"}]

    def boom(sql, params):
        raise RuntimeError("db down")

    monkeypatch.setattr(ib, "fetch_all", boom)
    out = ib.backfill_item_ids(items)  # 不应抛，返回原列表
    assert out is items


def test_empty():
    assert ib.backfill_item_ids([]) == []
