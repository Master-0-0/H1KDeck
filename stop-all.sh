#!/usr/bin/env bash
# 停止 Havoc 全部服务（Teamserver + Web 控制台 + 一键上线暂存服务）
#
# 用法：
#   ./stop-all.sh                    # 全部停止
#   ./stop-all.sh --keep-webui       # 保留 Web 控制台
#   ./stop-all.sh --keep-stager      # 保留暂存服务
#   ./stop-all.sh --keep-teamserver  # 保留 teamserver
#   ./stop-all.sh -h                 # 帮助
#
# 对应启动脚本：./start-all.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HAVOC_SRC="$ROOT/havoc-src"
TS_BIN="$HAVOC_SRC/havoc"
TS_PID_FILE="$ROOT/logs/teamserver.pid"

KEEP_TEAMSERVER=0
KEEP_WEBUI=0
KEEP_STAGER=0

log()  { printf '%s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

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

# 先 TERM，最多等 5s，再 KILL
kill_pids() {
    local pids=("$@") pid alive
    (( ${#pids[@]} )) || return 0
    kill "${pids[@]}" 2>/dev/null || true
    for _ in $(seq 1 50); do
        alive=0
        for pid in "${pids[@]}"; do
            if pid_alive "$pid"; then
                alive=1
                break
            fi
        done
        if (( alive == 0 )); then
            return 0
        fi
        sleep 0.1
    done
    for pid in "${pids[@]}"; do
        kill -9 "$pid" 2>/dev/null || true
    done
}

stop_teamserver() {
    local pids=() pid p seen
    pid="$(read_pid "$TS_PID_FILE")"
    if pid_alive "$pid" && is_our_teamserver "$pid"; then
        pids+=("$pid")
    fi
    while read -r pid; do
        [[ -n "$pid" ]] || continue
        seen=0
        for p in "${pids[@]}"; do
            if [[ "$p" == "$pid" ]]; then
                seen=1
                break
            fi
        done
        if (( seen == 0 )); then
            pids+=("$pid")
        fi
    done < <(find_teamserver_pid_all)

    rm -f "$TS_PID_FILE"

    if (( ${#pids[@]} == 0 )); then
        log "teamserver 未运行"
        return 0
    fi

    log "停止 teamserver（pid ${pids[*]}）…"
    kill_pids "${pids[@]}"

    pid="$(find_teamserver_pid_all | head -n 1)"
    if [[ -n "$pid" ]]; then
        warn "teamserver 仍有残留进程：pid $pid"
        return 1
    fi
    log "teamserver 已停止"
}

for arg in "$@"; do
    case "$arg" in
        --keep-teamserver) KEEP_TEAMSERVER=1 ;;
        --keep-webui)      KEEP_WEBUI=1 ;;
        --keep-stager)     KEEP_STAGER=1 ;;
        -h|--help)         usage; exit 0 ;;
        *)                 warn "未知参数：$arg（见 ./stop-all.sh -h）" ;;
    esac
done

if [[ "$KEEP_WEBUI" -eq 0 ]]; then
    if [[ -x "$ROOT/webui/stop-webui.sh" ]]; then
        "$ROOT/webui/stop-webui.sh"
    else
        warn "找不到 $ROOT/webui/stop-webui.sh，跳过 webui"
    fi
fi

if [[ "$KEEP_STAGER" -eq 0 ]]; then
    if [[ -x "$ROOT/stop-stager.sh" ]]; then
        "$ROOT/stop-stager.sh"
    else
        warn "找不到 $ROOT/stop-stager.sh，跳过 stager"
    fi
fi

if [[ "$KEEP_TEAMSERVER" -eq 0 ]]; then
    stop_teamserver
fi
