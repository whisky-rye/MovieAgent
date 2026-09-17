"""可观测性（config/logging_setup.py + config/langsmith_setup.py）单测。

验证：
- JsonFormatter 把 msg + extra 维度序列化为合法 JSON（含 ts/level/logger/msg + 自定义字段）；
- run_metadata 产出固定标签集合且可附加标量；
- setup_langsmith 在关闭/缺 Key 时返回 False（不抛、不影响业务）。
"""
import os
import sys
import json
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.langsmith_setup as ls
from config.logging_setup import JsonFormatter


def test_json_formatter_with_extra():
    fmt = JsonFormatter()
    rec = logging.LogRecord("movie_agent.test", logging.INFO, __file__, 1, "hello", None, None)
    rec.request_id = "r1"
    rec.event = "evt"
    rec.user_id = "u1"
    d = json.loads(fmt.format(rec))
    assert d["msg"] == "hello" and d["request_id"] == "r1" and d["event"] == "evt"


def test_json_formatter_plain():
    fmt = JsonFormatter()
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "warn", None, None)
    d = json.loads(fmt.format(rec))
    assert d["level"] == "WARNING" and "msg" in d


def test_run_metadata_keys():
    md = ls.run_metadata(user_id="u1", request_id="r2")
    for k in ("app", "app_env", "llm_provider", "fusion_strategy", "vector_backend",
              "embed_provider", "user_id", "request_id"):
        assert k in md
    assert md["user_id"] == "u1"


def test_run_metadata_extra():
    md = ls.run_metadata(intent="recommend")
    assert md["intent"] == "recommend"


def test_setup_langsmith_disabled(monkeypatch):
    monkeypatch.setattr(ls.settings, "LANGSMITH_TRACING", False)
    assert ls.setup_langsmith() is False
