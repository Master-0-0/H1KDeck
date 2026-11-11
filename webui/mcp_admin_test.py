#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 管理页后端验收：status / start / stop / 503 gate / 持久化 / 令牌轮换。

使用独立临时端口、临时令牌文件与临时状态文件，不连接真实 teamserver，
也不会触碰运行中网关的 MCP 运行态。

仅用于已获得明确授权的安全测试环境。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import gateway  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  ({detail})" if detail and not ok else ""))
    return bool(ok)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def rpc(session: aiohttp.ClientSession, url: str, method: str,
              params: dict | None = None, token: str = "", session_id: str = "",
              msg_id: int = 1) -> tuple[int, dict, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    async with session.post(url, json=payload, headers=headers) as resp:
        try:
            body = await resp.json()
        except Exception:  # noqa: BLE001
            body = {"raw": await resp.text()}
        return resp.status, body, dict(resp.headers)


async def login(session: aiohttp.ClientSession, base: str, cfg: dict) -> bool:
    web_cfg = cfg["web"]
    async with session.post(base + "/api/login", json={
        "username": web_cfg.get("user", "admin"),
        "password": web_cfg.get("password", ""),
    }) as resp:
        return resp.status == 200


async def build_running_app(cfg: dict,
                            config_path: Path | None = None) -> tuple[web.AppRunner, str, int]:
    port = int(cfg["web"]["port"])
    app = gateway.build_app(cfg, config_path=config_path)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, f"http://127.0.0.1:{port}", port


async def test_admin(tmp: Path) -> None:
    print("\n[1] MCP 管理 API：status / stop 503 / start 恢复 / 审计")
    cfg = json.loads((BASE_DIR / "config.json").read_text("utf-8"))
    port = free_port()
    token_file = tmp / "mcp.token"
    state_file = tmp / "mcp_state.json"
    cfg["web"].update({"host": "127.0.0.1", "port": port, "tls_cert": "", "tls_key": ""})
    cfg["teamserver"]["port"] = 59999       # 不连接真实 teamserver
    cfg["python"] = {"enabled": False}
    cfg["mcp"] = {"enabled": True, "token": "", "token_file": str(token_file),
                  "state_file": str(state_file)}

    runner, base, _ = await build_running_app(cfg)
    token = token_file.read_text("utf-8").strip()
    check("自动生成 MCP 令牌文件", len(token) >= 16 and token_file.exists())
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            check("登录 Web 操作员", await login(client, base, cfg))

            async with client.get(base + "/api/mcp/status") as resp:
                status = await resp.json()
                check("GET /api/mcp/status 200", resp.status == 200, str(status)[:160])
            check("enabled/running", status.get("enabled") is True and status.get("running") is True)
            check("status=running", status.get("status") == "running")
            check("端点使用当前 host", status.get("endpoint") == f"{base}/mcp", status.get("endpoint", ""))
            check("传输 Streamable HTTP", status.get("transport") == "streamable-http")
            check("协议版本列表", "2025-06-18" in (status.get("protocols") or []))
            check("工具 69 / 资源 15 / 提示词 3",
                  (status.get("tool_count"), status.get("resource_count"), status.get("prompt_count")) == (69, 15, 3),
                  f"{status.get('tool_count')}/{status.get('resource_count')}/{status.get('prompt_count')}")
            check("令牌掩码且非明文",
                  "…" in (status.get("token_masked") or "")
                  and status.get("token_masked") != token)
            check("令牌文件路径", status.get("token_file") == str(token_file))
            check("客户端配置 http/stdio",
                  bool((status.get("client_config") or {}).get("http", {}).get("mcpServers"))
                  and bool((status.get("client_config") or {}).get("stdio", {}).get("mcpServers")))
            check("启动时间/运行时长存在",
                  bool(status.get("started_at_text")) and int(status.get("uptime") or 0) >= 0)
            check("状态不含完整令牌", "token" not in status or status.get("token") != token)

            status_code, body, headers = await rpc(
                client, base + "/mcp", "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "admin-test", "version": "1"}},
                token=token)
            check("停止前 initialize 200", status_code == 200 and "result" in body)
            session_id = headers.get("Mcp-Session-Id", "")
            check("initialize 返回 Mcp-Session-Id", bool(session_id))

            # 令牌只读 status；不能用令牌启停/读明文令牌。
            async with aiohttp.ClientSession() as token_only:
                async with token_only.get(base + "/api/mcp/status",
                                          headers={"Authorization": f"Bearer {token}"}) as resp:
                    check("MCP 令牌可读 status", resp.status == 200)
                async with token_only.post(base + "/api/mcp/stop",
                                           headers={"Authorization": f"Bearer {token}"}) as resp:
                    check("MCP 令牌不能 stop（401）", resp.status == 401)
                async with token_only.get(base + "/api/mcp/token",
                                          headers={"Authorization": f"Bearer {token}"}) as resp:
                    check("MCP 令牌不能读明文（401）", resp.status == 401)

            async with client.post(base + "/api/mcp/stop") as resp:
                stopped = await resp.json()
                check("POST /api/mcp/stop 200", resp.status == 200, str(stopped)[:160])
            check("stop 后 running=false", stopped.get("running") is False and stopped.get("status") == "stopped")
            check("状态文件已写 running=false",
                  state_file.exists()
                  and json.loads(state_file.read_text("utf-8")).get("running") is False)

            status_code, body, _ = await rpc(
                client, base + "/mcp", "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "after-stop", "version": "1"}},
                token=token)
            message = ((body.get("error") or {}).get("message") or "")
            check("停止后 POST /mcp 503", status_code == 503, f"HTTP {status_code}")
            check("503 JSON-RPC 明确“MCP 已停止”", "MCP 已停止" in message, message)

            async with client.get(base + "/mcp",
                                  headers={"Authorization": f"Bearer {token}"}) as resp:
                check("停止后 GET /mcp 503", resp.status == 503)
            async with client.delete(base + "/mcp",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Mcp-Session-Id": session_id}) as resp:
                check("停止后 DELETE /mcp 503", resp.status == 503)
            status_code, body, _ = await rpc(client, base + "/mcp", "ping", token=token,
                                             session_id=session_id)
            check("停止后旧会话 503", status_code == 503, f"HTTP {status_code}")

            # 浏览器 REST/WS 与投递链不依赖 MCP 运行态。
            async with client.get(base + "/api/state") as resp:
                check("停止 MCP 后 /api/state 仍 200", resp.status == 200)
            async with client.get(base + "/api/events?limit=5") as resp:
                check("停止 MCP 后 /api/events 仍 200", resp.status == 200)

            async with client.post(base + "/api/mcp/stop") as resp:
                check("重复 stop 返回 409 not_running", resp.status == 409
                      and (await resp.json()).get("error") == "not_running")

            async with client.post(base + "/api/mcp/start") as resp:
                started = await resp.json()
                check("POST /api/mcp/start 200", resp.status == 200, str(started)[:160])
            check("start 后 running=true", started.get("running") is True and started.get("status") == "running")
            check("状态文件已写 running=true",
                  json.loads(state_file.read_text("utf-8")).get("running") is True)

            async with client.post(base + "/api/mcp/start") as resp:
                check("重复 start 返回 409 already_running", resp.status == 409
                      and (await resp.json()).get("error") == "already_running")

            status_code, body, headers = await rpc(
                client, base + "/mcp", "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "after-start", "version": "1"}},
                token=token)
            check("start 后 initialize 200", status_code == 200 and "result" in body)
            new_session = headers.get("Mcp-Session-Id", "")
            status_code, body, _ = await rpc(client, base + "/mcp", "tools/list", token=token,
                                             session_id=new_session)
            tools = (body.get("result") or {}).get("tools") or []
            check("start 后 tools/list 仍 69", status_code == 200 and len(tools) == 69, str(len(tools)))

            async with client.get(base + "/api/mcp/info") as resp:
                info = await resp.json()
                check("兼容 /api/mcp/info 仍可用", resp.status == 200
                      and info.get("enabled") is True and info.get("tool_count") == 69)

            async with client.get(base + "/api/events?limit=200") as resp:
                events = (await resp.json()).get("events") or []
            texts = [e.get("text", "") for e in events if e.get("source") == "mcp"]
            check("审计事件包含启动记录", any("启动" in t for t in texts), str(texts[-3:]))
            check("审计事件包含停止记录", any("停止" in t for t in texts), str(texts[-3:]))

            # 令牌轮换：旧令牌 401，新令牌可用，旧会话失效。
            async with client.post(base + "/api/mcp/token/rotate") as resp:
                rotated = await resp.json()
                check("令牌轮换 200", resp.status == 200, str(rotated)[:160])
            new_token = rotated.get("token") or ""
            check("轮换返回新令牌", bool(new_token) and new_token != token)
            check("令牌文件已更新且 0600",
                  token_file.read_text("utf-8").strip() == new_token
                  and stat.S_IMODE(os.stat(token_file).st_mode) == 0o600)

            status_code, _, _ = await rpc(
                client, base + "/mcp", "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "old-token", "version": "1"}},
                token=token)
            check("旧令牌 initialize 401", status_code == 401, f"HTTP {status_code}")
            status_code, _, headers = await rpc(
                client, base + "/mcp", "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "new-token", "version": "1"}},
                token=new_token)
            check("新令牌 initialize 200", status_code == 200)
            check("轮换后旧会话失效", bool(new_session))
            status_code, _, _ = await rpc(client, base + "/mcp", "ping", token=new_token,
                                          session_id=new_session)
            check("轮换后旧 Session-Id 404", status_code == 404, f"HTTP {status_code}")

            async with client.get(base + "/api/mcp/token") as resp:
                plain = await resp.json()
                check("登录后可读取明文令牌（用于复制）", resp.status == 200
                      and plain.get("token") == new_token)
    finally:
        await runner.cleanup()

    print("\n[2] 持久化：重启后保持停止态，可再次启动")
    # 上一步最后运行中；先停止并确认状态文件，再重建应用。
    app2_cfg = dict(cfg)
    app2_cfg["mcp"] = dict(cfg["mcp"])
    app2_cfg["web"] = dict(cfg["web"])
    app2_cfg["web"]["port"] = port
    runner2, base2, _ = await build_running_app(app2_cfg)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client2:
            check("重建应用后登录", await login(client2, base2, app2_cfg))
            async with client2.get(base2 + "/api/mcp/status") as resp:
                st = await resp.json()
                check("重启后恢复上次运行态（running）", resp.status == 200 and st.get("running") is True)
            async with client2.post(base2 + "/api/mcp/stop") as resp:
                check("重启后停止 200", resp.status == 200)
        await runner2.cleanup()

        runner3, base3, _ = await build_running_app(app2_cfg)
        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client3:
                check("再次登录", await login(client3, base3, app2_cfg))
                async with client3.get(base3 + "/api/mcp/status") as resp:
                    st = await resp.json()
                    check("持久化停止态：restart 后 running=false",
                          resp.status == 200 and st.get("running") is False and st.get("status") == "stopped")
                status_code, body, _ = await rpc(
                    client3, base3 + "/mcp", "initialize",
                    {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "persisted-stop", "version": "1"}},
                    token=new_token)
                check("持久化停止态：/mcp 503", status_code == 503)
                async with client3.post(base3 + "/api/mcp/start") as resp:
                    check("持久化停止态可再次启动", resp.status == 200)
                async with client3.get(base3 + "/api/mcp/status") as resp:
                    st = await resp.json()
                    check("启动后状态 running", st.get("running") is True)
        finally:
            await runner3.cleanup()
    finally:
        pass


async def test_port(tmp: Path) -> None:
    print("\n[4] MCP 独立端口：页面修改 / 绑定 / 回退 / 持久化")
    cfg = json.loads((BASE_DIR / "config.json").read_text("utf-8"))
    web_port = free_port()
    token_file = tmp / "port.token"
    state_file = tmp / "port_state.json"
    config_path = tmp / "port_config.json"
    cfg["web"].update({"host": "127.0.0.1", "port": web_port,
                       "tls_cert": "", "tls_key": ""})
    cfg["teamserver"]["port"] = 59999
    cfg["python"] = {"enabled": False}
    cfg["mcp"] = {"enabled": True, "token": "", "token_file": str(token_file),
                  "state_file": str(state_file), "port": 0}
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    token = ""

    runner, base, _ = await build_running_app(cfg, config_path)
    token = token_file.read_text("utf-8").strip()
    try:
        async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            check("端口测试登录", await login(client, base, cfg))
            async with client.get(base + "/api/mcp/status") as resp:
                st = await resp.json()
            check("默认端口 0=共享 WebUI 端口",
                  st.get("port") == 0 and st.get("port_mode") == "shared"
                  and st.get("web_port") == web_port
                  and st.get("endpoint") == f"{base}/mcp")

            new_port = free_port()
            async with client.post(base + "/api/mcp/port",
                                   json={"port": new_port}) as resp:
                st = await resp.json()
                check("修改为独立端口 200", resp.status == 200
                      and st.get("port") == new_port
                      and st.get("port_mode") == "dedicated"
                      and st.get("listener_active") is True, str(st)[:200])
            check("端点切换到新端口",
                  st.get("endpoint") == f"http://127.0.0.1:{new_port}/mcp")
            check("端口写入 config.json（持久化）",
                  json.loads(config_path.read_text("utf-8"))["mcp"]["port"] == new_port)

            async with client.get(base + "/api/mcp/info") as resp:
                info = await resp.json()
                check("info.endpoint_url 使用独立端口",
                      info.get("endpoint_url") == f"http://127.0.0.1:{new_port}/mcp")

            status_code, body, _ = await rpc(
                client, base + "/mcp", "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "shared-after-move", "version": "1"}},
                token=token)
            check("独立端口启用后 WebUI /mcp 404 moved", status_code == 404
                  and (body.get("error") == "moved"), str(body)[:160])

            dedicated = f"http://127.0.0.1:{new_port}/mcp"
            status_code, body, headers = await rpc(
                client, dedicated, "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "dedicated", "version": "1"}},
                token=token)
            check("独立端口 initialize 200", status_code == 200 and "result" in body)
            sid = headers.get("Mcp-Session-Id", "")
            status_code, body, _ = await rpc(client, dedicated, "tools/list",
                                             token=token, session_id=sid)
            check("独立端口 tools/list 69",
                  status_code == 200 and len((body.get("result") or {}).get("tools") or []) == 69)

            async with client.post(base + "/api/mcp/stop") as resp:
                check("停止 MCP 200", resp.status == 200)
            status_code, body, _ = await rpc(client, dedicated, "ping", token=token)
            check("独立端口停止后 503", status_code == 503
                  and "MCP 已停止" in ((body.get("error") or {}).get("message") or ""))
            async with client.post(base + "/api/mcp/start") as resp:
                check("重新启动 MCP 200", resp.status == 200)
            status_code, _, _ = await rpc(client, dedicated, "ping", token=token)
            check("独立端口启动后恢复 200", status_code == 200)

            for bad in (-1, 65536, "abc", web_port, True):
                async with client.post(base + "/api/mcp/port",
                                       json={"port": bad}) as resp:
                    check(f"非法端口 {bad!r} 被拒绝", resp.status == 400)

            conflict = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            conflict.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            conflict.bind(("127.0.0.1", 0))
            conflict.listen(1)
            conflict_port = conflict.getsockname()[1]
            try:
                async with client.post(base + "/api/mcp/port",
                                       json={"port": conflict_port}) as resp:
                    body = await resp.json()
                    check("端口被占用返回 409", resp.status == 409
                          and body.get("error") == "port_unavailable")
            finally:
                conflict.close()
            async with client.get(base + "/api/mcp/status") as resp:
                st = await resp.json()
            check("绑定失败保持原端口", st.get("port") == new_port)
            status_code, _, _ = await rpc(client, dedicated, "ping", token=token)
            check("绑定失败后原独立端口仍可用", status_code == 200)

            async with client.get(base + "/api/events?limit=100") as resp:
                events = (await resp.json()).get("events") or []
            check("端口修改写审计",
                  any("监听端口" in e.get("text", "") for e in events
                      if e.get("source") == "mcp"))

            async with client.post(base + "/api/mcp/port", json={"port": 0}) as resp:
                st = await resp.json()
                check("恢复 0=共享端口 200", resp.status == 200
                      and st.get("port") == 0 and st.get("port_mode") == "shared")
            status_code, _, _ = await rpc(
                client, base + "/mcp", "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "shared-restored", "version": "1"}},
                token=token)
            check("恢复后 WebUI /mcp 200", status_code == 200)
            refused = False
            try:
                async with client.post(dedicated, json={"jsonrpc": "2.0", "id": 1,
                                                        "method": "ping"},
                                       headers={"Authorization": f"Bearer {token}"}) as resp:
                    refused = False
            except aiohttp.ClientError:
                refused = True
            check("恢复后独立端口已关闭", refused)

            # 再切到独立端口，验证重启后从 config.json 恢复。
            async with client.post(base + "/api/mcp/port",
                                   json={"port": new_port}) as resp:
                check("再次切到独立端口", resp.status == 200)

        async with aiohttp.ClientSession() as token_only:
            async with token_only.post(base + "/api/mcp/port",
                                       json={"port": 0},
                                       headers={"Authorization": f"Bearer {token}"}) as resp:
                check("MCP 令牌不能改端口（401）", resp.status == 401)
    finally:
        await runner.cleanup()

    # 重新读取 config.json 构建应用，验证 mcp.port 在重启后生效。
    cfg2 = json.loads(config_path.read_text("utf-8"))
    runner2, base2, _ = await build_running_app(cfg2, config_path)
    try:
        async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)) as client2:
            check("端口持久化后登录", await login(client2, base2, cfg2))
            async with client2.get(base2 + "/api/mcp/status") as resp:
                st = await resp.json()
            check("重启后仍使用独立端口",
                  st.get("port") == new_port and st.get("listener_active") is True)
            status_code, _, _ = await rpc(
                client2, f"http://127.0.0.1:{new_port}/mcp", "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "restored-port", "version": "1"}},
                token=token)
            check("重启后独立端口 initialize 200", status_code == 200)
            status_code, _, _ = await rpc(
                client2, base2 + "/mcp", "ping", token=token)
            check("重启后 WebUI /mcp 仍 404 moved", status_code == 404)
            async with client2.post(base2 + "/api/mcp/port", json={"port": 0}) as resp:
                check("测试结束恢复共享端口", resp.status == 200)
    finally:
        await runner2.cleanup()


async def test_disabled(tmp: Path) -> None:
    print("\n[3] 配置禁用：页面可见，启动被拒绝，/mcp 404")
    cfg = json.loads((BASE_DIR / "config.json").read_text("utf-8"))
    port = free_port()
    cfg["web"].update({"host": "127.0.0.1", "port": port, "tls_cert": "", "tls_key": ""})
    cfg["teamserver"]["port"] = 59999
    cfg["python"] = {"enabled": False}
    cfg["mcp"] = {"enabled": False, "token": "", "token_file": str(tmp / "disabled.token"),
                  "state_file": str(tmp / "disabled_state.json")}
    runner, base, _ = await build_running_app(cfg)
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            check("禁用时登录", await login(client, base, cfg))
            async with client.get(base + "/api/mcp/status") as resp:
                st = await resp.json()
                check("禁用时 status 仍可读", resp.status == 200)
            check("status=disabled", st.get("enabled") is False and st.get("running") is False
                  and st.get("status") == "disabled")
            async with client.post(base + "/api/mcp/start") as resp:
                body = await resp.json()
                check("禁用时 start 409 disabled", resp.status == 409 and body.get("error") == "disabled")
            status_code, _, _ = await rpc(
                client, base + "/mcp", "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {},
                 "clientInfo": {"name": "disabled", "version": "1"}},
                token="anything")
            check("禁用时 /mcp 404（路由未挂载）", status_code == 404, f"HTTP {status_code}")
    finally:
        await runner.cleanup()


async def main() -> int:
    print("=" * 60)
    print("Havoc WebUI MCP 管理页验收")
    print("=" * 60)
    tmp = Path(tempfile.mkdtemp(prefix="havoc-mcp-admin-"))
    try:
        await test_admin(tmp)
        await test_disabled(tmp)
        await test_port(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("\n" + "=" * 60)
    print(f"通过 {passed} / {total}")
    if passed != total:
        for name, ok, detail in CHECKS:
            if not ok:
                print(f"  [FAIL] {name}  {detail}")
        return 1
    print("MCP 管理页验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
