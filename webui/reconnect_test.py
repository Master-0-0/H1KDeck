#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收脚本：浏览器登录态在网关重启后依然有效，WS 自动重连并拿到快照。"""
import asyncio
import pathlib
import subprocess
import sys

import aiohttp

BASE = "http://127.0.0.1:8081"
HERE = pathlib.Path(__file__).resolve().parent


async def ws_snapshot(session, timeout=15):
    ws = await session.ws_connect(BASE.replace("http", "ws") + "/ws")
    try:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        return msg
    finally:
        await ws.close()


async def main() -> int:
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.post(f"{BASE}/api/login",
                                json={"username": "admin", "password": "havoc1234"}) as r:
            print("login:", r.status)
            if r.status != 200:
                return 1
        snap = await ws_snapshot(session)
        print("before restart: sessions=%d conn=%s" % (len(snap["sessions"]), snap["connection"]["state"]))

        print("restarting gateway ...")
        subprocess.run(["bash", str(HERE / "stop-webui.sh")], check=False)
        await asyncio.sleep(1.5)
        subprocess.run(["bash", str(HERE / "start-webui.sh")], check=False)

        ok = False
        for attempt in range(1, 16):
            await asyncio.sleep(1.5)
            try:
                snap = await ws_snapshot(session, timeout=8)
            except Exception as exc:
                print(f"  attempt {attempt}: {type(exc).__name__}: {exc}")
                continue
            print(f"after restart: sessions={len(snap['sessions'])} conn={snap['connection']['state']}")
            ok = bool(snap["sessions"]) and snap["connection"]["state"] == "connected"
            if ok:
                break
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
