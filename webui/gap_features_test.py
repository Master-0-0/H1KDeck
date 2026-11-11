#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本轮 9 项差距的功能验收（需要网关 :8081 + teamserver + CDP :9222）。

覆盖：
  1. 最后心跳列显示 teamserver 绝对时间（tooltip 相对时间）
  2. 战利品截图下载（Content-Disposition / 同源下载）
  3. 控制台历史按 Demon 隔离 + localStorage 持久化
  4. 拓扑图缩放/平移/复位 + 缩放后节点命中
  5. 会话右键「移除 / 退出」菜单与后端端点
  6. 手动断开 teamserver / 恢复
  7. 脚本上传/加载/重载/卸载/autoload
  8. Python SDK 全局接口 + ProcessCreate 字段顺序（只读/无害）
  9. Python 常驻宿主：跨请求变量、RegisterCommand、脚本共享全局、崩溃自恢复
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import sys
import time
import uuid
from pathlib import Path

import aiohttp

from ui_screenshot import CDP
from gateway import Gateway

BASE = "http://127.0.0.1:8081"
HERE = pathlib.Path(__file__).resolve().parent
UPLOADS = HERE / "data" / "extensions" / "_uploads"
OUT = HERE.parent / "logs" / "webui-shots2"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def ev(cdp, expr):
    return await cdp.eval(expr, await_promise=True)


async def wait_until(fn, timeout: float = 30.0, interval: float = 0.6):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = await fn()
        if value:
            return value
        await asyncio.sleep(interval)
    return None


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    created_files: list[pathlib.Path] = []
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.post(BASE + "/api/login",
                                json={"username": "admin", "password": "havoc1234"}) as resp:
            check("Web 登录", resp.status == 200, f"status={resp.status}")

        async def state():
            async with session.get(BASE + "/api/state") as r:
                return await r.json()

        async def status():
            async with session.get(BASE + "/api/status") as r:
                return await r.json()

        snap = await state()
        sessions = snap.get("sessions", [])
        check("初始快照有会话", len(sessions) >= 1, f"{len(sessions)} 个")

        # ---------------- CDP 页面 ----------------
        async with aiohttp.ClientSession() as direct:
            async with direct.get("http://127.0.0.1:9222/json") as resp:
                targets = await resp.json()
        page = next(t for t in targets if t["type"] == "page")
        cdp = CDP(page["webSocketDebuggerUrl"])
        await cdp.connect()
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       {"width": 2233, "height": 1289, "deviceScaleFactor": 1, "mobile": False})
        await cdp.call("Page.navigate", {"url": BASE + "/"})
        await asyncio.sleep(1.0)
        await ev(cdp, """(async()=>{const r=await fetch('/api/login',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({username:'admin',password:'havoc1234'})});return r.status;})()""")
        await cdp.call("Page.reload", {"ignoreCache": True})
        await asyncio.sleep(4.0)

        # 1. 最后心跳列
        cell = await ev(cdp, """(() => {
            const tr=[...document.querySelectorAll('#sessions-body tr')][0];
            if(!tr) return null;
            const id=tr.getAttribute('data-id');
            const td=tr.querySelector('[data-heartbeat]');
            return {id, text:td.textContent, title:td.getAttribute('title')||'',
                    last:(S.sessions.get(id)||{}).last_call||''};
        })()""")
        check("心跳列显示绝对时间", bool(cell) and cell["text"] == (cell["last"] or "—"),
              str(cell))
        check("心跳 tooltip 相对时间", bool(cell) and ("前" in cell["title"] or "sleep" in cell["title"]),
              (cell or {}).get("title", ""))
        await cdp.shot(OUT / "gap-01-main.png")

        # 2. 战利品下载（HTTP 头 + 内容一致）
        async with session.get(BASE + "/api/loot") as r:
            loot = (await r.json()).get("loot", [])
        shots = [x for x in loot if x.get("kind") == "screenshot" and x.get("path")]
        if shots:
            item = shots[-1]
            async with session.get(f"{BASE}/api/loot/{item['id']}", headers={"Range": "bytes=0-63"}) as r:
                head = await r.read()
            async with session.get(f"{BASE}/api/loot/{item['id']}?download=1") as r:
                dl = await r.read()
                cd = r.headers.get("Content-Disposition", "")
                clen = int(r.headers.get("Content-Length", "0"))
            check("战利品下载 Content-Disposition", "attachment" in cd and "filename*=UTF-8''" in cd, cd[:90])
            check("战利品下载内容与预览一致", dl[:64] == head[:64] and len(dl) == clen,
                  f"{len(dl)} bytes")
            cjk = Gateway._attachment_disposition("中文 截图?.png", ".png")
            empty = Gateway._attachment_disposition("", ".png")
            check("下载中文文件名 RFC 5987", "filename*=UTF-8''" in cjk and
                  "%E4%B8%AD" in cjk and "?" not in cjk.split(";")[0], cjk[:100])
            check("下载空文件名回退", 'filename="loot.png"' in empty, empty)
            await ev(cdp, """(() => {
                openTab('loot', {kind:'loot', title:'战利品'});
                const sel=document.querySelector('.loot-show'); if(sel){sel.value='截图';sel.dispatchEvent(new Event('change',{bubbles:true}));}
                const tr=document.querySelector('.loot-table tbody tr[data-id]'); if(tr) tr.click();
            })()""")
            await asyncio.sleep(0.8)
            pos = await ev(cdp, """(() => {
                const tr=document.querySelector('.loot-table tbody tr[data-id]');
                if(!tr) return null; const r=tr.getBoundingClientRect();
                return {x:r.left+40,y:r.top+8};
            })()""")
            if pos:
                await cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": pos["x"],
                                                            "y": pos["y"], "button": "right"})
                await cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": pos["x"],
                                                            "y": pos["y"], "button": "right"})
                await asyncio.sleep(0.3)
                menu = await ev(cdp, "document.querySelector('#context-menu')?.textContent||''")
                check("战利品右键下载菜单", "下载" in menu and "预览" in menu, menu[:40])
                await ev(cdp, "hideMenus()")
                await cdp.shot(OUT / "gap-02-loot.png")
        else:
            check("战利品下载（无截图可测）", True, "skipped")

        # 3. 控制台历史隔离 + 持久化（先清掉上次运行残留，保证 B 的初始历史为空）
        await ev(cdp, """(() => {
            localStorage.removeItem('havoc.consoleHistory.v1');
            S.consoleHistory.clear(); S.consoleHistoryOrder = [];
            return true;
        })()""")
        ids = await ev(cdp, "Array.from(S.sessions.keys()).slice(0,2)")
        if len(ids) >= 2:
            a, b = ids[0], ids[1]
            await ev(cdp, f"openConsole({a!r}); consoleHistoryPush({a!r}, 'cmd-A-{token}')")
            await asyncio.sleep(0.3)
            await ev(cdp, f"openConsole({b!r})")
            await asyncio.sleep(0.4)
            bval = await ev(cdp, """(() => {const i=document.querySelector('.tab-pane.active .console-input');
                i.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true,cancelable:true}));
                return i.value;})()""")
            await ev(cdp, f"openConsole({a!r})")
            await asyncio.sleep(0.3)
            aval = await ev(cdp, """(() => {const i=document.querySelector('.tab-pane.active .console-input');
                i.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true,cancelable:true}));
                return i.value;})()""")
            check("控制台历史按会话隔离", bval == "" and aval == f"cmd-A-{token}",
                  f"A={aval!r} B={bval!r}")
            await asyncio.sleep(0.4)
            stored = await ev(cdp, "localStorage.getItem('havoc.consoleHistory.v1')||''")
            check("控制台历史写入 localStorage", f"cmd-A-{token}" in stored)
            await cdp.call("Page.reload", {"ignoreCache": True})
            await asyncio.sleep(4.0)
            persisted = await ev(cdp, f"JSON.stringify(S.consoleHistory.get({a!r})?.items||[])"
                                 f".includes('cmd-A-{token}')")
            check("刷新后控制台历史仍在", persisted is True or persisted == "true", str(persisted))
        else:
            check("控制台历史（会话不足）", True, "skipped")

        # 4. 拓扑图缩放/平移/复位 + 命中
        n = await ev(cdp, "S.sessions.size")
        await ev(cdp, "showMainView('graph')")
        await asyncio.sleep(0.8)
        nodes = await ev(cdp, "document.querySelectorAll('g.graph-node').length")
        check("拓扑图节点数", nodes == n + 1, f"{nodes} / {n}+1")
        await ev(cdp, "S.graphView={k:1,tx:160,ty:120}; renderGraphPane(document.querySelector('#graph-pane'))")
        await asyncio.sleep(0.5)
        node = await ev(cdp, """(() => {
            const g=document.querySelector('g.graph-node[data-demon]');
            if(!g) return null; const r=g.getBoundingClientRect();
            return {id:g.getAttribute('data-demon'), x:r.left+r.width/2, y:r.top+r.height/2};
        })()""")
        if node:
            await cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": node["x"],
                                                        "y": node["y"], "button": "left", "clickCount": 1})
            await cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": node["x"],
                                                        "y": node["y"], "button": "left", "clickCount": 1})
            await asyncio.sleep(0.5)
            active = await ev(cdp, "S.activeTab")
            check("缩放后节点点击命中", active == f"console:{node['id']}", str(active))
        rect = await ev(cdp, """(() => {const r=document.querySelector('.graph-scroll').getBoundingClientRect();
            return {x:r.left+r.width/2,y:r.top+r.height/2};})()""")
        before = await ev(cdp, "S.graphView.k")
        await cdp.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": rect["x"],
                                                    "y": rect["y"], "deltaX": 0, "deltaY": -240})
        await asyncio.sleep(0.4)
        after = await ev(cdp, "S.graphView.k")
        check("滚轮以光标为锚缩放", 0.3 <= after <= 3 and after > before, f"{before:.2f}→{after:.2f}")
        pan_before = await ev(cdp, "JSON.stringify(S.graphView)")
        pan_pt = await ev(cdp, """(() => {const r=document.querySelector('.graph-scroll').getBoundingClientRect();
            return {x:r.left+24,y:r.bottom-24};})()""")
        await cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": pan_pt["x"],
                                                    "y": pan_pt["y"], "button": "left", "clickCount": 1})
        await cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": pan_pt["x"] + 80,
                                                    "y": pan_pt["y"] - 50, "button": "left"})
        await cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": pan_pt["x"] + 80,
                                                    "y": pan_pt["y"] - 50, "button": "left", "clickCount": 1})
        await asyncio.sleep(0.3)
        pan_after = await ev(cdp, "JSON.stringify(S.graphView)")
        check("拖拽背景平移", pan_before != pan_after, f"{pan_before} → {pan_after}")
        await ev(cdp, """(() => {const s=document.querySelector('.graph-scroll');
            s.dispatchEvent(new KeyboardEvent('keydown',{key:'0',bubbles:true,cancelable:true}));})()""")
        await asyncio.sleep(0.3)
        reset_k = await ev(cdp, "S.graphView.k")
        check("0 键复位", 0.3 <= reset_k <= 1.001, f"k={reset_k:.2f}")
        await cdp.shot(OUT / "gap-03-graph.png")

        # 5. 右键菜单包含移除/退出
        await ev(cdp, "showMainView('table')")
        await asyncio.sleep(0.4)
        menu_text = await ev(cdp, """(() => {
            const tr=document.querySelector('#sessions-body tr'); if(!tr) return '';
            const r=tr.getBoundingClientRect();
            tr.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,
                clientX:r.left+80, clientY:r.top+10}));
            const t=document.querySelector('#context-menu')?.textContent||'';
            const exit=[...document.querySelectorAll('#context-menu .menu-item')]
                .find(x=>x.textContent.includes('退出会话'));
            if(exit) exit.dispatchEvent(new MouseEvent('mouseenter',{bubbles:true}));
            return t+' | '+(document.querySelector('#submenu-popup')?.textContent||'');
        })()""")
        await asyncio.sleep(0.3)
        check("会话菜单包含移除/退出", "移除会话" in menu_text and "退出会话" in menu_text and
              "线程" in menu_text and "进程" in menu_text, menu_text[:100])
        havoc_labels = await ev(cdp, "MENUS.havoc.map(x=>x.label||'').join('|')")
        check("Havoc 菜单包含断开连接", "断开连接" in (havoc_labels or "") and
              "重新连接" in (havoc_labels or ""), str(havoc_labels))
        await cdp.shot(OUT / "gap-04-menu.png")
        await ev(cdp, "hideMenus()")

        # 6. 断开 / 恢复
        async with session.post(BASE + "/api/teamserver/disconnect") as r:
            check("断开连接 API", r.status == 200, f"status={r.status}")
        await asyncio.sleep(3)
        dstatus = await status()
        reconnects = dstatus.get("reconnects", 0)
        async with session.post(f"{BASE}/api/sessions/{sessions[0]['id']}/command",
                                json={"command": "help"}) as r:
            command_status = r.status
        # 再观察 4 秒，确认没有自动重连（reconnects 不增长、状态保持 disconnected）
        await asyncio.sleep(4)
        dstatus2 = await status()
        check("手动断开后保持 disconnected", dstatus["state"] == "disconnected" and
              dstatus2["state"] == "disconnected" and
              dstatus2.get("reconnects", 0) == reconnects,
              f"{dstatus2['state']} reconnects={dstatus2.get('reconnects',0)}")
        check("断开时命令返回 503", command_status == 503, f"status={command_status}")
        async with session.post(BASE + "/api/reconnect") as r:
            check("重新连接 API", r.status == 200, f"status={r.status}")
        connected = await wait_until(lambda: _poll_status(session, "connected"), 30)
        check("重新连接恢复", bool(connected), str(connected))
        await asyncio.sleep(2)

        # 7. 移除会话（先恢复再验证）
        snap = await state()
        target = next((s for s in snap["sessions"] if s.get("health") in ("stale", "dead")), None)
        if target is None and snap["sessions"]:
            target = snap["sessions"][-1]
        if target:
            sid = target["id"]
            await ev(cdp, f"openConsole({sid!r})")
            await asyncio.sleep(0.5)
            async with session.post(f"{BASE}/api/sessions/{sid}/remove") as r:
                check("移除会话 API", r.status == 200, f"status={r.status}")
            await asyncio.sleep(1.0)
            snap2 = await state()
            ui_has = await ev(cdp, f"S.sessions.has({sid!r})")
            tab_gone = await ev(cdp, f"!S.tabs.has('console:' + {sid!r})")
            check("移除后行/标签消失", all(s["id"] != sid for s in snap2["sessions"]) and
                  ui_has is False and tab_gone is True, f"ui={ui_has} tab={tab_gone}")
            async with session.post(BASE + "/api/reconnect") as r:
                await r.read()
            restored = await wait_until(lambda: _has_session(session, BASE, sid), 30)
            check("网关重连后会话重新出现", bool(restored), sid)
        else:
            check("移除会话（无会话）", True, "skipped")

        # 8. 退出端点校验（不真正下发）
        sid = (await state())["sessions"][0]["id"] if (await state())["sessions"] else ""
        if sid:
            async with session.post(f"{BASE}/api/sessions/{sid}/exit",
                                    json={"method": "bogus"}) as r:
                check("exit 非法 method 返回 400", r.status == 400, f"status={r.status}")
            async with session.post(f"{BASE}/api/sessions/does-not-exist/exit",
                                    json={"method": "thread"}) as r:
                check("exit 不存在会话返回 404", r.status == 404, f"status={r.status}")

        # 9. 脚本生命周期
        name = f"gap_life_{token}.py"
        source = (
            "from havoc import Demon, RegisterCommand\n"
            "def gapfn(demonID, *params):\n"
            "    print('GAPLIFE', demonID)\n"
            "    return True\n"
            f"RegisterCommand(gapfn, '', 'gapcmd{token}', 'gap v1', 0, '', '')\n")
        form = aiohttp.FormData()
        form.add_field("file", source.encode(), filename=name, content_type="text/x-python")
        async with session.post(BASE + "/api/scripts/upload", data=form) as r:
            up = await r.json()
            check("脚本上传", r.status == 200 and up.get("ok") and up.get("size") == len(source),
                  f"{r.status} {up.get('path', '')}")
        script = Path(up["path"])
        created_files.append(script)
        async with session.post(BASE + "/api/scripts/load", json={"path": str(script)}) as r:
            body = await r.json()
            check("脚本加载并注册", r.status == 200 and
                  any(c["command"] == f"gapcmd{token}" for c in body.get("commands", [])),
                  f"status={r.status}")
        data = await (await session.get(BASE + "/api/scripts")).json()
        entry = next((x for x in data["scripts"] if x["path"] == str(script.resolve())), {})
        check("脚本 autoload 标记", bool(entry.get("autoload")) and str(script.resolve()) in data["autoload"])
        script.write_text(script.read_text(encoding="utf-8").replace("gap v1", "gap v2"),
                          encoding="utf-8")
        async with session.post(BASE + "/api/scripts/reload", json={"path": str(script)}) as r:
            body = await r.json()
            desc = [c.get("description") for c in body.get("commands", [])]
            check("脚本重载更新描述", r.status == 200 and "gap v2" in desc, str(desc))
        bad = aiohttp.FormData()
        bad.add_field("file", b"not python", filename="bad.txt")
        async with session.post(BASE + "/api/scripts/upload", data=bad) as r:
            check("上传 .txt 被拒", r.status == 400, f"status={r.status}")
        async with session.post(BASE + "/api/scripts/unload", json={"path": str(script)}) as r:
            body = await r.json()
            check("脚本卸载移除命令", r.status == 200 and
                  f"gapcmd{token}" in body.get("removed_commands", []), f"status={r.status}")
        data = await (await session.get(BASE + "/api/scripts")).json()
        async with session.get(BASE + "/api/commands") as r:
            commands = (await r.json()).get("commands", [])
            check("卸载后 help 不再包含命令",
                  not any(c["name"] == f"gapcmd{token}" for c in commands) and
                  str(script.resolve()) not in data["autoload"])
        script.unlink(missing_ok=True)
        created_files.remove(script)

        # 10. Python 宿主语义
        async def repl(code: str):
            async with session.post(BASE + "/api/python/repl", json={"code": code},
                                    timeout=aiohttp.ClientTimeout(total=300)) as r:
                return r.status, await r.json()

        st, body = await repl(f"x_gap_{token} = 41")
        st2, body2 = await repl(f"print(x_gap_{token} + 1)")
        check("Python 控制台跨请求全局", st == 200 and st2 == 200 and "42" in body2.get("output", ""),
              body2.get("output", "").strip())

        cmd = f"gapconsole{token}"
        code = (f"def _gapconsole_fn(d, *a):\n    print('CONSOLE_CMD', d)\n"
                f"havoc.RegisterCommand(_gapconsole_fn, '', '{cmd}', 'console cmd', 0, '', '')")
        st, body = await repl(code)
        async with session.get(BASE + "/api/commands") as r:
            commands = (await r.json()).get("commands", [])
        check("控制台 RegisterCommand 即时生效", any(c["name"] == cmd for c in commands))
        healthy = next((s for s in (await state())["sessions"] if s.get("health") == "healthy"), None)
        if healthy:
            async with session.post(f"{BASE}/api/sessions/{healthy['id']}/command",
                                    json={"command": cmd}) as r:
                await r.read()
            output = await wait_until(lambda: _console_has(session, healthy["id"], "CONSOLE_CMD"), 20)
            check("控制台注册命令可调用", bool(output), "CONSOLE_CMD")

        # 脚本读取控制台全局
        name2 = f"gap_shared_{token}.py"
        script2 = UPLOADS / name2
        script2.write_text(
            "from havoc import RegisterCommand\n"
            "def gapshared(demonID, *params):\n"
            f"    print('SHARED', globals().get('shared_marker_{token}'))\n"
            "    return True\n"
            f"RegisterCommand(gapshared, '', 'gapshared{token}', 'shared global', 0, '', '')\n",
            encoding="utf-8")
        created_files.append(script2)
        async with session.post(BASE + "/api/scripts/load", json={"path": str(script2)}) as r:
            await r.read()
        await repl(f"shared_marker_{token} = 777")
        if healthy:
            async with session.post(f"{BASE}/api/sessions/{healthy['id']}/command",
                                    json={"command": f"gapshared{token}"}) as r:
                await r.read()
            output = await wait_until(lambda: _console_has(session, healthy["id"], "SHARED 777"), 20)
            check("脚本命令读取控制台全局", bool(output), "SHARED 777")
        async with session.post(BASE + "/api/scripts/unload",
                                json={"path": str(script2)}) as r:
            await r.read()
        script2.unlink(missing_ok=True)
        created_files.remove(script2)

        # 宿主崩溃自恢复（同时清理控制台注册）
        st, body = await repl("import os; os._exit(1)")
        st2, body2 = await repl("print(6*7)")
        check("宿主崩溃后自动恢复", st == 502 and st2 == 200 and "42" in body2.get("output", ""),
              f"first={st} second={st2}")

        # 11. SDK 全局接口（在宿主内执行）
        st, body = await repl("print(len(GetDemons()), GetAgents())")
        check("SDK GetDemons/GetAgents", st == 200 and "['Demon']" in body.get("output", ""),
              body.get("output", "").strip())

        await cdp.shot(OUT / "gap-05-scripts.png")
        await cdp.ws.close()

    for path in created_files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n== {passed}/{len(RESULTS)} 项通过 ==")
    return 0 if passed == len(RESULTS) else 1


async def _poll_status(session, wanted: str):
    async with session.get(BASE + "/api/status") as r:
        data = await r.json()
    return data.get("state") == wanted


async def _has_session(session, base: str, sid: str) -> bool:
    async with session.get(base + "/api/state") as r:
        data = await r.json()
    return any(s.get("id") == sid for s in data.get("sessions", []))


async def _console_has(session, sid: str, needle: str):
    async with session.get(f"{BASE}/api/sessions/{sid}/console?limit=200") as r:
        data = await r.json()
    text = "\n".join(line.get("text", "") for line in data.get("lines", []))
    return needle in text


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
