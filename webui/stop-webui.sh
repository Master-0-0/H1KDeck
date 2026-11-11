#!/usr/bin/env bash
# 停止 Havoc Web 控制台网关。
set -uo pipefail
cd "$(dirname "$0")"

# 端口以 config.json 的 web.port 为准（默认 8081），用于无 pid 文件时的兜底。
WEB_PORT="$(python3 - <<'PY' 2>/dev/null || true
import json
try:
    print(int(json.load(open("config.json", encoding="utf-8"))["web"]["port"]))
except Exception:
    print(8081)
PY
)"
WEB_PORT="${WEB_PORT:-8081}"

stop_pid() {
    local pid="$1"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null || true
}

# pid 文件可能过期且 pid 已被复用：只有命令行确实是 gateway.py 才停。
PID_FILE=run/webui.pid
if [[ -f "$PID_FILE" ]]; then
    PID="$(tr -dc '0-9' < "$PID_FILE" 2>/dev/null)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null \
        && grep -qa 'gateway.py' "/proc/$PID/cmdline" 2>/dev/null; then
        stop_pid "$PID"
        rm -f "$PID_FILE"
        echo "webui 已停止"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

# 没有可用 pid 文件时按端口兜底
PID="$(ss -ltnp 2>/dev/null \
    | grep -E "[:.]$WEB_PORT[[:space:]]" \
    | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
if [[ -n "${PID:-}" ]]; then
    stop_pid "$PID"
    echo "webui 已停止（pid $PID）"
else
    echo "webui 未运行"
fi
