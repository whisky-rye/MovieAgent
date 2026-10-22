"""日志链路分析（E4/O18 的「多维度」之一）：解析结构化 JSON 日志（log/app.log），
跨事件维度聚合 QPS / 延迟分位 / 错误率 / 额度熔断 / 护栏拦截 / 模型降级，形成「日志链路」
视图，弥补进程内指标在多 worker / 重启后的盲区（日志是跨进程、可长期留存的事实源）。

设计为可被 import 的纯函数（供 GET /admin/dashboard 在线聚合），同时提供 CLI 入口
scripts/analyze_logs.py。不依赖任何第三方库。
"""
from __future__ import annotations

import json
from collections import Counter, deque
from pathlib import Path
from typing import Iterable, Iterator


def iter_log_events(path: str | Path) -> Iterator[dict]:
    """逐行读取 JSON 行日志，yield 解析成功的事件 dict（跳过非 JSON / 损坏行）。"""
    p = Path(path)
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if isinstance(ev, dict):
                    yield ev
    except Exception:
        return


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = min(n - 1, int(p * n))
    return sorted_vals[idx]


def summarize(events: Iterable[dict], window_last_n: int | None = None) -> dict:
    """聚合事件流为日志链路摘要。

    - 请求总数、按状态桶(2xx/3xx/4xx/5xx)分布、错误率
    - HTTP 延迟 min/max/p95/p99（取 latency_ms 字段）
    - 各端点请求数 Top
    - 关键事件计数：guardrail 拦截、llm_quota_exhausted、llm_failover(model_fail)、
      overloaded、rate_limited、llm_tokens 累计（input/output/cached）
    - 时间跨度（ts 最早/最晚）
    """
    total = 0
    status = Counter()
    latencies: list[float] = []
    endpoints = Counter()
    guardrail = 0
    quota = 0
    failover = 0
    overloaded = 0
    rate_limited = 0
    tokens = Counter()
    ts_min = None
    ts_max = None

    for ev in events:
        ev_name = ev.get("event", "")
        ts = ev.get("ts")
        if ts:
            if ts_min is None or ts < ts_min:
                ts_min = ts
            if ts_max is None or ts > ts_max:
                ts_max = ts

        if ev_name == "http_request":
            total += 1
            sb = ev.get("status_code")
            if sb is not None:
                status[f"{sb // 100}xx"] += 1
            if (ev.get("latency_ms") or 0) > 0:
                latencies.append(float(ev["latency_ms"]))
            path = ev.get("path")
            if path:
                endpoints[path] += 1
        elif ev_name == "guardrail":
            guardrail += 1
        elif ev_name == "quota_exhausted":
            quota += 1
        elif ev_name == "model_fail":
            failover += 1
        elif ev_name == "overloaded":
            overloaded += 1
        elif ev_name == "rate_limited":
            rate_limited += 1
        elif ev_name == "llm_call":
            for kind in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
                v = ev.get(kind)
                if isinstance(v, (int, float)) and v:
                    tokens[kind] += int(v)

    lat = sorted(latencies)
    err_rate = (status.get("5xx", 0) / total) if total else 0.0
    summary = {
        "total_requests": total,
        "status": dict(status),
        "error_rate_5xx": round(err_rate, 4),
        "latency": {
            "count": len(lat),
            "min": lat[0] if lat else 0,
            "max": lat[-1] if lat else 0,
            "p95": _pct(lat, 0.95),
            "p99": _pct(lat, 0.99),
        },
        "top_endpoints": endpoints.most_common(10),
        "events": {
            "guardrail_block": guardrail,
            "llm_quota_exhausted": quota,
            "llm_failover": failover,
            "overloaded": overloaded,
            "rate_limited": rate_limited,
        },
        "llm_tokens": dict(tokens),
        "time_span": {"from": ts_min, "to": ts_max},
    }
    return summary


def analyze_log_file(path: str | Path, max_lines: int = 200_000) -> dict:
    """解析单个日志文件并返回摘要（限制读取行数，避免大文件拖慢）。"""
    events = deque(maxlen=max_lines)
    for ev in iter_log_events(path):
        events.append(ev)
    return summarize(events)


def analyze_log_dir(log_dir: str | Path = "log") -> dict:
    """聚合 log 目录下全部 *.log（当前实现仅主日志 app.log 含结构化 JSON）。"""
    d = Path(log_dir)
    if not d.is_dir():
        return {"error": f"日志目录不存在: {log_dir}"}
    app_log = d / "app.log"
    if app_log.exists():
        return analyze_log_file(app_log)
    return {"error": "未找到 app.log", "dir": str(d)}
