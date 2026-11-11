#!/usr/bin/env bash
# 启动 Havoc 客户端（Qt5 GUI）
# 用法: ./start-client.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT/havoc-src"

# libQt5WebSockets 只存在于本地前缀，必须通过 LD_LIBRARY_PATH 提供
export LD_LIBRARY_PATH="$ROOT/toolchain/qt5/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"

exec ./client/Havoc "$@"
