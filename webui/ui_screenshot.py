#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Chromium CDP 给 Web 控制台截图（开发/验收用）。

前置：
    chromium --headless=new --no-sandbox --remote-debugging-port=9222 \
             --window-size=1600,1000 --user-data-dir=/tmp/chrome-webui about:blank &

用法：
    python3 ui_screenshot.py --out ../logs/webui-shots
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import time

import aiohttp
import websockets


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

    async def eval(self, expression: str, await_promise: bool = True):
        result = await self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        })
        return result.get("result", {}).get("value")

    async def shot(self, path: pathlib.Path, full: bool = False):
        params = {"format": "png"}
        if full:
            params["captureBeyondViewport"] = True
        result = await self.call("Page.captureScreenshot", params)
        path.write_bytes(base64.b64decode(result["data"]))
        print("saved", path, path.stat().st_size, "bytes")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--base", default="http://127.0.0.1:8081")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="havoc1234")
    ap.add_argument("--out", default="../logs/webui-shots")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{args.cdp}/json") as resp:
            targets = await resp.json()
    page = next(t for t in targets if t["type"] == "page")
    cdp = CDP(page["webSocketDebuggerUrl"])
    await cdp.connect()

    await cdp.call("Page.enable")
    await cdp.call("Runtime.enable")
    await cdp.call("Network.enable")
    await cdp.call("Network.clearBrowserCookies")
    await cdp.call("Emulation.setDeviceMetricsOverride",
                   {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})

    await cdp.call("Page.navigate", {"url": args.base + "/"})
    await asyncio.sleep(1.2)
    await cdp.shot(out / "01-login.png")

    # 登录：直接调用页面里的 fetch，拿到 cookie
    login_js = f"""
    (async () => {{
      const r = await fetch('/api/login', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{username: {json.dumps(args.user)}, password: {json.dumps(args.password)}}})
      }});
      return r.status;
    }})()
    """
    status = await cdp.eval(login_js)
    print("login status:", status)
    await cdp.call("Page.reload", {"ignoreCache": True})
    await asyncio.sleep(2.0)
    await cdp.shot(out / "02-sessions.png")

    # 打开第一个健康会话的控制台
    opened = await cdp.eval("""
    (() => {
      const rows = Array.from(document.querySelectorAll('#sessions-body tr'));
      const row = rows.find(r => r.querySelector('.status-dot.healthy')) || rows[0];
      if (!row) return 'no-row';
      row.click();
      return row.getAttribute('data-demon');
    })()
    """)
    print("opened console:", opened)
    await asyncio.sleep(1.0)
    if opened and opened != "no-row":
        await cdp.eval("""
        (async () => {
          await fetch('/api/sessions/' + encodeURIComponent(window.__demo || '') + '/command', {method:'POST'});
        })()
        """) if False else None
        demo = opened
        await cdp.eval(f"""
        (async () => {{
          await fetch('/api/sessions/{demo}/command', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{command: 'shell whoami'}})
          }});
        }})()
        """)
        await asyncio.sleep(6.5)
        await cdp.shot(out / "03-console.png")

    # 监听器
    await cdp.eval("document.querySelector('.nav-item[data-view=listeners]').click()")
    await asyncio.sleep(0.6)
    await cdp.shot(out / "04-listeners.png")

    # 事件
    await cdp.eval("document.querySelector('.nav-item[data-view=events]').click()")
    await asyncio.sleep(0.6)
    await cdp.shot(out / "05-events.png")

    # 帮助
    await cdp.eval("document.querySelector('.nav-item[data-view=help]').click()")
    await asyncio.sleep(0.4)
    await cdp.shot(out / "06-help.png")

    await cdp.ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
