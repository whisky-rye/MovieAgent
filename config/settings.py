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

    # ---- 部署环境（决定云端彻底失败后的兜底行为） ----
    # local（默认）：云端全部模型失败后，回退本地 Ollama（deepseek）兜底；
    # production     ：云端全部失败直接返回报错，不做本地兜底。
    APP_ENV: str = "local"         # local | production

    # ---- 云端 LLM（OpenAI 兼容：阿里百炼/硅基流动/火山方舟等） ----
    CLOUD_LLM_BASE_URL: str = ""            # 兼容 base_url，例：https://api.siliconflow.cn/v1
    CLOUD_LLM_API_KEY: str = ""             # 供应商 API Key（仅本地 .env，不入仓）
    CLOUD_LLM_EXTRA_BODY: str = ""          # 供应商特有参数 JSON（可选），例：{"enable_thinking": false}
    # ---- 云端模型强弱分档（各自独立按序降级；可在 .env 自由配置） ----
    # 强档：解释/对话/工具调用（重质量）；前一个失败/熔断自动切下一个
    CLOUD_LLM_STRONG_MODEL_LIST: str = (
        "qwen3.8-27b,kimi-k3,qwen3.8-max-0902"
    )
    # 弱档：意图路由/重排（重速度）；前一个失败/熔断自动切下一个
    CLOUD_LLM_LIGHT_MODEL_LIST: str = (
        "qwen3.7-flash,qwen3.8-flash,deepseek-v4-flash-0731,glm-5.2"
    )
    CLOUD_LLM_MAX_FALLBACKS: int = 4        # 单档单次调用最多触发的降级次数（primary + 4 次 fallback）
    # 云端全部失败后的本地兜底模型（仅 APP_ENV=local 生效）；留空则不做本地兜底
    CLOUD_LLM_LOCAL_FALLBACK: str = "deepseek-r1:7b"
    # 兼容旧字段：强弱两档都未配置时，回退到这个混合链（阿里云百炼 7 模型）
    CLOUD_LLM_MODEL_LIST: str = (
        "qwen3.8-27b,kimi-k3,qwen3.8-max-0902,qwen3.7-flash,"
        "qwen3.8-flash,deepseek-v4-flash-0731,glm-5.2"
    )

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

    # ---- 偏好记忆后端（file / redis / fallback；fallback = Redis 优先 + file 兜底） ----
    MEMORY_BACKEND: str = "file"    # fallback（默认）| redis | file
    MEMORY_FILE_ROOT: str = "data/memory"

    # ---- 日志（精细化可观测：JSON 行 + 滚动文件 + 专用云端 API 调用日志） ----
    LOG_DIR: str = "log"                # 运行期日志目录（.gitignore 已忽略）
    LOG_LEVEL: str = "INFO"             # DEBUG | INFO | WARNING | ERROR
    # 云端 API 请求体/响应体（输入词/输出词文本）记录开关：True=记原文，False=只记字符数不记原文
    LLM_LOG_BODY: bool = False
    # HTTP 请求/响应体记录开关（/recommend、/chat 的 query/answer 片段）；默认关，避免大文本落盘
    ACCESS_LOG_BODY: bool = False

    # ---- 内容安全护栏（P5 补充：敏感拒答 / 越域引导） ----
    GUARDRAIL_ENABLED: bool = True          # 总开关；False 则全部放行
    GUARDRAIL_BLOCK_OFFTOPIC: bool = True   # 越域（非影视）是否引导；False 则交给 Agent 降级处理
    GUARDRAIL_MODE: str = "rule"            # rule（零额度，纯本地规则）| hybrid（边界 case 调 chat_light 复核）

    # ---- 限流（P5 补充：令牌桶，内存级单进程） ----
    RATELIMIT_ENABLED: bool = True   # 总开关；False 则不限制
    RATELIMIT_RATE: float = 0.2      # 令牌补充速率（个/秒）= 每 5 秒允许 1 次真实 LLM 调用
    RATELIMIT_BURST: int = 3         # 桶容量（突发上限）= 允许瞬时最多 3 次真实调用

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def MYSQL_DSN(self) -> str:
        """组装异步 MySQL DSN（aiomysql 驱动，utf8mb4）。"""
        return (
            f"mysql+aiomysql://{self.IMDB_MYSQL_USER}:{self.IMDB_MYSQL_PASS}"
            f"@{self.IMDB_MYSQL_ADDR}/{self.IMDB_MYSQL_DB}?charset=utf8mb4"
        )

    @property
    def cloud_strong_chain(self) -> List[str]:
        """云端强档模型链（按序降级）。两档列表空时回退到旧混合链。"""
        return self._split_chain(self.CLOUD_LLM_STRONG_MODEL_LIST) or self._split_chain(
            self.CLOUD_LLM_MODEL_LIST
        )

    @property
    def cloud_light_chain(self) -> List[str]:
        """云端弱档模型链（按序降级）。两档列表空时回退到旧混合链。"""
        return self._split_chain(self.CLOUD_LLM_LIGHT_MODEL_LIST) or self._split_chain(
            self.CLOUD_LLM_MODEL_LIST
        )

    @property
    def cloud_model_chain(self) -> List[str]:
        """兼容旧字段：返回强档链（优先）。"""
        return self.cloud_strong_chain

    @staticmethod
    def _split_chain(raw: str) -> List[str]:
        raw = (raw or "").strip()
        if not raw:
            return []
        return [m.strip() for m in raw.split(",") if m.strip()]


settings = Settings()
