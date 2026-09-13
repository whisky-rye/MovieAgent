"""开发期配置中心（pydantic-settings）。

读取本地 .env（含 MySQL 票据等），统一对外暴露配置。
密钥仅来自环境变量/本地 .env，绝不硬编码、不入仓。
"""
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ---- MySQL 票据（用户提供的 IMDB_MYSQL_*） ----
    IMDB_MYSQL_USER: str = "root"
    IMDB_MYSQL_PASS: str = "root"
    IMDB_MYSQL_ADDR: str = "127.0.0.1:3306"
    IMDB_MYSQL_DB: str = "imdb"

    # ---- 本地 Ollama（已就绪，仅核对） ----
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"  # Ollama 默认端口（实测含 bge-m3/qwen3.5/deepseek）
    EMBED_MODEL: str = "bge-m3:567m"   # 本地已装镜像（非默认 bge-m3）
    LLM_STRONG: str = "deepseek-r1:7b"  # 本地已装：统一用 DeepSeek（STRONG/LIGHT 一致，用户要求）
    LLM_LIGHT: str = "deepseek-r1:7b"  # 本地已装：意图路由/重排（较小）

    # ---- LLM Provider 开关（本地优先 / 云端可切换） ----
    LLM_PROVIDER: str = "ollama"   # ollama（本地优先） | cloud（OpenAI 兼容网关）
    # ---- 云端 LLM（OpenAI 兼容：七牛/硅基流动/阿里百炼/火山方舟等） ----
    CLOUD_LLM_BASE_URL: str = ""            # 兼容 base_url，例：https://api.siliconflow.cn/v1
    CLOUD_LLM_API_KEY: str = ""             # 供应商 API Key（仅本地 .env，不入仓）
    CLOUD_LLM_STRONG_MODEL: str = "deepseek-chat"   # 云端强档模型名（被下方列表首模型覆盖时仅兜底）
    CLOUD_LLM_LIGHT_MODEL: str = "deepseek-chat"    # 云端弱档模型名
    CLOUD_LLM_EXTRA_BODY: str = ""          # 供应商特有参数 JSON（可选），例：{"enable_thinking": false}
    # ---- 云端模型顺序降级链（阿里云百炼提供，按此顺序尝试，前一个失败/熔断自动切下一个） ----
    # 顺序即优先级；命中 403 AllocationQuota.FreeTierOnly（免费额度用完）的模型会被永久熔断，后续跳过
    CLOUD_LLM_MODEL_LIST: str = (
        "qwen3.8-27b,kimi-k3,qwen3.8-max-0902,qwen3.7-flash,"
        "qwen3.8-flash,deepseek-v4-flash-0731,glm-5.2"
    )
    CLOUD_LLM_MAX_FALLBACKS: int = 4        # 单次调用最多触发的降级次数（primary + 4 次 fallback）

    # ---- 向量库（开发期纯本地） ----
    VECTOR_BACKEND: str = "chroma"   # chroma | qdrant
    CHROMA_PATH: str = "./data/chroma"
    QDRANT_PATH: str = "./data/qdrant"

    # ---- 缓存 / 可观测 ----
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""
    REDIS_TTL: int = 300         # 召回结果缓存 TTL（秒）
    LANGSMITH_API_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def MYSQL_DSN(self) -> str:
        """组装异步 MySQL DSN（aiomysql 驱动，utf8mb4）。"""
        return (
            f"mysql+aiomysql://{self.IMDB_MYSQL_USER}:{self.IMDB_MYSQL_PASS}"
            f"@{self.IMDB_MYSQL_ADDR}/{self.IMDB_MYSQL_DB}?charset=utf8mb4"
        )

    @property
    def cloud_model_chain(self) -> List[str]:
        """云端模型有序降级链：优先用 CLOUD_LLM_MODEL_LIST，缺省回退到 STRONG/LIGHT。"""
        raw = (self.CLOUD_LLM_MODEL_LIST or "").strip()
        if raw:
            return [m.strip() for m in raw.split(",") if m.strip()]
        return [m for m in (self.CLOUD_LLM_STRONG_MODEL, self.CLOUD_LLM_LIGHT_MODEL) if m]


settings = Settings()
