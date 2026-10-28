# 影片推荐智能体（Movie Recommendation Agent）

基于 **LangChain(LangGraph) + 本地 Chroma 向量库 + MySQL 片源 + Ollama/云端 LLM** 的自然语言影片推荐服务。自然语言 → 意图识别 → 多路召回 → 重排(MMR) → 解说生成，对外暴露 FastAPI 接口。

---

## 1. 功能与架构

**能力**：`recommend`（结构化候选+解说）、`similar`（找相似）、`board`（高分榜）、`explain`（解说）、`preference`（记住用户偏好，下次优先）。

**六层依赖**（自下而上）：
```
api/        → agent/ → retrieval/ + ranker/ → vector/ + graph/ → etl/ + db/ → config/ + llm/ + data/
```
- `agent/`：LangGraph 状态图（意图 → 规划 → 召回 → 反思兜底 → 生成）
- `retrieval/` + `ranker/`：多路召回 + MMR 重排
- `vector/`：Chroma（开发期）；`graph/`：知识图谱占位
- `llm/`：`LLM_PROVIDER=ollama|cloud` 开关；云端走 OpenAI 兼容 + **强弱分档**（`CLOUD_LLM_STRONG/LIGHT_MODEL_LIST`，各自独立按序降级）+ 免费额度熔断 + **本地兜底**（`APP_ENV=local` 时云端全失败回退 Ollama deepseek）

---

## 2. 环境要求

| 组件 | 版本/地址 | 说明 |
|------|-----------|------|
| Python | 3.12+ | 项目用 `.venv`（Windows: `.venv\Scripts\python.exe`） |
| Ollama | 端口 11434 | 本地 LLM `deepseek-r1:7b`、嵌入 `bge-m3:567m`（开发期统一本地） |
| Redis | 127.0.0.1:6379 / db0 / 无密码 | 缓存；不可达自动进程内兜底（无需强制） |
| MySQL | 127.0.0.1:3306，库 `imdb` | 片源（`douban_main` + `imdb_main`） |

---

## 3. 快速开始

```bash
# 1) 建虚拟环境并装依赖
python -m venv .venv
.venv\Scripts\activate        # Windows Git Bash / PowerShell
pip install -r requirements.txt

# 2) 配置环境变量（票据见 .env，已随仓库提供；如需重导数据改 IMDB_*）
# 3) 若 Chroma 库未建，先建库：
python -m etl.build_chroma

# 4) 一键启动（无 Docker，纯本地）
./scripts/dev.sh start        # 后台起 uvicorn，脱离当前 shell 独立存活
./scripts/dev.sh status       # 看健康 + 当前缓存后端(redis/local)
./scripts/dev.sh test         # 跑一次 /recommend 冒烟
./scripts/dev.sh logs         # 跟踪 uvicorn 日志
./scripts/dev.sh stop         # 停止
```
> Windows 双击可用 `scripts/dev.bat`（内部调 Git Bash 跑 `dev.sh`）。

---

## 4. API 用法

服务默认 `http://127.0.0.1:8000`。

### 4.1 `POST /recommend` — 结构化推荐
请求体：
```json
{"query":"推荐一部太空科幻片","user_id":"demo","top_k":5}
```
返回（节选）：
```json
{
  "query":"推荐一部太空科幻片","user_id":"demo","intent":"recommend",
  "answer":"…解说文案…\n\n〔信息来源〕豆瓣 Douban（17 部）、IMDb（3 部）（各来源作品信息已在返回结果的 sources 字段中分开展示）",
  "count":5,
  "items":[{ "id":"douban:123","source":"douban","source_label":"豆瓣 Douban",
    "title":…, "year":…, "genres":[…],"rating":8.7,"recall_source":"search_by_text","score":0.91 }],
  "sources":{
    "summary":"豆瓣 Douban 17 部 · IMDb 3 部","has_both":true,
    "blocks":[
      {"source":"douban","label":"豆瓣 Douban","count":17,
       "movies":[{"title":…,"year":…,"genres":[…],"country":"美国","rating":8.6,"rating_count":…,"intro":"…简介…"}]},
      {"source":"imdb","label":"IMDb","count":3,
       "movies":[{"title":"Spider-Man","year":2002,"genres":["Action","Sci-Fi"],"country":"United States","rating":0,"rating_count":0,"intro":"…"}]}
    ]
  },
  "steps":[
    {"order":1,"node":"parse_intent","kind":"llm","summary":"意图识别：recommend",
     "detail":{"intent":"recommend","slots":{...}},"elapsed_ms":1234,
     "thinking":"…route 步骤（parse_intent）的模型思考过程…"},
    {"order":2,"node":"plan","kind":"route","summary":"规划工具链：['search_by_text']",
     "detail":{"plan":["search_by_text"]},"elapsed_ms":3,"thinking":""},
    {"order":3,"node":"execute","kind":"retrieval","summary":"多路召回 20 条 → 重排 20 条",
     "detail":{"plan":["search_by_text"],"recall_counts":{"search_by_text":20},"ranked_count":20},
     "elapsed_ms":560,"thinking":""},
    {"order":4,"node":"generate","kind":"generate","summary":"生成推荐解说（基于 20 部候选）",
     "detail":{"candidate_count":20},"elapsed_ms":61000,
     "thinking":"…generate 步骤的解说构思思考过程…"}
  ],
  "thinking":{"route":"…parse_intent 的深度思考…","generate":"…generate 的解说构思思考…"},
  "elapsed_ms":54029,"cached":false,"cache_backend":"redis"
}
```

**来源透明化（来源标记）**：每个推荐结果都带信息来源（豆瓣 `douban` / IMDb `imdb`）。
- **对话文案末尾一句话概括**：`answer` 末尾自动追加 `〔信息来源〕…` 一行（单来源如「豆瓣 Douban（17 部）」，双来源如「豆瓣 Douban（17 部）、IMDb（3 部）」并提示详见 `sources` 字段）。
- **API 详细字段 `sources`**：按来源分块（`blocks`），每块带明显 `label` 标记（「豆瓣 Douban」/「IMDb」），块内 `movies` 列出该来源作品的基本信息（标题/年份/类型/国家）+ 评价（评分 `rating`/`rating_count`）+ 简介 `intro`。两个来源都出现时两块并列展示，一眼可区分。
- 每个 `items` 元素额外带 `source_label`（「豆瓣 Douban」/「IMDb」），便于前端逐条标记来源。
- 实现：`agent/rag.py` 的 `build_source_meta()` / `source_one_liner()`；`graph._generate` 写入 `state.sources` 并追加到 `answer`；`api/main.py` 的 `_run`/`recommend` 透传 `sources`、`_candidate_to_dict` 加 `source_label`。
**测试命令**（任选其一）：
```bash
# 最稳，跨 shell（已附 log/req.json）
curl -N -X POST http://127.0.0.1:8000/recommend \
  -H "Content-Type: application/json" -d @log/req.json

# Git Bash / PowerShell（单引号可用）
curl -N -X POST http://127.0.0.1:8000/recommend -H "Content-Type: application/json" \
  -d '{"query":"推荐一部太空科幻片","user_id":"demo","top_k":5}'

# Windows CMD（双引号 + 内部转义）
curl -N -X POST http://127.0.0.1:8000/recommend -H "Content-Type: application/json" \
  -d "{\"query\":\"推荐一部太空科幻片\",\"user_id\":\"demo\",\"top_k\":5}"
```

### 4.2 `POST /chat` — 流式对话
请求体：`{"query": "...", "user_id": "..."}`（**无 top_k**）。
返回 `text/plain` **逐句流式**推送（中文按 `。！？\n` 断句）。
```bash
curl -N -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" -d @log/chat.json
```
> `-N`(no-buffer) 必加，否则看不到流式效果。
> **注意**：`/chat` 是“伪流式”——Agent 先在后台线程跑完整回答（云端 qwen3.8-27b 约 60–90s），再逐句刷出，前 1 分多钟终端会静默，属正常。且 `/chat` **不走缓存**，每次都实打实调 LLM。

### 4.3 `GET /health` — 健康检查
```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","service":"movie-agent-api","version":"0.1.0","cache_backend":"redis","memory_backend":"redis"}
```
`cache_backend` / `memory_backend` 两行即可分别观测 P4.2 缓存与偏好记忆的兜底状态（`redis` / `local` / `file`）。

### 4.4 `POST /recommend/stream` — 流式推荐（SSE）
在 `/recommend` 基础上增加**流式兼容**：同一套 Agent、护栏、限流，但实时把「工具链步骤」「大模型深度思考」「答案 token」逐条推给前端（SSE，`text/event-stream`），最后以 `done` 事件给出完整结构化结果。专为前端流式渲染（步骤时间轴 + 思考展开 + 正文逐字）设计。

请求体与 `/recommend` 相同：`{"query":"...","user_id":"...","top_k":5}`。

**事件协议**（每条 `event: <type>\ndata: {json}\n\n`）：
| event | data 字段 | 含义 |
|-------|-----------|------|
| `step` | `{order,node,kind,summary,detail,elapsed_ms,thinking?}` | 工具链一个节点的步骤记录（`kind`∈`llm`/`route`/`retrieval`/`generate`…） |
| `thinking` | `{content}` | 大模型深度思考（`reasoning_content`）增量，随 `generate` 步骤实时流出 |
| `token` | `{content}` | 答案正文 token 增量 |
| `done` | `{intent,answer,items,sources,steps,thinking,elapsed_ms,request_id}` | 完整结果（与 `/recommend` 字段一致，含 `steps`/`thinking`） |
| `error` | `{action,category?,message}` | 护栏拦截 / 限流 / 运行异常（同 `/recommend` 的 `guardrail` 字段） |

`step` 与 `/recommend` 返回里的 `steps` 同构；`thinking` 的 `route`/`generate` 两段对应两处 LLM 调用。`done` 的 `steps`/`thinking` 为最终合集，前端可据此做「步骤时间轴 + 思考展开 + 正文流式」。

```bash
curl -N -X POST http://127.0.0.1:8000/recommend/stream \
  -H "Content-Type: application/json" -d @log/req.json
# 终端依次收到：step ×N → token ×M（及期间 thinking 增量）→ done（完整 payload）
```

> **注意**：`/recommend/stream` **不读缓存**（实时链路追踪，每次真实跑 Agent）；并发仍走 `_RUN_LOCK` 单飞（见第 8 节），多个流会排队。护栏/限流在锁前判定，越域/限流直接 `error` 事件收尾。
> 服务端不保留半程状态——前端负责把「已推步骤 + 当前 token」在刷新时续上。

---

## 5. 缓存策略（P4.2）
`api/cache.py` 的 `DualCache`：**Redis 优先**（`protocol=2` 兼容老版本 3.x），连接/读写异常自动回退**进程内 TTL 缓存**（默认 300s）。
- 仅 `/recommend` 命中缓存；同参二次请求 `cached=true` 秒回。
- Redis 故障/未启动 → 自动 `backend=local`，主链路不受影响（已单测验证）。

---

## 5.1 偏好记忆后端（强 MySQL，无 file 兜底）
`agent/memory.py` 的 `MySQLMemoryStore` 落 `movie_agent.user_preferences` 表（与 `MySQLSessionStore` / `MySQLShareStore` / `MySQLUserStore` 同规则）：
- **强 MySQL 单一后端**：构造时探测 `user_preferences` 表，不可达 / 表缺失则 `RuntimeError` fail-fast，服务拒绝启动；运行期异常直接向上抛（接口层转 500），绝不静默降级到文件 / Redis。
- 单一 JSON 列 `data` 存整份 `{user_id, preferences, history}`，与 Agent（`graph.py` / `tools.py` 的 `remember_preference`）及前端 `/memory` 接口共用同一张表 = 真正的单一数据源。
- 该表由 `scripts/init_movie_agent_db.py` 一次性初始化（DDL 见 `db/schema/movie_agent.sql`）。

---

## 5.2 结构化日志（精细化可观测）
集中式日志（`config/logging_setup.py`）——**JSON 单行 + 滚动文件**，覆盖告警/错误/请求/云端 API 四类维度：

- `log/app.log`：主日志（控制台同款），含全部 `movie_agent.*` 日志（缓存/记忆/接口/LLM/告警）。
- `log/llm_calls.log`：**专用云端 API 调用日志**（`logger=movie_agent.llm`），排查「请求云端 api」细节时单独 grep 即可。
- 日志级别、目录由 `LOG_LEVEL` / `LOG_DIR` 控制。

**云端 API 调用日志（`event=llm_call`）必记字段**：`provider`(cloud/ollama)、`tier`(strong/light)、`model`、`user_id`、`request_id`、输入/输出 token（`input_tokens`/`output_tokens`/`total_tokens`）、**缓存 token 与是否命中缓存**（`cached_tokens`/`cache_read`）、**请求耗时**（`latency_ms`）、输入/输出字符数（`input_chars`/`output_chars`）。
**请求体/响应体开关**：`LLM_LOG_BODY=true` 时额外记录 `input_text`/`output_text` 原文；默认 `false`（只记字符数，不记原文，避免大文本/敏感信息落盘）。
- 告警/错误：`event=quota_exhausted`(免费额度熔断·ERROR)、`model_fail`(瞬时失败降级·WARNING)、`max_fallbacks`(降级用尽·ERROR)、`local_fallback`/`local_fallback_fail`、`all_exhausted`；接口层 `event=api_error`(异常·带堆栈)。

**HTTP 访问日志**（`api/main.py` 中间件，`event=http_request`）：方法/路径/用户/状态码/耗时，并生成 `request_id` 贯穿 HTTP→LLM→recommend 日志便于链路串联；护栏拦截（`event=guardrail`）、限流（`event=rate_limited`）、缓存命中（`event=recommend, cached=true`）均单独留痕。`ACCESS_LOG_BODY=true` 时附带请求 query 片段。

---

## 6. 内容安全护栏（P5 补充：敏感拒答 / 越域引导）

`agent/guardrail.py` 是合规第一道闸门，在 Agent 调用**之前**（锁之外）判定；命中拦截**不进入 Agent、不消耗云端额度**。

两类拦截：
- **敏感/违规（硬拒答）**：涉政分裂/暴恐、色情、毒品、赌博诈骗、危险武器制造、自残自杀、仇恨歧视等 → 直接返回合规拒答语（`intent=guardrail`，`guardrail.action=block`）。
- **越域闲聊（软引导）**：明显非影视领域且非用户偏好/记忆（如「讲个冷笑话」「今天天气」）→ 引导回影视推荐（`guardrail.action=guide`），同样不调 LLM。

放行（`action=allow`）才进入原推荐/解说链路。`/recommend` 返回与 `/chat` 流式推送都带拒答语；`/recommend` 额外返回 `guardrail` 字段：
```json
{ "intent":"guardrail", "guardrail": {"action":"block", "category":"sensitive:porn", "reason":"…"}, "items": [], "answer":"…合规拒答语…" }
```

开关（`.env`，见第 9 节）：
```
GUARDRAIL_ENABLED=true        # 总开关；false 全部放行
GUARDRAIL_BLOCK_OFFTOPIC=true # 越域是否引导；false 则交给 Agent 降级
GUARDRAIL_MODE=rule           # rule（零额度，纯本地规则）| hybrid（边界 case 调 chat_light 复核，耗额度）
```
> 本地护栏是轻量第一道闸门；正式生产建议叠加专业内容安全服务（如阿里云内容安全）兜底。

---

## 7. 接口限流（令牌桶，P5 补充）

`api/ratelimit.py` 的内存级**令牌桶**，限制「真实触发 LLM 的速率」，防止云端额度被刷爆 / 并发排队雪崩。

- **按 key 隔离**（默认 `user_id`）：不同用户各一桶，互不影响；`consume(user_id)` 取 1 令牌。
- **仅在护栏放行 + 非缓存命中时消耗令牌**：缓存命中（`/recommend`）、护栏拦截都不计，只有真正进 Agent 调 LLM 才扣令牌——最贵的资源才被限。
- **接入点**：`api/main.py` 的 `_run()` 内、护栏放行**之后**、锁**之前**。超限时 `/recommend` 返回标准 **HTTP 429 + `Retry-After` 头**；`/chat` 流式推提示文本（首字节已发无法改状态码）。
- **算法**：桶容量 `burst`，按 `rate`（个/秒）持续补充；令牌耗尽即拒，不排队进锁。

开关（`.env`，见第 9 节）：
```
RATELIMIT_ENABLED=true   # 总开关；false 则不限制
RATELIMIT_RATE=0.2       # 令牌补充速率（个/秒）= 每 5 秒允许 1 次真实 LLM 调用
RATELIMIT_BURST=3        # 桶容量（突发上限）= 允许瞬时最多 3 次真实调用
```
> 实测：默认配置下正常单次 / demo 交互不会误伤；收紧 `burst=1` 后，同用户第 2 个未缓存请求秒回 `429 Retry-After: 14`。
> 生产多进程 / 多机需换 Redis 令牌桶（redis-py + Lua 原子扣减），否则每 worker 各一桶、总配额 = 单桶 × worker 数。

---

## 8. 并发说明（重要）
模块级 `_AGENT` 与云端 LLM 客户端并非为并发调用设计——实测并发请求会把 worker 打挂（连接被重置）。`api/main.py` 用 `threading.Lock` 把 Agent/LLM 调用**串行化（单飞）**，保证稳定性；代价是并发请求会排队（一个 60s 的 `/chat` 会让后续 `/recommend` 等它结束）。
- 单机 Demo / 单用户场景完全够用。
- 若要高并发：改为多 uvicorn worker（`--workers N`）+ 每请求独立 LLM 客户端，或把锁缩小到仅 LLM 调用。

---

## 9. 配置（`.env` 关键项）
```
LLM_PROVIDER=ollama|cloud          # 默认 ollama 本地优先
APP_ENV=local                      # local=云端全失败回退本地Ollama(deepseek)；production=直接报错
# 云端（阿里云百炼 OpenAI 兼容）
CLOUD_LLM_BASE_URL=https://.../compatible-mode/v1
CLOUD_LLM_API_KEY=sk-...
# 云端模型强弱分档（各自独立按序降级，最多 4 次；可在 .env 自由配置）
CLOUD_LLM_STRONG_MODEL_LIST=qwen3.8-27b,kimi-k3,qwen3.8-max-0902   # 强档：解释/对话/工具调用
CLOUD_LLM_LIGHT_MODEL_LIST=qwen3.7-flash,qwen3.8-flash,deepseek-v4-flash-0731,glm-5.2  # 弱档：意图/重排
CLOUD_LLM_LOCAL_FALLBACK=deepseek-r1:7b  # 云端全失败后的本地兜底模型（仅 APP_ENV=local 生效；留空不兜底）
CLOUD_LLM_MODEL_LIST=qwen3.8-27b,...     # 兼容旧字段：两档都未配置时回退用
CLOUD_LLM_RESET_QUOTA=1            # 启动清空免费额度熔断
# Redis
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=
REDIS_TTL=300
# 偏好记忆后端：强 MySQL（落 movie_agent.user_preferences 表）；MySQL 不可达则服务拒绝启动，无 file 兜底
# 日志（精细化可观测；见 5.2 节）
LOG_DIR=log
LOG_LEVEL=INFO
LLM_LOG_BODY=false          # true=记录云端 API 输入/输出原文；false=只记字符数
ACCESS_LOG_BODY=false       # true=记录 HTTP 请求/响应体片段
# 内容安全护栏（P5 补充）
GUARDRAIL_ENABLED=true
GUARDRAIL_BLOCK_OFFTOPIC=true
GUARDRAIL_MODE=rule           # rule 零额度 | hybrid 边界 case 调 chat_light 复核
# MySQL 片源
IMDB_MYSQL_USER=<你的MySQL用户名>      # 必须显式配置，已无 root/root 弱默认（缺失即启动失败）
IMDB_MYSQL_PASS=<你的MySQL密码>
IMDB_MYSQL_ADDR=127.0.0.1:3306          # 对应旧式 HOST/PORT 合并写法
IMDB_MYSQL_DB=imdb
# 业务库（users/sessions/preferences）可改用远端 DSN 覆盖：
# MOVIE_AGENT_MYSQL_DSN_OVERRIDE=mysql+pymysql://user:pass@host:3306/movie_agent?charset=utf8mb4
```

---

## 10. 排错速查
| 现象 | 原因 / 解决 |
|------|--------------|
| `{"detail":"There was an error parsing the body"}` | Windows CMD 用单引号 `'` 包裹 JSON 导致非法 → 用 `-d @log/req.json` 文件方式，或 Git Bash/PowerShell 单引号，或 CMD 双引号转义 `\"` |
| `unknown command 'HELLO'`（redis-py） | 本地 Redis 太老（3.2.100）→ 已用 `protocol=2`（RESP2）兼容 |
| 后台服务起不来 / 被回收 | Git Bash 无 `nohup`/`setsid` 且每次 Bash 是新 shell → 用 `scripts/_serve.py`（`DETACHED_PROCESS` 脱离启动）+ `dev.sh` 管理 |
| 等待命令无 `sleep`/`seq` | Git Bash 缺这两条 → 用 `curl --retry N --retry-delay 2 --retry-connrefused` 轮询就绪 |
| 并发后服务挂掉 | 见第 8 节（已加锁修复稳定性，但会串行排队） |

---

## 11. 目录结构
```
movie_agent/
├── api/            # FastAPI 服务（main.py / cache.py / ratelimit.py）
├── agent/          # LangGraph 编排（graph / intent / tools / rag / memory）
├── retrieval/      # 多路召回
├── ranker/         # MMR 重排
├── vector/         # Chroma 后端（backend.py 抽象，预留 Qdrant）
├── graph/          # 知识图谱占位
├── etl/            # 建库脚本
├── db/             # MySQL 访问
├── config/         # settings.py（读 .env）
├── llm/            # client.py + failover.py（云端降级/熔断）
├── data/           # 样本数据
├── scripts/        # dev.sh / dev.bat / _serve.py 一键启动
├── log/            # 运行期产物（uvicorn 日志、执行记录、请求夹具）
└── docs/           # 整体架构与技术选型方案（修订版）
```
