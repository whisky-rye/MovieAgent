"""E4/O18 监控看板路由：Prometheus 指标端点 + JSON 快照 + HTML 看板 + 日志链路摘要。

- GET /metrics              Prometheus 文本格式（可被 Prometheus 直接 scrape）
- GET /admin/stats          JSON：指标快照 + 当前告警 + 日志链路摘要（便于自动化/外部看板）
- GET /admin/dashboard      HTML 自包含看板（零外部依赖，直接浏览器打开）
- GET /admin/alerts         JSON：仅当前告警列表（便于告警聚合器轮询）

HTML 渲染逻辑在 observability/dashboard.py（与 HTTP 层解耦、可离线单测）。
所有端点均受 METRICS_ENABLED / DASHBOARD_ENABLED / PROMETHEUS_ENABLED 开关控制。

访问控制（P1-4）：监控端点暴露内部指标/告警/日志链路摘要（错误率、峰值路径、
熔断状态），属侦察面。整个 router 挂 require_metrics_access：
- 配置 METRICS_ADMIN_TOKEN → 必须携带匹配的 X-Admin-Token 头（或 HTML 看板
  用 ?token= 查询参数，便于浏览器直接打开；恒定时间比较）；
- 未配置 → 退回用户鉴权（AUTH_ENABLED=true 需登录；调试态开放，启动有告警）。
"""
import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from api.deps import get_current_user
from config.settings import settings
from observability.metrics import metrics
from observability.alerting import evaluate_alerts, emit_alerts
from observability.log_analyzer import analyze_log_dir
from observability.dashboard import render_dashboard_html

logger = logging.getLogger("movie_agent.metrics")


async def require_metrics_access(request: Request) -> None:
    """监控端点访问控制（P1-4，router 级依赖）。

    与用户鉴权开关解耦：监控是运维面而非用户面，即便 AUTH_ENABLED=false 的
    调试部署，也可用 METRICS_ADMIN_TOKEN 把端点锁起来。
    """
    if settings.METRICS_ADMIN_TOKEN:
        submitted = (request.headers.get("X-Admin-Token")
                     or request.query_params.get("token", ""))
        if not submitted or not hmac.compare_digest(submitted, settings.METRICS_ADMIN_TOKEN):
            raise HTTPException(
                status_code=401,
                detail="监控端点需要有效的 Admin Token（X-Admin-Token 头或 ?token= 参数）",
            )
        return
    # 未配置独立 token：退回用户鉴权（生产需登录；调试态开放，启动有显式告警）
    await get_current_user(request)


router = APIRouter(dependencies=[Depends(require_metrics_access)])


def _thresholds() -> dict:
    return {
        "error_rate": settings.ALERT_ERROR_RATE,
        "latency_p95_ms": settings.ALERT_LATENCY_P95_MS,
        "guardrail_rate": settings.ALERT_GUARDRAIL_RATE,
    }


def _log_summary() -> dict:
    try:
        return analyze_log_dir(settings.LOG_DIR)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


@router.get("/metrics")
def prometheus_metrics():
    if not settings.PROMETHEUS_ENABLED:
        return PlainTextResponse(
            "# Prometheus 端点已关闭（PROMETHEUS_ENABLED=false）\n", status_code=200)
    return PlainTextResponse(metrics.to_prometheus(),
                             media_type="text/plain; version=0.0.4")


@router.get("/admin/alerts")
def get_alerts():
    alerts = evaluate_alerts()
    return JSONResponse({"alerts": alerts, "count": len(alerts)})


@router.get("/admin/stats")
def get_stats():
    return JSONResponse({
        "metrics": metrics.snapshot(),
        "alerts": evaluate_alerts(),
        "log_chain": _log_summary(),
        "config": {
            "metrics_enabled": settings.METRICS_ENABLED,
            "alert_error_rate": settings.ALERT_ERROR_RATE,
            "alert_latency_p95_ms": settings.ALERT_LATENCY_P95_MS,
            "alert_guardrail_rate": settings.ALERT_GUARDRAIL_RATE,
        },
    })


@router.get("/admin/dashboard", response_class=HTMLResponse)
def dashboard():
    if not settings.DASHBOARD_ENABLED:
        return HTMLResponse("<h2>看板已关闭（DASHBOARD_ENABLED=false）</h2>", status_code=200)
    # 访问即评估一次告警（后台循环未启用时也保证 /admin/stats / 看板能看到最新告警）
    alerts = []
    try:
        alerts = evaluate_alerts()
        if alerts:
            emit_alerts(alerts)
    except Exception:  # noqa: BLE001
        pass
    html = render_dashboard_html(
        metrics.snapshot(),
        alerts,
        _log_summary(),
        cfg=_thresholds(),
        log_dir=settings.LOG_DIR,
    )
    return HTMLResponse(html)
