"""集中式结构化日志（P5 之后补充：精细化可观测）。

为什么单独建：原项目只有两个 getLogger、无 handler/格式/级别，info/warning 大多只进 stderr 丢失；
云端 API、HTTP 请求、告警/错误均无结构化落盘。本模块统一：

- JSON 行格式（便于 ELK/grep），含 ts/level/logger/msg + 任意 extra 维度字段；
- 控制台（stderr） + 主日志 log/app.log（滚动）；
- 专用云端 API 调用日志 log/llm_calls.log（logger=movie_agent.llm），方便单独排查「请求云端 api」细节；
- 级别与目录由 settings 控制；LLM 请求体/响应体由 LLM_LOG_BODY 开关控制（默认关，避免泄露大文本）。

调用：在程序入口（api/main.py）顶部 import 即触发 setup_logging()。
"""
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from config.settings import settings

_CONFIGURED = False

# 结构化日志允许携带的扩展维度（extra= 注入，自动序列化为 JSON 字段）
EXTRA_KEYS = (
    "request_id", "user_id", "model", "provider", "tier", "event",
    "input_tokens", "output_tokens", "cached_tokens", "cache_read",
    "total_tokens", "latency_ms", "cached", "cache_backend",
    "intent", "action", "category", "status_code", "path", "method",
    "client", "retry_after", "input_chars", "output_chars",
    "input_text", "output_text",
)


class JsonFormatter(logging.Formatter):
    """把 record 序列化成单行 JSON；自动带上 EXTRA_KEYS 里出现的自定义字段。"""

    def format(self, record: logging.LogRecord) -> str:
        d: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in EXTRA_KEYS:
            v = getattr(record, k, None)
            if v is not None:
                d[k] = v
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


def setup_logging() -> None:
    """幂等：整个进程只配置一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level = getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO)
    log_dir = settings.LOG_DIR or "log"
    os.makedirs(log_dir, exist_ok=True)
    fmt = JsonFormatter()

    root = logging.getLogger()
    root.setLevel(level)
    # 清理可能来自 reload/库预置的 handler，避免重复
    for h in list(root.handlers):
        root.removeHandler(h)

    # 控制台
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # 主日志（滚动，单文件 5MB × 5 备份）
    app_fh = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=5_000_000, backupCount=5, encoding="utf-8",
    )
    app_fh.setLevel(level)
    app_fh.setFormatter(fmt)
    root.addHandler(app_fh)

    # 专用云端 API 调用日志（movie_agent.llm），同时继续向 root 传播（也进 app.log）
    llm_logger = logging.getLogger("movie_agent.llm")
    llm_fh = RotatingFileHandler(
        os.path.join(log_dir, "llm_calls.log"),
        maxBytes=5_000_000, backupCount=5, encoding="utf-8",
    )
    llm_fh.setLevel(logging.INFO)
    llm_fh.setFormatter(fmt)
    llm_logger.addHandler(llm_fh)
    llm_logger.propagate = True

    # 抑制过于啰嗦的第三方库访问日志（结构化访问日志由本服务中间件负责）
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
