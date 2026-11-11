#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc Web 控制台 Python SDK（在网关子进程中运行）。

脚本通过环境变量获得网关地址与内部令牌：
    HAVOC_WEB_URL   http://127.0.0.1:8081
    HAVOC_WEB_TOKEN 网关内部令牌
    HAVOC_SCRIPT    当前脚本路径（用于解析相对 .o 路径）

只支持已授权测试环境；SDK 不包含任何本地提权/执行能力，所有操作都经网关下发。
"""
from __future__ import annotations

import base64
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("HAVOC_WEB_URL", "http://127.0.0.1:8081").rstrip("/")
TOKEN = os.environ.get("HAVOC_WEB_TOKEN", "")
SCRIPT = os.environ.get("HAVOC_SCRIPT", "")

REGISTERED: list[dict] = []
REGISTERED_MODULES: list[dict] = []
# RegisterCommand 收到的函数对象（按函数名）。供常驻宿主在同一个解释器里
# 直接绑定 callable，避免依赖函数名重新 import。
REGISTERED_OBJECTS: dict[str, object] = {}
# 与客户端 RegisteredCallbacks 对齐：新会话回调列表。
EVENT_CALLBACKS: dict[str, list] = {"new_session": []}


class HavocError(Exception):
    pass


class Packer:
    """与 client/Modules/Packer/packer.py 一致的 BOF 参数打包器。"""

    def __init__(self):
        self.buffer: bytes = b""
        self.size: int = 0

    def getbuffer(self) -> bytes:
        from struct import pack
        return pack("<L", self.size) + self.buffer

    def addstr(self, s):
        from struct import pack, calcsize
        if s is None:
            s = ""
        if isinstance(s, str):
            s = s.encode("utf-8")
        fmt = "<L{}s".format(len(s) + 1)
        self.buffer += pack(fmt, len(s) + 1, s)
        self.size += calcsize(fmt)

    def addWstr(self, s):
        from struct import pack, calcsize
        if s is None:
            s = ""
        s = s.encode("utf-16_le")
        fmt = "<L{}s".format(len(s) + 2)
        self.buffer += pack(fmt, len(s) + 2, s)
        self.size += calcsize(fmt)

    def addbytes(self, b):
        from struct import pack, calcsize
        if b is None:
            b = b""
        fmt = "<L{}s".format(len(b))
        self.buffer += pack(fmt, len(b), b)
        self.size += calcsize(fmt)

    def addbool(self, b):
        from struct import pack
        self.buffer += pack("<I", 1 if b else 0)
        self.size += 4

    def adduint32(self, n):
        from struct import pack
        self.buffer += pack("<I", n)
        self.size += 4

    def addint(self, dint):
        from struct import pack
        self.buffer += pack("<i", dint)
        self.size += 4

    def addshort(self, n):
        from struct import pack
        self.buffer += pack("<h", n)
        self.size += 2


def _req(method: str, path: str, payload=None, timeout: float = 30.0):
    data = None
    headers = {"X-Python-Token": TOKEN}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise HavocError(f"HTTP {exc.code}: {text}") from exc
    except Exception as exc:  # URLError / timeout
        raise HavocError(str(exc)) from exc


def _req_bytes(method: str, path: str, timeout: float = 60.0) -> bytes:
    """下载二进制（载荷/战利品）。与 _req 共用内部令牌鉴权。"""
    req = urllib.request.Request(BASE + path, method=method,
                                 headers={"X-Python-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise HavocError(f"HTTP {exc.code}: {text}") from exc
    except Exception as exc:
        raise HavocError(str(exc)) from exc


def _error_status(exc: Exception) -> int:
    text = str(exc)
    if text.startswith("HTTP "):
        try:
            return int(text.split()[1].rstrip(":"))
        except Exception:
            return 0
    return 0


def _task_id() -> str:
    return f"{random.getrandbits(32):08X}"


def _resolve(path: str) -> str:
    if os.path.isabs(path):
        return path
    base = os.path.dirname(SCRIPT) if SCRIPT else os.getcwd()
    return os.path.join(base, path)


def list_sessions() -> list[dict]:
    return _req("GET", "/api/sessions").get("sessions", [])


def get_session(demon_id: str) -> dict:
    return _req("GET", f"/api/sessions/{urllib.parse.quote(demon_id)}").get("session", {})


def chat(message: str) -> None:
    _req("POST", "/api/chat", {"message": message})


# ---------------------------------------------------------------- 全局 API


def GetDemons() -> list[str]:
    """返回当前所有 Demon 会话 ID（客户端签名）。"""
    return [str(item.get("id", "")) for item in list_sessions() if item.get("id")]


def GetListeners() -> list[str]:
    """返回监听器名称列表。"""
    data = _req("GET", "/api/listeners")
    return [str(item.get("name", "")) for item in data.get("listeners", []) if item.get("name")]


def GetAgents() -> list[str]:
    """返回可用的 Agent 类型。

    本批只实现 Demon；第三方 Agent 的元数据/命令表不在本轮范围
    （见 HANDOFF 的“明确不在本批”清单），因此这里固定返回 ["Demon"]。
    """
    return ["Demon"]


def LoadScript(path: str) -> bool:
    """加载脚本并注册命令（对应客户端 havoc.LoadScript）。

    只允许网关 extensions/Modules 目录内的路径；越界会抛 HavocError 而不是静默
    返回 False。普通加载失败（语法错误等）返回 False。

    在常驻宿主内运行时直接导入脚本（不能回调网关 /api/scripts/load，否则会等待
    正在执行本条代码的宿主，形成死锁）；其它场景仍走网关 API。
    """
    target = _resolve(str(path))
    if not os.path.isfile(target):
        return False
    local_loader = globals().get("LOAD_SCRIPT_LOCAL")
    if callable(local_loader):
        try:
            # 先用只读接口做路径白名单校验（不会回调宿主，无死锁风险）
            _req("GET", "/api/script?path=" + urllib.parse.quote(target), timeout=15)
        except HavocError as exc:
            if _error_status(exc) == 403:
                raise HavocError(
                    f"LoadScript 路径不允许：{target}（Web 只允许 webui/data/extensions 或 "
                    f"havoc-src/client/Modules 内的脚本）") from exc
            return False
        result = local_loader(target)
        if not result.get("ok"):
            return False
        try:
            _req("POST", "/api/python/autoload", {"path": target, "autoload": True}, timeout=15)
        except HavocError:
            pass
        return True
    try:
        _req("POST", "/api/scripts/load", {"path": target}, timeout=90)
        return True
    except HavocError as exc:
        if _error_status(exc) == 403:
            raise HavocError(
                f"LoadScript 路径不允许：{target}（Web 只允许 webui/data/extensions 或 "
                f"havoc-src/client/Modules 内的脚本）") from exc
        return False


def GeneratePayload(callback, agent: str, listener: str, arch: str, format: str,
                    config) -> None:
    """异步构建载荷；完成后 callback 收到 base64 字符串（对齐 Packager.cc:558）。

    注意：Web SDK 是同步 HTTP，本函数会轮询到构建结束再调用 callback（总超时 180s）。
    config 可以是 JSON 字符串或 dict。agent 目前只支持 Demon。
    """
    if agent != "Demon":
        raise HavocError(f"Web SDK 目前只支持 Demon 载荷，收到 agent={agent!r}")
    if not callable(callback):
        raise TypeError("callback 必须可调用")
    cfg = config
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg) if cfg.strip() else {}
        except ValueError as exc:
            raise HavocError(f"config 不是合法 JSON：{exc}") from exc
    if not isinstance(cfg, dict):
        raise HavocError("config 必须是 JSON 对象字符串或 dict")
    try:
        data = _req("POST", "/api/payloads/build", {
            "listener": listener, "agent": agent, "arch": arch,
            "format": format, "config": cfg,
        }, timeout=30)
    except HavocError as exc:
        status = _error_status(exc)
        if status == 409:
            raise HavocError("已有载荷正在构建，请等待当前构建结束后重试（HTTP 409）") from exc
        raise
    build_id = str((data.get("build") or {}).get("id", ""))
    if not build_id:
        raise HavocError("网关没有返回 build id")
    deadline = time.time() + 180
    last_error = ""
    while time.time() < deadline:
        builds = _req("GET", "/api/payloads").get("builds", [])
        build = next((b for b in builds if str(b.get("id")) == build_id), None)
        if build is None:
            last_error = "构建记录丢失"
        elif build.get("status") == "done":
            raw = _req_bytes("GET", f"/api/payloads/{urllib.parse.quote(build_id)}/download")
            callback(base64.b64encode(raw).decode())
            return
        elif build.get("status") == "error":
            raise HavocError(f"载荷构建失败：{build.get('error') or '未知错误'}")
        time.sleep(0.7)
    raise HavocError(f"载荷构建超时（180s）：{last_error or build_id}")


# ---------------------------------------------------------------- Agent


class Agent:
    """第三方/服务型 Agent 的兼容对象。

    本批只提供元数据与 ConsoleWrite；Command 依赖第三方 Agent 命令表（不在本批），
    会抛出明确的 NotImplementedError，避免静默 no-op。
    """

    CONSOLE_INFO = 1
    CONSOLE_ERROR = 2
    CONSOLE_TASK = 3

    def __init__(self, agent_id: str):
        info = {}
        try:
            info = _req("GET", f"/api/sessions/{urllib.parse.quote(agent_id)}").get("session", {})
        except HavocError:
            info = {}
        if not info:
            raise HavocError(f"Agent 不存在：{agent_id}")
        self.AgentID = agent_id
        self.NameID = agent_id
        self.Listener = info.get("listener", "")
        self.ExternalIP = info.get("external_ip", "")
        self.InternalIP = info.get("internal_ip", "")
        self.User = info.get("user", "")
        self.Username = self.User
        self.Computer = info.get("host", "")
        self.Hostname = self.Computer
        self.Domain = info.get("domain", "")
        self.OS = info.get("os", "")
        self.OSBuild = info.get("os_build", "")
        self.OSArch = info.get("os_arch", "")
        self.ProcessName = info.get("process", "")
        self.ProcessID = str(info.get("pid", ""))
        self.ProcessArch = info.get("proc_arch", "")
        self.MagicValue = info.get("magic", "")
        self.AgentType = info.get("agent_type", "")

    def ConsoleWrite(self, type: int, message: str) -> str:
        kind = {self.CONSOLE_INFO: "info", self.CONSOLE_ERROR: "error",
                self.CONSOLE_TASK: "task"}.get(type, "info")
        try:
            _req("POST", "/api/python/console",
                 {"demon": self.AgentID, "kind": kind, "text": str(message)})
        except HavocError:
            pass
        return _task_id()

    def Command(self, task_id: str, name: str, data: bytes):
        raise NotImplementedError(
            "Web SDK 尚未实现第三方 Agent 命令表（不在本批范围）："
            f"Agent.Command({task_id!r}, {name!r}, ...)")


# ---------------------------------------------------------------- 事件


def RegisterCallback(callback) -> None:
    """注册新会话回调（与客户端 RegisteredCallbacks 一致）。"""
    if not callable(callback):
        raise TypeError("callback 必须可调用")
    EVENT_CALLBACKS.setdefault("new_session", []).append(callback)


class Event:
    @staticmethod
    def OnNewSession(callback) -> None:
        RegisterCallback(callback)


class Demon:
    """与 Havoc 客户端 PyDemonClass 兼容的会话对象（常用子集）。"""

    CONSOLE_INFO = 1
    CONSOLE_ERROR = 2
    CONSOLE_TASK = 3

    def __init__(self, demon_id: str):
        info = get_session(demon_id)
        if not info:
            raise HavocError(f"会话不存在：{demon_id}")
        self._info = info
        self.DemonID = demon_id
        self.NameID = demon_id
        self.Listener = info.get("listener", "")
        self.ExternalIP = info.get("external_ip", "")
        self.InternalIP = info.get("internal_ip", "")
        self.User = info.get("user", "")
        self.Username = self.User
        self.Computer = info.get("host", "")
        self.Hostname = self.Computer
        self.Domain = info.get("domain", "")
        self.OS = info.get("os", "")
        self.OSBuild = info.get("os_build", "")
        self.OSArch = info.get("os_arch", "")
        self.ProcessName = info.get("process", "")
        self.ProcessID = str(info.get("pid", ""))
        self.PID = self.ProcessID
        self.ProcessArch = info.get("proc_arch", "")
        self.MagicValue = info.get("magic", "")
        self.Elevated = bool(info.get("elevated"))
        self.SleepDelay = info.get("sleep", 0)
        self.SleepJitter = info.get("jitter", 0)
        self.LastCallIn = info.get("last_call", "")
        self.Marked = "Alive" if info.get("active") else "Dead"

    # ---- 控制台输出 ----
    def ConsoleWrite(self, type: int, message: str) -> str:
        kind = {self.CONSOLE_INFO: "info", self.CONSOLE_ERROR: "error", self.CONSOLE_TASK: "task"}.get(type, "info")
        try:
            _req("POST", "/api/python/console", {"demon": self.DemonID, "kind": kind, "text": str(message)})
        except HavocError:
            pass
        return _task_id()

    # ---- 命令 ----
    def Command(self, task_id: str, command: str) -> None:
        _req("POST", "/api/python/command",
             {"demon": self.DemonID, "command": command, "task_id": task_id or _task_id()})

    def CommandGetOutput(self, task_id: str, command: str, callback) -> None:
        task_id = task_id or _task_id()
        self.Command(task_id, command)
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                data = _req("GET", f"/api/python/task?task_id={urllib.parse.quote(task_id)}")
                task = data.get("task") or {}
                if task.get("completed"):
                    callback(task.get("output", ""))
                    return
            except HavocError:
                pass
            time.sleep(0.6)

    def Shell(self, command: str) -> str:
        task_id = _task_id()
        self.Command(command=command, task_id=task_id)
        return task_id

    def Sleep(self, seconds: int, jitter: int = 0) -> str:
        task_id = _task_id()
        self.Command(task_id, f"sleep {int(seconds)} {int(jitter)}")
        return task_id

    def Checkin(self) -> str:
        task_id = _task_id()
        self.Command(task_id, "checkin")
        return task_id

    def Pwd(self) -> str:
        task_id = _task_id()
        self.Command(task_id, "pwd")
        return task_id

    def Ls(self, path: str = ".") -> str:
        task_id = _task_id()
        self.Command(task_id, f"dir {path}")
        return task_id

    def Cd(self, path: str) -> str:
        task_id = _task_id()
        self.Command(task_id, f"cd {path}")
        return task_id

    def Cat(self, path: str) -> str:
        task_id = _task_id()
        self.Command(task_id, f"cat {path}")
        return task_id

    def Download(self, path: str) -> str:
        task_id = _task_id()
        self.Command(task_id, f"download {path}")
        return task_id

    def Screenshot(self) -> str:
        task_id = _task_id()
        self.Command(task_id, "screenshot")
        return task_id

    def Ps(self) -> str:
        task_id = _task_id()
        self.Command(task_id, "ps")
        return task_id

    # ---- BOF ----
    def InlineExecute(self, task_id: str, entry: str, path: str, args: bytes, threaded: bool) -> None:
        task_id = task_id or _task_id()
        payload = {
            "demon": self.DemonID,
            "task_id": task_id,
            "entry": entry,
            "path": _resolve(path),
            "args": base64.b64encode(args or b"").decode(),
            "threaded": bool(threaded),
            "has_callback": False,
        }
        _req("POST", "/api/python/inline-execute", payload, timeout=60)

    def InlineExecuteGetOutput(self, callback, entry: str, path: str, args: bytes) -> str:
        task_id = _task_id()
        payload = {
            "demon": self.DemonID,
            "task_id": task_id,
            "entry": entry,
            "path": _resolve(path),
            "args": base64.b64encode(args or b"").decode(),
            "threaded": False,
            "has_callback": True,
        }
        _req("POST", "/api/python/inline-execute", payload, timeout=60)
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                data = _req("GET", f"/api/python/bof-result?task_id={urllib.parse.quote(task_id)}")
                if data.get("ready"):
                    result = data.get("result") or {}
                    worked = str(result.get("worked", "")).lower() == "true"
                    try:
                        callback(self.DemonID, task_id, worked, result.get("output", ""), result.get("error", ""))
                    except Exception as exc:  # 回调异常不能拖垮 SDK
                        self.ConsoleWrite(self.CONSOLE_ERROR, f"BOF 回调异常：{exc}")
                    return task_id
            except HavocError:
                pass
            time.sleep(0.5)
        self.ConsoleWrite(self.CONSOLE_ERROR, f"BOF 输出超时：{path}")
        return task_id

    # 兼容客户端 API 中的未实现方法：给出明确错误而不是静默失败
    def DllSpawn(self, task_id: str, path: str, args=b"") -> str:
        """对应客户端 CommandSend.cc::DllSpawn：INJECT_DLL_SPAWN + base64(binary/args)。"""
        task_id = task_id or _task_id()
        payload = {
            "demon": self.DemonID,
            "task_id": task_id,
            "path": _resolve(path),
            "args": base64.b64encode(bytes(args or b"")).decode(),
        }
        _req("POST", "/api/python/dll-spawn", payload, timeout=60)
        return task_id

    def DllInject(self, task_id: str, pid: str, dll_path: str, dll_args="") -> str:
        """对应客户端 Demon.DllInject(TaskID, Pid, DllPath, DllArgs) → dll inject。"""
        task_id = task_id or _task_id()
        if isinstance(dll_args, (bytes, bytearray)):
            dll_args = bytes(dll_args).decode("utf-8", "replace")
        extra = f" {dll_args}".rstrip() if dll_args else ""
        self.Command(task_id, f"dll inject {int(pid)} {_resolve(str(dll_path))}{extra}")
        return task_id

    def DotnetInlineExecute(self, task_id: str, path: str, arguments: str = "") -> str:
        """对应客户端 Demon.DotnetInlineExecute(TaskID, Path, Args) → dotnet inline-execute。"""
        task_id = task_id or _task_id()
        if isinstance(arguments, (bytes, bytearray)):
            arguments = bytes(arguments).decode("utf-8", "replace")
        extra = f" {arguments}".rstrip() if arguments else ""
        self.Command(task_id, f"dotnet inline-execute {_resolve(str(path))}{extra}")
        return task_id

    def ProcessCreate(self, task_id: str, app: str, cmdline: str, suspended: bool = False,
                      piped: bool = True, verbose: bool = False) -> str:
        """对应客户端 Demon.ProcessCreate(...)。

        teamserver 解析顺序是 state;verbose;piped;app;b64(cmdline)
        （teamserver/pkg/agent/demons.go: DEMON_COMMAND_PROC_CREATE）。客户端
        PyDemonClass.cc 的拼接顺序与 teamserver 不一致，这里按 teamserver 修正。
        """
        task_id = task_id or _task_id()
        _req("POST", "/api/python/proc-create", {
            "demon": self.DemonID,
            "task_id": task_id,
            "app": str(app),
            "cmdline": str(cmdline),
            "suspended": bool(suspended),
            "piped": bool(piped),
            "verbose": bool(verbose),
        })
        return task_id

    def ShellcodeSpawn(self, task_id: str, technique: str, arch: str, path: str,
                       args=b"") -> str:
        """对应客户端 Demon.ShellcodeSpawn(TaskID, Technique, Arch, Path, Args)。"""
        task_id = task_id or _task_id()
        if isinstance(args, (bytes, bytearray)):
            text_args = bytes(args).decode("utf-8", "replace")
        else:
            text_args = str(args or "")
        extra = f" {text_args}".rstrip() if text_args else ""
        self.Command(task_id,
                     f"shellcode spawn {arch} {_resolve(str(path))}{extra}")
        return task_id


def RegisterCommand(function, module, command, description, behavior=0, usage="", example="", agent="Demon"):
    """兼容 Havoc 客户端签名：RegisterCommand(func, module, command, desc, behavior, usage, example)."""
    name = getattr(function, "__name__", str(function))
    REGISTERED.append({
        "function": name,
        "module": module or "",
        "command": command,
        "description": description or "",
        "behavior": behavior,
        "usage": usage or "",
        "example": example or "",
        "agent": agent or "Demon",
    })
    if callable(function):
        REGISTERED_OBJECTS[name] = function


def RegisterModule(name, description="", behavior="", usage="", example="", options="", *args, **kwargs):
    """兼容客户端 RegisterModule(Name, Description, Behavior, Usage, Example, Options)。"""
    REGISTERED_MODULES.append({
        "name": name or "",
        "description": description or "",
        "behavior": behavior or "",
        "usage": usage or "",
        "example": example or "",
        "options": options or "",
        "agent": kwargs.get("agent", "Demon"),
    })
    return None


__all__ = [
    "Demon", "Agent", "Event", "HavocError", "Packer", "RegisterCommand", "RegisterModule",
    "RegisterCallback", "LoadScript", "GetDemons", "GetListeners", "GetAgents",
    "GeneratePayload", "list_sessions", "get_session", "chat", "REGISTERED",
]
