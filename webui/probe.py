#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc WebUI Phase 0 探针：登录 teamserver 并打印事件帧。

用法:
    python3 probe.py [--user Neo] [--seconds 20] [--host 127.0.0.1] [--port 40056]

仅用于已授权测试环境。不修改 teamserver 状态（只登录 + 读事件）。
"""
import argparse
import asyncio
import hashlib
import json
import ssl
import sys
import time
from datetime import datetime

import websockets


def sha3_hex(text: str) -> str:
    return hashlib.sha3_256(text.encode()).hexdigest()


def make_login(user: str, password: str) -> bytes:
    pkg = {
        "Head": {"Event": 1, "User": user, "Time": ""},
        "Body": {
            "SubEvent": 3,
            "Info": {"User": user, "Password": sha3_hex(password)},
        },
    }
    return json.dumps(pkg, separators=(",", ":")).encode()


def brief(pkg: dict) -> str:
    head = pkg.get("Head", {}) or {}
    body = pkg.get("Body", {}) or {}
    ev = head.get("Event")
    sub = body.get("SubEvent")
    info = body.get("Info", {}) or {}
    keys = ",".join(info.keys())
    return f"Event={ev} Sub={sub} user={head.get('User')!r} keys=[{keys}]"


def dump(pkg: dict) -> str:
    out = json.dumps(pkg, ensure_ascii=False, separators=(",", ":"))
    if len(out) > 4000:
        out = out[:4000] + f"... ({len(out)} bytes)"
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=40056)
    ap.add_argument("--user", default="Neo")
    ap.add_argument("--password", default="password1234")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--send", default=None, help="登录后发送的命令行(调试用)")
    args = ap.parse_args()

    url = f"wss://{args.host}:{args.port}/havoc/"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    print(f"[probe] connecting {url} as {args.user}", flush=True)
    async with websockets.connect(url, ssl=ctx, max_size=None, ping_interval=None) as ws:
        await ws.send(make_login(args.user, args.password))
        print("[probe] login sent", flush=True)
        deadline = time.monotonic() + args.seconds
        count = 0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            except websockets.ConnectionClosed as exc:
                print(f"[probe] closed: {exc}", flush=True)
                return 1
            count += 1
            stamp = datetime.now().strftime("%H:%M:%S")
            try:
                pkg = json.loads(raw)
            except Exception:
                print(f"[{stamp}] #{count} non-json {len(raw)}B: {raw[:200]!r}", flush=True)
                continue
            print(f"[{stamp}] #{count} {brief(pkg)}", flush=True)
            print(f"        {dump(pkg)}", flush=True)
        print(f"[probe] done, received {count} messages", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
