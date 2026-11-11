#!/usr/bin/env bash
# 停止「一键上线」暂存服务
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$ROOT/logs/stager.pid"

stop_pid() {
    local pid="$1"
    [[ -n "$pid" ]] || return 0
    kill -0 "$pid" 2>/dev/null || return 0
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.1
    done
    kill -9 "$pid" 2>/dev/null || true
}

# pid 文件可能过期且 pid 已被复用：只有命令行确实是 stager_server.py 才停。
if [[ -f "$PIDFILE" ]]; then
    PID="$(tr -dc '0-9' < "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null \
        && grep -qa 'stager_server.py' "/proc/$PID/cmdline" 2>/dev/null; then
        stop_pid "$PID"
    fi
    rm -f "$PIDFILE"
fi

# 兜底：清理没有 pidfile 的实例（只匹配本目录下的脚本）
while read -r pid; do
    [[ -n "$pid" ]] && stop_pid "$pid"
done < <(pgrep -f "$ROOT/stager/stager_server.py" 2>/dev/null || true)

echo "stager 已停止（如未运行则无操作）"
