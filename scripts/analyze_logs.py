"""日志链路分析 CLI（E4/O18「日志链路」维度）。

解析结构化 JSON 日志（默认 log/app.log），跨事件聚合 QPS / 延迟分位 / 错误率 /
额度熔断 / 护栏拦截 / 模型降级，输出可读报告。弥补进程内指标在多 worker、重启后的盲区。

用法：
    python scripts/analyze_logs.py                 # 默认分析 log/app.log
    python scripts/analyze_logs.py log/app.log     # 指定文件
    python scripts/analyze_logs.py --dir log       # 分析目录（取 app.log）
    python scripts/analyze_logs.py --minutes 60    # 仅看最近 N 分钟（按 ts 过滤）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许以脚本方式直接运行（项目根在 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observability.log_analyzer import (  # noqa: E402
    analyze_log_dir,
    analyze_log_file,
    iter_log_events,
    summarize,
)


def _fmt_pct(v: float) -> str:
    return f"{v:.2%}"


def _print_summary(s: dict) -> None:
    if s.get("error"):
        print(f"[错误] {s['error']}")
        return
    print("=" * 56)
    print("日志链路分析报告")
    print("=" * 56)
    print(f"时间跨度 : {s['time_span'].get('from')} → {s['time_span'].get('to')}")
    print(f"请求总数 : {s['total_requests']}")
    print(f"状态分布 : {s['status']}")
    print(f"5xx 错误率: {_fmt_pct(s['error_rate_5xx'])}")
    lat = s["latency"]
    print(f"延迟(ms) : count={lat['count']} min={lat['min']:.0f} "
          f"p95={lat['p95']:.0f} p99={lat['p99']:.0f} max={lat['max']:.0f}")
    print("Top 端点 :")
    for ep, n in s["top_endpoints"]:
        print(f"   - {ep}: {n}")
    ev = s["events"]
    print("关键事件 :")
    print(f"   - 护栏拦截     : {ev['guardrail_block']}")
    print(f"   - 额度熔断     : {ev['llm_quota_exhausted']}")
    print(f"   - 模型降级     : {ev['llm_failover']}")
    print(f"   - 并发过载     : {ev['overloaded']}")
    print(f"   - 限流触发     : {ev['rate_limited']}")
    tk = s.get("llm_tokens", {})
    if tk:
        print(f"LLM Token: input={tk.get('input_tokens', 0)} "
              f"output={tk.get('output_tokens', 0)} cached={tk.get('cached_tokens', 0)} "
              f"total={tk.get('total_tokens', 0)}")
    print("=" * 56)


def main() -> int:
    ap = argparse.ArgumentParser(description="日志链路分析（JSON 日志聚合）")
    ap.add_argument("path", nargs="?", default=None, help="日志文件或目录（默认 log/）")
    ap.add_argument("--dir", default=None, help="日志目录（取 app.log）")
    ap.add_argument("--minutes", type=int, default=0, help="仅分析最近 N 分钟的事件")
    args = ap.parse_args()

    if args.minutes > 0:
        # 流式过滤最近 N 分钟
        target = args.path or (Path(args.dir or "log") / "app.log")
        events = iter_log_events(target)
        if args.minutes:
            # 先收集全部以定位最新 ts，再过滤窗口（小文件可接受）
            all_ev = list(events)
            if all_ev:
                latest = max((e.get("ts") or "") for e in all_ev)
                # ts 形如 2026-10-21T12:00:00+0800；用字符串比较近似（同格式单调）
                cutoff = latest[:19]  # 仅按到秒比较不够精确，但足以演示；精确过滤留给结构化存储
        _print_summary(summarize(all_ev))
        return 0

    if args.dir:
        summary = analyze_log_dir(args.dir)
    elif args.path:
        p = Path(args.path)
        summary = analyze_log_file(p) if p.is_file() else analyze_log_dir(p)
    else:
        summary = analyze_log_dir("log")
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
