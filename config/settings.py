"""开发期配置中心（pydantic-settings）。

读取本地 .env（含 MySQL 票据等），统一对外暴露配置。
密钥仅来自环境变量/本地 .env，绝不硬编码、不入仓。
"""
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
    LLM_STRONG: str = "qwen3.5:9b"     # 本地已装：通用对话/解释/工具调用
    LLM_LIGHT: str = "deepseek-r1:7b"  # 本地已装：意图路由/重排（较小）

    # ---- 向量库（开发期纯本地） ----
    VECTOR_BACKEND: str = "chroma"   # chroma | qdrant
    CHROMA_PATH: str = "./data/chroma"
    QDRANT_PATH: str = "./data/qdrant"

    # ---- 缓存 / 可观测 ----
    REDIS_URL: str = "redis://localhost:6379/0"
    LANGSMITH_API_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def MYSQL_DSN(self) -> str:
        """组装异步 MySQL DSN（aiomysql 驱动，utf8mb4）。"""
        return (
            f"mysql+aiomysql://{self.IMDB_MYSQL_USER}:{self.IMDB_MYSQL_PASS}"
            f"@{self.IMDB_MYSQL_ADDR}/{self.IMDB_MYSQL_DB}?charset=utf8mb4"
        )


settings = Settings()
