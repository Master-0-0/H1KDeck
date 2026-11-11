#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关端到端自测：登录 -> WS 快照 -> 下发命令 -> 观察 console/task 推送。"""
import argparse
import asyncio
import json
import sys

import aiohttp


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8081")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="havoc1234")
    ap.add_argument("--demon", default=None)
    ap.add_argument("--command", default="shell whoami")
    ap.add_argument("--seconds", type=float, default=12)
    args = ap.parse_args()

    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.post(f"{args.base}/api/login",
                                json={"username": args.user, "password": args.password}) as resp:
            print("login:", resp.status, await resp.text())
            if resp.status != 200:
                return 1

        ws = await session.ws_connect(f"{args.base.replace('http', 'ws')}/ws")
        snap = await ws.receive_json()
        print("snapshot:", snap["type"], "sessions:", len(snap["sessions"]),
              "listeners:", len(snap["listeners"]), "conn:", snap["connection"]["state"])
        demon = args.demon or next((s["id"] for s in snap["sessions"] if s["health"] == "healthy"), None)
        if not demon:
            print("no demon")
            return 1
        print("target demon:", demon)

        async with session.post(f"{args.base}/api/sessions/{demon}/command",
                                json={"command": args.command}) as resp:
            print("send:", resp.status, await resp.text())

        end = asyncio.get_event_loop().time() + args.seconds
        while asyncio.get_event_loop().time() < end:
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=end - asyncio.get_event_loop().time())
            except asyncio.TimeoutError:
                break
            t = msg.get("type")
            if t == "console":
                line = msg["line"]
                print(f"[console:{line['kind']}] {line['text'][:160]!r}")
            elif t == "task":
                task = msg["task"]
                print(f"[task] {task['task_id']} status={task['status']} out={task['output'][:80]!r}")
            elif t == "session":
                print(f"[session] {msg.get('action')} {msg.get('session', {}).get('id')}")
            elif t == "event":
                print(f"[event:{msg['event']['level']}] {msg['event']['text'][:140]}")
            elif t == "status":
                pass
            elif t == "connection":
                print("[connection]", msg["connection"]["state"], msg["connection"].get("detail"))
        await ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
