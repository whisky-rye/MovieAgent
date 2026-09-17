"""O7/E5：CORS 生产白名单拦截中间件。

仅当 APP_ENV=production 时生效：若请求携带 Origin 头且该源不在 CORS_ORIGINS 白名单内，
直接返回 403，明确拒绝未授权跨域来源（不依赖浏览器自行拦截）。

- 无 Origin 头的请求（同源 / curl / 服务端到服务端的 Bearer API 调用）不受影响；
- 非 production 环境保持开发期宽松行为（仅由 CORSMiddleware 按 CORS_ORIGINS 回声）。
"""
from fastapi import Request
from fastapi.responses import JSONResponse

from config.settings import settings


def _allowed_origins() -> set[str]:
    return {o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()}


async def cors_enforce(request: Request, call_next):
    """生产环境 CORS 白名单硬拦截；非生产直接放行。"""
    if settings.APP_ENV != "production":
        return await call_next(request)

    origin = request.headers.get("origin")
    if origin and origin not in _allowed_origins():
        return JSONResponse(
            status_code=403,
            content={"detail": "CORS 拒绝：来源不在生产白名单内"},
        )
    return await call_next(request)
