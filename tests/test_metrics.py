"""E4/O18 监控告警看板：指标注册表 / Prometheus 导出 / 告警规则 / 日志链路分析。

全部离线可跑（不依赖 MySQL / Redis / 云端）：
- observability.metrics：线程安全注册表（Counter/Gauge/Histogram + tags）
- observability.alerting：纯函数规则评估（错误率 / 延迟 / 护栏率 / 额度熔断 / 过载）
- observability.log_analyzer：结构化 JSON 日志聚合（QPS/延迟/错误率/关键事件）
- llm.failover 埋点助手：token/延迟/降级/额度熔断计数
"""
import json

import pytest

from observability.metrics import Metrics, metrics
from observability.alerting import evaluate, evaluate_alerts, emit_alerts
from observability.log_analyzer import (
    analyze_log_file,
    iter_log_events,
    summarize,
)


# ---------- 指标注册表 ----------

def test_counter_incr_and_snapshot():
    m = Metrics()
    m.incr("http_requests_total", 1, {"path": "/recommend", "status": "2xx"})
    m.incr("http_requests_total", 2, {"path": "/recommend", "status": "2xx"})
    snap = m.snapshot()
    entries = snap["counters"]["http_requests_total"]
    assert len(entries) == 1
    assert entries[0]["value"] == 3
    assert entries[0]["tags"] == {"path": "/recommend", "status": "2xx"}


def test_counter_with_different_tags_are_separate():
    m = Metrics()
    m.incr("http_requests_total", 1, {"status": "2xx"})
    m.incr("http_requests_total", 1, {"status": "5xx"})
    snap = m.snapshot()
    vals = {e["tags"]["status"]: e["value"] for e in snap["counters"]["http_requests_total"]}
    assert vals == {"2xx": 1, "5xx": 1}


def test_gauge_set():
    m = Metrics()
    m.set("storage_ok", 1)
    m.set("storage_ok", 0)
    snap = m.snapshot()
    assert snap["gauges"]["storage_ok"][0]["value"] == 0


def test_histogram_stats_and_percentiles():
    m = Metrics()
    for v in range(1, 101):
        m.observe("http_request_duration_ms", v)
    st = m.snapshot()["histograms"]["http_request_duration_ms"][0]
    assert st["count"] == 100
    assert st["min"] == 1
    assert st["max"] == 100
    assert st["p95"] >= 95
    assert st["p99"] >= 99
    assert st["sum"] == sum(range(1, 101))


def test_histogram_empty_is_safe():
    m = Metrics()
    m.observe("x", 1)  # 仅创建
    st = m.snapshot()["histograms"]["x"][0]
    assert st["count"] == 1


def test_prometheus_export_format():
    m = Metrics()
    m.incr("http_requests_total", 5, {"path": "/recommend", "status": "2xx"})
    m.observe("http_request_duration_ms", 120, {"path": "/recommend"})
    out = m.to_prometheus()
    assert 'http_requests_total{path="/recommend",status="2xx"} 5' in out
    assert "# TYPE http_requests_total counter" in out
    assert "http_request_duration_ms_count" in out
    assert "http_request_duration_ms_p95" in out


def test_prometheus_export_empty_is_valid():
    out = Metrics().to_prometheus()
    assert out.strip() == "" or out.startswith("#")


def test_metrics_disabled_blocks_writes():
    m = Metrics()
    m.set_enabled(False)
    m.incr("x", 1)
    m.observe("y", 1)
    m.set("z", 1)
    snap = m.snapshot()
    assert snap["counters"] == {} and snap["histograms"] == {} and snap["gauges"] == {}


def test_metrics_enabled_resumes():
    m = Metrics()
    m.set_enabled(False)
    m.incr("x", 1)
    m.set_enabled(True)
    m.incr("x", 1)
    assert m.snapshot()["counters"]["x"][0]["value"] == 1


def test_global_metrics_singleton_is_shared():
    metrics.incr("unit_test_probe_total", 1)
    assert metrics.snapshot()["counters"]["unit_test_probe_total"][0]["value"] >= 1


# ---------- 告警规则 ----------

def _snap(**counters):
    """构造最小快照：counters 为 {name: [(tags, value), ...]}"""
    snap = {"counters": {}, "gauges": {}, "histograms": {}}
    for name, items in counters.items():
        snap["counters"][name] = [
            {"tags": t, "value": v} for t, v in items
        ]
    return snap


def _cfg(**over):
    cfg = {"error_rate": 0.05, "latency_p95_ms": 8000,
           "guardrail_rate": 0.30, "quota_exhausted": True}
    cfg.update(over)
    return cfg


def test_no_alerts_when_healthy():
    snap = _snap(http_requests_total=[({"status": "2xx"}, 100)])
    assert evaluate(snap, _cfg()) == []


def test_high_error_rate_triggers_critical():
    snap = _snap(http_requests_total=[({"status": "2xx"}, 90), ({"status": "5xx"}, 10)])
    alerts = evaluate(snap, _cfg())
    names = [a["name"] for a in alerts]
    assert "high_error_rate" in names
    err = next(a for a in alerts if a["name"] == "high_error_rate")
    assert err["severity"] == "critical"
    assert err["value"] == pytest.approx(0.1)


def test_4xx_does_not_count_as_fault():
    """4xx 属越域/参数问题，不计入故障率（避免误告警）。"""
    snap = _snap(http_requests_total=[({"status": "2xx"}, 90), ({"status": "4xx"}, 10)])
    assert evaluate(snap, _cfg()) == []


def test_high_latency_triggers_warning():
    snap = {"counters": {}, "gauges": {},
            "histograms": {"http_request_duration_ms": [
                {"tags": {"path": "/recommend"}, "count": 10, "sum": 100000,
                 "min": 100, "max": 20000, "avg": 10000, "p50": 9000,
                 "p95": 15000, "p99": 19000}]}}
    alerts = evaluate(snap, _cfg())
    lat = next(a for a in alerts if a["name"] == "high_latency")
    assert lat["severity"] == "warning"
    assert lat["value"] == 15000


def test_high_guardrail_rate_triggers_warning():
    snap = _snap(http_requests_total=[({"status": "2xx"}, 50)],
                 guardrail_block_total=[({"action": "block"}, 30)])
    alerts = evaluate(snap, _cfg())
    assert any(a["name"] == "high_guardrail_block_rate" for a in alerts)


def test_quota_exhausted_triggers_critical():
    snap = _snap(llm_quota_exhausted_total=[({"tier": "strong"}, 1)])
    alerts = evaluate(snap, _cfg())
    q = next(a for a in alerts if a["name"] == "llm_quota_exhausted")
    assert q["severity"] == "critical"


def test_quota_alert_can_be_disabled():
    snap = _snap(llm_quota_exhausted_total=[({"tier": "strong"}, 1)])
    assert evaluate(snap, _cfg(quota_exhausted=False)) == []


def test_overload_triggers_warning():
    snap = _snap(overloaded_total=[({"endpoint": "recommend"}, 2)])
    alerts = evaluate(snap, _cfg())
    assert any(a["name"] == "overloaded" for a in alerts)


def test_alerts_sorted_by_severity():
    snap = _snap(http_requests_total=[({"status": "5xx"}, 100)],
                 llm_quota_exhausted_total=[({"tier": "strong"}, 1)])
    alerts = evaluate(snap, _cfg())
    sev = [a["severity"] for a in alerts]
    assert sev == sorted(sev, key=lambda s: {"critical": 3, "warning": 2, "info": 1}[s],
                         reverse=True)


def test_evaluate_alerts_uses_global_metrics():
    metrics.incr("http_requests_total", 1000, {"status": "5xx"})
    alerts = evaluate_alerts()
    assert any(a["name"] == "high_error_rate" for a in alerts)


def test_emit_alerts_never_raises(caplog):
    """告警出口必须 best-effort：即便 webhook 不可达也不抛异常。"""
    alerts = [{"severity": "critical", "name": "x", "message": "m",
               "value": 1, "threshold": 0}]
    emit_alerts(alerts)  # 不抛异常即通过
    emit_alerts([])     # 空列表不报错


# ---------- 日志链路分析 ----------

def _ev(name, **kw):
    d = {"ts": "2026-10-21T12:00:00+0800", "level": "INFO",
         "logger": "movie_agent.api", "msg": name, "event": name}
    d.update(kw)
    return d


def test_iter_log_events_skips_broken_lines(tmp_path):
    p = tmp_path / "app.log"
    p.write_text(
        json.dumps(_ev("http_request", status_code=200, latency_ms=10, path="/recommend")) + "\n"
        + "not-json-line\n"
        + json.dumps(_ev("http_request", status_code=500, latency_ms=30, path="/x")) + "\n",
        encoding="utf-8",
    )
    evs = list(iter_log_events(p))
    assert len(evs) == 2


def test_iter_log_events_missing_file(tmp_path):
    assert list(iter_log_events(tmp_path / "nope.log")) == []


def test_summarize_aggregates_requests_and_latency():
    events = [
        _ev("http_request", status_code=200, latency_ms=100, path="/recommend"),
        _ev("http_request", status_code=200, latency_ms=300, path="/recommend"),
        _ev("http_request", status_code=500, latency_ms=900, path="/chat"),
        _ev("guardrail"),
        _ev("quota_exhausted"),
        _ev("model_fail"),
        _ev("overloaded"),
        _ev("rate_limited"),
        _ev("llm_call", input_tokens=100, output_tokens=50,
           cached_tokens=10, total_tokens=150),
    ]
    s = summarize(events)
    assert s["total_requests"] == 3
    assert s["status"] == {"2xx": 2, "5xx": 1}
    assert s["error_rate_5xx"] == pytest.approx(1 / 3, abs=1e-3)
    assert s["latency"]["max"] == 900
    assert s["latency"]["p95"] >= 900
    assert s["top_endpoints"][0][0] == "/recommend"
    assert s["events"]["guardrail_block"] == 1
    assert s["events"]["llm_quota_exhausted"] == 1
    assert s["events"]["llm_failover"] == 1
    assert s["events"]["overloaded"] == 1
    assert s["events"]["rate_limited"] == 1
    assert s["llm_tokens"]["input_tokens"] == 100


def test_summarize_empty_is_safe():
    s = summarize([])
    assert s["total_requests"] == 0
    assert s["error_rate_5xx"] == 0
    assert s["latency"]["p95"] == 0


def test_analyze_log_file(tmp_path):
    p = tmp_path / "app.log"
    p.write_text(
        "\n".join(json.dumps(_ev("http_request", status_code=200,
                                 latency_ms=10 + i, path="/recommend")) for i in range(5)),
        encoding="utf-8",
    )
    s = analyze_log_file(p)
    assert s["total_requests"] == 5


# ---------- LLM 埋点助手 ----------

def test_failover_metric_helpers():
    from llm.failover import (
        _record_failover,
        _record_llm_success,
        _record_local_fallback,
        _record_quota_exhausted,
    )

    class _Result:
        llm_output = {"token_usage": {"prompt_tokens": 100,
                                       "completion_tokens": 50,
                                       "total_tokens": 150}}

    metrics.incr("llm_calls_total", 0, {"provider": "cloud"})  # 占位，保证 key 存在
    _record_llm_success("m1", "cloud", "strong", _Result(), 250)
    _record_quota_exhausted("m1", "strong")
    _record_failover("strong", "transient")
    _record_local_fallback("deepseek-r1:7b", "strong")

    snap = metrics.snapshot()
    calls = snap["counters"]["llm_calls_total"]
    assert any(c["tags"].get("tier") == "strong" and c["value"] >= 1 for c in calls)
    assert snap["counters"]["llm_quota_exhausted_total"][0]["value"] >= 1
    assert snap["counters"]["llm_failover_total"][0]["value"] >= 1
    assert snap["counters"]["llm_local_fallback_total"][0]["value"] >= 1
    tokens = snap["counters"]["llm_tokens_total"]
    assert any(t["tags"]["kind"] == "input" and t["value"] >= 100 for t in tokens)
    assert any(t["tags"]["kind"] == "output" and t["value"] >= 50 for t in tokens)
    assert snap["histograms"]["llm_call_duration_ms"][0]["count"] >= 1


# ---------- HTML 看板渲染 ----------

def test_render_dashboard_html_contains_sections():
    from observability.dashboard import render_dashboard_html

    snap = {"counters": {"http_requests_total": [{"tags": {"status": "2xx"}, "value": 3}]},
            "gauges": {}, "histograms": {"http_request_duration_ms": [
                {"tags": {}, "count": 3, "sum": 30, "min": 5, "max": 15,
                 "avg": 10, "p50": 10, "p95": 15, "p99": 15}]}}
    alerts = [{"severity": "critical", "name": "high_error_rate", "message": "错误率高"}]
    log = {"total_requests": 3, "error_rate_5xx": 0.0,
           "latency": {"p95": 15, "p99": 15}, "events": {}, "time_span": {}}
    html = render_dashboard_html(snap, alerts, log, cfg={}, log_dir="log")
    assert "<!doctype html>" in html
    assert "http_requests_total" in html
    assert "high_error_rate" in html          # 告警渲染
    assert "当前无告警" not in html            # 有告警时不显示无告警文案
    assert "http_request_duration_ms" in html  # 分布表
    assert "log/app.log" in html


def test_render_dashboard_html_no_alert_state():
    from observability.dashboard import render_dashboard_html

    html = render_dashboard_html({"counters": {}, "gauges": {}, "histograms": {}},
                                  [], {"error": "日志目录不存在"})
    assert "当前无告警" in html
    assert "日志目录不存在" in html
    assert "暂无计数" in html


def test_render_dashboard_html_is_self_contained():
    """看板不得引用外部 CSS/JS（内网/离线环境可用）。"""
    from observability.dashboard import render_dashboard_html

    html = render_dashboard_html({"counters": {}, "gauges": {}, "histograms": {}},
                                  [], {"error": "x"})
    assert "http://" not in html
    assert "https://" not in html.replace("https://www.w3.org", "")
    assert "<link" not in html
    assert "<script src" not in html


# ---------- CLI ----------

def test_analyze_logs_cli_runs(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    p = tmp_path / "app.log"
    p.write_text(
        "\n".join(json.dumps(_ev("http_request", status_code=200,
                                 latency_ms=10 + i, path="/recommend")) for i in range(3)),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, str(root / "scripts" / "analyze_logs.py"), str(p)],
        capture_output=True, text=True, cwd=str(root), timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "日志链路分析报告" in r.stdout
    assert "请求总数 : 3" in r.stdout

