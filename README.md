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
- `llm/`：`LLM_PROVIDER=ollama|cloud` 开关；云端走 OpenAI 兼容 + 顺序降级 + 免费额度熔断

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
  "answer":"…解说文案…","count":5,"items":[{ "id":…, "title":…, "year":…,
  "genres":[…],"rating":8.7,"recall_source":"search_by_text","score":0.91 }],
  "elapsed_ms":54029,"cached":false,"cache_backend":"redis"
}
```
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
# {"status":"ok","service":"movie-agent-api","version":"0.1.0","cache_backend":"redis"}
```
`cache_backend` 一行即可观测 P4.2 兜底状态（`redis` / `local`）。

---

## 5. 缓存策略（P4.2）
`api/cache.py` 的 `DualCache`：**Redis 优先**（`protocol=2` 兼容老版本 3.x），连接/读写异常自动回退**进程内 TTL 缓存**（默认 300s）。
- 仅 `/recommend` 命中缓存；同参二次请求 `cached=true` 秒回。
- Redis 故障/未启动 → 自动 `backend=local`，主链路不受影响（已单测验证）。

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

开关（`.env`，见第 8 节）：
```
GUARDRAIL_ENABLED=true        # 总开关；false 全部放行
GUARDRAIL_BLOCK_OFFTOPIC=true # 越域是否引导；false 则交给 Agent 降级
GUARDRAIL_MODE=rule           # rule（零额度，纯本地规则）| hybrid（边界 case 调 chat_light 复核，耗额度）
```
> 本地护栏是轻量第一道闸门；正式生产建议叠加专业内容安全服务（如阿里云内容安全）兜底。

---

## 7. 并发说明（重要）
模块级 `_AGENT` 与云端 LLM 客户端并非为并发调用设计——实测并发请求会把 worker 打挂（连接被重置）。`api/main.py` 用 `threading.Lock` 把 Agent/LLM 调用**串行化（单飞）**，保证稳定性；代价是并发请求会排队（一个 60s 的 `/chat` 会让后续 `/recommend` 等它结束）。
- 单机 Demo / 单用户场景完全够用。
- 若要高并发：改为多 uvicorn worker（`--workers N`）+ 每请求独立 LLM 客户端，或把锁缩小到仅 LLM 调用。

---

## 8. 配置（`.env` 关键项）
```
LLM_PROVIDER=ollama|cloud          # 默认 ollama 本地优先
# 云端（阿里云百炼 OpenAI 兼容）
CLOUD_LLM_BASE_URL=https://.../compatible-mode/v1
CLOUD_LLM_API_KEY=sk-...
CLOUD_LLM_MODEL_LIST=qwen3.8-27b,kimi-k3,...   # 顺序降级，最多 4 次
CLOUD_LLM_RESET_QUOTA=1            # 启动清空免费额度熔断
# Redis
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=
REDIS_TTL=300
# 内容安全护栏（P5 补充）
GUARDRAIL_ENABLED=true
GUARDRAIL_BLOCK_OFFTOPIC=true
GUARDRAIL_MODE=rule           # rule 零额度 | hybrid 边界 case 调 chat_light 复核
# MySQL 片源
IMDB_MYSQL_HOST=127.0.0.1
IMDB_MYSQL_PORT=3306
IMDB_MYSQL_USER=root
IMDB_MYSQL_PASSWORD=...
IMDB_MYSQL_DATABASE=imdb
```

---

## 9. 排错速查
| 现象 | 原因 / 解决 |
|------|--------------|
| `{"detail":"There was an error parsing the body"}` | Windows CMD 用单引号 `'` 包裹 JSON 导致非法 → 用 `-d @log/req.json` 文件方式，或 Git Bash/PowerShell 单引号，或 CMD 双引号转义 `\"` |
| `unknown command 'HELLO'`（redis-py） | 本地 Redis 太老（3.2.100）→ 已用 `protocol=2`（RESP2）兼容 |
| 后台服务起不来 / 被回收 | Git Bash 无 `nohup`/`setsid` 且每次 Bash 是新 shell → 用 `scripts/_serve.py`（`DETACHED_PROCESS` 脱离启动）+ `dev.sh` 管理 |
| 等待命令无 `sleep`/`seq` | Git Bash 缺这两条 → 用 `curl --retry N --retry-delay 2 --retry-connrefused` 轮询就绪 |
| 并发后服务挂掉 | 见第 7 节（已加锁修复稳定性，但会串行排队） |

---

## 10. 目录结构
```
movie_agent/
├── api/            # FastAPI 服务（main.py / cache.py）
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
