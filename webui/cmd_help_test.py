#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`<命令> help` / 用法提示 的回归测试（Web 控制台）。

验证：
  1. `klist help` 与 `help klist` 输出一致（缺参数时能直接查用法）；
  2. `schtasksquery help`、`proc help`（模块）同样能查；
  3. `cat help` 这类首参是路径的命令不被当成帮助（仍然下发给 agent）；
  4. 参数不足被拒时，错误信息里带「用法：…」和「<cmd> help」提示。

用法：cd webui && python3 cmd_help_test.py
前置：gateway(:8081) 在线，至少一个会话。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
BASE = f"http://127.0.0.1:{CONFIG['web'].get('port', 8081)}"
USER = CONFIG["web"].get("user", "admin")
PASS = CONFIG["web"].get("password", "havoc1234")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def send(session, sid: str, line: str) -> tuple[int, dict]:
    async with session.post(f"{BASE}/api/sessions/{sid}/command", json={"command": line}) as resp:
        return resp.status, await resp.json()


async def main() -> int:
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.post(f"{BASE}/api/login",
                                json={"username": USER, "password": PASS}) as resp:
            if resp.status != 200:
                print("login failed", resp.status)
                return 2
        async with session.get(f"{BASE}/api/sessions") as resp:
            sessions = (await resp.json())["sessions"]
        if not sessions:
            print("no session")
            return 2
        sid = sessions[0]["id"]

        st1, b1 = await send(session, sid, "klist help")
        st2, b2 = await send(session, sid, "help klist")
        check("klist help 返回用法", st1 == 200 and "Usage" in b1.get("help", ""), f"status={st1}")
        check("klist help 与 help klist 等价",
              b1.get("help") == b2.get("help") and st2 == 200, f"{st2}")

        st, b = await send(session, sid, "schtasksquery help")
        check("schtasksquery help 含 taskpath",
              st == 200 and "taskpath" in b.get("help", ""), f"status={st}")

        st, b = await send(session, sid, "proc help")
        check("模块 proc help 仍然正常",
              st == 200 and "Command       :  proc" in b.get("help", ""), f"status={st}")

        st, b = await send(session, sid, "cat help")
        check("cat help 不当成帮助（仍下发）",
              st == 200 and not b.get("help") and bool((b.get("task") or {}).get("task_id")),
              f"status={st}")

        st, b = await send(session, sid, "klist")
        msg = b.get("message", "")
        check("缺参数时带用法与提示",
              st == 400 and "用法：" in msg and "klist help" in msg, msg.replace("\n", " | ")[:80])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n== {passed}/{len(RESULTS)} 项通过 ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
