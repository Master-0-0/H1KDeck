#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隔离实验网专用：远端命令执行服务（模拟 SSH/WinRM 横向通道）。

仅监听在实验用 network namespace 内（10.10.10.10 / 10.10.20.10 / 10.10.30.10:2222），
用于验证「已有会话 → shell 命令 → 一键上线」的三层投递链路。它不是生产组件，
实验结束由 teardown_lab.sh 删除命名空间时一并终止。
"""
from __future__ import annotations

import argparse
import os
import signal
import socketserver
import subprocess
import sys
import time
from datetime import datetime


class Handler(socketserver.StreamRequestHandler):
    timeout = 200

    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        line = self.rfile.readline(8192)
        if not line:
            return
        command = line.decode("utf-8", "replace").strip()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"[{stamp}] {peer} -> {command[:500]}")
        if not command:
            return
        try:
            self.wfile.write(f"[execd] {peer} run: {command}\n".encode("utf-8", "replace"))
            self.wfile.flush()
        except OSError:
            return
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        started = time.time()
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                capture_output=True,
                timeout=self.server.timeout,  # type: ignore[attr-defined]
                env=env,
                cwd="/",
                start_new_session=True,
            )
            output = proc.stdout + proc.stderr
            rc = proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b"") + (exc.stderr or b"") + b"\n[execd] timeout\n"
            rc = 124
        except Exception as exc:  # noqa: BLE001
            output = f"[execd] error: {exc}\n".encode()
            rc = 1
        elapsed = time.time() - started
        try:
            self.wfile.write(output)
            self.wfile.write(f"\n[execd] rc={rc} elapsed={elapsed:.2f}s\n".encode())
            self.wfile.flush()
        except OSError:
            pass


LOG_PATH = ""


def log(text: str) -> None:
    line = text.rstrip("\n")
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    print(line, flush=True)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    global LOG_PATH
    parser = argparse.ArgumentParser(description="隔离实验网远端命令执行服务")
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--log", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    LOG_PATH = args.log
    server = Server((args.bind, args.port), Handler)
    server.timeout = args.timeout  # type: ignore[attr-defined]
    log(f"[execd] listening on {args.bind}:{args.port} (timeout={args.timeout}s)")

    def _stop(signum, frame):  # noqa: ARG001
        log("[execd] stopping")
        server.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
