"""离线渲染监控看板 HTML（E4/O18）。

用日志链路分析结果（log/app.log）+ 一个「由日志反推的指标快照」渲染自包含 HTML 看板，
无需启动服务即可查看/归档当前运行状况（如：交付评审、变更前后对比、线上快照留档）。

用法：
    python scripts/render_dashboard.py                       # 输出到 data/dashboard.html
    python scripts/render_dashboard.py -o tmp/board.html     # 指定输出
    python scripts/render_dashboard.py --dir log             # 指定日志目录
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observability.log_analyzer import analyze_log_dir  # noqa: E402
from observability.dashboard import render_dashboard_html  # noqa: E402
from observability.alerting import evaluate  # noqa: E402


def snapshot_from_log(summary: dict) -> dict:
    """由日志链路摘要反推一个「指标快照」结构（与 metrics.snapshot() 同形）。

    这样离线看板也能展示与在线看板一致的指标视图（数据源为日志事实）。
    """
    counters: dict[str, list] = {}
    histograms: dict[str, list] = {}

    status = summary.get("status", {}) or {}
    for bucket, n in status.items():
        counters.setdefault("http_requests_total", []).append(
            {"tags": {"status": bucket, "path": "*"}, "value": n})
    ev = summary.get("events", {}) or {}
    for name, key in (
        ("guardrail_block_total", "guardrail_block"),
        ("llm_quota_exhausted_total", "llm_quota_exhausted"),
        ("llm_failover_total", "llm_failover"),
        ("overloaded_total", "overloaded"),
        ("rate_limited_total", "rate_limited"),
    ):
        v = ev.get(key, 0)
        if v:
            counters.setdefault(name, []).append({"tags": {"source": "log"}, "value": v})
    tk = summary.get("llm_tokens", {}) or {}
    for kind, key in (("input", "input_tokens"), ("output", "output_tokens"),
                      ("cached", "cached_tokens"), ("total", "total_tokens")):
        v = tk.get(key, 0)
        if v:
            counters.setdefault("llm_tokens_total", []).append(
                {"tags": {"kind": kind, "tier": "*"}, "value": v})

    lat = summary.get("latency", {}) or {}
    if lat.get("count"):
        histograms.setdefault("http_request_duration_ms", []).append({
            "tags": {"path": "*"},
            "count": lat["count"], "sum": lat.get("max", 0) * lat["count"],
            "min": lat.get("min", 0), "max": lat.get("max", 0),
            "avg": lat.get("max", 0), "p50": lat.get("p95", 0),
            "p95": lat.get("p95", 0), "p99": lat.get("p99", 0),
        })
    return {"counters": counters, "gauges": {}, "histograms": histograms}


def main() -> int:
    ap = argparse.ArgumentParser(description="离线渲染监控看板 HTML")
    ap.add_argument("--dir", default="log", help="日志目录（默认 log）")
    ap.add_argument("-o", "--out", default="data/dashboard.html", help="输出 HTML 路径")
    args = ap.parse_args()

    summary = analyze_log_dir(args.dir)
    if summary.get("error"):
        print(f"[警告] {summary['error']}（看板将展示空指标）")
        summary = {"total_requests": 0, "status": {}, "events": {}, "latency": {},
                   "llm_tokens": {}, "time_span": {}, "top_endpoints": [],
                   "error_rate_5xx": 0.0}
    snap = snapshot_from_log(summary)
    alerts = evaluate(snap, {"error_rate": 0.05, "latency_p95_ms": 8000,
                              "guardrail_rate": 0.30, "quota_exhausted": True})
    html = render_dashboard_html(
        snap, alerts, summary,
        cfg={"error_rate": 0.05, "latency_p95_ms": 8000, "guardrail_rate": 0.30},
        log_dir=args.dir,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"看板已生成: {out}  (请求 {summary.get('total_requests', 0)} 条, "
          f"告警 {len(alerts)} 条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
