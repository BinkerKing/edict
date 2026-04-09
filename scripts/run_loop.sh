#!/bin/bash
# 三省六部 · 数据刷新循环
# 用法: ./run_loop.sh [间隔秒数 [巡检间隔秒数]]
#   间隔秒数：数据刷新频率，默认 60 秒
#   巡检间隔秒数：自动重试卡住任务的频率，默认 300 秒

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export EDICT_HOME="${EDICT_HOME:-$(dirname "$SCRIPT_DIR")}"
INTERVAL="${1:-60}"
LOG="/tmp/sansheng_liubu_refresh.log"
PIDFILE="/tmp/sansheng_liubu_refresh.pid"
MAX_LOG_SIZE=$((10 * 1024 * 1024))  # 10MB

# ── 单实例保护 ──
if [[ -f "$PIDFILE" ]]; then
  OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "❌ 已有实例运行中 (PID=$OLD_PID)，退出"
    exit 1
  fi
  rm -f "$PIDFILE"
fi
echo $$ > "$PIDFILE"

# ── 优雅退出 ──
cleanup() {
  echo "$(date '+%H:%M:%S') [loop] 收到退出信号，清理中..." >> "$LOG"
  rm -f "$PIDFILE"
  exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# ── 日志轮转 ──
rotate_log() {
  if [[ -f "$LOG" ]] && (( $(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG" 2>/dev/null || echo 0) > MAX_LOG_SIZE )); then
    mv "$LOG" "${LOG}.1"
    echo "$(date '+%H:%M:%S') [loop] 日志已轮转" > "$LOG"
  fi
}

SCAN_INTERVAL="${2:-300}"  # 巡检间隔(秒), 默认 300
SCAN_COUNTER=0
SCRIPT_TIMEOUT=30  # 默认脚本最大执行时间(秒)
DASHBOARD_PORT="${EDICT_DASHBOARD_PORT:-7891}"  # 看板端口，可通过环境变量覆盖

echo "🏛️  三省六部数据刷新循环启动 (PID=$$)"
echo "   脚本目录: $SCRIPT_DIR"
echo "   间隔: ${INTERVAL}s"
echo "   巡检间隔: ${SCAN_INTERVAL}s"
echo "   脚本超时: ${SCRIPT_TIMEOUT}s"
echo "   日志: $LOG"
echo "   PID文件: $PIDFILE"
echo "   按 Ctrl+C 停止"

# ── 安全执行（带超时保护）──
safe_run() {
  local script="$1"
  local timeout="${2:-$SCRIPT_TIMEOUT}"
  # macOS 默认无 GNU timeout，统一使用 Python 子进程超时，避免循环卡死。
  python3 - "$script" "$timeout" >> "$LOG" 2>&1 <<'PY'
import subprocess, sys
script = sys.argv[1]
timeout_sec = int(sys.argv[2])
try:
    subprocess.run(["python3", script], check=False, timeout=max(1, timeout_sec))
except subprocess.TimeoutExpired:
    print(f"[loop] ⚠️ 脚本超时({timeout_sec}s): {script}")
except Exception as e:
    print(f"[loop] ⚠️ 脚本异常: {script} :: {e}")
PY
}

while true; do
  rotate_log
  safe_run "$SCRIPT_DIR/sync_from_openclaw_runtime.py"
  safe_run "$SCRIPT_DIR/sync_agent_config.py"
  safe_run "$SCRIPT_DIR/apply_model_changes.py"
  safe_run "$SCRIPT_DIR/dispatch_pending_agents.py" 280
  safe_run "$SCRIPT_DIR/verify_taizi_transfer.py"
  safe_run "$SCRIPT_DIR/sync_officials_stats.py"
  safe_run "$SCRIPT_DIR/refresh_live_data.py"

  # 定期巡检：检测卡住的任务并自动重试
  SCAN_COUNTER=$((SCAN_COUNTER + INTERVAL))
  if (( SCAN_COUNTER >= SCAN_INTERVAL )); then
    SCAN_COUNTER=0
    curl -s -X POST "http://127.0.0.1:${DASHBOARD_PORT}/api/scheduler-scan" \
      -H 'Content-Type: application/json' -d '{"thresholdSec":600}' >> "$LOG" 2>&1 || true
  fi

  sleep "$INTERVAL"
done
