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

    # ---- 新业务库 movie_agent（user / session / preference 等新增表统一落这里，与 imdb 语料库隔离）----
    # host / 账号 / 密码 直接复用上方 IMDB_MYSQL_*（同一 MySQL 实例，仅库名不同）
    MOVIE_AGENT_MYSQL_DB: str = "movie_agent"

    # ---- 前端跨域（G2/O7）：允许的前端源（白名单），逗号分隔 ----
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"
    # 生产收紧：仅暴露业务所需的最小方法/头集合（替代原先的 "*"）
    CORS_ALLOW_METHODS: str = "GET,POST,OPTIONS"
    CORS_ALLOW_HEADERS: str = "Content-Type,Authorization,X-CSRF-Token,X-Requested-With"

    # ---- 会话存储（G3）：强 MySQL——sessions 表落 movie_agent 库；连不上/表缺失则服务拒绝启动（无 file 兜底） ----
    SESSION_MAX_MESSAGES: int = 200            # 单会话消息上限（超出截断最旧，防无限膨胀）

    # ---- 会话共享（公开只读查看 /share/{token}）：shared_sessions 表落 movie_agent，强 MySQL、无 file 兜底 ----
    # 共享链接对外域名前缀：复制出去的链接 = {APP_PUBLIC_ORIGIN}/share/{token}，外部访客直接打开即可。
    # 部署时改成前端实际对外地址（如 https://movie.example.com）；留空则回退 VITE_API_BASE 同源前端。
    APP_PUBLIC_ORIGIN: str = "http://localhost:5173"

    # ---- 鉴权（G1）：登录/注册 token（零新依赖，stdlib pbkdf2 + HMAC） ----
    # 总开关：true=强制 Bearer token 鉴权（业务 user_id 取自 token、忽略 body 防越权）；
    #         false=退回 body/query.user_id 旧模式（调试兼容，现有 curl/前端联调不中断）
    AUTH_ENABLED: bool = False
    AUTH_SECRET: str = ""                       # HMAC 签名密钥（必填；生产走 Secret/Vault，本地 .env，不入仓）
    AUTH_TOKEN_TTL: int = 604800                # token 有效期（秒），默认 7 天
    AUTH_PBKDF2_ITERATIONS: int = 100000        # 密码哈希迭代次数
    # ---- 鉴权 Cookie / CSRF（Token→httpOnly Cookie 方案） ----
    AUTH_COOKIE_NAME: str = "ma_token"          # 鉴权 Cookie（HttpOnly，不可被 JS 读取，防 XSS 窃取）
    AUTH_COOKIE_SAMESITE: str = "lax"           # lax：允许同站跨端口（前端 5173 ↔ 后端 8000）携带
    CSRF_COOKIE_NAME: str = "ma_csrf"           # 双提交 CSRF token（非 HttpOnly，供前端 JS 读取）
    CSRF_HEADER_NAME: str = "X-CSRF-Token"      # 前端把 ma_csrf 值放入此请求头，后端校验一致
    COOKIE_SECURE: bool = False                 # True=仅 HTTPS 传输；生产(HTTPS)自动置 True（见 auth 路由）

    # ---- 本地 Ollama（已就绪，仅核对） ----
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"  # Ollama 默认端口（实测含 bge-m3/qwen3.5/deepseek）
    EMBED_MODEL: str = "bge-m3:567m"   # 本地已装镜像（非默认 bge-m3）

    # ---- 嵌入 Provider 开关（本地 Ollama bge-m3 | 云端 阿里百炼 qwen 嵌入） ----
    # ollama：本地 bge-m3，零成本、零联网、最稳；开发期默认。
    # cloud ：阿里百炼 compatible-mode /v1/embeddings（qwen3.7-text-embedding 等），
    #         需联网+额度，中文语义/维度可调，适合对齐线上检索分布。
    EMBED_PROVIDER: str = "ollama"     # ollama | cloud
    # 向量维度（建库/召回必须一致）。本地 bge-m3=1024；云端 qwen 嵌入默认 1024 亦可配 512/768/1536。
    EMBED_DIM: int = 1024
    # 云端嵌入接入（阿里百炼）：默认复用 CLOUD_LLM_BASE_URL 同一 compatible-mode 网关与 API Key
    CLOUD_EMBED_BASE_URL: str = ""     # 留空则复用 CLOUD_LLM_BASE_URL（阿里百炼 compatible-mode）
    CLOUD_EMBED_API_KEY: str = ""      # 留空则复用 CLOUD_LLM_API_KEY
    CLOUD_EMBED_MODEL: str = "qwen3.7-text-embedding"
    # 是否向云端嵌入请求传 dimensions（qwen 嵌入支持动态维度；部分供应商不支持则关）
    CLOUD_EMBED_USE_DIMENSIONS: bool = True
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

    # ---- 向量库（开发期纯本地，支持多后端切换） ----
    # chroma  ：开发期默认，进程内文件落盘，无需服务。
    # qdrant  ：本地嵌入式文件模式（qdrant-client 本地直装，无需起服务/Docker）。
    # milvus  ：Milvus Lite 单文件嵌入式（milvus-lite + pymilvus），超大规模预留。
    VECTOR_BACKEND: str = "chroma"   # chroma | qdrant | milvus
    CHROMA_PATH: str = "./data/chroma"
    QDRANT_PATH: str = "./data/qdrant"
    MILVUS_PATH: str = "./data/milvus_lite.db"   # Milvus Lite 单文件

    # ---- 建库/导入（P 导入脚本分批分页用） ----
    # 每批从 MySQL 主表拉取的行数（LIMIT/OFFSET 分页），避免全量 SELECT 一次拉爆内存。
    BUILD_PAGE_SIZE: int = 5000
    # 云端嵌入并发线程数（cloud 网络 IO 密集，建议 4~8；本地 Ollama 单线程即可，设 1）。
    EMBED_CONCURRENCY: int = 1

    # ---- 缓存 / 可观测 ----
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""
    REDIS_TTL: int = 300         # 召回结果缓存 TTL（秒）
    # ---- LangSmith 全链路追踪（P4.4） ----
    # 启用后：LangChain/LangGraph 的 LLM 调用与图节点 invoke 自动上报到 LangSmith。
    LANGSMITH_API_KEY: str = ""          # 默认空 = 关闭；填入后配合 LANGSMITH_TRACING 生效
    LANGSMITH_TRACING: bool = False     # True 时启用；False 或 Key 缺失则自动退化为关闭
    LANGSMITH_PROJECT: str = "movie_agent"
    LANGSMITH_ENDPOINT: str = "https://api.smith.langchain.com"

    # ---- 偏好记忆后端：强 MySQL（落 movie_agent.user_preferences 表，无 file / redis / fallback 兜底） ----
    # 业务代码（agent/memory.py）强制走 MySQLMemoryStore，MySQL 不可达则服务拒绝启动。

    # ---- 日志（精细化可观测：JSON 行 + 滚动文件 + 专用云端 API 调用日志） ----
    LOG_DIR: str = "log"                # 运行期日志目录（.gitignore 已忽略）
    LOG_LEVEL: str = "INFO"             # DEBUG | INFO | WARNING | ERROR
    # 云端 API 请求体/响应体（输入词/输出词文本）记录开关：True=记原文，False=只记字符数不记原文
    LLM_LOG_BODY: bool = False
    # HTTP 请求/响应体记录开关（/recommend、/chat 的 query/answer 片段）；默认关，避免大文本落盘
    ACCESS_LOG_BODY: bool = False

    # ---- 内容安全护栏（P5 补充：敏感拒答 / 越域引导 / 提示注入防护） ----
    GUARDRAIL_ENABLED: bool = True          # 总开关；False 则全部放行
    GUARDRAIL_BLOCK_OFFTOPIC: bool = True   # 越域（非影视）是否引导；False 则交给 Agent 降级处理
    GUARDRAIL_MODE: str = "rule"            # rule（零额度，纯本地规则）| hybrid（边界 case 调 chat_light 复核）
    # —— 高规格分层防护（S3：输入净化 + 边界约束）——
    # 提示注入 / 越狱尝试：命中即硬拒答（更高规格，默认拦截），不进入 Agent、不耗额度。
    GUARDRAIL_BLOCK_INJECTION: bool = True
    # 生成后输出护栏：扫描回答中的注入回显 / 策略泄漏 / 有害 how-to，命中以安全文案兜底替换。
    GUARDRAIL_OUTPUT_GATE: bool = True
    # 检索上下文净化：喂给 LLM 的检索片段去除注入标记 / 零宽字符，降低 RAG 被注入风险。
    GUARDRAIL_SANITIZE_CONTEXT: bool = True
    # 输入长度上限（字符），超长截断，防止畸形/填充攻击。
    GUARDRAIL_MAX_QUERY_LEN: int = 2000

    # ---- 可观测 / 监控告警（E4/O18：指标采集 + 看板 + 主动预警） ----
    # 进程内指标注册表（零依赖，线程安全）：QPS / 延迟 / 错误率 / 额度消耗 / 降级熔断 等。
    # 多 worker 场景：各 worker 独立计数（看板/指标取「当前进程」快照；Prometheus
    # 文本端点可被各 worker 分别 scrape 后聚合）。如需跨进程汇总，后续可接 Redis 后端。
    METRICS_ENABLED: bool = True          # 总开关；False 则停止埋点（零开销）
    PROMETHEUS_ENABLED: bool = True       # GET /metrics（Prometheus 文本格式）开关
    DASHBOARD_ENABLED: bool = True        # GET /admin/dashboard（HTML 看板）与 /admin/stats（JSON）开关
    ALERT_WEBHOOK_URL: str = ""          # 告警回调（钉钉/飞书/Slack incoming webhook）；空=仅落日志
    ALERT_EVAL_INTERVAL: int = 60        # 后台告警评估周期（秒）；0=关闭后台循环（仍可在 /admin/stats 手动触发）
    # —— 告警阈值（基于指标快照评估）——
    ALERT_ERROR_RATE: float = 0.05       # 5xx 占总请求比 ≥ 此值 → critical（疑似故障）
    ALERT_LATENCY_P95_MS: int = 8000     # 推荐链路 P95 延迟 ≥ 此值(ms) → warning（体验劣化）
    ALERT_GUARDRAIL_RATE: float = 0.30   # 护栏拦截占总请求比 ≥ 此值 → warning（疑似越域风暴/被攻击）
    ALERT_LLM_QUOTA_EXHAUSTED: bool = True  # 任一模型免费额度熔断 → critical（计费风险，必须告警）

    # ---- MCP 接入：生产库只读通道（docs/MCP集成可行性调研报告.md）----
    # 🔴 红线：远端生产库仅允许 SELECT。本通道所有 SQL 必先过 mcp_prod/guard.py
    #    （语句/表前缀白名单 + 禁多语句 + 会话 READ ONLY + 限流 + 熔断）。
    # 按调研报告 §5：MCP 定位为「补充/校验通道」，**默认关闭**，主召回仍走本地强 MySQL。
    MCP_ENABLED: bool = False            # 总开关：默认关闭（按需开启）
    MCP_SERVER_NAME: str = "movie-agent-prod-readonly"
    MCP_TRANSPORT: str = "stdio"         # stdio | sse | streamable-http
    # —— 凭据：仅从环境变量注入，**无默认值**（不入库、不落日志）；留空即视为未配置 ——
    MCP_DB_HOST: str = ""
    MCP_DB_PORT: int = 3306
    MCP_DB_USER: str = ""
    MCP_DB_PASSWORD: str = ""
    MCP_DB_IMDB: str = "imdb"            # 仅读 imdb_* 表
    MCP_DB_DOUBAN: str = "imdb2"         # 仅读 douban_* 表
    # —— 限流 / 熔断 / 超时（防打爆生产库，调研报告 §3.4/§4）——
    MCP_RATE_LIMIT_QPM: int = 60         # 每分钟最大查询数
    MCP_RATE_LIMIT_BURST: int = 10       # 突发桶容量
    MCP_CIRCUIT_FAIL_THRESHOLD: int = 3  # 连续失败达阈值 → 熔断（fail-fast）
    MCP_CIRCUIT_COOLDOWN: int = 60       # 熔断冷却秒数
    MCP_QUERY_TIMEOUT: int = 15          # 单次查询超时（秒）
    MCP_CONNECT_TIMEOUT: int = 10        # 连接超时（秒）
    MCP_MAX_LIMIT: int = 200             # 单次返回最大行数（护栏 LIMIT 钳制）
    MCP_POOL_SIZE: int = 4               # 连接池容量（按库各一份）
    MCP_CACHE_TTL: int = 60              # 客户端短 TTL 缓存（秒），降低生产库压力
    MCP_AUDIT_LOG: bool = True           # 查询审计日志（绝不记录凭据/参数明文）

    # ---- 知识图谱召回（F1/O12） ----
    # 默认开启：多跳图谱召回（影片→人物→影片 + 可选类型/地区）并入融合，
    # 解决原 relation_recall 仅 1 跳「同剧组」覆盖不足的问题。生产可整库换 Neo4j（接口不变）。
    GRAPH_ENABLED: bool = True
    GRAPH_MAX_HOPS: int = 2                 # 多跳层数（2 = 影片→人物→影片→人物→影片）
    GRAPH_EXPAND_GENRE: bool = True         # 是否经「同类型」扩展（增加多样性）
    GRAPH_EXPAND_COUNTRY: bool = False      # 是否经「同地区」扩展（默认关，信号弱）
    GRAPH_CACHE_MAX: int = 20000            # 邻接缓存容量上限（FIFO 淘汰）

    # ---- 限流（P5 补充：令牌桶） ----
    # 后端：redis（默认，多进程/多机部署）= 共享桶，总配额稳定 = 单桶配额；
    #       memory（单进程/调试）= 每 worker 各一个桶（多 worker 时总配额 = 单桶配额 × workers）。
    #       Redis 不可达时自动回退 memory 并告警。
    # 注意：多 worker 部署（P0）务必用 redis 后端，否则各 worker 各自计数，限流形同虚设。
    RATELIMIT_ENABLED: bool = True   # 总开关；False 则不限制
    RATELIMIT_BACKEND: str = "redis"  # memory | redis
    RATELIMIT_REDIS_URL: str = ""    # 留空则复用 REDIS_URL（共享令牌桶）

    # ---- 并发控制（P0 多 worker + 每请求客户端） ----
    # 单 worker 内允许「真正并行」执行的 Agent/LLM 调用数（其余排队）。配合多 worker 部署，
    # 理论并发 ≈ workers × MAX_CONCURRENT_RUNS。值过大可能压垮本地模型 / 触发云端额度熔断，按需调小。
    # 取代旧版「全局串行锁」——不再把全部请求单飞成 1 并发，而是有界并行。
    MAX_CONCURRENT_RUNS: int = 4
    # 并发槽获取超时（秒）：超过则视为过载，请求返回 503（而非无限阻塞占用连接）。
    RUN_SLOT_TIMEOUT: int = 30
    # 多 worker 启动时的 worker 数（仅 api/main.py 的 __main__ 入口读取；命令行 `--workers` 优先）。
    # 1 = 单进程（开发期默认）；生产多核机器建议 2~4。
    UVICORN_WORKERS: int = 1
    RATELIMIT_RATE: float = 0.2      # 令牌补充速率（个/秒）= 每 5 秒允许 1 次真实 LLM 调用
    RATELIMIT_BURST: int = 3         # 桶容量（突发上限）= 允许瞬时最多 3 次真实调用

    # ---- 召回融合策略（v2.14：RRF 开关） ----
    # 'rrf'  ：基于排名位次的融合（默认），免疫各路分数量级差异，新增召回路零成本接入
    # 'score'：原 min-max 归一化加权融合，向后兼容回退用
    FUSION_STRATEGY: str = "rrf"
    # RRF 常数 K（名次惩罚项）：越大则高排名优势相对越弱、各路越平等；常用 60
    RRF_K: int = 60
    # RRF 来源权重（P3 配置化）：内容相似最高、关系次之、冷启动兜底最低
    # （避免「同剧组」关系召回过强）。格式 key:value 逗号分隔；仅作相对偏置，可省略。
    # graph：知识图谱多跳召回（F1/O12），与 relation 同源但覆盖更广（多跳+类型/地区），权重略低于 relation。
    # prod ：生产库只读通道补充召回（MCP），默认关闭（MCP_ENABLED=false 时该路无产出），权重最低。
    RRF_WEIGHTS: str = "content:1.0,relation:0.8,coldstart:0.6,graph:0.7,person:0.9,prod:0.5"

    # ---- 二级重排 pipeline（P2/P3 配置化） ----
    # 融合后是否做 MMR 多样性重排（避免清一色同类型）；默认开，等价原 strategy='mmr'
    RERANK_DIVERSIFY: bool = True
    # 可选 LLM pointwise 精排：对前 N 名做 LIGHT 相关性微调；0=关闭（默认，控成本）
    RERANK_LLM_TOPN: int = 30
    # MMR 多样性强度 λ：0=只看融合分，1=只看多样性；0.3 为常用值
    MMR_LAMBDA: float = 0.3
    # ---- 精排实现开关（新接入点：阿里百炼重排序模型） ----
    # 阶段二「精排」的实现选择：
    #   local ：本地 LIGHT 模型 pointwise 精排（旧逻辑，默认，零成本零联网）
    #   cloud ：阿里百炼 qwen3.7-text-rerank 重排序模型（新接入点，需联网+额度）
    # 无论哪种，精排都只对前 RERANK_LLM_TOPN 名做（控成本）；RERANK_LLM_TOPN=0 则整段关闭。
    RERANK_PROVIDER: str = "cloud"     # local | cloud
    CLOUD_RERANK_MODEL: str = "qwen3.7-text-rerank"
    # 重排端点与 LLM/嵌入不同路径（百炼走 /api/v1/services/rerank/...）。留空则按 CLOUD_LLM_BASE_URL 推导。
    CLOUD_RERANK_BASE_URL: str = ""
    CLOUD_RERANK_API_KEY: str = ""     # 留空则复用 CLOUD_LLM_API_KEY（同一供应商，同一 Key）
    # 是否传 instruct（自定义排序任务指令）；默认 True（推荐场景：匹配用户偏好）
    CLOUD_RERANK_USE_INSTRUCT: bool = True

    # ---- O11 用户反馈调权闭环 ----
    # 是否启用「反馈→排序」回灌（默认开；保留开关便于回退/灰度；环境变量 FEEDBACK_REWEIGHT_ENABLED 可关）
    FEEDBACK_REWEIGHT_ENABLED: bool = True
    # 反馈时间衰减半衰期（天）：旧反馈按 0.5^(age/半衰期) 淡出（环境变量 FEEDBACK_HALF_LIFE_DAYS 可改）
    FEEDBACK_HALF_LIFE_DAYS: int = 30

    # ---- 推荐海报生成（阿里百炼视觉模型，G-Poster） ----
    # 每次对话完成后，调用百炼视觉模型为推荐列表 Top3 生成宣传海报（文生图）。
    # 与 rerank/embed 同一供应商、同一 Key，专属键留空即回退 CLOUD_LLM_*。
    # 随机触发模型见 agent/poster.py POSTER_MODELS（当前为北京 MaaS 实际可用的三款文生图模型）。
    # 端点走百炼「模型服务」统一生成接口：/services/aigc/multimodal-generation/generation
    #   —— 公共 dashscope 端点的 text2image/image-synthesis 路径已废弃/不可用，必须用北京专属 MaaS 网关域名。

    # 北京 MaaS 网关域名二选一提供：
    #   方案 A（推荐）：填 WorkspaceId，自动拼成 https://{WS}.{REGION}.maas.aliyuncs.com/api/v1
    POSTER_WORKSPACE_ID: str = ""
    POSTER_REGION: str = "cn-beijing"
    #   方案 B：直接给完整 base（含 /api/v1）。优先级高于方案 A；两者皆空则回退 CLOUD_LLM_BASE_URL
    CLOUD_POSTER_BASE_URL: str = ""
    CLOUD_POSTER_API_KEY: str = ""          # 留空则复用 CLOUD_LLM_API_KEY
    # 统一生成接口路径（MaaS 视觉模型通用，文生图/文生视频共用此路径）
    POSTER_ENDPOINT_PATH: str = "/services/aigc/multimodal-generation/generation"

    # 海报落盘目录（相对项目根）；静态下载经 /poster/download/{session_id} 路由，无需挂载 StaticFiles
    POSTER_DATA_DIR: str = "data/poster"

    # ---- 语义缓存（O8：近义 query 命中同一份缓存） ----
    # 复用向量库建独立集合存 query 嵌入，新请求算嵌入后 ANN 检索，相似度≥阈值即命中。
    # embed 失败自动降级为「仅精确匹配」（不阻断主链路）。
    SEMANTIC_CACHE_ENABLED: bool = True
    SEMANTIC_CACHE_THRESHOLD: float = 0.92   # 余弦相似度阈值：≥ 此值视为同义，复用候选
    QUERY_CACHE_COLLECTION: str = "query_cache"
    # 生成失败/无 Key/无网络时是否自动降级为占位图（PIL 生成，便于断网演示）；默认 False（仅记日志、前端不显示海报块）
    POSTER_FALLBACK_PLACEHOLDER: bool = False
    # 异步任务轮询上限（秒）与轮询间隔（秒）
    POSTER_TASK_TIMEOUT: int = 300
    POSTER_TASK_POLL: int = 3

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def MYSQL_DSN(self) -> str:
        """组装异步 MySQL DSN（aiomysql 驱动，utf8mb4）。"""
        return (
            f"mysql+aiomysql://{self.IMDB_MYSQL_USER}:{self.IMDB_MYSQL_PASS}"
            f"@{self.IMDB_MYSQL_ADDR}/{self.IMDB_MYSQL_DB}?charset=utf8mb4"
        )

    @property
    def MOVIE_AGENT_MYSQL_DSN(self) -> str:
        """同步 MySQL DSN，指向 movie_agent 业务库（pymysql 驱动，utf8mb4）。"""
        return (
            f"mysql+pymysql://{self.IMDB_MYSQL_USER}:{self.IMDB_MYSQL_PASS}"
            f"@{self.IMDB_MYSQL_ADDR}/{self.MOVIE_AGENT_MYSQL_DB}?charset=utf8mb4"
        )

    @property
    def MOVIE_AGENT_MYSQL_ROOT_DSN(self) -> str:
        """无库名 DSN，用于首次启动时 CREATE DATABASE（同一实例、同一账号）。"""
        return (
            f"mysql+pymysql://{self.IMDB_MYSQL_USER}:{self.IMDB_MYSQL_PASS}"
            f"@{self.IMDB_MYSQL_ADDR}/?charset=utf8mb4"
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

    @property
    def RRF_WEIGHTS_DICT(self) -> dict:
        """把 RRF_WEIGHTS 字符串解析为 {canon_key: float} 权重字典。

        例：'content:1.0,relation:0.8,coldstart:0.6' -> {'content':1.0,'relation':0.8,'coldstart':0.6}
        解析失败项自动跳过；空字符串返回 {}（即全部等权 1.0）。
        """
        out: dict = {}
        for kv in (self.RRF_WEIGHTS or "").split(","):
            kv = kv.strip()
            if not kv or ":" not in kv:
                continue
            k, v = kv.split(":", 1)
            k = k.strip()
            try:
                out[k] = float(v.strip())
            except ValueError:
                continue
        return out


settings = Settings()
