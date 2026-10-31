# 影片推荐智能体

用自然语言找片：`推荐一部太空科幻片` → 意图识别 → 多路召回 → 融合重排 → 生成带推荐理由的片单。

后端 FastAPI，Agent 用 LangGraph 编排，片源来自 MySQL 的豆瓣 + IMDb 双源数据，向量召回走 Chroma，LLM 可本地 Ollama 也可走云端 OpenAI 兼容接口。

## 能力

- 推荐 / 找相似 / 高分榜 / 解说理由 / 记住用户偏好
- 多路召回（向量、关系、榜单、知识图谱）后用 RRF 融合、MMR 保多样性
- 流式输出：SSE 推「步骤 + 模型思考 + 正文 token」
- 多会话与长期偏好落 MySQL；内容安全护栏 + 接口限流
- 附带一个 Vue3 前端（登录、会话、流式对话）

## 项目特色

- **双源合一，不重复出片**：豆瓣与 IMDb 两套数据按 `imdb_id` 对齐（缺失时退 `douban_id`），同一部电影只占一个坑，两个平台的外链都保留；回答末尾自动标注信息来源，接口另给 `sources` 分源明细。
- **模型分档 + 三重韧性**：强档（解说 / 对话）与弱档（意图 / 重排）各维护一串模型，单次调用最多降级 4 次；某模型免费额度耗尽自动熔断跳过；云端全挂时回退本地 Ollama（`APP_ENV=local`）。
- **排序过程全透明**：6 路召回 → RRF 按名次融合（`RRF_K=60`，各路权重可配）→ 叠加质量分 / 偏好分 / 反馈分 → MMR 去重。每步都写进 `steps`，流式接口连模型思考一起推。
- **反馈真的回流**：点赞点踩落库后按时间半衰期（默认 30 天）衰减加权，直接参与后续排序，不是躺在表里的死数据。
- **兜底但绝不偷偷降级**：用户 / 会话 / 偏好 / 分享四类存储强绑 MySQL，表缺失或连不上就 fail-fast 拒绝启动，不做文件兜底——宁可起不来，也不要线上悄悄丢数据。
- **护栏前置，不烧额度**：输入净化 → 注入检测 → 上下文净化 → 输出网关四层，命中直接拒答或引导回影视，全程不调模型。
- **只读 MCP 四道锁**：跨库查生产的通道默认关闭；开启后仍要过 SQL 静态扫描（禁写）→ 只读事务 → 限流熔断 → 审计日志。
- **自带可观测 + 顺手出图**：指标、告警、HTML 看板、日志分析全部标准库实现，不依赖外部 APM，告警可发 Webhook / 飞书；推荐完成后异步生成一张海报供前端轮询。
- **测试当护栏用**：候选字段白名单同步测试、禁止 file 兜底的静态扫描，防止重构时静默丢字段或偷偷降级。

## 后端链路

一次 `/recommend` 走完下面 5 个节点（LangGraph，`agent/graph.py`）：

```
parse_intent → plan → execute → reflect → generate
```

| 节点 | 做什么 |
|---|---|
| `parse_intent` | 用轻量模型解析意图（recommend / similar / board / explain / preference）与槽位（类型、国家、年代…） |
| `plan` | 规则式规划器按意图产出工具链，如 `['search_by_text']`，不额外调模型 |
| `execute` | 并行跑多路召回，候选按来源分组，再做融合与重排 |
| `reflect` | 候选不足 5 条时补一次高分榜兜底并重排，不重复生成 |
| `generate` | 用强模型基于候选写解说，附带来源标记；失败时降级为模板文案 |

**召回与融合**：检索工具（`agent/tools.py`）注册在 `RUNNERS` 里——

| 工具 | 召回什么 |
|---|---|
| `search_by_text` | 向量相似（Chroma） |
| `search_similar` | 给定影片的相似片 |
| `search_by_person` | 同导演 / 同演员 |
| `search_graph` | 知识图谱多跳关系 |
| `high_quality_board` | 高分榜冷启动 |
| `search_prod` | 只读生产库（`mcp_prod/`，默认关闭） |

多路结果用 RRF（按排名位次融合，`ranker/rrf.py`）合并，再叠加质量分与偏好分；`ranker/scorer.py` 的默认权重为 `content 1.0 / relation 0.8 / coldstart 0.6`，最后 MMR 去重保多样性。

**链路外围**（在进 Agent 之前依次判定，命中即不消耗模型额度）：

```
鉴权 → 缓存（仅 /recommend，先精确再语义）→ 护栏（敏感拒答 / 越域引导）→ 限流令牌桶 → 并发信号量 → Agent
```

- 并发用有界信号量 `_RUN_SEM`（默认 4），排队超过 30s 返回 503。
- `/recommend/stream` 不读缓存，把节点步骤、模型思考、正文 token 分别以 `step` / `thinking` / `token` 事件实时推给前端，末尾 `done` 给完整结果。
- 完成后异步生成一张推荐海报，前端按 `session_id` 轮询 `/poster/{sid}` 取图。

## 技术栈

Python 3.12+ · FastAPI · LangGraph · MySQL · Chroma · Ollama / 云端 LLM · Vue 3 + Vite + TypeScript

## 快速开始

```bash
# 1) 依赖
python -m venv .venv
.venv\Scripts\activate            # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt

# 2) 配置：仓库不含 .env，自行创建，至少填 IMDB_MYSQL_USER / IMDB_MYSQL_PASS
#    其余字段见 config/settings.py

# 3) 初始化业务表（幂等；不跑则服务启动即报错）
python scripts/init_movie_agent_db.py

# 4) 建向量库（读 MySQL 语料 → 嵌入 → 落 data/chroma，可选）
python scripts/build_vectors.py

# 5) 启动（纯本地，无 Docker）
./scripts/dev.sh start     # 后台起 uvicorn
./scripts/dev.sh status    # 健康检查
./scripts/dev.sh logs      # 跟踪日志
./scripts/dev.sh stop      # 停止
```

## 主要接口

服务默认 `http://127.0.0.1:8000`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/recommend` | 结构化推荐，返回 `answer` + `items` + `steps`，带缓存 |
| POST | `/recommend/stream` | 同上，SSE 流式（`step` / `thinking` / `token` / `done` / `error`） |
| POST | `/chat` | 纯文本逐句流式对话，无缓存 |
| GET | `/health` | 健康检查，依赖不可用时返回 503 |
| — | `/auth/*` | 注册 / 登录 / 当前用户 |
| — | `/sessions/*` | 多会话增删查 |
| — | `/memory/*` | 长期偏好读写 |
| — | `/share/*` `/export/*` `/poster/*` `/feedback` | 会话分享、导出、推荐海报、反馈 |
| — | `/metrics` `/admin/*` | 指标、看板、告警 |

示例：

```bash
curl -N -X POST http://127.0.0.1:8000/recommend \
  -H "Content-Type: application/json" \
  -d '{"query":"推荐一部太空科幻片","user_id":"demo","top_k":5}'
```

## 目录结构

```
movie_agent/
├── api/            # FastAPI 入口 main.py + routes/（auth/sessions/memory/share/export/poster/feedback/metrics）
├── agent/          # LangGraph 编排、工具、提示词、护栏、记忆与会话存储、海报
├── retrieval/      # 多路召回（内容/关系/冷启动）
├── ranker/         # 融合与重排（scorer / rrf / rerank）
├── vector/         # 向量后端（Chroma / Qdrant / Milvus）
├── graph/          # 知识图谱召回（多跳 BFS）
├── etl/            # 语料构建
├── db/             # MySQL 访问与建表 SQL（db/schema/movie_agent.sql）
├── config/         # settings / 日志 / LangSmith
├── llm/            # 模型客户端、强弱分档降级、熔断、本地兜底
├── mcp_prod/       # 只读生产库 MCP（默认关闭）
├── observability/  # 指标、告警、日志分析、看板
├── eval/           # 离线评测
├── scripts/        # 建库、建向量、评测、启动脚本
├── tests/          # 测试（含 unit/ 与 e2e/）
└── frontend/       # Vue3 + Vite + TS 前端
```

运行期产物 `data/`、`log/` 已被 gitignore。

## 前端

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173
npm run build    # 产物 frontend/dist
npm test         # vitest
```

后端地址用 `VITE_API_BASE` 指定（默认 `http://localhost:8000`），后端已放行 5173 跨域。

**页面与状态**：不引 Pinia / vue-router，状态用 Vue `reactive` 单例 store，页面切换靠 hash（`urlState.ts` 的 `?view=chat|memory&sid=...`）。

| 位置 | 作用 |
|---|---|
| `views/Login.vue` | 登录 / 注册 |
| `views/ChatView.vue` | 主界面：左侧会话列表 + 右侧对话 + 流式渲染 + 海报轮询 |
| `views/MemoryView.vue` | 长期偏好管理 |
| `views/ShareView.vue` | 只读分享页 |
| `store/auth.ts` `sessions.ts` `memory.ts` `feedback.ts` `ui.ts` | 登录态、会话、偏好、反馈、主题 |

**请求与流式渲染**：

```
ChatView 提交 query
  → api/stream.ts（POST /recommend/stream，带 AbortController 可中断）
  → utils/sse.ts 按 \n\n 切帧，解析 event/data
  → step     → 步骤时间轴追加
     thinking → 思考块折叠展示
     token    → 正文打字机
     done     → 回填 items / sources，并触发海报轮询
     error    → 渲染为提示气泡（护栏 / 限流 / 异常）
```

- 请求统一走 `api/client.ts`：注入 Cookie 与 CSRF 头、401 跳登录、429 读 `Retry-After` 提示。
- 鉴权为 httpOnly Cookie（`ma_token`）+ CSRF 双提交（`ma_csrf` → `X-CSRF-Token`），前端不存 token。
- 海报是异步产物：`done` 后用 `session_id` 轮询 `/poster/{sid}`，只认 `ready` / `pending`，其余状态不再重试。

## 配置要点

仓库不含 `.env`，全部配置读 `config/settings.py`（pydantic-settings，环境变量同名覆盖，带默认值）。下面按用途分组，只列最常改的。

**1. 基础与数据库（这部分必须配）**

```
IMDB_MYSQL_USER=<用户名>          # 必填，无 root/root 弱默认，缺失即启动失败
IMDB_MYSQL_PASS=<密码>            # 必填
IMDB_MYSQL_ADDR=127.0.0.1:3306
IMDB_MYSQL_DB=imdb                # 片源库（douban_main / imdb_main）
MOVIE_AGENT_MYSQL_DB=movie_agent  # 业务库（users / sessions / user_preferences / shared_sessions）
# MOVIE_AGENT_MYSQL_DSN_OVERRIDE=mysql+pymysql://user:pass@host:3306/movie_agent?charset=utf8mb4
```

**2. 大模型（本地 or 云端）**

```
LLM_PROVIDER=ollama               # ollama 本地优先 | cloud 云端
APP_ENV=local                     # local：云端全失败回退本地模型；production：直接报错
OLLAMA_BASE_URL=http://127.0.0.1:11434
LLM_STRONG=deepseek-r1:7b         # 本地强档
LLM_LIGHT=deepseek-r1:7b          # 本地弱档
# 云端（OpenAI 兼容）
CLOUD_LLM_BASE_URL=https://.../compatible-mode/v1
CLOUD_LLM_API_KEY=sk-...
CLOUD_LLM_STRONG_MODEL_LIST=qwen3.8-27b,kimi-k3,qwen3.8-max-0902   # 强档：解说/对话
CLOUD_LLM_LIGHT_MODEL_LIST=qwen3.7-flash,qwen3.8-flash,deepseek-v4-flash-0731,glm-5.2  # 弱档：意图/重排
CLOUD_LLM_MAX_FALLBACKS=4         # 单次调用最多降级次数
CLOUD_LLM_LOCAL_FALLBACK=deepseek-r1:7b   # 云端全挂后的本地兜底（仅 APP_ENV=local 生效）
```

**3. 嵌入与向量库**

```
EMBED_PROVIDER=ollama             # ollama | cloud
EMBED_MODEL=bge-m3:567m           # 本地嵌入，维度 1024
EMBED_DIM=1024
CLOUD_EMBED_MODEL=qwen3.7-text-embedding   # 用云端嵌入时
VECTOR_BACKEND=chroma             # chroma | qdrant | milvus
CHROMA_PATH=./data/chroma         # 另两个：QDRANT_PATH / MILVUS_PATH
BUILD_PAGE_SIZE=5000              # 建库时分页读 MySQL 的批大小
EMBED_CONCURRENCY=1               # 嵌入并发（本地模型建议 1）
```

**4. 鉴权与跨域**

```
AUTH_ENABLED=false                # 关：退回 body.user_id 旧模式；开：强制 Cookie/Token
AUTH_SECRET=<随机长串>            # 开启鉴权时必填，token HMAC 签名密钥
AUTH_TOKEN_TTL=604800             # token 有效期（秒），默认 7 天
AUTH_PBKDF2_ITERATIONS=100000     # 密码哈希迭代
AUTH_COOKIE_NAME=ma_token         # HttpOnly
CSRF_COOKIE_NAME=ma_csrf          # 双提交 Cookie
CSRF_HEADER_NAME=X-CSRF-Token     # 前端回传的头名
COOKIE_SECURE=                    # 留空=按环境推断；https 部署建议 true
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
APP_PUBLIC_ORIGIN=http://localhost:5173   # 分享链接拼接用
SESSION_MAX_MESSAGES=200          # 单会话消息上限，超出截断最旧
```

**5. 检索与融合**

```
FUSION_STRATEGY=rrf               # rrf | 分数归一化融合
RRF_K=60                          # RRF 平滑常数
RRF_WEIGHTS=content:1.0,relation:0.8,coldstart:0.6,graph:0.7,person:0.9,prod:0.5
RERANK_PROVIDER=cloud             # local | cloud（云端重排走 qwen3.7-text-rerank）
RERANK_DIVERSIFY=true             # 是否 MMR 去重
RERANK_LLM_TOPN=0                 # >0 时对前 N 条再走一次 LLM 精排
MMR_LAMBDA=0.3                    # 相关性 vs 多样性权衡
GRAPH_ENABLED=true                # 知识图谱召回
GRAPH_MAX_HOPS=2                  # 多跳层数
```

**6. 内容护栏**

```
GUARDRAIL_ENABLED=true            # 总开关，false 全部放行
GUARDRAIL_BLOCK_OFFTOPIC=true     # 越域是否引导回影视
GUARDRAIL_MODE=rule               # rule 零额度 | hybrid 边界 case 调弱模型复核
GUARDRAIL_BLOCK_INJECTION=true    # 提示词注入检测
GUARDRAIL_OUTPUT_GATE=true        # 输出侧兜底校验
GUARDRAIL_SANITIZE_CONTEXT=true   # 上下文净化
GUARDRAIL_MAX_QUERY_LEN=2000
```

**7. 并发与限流**

```
MAX_CONCURRENT_RUNS=4             # 同时进 Agent 的上限，超出排队
RUN_SLOT_TIMEOUT=30               # 排队超过 30s 返回 503
UVICORN_WORKERS=1                 # 多 worker 时理论并发 ≈ workers × MAX_CONCURRENT_RUNS
RATELIMIT_ENABLED=true            # 只限真实调 LLM 的请求，缓存命中/护栏拦截不扣
RATELIMIT_BACKEND=redis           # redis | memory（多进程必须 redis）
RATELIMIT_RATE=0.2                # 令牌补充速率（个/秒）
RATELIMIT_BURST=3                 # 突发桶容量
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_TTL=300                     # 召回缓存 TTL（秒）
```

**8. 可观测与告警**

```
LOG_DIR=log                       # 日志目录（已 gitignore）
LOG_LEVEL=INFO
LLM_LOG_BODY=false                # true 才记云端请求/响应原文
ACCESS_LOG_BODY=false             # true 才记 HTTP body 片段
METRICS_ENABLED=true
PROMETHEUS_ENABLED=true           # /metrics
DASHBOARD_ENABLED=true            # /admin/dashboard、/admin/stats
METRICS_ADMIN_TOKEN=<token>       # 设了才要求带 token 访问管理端
ALERT_EVAL_INTERVAL=60            # 后台告警评估周期（秒），0=关闭
ALERT_ERROR_RATE=0.05             # 5xx 占比阈值
ALERT_LATENCY_P95_MS=8000         # P95 延迟阈值
ALERT_WEBHOOK_URL=...             # 或 ALERT_FEISHU_WEBHOOK / ALERT_FEISHU_SECRET
LANGSMITH_API_KEY=...             # 可选，配了才开 tracing
LANGSMITH_TRACING=false
```

**9. 只读生产库 MCP（默认关闭）**

```
MCP_ENABLED=false                 # 开启前请确认已有只读账号
MCP_DB_HOST=...                   # 生产库地址（务必用只读账号）
MCP_DB_USER=...
MCP_DB_PASSWORD=...
MCP_RATE_LIMIT_QPM=60             # 每分钟查询上限
MCP_MAX_LIMIT=200                 # 单次返回行数钳制
MCP_QUERY_TIMEOUT=15              # 单次查询超时（秒）
MCP_AUDIT_LOG=true                # 审计每条 SQL（不记凭据与参数明文）
```

**10. 海报与反馈（可选）**

```
POSTER_WORKSPACE_ID=...           # 文生图需要，留空则不出海报
CLOUD_POSTER_API_KEY=             # 留空复用 CLOUD_LLM_API_KEY
POSTER_DATA_DIR=data/poster
POSTER_TASK_TIMEOUT=300
FEEDBACK_REWEIGHT_ENABLED=true    # 反馈回流排序
FEEDBACK_HALF_LIFE_DAYS=30        # 旧反馈衰减半衰期
```

以上只是常用项，全部字段（含注释与默认值）见 `config/settings.py`。

## 说明

- 业务表（用户 / 会话 / 偏好）强依赖 MySQL：连不上或表缺失时服务按设计直接拒绝启动，没有文件兜底。
- 测试：`pytest tests`（`tests/e2e/` 在 MySQL 不可用时自动跳过）。
