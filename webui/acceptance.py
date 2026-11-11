#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 控制台验收脚本（Phase 1–3）。

覆盖：
  1. 未登录访问 API → 401
  2. 登录 → /api/state 有会话、监听器、连接状态 connected
  3. 会话心跳时间戳合理
  4. shell whoami → admin\\administrator
  5. shell net user → 有用户列表
  6. 非法命令 → 400
  7. 网关重启后 cookie 仍有效、WS 自动重连
  8. 9901 暂存服务未被影响
"""
from __future__ import annotations

import asyncio
import pathlib
import subprocess
import sys
import time

import aiohttp

BASE = "http://127.0.0.1:8081"
STAGER = "http://127.0.0.1:9901/health"
HERE = pathlib.Path(__file__).resolve().parent
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def poll_console(session, demon: str, needle: str, timeout: float = 40.0) -> str:
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        async with session.get(f"{BASE}/api/sessions/{demon}/console?limit=200") as resp:
            data = await resp.json()
        lines = data.get("lines", [])
        last = "\n".join(l["text"] for l in lines[-10:])
        if needle in "".join(l["text"] for l in lines):
            return "".join(l["text"] for l in lines)
        await asyncio.sleep(1.5)
    return last


async def main() -> int:
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        # 1. 未登录
        async with session.get(f"{BASE}/api/state") as resp:
            record("未登录访问 API 返回 401", resp.status == 401, f"status={resp.status}")

        # 2. 登录
        async with session.post(f"{BASE}/api/login",
                                json={"username": "admin", "password": "havoc1234"}) as resp:
            record("Web 登录成功", resp.status == 200, f"status={resp.status}")

        # 3. 快照
        async with session.get(f"{BASE}/api/state") as resp:
            snap = await resp.json()
        sessions = snap.get("sessions", [])
        listeners = snap.get("listeners", [])
        record("快照返回会话", len(sessions) >= 1, f"{len(sessions)} 个会话")
        record("快照返回监听器", any(l["name"] == "LAN-Test" for l in listeners),
               f"{[l['name'] for l in listeners]}")
        record("teamserver 连接状态", snap["connection"]["state"] == "connected",
               snap["connection"].get("detail", ""))

        healthy = [s for s in sessions if s.get("health") == "healthy"]
        record("存在健康会话", len(healthy) >= 1, f"{len(healthy)} 个在线")
        # 本脚本的 whoami / net user 断言是 Windows 语义；显式挑一个健康 Windows 会话，
        # 避免快照顺序变化时选中 Linux 会话（Linux 由 linux_agent_test 覆盖）。
        windows = [s for s in healthy
                   if "windows" in f"{s.get('platform') or ''} {s.get('os') or ''}".lower()]
        demon = (windows or healthy or sessions)[0]["id"]

        fresh = all(abs(time.time() - s["last_seen_ts"]) < 3600 for s in healthy if s.get("last_seen_ts"))
        record("心跳时间戳有效（1 小时内）", fresh)

        # 4. shell whoami
        async with session.post(f"{BASE}/api/sessions/{demon}/command",
                                json={"command": "shell whoami"}) as resp:
            body = await resp.json()
        record("下发 shell whoami", resp.status == 200, body.get("task", {}).get("task_id", ""))
        text = await poll_console(session, demon, "administrator")
        record("shell whoami 回显", "administrator" in text.lower(),
               text.strip().splitlines()[-1] if text.strip() else "无回显")

        # 5. shell net user
        async with session.post(f"{BASE}/api/sessions/{demon}/command",
                                json={"command": "shell net user"}) as resp:
            resp_body = await resp.json()
        record("下发 shell net user", resp.status == 200, resp_body.get("task", {}).get("task_id", ""))
        text = await poll_console(session, demon, "User accounts")
        record("shell net user 回显", "User accounts" in text or "用户帐户" in text,
               "已收到用户列表" if ("User accounts" in text or "用户帐户" in text) else text[-120:])

        # 6. 非法命令
        async with session.post(f"{BASE}/api/sessions/{demon}/command",
                                json={"command": "definitely-not-a-command"}) as resp:
            record("非法命令返回 400", resp.status == 400, f"status={resp.status}")

        # 7. 事件日志包含审计
        async with session.get(f"{BASE}/api/events?limit=200") as resp:
            events = (await resp.json()).get("events", [])
        has_audit = any("shell whoami" in e["text"] for e in events)
        record("事件日志包含命令审计", has_audit, f"{len(events)} 条事件")

        # 8. 网关重启 + 登录态
        print("--- 重启网关 ---")
        subprocess.run(["bash", str(HERE / "stop-webui.sh")], check=False, stdout=subprocess.DEVNULL)
        await asyncio.sleep(1.5)
        subprocess.run(["bash", str(HERE / "start-webui.sh")], check=False, stdout=subprocess.DEVNULL)
        ok_reconnect = False
        for _ in range(15):
            await asyncio.sleep(1.5)
            try:
                async with session.get(f"{BASE}/api/state") as resp:
                    if resp.status != 200:
                        continue
                    snap2 = await resp.json()
                ws = await session.ws_connect(BASE.replace("http", "ws") + "/ws")
                first = await asyncio.wait_for(ws.receive_json(), timeout=8)
                await ws.close()
                # 会话数量取决于在线 Demon（环境会随时间波动），此处只要求仍有会话且 WS 正常。
                if (len(snap2.get("sessions", [])) >= 1
                        and snap2["connection"]["state"] == "connected"
                        and first.get("type") == "snapshot"):
                    ok_reconnect = True
                    break
            except Exception:
                continue
        record("网关重启后 cookie 有效并自动重连", ok_reconnect)

        # 9. stager 未被影响
        try:
            async with session.get(STAGER, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                stager = await resp.json()
            record("9901 暂存服务正常", resp.status == 200 and stager.get("status") == "ok",
                   stager.get("payload", ""))
        except Exception as exc:
            record("9901 暂存服务正常", False, str(exc))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n== {passed}/{total} 项通过 ==")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
