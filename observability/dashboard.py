"""监控看板 HTML 渲染（E4/O18）。

与 HTTP 层解耦：纯函数 render_dashboard_html(snapshot, alerts, log_summary, cfg) -> str，
不依赖 api 包（便于离线单测），不引用任何外部 CSS/JS（零外网依赖）。

样式使用 CSS 变量，随项目深色主题；如需浅色主题，覆写 :root 变量即可。
"""
from __future__ import annotations

_CSS = """
  :root{--bg:#0f1420;--card:#1a2030;--fg:#e6eaf2;--muted:#8b95a7;--accent:#4f8cff;--crit:#ff5d5d;--warn:#ffb02e;--ok:#3ddc84;}
  *{box-sizing:border-box}
  body{font-family:-apple-system,Segoe UI,Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
       background:var(--bg);color:var(--fg);margin:0;padding:24px;}
  h1{font-size:20px;margin:0 0 4px}
  h2{font-size:15px;color:var(--accent);margin:24px 0 10px;border-left:3px solid var(--accent);padding-left:8px}
  .sub{color:var(--muted);font-size:12px;margin-bottom:8px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:900px){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border-radius:10px;padding:14px 16px;overflow:auto}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #2a3142}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  td.tags{color:var(--muted);font-size:11px}
  .alert{border-radius:8px;padding:10px 12px;margin:6px 0;font-size:13px}
  .alert.crit{background:rgba(255,93,93,.15);border:1px solid var(--crit);color:#ffd0d0}
  .alert.warn{background:rgba(255,176,46,.12);border:1px solid var(--warn);color:#ffe6b0}
  .ok{color:var(--ok);font-size:14px}
  .muted{color:var(--muted);font-size:12px}
  code{background:#0c1018;padding:1px 5px;border-radius:4px}
  ul{margin:6px 0;padding-left:18px;font-size:13px;line-height:1.7}
"""


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def _alerts_html(alerts: list[dict]) -> str:
    if not alerts:
        return "<div class='ok'>✓ 当前无告警</div>"
    out = ""
    for a in alerts:
        cls = "crit" if a.get("severity") == "critical" else "warn"
        out += (
            f"<div class='alert {cls}'><b>[{str(a.get('severity', '')).upper()}] "
            f"{a.get('name', '')}</b><br>{a.get('message', '')}</div>"
        )
    return out


def _counters_html(snapshot: dict) -> str:
    rows = ""
    for name, entries in (snapshot.get("counters") or {}).items():
        for e in entries:
            tags = " ".join(f"{k}={v}" for k, v in (e.get("tags") or {}).items())
            rows += (
                f"<tr><td>{name}</td><td class='num'>{_fmt(e.get('value'))}</td>"
                f"<td class='tags'>{tags or '-'}</td></tr>"
            )
    if not rows:
        rows = "<tr><td colspan=3>暂无计数（服务刚启动或尚未产生流量）</td></tr>"
    return rows


def _histograms_html(snapshot: dict) -> str:
    rows = ""
    for name, entries in (snapshot.get("histograms") or {}).items():
        for e in entries:
            tags = " ".join(f"{k}={v}" for k, v in (e.get("tags") or {}).items())
            rows += (
                f"<tr><td>{name}</td><td class='num'>{e.get('count', 0)}</td>"
                f"<td class='num'>{_fmt(e.get('p95', 0))}</td>"
                f"<td class='num'>{_fmt(e.get('p99', 0))}</td>"
                f"<td class='num'>{_fmt(e.get('max', 0))}</td>"
                f"<td class='tags'>{tags or '-'}</td></tr>"
            )
    if not rows:
        rows = "<tr><td colspan=6>暂无分布采样</td></tr>"
    return rows


def _log_html(log_summary: dict) -> str:
    if not isinstance(log_summary, dict) or log_summary.get("error"):
        err = (log_summary or {}).get("error", "日志不可用")
        return f"<div class='muted'>{err}</div>"
    lr = log_summary.get("latency", {}) or {}
    ev = log_summary.get("events", {}) or {}
    span = log_summary.get("time_span", {}) or {}
    return f"""
        <ul>
          <li>日志请求总数：<b>{log_summary.get('total_requests', 0)}</b></li>
          <li>5xx 错误率：<b>{log_summary.get('error_rate_5xx', 0):.2%}</b></li>
          <li>延迟 P95 / P99：<b>{lr.get('p95', 0):.0f} / {lr.get('p99', 0):.0f}</b> ms</li>
          <li>护栏拦截 / 额度熔断 / 模型降级：<b>{ev.get('guardrail_block', 0)}</b>
              / <b>{ev.get('llm_quota_exhausted', 0)}</b>
              / <b>{ev.get('llm_failover', 0)}</b></li>
          <li>过载 / 限流：<b>{ev.get('overloaded', 0)}</b> / <b>{ev.get('rate_limited', 0)}</b></li>
          <li>时间跨度：{span.get('from')} → {span.get('to')}</li>
        </ul>"""


def render_dashboard_html(
    snapshot: dict,
    alerts: list[dict],
    log_summary: dict,
    cfg: dict | None = None,
    log_dir: str = "log",
) -> str:
    """渲染自包含 HTML 看板。

    - snapshot: metrics.snapshot()
    - alerts  : evaluate_alerts() 结果
    - log_summary: observability.log_analyzer 分析结果
    - cfg      : 阈值/开关信息（展示用）
    """
    cfg = cfg or {}
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>影片推荐智能体 · 监控看板</title>
<style>{_CSS}</style></head>
<body>
  <h1>影片推荐智能体 · 监控看板</h1>
  <div class="sub">进程内指标快照 + 日志链路聚合 · 自动刷新 <code>15s</code> ·
    端点 <code>/metrics</code>(Prometheus) · <code>/admin/stats</code>(JSON) ·
    <code>/admin/alerts</code>(告警) · 阈值 错误率≥{cfg.get('error_rate', 0.05):.0%} /
    P95≥{cfg.get('latency_p95_ms', 8000)}ms / 护栏率≥{cfg.get('guardrail_rate', 0.30):.0%}</div>

  <h2>告警（实时规则评估）</h2>
  <div class="card">{_alerts_html(alerts)}</div>

  <div class="grid">
    <div>
      <h2>计数器（Counters）</h2>
      <div class="card"><table>
        <tr><th>指标</th><th>值</th><th>维度</th></tr>{_counters_html(snapshot)}
      </table></div>
    </div>
    <div>
      <h2>分布采样（延迟 / 分位 ms）</h2>
      <div class="card"><table>
        <tr><th>指标</th><th>count</th><th>p95</th><th>p99</th><th>max</th><th>维度</th></tr>{_histograms_html(snapshot)}
      </table></div>
    </div>
  </div>

  <h2>日志链路摘要（解析 {log_dir}/app.log）</h2>
  <div class="card">{_log_html(log_summary)}</div>

  <script>setTimeout(()=>location.reload(), 15000);</script>
</body></html>"""
