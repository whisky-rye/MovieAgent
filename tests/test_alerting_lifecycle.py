"""P1-11 告警生命周期 + 通知器框架（飞书群机器人）单测（离线）。

覆盖：
1. AlertStateTracker：firing 只通知一次 / 持续存在不重复 / severity 升级重通知 /
   消失后 resolved 恢复通知；
2. FeishuBotNotifier：未配 webhook=禁用 / 配了按官方协议下发（timestamp+sign+
   msg_type）/ 加签算法可独立复算验证 / firing 走卡片、resolved 走文本 / HTTP
   失败重试一次后放弃且绝不抛异常；
3. notify() 只向已启用通道分发；emit_resolved 受 ALERT_NOTIFY_RESOLVED 控制；
4. collect_dependency_alerts：Redis 降级探针（healthy→无告警，挂→告警）；
5. alert_loop_status：未启动时 configured/running 判定。
"""
import base64
import hashlib
import hmac
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config.settings as cs
from observability import alerting as al
from observability import notifiers as nt


@pytest.fixture
def tracker():
    return al.AlertStateTracker()


def _alert(name, severity="warning"):
    return {"severity": severity, "name": name, "message": f"{name} 消息",
            "value": 1, "threshold": 0}


# ---------- 1) 生命周期 ----------

def test_tracker_fires_once_then_resolves(tracker):
    f1, r1 = tracker.transition([_alert("a")])
    assert [a["name"] for a in f1] == ["a"] and r1 == []
    f2, _ = tracker.transition([_alert("a")])          # 持续存在：不重复通知
    assert f2 == []
    f3, r3 = tracker.transition([])                     # 消失：恢复通知
    assert f3 == [] and [a["name"] for a in r3] == ["a"]
    assert "已恢复" in r3[0]["message"]
    assert tracker.firing_names() == []


def test_tracker_escalation_renotifies(tracker):
    tracker.transition([_alert("a", "warning")])
    f, _ = tracker.transition([_alert("a", "critical")])  # 升级必须重通知
    assert [a["name"] for a in f] == ["a"]
    f2, _ = tracker.transition([_alert("a", "critical")])  # 同级不重复
    assert f2 == []


def test_tracker_independent_names(tracker):
    f, _ = tracker.transition([_alert("a"), _alert("b")])
    assert {a["name"] for a in f} == {"a", "b"}
    f2, r2 = tracker.transition([_alert("b")])             # 只恢复 a
    assert f2 == [] and [a["name"] for a in r2] == ["a"]
    assert tracker.firing_names() == ["b"]


# ---------- 2) 飞书机器人 ----------

@pytest.fixture
def capture_post(monkeypatch):
    """替换 HTTP 出口，记录每次 payload；返回 False 可模拟失败。"""
    calls = {"n": 0, "payloads": [], "fail": False}

    def _fake(url, payload, timeout=5):
        calls["n"] += 1
        calls["payloads"].append((url, payload))
        return not calls["fail"]

    monkeypatch.setattr(nt, "_http_post_json", _fake)
    return calls


def test_feishu_disabled_without_webhook(monkeypatch, capture_post):
    monkeypatch.setattr(cs.settings, "ALERT_FEISHU_WEBHOOK", "")
    bot = nt.FeishuBotNotifier()
    assert bot.enabled() is False
    assert bot.send([_alert("a")]) is False
    assert capture_post["n"] == 0  # 不发起任何请求


def test_feishu_firing_card_payload(monkeypatch, capture_post):
    monkeypatch.setattr(cs.settings, "ALERT_FEISHU_WEBHOOK", "https://open.feishu.cn/hook/xxx")
    monkeypatch.setattr(cs.settings, "ALERT_FEISHU_SECRET", "s3cr3t")
    bot = nt.FeishuBotNotifier()
    assert bot.enabled() is True
    assert bot.send([_alert("high_error_rate", "critical")]) is True
    url, payload = capture_post["payloads"][0]
    assert url == "https://open.feishu.cn/hook/xxx"
    assert payload["msg_type"] == "interactive"
    assert payload["card"]["header"]["template"] == "red"  # critical → 红
    assert any("high_error_rate" in e.get("content", "")
               for e in payload["card"]["elements"])
    # 加签可独立复算验证（官方算法：base64(HMAC-SHA256(key=f"{ts}\n{secret}", msg="")))
    ts = payload["timestamp"]
    expect = base64.b64encode(
        hmac.new(f"{ts}\ns3cr3t".encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    assert payload["sign"] == expect


def test_feishu_no_secret_empty_sign(monkeypatch, capture_post):
    monkeypatch.setattr(cs.settings, "ALERT_FEISHU_WEBHOOK", "https://open.feishu.cn/hook/xxx")
    monkeypatch.setattr(cs.settings, "ALERT_FEISHU_SECRET", "")
    bot = nt.FeishuBotNotifier()
    assert bot.send([_alert("a")]) is True
    assert capture_post["payloads"][0][1]["sign"] == ""


def test_feishu_resolved_uses_text(monkeypatch, capture_post):
    monkeypatch.setattr(cs.settings, "ALERT_FEISHU_WEBHOOK", "https://open.feishu.cn/hook/xxx")
    bot = nt.FeishuBotNotifier()
    resolved = [{"name": "redis_degraded", "severity": "warning",
                 "message": "已恢复：Redis 不可用…", "value": "local", "threshold": "redis"}]
    assert bot.send(resolved, state="resolved") is True
    payload = capture_post["payloads"][0][1]
    assert payload["msg_type"] == "text"
    assert "恢复" in payload["content"]["text"]


def test_feishu_http_failure_retries_then_gives_up(monkeypatch, capture_post):
    monkeypatch.setattr(cs.settings, "ALERT_FEISHU_WEBHOOK", "https://open.feishu.cn/hook/xxx")
    capture_post["fail"] = True
    bot = nt.FeishuBotNotifier()
    assert bot.send([_alert("a")]) is False   # 绝不抛异常
    assert capture_post["n"] == 2             # 重试一次后放弃


def test_notify_only_enabled_channels(monkeypatch, capture_post):
    """只配飞书 → 只推飞书；通用 webhook 不启用。"""
    monkeypatch.setattr(cs.settings, "ALERT_WEBHOOK_URL", "")
    monkeypatch.setattr(cs.settings, "ALERT_FEISHU_WEBHOOK", "https://open.feishu.cn/hook/xxx")
    results = nt.notify([_alert("a")])
    assert results == {"feishu": True}
    monkeypatch.setattr(cs.settings, "ALERT_WEBHOOK_URL", "https://hook.example/x")
    results2 = nt.notify([_alert("a")])
    assert set(results2) == {"feishu", "webhook"}


def test_notify_empty_alerts_noop(monkeypatch, capture_post):
    assert nt.notify([]) == {}
    assert capture_post["n"] == 0


# ---------- 3) emit_resolved 开关 ----------

def test_emit_resolved_respects_setting(monkeypatch, capture_post):
    monkeypatch.setattr(cs.settings, "ALERT_FEISHU_WEBHOOK", "https://open.feishu.cn/hook/xxx")
    resolved = [_alert("a")]
    monkeypatch.setattr(cs.settings, "ALERT_NOTIFY_RESOLVED", False)
    al.emit_resolved(resolved)
    assert capture_post["n"] == 0   # 关闭恢复推送（仍落日志）
    monkeypatch.setattr(cs.settings, "ALERT_NOTIFY_RESOLVED", True)
    al.emit_resolved(resolved)
    assert capture_post["n"] == 1


# ---------- 4) 依赖探针 ----------

class _FakeCache:
    def __init__(self, backend, healthy):
        self._b, self._h = backend, healthy

    def backend(self):
        return self._b

    def health(self):
        return self._h


def test_dependency_probe_redis_down(monkeypatch):
    monkeypatch.setattr("api.cache.get_cache", lambda: _FakeCache("redis", False))
    alerts = al.collect_dependency_alerts()
    assert [a["name"] for a in alerts] == ["redis_degraded"]


def test_dependency_probe_healthy_or_local(monkeypatch):
    monkeypatch.setattr("api.cache.get_cache", lambda: _FakeCache("redis", True))
    assert al.collect_dependency_alerts() == []
    monkeypatch.setattr("api.cache.get_cache", lambda: _FakeCache("local", False))
    assert al.collect_dependency_alerts() == []  # 本地兜底是设计内降级，不告警


# ---------- 5) 循环活性 ----------

def test_alert_loop_status_not_started(monkeypatch):
    monkeypatch.setattr(al, "_LOOP_THREAD", None)
    monkeypatch.setattr(cs.settings, "ALERT_EVAL_INTERVAL", 60)
    st = al.alert_loop_status()
    assert st["configured"] is True and st["running"] is False
    assert st["last_eval_age_s"] is None and st["firing"] == []


def test_alert_loop_status_disabled_configured_false(monkeypatch):
    monkeypatch.setattr(al, "_LOOP_THREAD", None)
    monkeypatch.setattr(cs.settings, "ALERT_EVAL_INTERVAL", 0)
    assert al.alert_loop_status()["configured"] is False
