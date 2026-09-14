"""LangSmith 全链路追踪接入（P4.4）。

设计要点：
- 在程序入口（api/main.py 模块加载时）调用一次 setup_langsmith() 即可，无需改动任何业务调用。
- 原理：LangChain/LangGraph 在「运行时」读取环境变量决定是否上报追踪。本模块把
  settings 里的开关与凭证写入标准环境变量（LANGCHAIN_TRACING_V2 / LANGSMITH_*），
  之后所有 BaseChatModel（FailoverChatModel / ChatOllama）与 LangGraph app 的 invoke/stream
  都会自动把 LLM 调用、图节点、工具调用上报到 LangSmith。
- 同时写 LANGCHAIN_* 老前缀与 LANGSMITH_* 新前缀，兼容不同版本 SDK（langchain-core 1.x / langsmith 0.12+）。
- 根 run 通过 run_metadata() 注入 metadata（app_env / user_id / request_id / llm_provider 等），
  便于在 LangSmith 控制台按用户、请求、环境过滤与对比。

关闭条件：LANGSMITH_TRACING=False，或 LANGSMITH_API_KEY 缺失 → 自动退化为关闭（不抛错、不影响业务）。
"""
import logging
import os

from config.settings import settings

logger = logging.getLogger("movie_agent.langsmith")

# LangSmith 公有云默认端点；如需私有部署（self-hosted）在 .env 覆盖 LANGSMITH_ENDPOINT 即可。
_DEFAULT_ENDPOINT = "https://api.smith.langchain.com"


def setup_langsmith() -> bool:
    """应用入口调用一次。返回是否真正启用了追踪。

    必须早于任何 LangChain/LangGraph 的 invoke 调用；在 api/main.py 顶部
    setup_logging() 之后调用即可（模块加载即执行）。
    """
    if not settings.LANGSMITH_TRACING:
        return False
    if not settings.LANGSMITH_API_KEY:
        logger.warning(
            "LangSmith 追踪未启用：LANGSMITH_TRACING=开启但缺少 API Key，已退化为关闭",
            extra={"event": "langsmith_disabled", "reason": "missing_api_key"},
        )
        return False

    endpoint = settings.LANGSMITH_ENDPOINT or _DEFAULT_ENDPOINT
    project = settings.LANGSMITH_PROJECT or "movie_agent"

    # 新前缀（langsmith 0.12+）
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.LANGSMITH_API_KEY
    os.environ["LANGSMITH_PROJECT"] = project
    os.environ["LANGSMITH_ENDPOINT"] = endpoint
    # 老前缀（langchain-core 1.x 仍读这些）
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.LANGSMITH_API_KEY
    os.environ["LANGCHAIN_PROJECT"] = project
    os.environ["LANGCHAIN_ENDPOINT"] = endpoint

    logger.info(
        "LangSmith 全链路追踪已启用",
        extra={"event": "langsmith_enabled", "project": project, "endpoint": endpoint,
               "app_env": settings.APP_ENV},
    )
    return True


def run_metadata(user_id: str = "", request_id: str = "", **extra) -> dict:
    """构造注入到 LangSmith 根 run 的 metadata（便于过滤/对比）。

    api/main.py 在 _AGENT.invoke(state, config={"metadata": run_metadata(...)}) 透传。
    仅放低风险、可观测的标签；不放入任何 PII 正文。
    """
    md = {
        "app": "movie-agent",
        "app_env": settings.APP_ENV,
        "llm_provider": settings.LLM_PROVIDER,
        "fusion_strategy": settings.FUSION_STRATEGY,
        "vector_backend": settings.VECTOR_BACKEND,
        "embed_provider": settings.EMBED_PROVIDER,
        "user_id": user_id or "-",
        "request_id": request_id or "-",
    }
    # 允许调用方追加任意标量标签（如 intent、cache_hit 等）
    for k, v in extra.items():
        if v is not None:
            md[k] = v
    return md
