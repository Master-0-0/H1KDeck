# -*- coding: utf-8 -*-
"""Havoc Web 控制台 MCP 工具、资源与提示词注册表。

这里把 webui 的全部 HTTP 能力（会话、命令、文件、进程、监听器、聊天、事件、
战利品、载荷、扩展商店、脚本、Python 宿主、暂存服务、团队服务器）映射为 MCP
工具，另外提供只读资源与少量提示词。所有调用最终都走网关 REST API，保证
MCP 与浏览器看到的是同一份状态、同一套权限与审计。

仅用于已获得明确授权的安全测试环境。
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from urllib.parse import quote

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查，避免循环导入
    from mcp_server import GatewayClient

MAX_INLINE_DOWNLOAD = 1024 * 1024        # 不落盘时最多内联 1 MiB
MAX_DOWNLOAD = 64 * 1024 * 1024          # 单次下载上限
MAX_LOCAL_UPLOAD = 32 * 1024 * 1024      # 目标会话直传上限（网关限制 32 MiB）
MAX_STAGING_UPLOAD = 64 * 1024 * 1024    # 控制台暂存上传上限（网关限制 64 MiB）
MAX_SCRIPT_UPLOAD = 2 * 1024 * 1024      # 脚本上传上限（网关限制 2 MiB）

_PATH_ARG_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ---------------------------------------------------------------- JSON Schema 辅助

def S(desc: str, **kw: Any) -> dict:
    return {"type": "string", "description": desc, **kw}


def I(desc: str, **kw: Any) -> dict:
    return {"type": "integer", "description": desc, **kw}


def B(desc: str, **kw: Any) -> dict:
    return {"type": "boolean", "description": desc, **kw}


def E(desc: str, values: list[str], **kw: Any) -> dict:
    return {"type": "string", "description": desc, "enum": list(values), **kw}


def A(desc: str, items: dict, **kw: Any) -> dict:
    return {"type": "array", "description": desc, "items": items, **kw}


def O(desc: str, props: Optional[dict] = None) -> dict:
    out: dict = {"type": "object", "description": desc}
    if props:
        out["properties"] = props
    out["additionalProperties"] = True
    return out


def _schema(props: dict, required: list[str]) -> dict:
    schema: dict = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


# ---------------------------------------------------------------- 注册表模型

@dataclass
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    handler: Callable[["GatewayClient", dict], Awaitable[Any]]
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False

    def spec(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "openWorldHint": self.open_world,
            },
        }


@dataclass
class Resource:
    uri: str
    name: str
    description: str
    path: str
    query: dict = field(default_factory=dict)
    mime_type: str = "application/json"

    def spec(self) -> dict:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }

    async def read(self, client: "GatewayClient") -> str:
        from mcp_server import json_dumps  # 延迟导入，避免模块循环

        data = await client.request_json("GET", self.path, params=dict(self.query))
        return json_dumps(data)


@dataclass
class Prompt:
    name: str
    title: str
    description: str
    arguments: list[dict]
    render: Callable[[dict], str]

    def spec(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "arguments": self.arguments,
        }


class Registry:
    def __init__(self, tools: list[Tool], resources: list[Resource], prompts: list[Prompt]):
        self.tools = tools
        self.tools_by_name = {tool.name: tool for tool in tools}
        self.resources = resources
        self.resources_by_uri = {res.uri: res for res in resources}
        self.prompts = prompts
        self.prompts_by_name = {prompt.name: prompt for prompt in prompts}


# ---------------------------------------------------------------- 通用辅助

def _fill_path(template: str, args: dict) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key not in args or args[key] in (None, ""):
            raise ValueError(f"缺少路径参数：{key}")
        return quote(str(args[key]), safe="")

    return _PATH_ARG_RE.sub(repl, template)


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _query_params(args: dict, names: tuple[str, ...]) -> dict:
    out = {}
    for name in names:
        value = args.get(name)
        if value is not None:
            out[name] = _query_value(value)
    return out


def _body_payload(args: dict, names: tuple[str, ...], mapping: dict[str, str]) -> dict:
    payload = {}
    for name in names:
        if name in args and args[name] is not None:
            payload[mapping.get(name, name)] = args[name]
    return payload


def _filename_from_headers(headers: dict, fallback: str) -> str:
    disposition = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if match:
        from urllib.parse import unquote

        return Path(unquote(match.group(1))).name or fallback
    match = re.search(r'filename="?([^";]+)"?', disposition, re.IGNORECASE)
    if match:
        return Path(match.group(1)).name or fallback
    return fallback


def _safe_local_name(name: str, fallback: str = "download.bin") -> str:
    base = Path(str(name or "").replace("\\", "/")).name
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip().strip(".")
    return base or fallback


def _resolve_save_path(save_to: str, filename: str) -> Path:
    target = Path(os.path.expanduser(str(save_to)))
    if target.is_dir():
        target = target / _safe_local_name(filename)
    return target


def _read_local_file(path: str, limit: int, *, label: str) -> tuple[bytes, str]:
    target = Path(os.path.expanduser(str(path)))
    if not target.is_file():
        raise ValueError(f"{label}不存在：{target}")
    size = target.stat().st_size
    if size > limit:
        raise ValueError(f"{label}超过 {limit // (1024 * 1024)} MiB 上限：{size} 字节")
    return target.read_bytes(), target.name


def _decode_b64_text(text: str, *, label: str) -> bytes:
    try:
        return base64.b64decode(str(text), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{label}不是合法 Base64：{exc}") from exc


# ---------------------------------------------------------------- 工具工厂

def _rest_tool(
    name: str,
    title: str,
    description: str,
    props: dict,
    *,
    method: str = "GET",
    path: str,
    query: tuple[str, ...] = (),
    body: tuple[str, ...] = (),
    body_map: Optional[dict[str, str]] = None,
    required: tuple[str, ...] = (),
    destructive: bool = False,
    open_world: bool = False,
) -> Tool:
    path_args = tuple(_PATH_ARG_RE.findall(path))
    req = list(dict.fromkeys([*path_args, *required]))
    mapping = body_map or {}

    async def handler(client: "GatewayClient", args: dict) -> Any:
        url = _fill_path(path, args)
        params = _query_params(args, query)
        payload = _body_payload(args, body, mapping)
        return await client.request_json(
            method, url, params=params or None, json_body=payload if body else None
        )

    return Tool(
        name=name,
        title=title,
        description=description,
        input_schema=_schema(props, req),
        handler=handler,
        read_only=method == "GET",
        destructive=destructive,
        open_world=open_world,
    )


def _download_tool(
    name: str,
    title: str,
    description: str,
    props: dict,
    *,
    path: str,
    query: tuple[str, ...] = (),
    fixed_query: Optional[dict] = None,
    required: tuple[str, ...] = (),
    default_name: str,
    destructive: bool = False,
) -> Tool:
    path_args = tuple(_PATH_ARG_RE.findall(path))
    req = list(dict.fromkeys([*path_args, *required]))

    async def handler(client: "GatewayClient", args: dict) -> Any:
        url = _fill_path(path, args)
        params = {**(fixed_query or {}), **_query_params(args, query)}
        limit = int(args.get("max_bytes") or MAX_DOWNLOAD)
        result = await client.request_bytes("GET", url, params=params or None, max_bytes=limit)
        data: bytes = result["data"]
        filename = _filename_from_headers(result["headers"], default_name)
        meta: dict = {
            "filename": filename,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": result["content_type"],
        }
        save_to = args.get("save_to")
        if save_to:
            target = _resolve_save_path(str(save_to), filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            meta["saved_to"] = str(target)
            return meta
        if len(data) <= MAX_INLINE_DOWNLOAD:
            meta["encoding"] = "base64"
            meta["content_b64"] = base64.b64encode(data).decode("ascii")
            return meta
        meta["message"] = (
            f"内容 {len(data)} 字节，超过 {MAX_INLINE_DOWNLOAD} 字节内联上限；"
            "传入 save_to（文件或已存在目录）可直接落盘。"
        )
        return meta

    props = dict(props)
    props.setdefault("save_to", S("可选：保存到 MCP 服务所在主机的路径（目录或文件）；不传则按内联上限返回 Base64"))
    props.setdefault("max_bytes", I(f"下载大小上限，默认 {MAX_DOWNLOAD}", minimum=1, maximum=512 * 1024 * 1024))

    return Tool(
        name=name,
        title=title,
        description=description,
        input_schema=_schema(props, req),
        handler=handler,
        read_only=False,
        destructive=destructive,
        open_world=False,
    )


def _upload_tool(
    name: str,
    title: str,
    description: str,
    props: dict,
    *,
    path: str,
    required: tuple[str, ...] = (),
    path_field: Optional[str] = None,
    limit: int,
    default_filename: str,
) -> Tool:
    path_args = tuple(_PATH_ARG_RE.findall(path))
    req = list(dict.fromkeys([*path_args, *required]))

    async def handler(client: "GatewayClient", args: dict) -> Any:
        url = _fill_path(path, args)
        if args.get("local_file"):
            data, filename = _read_local_file(str(args["local_file"]), limit, label="本地文件")
        elif args.get("content_b64"):
            data = _decode_b64_text(str(args["content_b64"]), label="content_b64")
            filename = _safe_local_name(str(args.get("filename") or default_filename), default_filename)
        else:
            raise ValueError("需要 local_file 或 content_b64 之一")

        form = client.form_data()
        if path_field:
            form.add_field(path_field, str(args.get(path_field) or "."))
        form.add_field("file", data, filename=filename)
        return await client.request_json("POST", url, form=form)

    return Tool(
        name=name,
        title=title,
        description=description,
        input_schema=_schema(props, req),
        handler=handler,
        read_only=False,
        destructive=False,
        open_world=False,
    )


# ---------------------------------------------------------------- 工具定义

_SID = S("会话 ID（Demon 的 NameID，例如 3448940c）")
_REMOTE_PATH = S("目标主机上的路径（Windows 用 C:\\...，Linux 用 /...；相对路径按会话当前目录解析）")
_LISTENER_NAME = S("监听器名称")
_SAVE_TO = S("可选：保存到 MCP 服务所在主机的路径（目录或文件）")


def _listener_props() -> dict:
    return {
        "name": _LISTENER_NAME,
        "protocol": E("监听器协议；传输监听器可用 TCP/KCP/UDP/WebSocket/DNS/DoH/DoT/OSS 等 mode",
                      ["Http", "Https", "Smb", "External", "tcp", "kcp", "udp", "ws", "dns", "doh", "dot", "oss"]),
        "mode": S("传输监听器模式（等价于 protocol，二选一）"),
        "host_bind": S("监听绑定地址，默认 0.0.0.0"),
        "port_bind": S("监听端口"),
        "port_conn": S("回连端口（可选）"),
        "hosts": A("回连主机列表", S("主机")),
        "headers": A("HTTP 头列表", S("Header: value")),
        "uris": A("URI 列表", S("URI")),
        "user_agent": S("User-Agent"),
        "host_header": S("Host 头"),
        "secure": B("是否启用 TLS"),
        "proxy_enabled": B("是否启用上游代理"),
        "proxy_type": S("代理类型，默认 http"),
        "proxy_host": S("代理主机"),
        "proxy_port": S("代理端口"),
        "proxy_username": S("代理用户名"),
        "proxy_password": S("代理密码"),
        "pipe_name": S("SMB 管道名（不含 \\\\.\\pipe\\ 前缀）"),
        "endpoint": S("External 监听器端点（单个路径段）"),
        "client_user": S("服务插件监听器的 ClientUser"),
        "extra": O("透传给网关的额外字段（传输监听器参数、服务插件自定义字段等）"),
    }


def _python_register_props() -> dict:
    command_schema = O("命令定义", {
        "command": S("命令名"),
        "module": S("所属模块（可选）"),
        "function": S("Python 函数名"),
        "description": S("描述"),
        "usage": S("用法"),
        "example": S("示例"),
        "behavior": S("行为标记"),
        "agent": S("适用 Agent，默认 Demon"),
    })
    module_schema = O("模块定义", {
        "name": S("模块名"),
        "description": S("描述"),
        "behavior": S("行为"),
        "usage": S("用法"),
        "example": S("示例"),
        "options": S("选项说明"),
        "agent": S("适用 Agent，默认 Demon"),
    })
    return {
        "script": S("脚本绝对路径，或 <console> 表示 Python 控制台"),
        "replace": B("是否先移除该脚本的旧注册"),
        "commands": A("注册的命令数组", command_schema),
        "modules": A("注册的模块数组", module_schema),
    }


def _build_tools() -> list[Tool]:
    tools: list[Tool] = []

    # ---------------- 网关 / 团队服务器 ----------------
    tools += [
        _rest_tool("havoc_health", "网关健康检查", "返回网关存活状态与服务器时间。",
                   {}, path="/api/health"),
        _rest_tool("havoc_me", "当前登录身份", "返回 MCP/网关注入的操作员身份与 webui 版本。",
                   {}, path="/api/me"),
        _rest_tool("havoc_state", "全量快照", "返回网关内存中的全量状态：连接、会话、监听器、事件、聊天、战利品、载荷、Python 命令。",
                   {}, path="/api/state"),
        _rest_tool("havoc_status", "连接与计数", "返回 teamserver 连接视图、会话/监听器/传输计数、浏览器 WS 客户端数。",
                   {}, path="/api/status"),
        _rest_tool("havoc_profile", "团队服务器 Profile", "返回 teamserver profile 与连接信息。",
                   {}, path="/api/profile"),
        _rest_tool("havoc_reconnect", "重连 teamserver", "恢复自动重连并立即尝试连接 teamserver。",
                   {}, method="POST", path="/api/reconnect"),
        _rest_tool("havoc_disconnect_teamserver", "断开 teamserver", "关闭到 teamserver 的 WSS 并暂停自动重连（浏览器 WS 不受影响）。",
                   {}, method="POST", path="/api/teamserver/disconnect", destructive=True),
        _rest_tool("havoc_stager_health", "暂存服务健康检查", "代理查询 9901 暂存服务的 /health。",
                   {}, path="/api/stager/health"),
        _rest_tool("havoc_start_stager", "启动暂存服务", "执行 start-stager.sh --detach 拉起 9901 暂存服务。",
                   {}, method="POST", path="/api/stager/start"),
    ]

    # ---------------- 会话 / 命令 ----------------
    tools += [
        _rest_tool("havoc_list_sessions", "会话列表", "列出当前所有在线/离线会话（含地址、用户、系统、进程、心跳、sleep/jitter）。",
                   {}, path="/api/sessions"),
        _rest_tool("havoc_get_session", "会话详情", "返回单个会话详情与最近 30 条任务。",
                   {"sid": _SID}, path="/api/sessions/{sid}"),
        _rest_tool("havoc_session_console", "控制台回显", "读取指定会话的控制台行（最多 2000 行）。",
                   {"sid": _SID, "limit": I("返回行数，默认 800", minimum=1, maximum=2000)},
                   path="/api/sessions/{sid}/console", query=("limit",)),
        _rest_tool("havoc_session_tasks", "会话任务", "返回指定会话最近 100 条任务的命令、状态与输出。",
                   {"sid": _SID}, path="/api/sessions/{sid}/tasks"),
        _rest_tool("havoc_mark_session", "标记会话存活状态", "把会话标记为 Alive 或 Dead（与客户端右键菜单一致）。",
                   {"sid": _SID, "marked": E("目标标记", ["Alive", "Dead"])},
                   method="POST", path="/api/sessions/{sid}/mark", body=("marked",),
                   required=("marked",)),
        _rest_tool("havoc_remove_session", "移除会话", "仅从 Web 本地会话表移除，不向 teamserver 发送下线命令。",
                   {"sid": _SID}, method="POST", path="/api/sessions/{sid}/remove",
                   destructive=True),
        _rest_tool("havoc_exit_session", "退出会话", "向 Demon 下发 exit thread/process（仅 Demon 会话）。",
                   {"sid": _SID, "method": E("退出方式", ["thread", "process"])},
                   method="POST", path="/api/sessions/{sid}/exit", body=("method",),
                   required=("method",), destructive=True),
    ]

    async def run_command(client: "GatewayClient", args: dict) -> Any:
        sid = str(args["sid"])
        command = str(args["command"])
        wait = float(args.get("wait_seconds") or 0)
        url = f"/api/sessions/{quote(sid, safe='')}/command"
        data = await client.request_json("POST", url, json_body={"command": command})
        task = data.get("task") if isinstance(data, dict) else None
        task_id = (task or {}).get("task_id")
        if not wait or not task_id:
            return data
        import asyncio
        import time as _time

        deadline = _time.monotonic() + min(wait, 300.0)
        last = task
        while _time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            listing = await client.request_json("GET", f"/api/sessions/{quote(sid, safe='')}/tasks")
            for item in listing.get("tasks") or []:
                if item.get("task_id") == task_id:
                    last = item
                    break
            if last and last.get("completed"):
                await asyncio.sleep(float(args.get("settle_seconds") or 0.6))
                listing = await client.request_json("GET", f"/api/sessions/{quote(sid, safe='')}/tasks")
                for item in listing.get("tasks") or []:
                    if item.get("task_id") == task_id:
                        last = item
                        break
                break
        out = dict(data) if isinstance(data, dict) else {"ok": True}
        out["task"] = last
        out["output"] = (last or {}).get("output", "")
        out["completed"] = bool((last or {}).get("completed"))
        return out

    tools.append(Tool(
        name="havoc_run_command",
        title="下发控制台命令",
        description=(
            "在指定会话执行一行控制台命令（与 Web 控制台输入框完全一致：shell、powershell、"
            "文件、进程、注入、BOF、模块命令等都可以）。可选 wait_seconds 轮询任务直到完成并返回输出。"
        ),
        input_schema=_schema({
            "sid": _SID,
            "command": S("完整命令行，例如 `shell whoami`、`dir C:\\`、`help`"),
            "wait_seconds": I("等待任务完成的最长秒数（0 表示立即返回，最大 300）", minimum=0, maximum=300),
            "settle_seconds": I("任务完成后额外收集输出的时间（默认 0.6）", minimum=0, maximum=10),
        }, ["sid", "command"]),
        handler=run_command,
        destructive=True,
    ))

    # ---------------- 文件管理 ----------------
    tools += [
        _rest_tool("havoc_files_cache", "文件列表缓存", "读取网关缓存的该会话文件列表（不会向目标发命令）。",
                   {"sid": _SID}, path="/api/sessions/{sid}/files"),
        _rest_tool("havoc_files_list", "列出远程目录", "向目标会话下发目录列表请求并返回 path 与 entries。",
                   {"sid": _SID, "path": S("目录路径，默认 .")},
                   method="POST", path="/api/sessions/{sid}/files", body=("path",)),
        _rest_tool("havoc_files_mkdir", "新建远程目录", "在目标主机创建目录。",
                   {"sid": _SID, "path": _REMOTE_PATH},
                   method="POST", path="/api/sessions/{sid}/files/mkdir", body=("path",),
                   required=("path",)),
        _rest_tool("havoc_files_remove", "删除远程文件/目录", "删除目标主机上的文件或目录；目录需要 recursive=true。",
                   {"sid": _SID, "path": _REMOTE_PATH, "recursive": B("目录是否递归删除，默认 false")},
                   method="POST", path="/api/sessions/{sid}/files/remove",
                   body=("path", "recursive"), required=("path",), destructive=True),
        _rest_tool("havoc_files_move", "移动/重命名", "在目标主机上移动或重命名文件/目录。",
                   {"sid": _SID, "src": _REMOTE_PATH, "dst": S("目标路径"),
                    "overwrite": B("是否覆盖已存在的目标，默认 false")},
                   method="POST", path="/api/sessions/{sid}/files/move",
                   body=("src", "dst", "overwrite"), body_map={"src": "from", "dst": "to"},
                   required=("src", "dst"), destructive=True),
        _rest_tool("havoc_files_copy", "复制文件/目录", "在目标主机上复制文件或目录。",
                   {"sid": _SID, "src": _REMOTE_PATH, "dst": S("目标路径"),
                    "overwrite": B("是否覆盖已存在的目标，默认 false")},
                   method="POST", path="/api/sessions/{sid}/files/copy",
                   body=("src", "dst", "overwrite"), body_map={"src": "from", "dst": "to"},
                   required=("src", "dst")),
        _rest_tool("havoc_files_write", "写入远程文本文件", "新建或覆盖目标主机上的文本文件（最多 32 MiB，按 encoding 编码）。",
                   {"sid": _SID, "path": _REMOTE_PATH,
                    "content": S("文本内容（UTF-8 字符串）"),
                    "content_b64": S("Base64 内容（二进制安全；与 content 二选一，优先）"),
                    "encoding": S("文本编码，默认 utf-8（如 gbk）")},
                   method="POST", path="/api/sessions/{sid}/files/write",
                   body=("path", "content", "content_b64", "encoding"), required=("path",),
                   destructive=True),
        _rest_tool("havoc_files_read", "读取远程文本文件", "读取目标主机上的文本文件（默认上限 1 MiB）。",
                   {"sid": _SID, "path": _REMOTE_PATH,
                    "max": I("读取字节上限", minimum=1, maximum=16 * 1024 * 1024)},
                   path="/api/sessions/{sid}/files/read", query=("path", "max"),
                   required=("path",)),
    ]
    tools.append(_download_tool(
        "havoc_files_download", "下载远程文件",
        "通过 FS download 把目标文件取回网关，再返回给 MCP 调用方；可用 save_to 直接落盘。",
        {"sid": _SID, "path": _REMOTE_PATH},
        path="/api/sessions/{sid}/files/download", query=("path",), required=("path",),
        default_name="download.bin",
    ))
    tools.append(_upload_tool(
        "havoc_files_upload", "上传文件到会话",
        "把 MCP 服务所在主机的本地文件（或 Base64 内容）直接上传到目标会话目录（≤32 MiB）。",
        {"sid": _SID, "path": S("目标目录，默认 ."),
         "local_file": S("MCP 服务主机上的本地文件路径"),
         "content_b64": S("Base64 内容（与 local_file 二选一）"),
         "filename": S("content_b64 模式下的目标文件名")},
        path="/api/sessions/{sid}/files/upload", path_field="path", limit=MAX_LOCAL_UPLOAD,
        default_filename="upload.bin",
    ))
    tools.append(_upload_tool(
        "havoc_upload_staging", "上传到控制台暂存目录",
        "把文件保存到网关 data/uploads/<demon>/ 并返回服务器路径；供 inline-execute、dll、shellcode、dotnet、upload 等命令引用（≤64 MiB）。",
        {"sid": _SID,
         "local_file": S("MCP 服务主机上的本地文件路径"),
         "content_b64": S("Base64 内容（与 local_file 二选一）"),
         "filename": S("content_b64 模式下的目标文件名")},
        path="/api/sessions/{sid}/upload", limit=MAX_STAGING_UPLOAD,
        default_filename="upload.bin",
    ))

    # ---------------- 进程 ----------------
    tools += [
        _rest_tool("havoc_processes_cache", "进程列表缓存", "读取网关缓存的进程列表（不会向目标发命令）。",
                   {"sid": _SID}, path="/api/sessions/{sid}/processes"),
        _rest_tool("havoc_processes_list", "请求进程列表", "向会话下发 ps 请求（结果进入缓存与事件流）。",
                   {"sid": _SID}, method="POST", path="/api/sessions/{sid}/processes"),
        _rest_tool("havoc_kill_process", "结束进程", "向会话下发 proc kill <pid>。",
                   {"sid": _SID, "pid": S("目标 PID（数字字符串）")},
                   method="POST", path="/api/sessions/{sid}/processes/kill",
                   body=("pid",), required=("pid",), destructive=True),
    ]

    # ---------------- 监听器 ----------------
    tools += [
        _rest_tool("havoc_list_listeners", "监听器列表", "列出原生监听器与加密传输监听器（含在线状态、连接数、流量）。",
                   {}, path="/api/listeners"),
        _rest_tool("havoc_list_service_listeners", "服务插件监听器协议", "列出团队服务器插件注册的自定义监听器协议与字段。",
                   {}, path="/api/service-listeners"),
        _rest_tool("havoc_transport_modes", "传输模式元数据", "返回 TCP/KCP/UDP/WebSocket/DNS/DoH/DoT/OSS 等传输模式定义。",
                   {}, path="/api/transports/modes"),
    ]

    async def add_listener(client: "GatewayClient", args: dict) -> Any:
        body = {k: v for k, v in args.items() if k != "extra" and v is not None}
        extra = args.get("extra")
        if isinstance(extra, dict):
            body.update(extra)
        if not str(body.get("name") or "").strip():
            raise ValueError("监听器 name 必填")
        return await client.request_json("POST", "/api/listeners", json_body=body)

    tools.append(Tool(
        name="havoc_add_listener",
        title="创建监听器",
        description=(
            "创建原生 HTTP/HTTPS/SMB/External 监听器、加密传输监听器（protocol/mode 为 tcp/kcp/udp/ws/dns/doh/dot/oss）"
            "或服务插件监听器；额外字段通过 extra 透传。"
        ),
        input_schema=_schema(_listener_props(), ["name"]),
        handler=add_listener,
        destructive=True,
        open_world=True,
    ))

    async def edit_listener(client: "GatewayClient", args: dict) -> Any:
        name = str(args["name"])
        body = {k: v for k, v in args.items() if k not in ("name", "extra") and v is not None}
        extra = args.get("extra")
        if isinstance(extra, dict):
            body.update(extra)
        return await client.request_json(
            "PUT", f"/api/listeners/{quote(name, safe='')}", json_body=body
        )

    tools.append(Tool(
        name="havoc_edit_listener",
        title="编辑监听器",
        description="编辑 Http/Https 监听器或加密传输监听器；SMB/External/服务插件监听器需删除后重建。",
        input_schema=_schema(_listener_props(), ["name"]),
        handler=edit_listener,
        destructive=True,
        open_world=True,
    ))

    tools += [
        _rest_tool("havoc_remove_listener", "删除监听器", "删除原生、传输或服务插件监听器。",
                   {"name": _LISTENER_NAME}, method="DELETE", path="/api/listeners/{name}",
                   destructive=True, open_world=True),
        _rest_tool("havoc_start_listener", "启动传输监听器", "启动指定加密传输监听器。",
                   {"name": _LISTENER_NAME}, method="POST", path="/api/listeners/{name}/start",
                   open_world=True),
        _rest_tool("havoc_stop_listener", "停止传输监听器", "停止指定加密传输监听器。",
                   {"name": _LISTENER_NAME}, method="POST", path="/api/listeners/{name}/stop",
                   destructive=True),
    ]
    tools.append(_download_tool(
        "havoc_build_relay", "编译/下载 Go 中继器",
        "为指定传输监听器现场编译 Go 中继器并返回二进制；save_to 可直接落盘（os=windows/linux，arch=amd64/386/arm64）。",
        {"name": _LISTENER_NAME,
         "os": E("目标系统", ["windows", "linux"]),
         "arch": E("目标架构", ["amd64", "386", "arm64"]),
         "listen": S("中继器本地监听地址，默认 127.0.0.1:1080")},
        path="/api/listeners/{name}/relay", query=("os", "arch", "listen"),
        default_name="htp-relay.bin",
    ))

    # ---------------- 聊天 / 事件 / 战利品 ----------------
    tools += [
        _rest_tool("havoc_chat_history", "团队聊天记录", "返回最近 200 条团队聊天消息。",
                   {}, path="/api/chat"),
        _rest_tool("havoc_send_chat", "发送团队聊天", "以网关操作员身份发送一条团队聊天消息（≤4000 字符）。",
                   {"message": S("消息内容")}, method="POST", path="/api/chat",
                   body=("message",), required=("message",)),
        _rest_tool("havoc_events", "事件日志", "返回事件流；after 用于增量拉取（传入上次最大事件 id）。",
                   {"limit": I("返回条数，默认 500，最大 3000", minimum=1, maximum=3000),
                    "after": I("只返回 id 大于该值的事件", minimum=0)},
                   path="/api/events", query=("limit", "after")),
        _rest_tool("havoc_list_loot", "战利品列表", "列出截图/下载文件等战利品条目。",
                   {}, path="/api/loot"),
    ]
    tools.append(_download_tool(
        "havoc_download_loot", "下载战利品", "按 id 下载战利品原始文件（`download=1`），可用 save_to 落盘。",
        {"lid": I("战利品 ID", minimum=1)},
        path="/api/loot/{lid}", fixed_query={"download": "1"}, default_name="loot.bin",
    ))

    # ---------------- 载荷 ----------------
    tools += [
        _rest_tool("havoc_list_payloads", "载荷构建列表", "返回最近 20 次载荷构建记录与当前活动构建。",
                   {}, path="/api/payloads"),
    ]

    async def build_payload(client: "GatewayClient", args: dict) -> Any:
        cfg = args.get("config")
        body = {
            "listener": args.get("listener"),
            "arch": args.get("arch") or "x64",
            "format": args.get("format") or "Windows Exe",
            "config": cfg if isinstance(cfg, dict) else {},
        }
        return await client.request_json("POST", "/api/payloads/build", json_body=body)

    tools.append(Tool(
        name="havoc_build_payload",
        title="生成载荷",
        description=(
            "通过 teamserver 现场交叉编译 Demon 载荷。config 可含 sleep、jitter、spawn64、spawn32、"
            "indirect、sleep_technique、sleep_jmp、stack、proxy、amsi、alloc、execute、service_name。"
        ),
        input_schema=_schema({
            "listener": S("已在线监听器名称（不能是传输或 External 监听器）"),
            "arch": E("架构", ["x64", "x86"]),
            "format": E("格式", ["Windows Exe", "Windows Dll", "Windows Shellcode",
                                 "Windows Service Exe", "Linux Exe"]),
            "config": O("构建配置", {
                "sleep": I("Sleep 秒数", minimum=0),
                "jitter": I("Jitter 百分比", minimum=0, maximum=100),
                "spawn64": S("Spawn64 进程路径"),
                "spawn32": S("Spawn32 进程路径"),
                "indirect": B("间接系统调用"),
                "sleep_technique": S("休眠技术"),
                "sleep_jmp": S("Sleep JMP  gadget"),
                "stack": B("栈复制"),
                "proxy": S("代理加载方式"),
                "amsi": S("AMSI/ETW 补丁"),
                "alloc": S("内存分配方式"),
                "execute": S("内存执行方式"),
                "service_name": S("服务名（Windows Service Exe）"),
            }),
        }, ["listener"]),
        handler=build_payload,
        destructive=True,
        open_world=True,
    ))
    tools.append(_download_tool(
        "havoc_download_payload", "下载载荷", "下载已构建完成的载荷文件；save_to 可直接落盘。",
        {"bid": S("载荷构建 ID")},
        path="/api/payloads/{bid}/download", default_name="payload.bin",
    ))

    # ---------------- 扩展商店 / 脚本 ----------------
    tools += [
        _rest_tool("havoc_store_list", "扩展商店目录", "列出扩展商店模块；refresh=true 强制刷新上游目录。",
                   {"refresh": B("强制刷新")}, path="/api/store", query=("refresh",)),
        _rest_tool("havoc_install_extension", "安装扩展", "按商店索引安装扩展，安装后自动加载并纳入自动加载。",
                   {"index": I("商店模块索引（从 havoc_store_list 获取）", minimum=0)},
                   method="POST", path="/api/store/install", body=("index",), required=("index",),
                   destructive=True, open_world=True),
        _rest_tool("havoc_list_scripts", "脚本列表", "列出扩展脚本与客户端内置模块脚本，含加载状态、自动加载与注册命令。",
                   {}, path="/api/scripts"),
        _rest_tool("havoc_get_script", "读取脚本源码", "读取扩展/内置脚本的源码（只允许网关脚本目录内路径）。",
                   {"path": S("脚本绝对路径（取自 havoc_list_scripts）")},
                   path="/api/script", query=("path",), required=("path",)),
        _rest_tool("havoc_load_script", "加载脚本", "通过常驻 Python 宿主加载脚本并注册命令（自动加入自动加载）。",
                   {"path": S("脚本绝对路径")}, method="POST", path="/api/scripts/load",
                   body=("path",), required=("path",)),
        _rest_tool("havoc_reload_script", "重载脚本", "重新加载脚本，原子替换其旧命令注册。",
                   {"path": S("脚本绝对路径")}, method="POST", path="/api/scripts/reload",
                   body=("path",), required=("path",)),
        _rest_tool("havoc_unload_script", "卸载脚本", "移除脚本注册的命令/模块并从自动加载中删除（不删除文件）。",
                   {"path": S("脚本绝对路径")}, method="POST", path="/api/scripts/unload",
                   body=("path",), required=("path",), destructive=True),
    ]
    tools.append(_upload_tool(
        "havoc_upload_script", "上传 Python 脚本",
        "把本地 .py 脚本上传到网关扩展目录（≤2 MiB）；返回路径后可再调用 havoc_load_script。",
        {"local_file": S("MCP 服务主机上的 .py 文件路径"),
         "content_b64": S("Base64 内容（与 local_file 二选一）"),
         "filename": S("content_b64 模式下的文件名（需以 .py 结尾）")},
        path="/api/scripts/upload", limit=MAX_SCRIPT_UPLOAD, default_filename="script.py",
    ))

    # ---------------- Python 宿主 / SDK ----------------
    tools += [
        _rest_tool("havoc_python_repl", "Python 控制台执行", "在网关常驻 Python 宿主中执行代码并返回输出（拥有与网关同等的控制面权限）。",
                   {"code": S("Python 代码")}, method="POST", path="/api/python/repl",
                   body=("code",), required=("code",), destructive=True, open_world=True),
        _rest_tool("havoc_python_console", "写入会话控制台", "向指定会话的控制台追加一行（SDK 内部接口）。",
                   {"demon": _SID, "kind": E("类型", ["info", "output", "error", "good", "warn", "prompt"]),
                    "text": S("文本")},
                   method="POST", path="/api/python/console",
                   body=("demon", "kind", "text"), required=("demon", "text")),
        _rest_tool("havoc_python_command", "SDK 下发命令", "以 Python SDK 来源向会话下发命令。",
                   {"demon": _SID, "command": S("命令"), "task_id": S("可选任务 ID")},
                   method="POST", path="/api/python/command",
                   body=("demon", "command", "task_id"), required=("demon", "command"),
                   destructive=True),
        _rest_tool("havoc_python_task", "查询任务", "按 task_id 查询任意任务的状态与输出。",
                   {"task_id": S("任务 ID")}, path="/api/python/task", query=("task_id",),
                   required=("task_id",)),
        _rest_tool("havoc_python_inline_execute", "BOF 对象文件执行", "对目标会话执行 .o/BOF 对象文件（需先上传到网关脚本目录）。",
                   {"demon": _SID, "path": S("网关本机对象文件路径"),
                    "entry": S("入口函数，默认 go"), "args": S("Base64 参数"),
                    "threaded": B("是否线程执行"), "has_callback": B("是否需要回调"),
                    "task_id": S("可选任务 ID")},
                   method="POST", path="/api/python/inline-execute",
                   body=("demon", "path", "entry", "args", "threaded", "has_callback", "task_id"),
                   required=("demon", "path"), destructive=True),
        _rest_tool("havoc_python_dll_spawn", "DLL 注入执行", "对应 SDK DllSpawn：把 DLL 注入并执行。",
                   {"demon": _SID, "path": S("网关本机 DLL 路径"), "args": S("参数"),
                    "task_id": S("可选任务 ID")},
                   method="POST", path="/api/python/dll-spawn",
                   body=("demon", "path", "args", "task_id"), required=("demon", "path"),
                   destructive=True),
        _rest_tool("havoc_python_proc_create", "创建进程", "对应 SDK ProcessCreate：按 teamserver 字段顺序构造 PROC_CREATE。",
                   {"demon": _SID, "app": S("可执行文件路径"), "cmdline": S("命令行"),
                    "suspended": B("挂起创建"), "piped": B("管道回显"), "verbose": B("详细输出"),
                    "task_id": S("可选任务 ID")},
                   method="POST", path="/api/python/proc-create",
                   body=("demon", "app", "cmdline", "suspended", "piped", "verbose", "task_id"),
                   required=("demon", "app")),
        _rest_tool("havoc_python_register", "注册 Python 命令", "注册脚本命令/模块（SDK 内部接口，支持 replace 原子替换）。",
                   _python_register_props(), method="POST", path="/api/python/register",
                   body=("script", "replace", "commands", "modules"), required=("script",)),
        _rest_tool("havoc_python_autoload", "设置脚本自动加载", "把脚本加入或移出 data/scripts.json 的自动加载列表。",
                   {"path": S("脚本绝对路径"), "autoload": B("true 加入，false 移除")},
                   method="POST", path="/api/python/autoload", body=("path", "autoload"),
                   required=("path",)),
        _rest_tool("havoc_python_bof_result", "查询 BOF 回调", "按 task_id 查询 BOF 回调结果。",
                   {"task_id": S("任务 ID")}, path="/api/python/bof-result", query=("task_id",),
                   required=("task_id",)),
    ]

    # ---------------- 帮助 / 元数据 ----------------
    tools += [
        _rest_tool("havoc_help", "命令帮助", "返回 Demon 命令总表，或指定命令/子命令的中文用法。",
                   {"command": S("命令名（可选）"), "sub": S("子命令（可选）"),
                    "platform": E("平台", ["", "linux"])},
                   path="/api/help", query=("command", "sub", "platform")),
        _rest_tool("havoc_commands", "命令元数据", "返回命令/模块/子命令/用法/示例，用于补全与参数提示。",
                   {}, path="/api/commands"),
        _rest_tool("havoc_mcp_info", "MCP 服务信息", "返回 MCP 端点、协议版本、令牌文件与工具数量。",
                   {}, path="/api/mcp/info"),
    ]

    return tools


# ---------------------------------------------------------------- 资源

def _build_resources() -> list[Resource]:
    return [
        Resource("havoc://state", "全量快照", "连接、会话、监听器、事件、聊天、战利品、载荷与 Python 命令。",
                 "/api/state"),
        Resource("havoc://status", "连接状态", "teamserver 连接视图与各项计数。", "/api/status"),
        Resource("havoc://sessions", "会话列表", "当前全部会话。", "/api/sessions"),
        Resource("havoc://listeners", "监听器列表", "原生与传输监听器。", "/api/listeners"),
        Resource("havoc://service-listeners", "服务插件监听器", "插件注册的自定义监听器协议。",
                 "/api/service-listeners"),
        Resource("havoc://events", "事件日志", "最近 500 条事件。", "/api/events", {"limit": "500"}),
        Resource("havoc://chat", "团队聊天", "最近 200 条聊天消息。", "/api/chat"),
        Resource("havoc://loot", "战利品", "战利品条目。", "/api/loot"),
        Resource("havoc://payloads", "载荷构建", "最近 20 次构建记录。", "/api/payloads"),
        Resource("havoc://scripts", "脚本", "脚本列表、自动加载与已注册命令。", "/api/scripts"),
        Resource("havoc://profile", "Profile", "teamserver profile 与连接信息。", "/api/profile"),
        Resource("havoc://commands", "命令元数据", "命令/模块/子命令/用法。", "/api/commands"),
        Resource("havoc://help", "命令帮助总表", "Demon 命令帮助。", "/api/help"),
        Resource("havoc://transports", "传输模式", "加密传输模式元数据。", "/api/transports/modes"),
        Resource("havoc://mcp", "MCP 服务信息", "MCP 端点、协议版本与工具数量。", "/api/mcp/info"),
    ]


# ---------------------------------------------------------------- 提示词

def _prompt_triage(args: dict) -> str:
    sid = str(args.get("session_id") or "").strip() or "<会话 ID>"
    return (
        f"你正在通过 MCP 操作已授权的 Havoc Web 控制台。请对会话 {sid} 做一次只读排障：\n"
        f"1. 调用 havoc_get_session(sid=\"{sid}\") 查看地址、用户、系统、进程与心跳；\n"
        f"2. 调用 havoc_session_console(sid=\"{sid}\", limit=200) 查看最近回显；\n"
        f"3. 调用 havoc_session_tasks(sid=\"{sid}\") 查看最近任务是否成功；\n"
        "4. 汇总：会话是否健康、最后一条命令结果、可见的异常，以及下一步建议（不要主动下发破坏性命令）。"
    )


def _prompt_listener_review(args: dict) -> str:
    return (
        "请对当前 Havoc 监听器做一次配置审查：\n"
        "1. 调用 havoc_list_listeners 与 havoc_list_service_listeners；\n"
        "2. 对每个在线监听器检查绑定地址、回连主机、端口、TLS、代理与传输模式；\n"
        "3. 指出暴露面过大的配置（例如 0.0.0.0 回连、明文 HTTP、未启用代理）并给出收敛建议；\n"
        "4. 输出一张 Markdown 表格，最后给出优先级排序的修改清单。"
    )


def _prompt_operation_summary(args: dict) -> str:
    return (
        "请生成当前 Havoc 行动摘要：\n"
        "1. 调用 havoc_state 获取连接、会话、监听器、事件、聊天、战利品、载荷与命令注册；\n"
        "2. 统计在线/离线会话、健康会话、监听器数量、最近事件与最近载荷构建；\n"
        "3. 用 Markdown 输出：概览、会话表、监听器表、最近 10 条事件、待办与风险。\n"
        "只使用工具返回的数据，不要编造不存在的会话或文件。"
    )


def _build_prompts() -> list[Prompt]:
    return [
        Prompt("triage_session", "会话排障", "对单个会话做只读排障并汇总。",
               [{"name": "session_id", "description": "会话 ID", "required": True}],
               _prompt_triage),
        Prompt("audit_listeners", "监听器审查", "审查监听器配置与暴露面。", [],
               _prompt_listener_review),
        Prompt("operation_summary", "行动摘要", "汇总当前会话、监听器、事件与载荷状态。", [],
               _prompt_operation_summary),
    ]


def build_registry() -> Registry:
    return Registry(_build_tools(), _build_resources(), _build_prompts())
