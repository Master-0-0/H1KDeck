#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""传输监听器端到端验收：8 种模式 × 加密隧道。

每个模式：
1. 启动一个回显 upstream（模拟 Havoc HTTP 监听器）；
2. 通过 TransportManager 启动真实监听器；
3. 用 HtpClient 建链、发数据、收回显；
4. 校验 start/stop、持久化和统计。

仅用于已获得明确书面授权的测试环境。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "tools"))

from transports import TransportManager  # noqa: E402
from htp_client import HtpClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s: %(message)s")
log = logging.getLogger("transport-test")

RESULTS = []


async def echo_server(port: int) -> asyncio.AbstractServer:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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

    return await asyncio.start_server(handle, "127.0.0.1", port)


def make_cfg(mode: str, port: int, domain: str = "t.lab") -> dict:
    return {
        "name": f"test-{mode}",
        "mode": mode,
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "connect_host": "127.0.0.1",
        "connect_port": port,
        "ws_path": "/ws",
        "dns_domain": domain,
        "public_dns": "127.0.0.1",
        "oss_prefix": "havoc",
        "upstream_host": "127.0.0.1",
        "upstream_port": 19190,
        "vkey": "test-vkey-" + secrets.token_hex(6),
        "salt": secrets.token_hex(8),
        "compress": True,
        "padding": 8,
        "http_rewrite": False,
        "ping_interval": 5,
        "disconnect_timeout": 20,
    }


async def test_mode(mode: str, port: int, payload_size: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="htp-test-"))
    events: list[tuple[str, str]] = []
    manager = TransportManager(tmp, event=lambda text, level: events.append((text, level)))
    cfg = make_cfg(mode, port)
    manager.add(cfg)
    started = time.time()
    await manager.start(cfg["name"])
    try:
        client_cfg = dict(cfg)
        client_cfg["server_host"] = "127.0.0.1"
        client_cfg["server_port"] = port
        client = HtpClient(client_cfg)
        await client.connect(timeout=15)
        payload = b"HELLO-" + mode.encode() + b"-" + os.urandom(payload_size)
        await client.write(payload)
        received = b""
        deadline = time.time() + 30
        while len(received) < len(payload) and time.time() < deadline:
            chunk = await asyncio.wait_for(client.read(), timeout=30)
            if chunk is None:
                break
            received += chunk
        await client.close()
        ok = received == payload
        detail = f"{len(payload)}B -> {len(received)}B"
        RESULTS.append((mode, ok, detail))
        log.info("[%s] %s (%s, %.1fs)", mode, "OK" if ok else "FAIL", detail, time.time() - started)
        if not ok:
            raise AssertionError(f"{mode}: echo mismatch")
    finally:
        await manager.stop(cfg["name"])

    # 重启后从磁盘恢复（给 UDP 端口一点释放时间）
    await asyncio.sleep(0.5)
    manager2 = TransportManager(tmp)
    assert manager2.get(cfg["name"]), f"{mode}: persistence lost"
    await manager2.start(cfg["name"])
    await manager2.stop(cfg["name"])
    assert any("上线" in text for text, _ in events), f"{mode}: no events"


async def main() -> int:
    echo = await echo_server(19190)
    cases = [
        ("tcp", 19101, 8000),
        ("udp", 19102, 3000),
        ("kcp", 19103, 3000),
        ("ws", 19104, 8000),
        ("dns", 19105, 260),
        ("doh", 19106, 1200),
        ("dot", 19107, 1200),
        ("oss", 19108, 1200),
    ]
    try:
        for mode, port, size in cases:
            try:
                await test_mode(mode, port, size)
            except Exception as exc:
                RESULTS.append((mode, False, str(exc)))
                log.error("[%s] %s", mode, exc)
    finally:
        echo.close()
        await echo.wait_closed()

    print("\n=== 结果 ===")
    for mode, ok, detail in RESULTS:
        print(f"{mode:5s} {'PASS' if ok else 'FAIL'}  {detail}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
