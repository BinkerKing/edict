#!/bin/bash
# Unified service control for 一人世界 local runtime (terminal/nohup mode).
# Usage:
#   bash scripts/edict_services.sh start|stop|restart|status

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${EDICT_PYTHON_BIN:-}" ]]; then
  PYTHON_BIN="$EDICT_PYTHON_BIN"
elif [[ -x "/opt/homebrew/bin/python3" ]]; then
  # macOS/Homebrew: 优先使用 Homebrew Python，避免系统 Python 在受限目录下偶发权限问题
  PYTHON_BIN="/opt/homebrew/bin/python3"
else
  PYTHON_BIN="$(command -v python3)"
fi

DASHBOARD_HOST="${EDICT_DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${EDICT_DASHBOARD_PORT:-7891}"
LOOP_INTERVAL="${EDICT_LOOP_INTERVAL:-60}"
SCAN_INTERVAL="${EDICT_SCAN_INTERVAL:-300}"

DASHBOARD_LOG="${EDICT_DASHBOARD_LOG:-/tmp/edict-dashboard.log}"
LOOP_LOG="${EDICT_LOOP_LOG:-/tmp/edict-run-loop.log}"

dashboard_pattern="${ROOT_DIR}/dashboard/server.py --host ${DASHBOARD_HOST} --port ${DASHBOARD_PORT}"
loop_pattern="${ROOT_DIR}/scripts/run_loop.sh ${LOOP_INTERVAL} ${SCAN_INTERVAL}"

start_gateway() {
  openclaw gateway start >/dev/null 2>&1 || true
}

dashboard_running() {
  curl -fsS "http://${DASHBOARD_HOST}:${DASHBOARD_PORT}/healthz" >/dev/null 2>&1
}

loop_running() {
  pgrep -f "${ROOT_DIR}/scripts/run_loop.sh" >/dev/null 2>&1
}

start_dashboard() {
  if dashboard_running; then
    echo "[dashboard] already running"
    return 0
  fi
  nohup "$PYTHON_BIN" "${ROOT_DIR}/dashboard/server.py" --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" \
    >"$DASHBOARD_LOG" 2>&1 < /dev/null &
  disown $! 2>/dev/null || true
  sleep 1
  if dashboard_running; then
    echo "[dashboard] started"
  else
    echo "[dashboard] failed to start; recent log:"
    tail -n 40 "$DASHBOARD_LOG" || true
    return 1
  fi
}

start_loop() {
  if loop_running; then
    echo "[loop] already running"
    return 0
  fi
  nohup /bin/bash "${ROOT_DIR}/scripts/run_loop.sh" "$LOOP_INTERVAL" "$SCAN_INTERVAL" \
    >"$LOOP_LOG" 2>&1 < /dev/null &
  disown $! 2>/dev/null || true
  sleep 1
  if loop_running; then
    echo "[loop] started"
  else
    echo "[loop] failed to start; recent log:"
    tail -n 40 "$LOOP_LOG" || true
    return 1
  fi
}

stop_dashboard() {
  # 兼容历史启动参数差异：不依赖固定 "--host --port" 参数串匹配
  pkill -f "${ROOT_DIR}/dashboard/server.py" >/dev/null 2>&1 || true
  local pids
  pids="$(lsof -tiTCP:"$DASHBOARD_PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "$pids" | xargs kill >/dev/null 2>&1 || true
  fi
  echo "[dashboard] stopped"
}

stop_loop() {
  pkill -f "${ROOT_DIR}/scripts/run_loop.sh" >/dev/null 2>&1 || true
  rm -f /tmp/sansheng_liubu_refresh.pid >/dev/null 2>&1 || true
  echo "[loop] stopped"
}

status_all() {
  local g_ok="not-ready"
  if openclaw gateway status >/tmp/edict-gateway-status.log 2>&1; then
    g_ok="running"
  fi

  local d_ok="stopped"
  if dashboard_running; then
    d_ok="running"
  elif lsof -nP -iTCP:"$DASHBOARD_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    d_ok="port-open-unhealthy"
  fi

  local l_ok="stopped"
  if loop_running; then
    l_ok="running"
  fi

  echo "gateway  : $g_ok"
  echo "dashboard: $d_ok"
  echo "run_loop : $l_ok"
  if dashboard_running; then
    echo "healthz  : ok (http://${DASHBOARD_HOST}:${DASHBOARD_PORT}/healthz)"
  else
    echo "healthz  : fail (http://${DASHBOARD_HOST}:${DASHBOARD_PORT}/healthz)"
  fi
}

cmd="${1:-status}"
case "$cmd" in
  start)
    start_gateway
    start_dashboard
    start_loop || echo "[loop] warning: failed to start (dashboard remains available)"
    status_all
    ;;
  stop)
    stop_loop
    stop_dashboard
    status_all
    ;;
  restart)
    stop_loop
    stop_dashboard
    start_gateway
    start_dashboard
    start_loop || echo "[loop] warning: failed to start (dashboard remains available)"
    status_all
    ;;
  status)
    status_all
    ;;
  *)
    echo "Usage: bash scripts/edict_services.sh {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
