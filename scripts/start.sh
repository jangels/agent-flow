#!/usr/bin/env bash
# agent-flow 一键启动脚本
#   ./scripts/start.sh        -> 本机 GUI 模式: langgraph-bridge(Python) + npm run tauri:dev(原生窗口)
#   ./scripts/start.sh web    -> 无头 Web 模式: bridge + npm run dev(vite HMR, 便于远程/CI 联调)
#   ./scripts/start.sh stop   -> 停掉后台 bridge
#
# 环境变量覆盖(可选):
#   AGENT_FLOW_BRIDGE_PORT  默认 8010
#   AGENT_FLOW_BRIDGE_HOST  默认 127.0.0.1
set -euo pipefail

# 脚本在 scripts/ 子目录里，项目根是其上一级
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_PORT="${AGENT_FLOW_BRIDGE_PORT:-8010}"
BRIDGE_HOST="${AGENT_FLOW_BRIDGE_HOST:-127.0.0.1}"
BRIDGE_DIR="$ROOT/langgraph-bridge"

# 导出给下游子进程（Rust / vite / tauri）读取，端口自定时也能对齐
export LANGGRAPH_BRIDGE_URL="http://$BRIDGE_HOST:$BRIDGE_PORT"
PID_FILE="$ROOT/.bridge.pid"
BRIDGE_LOG="$ROOT/.bridge.log"
MODE="${1:-gui}"

command -v uv >/dev/null 2>&1 || { echo "✗ 未找到 uv,请先安装: https://docs.astral.sh/uv/"; exit 1; }

health_check() {
  local n=1
  until curl -fsS "http://$BRIDGE_HOST:$BRIDGE_PORT/health" >/dev/null 2>&1; do
    if [ "$n" -ge 60 ]; then
      echo "✗ bridge 未在 30s 内就绪,详情见 $BRIDGE_LOG"
      tail -n 20 "$BRIDGE_LOG" 2>/dev/null || true
      return 1
    fi
    sleep 0.5; n=$((n + 1))
  done
  echo "✓ bridge 健康: http://$BRIDGE_HOST:$BRIDGE_PORT/health"
}

start_bridge() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "bridge 已在运行 (PID $(cat "$PID_FILE"))"
    return 0
  fi
  echo "▶ 启动 langgraph-bridge (port $BRIDGE_PORT)..."
  cd "$BRIDGE_DIR"
  # LANGGRAPH_BRIDGE_URL 已在脚本顶部 export，下游 Rust / vite / tauri 自动对齐
  uv run uvicorn server:app --host "$BRIDGE_HOST" --port "$BRIDGE_PORT" \
    > "$BRIDGE_LOG" 2>&1 &
  echo $! > "$PID_FILE"
  health_check
}

stop_bridge() {
  # 2>/dev/null 兜并发竞态：trap 清理与本 stop 同时跑时，检查和读取之间文件可能消失
  local pid; pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" && echo "✓ 已停止 bridge (PID $pid)"
  fi
  rm -f "$PID_FILE"
  [ -z "$pid" ] && echo "无 bridge PID 文件,跳过"
}

case "$MODE" in
  stop)
    stop_bridge
    ;;
  gui|web)
    start_bridge
    cleanup() { echo; echo "■ 关闭中..."; stop_bridge; }
    trap cleanup EXIT INT TERM
    if [ "$MODE" = "gui" ]; then
      echo "▶ 启动前端 GUI (npm run tauri:dev)..."
      cd "$ROOT"
      npm run tauri:dev
    else
      echo "▶ 启动前端 Web (npm run dev, vite HMR)..."
      cd "$ROOT"
      npm run dev
    fi
    ;;
  *)
    echo "用法: $0 [gui|web|stop]   # 从项目根执行: ./scripts/start.sh"
    exit 1
    ;;
esac
