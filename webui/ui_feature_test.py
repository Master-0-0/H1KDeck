#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc Web 控制台功能验收（Playwright 风格，使用 CDP）。

覆盖：聊天 / 监听器 / 扩展商店 / 脚本加载 / Python 控制台 / 会话图 /
载荷弹窗 / 控制台命令 / 右键菜单 / 文件浏览器 / 进程列表。
"""
import asyncio
import pathlib
import sys

import aiohttp

from ui_screenshot import CDP

BASE = "http://127.0.0.1:8081"
HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent / "logs" / "webui-shots2"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def ev(cdp, expr):
    return await cdp.eval(expr, await_promise=True)


async def click_context_item(cdp, text: str) -> bool:
    pos = await ev(cdp, f"""(() => {{
        const it=[...document.querySelectorAll('#context-menu .menu-item, #submenu-popup .menu-item')].find(x=>x.textContent.includes({text!r}));
        if(!it) return null;
        const r=it.getBoundingClientRect();
        return {{x:r.left+r.width/2, y:r.top+r.height/2}};
    }})()""")
    if not pos:
        return False
    await cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
    await cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
    return True


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
    await cdp.call("Emulation.setDeviceMetricsOverride",
                   {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
    await cdp.call("Page.reload", {"ignoreCache": True})
    await asyncio.sleep(4.0)

    # 会话与聊天
    sessions = await ev(cdp, "S.sessions.size")
    check("会话表加载", sessions >= 1, f"{sessions} 个")
    # 聊天记录只在网关内存中；测试自己发一条，避免依赖上一次运行残留（已存在则不重复发）。
    await ev(cdp, """(async () => {
        const has = (document.querySelector('.chat-list')?.innerText || '').includes('Web 控制台功能测试');
        if (!has) {
            await fetch('/api/chat', {method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({message:'Web 控制台功能测试'})});
        }
        return true;
    })()""")
    await asyncio.sleep(1.0)
    chat = await ev(cdp, "document.querySelector('.chat-list')?.innerText || ''")
    check("团队聊天消息", "Web 控制台功能测试" in chat, chat[:50].replace("\n", " "))

    # 监听器
    await ev(cdp, "openTab('listeners', {kind:'listeners', title:'监听器'})")
    await asyncio.sleep(2)
    ltxt = await ev(cdp, "document.querySelector('.listener-table')?.innerText || ''")
    # 模式标签按设计大写显示（HTTP），断言不应区分大小写
    check("监听器表", "lan-test" in ltxt.lower() and "http" in ltxt.lower(), ltxt[:80].replace("\n", " "))
    stager = await ev(cdp, "document.querySelector('.qd-text')?.textContent || ''")
    check("一键上线状态", "在线" in stager, stager)

    # 扩展商店
    await ev(cdp, "openTab('store', {kind:'store', title:'扩展商店'})")
    rows = 0
    for _ in range(12):
        await asyncio.sleep(1.5)
        rows = await ev(cdp, "document.querySelectorAll('.store-table tbody tr').length")
        if rows > 3:
            break
    source = await ev(cdp, "document.querySelector('.store-source')?.textContent || ''")
    check("扩展商店目录", rows >= 3, f"{rows} 项 · {source}")
    await cdp.shot(OUT / "26-store.png")

    # 脚本加载
    await ev(cdp, "openTab('scripts', {kind:'scripts', title:'脚本'})")
    await asyncio.sleep(2)
    await ev(cdp, """(() => { const it=[...document.querySelectorAll('.scripts-table tr[data-path]')].find(x=>x.textContent.includes('demo')); if(it) it.click(); })()""")
    await asyncio.sleep(1)
    await ev(cdp, """(() => { const b=document.querySelector('.scripts-load'); if(b) b.click(); })()""")
    await asyncio.sleep(4)
    log = await ev(cdp, "document.querySelector('.script-log')?.innerText || ''")
    check("脚本加载与命令注册", "已注册" in log and "pyhello" in log, log[-80:].replace("\n", " | "))
    await cdp.shot(OUT / "27-scripts.png")

    # Python 控制台
    await ev(cdp, "openTab('pyconsole', {kind:'pyconsole', title:'Python 控制台'})")
    await asyncio.sleep(1)
    await ev(cdp, """(() => { const t=document.querySelector('.py-input'); t.value='len(list_sessions())'; document.querySelector('.py-run').click(); })()""")
    await asyncio.sleep(2.5)
    pyout = await ev(cdp, "document.querySelector('.py-output')?.innerText || ''")
    check("Python 控制台", ">>>" in pyout and str(sessions) in pyout, pyout[-60:].replace("\n", " | "))
    await ev(cdp, """(() => { const t=document.querySelector('.py-input'); t.value='d=Demon("22c34f3a"); print(d.Computer, d.ProcessArch)'; document.querySelector('.py-run').click(); })()""")
    await asyncio.sleep(2.5)
    pyout2 = await ev(cdp, "document.querySelector('.py-output')?.innerText || ''")
    check("Python SDK Demon 对象", "ADMIN x64" in pyout2, pyout2[-60:].replace("\n", " | "))
    await cdp.shot(OUT / "28-pyconsole.png")

    # 会话图
    await ev(cdp, "openTab('graph', {kind:'graph', title:'会话图'})")
    await asyncio.sleep(2)
    nodes = await ev(cdp, "document.querySelectorAll('g.graph-node').length")
    check("会话图节点", nodes == sessions + 1, f"{nodes} 节点（含 TEAMSERVER）")
    await cdp.shot(OUT / "29-graph.png")
    await ev(cdp, "showMainView('table')")

    # 载荷弹窗
    await ev(cdp, "openPayloadDialog()")
    await asyncio.sleep(1)
    fields = await ev(cdp, "document.querySelectorAll('.payload-dialog .input').length")
    listeners = await ev(cdp, "document.querySelector('[name=listener]')?.options.length || 0")
    check("载荷生成表单", fields >= 10 and listeners >= 1, f"{fields} 字段 / {listeners} 监听器")
    await cdp.shot(OUT / "30-payload.png")
    await ev(cdp, "closeModal()")

    # 控制台 / 右键 / 文件 / 进程
    demon = await ev(cdp, """(() => {
        const rows = Array.from(document.querySelectorAll('#sessions-body tr'));
        const r = rows.find(x => (x.querySelector('.status-word')||{}).textContent === 'healthy');
        return r ? r.getAttribute('data-id') : null;
    })()""")
    check("选择健康会话", bool(demon), demon or "")
    if demon:
        await ev(cdp, f"openConsole({demon!r})")
        await asyncio.sleep(0.8)
        await ev(cdp, f"""(async () => {{
            await fetch('/api/sessions/{demon}/command', {{method:'POST', headers:{{'Content-Type':'application/json'}},
                body: JSON.stringify({{command:'shell whoami'}})}});
        }})()""")
        await asyncio.sleep(8)
        ctxt = await ev(cdp, "document.querySelector('.console-out')?.innerText || ''")
        check("控制台 shell whoami", "administrator" in ctxt.lower())
        await cdp.shot(OUT / "31-console.png")

        await ev(cdp, f"""(() => {{
            const tr = document.querySelector('#sessions-body tr[data-id="{demon}"]');
            const r = tr.getBoundingClientRect();
            tr.dispatchEvent(new MouseEvent('contextmenu', {{bubbles:true, cancelable:true,
                clientX: r.left + 80, clientY: r.top + 10}}));
        }})()""")
        await asyncio.sleep(0.5)
        await ev(cdp, """(() => {
            const it=[...document.querySelectorAll('#context-menu .menu-item')].find(x=>x.textContent.includes('资源管理器'));
            if(it) it.dispatchEvent(new MouseEvent('mouseenter',{bubbles:true}));
        })()""")
        await asyncio.sleep(0.4)
        menu = await ev(cdp, "(document.querySelector('#context-menu')?.textContent||'')+' '+(document.querySelector('#submenu-popup')?.textContent||'')")
        check("会话右键菜单", "资源管理器" in menu and "文件浏览器" in menu and "进程列表" in menu)

        # 通过右键菜单真实点击打开文件浏览器（回归：菜单项点击不能被 mousedown 吞掉）
        clicked = await click_context_item(cdp, "文件浏览器")
        active = fpath = None
        frows = 0
        for _ in range(20):
            await asyncio.sleep(1)
            active = await ev(cdp, "S.activeTab")
            fpath = await ev(cdp, "document.querySelector('.tab-pane.active .input.path')?.value || ''")
            frows = await ev(cdp, "document.querySelectorAll('.tab-pane.active .data-table tbody tr').length")
            if str(active).startswith("files:") and fpath and frows > 0:
                break
        check("右键菜单打开文件浏览器", clicked and str(active).startswith("files:") and bool(fpath) and frows > 0,
              f"{active} · {fpath} · {frows} 行")
        await cdp.shot(OUT / "32-files.png")

        await ev(cdp, f"openProcs({demon!r})")
        prows = 0
        for _ in range(20):
            await asyncio.sleep(1)
            prows = await ev(cdp, "document.querySelectorAll('.tab-pane.active .data-table tbody tr').length")
            if prows > 20:
                break
        check("进程列表", prows > 20, f"{prows} 行")
        await cdp.shot(OUT / "33-procs.png")

    await cdp.ws.close()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n== {passed}/{len(RESULTS)} 项通过 ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
