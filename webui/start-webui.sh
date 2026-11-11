#!/usr/bin/env bash
# 启动 Havoc Web 控制台网关（后台）。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p run ../logs

# 端口以 config.json 的 web.port 为准（默认 8081），健康检查与提示都跟随配置。
WEB_PORT="$(python3 - <<'PY' 2>/dev/null || true
import json
try:
    print(int(json.load(open("config.json", encoding="utf-8"))["web"]["port"]))
except Exception:
    print(8081)
PY
)"
WEB_PORT="${WEB_PORT:-8081}"

PID_FILE=run/webui.pid
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "webui 已在运行：pid $(cat "$PID_FILE")  http://0.0.0.0:$WEB_PORT/"
    exit 0
fi

nohup python3 gateway.py >> ../logs/webui-console.log 2>&1 &
echo $! > "$PID_FILE"

for _ in $(seq 1 40); do
    if curl -sf --max-time 1 "http://127.0.0.1:$WEB_PORT/api/health" >/dev/null 2>&1; then
        echo "webui 已启动：http://0.0.0.0:$WEB_PORT/  (pid $(cat "$PID_FILE"))"
        exit 0
    fi
    sleep 0.25
done

echo "webui 启动失败，请查看 ../logs/webui-console.log" >&2
exit 1
