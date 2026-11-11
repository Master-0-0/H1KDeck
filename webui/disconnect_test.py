#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断线/重连 UX 验收：停网关 → 界面提示 → 重启网关 → 自动恢复。"""
import asyncio
import pathlib
import subprocess
import sys

import aiohttp

from ui_screenshot import CDP

BASE = "http://127.0.0.1:8081"
HERE = __file__.rsplit("/", 1)[0]


async def state(cdp, label):
    value = await cdp.eval("""(() => ({
        conn: document.querySelector('#conn-text')?.textContent,
        pill: document.querySelector('#conn-pill')?.className,
        ws: document.querySelector('#status-ws')?.textContent,
        status: document.querySelector('#status-conn')?.textContent,
        sessions: document.querySelectorAll('#sessions-body tr').length,
        inputDisabled: document.querySelector('#console-input')?.disabled,
        view: document.querySelector('.view.active')?.id
    }))()""")
    print(label, value)
    return value


async def main() -> int:
    out = pathlib.Path(HERE) / ".." / "logs" / "webui-shots"
    out.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        async with session.get("http://127.0.0.1:9222/json") as resp:
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

    await cdp.call("Page.navigate", {"url": BASE + "/"})
    await asyncio.sleep(1.0)
    await cdp.eval("""(async () => {
        await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({username:'admin', password:'havoc1234'})});
    })()""")
    await cdp.call("Page.reload", {"ignoreCache": True})
    await asyncio.sleep(2.5)
    await state(cdp, "connected:")
    # 进入控制台，验证断线时输入框被禁用
    await cdp.eval("document.querySelector('#sessions-body tr')?.click()")
    await asyncio.sleep(0.8)
    await cdp.shot(out / "07-console-before-drop.png")

    print("stopping gateway ...")
    subprocess.run(["bash", f"{HERE}/stop-webui.sh"], check=False, stdout=subprocess.DEVNULL)
    await asyncio.sleep(4.0)
    await state(cdp, "stopped:")
    await cdp.shot(out / "08-disconnected.png")

    print("starting gateway ...")
    subprocess.run(["bash", f"{HERE}/start-webui.sh"], check=False, stdout=subprocess.DEVNULL)
    await asyncio.sleep(10.0)
    final = await state(cdp, "restarted:")
    await cdp.shot(out / "09-reconnected.png")
    await cdp.ws.close()

    ok = (final.get("sessions", 0) >= 6 and "已连接" in (final.get("conn") or "")
          and "已连接" in (final.get("ws") or ""))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
