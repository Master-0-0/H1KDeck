#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新 UI（Qt 客户端风格）截图：登录、会话、控制台、文件浏览器、进程、监听器、战利品、团队服务器、菜单。"""
import asyncio
import pathlib
import sys

import aiohttp

from ui_screenshot import CDP

BASE = "http://127.0.0.1:8081"
HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent / "logs" / "webui-shots2"
SIZE = (2233, 1289)  # 与 Qt 客户端窗口同尺寸，便于像素对照


async def eval_js(cdp, expr, await_promise=True):
    return await cdp.eval(expr, await_promise=await_promise)


async def shot(cdp, name):
    await cdp.shot(OUT / name)


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
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
                   {"width": SIZE[0], "height": SIZE[1], "deviceScaleFactor": 1, "mobile": False})

    await cdp.call("Page.navigate", {"url": BASE + "/"})
    await asyncio.sleep(1.2)
    await shot(cdp, "01-login.png")

    status = await eval_js(cdp, """(async () => {
        const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({username:'admin', password:'havoc1234'})});
        return r.status;
    })()""")
    print("login:", status)
    await cdp.call("Page.reload", {"ignoreCache": True})
    await asyncio.sleep(2.5)
    await shot(cdp, "02-main.png")

    demon = await eval_js(cdp, """(() => {
        const rows = Array.from(document.querySelectorAll('#sessions-body tr'));
        const healthy = rows.find(r => (r.querySelector('.status-word')||{}).textContent === 'healthy');
        const row = healthy || rows[0];
        return row ? row.getAttribute('data-id') : null;
    })()""")
    print("demon:", demon)
    if not demon:
        await cdp.ws.close()
        return 1

    await eval_js(cdp, f"openConsole({demon!r})")
    await asyncio.sleep(1.0)
    await eval_js(cdp, f"""(async () => {{
        await fetch('/api/sessions/{demon}/command', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{command:'shell whoami'}})}});
    }})()""")
    await asyncio.sleep(8.0)
    await shot(cdp, "03-console.png")

    # help 全量表（108 条 Demon 基线 + 已加载脚本命令）
    await eval_js(cdp, f"""(async () => {{
        await fetch('/api/sessions/{demon}/command', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{command:'help'}})}});
    }})()""")
    await asyncio.sleep(2.5)
    await eval_js(cdp, """(() => {
        const lines = [...document.querySelectorAll('.console-out .console-line')];
        const help = [...lines].reverse().find((l) => l.textContent.includes('Demon Commands'));
        if (help) help.scrollIntoView({block: 'start'});
    })()""")
    await asyncio.sleep(0.3)
    await shot(cdp, "03b-help.png")

    await eval_js(cdp, f"openFiles({demon!r})")
    await asyncio.sleep(3.0)
    await eval_js(cdp, f"""(async () => {{
        await fetch('/api/sessions/{demon}/files', {{method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{path:'C:\\\\Users\\\\Administrator'}})}});
    }})()""")
    await asyncio.sleep(5.0)
    await shot(cdp, "04-files.png")

    await eval_js(cdp, f"openProcs({demon!r})")
    await asyncio.sleep(8.0)
    await shot(cdp, "05-procs.png")

    await eval_js(cdp, "openTab('listeners', {kind:'listeners', title:'监听器'})")
    await asyncio.sleep(2.5)
    await shot(cdp, "06-listeners.png")

    await eval_js(cdp, """(() => {
        openTab('loot', {kind:'loot', title:'战利品'});
        const tr = document.querySelector('.loot-table tbody tr[data-id]');
        if (tr) tr.click();
    })()""")
    await asyncio.sleep(0.8)
    await shot(cdp, "07-loot.png")
    await eval_js(cdp, """(() => {
        const sel = document.querySelector('.loot-show');
        if (sel) { sel.value = '下载'; sel.dispatchEvent(new Event('change', {bubbles:true})); }
    })()""")
    await asyncio.sleep(0.5)
    await shot(cdp, "07b-loot-downloads.png")

    await eval_js(cdp, "openTab('teamserver', {kind:'teamserver', title:'团队服务器'})")
    await asyncio.sleep(0.6)
    await shot(cdp, "08-teamserver.png")

    await eval_js(cdp, "openTab('chat', {kind:'chat', title:'团队聊天'})")
    await asyncio.sleep(0.6)
    await shot(cdp, "10-chat.png")

    await eval_js(cdp, """(() => {
        const menu = document.querySelector('.menu[data-menu=view]');
        menu.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
    })()""")
    await asyncio.sleep(0.6)
    await shot(cdp, "09-menu.png")

    await cdp.ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
