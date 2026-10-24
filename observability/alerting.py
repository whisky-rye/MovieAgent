"""告警规则引擎（E4/O18）：基于指标快照评估规则，产出告警并推送。

设计：
- evaluate(snapshot, cfg) 纯函数：输入 metrics.snapshot() 与阈值配置，返回告警列表
  （不依赖全局状态，便于单测）。每条告警含 severity / name / message / value / threshold。
- evaluate_alerts() 便捷封装：读取 settings + 全局 metrics.snapshot()。
- AlertStateTracker：firing ↔ resolved 生命周期（P1-11）——同一告警持续存在不重复
  推送；severity 升级重新推送；**消失后推送恢复通知**（Redis 挂后恢复等也要让人知道）。
- emit_alerts / emit_resolved：统一出口——结构化日志 + 通知器框架多通道推送
  （通用 webhook / 飞书群机器人，见 observability/notifiers.py）。
- collect_dependency_alerts()：依赖探针（Redis 兜底降级等周期性状态类告警）。
- run_alert_loop()：后台线程周期评估；alert_loop_status() 供 /health 检查循环活性。

告警维度（多维度综合）：错误率、延迟 P95、护栏拦截率、免费额度熔断、并发过载、
依赖降级（缓存走本地兜底等）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from config.settings import settings
from observability.metrics import metrics
from observability.notifiers import notify

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


class AlertStateTracker:
    """告警生命周期：firing ↔ resolved（P1-11，进程内状态 + 锁）。

    - 首次出现的告警 → firing（通知一次）；
    - 持续存在 → 不重复通知（值的变化可在 /admin/alerts 查看）；
    - severity 升级（warning→critical）→ 重新通知；
    - 消失后的首次评估 → resolved（恢复通知， Redis 挂后恢复等场景关键）。
    """

    def __init__(self):
        self._firing: dict[str, dict] = {}
        self._lock = threading.Lock()

    def transition(self, alerts: list[dict]) -> tuple[list[dict], list[dict]]:
        """输入当前告警列表，返回 (需通知的 firing, 已恢复的 resolved 摘要)。"""
        with self._lock:
            now = {a["name"]: a for a in alerts}
            new_firing: list[dict] = []
            for a in alerts:
                prev = self._firing.get(a["name"])
                if prev is None:
                    new_firing.append(a)  # 首次触发
                elif _SEVERITY_ORDER.get(a["severity"], 0) > _SEVERITY_ORDER.get(prev["severity"], 0):
                    new_firing.append(a)  #  severity 升级，重新通知
            resolved = [
                {"name": name, "severity": prev["severity"],
                 "message": f"已恢复：{prev['message']}", "value": prev.get("value"),
                 "threshold": prev.get("threshold")}
                for name, prev in self._firing.items() if name not in now
            ]
            self._firing = now
            return new_firing, resolved

    def firing_names(self) -> list[str]:
        with self._lock:
            return sorted(self._firing)


_TRACKER = AlertStateTracker()
_LAST_EVAL_TS: float | None = None      # 上次评估完成的时间戳（供活性/僵死判定）
_LOOP_THREAD: threading.Thread | None = None  # 告警循环线程引用


def emit_alerts(alerts: list[dict]) -> None:
    """统一出口：结构化日志 + 通知器多通道推送。best-effort，绝不抛异常。"""
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
    try:
        results = notify(alerts, state="firing")
        for name, ok in results.items():
            if not ok:
                logger.warning("告警通道 %s 推送未成功（已重试）", name)
    except Exception as e:  # noqa: BLE001 - 推送失败绝不影响主链路
        logger.warning("告警推送失败（忽略）：%s", e)


def emit_resolved(resolved: list[dict]) -> None:
    """恢复通知出口（P1-11）：与 firing 同款日志 + 通道推送（state=resolved）。

    ALERT_NOTIFY_RESOLVED=false 可关闭恢复推送（仍落日志）。
    """
    if not resolved:
        return
    for a in resolved:
        logger.info("RESOLVED[%s] %s", a["name"], a["message"],
                    extra={"event": "alert_resolved", "alert": a["name"]})
    if not settings.ALERT_NOTIFY_RESOLVED:
        return
    try:
        notify(resolved, state="resolved")
    except Exception as e:  # noqa: BLE001
        logger.warning("恢复通知推送失败（忽略）：%s", e)


def collect_dependency_alerts() -> list[dict]:
    """依赖探针：周期性状态类告警（指标计数器看不出来的场景）。

    当前覆盖：Redis 缓存降级——use_redis 但 ping 失败（DualCache 已自动转本地
    兜底，此处负责让运维知道；恢复由 Tracker 的 resolved 通知闭环）。
    延迟导入 api.cache 避免 observability→api 模块级环。
    """
    alerts: list[dict] = []
    try:
        from api.cache import get_cache

        c = get_cache()
        if c.backend() == "redis" and not c.health():
            alerts.append({
                "severity": "warning", "name": "redis_degraded",
                "message": "Redis 不可用，缓存已降级为本地内存兜底（重启后缓存清空、多 worker 不共享）",
                "value": "local", "threshold": "redis",
            })
    except Exception as e:  # noqa: BLE001 - 探针失败不阻断告警循环
        logger.warning("依赖探针执行失败（忽略）：%s", e)
    return alerts


def run_alert_loop(interval: int | None = None) -> None:
    """后台线程：周期性评估并推送告警。interval 秒；<=0 不启动。

    P1-11：经 AlertStateTracker 做 firing/resolved 生命周期管理（恢复也通知），
    并合并依赖探针告警；每轮刷新 _LAST_EVAL_TS 供 /health 检查循环活性。
    """
    interval = interval if interval is not None else settings.ALERT_EVAL_INTERVAL
    if not interval or interval <= 0:
        return

    def _loop() -> None:
        global _LAST_EVAL_TS
        while True:
            time.sleep(interval)
            try:
                if not settings.METRICS_ENABLED:
                    continue
                alerts = evaluate_alerts() + collect_dependency_alerts()
                new_firing, resolved = _TRACKER.transition(alerts)
                if new_firing:
                    emit_alerts(new_firing)
                if resolved:
                    emit_resolved(resolved)
                _LAST_EVAL_TS = time.time()
            except Exception as e:  # noqa: BLE001
                logger.warning("告警循环异常（忽略）：%s", e)

    t = threading.Thread(target=_loop, name="alert-eval", daemon=True)
    t.start()
    global _LOOP_THREAD
    _LOOP_THREAD = t


def alert_loop_status() -> dict:
    """告警循环活性（供 /health）：configured=是否配置了周期；running=线程存活且不僵死；
    last_eval_age_s=距上次评估的秒数（None=从未评估）；firing=当前 firing 的告警名。"""
    configured = bool(settings.ALERT_EVAL_INTERVAL and settings.ALERT_EVAL_INTERVAL > 0)
    alive = bool(_LOOP_THREAD and _LOOP_THREAD.is_alive())
    age = round(time.time() - _LAST_EVAL_TS, 1) if _LAST_EVAL_TS else None
    # 线程活着但超过 3 个周期没更新时间戳 → 僵死（循环体卡死等）
    stale = bool(alive and age is not None
                 and age > 3 * max(1, settings.ALERT_EVAL_INTERVAL))
    return {
        "configured": configured,
        "running": alive and not stale,
        "stale": stale,
        "last_eval_age_s": age,
        "firing": _TRACKER.firing_names(),
    }
