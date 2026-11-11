#!/usr/bin/env bash
# 一键启动 Havoc：Teamserver + Web 控制台 + 一键上线暂存服务
#
# 用法：
#   ./start-all.sh                  # 后台启动全部服务（幂等：已在运行的直接复用）
#   ./start-all.sh --no-webui       # 不启动 Web 控制台
#   ./start-all.sh --no-stager      # 不启动暂存服务（:9901）
#   ./start-all.sh --no-teamserver  # 复用已在运行的 teamserver，只启动 Web/暂存
#   ./start-all.sh --status         # 只查看服务状态，不启动
#   ./start-all.sh -h               # 帮助
#
# 停止：./stop-all.sh
#
# 环境变量：
#   PROFILE=profiles/havoc.yaotl    teamserver 配置（相对 havoc-src/）
#   TEAMSERVER_PORT=40056           teamserver 端口（健康检查与占用检测）
#   STAGER_PORT=9901                暂存服务端口（仅状态显示）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HAVOC_SRC="$ROOT/havoc-src"
TS_BIN="$HAVOC_SRC/havoc"
TS_LOG="$ROOT/logs/teamserver.log"
TS_PID_FILE="$ROOT/logs/teamserver.pid"
WEBUI_PID_FILE="$ROOT/webui/run/webui.pid"
STAGER_PID_FILE="$ROOT/logs/stager.pid"
TS_PORT="${TEAMSERVER_PORT:-40056}"
STAGER_PORT="${STAGER_PORT:-9901}"

START_TEAMSERVER=1
START_WEBUI=1
START_STAGER=1
SHOW_STATUS=0

log()  { printf '%s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }
die()  { printf '错误：%s\n' "$*" >&2; exit 1; }

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

# ---------------------------------------------------------------- 通用工具

pid_alive() {
    local pid="${1:-}"
    [[ -n "$pid" ]] && [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
    local file="$1" pid=""
    if [[ -f "$file" ]]; then
        pid="$(tr -dc '0-9' < "$file" 2>/dev/null || true)"
    fi
    printf '%s' "$pid"
}

port_open() {
    local port="$1"
    timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port" 2>/dev/null
}

wait_port() {
    local port="$1" secs="${2:-30}" start=$SECONDS
    while (( SECONDS - start < secs )); do
        if port_open "$port"; then
            return 0
        fi
        sleep 0.25
    done
    return 1
}

webui_port() {
    local port
    port="$(python3 - "$ROOT/webui/config.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    print(int(json.load(open(sys.argv[1], encoding="utf-8"))["web"]["port"]))
except Exception:
    print(8081)
PY
)"
    printf '%s' "${port:-8081}"
}

rotate_log() {
    local f="$1" size
    [[ -f "$f" ]] || return 0
    size="$(stat -c%s "$f" 2>/dev/null || echo 0)"
    if [[ "$size" =~ ^[0-9]+$ ]] && (( size > 20 * 1024 * 1024 )); then
        mv -f "$f" "$f.1"
    fi
}

# ---------------------------------------------------------------- teamserver

# 只认本目录 havoc-src 下的 teamserver：exe 指向该二进制，
# 或 cwd 为该目录且首参数是 havoc。
is_our_teamserver() {
    local pid="$1" exe cwd cmd first
    [[ -n "$pid" ]] && [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    [[ -r "/proc/$pid/cmdline" ]] || return 1
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
    if [[ "$exe" == "$TS_BIN" ]]; then
        return 0
    fi
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    [[ "$cwd" == "$HAVOC_SRC" ]] || return 1
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    first="${cmd%% *}"
    [[ "$first" == *havoc ]] && [[ "$cmd" == *" server"* ]]
}

find_teamserver_pid_all() {
    local pid
    while read -r pid; do
        [[ -n "$pid" ]] || continue
        if is_our_teamserver "$pid"; then
            printf '%s\n' "$pid"
        fi
    done < <(pgrep -f 'havoc server' 2>/dev/null || true)
}

find_teamserver_pid() {
    find_teamserver_pid_all | head -n 1
}

teamserver_pid() {
    local pid
    pid="$(read_pid "$TS_PID_FILE")"
    if pid_alive "$pid" && is_our_teamserver "$pid"; then
        printf '%s' "$pid"
        return 0
    fi
    find_teamserver_pid || true
}

webui_pid() {
    local pid cmd
    pid="$(read_pid "$WEBUI_PID_FILE")"
    if pid_alive "$pid"; then
        cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
        if [[ "$cmd" == *gateway.py* ]]; then
            printf '%s' "$pid"
        fi
    fi
}

stager_pid() {
    local pid cmd
    pid="$(read_pid "$STAGER_PID_FILE")"
    if pid_alive "$pid"; then
        cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
        if [[ "$cmd" == *stager_server.py* ]]; then
            printf '%s' "$pid"
        fi
    fi
}

start_teamserver() {
    local pid start
    pid="$(teamserver_pid)"
    if [[ -n "$pid" ]]; then
        printf '%s\n' "$pid" > "$TS_PID_FILE"
        log "teamserver 已在运行（pid $pid），跳过"
        return 0
    fi

    if port_open "$TS_PORT"; then
        die "TCP $TS_PORT 已被其他进程占用（未识别到本目录的 teamserver），请先释放端口"
    fi
    [[ -x "$TS_BIN" ]] || die "找不到可执行文件：$TS_BIN"

    rotate_log "$TS_LOG"
    log "启动 teamserver（端口 $TS_PORT）…"
    ( cd "$HAVOC_SRC" && setsid nohup ./havoc server \
        --profile "${PROFILE:-profiles/havoc.yaotl}" -v \
        >> "$TS_LOG" 2>&1 < /dev/null & )

    start=$SECONDS
    pid=""
    while (( SECONDS - start < 20 )); do
        pid="$(find_teamserver_pid || true)"
        if [[ -n "$pid" ]]; then
            break
        fi
        sleep 0.2
    done
    if [[ -z "$pid" ]]; then
        tail -n 20 "$TS_LOG" >&2 2>/dev/null || true
        die "teamserver 启动失败，详见 $TS_LOG"
    fi
    printf '%s\n' "$pid" > "$TS_PID_FILE"

    if ! wait_port "$TS_PORT" 30; then
        tail -n 20 "$TS_LOG" >&2 2>/dev/null || true
        die "teamserver（pid $pid）未在 30s 内监听 $TS_PORT，详见 $TS_LOG"
    fi
    log "teamserver 已启动（pid $pid）"
}

# ---------------------------------------------------------------- webui / stager

start_webui() {
    local pid
    pid="$(webui_pid)"
    if [[ -n "$pid" ]]; then
        log "webui 已在运行（pid $pid），跳过"
        return 0
    fi
    if port_open "$WEB_PORT"; then
        warn "端口 $WEB_PORT 已被占用但未找到 webui pid 文件，跳过 webui 启动"
        return 0
    fi
    [[ -x "$ROOT/webui/start-webui.sh" ]] || die "找不到 $ROOT/webui/start-webui.sh"
    log "启动 webui（端口 $WEB_PORT）…"
    "$ROOT/webui/start-webui.sh"
}

start_stager() {
    local pid
    pid="$(stager_pid)"
    if [[ -n "$pid" ]]; then
        log "stager 已在运行（pid $pid），跳过"
        return 0
    fi
    if port_open "$STAGER_PORT"; then
        warn "端口 $STAGER_PORT 已被占用但未找到 stager pid 文件，跳过 stager 启动"
        return 0
    fi
    [[ -x "$ROOT/start-stager.sh" ]] || die "找不到 $ROOT/start-stager.sh"
    log "启动 stager（端口 $STAGER_PORT）…"
    "$ROOT/start-stager.sh" --detach --port "$STAGER_PORT"
}

# ---------------------------------------------------------------- 状态

print_status() {
    local pid
    log "Havoc 服务状态："

    pid="$(teamserver_pid)"
    if [[ -n "$pid" ]]; then
        log "  teamserver  运行中  pid $pid  wss://127.0.0.1:$TS_PORT/havoc/"
    elif port_open "$TS_PORT"; then
        log "  teamserver  未知    端口 $TS_PORT 已监听，但不是本目录的 havoc 进程"
    else
        log "  teamserver  已停止"
    fi

    pid="$(webui_pid)"
    if [[ -n "$pid" ]]; then
        log "  webui       运行中  pid $pid  http://0.0.0.0:$WEB_PORT/"
    elif port_open "$WEB_PORT"; then
        log "  webui       未知    端口 $WEB_PORT 已监听，但没有 run/webui.pid"
    else
        log "  webui       已停止"
    fi

    pid="$(stager_pid)"
    if [[ -n "$pid" ]]; then
        log "  stager      运行中  pid $pid  http://127.0.0.1:$STAGER_PORT/"
    elif port_open "$STAGER_PORT"; then
        log "  stager      未知    端口 $STAGER_PORT 已监听，但没有 logs/stager.pid"
    else
        log "  stager      已停止"
    fi
}

all_running() {
    local ok=0
    [[ -n "$(teamserver_pid)" ]] || ok=1
    [[ -n "$(webui_pid)" ]] || ok=1
    [[ -n "$(stager_pid)" ]] || ok=1
    return "$ok"
}

print_summary() {
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [[ -n "$ip" ]] || ip="127.0.0.1"
    log ""
    log "Havoc 已就绪："
    log "  Web 控制台   http://$ip:$WEB_PORT/        （默认 admin / havoc1234）"
    log "  Teamserver   wss://$ip:$TS_PORT/havoc/    （操作员 Neo，Qt 客户端已移除）"
    log "  暂存服务     http://$ip:$STAGER_PORT/"
    log ""
    log "  日志：logs/teamserver.log、logs/webui-console.log、logs/stager.log"
    log "  状态：./start-all.sh --status      停止：./stop-all.sh"
}

# ---------------------------------------------------------------- main

for arg in "$@"; do
    case "$arg" in
        --no-teamserver) START_TEAMSERVER=0 ;;
        --no-webui)      START_WEBUI=0 ;;
        --no-stager)     START_STAGER=0 ;;
        --status)        SHOW_STATUS=1 ;;
        -h|--help)       usage; exit 0 ;;
        *)               die "未知参数：$arg（见 ./start-all.sh -h）" ;;
    esac
done

mkdir -p "$ROOT/logs"
WEB_PORT="$(webui_port)"

if [[ "$SHOW_STATUS" -eq 1 ]]; then
    print_status
    if all_running; then
        exit 0
    fi
    exit 1
fi

if [[ "$START_TEAMSERVER" -eq 1 ]]; then
    start_teamserver
fi
if [[ "$START_WEBUI" -eq 1 ]]; then
    start_webui
fi
if [[ "$START_STAGER" -eq 1 ]]; then
    start_stager
fi

print_summary
print_status
log ""
