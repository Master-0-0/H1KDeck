#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断线/重连 UX 验收：停网关 → 界面失去连接 → 重启网关 → 自动恢复。

UI 复刻 Qt 客户端时去掉了底部状态栏（`app.js:updateConn()` 只更新用户名），
所以这里不再断言 `#conn-text` / `#conn-pill` / `#status-ws` / `#status-conn` 这类
从未存在过的 DOM。改用真实可观测的信号：

  * `fetch('/api/health')`     网关进程是否可达（断线期间必须失败）
  * `fetch('/api/me')`         cookie/登录态是否仍有效（断线不应该被踢回登录页）
  * `#sessions-body tr` 行数    会话表是否恢复
  * `#login-view` 是否隐藏      是否弹回登录页
  * `window.__disconnectProbe`  页面未被手动刷新（说明是 WS 自动重连）
  * `/api/status.ws_clients`    浏览器 WS 已重新连上网关

用法：
    cd webui && python3 disconnect_test.py        # 需要 CDP :9222
"""
import asyncio
import json
import pathlib
import subprocess
import sys

import aiohttp

from ui_screenshot import CDP

BASE = "http://127.0.0.1:8081"
HERE = pathlib.Path(__file__).resolve().parent
MIN_SESSIONS = 6

STATE_JS = """(async () => {
    const out = {
        health: null,
        me: null,
        me_user: "",
        sessions: document.querySelectorAll('#sessions-body tr').length,
        loginVisible: (() => { const el = document.querySelector('#login-view');
            return el ? !el.classList.contains('hidden') : null; })(),
        view: document.querySelector('.view.active')?.id || null,
    };
    try {
        const r = await fetch('/api/health', {cache: 'no-store'});
        out.health = r.ok ? ((await r.json()).ok === true) : `HTTP ${r.status}`;
    } catch (e) { out.health = `error: ${e.message}`; }
    try {
        const r = await fetch('/api/me', {cache: 'no-store'});
        out.me = r.status;
        if (r.ok) out.me_user = (await r.json()).user || "";
    } catch (e) { out.me = `error: ${e.message}`; }
    return out;
})()"""

WS_CLIENTS_JS = """(async () => {
    try {
        const r = await fetch('/api/status', {cache: 'no-store'});
        if (!r.ok) return null;
        const d = await r.json();
        return d.ws_clients ?? null;
    } catch (e) { return null; }
})()"""


async def state(cdp, label):
    value = await cdp.eval(STATE_JS)
    value = value if isinstance(value, dict) else {}
    print(label, json.dumps(value, ensure_ascii=False))
    return value


async def main() -> int:
    out = HERE.parent / "logs" / "webui-shots"
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

    before = await state(cdp, "connected:")
    probe = await cdp.eval(
        "(() => { window.__disconnectProbe = (window.__disconnectProbe || 0) + 1;"
        " return window.__disconnectProbe; })()")
    print("page probe:", probe)
    await cdp.shot(out / "07-before-drop.png")

    print("stopping gateway ...")
    subprocess.run(["bash", str(HERE / "stop-webui.sh")], check=False,
                   stdout=subprocess.DEVNULL)
    await asyncio.sleep(4.0)
    dropped = await state(cdp, "stopped:")
    await cdp.shot(out / "08-disconnected.png")

    print("starting gateway ...")
    subprocess.run(["bash", str(HERE / "start-webui.sh")], check=False,
                   stdout=subprocess.DEVNULL)

    final: dict = {}
    for attempt in range(1, 26):
        await asyncio.sleep(1.0)
        final = await state(cdp, f"restarted[{attempt}]:")
        if (final.get("health") is True and final.get("me") == 200
                and (final.get("sessions") or 0) >= MIN_SESSIONS
                and final.get("loginVisible") is False):
            break
    await cdp.shot(out / "09-reconnected.png")

    probe_after = await cdp.eval("window.__disconnectProbe")
    ws_clients = await cdp.eval(WS_CLIENTS_JS)
    print("page probe after:", probe_after, "ws_clients:", ws_clients)
    await cdp.ws.close()

    checks = {
        "connected: 网关 /api/health 正常": before.get("health") is True,
        "connected: 登录态有效（/api/me 200）": before.get("me") == 200,
        f"connected: 会话表 >= {MIN_SESSIONS} 行": (before.get("sessions") or 0) >= MIN_SESSIONS,
        "connected: 未显示登录页": before.get("loginVisible") is False,
        "stopped: /api/health 不可用": dropped.get("health") is not True,
        "stopped: /api/me 不可用": dropped.get("me") != 200,
        "stopped: 未弹回登录页（cookie 仍有效）": dropped.get("loginVisible") is False,
        "restarted: 自动恢复 /api/health": final.get("health") is True,
        "restarted: /api/me 200 且仍是原用户":
            final.get("me") == 200 and bool(final.get("me_user")),
        f"restarted: 会话表恢复 >= {MIN_SESSIONS} 行":
            (final.get("sessions") or 0) >= MIN_SESSIONS,
        "restarted: 未弹回登录页": final.get("loginVisible") is False,
        "restarted: 页面未刷新（WS 自动重连）": probe_after == probe,
        "restarted: 浏览器 WS 已重新连上": (ws_clients or 0) >= 1,
    }
    for name, ok in checks.items():
        print(("  [PASS] " if ok else "  [FAIL] ") + name)
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
