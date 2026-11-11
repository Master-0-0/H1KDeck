#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Go 中继器 × 8 种传输的端到端验收。

流程：回显 upstream → 启动传输监听器 → 运行 Go 中继器（本地监听）→
连接本地端口发数据 → 校验回显 → 停止。

仅用于已获得明确书面授权的测试环境。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from transports import TransportManager  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname).1s %(name)s: %(message)s")
log = logging.getLogger("relay-test")

RELAY_DIR = BASE_DIR / "tools" / "relay"
GO = BASE_DIR.parent / "toolchain" / "go" / "bin" / "go"
GOCACHE = BASE_DIR.parent / "toolchain" / "gocache"
UPSTREAM_PORT = 19190
CASES = [
    ("tcp", 19101, 8000),
    ("udp", 19102, 3000),
    ("kcp", 19103, 3000),
    ("ws", 19104, 8000),
    ("dns", 19105, 260),
    ("doh", 19106, 1200),
    ("dot", 19107, 1200),
    ("oss", 19108, 1200),
]
RESULTS: list[tuple[str, bool, str]] = []


def build_relay() -> Path:
    out = Path(tempfile.mkdtemp(prefix="htp-relay-bin-")) / "htprelay"
    env = dict(os.environ)
    env.update({"GOPROXY": "off", "GOFLAGS": "-mod=mod", "CGO_ENABLED": "0",
                "GOCACHE": str(GOCACHE)})
    subprocess.run([str(GO), "build", "-trimpath", "-o", str(out), "."],
                   cwd=str(RELAY_DIR), env=env, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    return out


async def echo_server() -> asyncio.AbstractServer:
    async def handle(reader, writer):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", UPSTREAM_PORT)


async def run_case(binary: Path, mode: str, port: int, size: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix=f"htp-{mode}-"))
    manager = TransportManager(tmp)
    cfg = {
        "mode": mode, "name": "relay-test", "listen_host": "127.0.0.1", "listen_port": port,
        "connect_host": "127.0.0.1", "connect_port": port, "ws_path": "/ws",
        "dns_domain": "t.lab", "oss_prefix": "havoc", "upstream_host": "127.0.0.1",
        "upstream_port": UPSTREAM_PORT, "vkey": "test-vkey-" + secrets.token_hex(6),
        "salt": secrets.token_hex(8), "compress": True, "padding": 8, "http_rewrite": False,
        "poll_interval_ms": 20,
    }
    manager.add(cfg)
    await manager.start(cfg["name"])
    relay_cfg = {
        "mode": mode, "server_host": "127.0.0.1", "server_port": port,
        "ws_path": "/ws", "dns_domain": "t.lab", "oss_url": f"http://127.0.0.1:{port}",
        "oss_prefix": "havoc", "vkey": cfg["vkey"], "salt": cfg["salt"],
        "compress": True, "padding": 8, "http_rewrite": False, "poll_interval_ms": 20,
    }
    cfg_path = tmp / "relay.json"
    cfg_path.write_text(json.dumps(relay_cfg), encoding="utf-8")
    local_port = port + 100
    proc = await asyncio.create_subprocess_exec(
        str(binary), "--config", str(cfg_path), "--listen", f"127.0.0.1:{local_port}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await asyncio.sleep(0.8)
    payload = b"RELAY-" + mode.encode() + b"-" + os.urandom(size)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
        writer.write(payload)
        await writer.drain()
        got = b""
        deadline = time.time() + 30
        while len(got) < len(payload) and time.time() < deadline:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=30)
            if not chunk:
                break
            got += chunk
        writer.close()
        ok = got == payload
        RESULTS.append((mode, ok, f"{len(payload)}B -> {len(got)}B"))
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
        await manager.stop(cfg["name"])


async def main() -> int:
    if not GO.exists():
        print(f"缺少 Go 工具链：{GO}", file=sys.stderr)
        return 2
    print("编译 Go 中继器…")
    binary = build_relay()
    echo = await echo_server()
    try:
        for mode, port, size in CASES:
            try:
                await run_case(binary, mode, port, size)
            except Exception as exc:
                RESULTS.append((mode, False, str(exc)))
    finally:
        echo.close()
        await echo.wait_closed()

    print("\n模式   结果  详情")
    for mode, ok, detail in RESULTS:
        print(f"{mode:5s} {'PASS' if ok else 'FAIL'}  {detail}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
