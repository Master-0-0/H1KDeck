#!/usr/bin/env bash
# 启动 Havoc Teamserver（默认使用 profiles/havoc.yaotl，监听 wss://0.0.0.0:40056）
# 用法: ./start-teamserver.sh [--debug] [其他 havoc server 参数]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT/havoc-src"

exec ./havoc server --profile "${PROFILE:-profiles/havoc.yaotl}" -v "$@"
