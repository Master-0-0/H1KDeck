#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 层验收：协议、工具、资源、提示词、鉴权、stdio 与网关挂载。

默认跑离线用例（内置一个假网关 + 独立 MCP HTTP 服务 + stdio 子进程），
`--live` 追加对正在运行的 webui（`http://127.0.0.1:8081`）的只读检查。

仅用于已获得明确授权的安全测试环境。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import socket
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import gateway  # noqa: E402
import mcp_server  # noqa: E402
import mcp_tools  # noqa: E402

TOKEN = "mcp-test-token-0123456789abcdef"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  ({detail})" if detail and not ok else ""))
    return bool(ok)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------- 假网关

def build_fake_gateway(requests: list, uploads: list) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    task_polls: dict[str, int] = {}

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path == "/api/health":
            return await handler(request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": "unauthorized", "message": "请先登录"}, status=401)
        return await handler(request)

    app.middlewares.append(auth)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "time": "2026-01-01 00:00:00"})

    async def state(request: web.Request) -> web.Response:
        return web.json_response({
            "type": "snapshot",
            "connection": {"state": "connected", "user": "Neo"},
            "sessions": [{"id": "demo01", "user": "admin", "host": "WIN-TEST"}],
            "listeners": [{"name": "LAN-Test", "protocol": "Http"}],
            "counts": {"sessions": 1, "listeners": 1},
        })

    async def sessions(request: web.Request) -> web.Response:
        return web.json_response({"sessions": [
            {"id": "demo01", "user": "admin", "host": "WIN-TEST", "health": "healthy"},
            {"id": "demo02", "user": "root", "host": "linux-box", "health": "healthy"},
        ]})

    async def session(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        requests.append(("GET", request.path, dict(request.query), None))
        return web.json_response({"session": {"id": sid, "user": "admin"},
                                  "tasks": [{"task_id": "TASK-0", "completed": True, "output": "ok"}]})

    async def tasks(request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        task_id = request.query.get("task_id") or "TASK-1"
        count = task_polls.get(task_id, 0) + 1
        task_polls[task_id] = count
        completed = count >= 2
        return web.json_response({"tasks": [{
            "task_id": task_id,
            "demon": sid,
            "command": "shell whoami",
            "status": "done" if completed else "sent",
            "completed": completed,
            "output": "admin\\administrator\r\n" if completed else "",
        }]})

    async def command(request: web.Request) -> web.Response:
        body = await request.json()
        requests.append(("POST", request.path, dict(request.query), body))
        return web.json_response({"ok": True, "task": {
            "task_id": "TASK-1", "command": body.get("command"), "status": "queued",
            "completed": False, "output": "",
        }})

    async def files(request: web.Request) -> web.Response:
        body = await request.json()
        requests.append(("POST", request.path, dict(request.query), body))
        return web.json_response({"ok": True, "path": body.get("path") or ".",
                                  "entries": [{"name": "flag.txt", "dir": False, "size": 12}]})

    async def download(request: web.Request) -> web.Response:
        requests.append(("GET", request.path, dict(request.query), None))
        return web.Response(
            body=b"hello-binary-payload",
            content_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="secret.txt"'},
        )

    async def upload(request: web.Request) -> web.Response:
        fields: dict = {}
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            data = await part.read()
            fields[part.name] = {"filename": part.filename, "bytes": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest()}
        uploads.append(fields)
        return web.json_response({"ok": True, "uploaded": fields.get("file")})

    async def loot(request: web.Request) -> web.Response:
        return web.Response(body=b"loot-bytes", content_type="application/octet-stream",
                            headers={"Content-Disposition": 'attachment; filename="loot.png"'})

    async def help_(request: web.Request) -> web.Response:
        return web.json_response({"help": f"help:{request.query.get('command', '')}"})

    async def mcp_info(request: web.Request) -> web.Response:
        return web.json_response({"enabled": True, "tool_count": 69})

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get("/api/sessions/{sid}", session)
    app.router.add_get("/api/sessions/{sid}/tasks", tasks)
    app.router.add_post("/api/sessions/{sid}/command", command)
    app.router.add_post("/api/sessions/{sid}/files", files)
    app.router.add_get("/api/sessions/{sid}/files/download", download)
    app.router.add_post("/api/sessions/{sid}/files/upload", upload)
    app.router.add_get("/api/loot/{lid}", loot)
    app.router.add_get("/api/help", help_)
    app.router.add_get("/api/mcp/info", mcp_info)
    return app


# ---------------------------------------------------------------- 测试用例

def test_registry() -> None:
    print("\n[1] 注册表")
    registry = mcp_tools.build_registry()
    check("工具数量 >= 60", len(registry.tools) >= 60, str(len(registry.tools)))
    check("工具名唯一", len({t.name for t in registry.tools}) == len(registry.tools))
    check("工具名以 havoc_ 开头", all(t.name.startswith("havoc_") for t in registry.tools))
    check("工具输入 schema 均为 object",
          all(t.input_schema.get("type") == "object" for t in registry.tools))
    required = {"havoc_state", "havoc_run_command", "havoc_files_list", "havoc_files_download",
                "havoc_files_upload", "havoc_add_listener", "havoc_build_payload",
                "havoc_install_extension", "havoc_python_repl", "havoc_upload_script"}
    check("关键工具齐全", required.issubset(registry.tools_by_name), str(sorted(required)))
    check("资源 >= 10", len(registry.resources) >= 10, str(len(registry.resources)))
    check("提示词 >= 3", len(registry.prompts) >= 3)
    for tool in registry.tools:
        props = tool.input_schema.get("properties", {})
        for req in tool.input_schema.get("required", []):
            if req not in props:
                check(f"{tool.name} required 字段存在", False, req)


async def rpc(session: aiohttp.ClientSession, url: str, method: str, params: dict | None = None,
              token: str = TOKEN, session_id: str | None = None,
              msg_id: int = 1) -> tuple[int, dict, dict]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    async with session.post(url, json=payload, headers=headers) as resp:
        try:
            body = await resp.json()
        except Exception:  # noqa: BLE001
            body = {"raw": await resp.text()}
        return resp.status, body, dict(resp.headers)


async def call_tool(session, url: str, name: str, arguments: dict | None = None,
                    session_id: str | None = None, token: str = TOKEN) -> dict:
    status, body, _ = await rpc(session, url, "tools/call",
                                {"name": name, "arguments": arguments or {}},
                                token=token, session_id=session_id)
    assert status == 200, body
    assert "result" in body, body
    return body["result"]


async def test_http(fake_url: str, workdir: Path) -> None:
    print("\n[2] 独立 HTTP 传输（Streamable HTTP）")
    bridge = mcp_server.MCPBridge(fake_url, TOKEN)
    port = free_port()
    app = mcp_server.build_http_app(bridge, TOKEN)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as client:
            status, body, headers = await rpc(client, base + "/mcp", "initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp_test", "version": "1"},
            })
            check("initialize 200", status == 200, str(status))
            result = body.get("result") or {}
            check("协议版本协商", result.get("protocolVersion") == "2025-06-18",
                  str(result.get("protocolVersion")))
            check("tools 能力", "tools" in (result.get("capabilities") or {}))
            session_id = headers.get("Mcp-Session-Id")
            check("返回 Mcp-Session-Id", bool(session_id))

            status, body, _ = await rpc(client, base + "/mcp", "tools/list", {}, session_id=session_id)
            tools = (body.get("result") or {}).get("tools") or []
            check("tools/list 数量", len(tools) >= 60, str(len(tools)))
            check("tools/list 含 details",
                  all("inputSchema" in t and "description" in t for t in tools))

            health = await call_tool(client, base + "/mcp", "havoc_health", session_id=session_id)
            check("havoc_health structuredContent",
                  (health.get("structuredContent") or {}).get("ok") is True,
                  json.dumps(health, ensure_ascii=False)[:200])

            listing = await call_tool(client, base + "/mcp", "havoc_files_list",
                                      {"sid": "demo01", "path": r"C:\Windows"}, session_id=session_id)
            check("havoc_files_list entries",
                  len((listing.get("structuredContent") or {}).get("entries") or []) == 1)

            saved = workdir / "download.bin"
            dl = await call_tool(client, base + "/mcp", "havoc_files_download",
                                 {"sid": "demo01", "path": r"C:\secret.txt", "save_to": str(saved)},
                                 session_id=session_id)
            meta = dl.get("structuredContent") or {}
            check("下载落盘", saved.is_file() and saved.read_bytes() == b"hello-binary-payload")
            check("下载 sha256", meta.get("sha256") == hashlib.sha256(b"hello-binary-payload").hexdigest())

            local = workdir / "upload.txt"
            local.write_bytes(b"upload-payload")
            up = await call_tool(client, base + "/mcp", "havoc_files_upload",
                                 {"sid": "demo01", "path": ".", "local_file": str(local)},
                                 session_id=session_id)
            check("上传返回 uploaded", bool((up.get("structuredContent") or {}).get("uploaded")))

            run = await call_tool(client, base + "/mcp", "havoc_run_command",
                                  {"sid": "demo01", "command": "shell whoami", "wait_seconds": 5,
                                   "settle_seconds": 0}, session_id=session_id)
            run_meta = run.get("structuredContent") or {}
            check("命令等待完成", run_meta.get("completed") is True)
            check("命令输出", "administrator" in (run_meta.get("output") or ""))

            status, body, _ = await rpc(client, base + "/mcp", "tools/call",
                                        {"name": "havoc_nope", "arguments": {}}, session_id=session_id)
            check("未知工具返回 -32602", ((body.get("error") or {}).get("code") == -32602),
                  json.dumps(body, ensure_ascii=False)[:200])

            status, body, _ = await rpc(client, base + "/mcp", "resources/list", {}, session_id=session_id)
            resources = (body.get("result") or {}).get("resources") or []
            check("resources/list", any(r.get("uri") == "havoc://sessions" for r in resources))

            status, body, _ = await rpc(client, base + "/mcp", "resources/read",
                                        {"uri": "havoc://sessions"}, session_id=session_id)
            text = ((body.get("result") or {}).get("contents") or [{}])[0].get("text", "")
            check("resources/read 内容", "demo01" in text)

            status, body, _ = await rpc(client, base + "/mcp", "prompts/list", {}, session_id=session_id)
            prompts = (body.get("result") or {}).get("prompts") or []
            check("prompts/list", any(p.get("name") == "triage_session" for p in prompts))

            status, body, _ = await rpc(client, base + "/mcp", "prompts/get",
                                        {"name": "triage_session", "arguments": {"session_id": "demo01"}},
                                        session_id=session_id)
            check("prompts/get", bool((body.get("result") or {}).get("messages")))

            status, body, _ = await rpc(client, base + "/mcp", "prompts/get",
                                        {"name": "triage_session", "arguments": {}}, session_id=session_id)
            check("prompts/get 缺参数报错", ((body.get("error") or {}).get("code") == -32602))

            status, body, _ = await rpc(client, base + "/mcp", "ping", {}, session_id=session_id)
            check("ping", status == 200 and body.get("result") == {})

            async with client.post(base + "/mcp", data="{bad json",
                                   headers={"Content-Type": "application/json",
                                            "Authorization": f"Bearer {TOKEN}"}) as resp:
                check("非法 JSON 400", resp.status == 400)

            async with client.post(base + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                   headers={"Authorization": "Bearer wrong-token"}) as resp:
                check("错误令牌 401", resp.status == 401)

            async with client.get(base + "/mcp",
                                  headers={"Authorization": f"Bearer {TOKEN}"}) as resp:
                check("GET /mcp 405", resp.status == 405)
    finally:
        await runner.cleanup()


async def test_stdio(fake_url: str) -> None:
    print("\n[3] stdio 传输")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(BASE_DIR / "mcp_server.py"), "--stdio",
        "--url", fake_url, "--token", TOKEN,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def send(message: dict) -> dict:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=15)
        return json.loads(line.decode())

    try:
        init = await send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}}})
        check("stdio initialize", init.get("result", {}).get("protocolVersion") == "2025-06-18")
        # 通知不应有响应；随后 ping 必须收到对应 id。
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
        await proc.stdin.drain()
        pong = await send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        check("stdio 通知无响应 + ping", pong.get("id") == 2 and pong.get("result") == {})
        call = await send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "havoc_list_sessions", "arguments": {}}})
        sessions = ((call.get("result") or {}).get("structuredContent") or {}).get("sessions") or []
        check("stdio 工具调用", len(sessions) == 2)
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        stderr = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
        if proc.returncode not in (0, -15, 143) and stderr:
            print(stderr[-1000:])


async def test_gateway_mount() -> None:
    print("\n[4] 网关内置 /mcp 挂载与令牌鉴权")
    cfg = json.loads((BASE_DIR / "config.json").read_text("utf-8"))
    port = free_port()
    cfg["web"].update({"host": "127.0.0.1", "port": port, "tls_cert": "", "tls_key": ""})
    cfg["teamserver"]["port"] = 59999          # 不连接真实 teamserver，避免干扰在线会话
    cfg["python"] = {"enabled": False}
    cfg["mcp"] = {"enabled": True, "token": TOKEN, "token_file": "run/mcp.token",
                  "state_file": f"data/mcp_state_mount_{port}.json"}
    app = gateway.build_app(cfg)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(base + "/api/health") as resp:
                check("公开 /api/health", resp.status == 200)
            async with client.post(base + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}) as resp:
                check("无令牌 /mcp 401", resp.status == 401)
            status, body, headers = await rpc(client, base + "/mcp", "initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "mount", "version": "1"},
            })
            check("挂载 initialize", status == 200 and "result" in body,
                  json.dumps(body, ensure_ascii=False)[:200])
            session_id = headers.get("Mcp-Session-Id")
            health = await call_tool(client, base + "/mcp", "havoc_health", session_id=session_id)
            check("挂载 havoc_health", (health.get("structuredContent") or {}).get("ok") is True)
            state = await call_tool(client, base + "/mcp", "havoc_state", session_id=session_id)
            check("挂载 havoc_state", (state.get("structuredContent") or {}).get("type") == "snapshot")
            info = await call_tool(client, base + "/mcp", "havoc_mcp_info", session_id=session_id)
            check("挂载 /api/mcp/info", (info.get("structuredContent") or {}).get("enabled") is True)
            # X-MCP-Token 也应被接受（部分客户端不方便设置 Authorization）。
            async with client.post(base + "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
                                   headers={"X-MCP-Token": TOKEN}) as resp:
                check("X-MCP-Token 鉴权", resp.status == 200)
    finally:
        await runner.cleanup()


async def test_live(url: str, token: str) -> None:
    print("\n[5] 在线网关只读检查")
    if not token:
        check("读取 MCP 令牌", False, "run/mcp.token 为空")
        return
    async with aiohttp.ClientSession() as client:
        status, body, headers = await rpc(client, url.rstrip("/") + "/mcp", "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "live", "version": "1"},
        }, token=token)
        if status != 200:
            check("在线 initialize", False, f"HTTP {status}: {body}")
            return
        check("在线 initialize", True)
        session_id = headers.get("Mcp-Session-Id")
        for name, arguments in [
            ("havoc_health", {}),
            ("havoc_status", {}),
            ("havoc_list_sessions", {}),
            ("havoc_list_listeners", {}),
            ("havoc_events", {"limit": 5}),
            ("havoc_mcp_info", {}),
        ]:
            result = await call_tool(client, url.rstrip("/") + "/mcp", name, arguments,
                                     session_id=session_id, token=token)
            check(f"在线 {name}", not result.get("isError"),
                  json.dumps(result, ensure_ascii=False)[:300])


async def run(args: argparse.Namespace) -> int:
    requests: list = []
    uploads: list = []
    fake_port = free_port()
    fake_app = build_fake_gateway(requests, uploads)
    fake_runner = web.AppRunner(fake_app, access_log=None)
    await fake_runner.setup()
    fake_site = web.TCPSite(fake_runner, "127.0.0.1", fake_port)
    await fake_site.start()
    fake_url = f"http://127.0.0.1:{fake_port}"
    try:
        test_registry()
        with __import__("tempfile").TemporaryDirectory() as tmp:
            await test_http(fake_url, Path(tmp))
        await test_stdio(fake_url)
        if requests:
            print(f"\n  假网关收到 {len(requests)} 次 REST 调用、{len(uploads)} 次 multipart 上传")
        await test_gateway_mount()
        if args.live:
            await test_live(args.url, mcp_server.load_token(Path(args.token_file)))
    finally:
        await fake_runner.cleanup()

    failed = [item for item in CHECKS if not item[1]]
    print("\n" + "=" * 60)
    print(f"通过 {len(CHECKS) - len(failed)} / {len(CHECKS)}")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL: {name} {detail}")
        return 1
    print("MCP 验收全部通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Havoc Web 控制台 MCP 验收")
    parser.add_argument("--live", action="store_true", help="追加对运行中网关的只读检查")
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    parser.add_argument("--token-file", default=str(BASE_DIR / "run" / "mcp.token"))
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
