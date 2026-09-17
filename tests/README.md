# 测试与「强 MySQL、无 file 兜底」规范

本目录是项目的**统一测试目录**。所有后端测试集中放在此处，运行：

```bash
# 在项目根目录（D:/WWW/movie_agent）
.venv/Scripts/python.exe -m pytest tests -q
```

## 一、前置条件（强 MySQL）

本服务所有持久化状态（用户 / 会话 / 共享映射 / 偏好记忆）**统一落 `movie_agent` 库，强 MySQL、无 file 兜底**：
- MySQL 不可达或业务表（`users` / `sessions` / `shared_sessions` / `user_preferences`）缺失时，
  服务在启动期即 `RuntimeError` fail-fast 拒绝启动（不回退 file）。
- 因此测试依赖真实 MySQL。运行前请确保：
  1. MySQL 在线；
  2. 已初始化业务库：`.venv/Scripts/python.exe scripts/init_movie_agent_db.py`（幂等，可重复执行）。

若 MySQL 不可用，依赖存储的用例会**自动 skip**（见 `conftest.py` 的 `mysql_ok` fixture），
`pytest tests` 仍可干净退出；`test_no_file_fallback.py` 是纯静态扫描，不依赖 MySQL，始终运行。

## 二、测试分层

| 文件 | 覆盖 | 是否依赖 MySQL |
|------|------|----------------|
| `test_no_file_fallback.py` | **静态把关**：扫描全项目源码，禁止任何 file 兜底符号（`File*Store` / `use_mysql` / `*_FILE_ROOT` / `MEMORY_BACKEND` / `回退 file` 等） | 否 |
| `test_user_store.py` | `agent/user_store.py` 强 MySQL 读写 + fail-fast | 是 |
| `test_memory_store.py` | `agent/memory.py` 偏好记忆读写 + fail-fast | 是 |
| `test_session_store.py` | `agent/session_store.py` 会话生命周期 + fail-fast | 是 |
| `test_share_store.py` | `agent/share_store.py` 共享映射 + fail-fast | 是 |
| `test_share.py` | 会话共享 HTTP 接口（集成） | 是 |
| `test_export.py` | 会话历史导出接口（注入假 store，不依赖真实存储） | 否（注入） |

## 三、强制规范：代码变更必须同步更新测试

> **铁律**：任何对 `agent/*.py`、`api/**`、`config/settings.py`、`db/schema/*.sql` 中
> 「MySQL 读取 / 存储」相关代码的改动，**必须同步验证并更新对应测试**，保持测试与代码一致。

具体约定：
1. **改存储逻辑** → 改对应 `test_*_store.py`（正常路径 + 边界 + fail-fast 一条都不能少）。
2. **新增/修改一种 file 兜底或回退分支** → 禁止；若误加，`test_no_file_fallback.py` 会立刻失败。
3. **改接口契约** → 同步改 `test_share.py` / `test_export.py` 等集成测试。
4. **改配置项**（如 DSN、超时）→ 在相关 store 测试里补充断言。
5. 提交前务必本地跑一遍 `pytest tests`，全绿方可视为完成。

## 四、如何新增一个存储测试

参考 `test_user_store.py`：
- 用 `mysql_ok` fixture 声明「需要 MySQL」（缺 MySQL 时自动 skip）；
- 用唯一 `uuid` 当 user_id / session_id 避免串扰，并在 `finally` 里物理清理；
- fail-fast 用例用 `monkeypatch` 把 `Settings.MOVIE_AGENT_MYSQL_DSN` 换成一个不可达地址，
  断言构造抛 `RuntimeError`（**且绝不能是 file 兜底**）。
