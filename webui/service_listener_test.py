#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Service/custom listener 验收（P0）。

覆盖：
  1. 启动最小服务端插件客户端，注册自定义监听器协议（EV_SERVICE/SubEvent=2）
  2. Web /api/state 能看到协议（service_listeners）
  3. Web API 创建插件监听器 → 插件真正绑定端口 → teamserver 广播 Listener.Add
  4. Web UI 协议下拉动态出现、按 Items 生成表单、可创建
  5. 编辑弹窗：协议禁用、editable=false 字段只读、保存被拦截（teamserver 不支持插件编辑）
  6. Web UI / API 删除插件监听器，列表恢复

前置：teamserver + webui 已启动；端口 9222 的 Chromium（UI 部分，可用 --no-ui 跳过）。
仅用于已获得明确书面授权的测试环境。
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
from typing import Optional

import aiohttp

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ui_screenshot import CDP  # noqa: E402

BASE = "http://127.0.0.1:8081"
CDP_BASE = "http://127.0.0.1:9222"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# ------------------------------------------------------------------ 工具

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_open(port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def service_password() -> str:
    profile = HERE.parent / "havoc-src" / "profiles" / "havoc.yaotl"
    try:
        text = profile.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"Service\s*\{[^}]*Password\s*=\s*\"([^\"]+)\"", text, re.S)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "service-password"


def teamserver_url() -> str:
    host, port = "127.0.0.1", 40056
    try:
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
        ts = cfg.get("teamserver", {})
        host = ts.get("host", host)
        port = int(ts.get("port", port))
    except Exception:
        pass
    return f"wss://{host}:{port}/service-endpoint"


async def api(session: aiohttp.ClientSession, method: str, path: str, **kwargs):
    async with session.request(method, BASE + path, **kwargs) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = None
        return resp.status, data


# ------------------------------------------------------------------ 服务插件

class Plugin:
    def __init__(self, protocol: str, log_path: pathlib.Path):
        self.protocol = protocol
        self.log_path = log_path
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("wb")
        self._log = log
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "tools" / "service_listener_demo.py"),
             "--url", teamserver_url(),
             "--password", service_password(),
             "--name", self.protocol],
            stdout=log, stderr=subprocess.STDOUT,
        )

    def stop(self) -> None:
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            self._log.close()
        except Exception:
            pass

    def log_tail(self, limit: int = 1200) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except Exception:
            return ""


# ------------------------------------------------------------------ UI

async def ui_checks(protocol: str, name: str, port: int):
    async with aiohttp.ClientSession() as session:
        async with session.get(CDP_BASE + "/json") as resp:
            targets = await resp.json()
    page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        return check("UI：找到 Chromium 页面", False, "没有 page target")
    check("UI：找到 Chromium 页面", True, page.get("url", ""))

    cdp = CDP(page["webSocketDebuggerUrl"])
    await cdp.connect()
    try:
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        await cdp.call("Page.reload", {"ignoreCache": True})
        await asyncio.sleep(3.0)
        check("UI：保持登录", bool(await cdp.eval("!!S.user")))
        defs = await cdp.eval("JSON.stringify(Array.from(S.serviceListeners.keys()))")
        check("UI：快照包含插件协议", protocol in (defs or ""), defs or "")

        await cdp.eval("openTab('listeners', {kind:'listeners', title:'监听器'})")
        await asyncio.sleep(0.8)
        await cdp.eval("openListenerModal(null)")
        await asyncio.sleep(0.4)
        await cdp.eval(
            "(() => { const s=document.querySelector('#modal-root [name=\"mode\"]');"
            f" s.value='svc:{protocol}'; s.dispatchEvent(new Event('change')); }})()"
        )
        await asyncio.sleep(0.3)
        fields = await cdp.eval(
            "JSON.stringify(Array.from(document.querySelectorAll('#modal-root [data-service-field]'))"
            ".map(n=>[n.dataset.serviceField,n.required]))"
        )
        field_data = json.loads(fields or "[]")
        field_names = [item[0] for item in field_data]
        check("UI：协议下拉生成插件字段", "Host" in field_names and "PortBind" in field_names, fields or "")
        desc = await cdp.eval("document.querySelector('#modal-root .mode-desc')?.textContent || ''")
        check("UI：插件模式说明", protocol in (desc or ""), (desc or "")[:80])

        ui_name = f"Svc-UI-{os.getpid()}"
        await cdp.eval(
            "(() => {"
            f" document.querySelector('#modal-root [name=\"name\"]').value={json.dumps(ui_name)};"
            " const f=Array.from(document.querySelectorAll('#modal-root [data-service-field]'));"
            " f.find(n=>n.dataset.serviceField==='Host').value='127.0.0.1';"
            f" f.find(n=>n.dataset.serviceField==='PortBind').value='{port}';"
            " const t=f.find(n=>n.dataset.serviceField==='Token'); if(t) t.value='ui-token';"
            " [...document.querySelectorAll('#modal-root .modal-foot button')]"
            ".find(b=>b.textContent==='创建').click();"
            "})()"
        )
        created = False
        for _ in range(30):
            await asyncio.sleep(0.3)
            if await cdp.eval(f"!!S.listeners.get({json.dumps(ui_name)})"):
                created = True
                break
        check("UI：创建插件监听器", created, ui_name)
        if created:
            info = await cdp.eval(f"JSON.stringify(S.listeners.get({json.dumps(ui_name)}))")
            check("UI：监听器协议/服务标记", protocol in (info or "") and '"service":true' in (info or ""),
                  (info or "")[:160])

        # 编辑弹窗语义
        await cdp.eval(f"openListenerModal(S.listeners.get({json.dumps(ui_name)}))")
        await asyncio.sleep(0.4)
        disabled = await cdp.eval("document.querySelector('#modal-root [name=\"mode\"]')?.disabled")
        buttons = await cdp.eval(
            "JSON.stringify([...document.querySelectorAll('#modal-root .modal-foot button')].map(b=>b.textContent))"
        )
        token_ro = await cdp.eval(
            "(() => { const n=[...document.querySelectorAll('#modal-root [data-service-field]')]"
            ".find(x=>x.dataset.serviceField==='Token'); return n ? n.readOnly : null; })()"
        )
        note_hidden = await cdp.eval(
            "document.querySelector('#modal-root .service-edit-note')?.classList.contains('hidden')"
        )
        check("UI：编辑时协议禁用", disabled is True)
        check("UI：editable=false 字段只读", token_ro is True)
        check("UI：编辑保存被拦截", buttons is not None and "关闭" in buttons and "保存" not in buttons,
              buttons or "")
        check("UI：显示不可编辑说明", note_hidden is False)
        await cdp.eval("closeModal()")

        # UI 删除
        await cdp.eval(
            "(() => { const rows=[...document.querySelectorAll('.listener-table tbody tr')];"
            f" const row=rows.find(r=>r.dataset.name==={json.dumps(ui_name)});"
            " if(row){ row.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
            " document.querySelector('.del-btn').click(); } })()"
        )
        await asyncio.sleep(0.5)
        await cdp.eval(
            "(() => { const b=[...document.querySelectorAll('#modal-root .modal-foot button')]"
            ".find(x=>x.textContent==='确定'); if(b) b.click(); })()"
        )
        removed = False
        for _ in range(20):
            await asyncio.sleep(0.3)
            if not await cdp.eval(f"!!S.listeners.get({json.dumps(ui_name)})"):
                removed = True
                break
        check("UI：删除插件监听器", removed, ui_name)
    finally:
        try:
            await cdp.ws.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 主流程

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ui", action="store_true", help="跳过 Chromium UI 检查")
    args = ap.parse_args()

    protocol = f"Svc-Test-{os.getpid()}"
    name = f"Svc-Api-{os.getpid()}"
    port = free_port()
    ui_port = free_port()
    plugin = Plugin(protocol, HERE.parent / "logs" / f"service-{protocol}.log")
    plugin.start()
    atexit.register(plugin.stop)

    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        status, _ = await api(session, "POST", "/api/login",
                              json={"username": "admin", "password": "havoc1234"})
        if not check("Web 登录", status == 200, f"status={status}"):
            plugin.stop()
            return 1

        # 1. 插件注册 → state 可见
        found = False
        deadline = time.time() + 15
        while time.time() < deadline:
            _, data = await api(session, "GET", "/api/service-listeners")
            names = [x.get("name") for x in (data or {}).get("listeners", [])]
            if protocol in names:
                found = True
                break
            await asyncio.sleep(0.4)
        check("服务插件注册协议可见", found, protocol)
        if not found:
            print(plugin.log_tail())
            plugin.stop()
            return 1

        _, info = await api(session, "GET", "/api/service-listeners")
        definition = next(x for x in info["listeners"] if x["name"] == protocol)
        fields = [x["name"] for x in definition.get("items", [])]
        check("协议字段来自 ListenerAdd", fields == ["Host", "PortBind", "Token"], ",".join(fields))

        # 2. API 创建
        started = time.time()
        status, data = await api(session, "POST", "/api/listeners", json={
            "name": name, "protocol": protocol,
            "Host": "127.0.0.1", "PortBind": str(port), "Token": "api-token",
        })
        elapsed = time.time() - started
        check("API 创建插件监听器", status == 200, f"status={status} data={data} {elapsed:.2f}s")
        check("API 创建耗时 < 2.5s 回执", elapsed < 2.5, f"{elapsed:.2f}s")
        _, listeners = await api(session, "GET", "/api/listeners")
        item = next((l for l in listeners.get("listeners", []) if l["name"] == name), None)
        check("监听器列表包含新监听器",
              bool(item) and item.get("protocol") == protocol
              and str(item.get("port_bind")) == str(port),
              json.dumps(item, ensure_ascii=False)[:200] if item else "not found")
        check("插件已实际绑定端口", port_open(port), f"127.0.0.1:{port}")

        # 3. 必填校验
        status, _ = await api(session, "POST", "/api/listeners", json={
            "name": name + "-bad", "protocol": protocol, "Host": "127.0.0.1",
        })
        check("必填字段校验", status == 400, f"status={status}")

        # 4. 编辑拦截
        status, data = await api(session, "PUT", f"/api/listeners/{name}", json={
            "name": name, "protocol": protocol, "Host": "127.0.0.1",
            "PortBind": str(port), "Token": "changed",
        })
        check("插件监听器编辑被明确拦截", status == 400
              and "不支持在线编辑" in json.dumps(data, ensure_ascii=False), f"status={status}")

        # 5. UI
        if not args.no_ui:
            try:
                await ui_checks(protocol, name, ui_port)
            except Exception as exc:
                check("UI 检查执行", False, repr(exc))

        # 6. API 删除
        status, _ = await api(session, "DELETE", f"/api/listeners/{name}")
        check("API 删除插件监听器", status == 200, f"status={status}")
        gone = False
        for _ in range(20):
            await asyncio.sleep(0.25)
            _, listeners = await api(session, "GET", "/api/listeners")
            if not any(l["name"] == name for l in listeners.get("listeners", [])):
                gone = True
                break
        check("删除后列表移除", gone, name)

        # 7. LAN-Test / 会话未受影响
        _, listeners = await api(session, "GET", "/api/listeners")
        names = [l["name"] for l in listeners.get("listeners", [])]
        check("LAN-Test 未被破坏", "LAN-Test" in names, ",".join(names))
        _, state = await api(session, "GET", "/api/state")
        healthy = state.get("counts", {}).get("healthy", 0)
        check("现有 healthy 会话仍在", healthy >= 1, f"healthy={healthy}")

    plugin.stop()
    await asyncio.sleep(0.3)
    print("\n插件日志尾部：\n" + plugin.log_tail(1600))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n===== {passed}/{total} PASS =====")
    if passed != total:
        print("失败项：")
        for name_, ok, detail in RESULTS:
            if not ok:
                print(f" - {name_} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
