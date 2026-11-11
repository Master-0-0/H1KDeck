#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 对照工具：抓 Web 图（CDP，尺寸与 Qt 客户端一致 2233x1289）、抓客户端图、
并排拼图 + 像素差统计。用于 UI 高保真迭代的量化反馈。

用法：
    python3 tools/ui_compare.py shot --name main            # 抓 Web 主界面
    python3 tools/ui_compare.py shot --name files --tab files:DEMON
    python3 tools/ui_compare.py client --name main          # 抓 Qt 客户端窗口
    python3 tools/ui_compare.py side --web main --client main --out main-side.png
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import subprocess
import sys

import aiohttp
import websockets

ROOT = pathlib.Path(__file__).resolve().parents[2]
WEBUI = ROOT / "webui"
SHOTS = ROOT / "logs" / "parity"
CLIENT_SHOTS = ROOT / "logs" / "client-shots" / "parity"
BASE = "http://127.0.0.1:8081"
CDP_HTTP = "http://127.0.0.1:9222"
SIZE = (2233, 1289)
WIN_ID = "23068689"


class CDP:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self.seq = 0

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=None, ping_interval=None)

    async def call(self, method: str, params: dict | None = None):
        self.seq += 1
        mid = self.seq
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = json.loads(await self.ws.recv())
            if raw.get("id") == mid:
                if "error" in raw:
                    raise RuntimeError(f"{method}: {raw['error']}")
                return raw.get("result", {})

    async def eval(self, expr: str, await_promise: bool = True):
        result = await self.call("Runtime.evaluate", {
            "expression": expr, "awaitPromise": await_promise, "returnByValue": True,
        })
        return result.get("result", {}).get("value")

    async def shot(self, path: pathlib.Path):
        result = await self.call("Page.captureScreenshot", {"format": "png"})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result["data"]))
        print("saved", path)


async def web_shot(name: str, js: str | None, wait: float = 2.0):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{CDP_HTTP}/json") as resp:
            targets = await resp.json()
    page = next(t for t in targets if t["type"] == "page")
    cdp = CDP(page["webSocketDebuggerUrl"])
    await cdp.connect()
    await cdp.call("Page.enable")
    await cdp.call("Runtime.enable")
    await cdp.call("Network.enable")
    await cdp.call("Network.clearBrowserCookies")
    await cdp.call("Emulation.setDeviceMetricsOverride", {
        "width": SIZE[0], "height": SIZE[1], "deviceScaleFactor": 1, "mobile": False,
    })
    await cdp.call("Page.navigate", {"url": BASE + "/"})
    await asyncio.sleep(1.0)
    if name == "login":
        await cdp.shot(SHOTS / "web-login.png")
        await cdp.ws.close()
        return
    status = await cdp.eval("""(async () => {
        const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({username:'admin', password:'havoc1234'})});
        return r.status;
    })()""")
    print("login:", status)
    await cdp.call("Page.reload", {"ignoreCache": True})
    await asyncio.sleep(1.8)
    if js:
        print("js:", await cdp.eval(js))
        await asyncio.sleep(wait)
    await cdp.shot(SHOTS / f"web-{name}.png")
    await cdp.ws.close()


def client_shot(name: str):
    CLIENT_SHOTS.mkdir(parents=True, exist_ok=True)
    out = CLIENT_SHOTS / f"client-{name}.png"
    subprocess.run(["import", "-window", WIN_ID, str(out)], check=True,
                   env={"DISPLAY": ":0", "PATH": "/usr/bin:/bin"})
    print("saved", out)


def side(web: str, client: str, out: str):
    from PIL import Image, ImageChops, ImageDraw
    a_path = SHOTS / f"web-{web}.png"
    b_path = CLIENT_SHOTS / f"client-{client}.png"
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    # 统一宽度到 1400，便于查看
    W = 1400
    a = a.resize((W, round(a.height * W / a.width)), Image.LANCZOS)
    b = b.resize((W, round(b.height * W / b.width)), Image.LANCZOS)
    H = max(a.height, b.height)
    canvas = Image.new("RGB", (W * 2 + 8, H + 26), (16, 17, 19))
    canvas.paste(a, (0, 26))
    canvas.paste(b, (W + 8, 26))
    d = ImageDraw.Draw(canvas)
    d.text((6, 7), "WEB", fill=(120, 200, 255))
    d.text((W + 14, 7), "QT CLIENT", fill=(120, 255, 160))
    out_path = SHOTS / out
    canvas.save(out_path)
    diff = ImageChops.difference(a, b.resize(a.size))
    bbox = diff.getbbox()
    stat = (sum(diff.convert("L").getdata()) / (a.width * a.height)) if bbox else 0.0
    print("saved", out_path, "mean-diff", round(stat, 2), "bbox", bbox)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("shot")
    p.add_argument("--name", default="main")
    p.add_argument("--js", default=None)
    p.add_argument("--wait", type=float, default=2.0)
    p = sub.add_parser("client")
    p.add_argument("--name", default="main")
    p = sub.add_parser("side")
    p.add_argument("--web", default="main")
    p.add_argument("--client", default="main")
    p.add_argument("--out", default="side.png")
    args = ap.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)
    if args.cmd == "shot":
        asyncio.run(web_shot(args.name, args.js, args.wait))
    elif args.cmd == "client":
        client_shot(args.name)
    else:
        side(args.web, args.client, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
