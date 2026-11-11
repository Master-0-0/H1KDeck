#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件管理器前端交互回归（Chromium CDP）。

前置：
    chromium --headless=new --no-sandbox --remote-debugging-port=9222 \
             --window-size=1600,1000 --user-data-dir=/tmp/chrome-webui about:blank &
    Web 网关 + teamserver + 至少一个目标平台会话在线。

用法：
    python3 file_manager_ui_test.py --platform linux
    python3 file_manager_ui_test.py --platform windows
    CHROME_CDP=http://127.0.0.1:9222 python3 file_manager_ui_test.py

测试项：工具栏、新建目录、进入目录、新建文件、浏览器上传、右键菜单、
编辑器加载/保存、删除文件、返回上级、递归删除目录。
仅用于已获得明确授权的测试环境。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
import websockets

HERE = Path(__file__).resolve().parent
try:
    CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
except Exception:
    CONFIG = {}

BASE = os.environ.get("HAVOC_WEBUI", f"http://127.0.0.1:{CONFIG.get('web', {}).get('port', 8081)}").rstrip("/") + "/"
CDP = os.environ.get("CHROME_CDP", "http://127.0.0.1:9222").rstrip("/")
USER = CONFIG.get("web", {}).get("user", "admin")
PASSWORD = CONFIG.get("web", {}).get("password", "havoc1234")

RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


class Session:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self.seq = 0

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.ws_url, max_size=None, ping_interval=None)

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.seq += 1
        mid = self.seq
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = json.loads(await self.ws.recv())
            if raw.get("id") != mid:
                continue
            if "error" in raw:
                raise RuntimeError(str(raw["error"]))
            return raw.get("result", {})

    async def js(self, expression: str):
        result = await self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False)[:1200])
        return result.get("result", {}).get("value")

    async def wait_js(self, expression: str, timeout: float = 45.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.js(expression):
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)
        return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="文件管理器 UI 回归")
    parser.add_argument("--platform", choices=["linux", "windows"], default="linux")
    parser.add_argument("--session", default="", help="指定会话 ID（默认自动挑选）")
    parser.add_argument("--screenshot", default="", help="截图保存路径")
    args = parser.parse_args()

    async with aiohttp.ClientSession() as http:
        try:
            tabs = await (await http.get(CDP + "/json")).json()
        except Exception as exc:
            print(f"无法连接 Chromium CDP {CDP}：{exc}", file=sys.stderr)
            return 2
    pages = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        print("CDP 没有可用 page target", file=sys.stderr)
        return 2

    cdp = Session(pages[0]["webSocketDebuggerUrl"])
    await cdp.connect()
    await cdp.call("Page.enable")
    await cdp.call("Runtime.enable")
    await cdp.call("Page.navigate", {"url": BASE})
    await asyncio.sleep(0.8)
    await cdp.call("Page.reload", {"ignoreCache": True})
    await asyncio.sleep(3)

    if not await cdp.js("!!(window.S && S.sessions && S.sessions.size)"):
        await cdp.js("(async()=>{await api('/api/login',{method:'POST',body:{username:%s,password:%s}});await bootApp();return true;})()"
                     % (json.dumps(USER), json.dumps(PASSWORD)))
        await asyncio.sleep(2)

    sess = args.session
    if not sess:
        if args.platform == "linux":
            sess = await cdp.js("(Array.from(S.sessions.values()).find(s=>/linux/i.test((s.platform||'')+' '+(s.os||'')))||{}).id")
        else:
            sess = await cdp.js("(Array.from(S.sessions.values()).find(s=>/windows/i.test((s.platform||'')+' '+(s.os||''))&&s.health==='healthy')||{}).id")
    if not sess:
        print(f"没有可用的 {args.platform} 会话", file=sys.stderr)
        return 2
    record(True, f"找到 {args.platform} 会话", sess)

    platform = args.platform
    st = str(int(time.time()))[-6:]
    dname = f"ui-fm-{platform[0]}-{st}"
    fname = "note.txt"
    sep = "/" if platform == "linux" else "\\"
    initial = ""
    base_dir = ""
    try:
        await cdp.js(f"openFiles({json.dumps(sess)})")
        ok = await cdp.wait_js("(()=>{const p=document.querySelector('.files-wrap .input.path');return !!p&&p.value.length>1;})()", 50)
        initial = await cdp.js("(document.querySelector('.files-wrap .input.path')||{}).value || ''")
        record(ok and (initial.startswith("/") or initial.startswith("C:")), "初始目录为绝对路径", initial)
        buttons = await cdp.js("[...document.querySelectorAll('.files-wrap .file-toolbar .btn')].map(b=>b.textContent.trim())")
        record(all(x in buttons for x in ["刷新", "新建目录", "新建文件", "上传"]), "工具栏按钮", str(buttons))

        await cdp.js("[...document.querySelectorAll('.files-wrap .file-toolbar .btn')].find(b=>b.textContent.includes('新建目录')).click()")
        await asyncio.sleep(0.3)
        await cdp.js(f"(()=>{{const m=document.querySelector('.modal');m.querySelector('.fm-name').value={json.dumps(dname)};m.querySelector('.modal-foot .btn.primary').click();}})()")
        has = await cdp.wait_js(f"[...document.querySelectorAll('.files-table tbody tr')].some(r=>r.getAttribute('data-name')==={json.dumps(dname)})", 40)
        record(has, "新建目录", dname)

        await cdp.js(f"(()=>{{const tr=[...document.querySelectorAll('.files-table tbody tr')].find(r=>r.getAttribute('data-name')==={json.dumps(dname)});tr.dispatchEvent(new MouseEvent('dblclick',{{bubbles:true}}));}})()")
        ok = await cdp.wait_js(f"(document.querySelector('.files-wrap .input.path')||{{}}).value.endsWith({json.dumps(dname)})", 45)
        cur = await cdp.js("(document.querySelector('.files-wrap .input.path')||{}).value || ''")
        record(ok, "进入目录", cur)
        empty = await cdp.wait_js("[...document.querySelectorAll('.files-table tbody tr')].some(r=>r.textContent.includes('目录为空'))", 25)
        record(empty, "空目录提示")

        base_dir = cur
        await cdp.js("[...document.querySelectorAll('.files-wrap .file-toolbar .btn')].find(b=>b.textContent.includes('新建文件')).click()")
        await asyncio.sleep(0.3)
        await cdp.js(f"(()=>{{const m=document.querySelector('.modal');m.querySelector('.fm-name').value={json.dumps(fname)};m.querySelector('.fm-content').value='hello from ui\\n第二行\\n';m.querySelector('.modal-foot .btn.primary').click();}})()")
        has = await cdp.wait_js(f"[...document.querySelectorAll('.files-table tbody tr')].some(r=>r.getAttribute('data-name')==={json.dumps(fname)})", 45)
        record(has, "新建文件", fname)

        await cdp.js(f"""window.__uploadDone=false;
          (async()=>{{
            const f=new File(['uploaded from browser'], 'upload.txt', {{type:'text/plain'}});
            await uploadFiles({json.dumps(sess)},[f],document.querySelector('.files-wrap .input.path').value);
            window.__uploadDone=true;
          }})()""")
        uploaded = await cdp.wait_js("window.__uploadDone===true", 60)
        has = await cdp.wait_js("[...document.querySelectorAll('.files-table tbody tr')].some(r=>r.getAttribute('data-name')==='upload.txt')", 30)
        record(uploaded and has, "浏览器上传", "upload.txt")

        await cdp.js(f"(()=>{{const tr=[...document.querySelectorAll('.files-table tbody tr')].find(r=>r.getAttribute('data-name')==={json.dumps(fname)});tr.querySelector('.file-more').click();}})()")
        await asyncio.sleep(0.3)
        menu = await cdp.js("[...document.querySelectorAll('#context-menu .menu-item')].map(x=>x.textContent.trim())")
        record(all(x in menu for x in ["编辑", "下载", "重命名", "复制到…", "移动到…", "删除", "复制路径", "属性"]),
               "右键菜单", str(menu))
        await cdp.js("[...document.querySelectorAll('#context-menu .menu-item')].find(x=>x.textContent.trim()==='编辑').click()")
        ok = await cdp.wait_js("!!document.querySelector('.file-editor-text')", 45)
        text = await cdp.js("(document.querySelector('.file-editor-text')||{}).value || ''")
        record(ok and text == "hello from ui\n第二行\n", "编辑器加载", repr(text[:60]))

        await cdp.js("(()=>{const t=document.querySelector('.file-editor-text');t.value='edited via ui\\n';t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.file-editor-modal .modal-foot .btn.primary').click();})()")
        closed = await cdp.wait_js("!document.querySelector('.file-editor-modal')", 60)
        content = await cdp.js(f"(async()=>{{const r=await api('/api/sessions/{sess}/files/read?path='+encodeURIComponent({json.dumps(base_dir)}+'/{fname}'));return r.content;}})()")
        record(closed and content == "edited via ui\n", "编辑器保存", repr(content[:60]))

        if args.screenshot:
            shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
            Path(args.screenshot).write_bytes(base64.b64decode(shot["data"]))
            record(True, "截图", args.screenshot)

        # 删除文件
        await cdp.js(f"(()=>{{const tr=[...document.querySelectorAll('.files-table tbody tr')].find(r=>r.getAttribute('data-name')==={json.dumps(fname)});tr.querySelector('.file-more').click();}})()")
        await asyncio.sleep(0.3)
        await cdp.js("[...document.querySelectorAll('#context-menu .menu-item')].find(x=>x.textContent.trim()==='删除').click()")
        await asyncio.sleep(0.3)
        await cdp.js("document.querySelector('.modal .modal-foot .btn.danger').click()")
        gone = await cdp.wait_js(f"![...document.querySelectorAll('.files-table tbody tr')].some(r=>r.getAttribute('data-name')==={json.dumps(fname)})", 45)
        record(gone, "删除文件", fname)

        # 返回上级并递归删除目录
        await cdp.js("document.querySelector('.files-wrap .up-btn').click()")
        await cdp.wait_js(f"!(document.querySelector('.files-wrap .input.path')||{{}}).value.endsWith({json.dumps(dname)})", 45)
        await cdp.js(f"(()=>{{const tr=[...document.querySelectorAll('.files-table tbody tr')].find(r=>r.getAttribute('data-name')==={json.dumps(dname)});tr.querySelector('.file-more').click();}})()")
        await asyncio.sleep(0.3)
        await cdp.js("[...document.querySelectorAll('#context-menu .menu-item')].find(x=>x.textContent.trim()==='删除').click()")
        await asyncio.sleep(0.3)
        await cdp.js("document.querySelector('.modal .modal-foot .btn.danger').click()")
        gone = await cdp.wait_js(f"![...document.querySelectorAll('.files-table tbody tr')].some(r=>r.getAttribute('data-name')==={json.dumps(dname)})", 60)
        record(gone, "递归删除目录", dname)
    finally:
        try:
            target = base_dir if base_dir else (initial.rstrip(sep) + sep + dname)
            await cdp.js(
                f"(async()=>{{await api('/api/sessions/{sess}/files/remove',"
                f"{{method:'POST',body:{{path:{json.dumps(target)},recursive:true}}}});return true;}})()"
            )
        except Exception:
            pass
        await cdp.ws.close()

    passed = sum(1 for x in RESULTS if x[0])
    print(f"\n{passed}/{len(RESULTS)} PASS")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
