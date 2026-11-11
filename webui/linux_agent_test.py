#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linux Demon 回归测试（P0/P1）。

流程：
  1. 通过 Web 网关构建一个临时 Linux Exe（x64，sleep 0），下载到临时目录；
  2. 启动临时 demon，等待新 Linux 会话上线；
  3. shell / 文件 / proc 命令验证；
  4. 用原始 WebSocket（Qt 客户端同款包）下发 whoami/tasklist/net users，
     验证 teamserver 的 Linux 命令兼容层（P0）；
  5. 起一个本地 banner 服务，在会话上 rportfwd 转发，验证 banner 立即返回（P1 流式）；
  6. 通过 rportfwd 访问 Web 网关，验证 HTTP keep-alive 不需要 Connection: close（P1）；
  7. 退出临时 demon、删除临时文件，退出码 0。

用法：
    cd webui && python3 linux_agent_test.py
前置：teamserver（40056/8080）、Web 网关（8081）、LAN-Test 监听器在线。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import aiohttp
import websockets

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
BASE = os.environ.get("HAVOC_WEBUI", f"http://127.0.0.1:{CONFIG['web'].get('port', 8081)}")
WEB_USER = CONFIG["web"].get("user", "admin")
WEB_PASS = CONFIG["web"].get("password", "havoc1234")
TS_HOST = CONFIG["teamserver"].get("host", "127.0.0.1")
TS_PORT = CONFIG["teamserver"].get("port", 40056)
TS_URL = f"wss://{TS_HOST}:{TS_PORT}/havoc/"
# Qt 客户端已移除，profile 中只剩网关使用的操作员（默认 Neo）。
TS_OPERATOR = os.environ.get("HAVOC_OPERATOR", CONFIG["teamserver"].get("user", "Neo"))
TS_PASSWORD = os.environ.get("HAVOC_OPERATOR_PASSWORD", "password1234")
LISTENER = os.environ.get("HAVOC_LISTENER", "LAN-Test")

RESULTS: list[tuple[str, str, str]] = []  # (status, name, detail)


def record(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def tid() -> str:
    return f"{random.randrange(0x10000000, 0xFFFFFFFF):08X}"


# ---------------------------------------------------------------------------
# 本地 banner 服务（模拟“服务端先说话”的协议，如 SSH）
# ---------------------------------------------------------------------------
class BannerServer(threading.Thread):
    def __init__(self, banner: bytes, hold: float = 3.0):
        super().__init__(daemon=True)
        self.banner = banner
        self.hold = hold
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.sendall(self.banner)
                conn.settimeout(self.hold)
                try:
                    conn.recv(1024)
                except OSError:
                    pass
            finally:
                conn.close()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 原始 WebSocket（与 Qt 客户端相同的包结构）
# ---------------------------------------------------------------------------
class RawClient:
    def __init__(self) -> None:
        self.ws = None
        self.reader = None
        self.ok = False

    async def connect(self) -> bool:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.ws = await websockets.connect(TS_URL, ssl=ctx, max_size=None,
                                           ping_interval=None)
        pwd = hashlib.sha3_256(TS_PASSWORD.encode()).hexdigest()
        await self.ws.send(json.dumps({
            "Head": {"Event": 1, "User": TS_OPERATOR, "Time": ""},
            "Body": {"SubEvent": 3, "Info": {"User": TS_OPERATOR, "Password": pwd}},
        }, separators=(",", ":")).encode())
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
            if msg["Head"]["Event"] == 1:
                if msg["Body"].get("SubEvent") == 1:
                    self.ok = True
                    self.reader = asyncio.create_task(self._drain())
                    return True
                if msg["Body"].get("SubEvent") == 2:
                    self.ok = False
                    return False
        return False

    async def _drain(self) -> None:
        try:
            async for _ in self.ws:
                pass
        except Exception:
            pass

    async def _send(self, info: dict) -> None:
        await self.ws.send(json.dumps({
            "Head": {"Event": 7, "User": TS_OPERATOR, "Time": ""},
            "Body": {"SubEvent": 3, "Info": info},
        }, separators=(",", ":")).encode())

    async def inline(self, demon: str, line: str) -> str:
        task = tid()
        await self._send({
            "TaskID": task, "CommandLine": line, "DemonID": demon, "CommandID": "20",
            "HasCallback": "false", "FunctionName": "go",
            "Binary": "aGVsbG8=", "Arguments": "", "Flags": "default",
        })
        return task

    async def net(self, demon: str, line: str, cmd: str, param: str = "") -> str:
        task = tid()
        await self._send({
            "TaskID": task, "CommandLine": line, "DemonID": demon, "CommandID": "2100",
            "NetCommand": cmd, "Param": param,
        })
        return task

    async def close(self) -> None:
        if self.reader:
            self.reader.cancel()
        if self.ws:
            await self.ws.close()


# ---------------------------------------------------------------------------
# Web API
# ---------------------------------------------------------------------------
async def login(session: aiohttp.ClientSession) -> bool:
    async with session.post(f"{BASE}/api/login",
                            json={"username": WEB_USER, "password": WEB_PASS}) as resp:
        return resp.status == 200


async def state(session: aiohttp.ClientSession) -> dict:
    async with session.get(f"{BASE}/api/state") as resp:
        return await resp.json()


async def console(session: aiohttp.ClientSession, sid: str) -> str:
    async with session.get(f"{BASE}/api/sessions/{sid}/console?limit=400") as resp:
        data = await resp.json()
    return "\n".join(line.get("text", "") for line in data.get("lines", []))


async def send_command(session: aiohttp.ClientSession, sid: str, command: str) -> bool:
    async with session.post(f"{BASE}/api/sessions/{sid}/command",
                            json={"command": command}) as resp:
        return resp.status == 200


async def wait_for_console(session: aiohttp.ClientSession, sid: str, needles: list[str],
                           before: str = "", timeout: float = 30.0,
                           forbid: list[str] | None = None) -> str:
    """等待控制台新增内容里出现所有 needles；返回命中的新文本，超时返回空串。"""
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        await asyncio.sleep(0.8)
        now = await console(session, sid)
        new = now[len(before):] if before and now.startswith(before) else now[-6000:]
        last = new
        low = new.lower()
        if forbid and any(x.lower() in low for x in forbid):
            return ""
        if all(n in new for n in needles):
            return new
    return ""


async def build_payload(session: aiohttp.ClientSession) -> tuple[Path | None, str]:
    """通过 Web 载荷 API 构建一个 Linux Exe，返回临时文件路径。"""
    # 等上一次构建结束
    for _ in range(60):
        async with session.get(f"{BASE}/api/payloads") as resp:
            data = await resp.json()
        if not data.get("active"):
            break
        await asyncio.sleep(1)
    body = {"listener": LISTENER, "arch": "x64", "format": "Linux Exe",
            "config": {"sleep": "0", "jitter": "0"}}
    async with session.post(f"{BASE}/api/payloads/build", json=body) as resp:
        data = await resp.json()
        if resp.status != 200:
            return None, f"HTTP {resp.status}: {data.get('message') or data}"
        build_id = data["build"]["id"]

    for _ in range(180):
        await asyncio.sleep(1.5)
        async with session.get(f"{BASE}/api/payloads") as resp:
            data = await resp.json()
        builds = [b for b in data.get("builds", []) if b.get("id") == build_id]
        if not builds:
            continue
        build = builds[0]
        if build.get("status") == "done":
            async with session.get(f"{BASE}/api/payloads/{build_id}/download") as resp:
                blob = await resp.read()
            if resp.status != 200 or len(blob) < 1024 * 1024:
                return None, f"下载失败（{resp.status}, {len(blob)} 字节）"
            path = Path(tempfile.gettempdir()) / f"linux-agent-test-{build_id}" / "demon.elf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            path.chmod(0o755)
            return path, f"build {build_id}, {len(blob)} 字节"
        if build.get("status") in ("error", "failed"):
            return None, str(build.get("error") or build)
    return None, "构建超时"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def main() -> int:
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    proc: subprocess.Popen | None = None
    workdir: Path | None = None
    banner: BannerServer | None = None
    raw = RawClient()
    sid = ""
    try:
        if not await login(session):
            record("FAIL", "Web 登录", BASE)
            return 1
        record("PASS", "Web 登录", BASE)

        snap = await state(session)
        record("PASS" if snap["connection"]["state"] == "connected" else "FAIL",
               "teamserver 连接", snap["connection"].get("detail", ""))
        listeners = {l["name"]: l for l in snap.get("listeners", [])}
        if LISTENER not in listeners:
            record("FAIL", f"监听器 {LISTENER} 存在")
            return 1
        record("PASS" if listeners[LISTENER].get("status", "").lower() == "online" else "FAIL",
               f"监听器 {LISTENER} 在线", listeners[LISTENER].get("status", ""))

        payload, detail = await build_payload(session)
        if not payload:
            record("FAIL", "构建 Linux Exe", detail)
            return 1
        record("PASS", "构建 Linux Exe", detail)
        workdir = payload.parent

        before_ids = {s["id"] for s in (await state(session)).get("sessions", [])}
        env = dict(os.environ)
        env.pop("DEMON_SELF_DELETE", None)  # 临时测试不启用自删除，便于清理
        proc = subprocess.Popen([str(payload)], cwd=str(workdir), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        session_obj = None
        deadline = time.time() + 90
        while time.time() < deadline:
            await asyncio.sleep(1)
            snap = await state(session)
            for item in snap.get("sessions", []):
                if item["id"] in before_ids:
                    continue
                if item.get("process") == str(payload) and item.get("health") == "healthy":
                    session_obj = item
                    break
            if session_obj:
                break
        if not session_obj:
            record("FAIL", "临时 Linux demon 上线", f"进程 {proc.pid}")
            return 1
        sid = session_obj["id"]
        record("PASS", "临时 Linux demon 上线",
               f"会话 {sid}，PID {session_obj.get('pid')}，系统 {session_obj.get('os')}，sleep {session_obj.get('sleep')}")
        record("PASS" if session_obj.get("listener") == LISTENER else "FAIL",
               "会话 Listener 字段", f"{session_obj.get('listener')!r}（期望 {LISTENER}）")

        # ---------------- 基础命令 ----------------
        async def check_cmd(name: str, command: str, needles: list[str], timeout: float = 30.0) -> bool:
            before = await console(session, sid)
            if not await send_command(session, sid, command):
                record("FAIL", name, "下发失败")
                return False
            hit = await wait_for_console(session, sid, needles, before, timeout)
            record("PASS" if hit else "FAIL", name, "" if hit else f"未在 {timeout:.0f}s 内看到 {needles}")
            return bool(hit)

        hostname = socket.gethostname()
        await check_cmd("shell id -un", "shell id -un", ["kali"])
        await check_cmd("shell hostname", "shell hostname", [hostname])
        await check_cmd("文件 pwd", "pwd", ["/"])
        await check_cmd("文件 ls", "ls /etc/hostname", ["hostname"])
        await check_cmd("文件 cat", "cat /etc/hostname", [hostname])
        await check_cmd("文件 download", "download /etc/hostname", ["Finished download"])
        await check_cmd("proc list", "proc list", ["PID", "Name"], timeout=40)

        # ---------------- P0：Qt 同款包（原始 WebSocket） ----------------
        if await raw.connect():
            record("PASS", "原始 WebSocket 登录", TS_OPERATOR)

            before = await console(session, sid)
            await raw.inline(sid, "whoami")
            hit = await wait_for_console(session, sid, ["uid=", "Path:[/bin/sh]"], before, 30,
                                         forbid=["not supported"])
            record("PASS" if hit else "FAIL", "Qt 包 whoami → /bin/sh id",
                   "teamserver 兼容层生效" if hit else "未看到 uid= 或 /bin/sh")

            before = await console(session, sid)
            await raw.inline(sid, "tasklist")
            hit = await wait_for_console(session, sid, ["Process List"], before, 30,
                                         forbid=["not supported"])
            record("PASS" if hit else "FAIL", "Qt 包 tasklist → proc list",
                   "" if hit else "未看到 Process List")

            before = await console(session, sid)
            await raw.net(sid, "net users", "9")
            hit = await wait_for_console(session, sid, ["root:"], before, 30,
                                         forbid=["not supported"])
            record("PASS" if hit else "FAIL", "Qt 包 net users → getent passwd",
                   "" if hit else "未看到 root:")
        else:
            record("SKIP", "原始 WebSocket 登录",
                   f"{TS_OPERATOR} 已被其他客户端占用（Qt 客户端在线？）")

        # ---------------- P1：rportfwd 流式（banner 先到） ----------------
        banner = BannerServer(b"SSH-2.0-LinuxAgentTest\r\n")
        banner.start()
        rport = free_port()
        before = await console(session, sid)
        await send_command(session, sid, f"rportfwd add 0.0.0.0 {rport} 127.0.0.1 {banner.port}")
        ready = await wait_for_console(session, sid, [str(rport)], before, 20)
        if ready:
            got = None
            started = time.time()
            for _ in range(20):
                try:
                    sock = socket.create_connection(("127.0.0.1", rport), timeout=3)
                    sock.settimeout(6)
                    line = b""
                    while b"\n" not in line:
                        chunk = sock.recv(128)
                        if not chunk:
                            break
                        line += chunk
                    sock.close()
                    if line:
                        got = line
                        break
                except OSError:
                    await asyncio.sleep(0.5)
            elapsed = time.time() - started
            ok = bool(got) and got.startswith(b"SSH-2.0-LinuxAgentTest") and elapsed < 5.0
            record("PASS" if ok else "FAIL", "rportfwd banner 立即返回",
                   f"{elapsed*1000:.0f} ms, {got.decode(errors='replace').strip() if got else '无数据'}")
        else:
            record("FAIL", "rportfwd add", "未看到监听器确认")

        # ---------------- P1：rportfwd 上 HTTP keep-alive ----------------
        web_port = free_port()
        before = await console(session, sid)
        await send_command(session, sid, f"rportfwd add 0.0.0.0 {web_port} 127.0.0.1 {CONFIG['web'].get('port', 8081)}")
        ready = await wait_for_console(session, sid, [f"0.0.0.0:{web_port}"], before, 20)
        if ready:
            import http.client
            ok = False
            info = ""
            for _ in range(20):
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", web_port, timeout=10)
                    conn.request("GET", "/api/health")
                    r1 = conn.getresponse()
                    b1 = r1.read()
                    r2 = None
                    if not r1.will_close:
                        conn.request("GET", "/static/app.js")
                        r2 = conn.getresponse()
                        b2 = r2.read()
                    conn.close()
                    if r1.status == 200 and r2 is not None and r2.status == 200 and len(b2) > 100000:
                        ok = True
                        info = f"同一连接两次 200（health {len(b1)}B / app.js {len(b2)}B）"
                    else:
                        info = (f"health={r1.status} will_close={r1.will_close} "
                                f"app.js={getattr(r2, 'status', None) if r2 else '未复用连接'}")
                    break
                except OSError as exc:
                    info = str(exc)
                    await asyncio.sleep(0.5)
            record("PASS" if ok else "FAIL", "rportfwd HTTP keep-alive", info)
        else:
            record("FAIL", "rportfwd add（HTTP）", "未看到监听器确认")

        # ---------------- 清理 ----------------
        if sid:
            # 客户端语法是 `exit <thread|process>`；Linux Demon 收到后立即 os.Exit。
            await send_command(session, sid, "exit process")
            deadline = time.time() + 15
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.5)
        record("PASS" if proc.poll() is not None else "FAIL", "临时 demon 退出",
               f"returncode={proc.poll()}")

    except Exception as exc:  # noqa: BLE001
        record("FAIL", "测试异常", f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await raw.close()
        except Exception:
            pass
        if banner:
            banner.stop()
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        await session.close()
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    total = len(RESULTS)
    passed = sum(1 for s, _, _ in RESULTS if s == "PASS")
    failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    skipped = sum(1 for s, _, _ in RESULTS if s == "SKIP")
    print(f"\n{passed}/{total} PASS, {failed} FAIL, {skipped} SKIP")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
