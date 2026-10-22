"""告警规则引擎（E4/O18）：基于指标快照评估规则，产出告警并推送。

设计：
- evaluate(snapshot, cfg) 纯函数：输入 metrics.snapshot() 与阈值配置，返回告警列表
  （不依赖全局状态，便于单测）。每条告警含 severity / name / message / value / threshold。
- evaluate_alerts() 便捷封装：读取 settings + 全局 metrics.snapshot()。
- emit_alerts(alerts) ：统一出口——落结构化日志（logger=movie_agent.alert），
  若配置了 ALERT_WEBHOOK_URL 则并发 POST（best-effort，失败仅记日志不影响主链路）。

告警维度（多维度综合）：错误率、延迟 P95、护栏拦截率、免费额度熔断、并发过载。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from config.settings import settings
from observability.metrics import metrics

logger = logging.getLogger("movie_agent.alert")

_SEVERITY_ORDER = {"info": 1, "warning": 2, "critical": 3}


def _sum_counter(snapshot: dict, name: str, tag_eq: dict | None = None) -> float:
    """对指定 counter 的所有 tag 变体求和；tag_eq 可限定仅统计某 tag 取值。"""
    total = 0.0
    for entry in snapshot.get("counters", {}).get(name, []):
        if tag_eq:
            ok = all(entry["tags"].get(k) == str(v) for k, v in tag_eq.items())
            if not ok:
                continue
        total += entry["value"]
    return total


def _p95_hist(snapshot: dict, name: str) -> float:
    best = 0.0
    for entry in snapshot.get("histograms", {}).get(name, []):
        if entry.get("p95", 0) > best:
            best = entry["p95"]
    return best


def evaluate(snapshot: dict, cfg: dict) -> list[dict]:
    """评估规则，返回告警列表（按严重程度降序）。"""
    alerts: list[dict] = []

    total_req = _sum_counter(snapshot, "http_requests_total")
    err5xx = _sum_counter(snapshot, "http_requests_total", {"status": "5xx"})
    guardrail_blocks = _sum_counter(snapshot, "guardrail_block_total")
    quota_exhausted = _sum_counter(snapshot, "llm_quota_exhausted_total")
    overloaded = _sum_counter(snapshot, "overloaded_total")
    rate_limited = _sum_counter(snapshot, "rate_limited_total")
    latency_p95 = _p95_hist(snapshot, "http_request_duration_ms")

    # 1) 5xx 错误率（仅 5xx 计入故障率；4xx 多为越域/参数，不计入）
    if total_req > 0:
        err_rate = err5xx / total_req
        if err_rate >= cfg.get("error_rate", 0.05):
            alerts.append({
                "severity": "critical", "name": "high_error_rate",
                "message": f"5xx 错误率 {err_rate:.1%} ≥ 阈值 {cfg.get('error_rate'):.1%}",
                "value": round(err_rate, 4), "threshold": cfg.get("error_rate"),
            })

    # 2) 推荐链路延迟 P95
    if latency_p95 >= cfg.get("latency_p95_ms", 8000):
        alerts.append({
            "severity": "warning", "name": "high_latency",
            "message": f"HTTP P95 延迟 {latency_p95:.0f}ms ≥ 阈值 {cfg.get('latency_p95_ms')}ms",
            "value": round(latency_p95, 1), "threshold": cfg.get("latency_p95_ms"),
        })

    # 3) 护栏拦截率（疑似越域风暴 / 被攻击）
    if total_req > 0 and (guardrail_blocks / total_req) >= cfg.get("guardrail_rate", 0.30):
        rate = guardrail_blocks / total_req
        alerts.append({
            "severity": "warning", "name": "high_guardrail_block_rate",
            "message": f"护栏拦截率 {rate:.1%} ≥ 阈值 {cfg.get('guardrail_rate'):.1%}，疑似越域风暴/注入攻击",
            "value": round(rate, 4), "threshold": cfg.get("guardrail_rate"),
        })

    # 4) 免费额度熔断（计费风险，必须 critical）
    if cfg.get("quota_exhausted", True) and quota_exhausted > 0:
        alerts.append({
            "severity": "critical", "name": "llm_quota_exhausted",
            "message": f"检测到 {int(quota_exhausted)} 次模型免费额度熔断，云端计费/可用性风险",
            "value": int(quota_exhausted), "threshold": 0,
        })

    # 5) 并发过载
    if overloaded > 0:
        alerts.append({
            "severity": "warning", "name": "overloaded",
            "message": f"检测到 {int(overloaded)} 次并发过载(503)，容量不足",
            "value": int(overloaded), "threshold": 0,
        })

    alerts.sort(key=lambda a: _SEVERITY_ORDER.get(a["severity"], 0), reverse=True)
    return alerts


def _cfg_from_settings() -> dict:
    return {
        "error_rate": settings.ALERT_ERROR_RATE,
        "latency_p95_ms": settings.ALERT_LATENCY_P95_MS,
        "guardrail_rate": settings.ALERT_GUARDRAIL_RATE,
        "quota_exhausted": settings.ALERT_LLM_QUOTA_EXHAUSTED,
    }


def evaluate_alerts() -> list[dict]:
    """读取 settings + 全局指标，返回当前告警列表。"""
    return evaluate(metrics.snapshot(), _cfg_from_settings())


def emit_alerts(alerts: list[dict]) -> None:
    """统一出口：结构化日志 + 可选 webhook 推送。best-effort，绝不抛异常。"""
    if not alerts:
        return
    for a in alerts:
        extra = {
            "event": "alert",
            "severity": a["severity"],
            "alert": a["name"],
            "value": a.get("value"),
            "threshold": a.get("threshold"),
        }
        if a["severity"] == "critical":
            logger.error("ALERT[%s] %s", a["name"], a["message"], extra=extra)
        else:
            logger.warning("ALERT[%s] %s", a["name"], a["message"], extra=extra)

    url = (settings.ALERT_WEBHOOK_URL or "").strip()
    if not url:
        return
    try:
        import requests

        requests.post(url, json={"alerts": alerts}, timeout=5)
    except Exception as e:  # noqa: BLE001
        logger.warning("告警 webhook 推送失败（忽略）：%s", e)


def run_alert_loop(interval: int | None = None) -> None:
    """后台线程：周期性评估并推送告警。interval 秒；<=0 不启动。"""
    interval = interval if interval is not None else settings.ALERT_EVAL_INTERVAL
    if not interval or interval <= 0:
        return

    def _loop() -> None:
        import time

        while True:
            time.sleep(interval)
            try:
                if not settings.METRICS_ENABLED:
                    continue
                alerts = evaluate_alerts()
                if alerts:
                    emit_alerts(alerts)
            except Exception as e:  # noqa: BLE001
                logger.warning("告警循环异常（忽略）：%s", e)

    t = threading.Thread(target=_loop, name="alert-eval", daemon=True)
    t.start()
