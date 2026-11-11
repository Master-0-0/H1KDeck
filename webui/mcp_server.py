#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc Web 控制台 MCP 服务。

把 webui 的全部 HTTP 能力通过 MCP（Model Context Protocol）对外暴露：

- 传输 1：stdio（`python3 mcp_server.py --stdio`），供 Claude Desktop/Cursor 等
  本机 MCP 客户端拉起；
- 传输 2：HTTP Streamable（网关内置 `POST /mcp`，或独立
  `python3 mcp_server.py --http --port 8090`），供远程 MCP 客户端接入。

两种传输共用同一份工具/资源/提示词注册表（`mcp_tools.py`）与同一个网关 REST
客户端，因此 MCP 调用与浏览器操作共享状态、审计与权限。鉴权使用
`Authorization: Bearer <token>`（令牌在网关 `run/mcp.token`，0600）。

仅用于已获得明确授权的安全测试环境。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiohttp
from aiohttp import web

from mcp_tools import Registry, build_registry

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_URL = os.environ.get("HAVOC_WEBUI_URL", "http://127.0.0.1:8081").rstrip("/")
DEFAULT_TOKEN_FILE = BASE_DIR / "run" / "mcp.token"

SERVER_NAME = "havoc-webui-mcp"
SERVER_VERSION = "1.0.0"
SERVER_TITLE = "Havoc Web 控制台 MCP"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"
MAX_JSON_REPLY = 32 * 1024 * 1024

INSTRUCTIONS = (
    "这是 Havoc Web 控制台的 MCP 入口，havoc_* 工具与浏览器界面同源同权限："
    "会话与心跳、控制台命令、文件管理（列目录/读写/上传下载/增删改）、进程、"
    "监听器（原生与 TCP/KCP/UDP/WS/DNS/DoH/DoT/OSS 传输）、团队聊天、事件、"
    "战利品、载荷生成、扩展商店、Python 脚本与 Python 宿主全部可用。"
    "破坏性操作（下发命令、结束进程、删除文件、注入、退出会话、删除监听器）"
    "请先确认目标与授权范围。只读资源使用 havoc:// 前缀；"
    "HTTP 端点为 /mcp，使用 Authorization: Bearer <run/mcp.token> 鉴权。"
)

log = logging.getLogger("havoc-mcp")


# ---------------------------------------------------------------- 错误与序列化

class GatewayError(Exception):
    """网关返回的错误或网络错误。"""

    def __init__(self, status: int, code: str, message: str, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload

    def as_text(self) -> str:
        prefix = f"HTTP {self.status} " if self.status else ""
        return f"{prefix}{self.code}: {self.message}"

    def to_dict(self) -> dict:
        out = {"status": self.status, "error": self.code, "message": self.message}
        if self.payload is not None:
            out["payload"] = self.payload
        return out


class MCPError(Exception):
    """JSON-RPC 协议层错误。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _json_reply(value: Any, *, status: int = 200, headers: Optional[dict] = None) -> web.Response:
    return web.json_response(
        value,
        status=status,
        headers=headers,
        dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str),
    )


# ---------------------------------------------------------------- 网关客户端

class GatewayClient:
    """最小化的 webui REST 客户端（Bearer 令牌 + 可选 HTTPS）。"""

    def __init__(self, base_url: str = DEFAULT_URL, token: str = "", *,
                 timeout: float = 90.0, insecure: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.insecure = insecure
        self._session: Optional[aiohttp.ClientSession] = None

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-MCP-Token"] = self.token
        return headers

    async def start(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        connector = None
        if self.insecure and self.base_url.startswith("https://"):
            connector = aiohttp.TCPConnector(ssl=False)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            trust_env=False,
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @staticmethod
    def form_data() -> aiohttp.FormData:
        return aiohttp.FormData()

    async def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                       json_body: Any = None, form: Optional[aiohttp.FormData] = None,
                       timeout: Optional[float] = None) -> dict:
        await self.start()
        assert self._session is not None
        kwargs: dict = {"params": params, "headers": self._headers()}
        if json_body is not None:
            kwargs["json"] = json_body
        if form is not None:
            kwargs["data"] = form
        if timeout is not None:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        url = self.base_url + path
        try:
            async with self._session.request(method, url, **kwargs) as resp:
                raw = await resp.read()
                if resp.status >= 400:
                    code, message, payload = _parse_error_body(resp.status, raw)
                    raise GatewayError(resp.status, code, message, payload)
                return {
                    "status": resp.status,
                    "headers": dict(resp.headers),
                    "content_type": resp.headers.get("Content-Type", ""),
                    "data": raw,
                }
        except aiohttp.ClientError as exc:
            raise GatewayError(0, "unreachable", f"无法连接网关 {self.base_url}：{exc}") from exc
        except asyncio.TimeoutError as exc:
            raise GatewayError(0, "timeout", f"网关请求超时：{method} {path}") from exc

    async def request_json(self, method: str, path: str, *, params: Optional[dict] = None,
                           json_body: Any = None, form: Optional[aiohttp.FormData] = None,
                           timeout: Optional[float] = None) -> Any:
        result = await self._request(method, path, params=params, json_body=json_body,
                                     form=form, timeout=timeout)
        raw: bytes = result["data"]
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            text = raw[:2000].decode("utf-8", "replace")
            raise GatewayError(result["status"], "bad_json",
                               f"网关返回了非 JSON 响应：{exc}", {"text": text}) from exc

    async def request_bytes(self, method: str, path: str, *, params: Optional[dict] = None,
                            max_bytes: int = 64 * 1024 * 1024,
                            timeout: Optional[float] = None) -> dict:
        await self.start()
        assert self._session is not None
        url = self.base_url + path
        kwargs: dict = {"params": params, "headers": self._headers()}
        if timeout is not None:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        try:
            async with self._session.request(method, url, **kwargs) as resp:
                if resp.status >= 400:
                    raw = await resp.read()
                    code, message, payload = _parse_error_body(resp.status, raw)
                    raise GatewayError(resp.status, code, message, payload)
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise GatewayError(413, "too_large",
                                           f"内容超过 {max_bytes} 字节上限，请提高 max_bytes 或改用其它方式")
                    chunks.append(chunk)
                return {
                    "data": b"".join(chunks),
                    "headers": dict(resp.headers),
                    "content_type": resp.headers.get("Content-Type", ""),
                }
        except aiohttp.ClientError as exc:
            raise GatewayError(0, "unreachable", f"无法连接网关 {self.base_url}：{exc}") from exc
        except asyncio.TimeoutError as exc:
            raise GatewayError(0, "timeout", f"网关下载超时：{path}") from exc


def _parse_error_body(status: int, raw: bytes) -> tuple[str, str, Any]:
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "http_error", text[:1000] or f"HTTP {status}", None
    if isinstance(data, dict):
        code = str(data.get("error") or "http_error")
        message = str(data.get("message") or data.get("detail") or text[:1000])
        return code, message, data
    return "http_error", text[:1000], data


# ---------------------------------------------------------------- MCP 核心

class MCPCore:
    """JSON-RPC 2.0 + MCP 方法分发（与传输无关）。"""

    def __init__(self, client: GatewayClient, registry: Optional[Registry] = None):
        self.client = client
        self.registry = registry or build_registry()

    # ---------- 消息入口 ----------

    async def handle_message(self, payload: Any) -> Optional[Any]:
        if isinstance(payload, list):
            if not payload:
                return _rpc_error(None, -32600, "Invalid Request：空批次")
            results = []
            for item in payload:
                reply = await self._handle_single(item)
                if reply is not None:
                    results.append(reply)
            return results or None
        return await self._handle_single(payload)

    async def _handle_single(self, message: Any) -> Optional[dict]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid Request：需要 JSON-RPC 2.0 对象")
        method = message.get("method")
        is_notification = "id" not in message or message.get("id") is None
        msg_id = message.get("id")
        if not isinstance(method, str) or not method:
            return None if is_notification else _rpc_error(msg_id, -32600, "Invalid Request：缺少 method")
        if is_notification:
            # notifications/initialized、notifications/cancelled 等无需响应。
            return None
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _rpc_error(msg_id, -32602, "Invalid params：params 必须是对象")
        try:
            result = await self._dispatch(method, params)
        except MCPError as exc:
            error = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": msg_id, "error": error}
        except GatewayError as exc:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32000, "message": exc.as_text(), "data": exc.to_dict()}}
        except Exception as exc:  # noqa: BLE001 - 协议层兜底
            log.exception("MCP internal error on %s", method)
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": f"内部错误：{exc}"}}
        if result is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    async def _dispatch(self, method: str, params: dict) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool.spec() for tool in self.registry.tools]}
        if method == "tools/call":
            return await self._tools_call(params)
        if method == "resources/list":
            return {"resources": [res.spec() for res in self.registry.resources]}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            return await self._resources_read(params)
        if method == "prompts/list":
            return {"prompts": [prompt.spec() for prompt in self.registry.prompts]}
        if method == "prompts/get":
            return self._prompts_get(params)
        if method == "logging/setLevel":
            return {}
        raise MCPError(-32601, f"Method not found：{method}")

    # ---------- 方法实现 ----------

    def _initialize(self, params: dict) -> dict:
        requested = str(params.get("protocolVersion") or "")
        version = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
                "logging": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "title": SERVER_TITLE,
                "version": SERVER_VERSION,
            },
            "instructions": INSTRUCTIONS,
        }

    async def _tools_call(self, params: dict) -> dict:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise MCPError(-32602, "Invalid params：arguments 必须是对象")
        tool = self.registry.tools_by_name.get(name)
        if tool is None:
            raise MCPError(-32602, f"未知工具：{name}")
        try:
            value = await tool.handler(self.client, arguments)
        except GatewayError as exc:
            return _tool_error(f"网关错误：{exc.as_text()}", exc.to_dict())
        except ValueError as exc:
            return _tool_error(f"参数错误：{exc}")
        except Exception as exc:  # noqa: BLE001 - 工具失败不应中断会话
            log.exception("tool %s failed", name)
            return _tool_error(f"工具执行失败：{exc}")
        return _tool_result(value)

    async def _resources_read(self, params: dict) -> dict:
        uri = str(params.get("uri") or "")
        resource = self.registry.resources_by_uri.get(uri)
        if resource is None:
            raise MCPError(-32602, f"未知资源：{uri}")
        try:
            text = await resource.read(self.client)
        except GatewayError as exc:
            raise MCPError(-32000, f"读取资源失败：{exc.as_text()}", exc.to_dict())
        return {"contents": [{"uri": uri, "mimeType": resource.mime_type, "text": text}]}

    def _prompts_get(self, params: dict) -> dict:
        name = str(params.get("name") or "")
        prompt = self.registry.prompts_by_name.get(name)
        if prompt is None:
            raise MCPError(-32602, f"未知提示词：{name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise MCPError(-32602, "Invalid params：arguments 必须是对象")
        for spec in prompt.arguments:
            if spec.get("required") and not str(args.get(spec.get("name")) or "").strip():
                raise MCPError(-32602, f"缺少参数：{spec.get('name')}")
        text = prompt.render(args)
        return {
            "description": prompt.description,
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.registry.tools]


def _rpc_error(msg_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _tool_result(value: Any) -> dict:
    if isinstance(value, str):
        text = value
    else:
        text = json_dumps(value)
    result: dict = {"content": [{"type": "text", "text": text}]}
    if isinstance(value, dict):
        result["structuredContent"] = value
    elif isinstance(value, list):
        result["structuredContent"] = {"items": value}
    return result


def _tool_error(message: str, detail: Any = None) -> dict:
    result: dict = {"content": [{"type": "text", "text": message}], "isError": True}
    if detail is not None:
        result["structuredContent"] = {"error": message, "detail": detail}
    return result


# ---------------------------------------------------------------- HTTP 传输

class MCPBridge:
    """把 MCP 核心挂到 aiohttp（网关内置或独立进程）上。"""

    def __init__(self, base_url: str = DEFAULT_URL, token: str = "", *,
                 insecure: bool = False, registry: Optional[Registry] = None):
        self.client = GatewayClient(base_url, token, insecure=insecure)
        self.core = MCPCore(self.client, registry)
        self.sessions: set[str] = set()
        # 运行态：网关管理页可在不重启进程的前提下 start()/stop()。
        self.running = True
        self.started_at = time.time()
        self.last_error = ""
        # True 表示 base_url 由 config.mcp.gateway_url 显式指定，main() 不再改写。
        self.explicit_base = False

    def set_base_url(self, base_url: str, *, insecure: Optional[bool] = None) -> None:
        self.client.base_url = base_url.rstrip("/")
        if insecure is not None:
            self.client.insecure = insecure

    async def start(self) -> None:
        """恢复服务：清空旧会话（旧 Session-Id 不复用），客户端需重新 initialize。"""
        self.running = True
        self.started_at = time.time()
        self.last_error = ""
        self.sessions.clear()

    async def stop(self) -> None:
        """停止服务：所有 Mcp-Session-Id 立即失效，并关闭回环 aiohttp 会话。"""
        self.running = False
        self.started_at = 0.0
        self.sessions.clear()
        try:
            await self.client.close()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"关闭 MCP 回环客户端失败：{exc}"
            log.warning("MCP client close failed: %s", exc)

    async def close(self, _app: Any = None) -> None:
        await self.stop()

    def _stopped_reply(self) -> web.Response:
        return _json_reply(
            _rpc_error(None, -32002,
                       "MCP 已停止：请在 Web 控制台「MCP 管理」页启动后重新 initialize"),
            status=503,
            headers={"Retry-After": "5"},
        )

    async def handle_post(self, request: web.Request) -> web.Response:
        if not self.running:
            return self._stopped_reply()
        session_id = request.headers.get("Mcp-Session-Id")
        if session_id and session_id not in self.sessions:
            return _json_reply(
                _rpc_error(None, -32000, "MCP 会话已失效，请重新 initialize"),
                status=404,
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return _json_reply(_rpc_error(None, -32700, "Parse error：请求体不是合法 JSON"), status=400)
        except Exception as exc:  # noqa: BLE001
            return _json_reply(_rpc_error(None, -32700, f"Parse error：{exc}"), status=400)

        result = await self.core.handle_message(payload)
        headers: dict[str, str] = {}
        if _contains_initialize(payload):
            new_id = uuid.uuid4().hex
            self.sessions.add(new_id)
            headers["Mcp-Session-Id"] = new_id
        if result is None:
            return web.Response(status=202, headers=headers)
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > MAX_JSON_REPLY:
            return _json_reply(
                _rpc_error(None, -32001, "响应超过 MCP 单次返回上限，请缩小 limit 或改用 save_to 落盘"),
                status=200,
            )
        return _json_reply(result, headers=headers)

    async def handle_get(self, request: web.Request) -> web.Response:
        if not self.running:
            return self._stopped_reply()
        return _json_reply(
            _rpc_error(None, -32601, "本服务不提供 SSE 推送通道，请使用 POST /mcp"),
            status=405,
            headers={"Allow": "POST, DELETE, OPTIONS"},
        )

    async def handle_delete(self, request: web.Request) -> web.Response:
        if not self.running:
            return self._stopped_reply()
        session_id = request.headers.get("Mcp-Session-Id")
        if session_id:
            self.sessions.discard(session_id)
        return web.Response(status=200)

    async def handle_options(self, request: web.Request) -> web.Response:
        if not self.running:
            return self._stopped_reply()
        return web.Response(
            status=204,
            headers={
                "Allow": "POST, DELETE, OPTIONS",
                "Access-Control-Allow-Methods": "POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, Mcp-Session-Id, X-MCP-Token",
                "Access-Control-Max-Age": "600",
            },
        )


def _contains_initialize(payload: Any) -> bool:
    if isinstance(payload, dict):
        return payload.get("method") == "initialize"
    if isinstance(payload, list):
        return any(isinstance(item, dict) and item.get("method") == "initialize" for item in payload)
    return False


# ---------------------------------------------------------------- 独立 HTTP 服务

def build_http_app(bridge: MCPBridge, token: str) -> web.Application:
    app = web.Application(client_max_size=MAX_JSON_REPLY)

    @web.middleware
    async def token_middleware(request: web.Request, handler):
        if request.path == "/health":
            return await handler(request)
        supplied = _bearer_token(request)
        if not token or not secrets.compare_digest(supplied, token):
            resp = _json_reply(
                _rpc_error(None, -32001, "unauthorized：需要 Authorization: Bearer <token>"),
                status=401,
            )
            resp.headers["WWW-Authenticate"] = 'Bearer realm="havoc-mcp"'
            return resp
        return await handler(request)

    app.middlewares.append(token_middleware)
    app.router.add_post("/mcp", bridge.handle_post)
    app.router.add_get("/mcp", bridge.handle_get)
    app.router.add_delete("/mcp", bridge.handle_delete)
    app.router.add_options("/mcp", bridge.handle_options)

    async def health(request: web.Request) -> web.Response:
        return _json_reply({
            "ok": True,
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "protocols": list(SUPPORTED_PROTOCOLS),
            "tools": len(bridge.core.registry.tools),
            "gateway": bridge.client.base_url,
        })

    app.router.add_get("/health", health)

    async def cleanup(app_: web.Application) -> None:
        await bridge.close()

    app.on_cleanup.append(cleanup)
    return app


def _bearer_token(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-MCP-Token", "").strip()


# ---------------------------------------------------------------- stdio 传输

async def _read_stdio_message(reader: asyncio.StreamReader) -> Any:
    """读取一条 stdio 消息：支持换行分隔 JSON 与 LSP 风格 Content-Length 帧。"""
    line = await reader.readline()
    while line and not line.strip():
        line = await reader.readline()
    if not line:
        return None
    if line.lower().startswith(b"content-length:"):
        try:
            length = int(line.split(b":", 1)[1].strip())
        except (IndexError, ValueError):
            raise ValueError("无效的 Content-Length 头")
        while True:
            header = await reader.readline()
            if header in (b"\r\n", b"\n", b""):
                break
        body = await reader.readexactly(length)
        return json.loads(body.decode("utf-8"))
    return json.loads(line.decode("utf-8"))


async def serve_stdio(bridge: MCPBridge) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    log.info("MCP stdio 就绪（网关 %s，%d 个工具）",
             bridge.client.base_url, len(bridge.core.registry.tools))
    while True:
        try:
            message = await _read_stdio_message(reader)
        except json.JSONDecodeError as exc:
            reply = _rpc_error(None, -32700, f"Parse error：{exc}")
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        except ValueError as exc:
            reply = _rpc_error(None, -32700, str(exc))
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        except asyncio.IncompleteReadError:
            break
        if message is None:
            break
        reply = await bridge.core.handle_message(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()
    log.info("MCP stdio 输入结束，退出")


# ---------------------------------------------------------------- 令牌与 CLI

def load_token(path: Path) -> str:
    try:
        data = path.read_text("utf-8").strip()
    except OSError:
        return ""
    return data


def resolve_token(explicit: str, token_file: str) -> tuple[str, Optional[Path]]:
    if explicit:
        return explicit, None
    path = Path(os.path.expanduser(token_file)) if token_file else DEFAULT_TOKEN_FILE
    token = load_token(path)
    if not token:
        log.warning("未找到 MCP 令牌文件 %s；请先在网关生成或使用 --token", path)
    return token, path


def list_tools() -> int:
    registry = build_registry()
    for tool in registry.tools:
        print(f"{tool.name}\t{tool.title}")
    print(f"\n共 {len(registry.tools)} 个工具、{len(registry.resources)} 个资源、"
          f"{len(registry.prompts)} 个提示词")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp_server.py",
        description="Havoc Web 控制台 MCP 服务（stdio / HTTP）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true", help="以 stdio 方式运行（默认）")
    mode.add_argument("--http", action="store_true", help="以独立 HTTP 服务运行")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8090, help="HTTP 监听端口（默认 8090）")
    parser.add_argument("--url", default=DEFAULT_URL, help="webui 网关地址（默认 %(default)s）")
    parser.add_argument("--token", default=os.environ.get("HAVOC_MCP_TOKEN", ""),
                        help="MCP 访问令牌（默认读取 run/mcp.token 或 HAVOC_MCP_TOKEN）")
    parser.add_argument("--token-file", default="", help="令牌文件路径（默认 webui/run/mcp.token）")
    parser.add_argument("--insecure", action="store_true", help="HTTPS 网关使用自签名证书时跳过校验")
    parser.add_argument("--list-tools", action="store_true", help="只打印工具清单后退出")
    parser.add_argument("--log-level", default="INFO", help="日志级别，默认 INFO")
    return parser


async def _run_http(args: argparse.Namespace, bridge: MCPBridge, token: str) -> int:
    app = build_http_app(bridge, token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    log.info("MCP HTTP 已启动：http://%s:%d/mcp（网关 %s，%d 个工具）",
             args.host, args.port, bridge.client.base_url, len(bridge.core.registry.tools))
    stop = asyncio.Event()

    def _stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
    return 0


async def _amain(args: argparse.Namespace) -> int:
    token, _ = resolve_token(args.token, args.token_file)
    bridge = MCPBridge(args.url, token, insecure=args.insecure)
    try:
        if args.http:
            return await _run_http(args, bridge, token)
        await bridge.client.start()
        await serve_stdio(bridge)
        return 0
    finally:
        await bridge.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    if args.list_tools:
        return list_tools()
    if not args.http and not args.stdio:
        # 默认 stdio，便于 MCP 客户端直接拉起；显式 --http 时走 HTTP。
        args.stdio = True
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
