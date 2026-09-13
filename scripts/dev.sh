#!/usr/bin/env bash
# P4.3 开发期一键本地启动器（纯本地，无 Docker）
#
# 用法（在 Git Bash 中执行，仓库根目录）：
#   ./scripts/dev.sh            # 默认 = start（后台起 uvicorn + 健康检查）
#   ./scripts/dev.sh start      # 后台启动并等待就绪
#   ./scripts/dev.sh stop       # 停止（按 pidfile，taskkill）
#   ./scripts/dev.sh restart    # 停止后重启
#   ./scripts/dev.sh status     # 健康检查 + 当前缓存后端（redis/local）
#   ./scripts/dev.sh test       # 跑一次示例 /recommend（文件方式，跨 shell 安全）
#   ./scripts/dev.sh logs       # 跟踪 uvicorn 日志
#
# 说明：
#   - 复用 .venv 的 python，PYTHONPATH 锁定仓库根，避免 Git Bash 下 activate 的 cygpath 坑。
#   - 子进程经 scripts/_serve.py 以 DETACHED_PROCESS 拉起，独立于启动 shell（Git Bash 无 nohup/setsid，
#     且每次 Bash 调用都是新 shell，普通 & 会被回收）。
#   - 健康检查用 curl 自带重试等待（Git Bash 无 sleep/seq，故不依赖 sleep）。
#   - Redis / Ollama 为本地独立进程，本脚本只管 API 服务；Redis 不可用时缓存自动走进程内兜底。
set -u

# ---- 定位到仓库根 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1

PY="./.venv/Scripts/python.exe"
SERVE="scripts/_serve.py"
HOST="127.0.0.1"
PORT="8000"
PIDFILE="log/dev.pid"
LOGFILE="log/dev_uvicorn.log"
REQFILE="log/req.json"
URL="http://$HOST:$PORT"

# ---- 辅助 ----
quick_ok() {
  # 单次快速探活（~2s 上限），用于“是否已运行”的即时判断
  curl -s --max-time 2 "$URL/health" 2>/dev/null | grep -q '"status":"ok"'
}

wait_ok() {
  # 启动后等待就绪：curl 自带重试代替 sleep（最多 ~40s）
  curl -s --max-time 1 --retry 40 --retry-delay 1 --retry-connrefused "$URL/health" 2>/dev/null \
    | grep -q '"status":"ok"'
}

wait_until_down() {
  # 停止后等待端口释放（用有限次 quick_ok 间接等待，单次 ~2s，最多 ~24s）
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if ! quick_ok; then return 0; fi
  done
  return 1
}

do_start() {
  if quick_ok; then
    if [ -f "$PIDFILE" ]; then
      echo "✅ 已在运行 (pid $(cat "$PIDFILE"))，无需重启。"
      echo "   打开 $URL/docs 查看接口文档"
      return 0
    else
      echo "⚠️  端口 $PORT 已被未知进程占用（无 dev.pid）。请先手动停止该进程再 start。"
      return 1
    fi
  fi

  echo "🚀 启动 uvicorn (api.main:app) ..."
  mkdir -p log
  local p; p="$("$PY" "$SERVE" 2>/dev/null)"
  if [ -z "$p" ]; then
    echo "❌ 启动失败：_serve.py 未返回 pid，最近日志："
    tail -n 25 "$LOGFILE" 2>/dev/null
    return 1
  fi
  echo "$p" > "$PIDFILE"
  echo "   pid=$p  ->  $PIDFILE"

  echo "⏳ 等待就绪 ..."
  if wait_ok; then
    echo "✅ 服务就绪："
    echo "   健康检查  $URL/health"
    echo "   接口文档  $URL/docs"
    echo "   示例调用  ./scripts/dev.sh test"
  else
    echo "❌ 启动超时，最近日志："
    tail -n 25 "$LOGFILE" 2>/dev/null
    return 1
  fi
}

do_stop() {
  if [ -f "$PIDFILE" ]; then
    local p; p="$(cat "$PIDFILE")"
    echo "🛑 停止进程 pid=$p"
    MSYS_NO_PATHCONV=1 taskkill /F /PID "$p" >/dev/null 2>&1 || true
    rm -f "$PIDFILE"
    echo "✅ 已发送停止信号"
  else
    echo "ℹ️  无运行中的 dev 进程（pidfile 不存在）"
  fi
}

do_status() {
  if quick_ok; then
    echo "✅ 健康检查通过"
    echo "   服务信息："; curl -s --max-time 3 "$URL/health"
    echo
  else
    echo "❌ 服务未运行（health 失败）"
    return 1
  fi
}

do_test() {
  if ! quick_ok; then echo "❌ 服务未运行，先执行：./scripts/dev.sh start"; return 1; fi
  if [ ! -f "$REQFILE" ]; then echo "❌ 缺少示例请求文件 $REQFILE"; return 1; fi
  echo "🧪 调用 /recommend（文件方式，跨 shell 安全）："
  echo "   query=推荐一部太空科幻片  top_k=5  （云端模型可能耗时数十秒）"
  curl -s --max-time 180 -X POST "$URL/recommend" \
    -H "Content-Type: application/json" -d @"$REQFILE"
  echo
}

do_logs() {
  if [ -f "$LOGFILE" ]; then tail -n 50 -f "$LOGFILE"; else echo "ℹ️ 无日志文件 $LOGFILE"; fi
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; if wait_until_down; then echo "🔄 端口已释放，重启中…"; else echo "⚠️ 端口未在预期内释放，仍尝试启动"; fi; do_start ;;
  status)  do_status ;;
  test)    do_test ;;
  logs)    do_logs ;;
  *) echo "用法: $0 {start|stop|restart|status|test|logs}"; exit 1 ;;
esac
