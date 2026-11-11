#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc 命令构造：把控制台输入解析成 Event=7/SubEvent=3 的 Info 字段。

字段来源以上游客户端源码 CommandSend.cc / ConsoleInput.cc 为准（Qt 客户端已移除，
这两份源码不再随仓库提供），teamserver 侧解析见 teamserver/pkg/agent/demons.go:TaskPrepare。

命令元数据（help / Usage / Example / 子命令）来自：
  - webui/reference/Commands.cc（上游 DemonCommandList 快照）
  - webui/reference/client-config.toml 列出的 Python 模块（脚本本体仍在
    havoc-src/client/Modules，由 Web 网关复用）
生成结果保存在 demon_commands.py（生成器：webui/tools/gen_demon_commands.py）。
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from demon_help_zh import description_for, sub_description_for
from demon_commands import (
    MODULE_BY_NAME,
    MODULE_SCRIPTS,
    REGISTERED_BY_KEY,
    REGISTERED_COMMANDS,
    REGISTERED_MODULES,
    STATIC_BY_NAME,
    STATIC_COMMANDS,
)


class CommandError(ValueError):
    """命令无法构造（语法错误/不支持）。"""


# teamserver/pkg/agent/commands.go / client/include/Havoc/DemonCmdDispatch.h
CMD_CHECKIN = 100
CMD_SLEEP = 11
CMD_PROC_LIST = 12
CMD_FS = 15
CMD_INLINE_EXECUTE = 20
CMD_JOB = 21
CMD_INJECT_DLL = 22
CMD_INJECT_SHELLCODE = 24
CMD_INJECT_DLL_SPAWN = 26
CMD_TOKEN = 40
CMD_PROC = 0x1010
CMD_INLINE_EXECUTE_ASSEMBLY = 0x2001
CMD_ASSEMBLY_LIST_VERSIONS = 0x2003
CMD_NET = 2100
CMD_CONFIG = 2500
CMD_SCREENSHOT = 2510
CMD_PIVOT = 2520
CMD_TRANSFER = 2530
CMD_SOCKET = 2540
CMD_KERBEROS = 2550
CMD_EXIT = 92

# DEMON_COMMAND_PROC_*
PROC_MODULES = 2
PROC_GREP = 3
PROC_CREATE = 4
PROC_BLOCKDLL = 5
PROC_MEMORY = 6
PROC_KILL = 7

CMD_EXE = r"c:\windows\system32\cmd.exe"
PS_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = Path(__file__).resolve().parent / "data" / "uploads"
EXTENSIONS_DIR = Path(__file__).resolve().parent / "data" / "extensions"

MAX_FILE_BYTES = 32 * 1024 * 1024

# Web 端允许读取的本地路径根（客户端本身允许任意本地路径；Web 只开放上传目录、
# 项目目录与 /tmp，避免控制台成为一个任意文件读取接口）。
_ALLOWED_FILE_ROOTS = [
    PROJECT_ROOT,
    Path("/tmp"),
]


def b64_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64(text: str) -> str:
    return b64_bytes(text.encode("utf-8"))


def _local_b64(text: str) -> str:
    """模拟 Qt 的 toLocal8Bit().toBase64()。"""
    return b64_bytes(text.encode("utf-8", "surrogateescape"))


@dataclass
class Built:
    info: dict
    note: str = ""
    # 前端展示用：任务完成提示
    kind: str = "task"
    # 客户端风格的 “[*] [TaskID] Tasked demon to …” 信息
    message: str = ""


@dataclass
class SessionRef:
    """构造命令所需的最小会话信息。"""
    name: str
    os: str = ""
    platform: str = ""
    arch: str = ""
    user: str = ""
    host: str = ""
    agent_type: str = "Demon"
    # 第三方 Agent 的 AgentData.Commands（Name/Description/Help/Params）
    commands: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 命令行解析：与 ConsoleInput.cc::ParseCommandLine 保持一致
# ---------------------------------------------------------------------------

def parse_argv(line: str) -> list[str]:
    """按客户端的规则切分参数：双引号包裹、反斜杠转义、引号后紧跟空格断词。"""
    cmdline = line or ""
    out: list[str] = []
    parsed: list[str] = []
    in_quotes = False
    i = 0
    n = len(cmdline)
    while i < n:
        c = cmdline[i]
        nxt = cmdline[i + 1] if i + 1 < n else ""
        nn = cmdline[i + 2] if i + 2 < n else ""
        if c == '"' and not in_quotes:
            in_quotes = True
        elif c == '"' and in_quotes:
            in_quotes = False
            if nxt != " ":
                out.append("".join(parsed))
                parsed = []
        elif c == "\\":
            if nxt == "\\":
                # \\ 后跟空格/引号 = 转义反斜杠；否则保留两个反斜杠（UNC 路径）
                if nn == " " or nn == '"':
                    parsed.append("\\")
                    i += 1
                else:
                    parsed.append("\\")
            else:
                parsed.append("\\")
        elif c == " " and not in_quotes:
            if parsed:
                out.append("".join(parsed))
                parsed = []
        else:
            parsed.append(c)
        i += 1
    if parsed:
        out.append("".join(parsed))
    return out


def _join_rest(line: str) -> str:
    """取命令行中去掉第一个 token 之后的原始文本（保留引号/空格）。"""
    stripped = (line or "").strip()
    head = stripped.split(None, 1)
    return head[1] if len(head) > 1 else ""


def _join(argv: list[str], index: int) -> str:
    return " ".join(argv[index:])


def _need(argv: list[str], count: int, message: str = "Not enough arguments") -> None:
    if len(argv) < count:
        raise CommandError(message)


# ---------------------------------------------------------------------------
# 本地文件（Web 上传）
# ---------------------------------------------------------------------------

def resolve_local_file(path_text: str) -> Path:
    """把命令里的本地路径解析成可读文件。

    - 绝对路径：必须在项目目录或 /tmp 下；
    - 相对路径：依次尝试运行目录、上传目录、扩展目录、项目根目录；
    - 仅文件名：在上传目录里按 basename 查找。
    """
    raw = (path_text or "").strip().strip('"')
    if not raw:
        raise CommandError("No file specified")
    candidate = Path(os.path.expanduser(raw))
    candidates: list[Path] = []
    if candidate.is_absolute():
        candidates.append(candidate)
    else:
        candidates.extend([
            Path.cwd() / candidate,
            UPLOAD_DIR / candidate,
            EXTENSIONS_DIR / candidate,
            PROJECT_ROOT / candidate,
        ])
        if "/" not in raw and "\\" not in raw and UPLOAD_DIR.is_dir():
            for p in UPLOAD_DIR.rglob(candidate.name):
                if p.is_file():
                    candidates.append(p)

    for p in candidates:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if not rp.is_file():
            continue
        if not _path_allowed(rp):
            continue
        if rp.stat().st_size > MAX_FILE_BYTES:
            raise CommandError(f"File is too large: {raw}")
        return rp
    raise CommandError(f"File not found: {raw}")


def _path_allowed(path: Path) -> bool:
    for root in _ALLOWED_FILE_ROOTS:
        try:
            path.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def read_local_file(path_text: str, not_found: Optional[str] = None) -> bytes:
    try:
        p = resolve_local_file(path_text)
        return p.read_bytes()
    except OSError as exc:
        raise CommandError(f"File not found: {path_text}") from exc
    except CommandError as exc:
        if not_found and str(exc).startswith("File not found"):
            raise CommandError(f"{not_found}: {path_text}") from exc
        raise


# ---------------------------------------------------------------------------
# 包构造辅助
# ---------------------------------------------------------------------------

def _proc(info: dict, subcommand: int, args: str) -> None:
    info.update({
        "CommandID": str(CMD_PROC),
        "ProcCommand": str(subcommand),
        "Args": args,
    })


def _fs(info: dict, subcommand: str, args: str) -> None:
    info.update({
        "CommandID": str(CMD_FS),
        "SubCommand": subcommand,
        "Arguments": args,
    })


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _is_linux(session: "SessionRef") -> bool:
    return ("linux" in (session.os or "").lower()
            or (session.platform or "").lower() == "linux")


def _is_windows(session: "SessionRef") -> bool:
    os_name = (session.os or "").lower()
    return os_name.startswith("windows") or "windows" in os_name


# ---------------------------------------------------------------------------
# build()
# ---------------------------------------------------------------------------

def build(session: SessionRef, line: str, task_id: str, agent_type: str = "Demon",
          shell_utf8: bool = False) -> Built:
    line = (line or "").strip()
    if not line:
        raise CommandError("命令为空")

    argv = parse_argv(line)
    if not argv:
        raise CommandError("命令为空")
    cmd = argv[0].lower()
    rest = _join_rest(line)

    info: dict = {
        "TaskID": task_id,
        "CommandLine": line,
        "DemonID": session.name,
    }

    note = ""
    message = ""

    # ------------------------------------------------------------------ sleep
    if cmd == "sleep":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        if len(argv) > 3:
            raise CommandError("Too many arguments")
        try:
            delay = int(argv[1], 10)
        except ValueError as exc:
            raise CommandError("Invalid delay") from exc
        if delay < 0:
            raise CommandError('"sleep" doesn\'t support negative delays')
        jitter = "0"
        if len(argv) == 3:
            try:
                jitter_f = float(argv[2])
            except ValueError as exc:
                raise CommandError("Invalid jitter") from exc
            if jitter_f < 0:
                raise CommandError('"sleep" doesn\'t support negative jitters')
            if jitter_f > 100:
                raise CommandError("The jitter can't be larger than 100")
            # 客户端原样把用户输入拼进 Arguments（teamserver 用 Atoi 解析）。
            jitter = argv[2]
        info.update({"CommandID": str(CMD_SLEEP), "Arguments": f"{delay};{jitter}"})
        message = f"Tasked demon to sleep for {delay} seconds" + (
            f" with {jitter}% jitter" if len(argv) == 3 else "")

    elif cmd == "interactive":
        if len(argv) > 1:
            raise CommandError("Too many arguments")
        info.update({"CommandID": str(CMD_SLEEP), "Arguments": "0;0"})
        message = "Tasked demon to enter interactive mode"

    # ---------------------------------------------------------------- checkin
    elif cmd == "checkin":
        info.update({"CommandID": str(CMD_CHECKIN)})
        message = "Tasked demon send back a checkin request"

    # ------------------------------------------------------------------- task
    elif cmd == "task":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        sub = argv[1]
        if sub == "list":
            info.update({"CommandID": "Teamserver", "Command": "task::list"})
            message = "Tasked teamserver to list commands in task queue"
        elif sub == "clear":
            info.update({"CommandID": "Teamserver", "Command": "task::clear"})
            message = "Tasked teamserver to clear all commands from task queue"
        else:
            raise CommandError(f"Sub command '{sub}' in 'task' not found found")

    # -------------------------------------------------------------------- job
    elif cmd == "job":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        sub = argv[1]
        if sub == "list":
            info.update({"CommandID": str(CMD_JOB), "Command": "list", "Param": "0"})
            message = "Tasked demon to list jobs"
        elif sub in ("suspend", "resume", "kill"):
            if len(argv) < 3:
                raise CommandError("Not enough arguments")
            info.update({"CommandID": str(CMD_JOB), "Command": sub, "Param": argv[2]})
            message = f"Tasked demon to {sub} job: {argv[2]}"
        else:
            raise CommandError(f"Sub command not found: {sub}")

    # -------------------------------------------------------------------- dir
    elif cmd in ("dir", "ls"):
        path = "."
        subdirs = files_only = dirs_only = list_only = False
        starts = contains = ends = ""
        if len(argv) > 1:
            path = argv[1]
            i = 2
            while i < len(argv):
                flag = argv[i]
                if flag == "/s":
                    subdirs = True
                elif flag == "/f":
                    files_only = True
                elif flag == "/d":
                    dirs_only = True
                elif flag == "/b":
                    list_only = True
                elif flag in ("/starts", "/contains", "/ends"):
                    if i + 1 >= len(argv):
                        raise CommandError("Not enough arguments")
                    value = argv[i + 1]
                    if flag == "/starts":
                        starts = value
                    elif flag == "/contains":
                        contains = value
                    else:
                        ends = value
                    i += 1
                else:
                    raise CommandError(f"Unknown parameter {flag}")
                i += 1
            if files_only and dirs_only:
                raise CommandError("Cannot set both /f and /d")
            message = f"Tasked demon to list {path}"
        else:
            message = "Tasked demon to list current directory"
        _fs(info, "dir", ";".join([
            path, _bool(subdirs), _bool(files_only), _bool(dirs_only),
            _bool(list_only), starts, contains, ends,
        ]))

    elif cmd == "cd":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        path = _join(argv, 1)
        _fs(info, "cd", path)
        message = f"Tasked demon to change directory: {path}"

    elif cmd == "cp":
        if len(argv) < 3:
            raise CommandError("Not enough arguments")
        src, dst = argv[1], _join(argv, 2)
        _fs(info, "cp", _local_b64(src) + ";" + _local_b64(dst))
        message = f"Tasked demon to copy file {src} to {dst}"

    elif cmd in ("mv", "move"):
        if len(argv) < 3:
            raise CommandError("Not enough arguments")
        src, dst = argv[1], _join(argv, 2)
        _fs(info, "mv", _local_b64(src) + ";" + _local_b64(dst))
        message = f"Tasked demon to move file {src} to {dst}"

    elif cmd == "remove":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        path = _join(argv, 1)
        _fs(info, "remove", path)
        message = f"Tasked demon to remove file or directory: {path}"

    elif cmd == "mkdir":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        path = _join(argv, 1)
        _fs(info, "mkdir", path)
        message = f"Tasked demon to create new directory: {path}"

    elif cmd == "pwd":
        _fs(info, "pwd", "")
        message = "Tasked demon to get current working directory"

    # ------------------------------------------------------------------ shell
    elif cmd == "shell":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        if _is_linux(session):
            _proc(info, PROC_CREATE, "0;FALSE;TRUE;/bin/sh;" + b64("-c " + rest))
            note = "通过 /bin/sh 执行"
            message = "Tasked demon to execute a shell command"
            return Built(info=info, note=note, message=message)
        payload = rest
        if shell_utf8:
            # 中文版 Windows 的 cmd/系统工具默认输出 GBK，teamserver 的 JSON 序列化会把
            # 非 UTF-8 字节替换成 U+FFFD（不可逆）。先切到 65001 代码页即可拿到正常中文。
            payload = "chcp 65001>nul & " + rest
            note = "通过 cmd.exe 执行（已切换 UTF-8 代码页）"
        else:
            note = "通过 cmd.exe 执行"
        _proc(info, PROC_CREATE, "0;FALSE;TRUE;" + CMD_EXE + ";" + b64("/c " + payload))
        message = "Tasked demon to execute a shell command"

    elif cmd == "powershell":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        if _is_linux(session):
            raise CommandError("Linux 会话没有 powershell，请使用 shell/bash")
        _proc(info, PROC_CREATE, "0;FALSE;TRUE;" + PS_EXE + ";" + b64("-C " + rest))
        note = "通过 powershell.exe 执行"
        message = "Tasked demon to execute a powershell command/script"

    # ------------------------------------------------------------------- proc
    elif cmd == "proc":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        sub = argv[1]
        if sub == "list":
            info.update({"CommandID": str(CMD_PROC_LIST), "FromProcessManager": "false"})
            message = "Tasked demon to enumerate and list all processes"
        elif sub == "modules":
            _need(argv, 3, "Not enough arguments")
            _proc(info, PROC_MODULES, argv[2])
            message = "Tasked demon to list all modules/dll of a remote process"
        elif sub == "grep":
            _need(argv, 3, "Not enough arguments")
            _proc(info, PROC_GREP, argv[2])
            message = "Tasked demon to grep information about the specified process"
        elif sub == "create":
            if len(argv) < 4:
                raise CommandError("Not enough arguments")
            index = 2
            flag = argv[index]
            if flag == "normal":
                flags = "0"
            elif flag == "suspended":
                flags = "4"
            else:
                raise CommandError(f"Process creation flag not found: {flag}")
            index += 1
            verbose = "TRUE"
            if index < len(argv) and argv[index] == "--silent":
                verbose = "FALSE"
                index += 1
            piped = "TRUE"
            if index < len(argv) and argv[index] == "--no-pipe":
                piped = "FALSE"
                index += 1
            if index >= len(argv):
                raise CommandError("Not enough arguments")
            program = argv[index]
            index += 1
            args = '"' + program + '"'
            for item in argv[index:]:
                args += " " + item
            _proc(info, PROC_CREATE, flags + ";" + verbose + ";" + piped + ";" + program + ";" + b64(args))
            message = ("Tasked demon to spawn a process in suspended state: " if flags == "4"
                       else "Tasked demon to spawn a process: ") + program
        elif sub == "blockdll":
            _need(argv, 3, "Not enough arguments")
            value = argv[2]
            if value not in ("on", "off"):
                raise CommandError("Argument not valid")
            _proc(info, PROC_BLOCKDLL, value)
            message = ("Tasked demon to enable blocking non microsoft signed dlls" if value == "on"
                       else "Tasked demon to disable blocking non microsoft signed dlls")
        elif sub == "kill":
            _need(argv, 3, "Not enough arguments")
            pid = argv[2]
            if not pid.isdigit():
                raise CommandError("Specified process id to kill is not a number.")
            _proc(info, PROC_KILL, pid)
            message = "Tasked demon to kill a process"
        elif sub == "memory":
            _need(argv, 4, "Not enough arguments")
            _proc(info, PROC_MEMORY, argv[2] + " " + argv[3])
            message = f"Tasked demon to query for {argv[3]} memory regions from {argv[2]}"
        else:
            raise CommandError(f"Modules command not found: {sub}")

    elif cmd == "ps":
        if len(argv) != 1:
            raise CommandError("Too many arguments")
        info.update({"CommandID": str(CMD_PROC_LIST), "FromProcessManager": "false"})
        message = "Tasked demon to enumerate and list all processes"

    # -------------------------------------------------------------------- dll
    elif cmd == "dll":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        sub = argv[1]
        if sub == "inject":
            _need(argv, 4, "Not enough arguments")
            pid, path = argv[2], argv[3]
            args = _join(argv, 4)
            binary = read_local_file(path, "Specified reflective dll file not found")
            info.update({
                "TaskID": task_id,
                "DemonID": session.name,
                "CommandID": str(CMD_INJECT_DLL),
                "CommandLine": line,
                "Binary": b64_bytes(binary),
                "Arguments": args,
                "PID": pid,
            })
            message = f"Tasked demon to inject a reflective dll: {path}"
        elif sub == "spawn":
            _need(argv, 3, "Not enough arguments")
            path = argv[2]
            args = _join(argv, 3)
            binary = read_local_file(path, "Specified reflective dll file not found")
            info.update({
                "CommandID": str(CMD_INJECT_DLL_SPAWN),
                "Binary": b64_bytes(binary),
                "Arguments": _local_b64(args),
            })
            message = f"Tasked demon to spawn a reflective dll: {path}"
        else:
            raise CommandError(f"Modules command not found: {sub}")

    # --------------------------------------------------------------- shellcode
    elif cmd == "shellcode":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        sub = argv[1]
        technique = "default"
        # 客户端 ShellcodeSpawn/Execute 把 Arguments（UTF-8 字符串）再做 base64；
        # 控制台 `shellcode spawn <arch> <path> <args...>` 的尾部参数按同样方式传递。
        shellcode_args = b64(_join(argv, 4))
        if sub == "inject":
            _need(argv, 5, "Not enough arguments")
            arch, pid, path = argv[2], argv[3], argv[4]
            if arch not in ("x64", "x86"):
                raise CommandError(f"Incorrect process arch specified: {arch}")
            binary = read_local_file(path, "Specified file not found")
            info.update({
                "TaskID": task_id,
                "DemonID": session.name,
                "CommandID": str(CMD_INJECT_SHELLCODE),
                "CommandLine": line,
                "Way": "Inject",
                "Technique": technique,
                "Binary": b64_bytes(binary),
                "Arguments": b64(_join(argv, 5)),
                "PID": pid,
                "Arch": arch,
            })
            message = "Tasked demon to inject shellcode into a remote process"
        elif sub in ("spawn", "execute"):
            _need(argv, 4, "Not enough arguments")
            arch, path = argv[2], argv[3]
            if arch not in ("x64", "x86"):
                raise CommandError(f"Incorrect process arch specified: {arch}")
            binary = read_local_file(path, "Couldn't find specified binary")
            info.update({
                "TaskID": task_id,
                "DemonID": session.name,
                "CommandID": str(CMD_INJECT_SHELLCODE),
                "CommandLine": line,
                "Way": "Spawn" if sub == "spawn" else "Execute",
                "Technique": technique,
                "Binary": b64_bytes(binary),
                "Arguments": shellcode_args,
                "Arch": arch,
            })
            message = ("Tasked demon to fork and inject a " if sub == "spawn"
                       else "Tasked demon to self inject a ") + f"{arch} shellcode"
        else:
            raise CommandError(f"Modules command not found: {sub}")

    # ------------------------------------------------------------------ token
    elif cmd == "token":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        sub = argv[1]
        args = ""
        if sub == "impersonate":
            _need(argv, 3, "Not enough arguments")
            args = argv[2]
            message = "Tasked demon to impersonate a process token"
        elif sub == "steal":
            _need(argv, 3, "Not enough arguments")
            if len(argv) > 4:
                raise CommandError("Too many arguments")
            handle = argv[3] if len(argv) == 4 else "0"
            args = argv[2] + ";" + handle
            message = "Tasked demon to steal a process token"
        elif sub == "list":
            message = "Tasked demon to list token vault"
        elif sub == "find":
            message = "Tasked demon to find tokens"
        elif sub == "make":
            if len(argv) < 5:
                raise CommandError("Not enough arguments")
            if len(argv) > 6:
                raise CommandError("Too many arguments")
            domain, user, password = argv[2], argv[3], argv[4]
            logon_type = "9"
            if len(argv) == 6:
                logon_type = {
                    "LOGON_INTERACTIVE": "2",
                    "LOGON_NETWORK": "3",
                    "LOGON_BATCH": "4",
                    "LOGON_SERVICE": "5",
                    "LOGON_UNLOCK": "7",
                    "LOGON_NETWORK_CLEARTEXT": "8",
                    "LOGON_NEW_CREDENTIALS": "9",
                }.get(argv[5], "")
                if not logon_type:
                    raise CommandError("Invalid token type")
            args = ";".join([_local_b64(domain), _local_b64(user),
                             _local_b64(password), logon_type])
            message = f"Tasked demon to make a new network token for {domain}\\{user}"
        elif sub == "revert":
            message = "Tasked demon to revert the process token"
        elif sub == "remove":
            _need(argv, 3, "Not enough arguments")
            args = argv[2]
            message = "Tasked demon to remove a token from the token vault"
        elif sub == "clear":
            message = "Tasked demon to clear token vault"
        elif sub == "getuid":
            message = "Tasked demon to get current user id"
        elif sub == "privs-list":
            message = "Tasked demon to list current token privileges"
        elif sub == "privs-get":
            _need(argv, 3, "Not enough arguments")
            if len(argv) > 4:
                raise CommandError("Too many arguments")
            args = argv[2]
            message = f"Tasked demon to enable a privilege: {args}"
        else:
            raise CommandError("Module command not found")
        info.update({
            "CommandID": str(CMD_TOKEN),
            "SubCommand": sub,
            "Arguments": args,
        })

    # --------------------------------------------------------- inline-execute
    elif cmd == "inline-execute":
        _need(argv, 2, "Not enough arguments")
        path = argv[1]
        args = _join(argv, 2)
        binary = read_local_file(path, "Specified object file not found")
        info.update({
            "CommandID": str(CMD_INLINE_EXECUTE),
            "HasCallback": "false",
            "FunctionName": "go",
            "Binary": b64_bytes(binary),
            "Arguments": b64(args),
            "Flags": "default",
        })
        message = f"Tasked demon to execute an object file: {path}"
        note = "BOF / 对象文件"

    # ----------------------------------------------------------------- dotnet
    elif cmd == "dotnet":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        sub = argv[1]
        if sub == "inline-execute":
            _need(argv, 3, "Not enough arguments")
            path = argv[2]
            args = _join(argv, 3)
            binary = read_local_file(path, "Couldn't find assembly file")
            info.update({
                "CommandID": str(CMD_INLINE_EXECUTE_ASSEMBLY),
                "Binary": b64_bytes(binary),
                "Arguments": args,
            })
            message = f"Tasked demon to inline execute a dotnet assembly: {path}"
            note = ".NET Assembly"
        elif sub == "list-versions":
            info.update({"CommandID": str(CMD_ASSEMBLY_LIST_VERSIONS)})
            message = "Tasked demon to list available dotnet versions"
        else:
            raise CommandError(f"Modules command not found: {sub}")

    # --------------------------------------------------------------- rportfwd
    elif cmd == "rportfwd":
        if len(argv) <= 1:
            raise CommandError('Not enough arguments for "rportfwd"')
        sub = argv[1]
        params = ""
        if sub == "add":
            if len(argv) < 6:
                raise CommandError('Not enough arguments for "rportfwd add"')
            params = ";".join(argv[2:6])
            message = (f"Tasked demon to start a reverse port forward {argv[2]}:{argv[3]} "
                       f"to {argv[4]}:{argv[5]}")
        elif sub == "list":
            message = "Tasked demon to list all reverse port forwards"
        elif sub == "remove":
            _need(argv, 3, 'Not enough arguments for "rportfwd remove"')
            params = argv[2]
            message = f"Tasked demon to close and remove a reverse port forward {params}"
        elif sub == "clear":
            message = "Tasked demon to close and clear all reverse port forwards"
        else:
            raise CommandError(f"Sub command not found: {sub}")
        info.update({
            "CommandID": str(CMD_SOCKET),
            "Command": f"rportfwd {sub}",
            "Params": params,
        })

    # ------------------------------------------------------------------ socks
    elif cmd == "socks":
        if len(argv) <= 1:
            raise CommandError('Not enough arguments for "socks"')
        sub = argv[1]
        params = ""
        if sub == "add":
            _need(argv, 3, 'Not enough arguments for "socks add"')
            params = argv[2]
        elif sub == "kill":
            _need(argv, 3, 'Not enough arguments for "socks kill"')
            params = argv[2]
        elif sub not in ("list", "clear"):
            raise CommandError(f"Sub command not found: {sub}")
        info.update({
            "CommandID": str(CMD_SOCKET),
            "Command": f"socks {sub}",
            "Params": params,
        })
        message = f"Tasked demon to {sub} socks5 proxy"

    # --------------------------------------------------------------- transfer
    elif cmd == "transfer":
        if len(argv) < 2:
            raise CommandError('Not enough arguments for "transfer"')
        sub = argv[1]
        file_id = ""
        if sub in ("stop", "resume", "remove"):
            if len(argv) <= 2:
                raise CommandError("Not enough arguments")
            file_id = argv[2]
        elif sub != "list":
            raise CommandError(f"Sub command not found: {sub}")
        info.update({
            "CommandID": str(CMD_TRANSFER),
            "Command": sub,
            "FileID": file_id,
        })
        message = f"Tasked demon to {sub} a download"

    # --------------------------------------------------------------- download
    elif cmd == "download":
        _need(argv, 2, "Not enough arguments")
        path = _join(argv, 1)
        _fs(info, "download", _local_b64(path))
        message = f"Tasked demon to download a file {path}"
        note = "文件传输"

    elif cmd in ("cat", "type"):
        _need(argv, 2, "Not enough arguments")
        path = _join(argv, 1)
        _fs(info, "cat", _local_b64(path))
        message = f"Tasked demon to display content of {path}"

    # ----------------------------------------------------------------- upload
    elif cmd == "upload":
        _need(argv, 2, "Not enough arguments")
        local_path = argv[1]
        remote_path = _join(argv, 2)
        content = read_local_file(local_path, "File not found")
        filename = local_path.replace("\\", "/").rsplit("/", 1)[-1]
        if not remote_path:
            remote_path = filename
        elif remote_path.endswith("\\") or remote_path.endswith("/"):
            remote_path += filename
        _fs(info, "upload", _local_b64(remote_path) + ";" + b64_bytes(content))
        message = f"Tasked demon to upload a file {local_path} to {remote_path}"
        note = "上传文件"

    # ----------------------------------------------------------------- config
    elif cmd == "config":
        if len(argv) < 3:
            raise CommandError("Not enough arguments")
        key, value = argv[1], _join(argv, 2)
        bool_keys = {
            "implant.verbose", "implant.sleep-mask", "implant.sleep-obf",
            "implant.coffee.veh", "implant.coffee.threaded",
        }
        number_keys = {
            "implant.sleep-obf.start-addr", "implant.sleep-obf.technique",
            "memory.alloc", "memory.execute",
        }
        if key in bool_keys and value not in ("true", "false"):
            raise CommandError("Wrong arguments")
        if key in number_keys and not value.replace("0x", "").isdigit():
            raise CommandError("Wrong argument: Is not a number")
        if key == "implant.sleep-obf.technique":
            raise CommandError("Not implemented")
        if key == "inject.spoofaddr":
            raise CommandError("Not implemented")
        if key not in {
            "implant.verbose", "implant.sleep-mask", "implant.sleep-obf",
            "implant.sleep-obf.start-addr", "implant.sleep-obf.technique",
            "implant.coffee.veh", "implant.coffee.threaded",
            "memory.alloc", "memory.execute", "inject.spoofaddr",
            "inject.spawn64", "inject.spawn32", "killdate", "workinghours",
        }:
            raise CommandError("Config does not exist")
        info.update({
            "CommandID": str(CMD_CONFIG),
            "ConfigKey": key,
            "ConfigVal": value,
        })
        message = f"Tasked demon to configure {key}: {value}"

    elif cmd == "screenshot":
        info.update({"CommandID": str(CMD_SCREENSHOT)})
        message = "Tasked demon to take a screenshot"

    # -------------------------------------------------------------------- net
    elif cmd == "net":
        if len(argv) < 2:
            raise CommandError("No sub command specified")
        sub = argv[1]
        table = {
            "domain": ("1", "", False),
            "logons": ("2", "\\\\localhost", True),
            "sessions": ("3", "\\\\localhost", True),
            "share": ("6", "\\\\localhost", True),
            "localgroup": ("7", "\\\\localhost", True),
            "group": ("8", "\\\\localhost", True),
            "users": ("9", "\\\\localhost", True),
        }
        if sub not in table:
            raise CommandError("Command not found: " + " ".join(argv))
        command_id, param, takes_param = table[sub]
        if takes_param and len(argv) > 2:
            param = argv[2]
        info.update({
            "CommandID": str(CMD_NET),
            "NetCommand": command_id,
            "Param": param,
        })
        message = f"Tasked demon net {sub}"

    # ------------------------------------------------------------------ pivot
    elif cmd == "pivot":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        sub = argv[1]
        command_id = ""
        param = ""
        if sub == "list":
            command_id = "1"
            message = "Tasked demon to list connected agent pivots"
        elif sub == "connect":
            if len(argv) < 4:
                raise CommandError("Not enough arguments")
            command_id = "10"
            param = "\\\\" + argv[2] + "\\pipe\\" + argv[3]
            message = f"Tasked demon to connect to a smb pivot: {param}"
        elif sub == "disconnect":
            _need(argv, 3, "Not enough arguments")
            command_id = "11"
            param = argv[2]
            message = f"Tasked demon to disconnect a smb pivot: {param}"
        else:
            raise CommandError("Command not found: " + " ".join(argv))
        info.update({
            "CommandID": str(CMD_PIVOT),
            "Command": command_id,
            "Param": param,
        })

    # --------------------------------------------------------------- kerberos
    elif cmd == "luid":
        info.update({"CommandID": str(CMD_KERBEROS), "Command": "luid"})
        message = "Tasked demon to get the current logon ID"

    elif cmd == "klist":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        if len(argv) > 3:
            raise CommandError("Too many arguments")
        arg1 = ""
        arg2 = ""
        if argv[1] == "/all":
            arg1 = "/all"
        elif argv[1] == "/luid":
            if len(argv) != 3:
                raise CommandError("Invalid parameter")
            arg1 = "/luid"
            arg2 = argv[2]
        else:
            raise CommandError("Invalid parameter")
        info.update({
            "CommandID": str(CMD_KERBEROS),
            "Command": "klist",
            "Argument1": arg1,
            "Argument2": arg2,
        })
        message = "Tasked demon to list Kerberos tickets"

    elif cmd == "purge":
        if len(argv) < 3:
            raise CommandError("Not enough arguments")
        if len(argv) > 3:
            raise CommandError("Too many arguments")
        if argv[1] != "/luid":
            raise CommandError("Invalid parameter")
        info.update({
            "CommandID": str(CMD_KERBEROS),
            "Command": "purge",
            "Argument": argv[2],
        })
        message = "Tasked demon to purge a Kerberos ticket"

    elif cmd == "ptt":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        if len(argv) > 4:
            raise CommandError("Too many arguments")
        ticket = argv[1]
        luid = "0"
        if len(argv) == 3:
            raise CommandError("Invalid arguments")
        if len(argv) == 4:
            if argv[2] != "/luid":
                raise CommandError("Invalid arguments")
            luid = argv[3]
        info.update({
            "CommandID": str(CMD_KERBEROS),
            "Command": "ptt",
            "Ticket": ticket,
            "Luid": luid,
        })
        message = "Tasked demon to import a Kerberos ticket"

    # ------------------------------------------------------------------- exit
    elif cmd == "exit":
        if len(argv) < 2:
            raise CommandError("Not enough arguments")
        if len(argv) > 2:
            raise CommandError("Too many arguments")
        method = argv[1]
        if method not in ("thread", "process"):
            raise CommandError(f"Option not found: {method}")
        info.update({"CommandID": str(CMD_EXIT), "ExitMethod": method})
        message = f"Tasked demon to cleanup and exit the {method}"
        note = "确认目标会退出"

    else:
        # 第三方 Agent：与 ConsoleInput.cc 的 AgentCommand 通用路径一致。
        generic = _build_generic(session, line, task_id, argv)
        if generic is not None:
            return generic
        raise CommandError(f"Command/Module not found: {line}")

    return Built(info=info, note=note, message=message)


def build_proc_create(session: SessionRef, task_id: str, app: str, cmdline: str,
                      suspended: bool = False, piped: bool = True,
                      verbose: bool = False) -> dict:
    """构造 DEMON_COMMAND_PROC_CREATE（供 Python SDK ProcessCreate 使用）。

    teamserver/pkg/agent/demons.go 的解析顺序是：
        state;verbose;piped;app;base64(cmdline)
    客户端 PyDemonClass.cc 的拼接顺序与之不一致（疑似客户端 bug）；Web SDK 以
    teamserver 为准。
    """
    state = "4" if suspended else "0"
    args = ";".join([
        state,
        "TRUE" if verbose else "FALSE",
        "TRUE" if piped else "FALSE",
        str(app or ""),
        b64(str(cmdline or "")),
    ])
    return {
        "TaskID": task_id,
        "CommandLine": f"proc create {'suspended' if suspended else 'normal'} {app}".strip(),
        "DemonID": session.name,
        "CommandID": str(CMD_PROC),
        "ProcCommand": str(PROC_CREATE),
        "Args": args,
    }


def _build_generic(session: SessionRef, line: str, task_id: str,
                   argv: list[str]) -> Optional[Built]:
    """构造 {TaskID, CommandLine, DemonID, Command, <Param>: <Value>}。

    不带 CommandID，和客户端 CommandExecute::AgentCommand 完全一致。只对
    SessionRef.commands 中存在的第三方 Agent 命令生效。
    """
    commands = session.commands or []
    if not commands:
        return None
    name = argv[0] if argv else ""
    command = None
    for item in commands:
        if not isinstance(item, dict):
            continue
        if str(item.get("Name") or item.get("name") or "") == name:
            command = item
            break
    if command is None:
        return None

    params = command.get("Params") or command.get("params") or []
    values = argv[1:]
    info: dict = {
        "TaskID": task_id,
        "CommandLine": line,
        "DemonID": session.name,
        "Command": name,
    }
    for index, param in enumerate(params):
        if not isinstance(param, dict):
            continue
        pname = str(param.get("Name") or param.get("name") or "")
        if not pname:
            continue
        is_file = bool(param.get("IsFilePath") or param.get("is_file_path"))
        optional = bool(param.get("IsOptional") or param.get("is_optional"))
        raw = ""
        if is_file:
            if index < len(values) and values[index]:
                try:
                    raw = b64_bytes(read_local_file(values[index]))
                except CommandError as exc:
                    raise CommandError(f"Failed to open file path: {values[index]}") from exc
        elif len(params) == 1 and len(values) > 1:
            raw = " ".join(values)
        elif index < len(values):
            raw = values[index]
        if not raw and not optional:
            raise CommandError(f"Not enough arguments for parameter: {pname}")
        info[pname] = raw
    return Built(info=info, note="第三方 Agent 命令", message=f"Tasked agent to {name}")


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------

def _configured_extra(extra: Optional[Iterable]) -> list[tuple[str, str, str]]:
    """把 Web 侧动态注册的 Python 命令并入 help（与客户端加载脚本后的行为一致）。"""
    rows: list[tuple[str, str, str]] = []
    if not extra:
        return rows
    for item in extra:
        if isinstance(item, dict):
            name = str(item.get("key") or item.get("command") or "")
            module = str(item.get("module") or "")
            # 模块子命令不单独占 help 一行；模块本身由 modules 参数提供。
            if module:
                continue
            typ = "Command"
            desc = str(item.get("description") or "")
        else:
            try:
                name, typ, desc = item
            except (TypeError, ValueError):
                continue
        if name:
            rows.append((str(name), str(typ), str(desc)))
    return rows


def _configured_modules(modules: Optional[Iterable]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for item in modules or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "")
            desc = str(item.get("description") or "")
        else:
            continue
        if name:
            rows.append((name, "Module", desc))
    return rows


def help_rows(extra: Optional[Iterable] = None,
              modules: Optional[Iterable] = None) -> list[tuple[str, str, str]]:
    """与 ConsoleInput.cc 的 help 分支一致：静态命令 + 模块 + 注册命令。"""
    rows: list[tuple[str, str, str]] = []
    for c in STATIC_COMMANDS:
        rows.append((c["command"], "Module" if c.get("module") else "Command",
                     description_for(c["command"], c.get("description", ""))))
    for m in REGISTERED_MODULES:
        rows.append((m["name"], "Module",
                     description_for(m["name"], m.get("description", ""))))
    for c in REGISTERED_COMMANDS:
        if not c.get("module"):
            rows.append((c["command"], "Command",
                         description_for(c["command"], c.get("description", ""))))
    rows.extend(_configured_modules(modules))
    rows.extend(_configured_extra(extra))

    # 去重（动态脚本可能重复注册内置命令），保留最后一次；客户端也是覆盖式注册。
    merged: dict[str, tuple[str, str, str]] = {}
    for name, typ, desc in rows:
        merged[name] = (name, typ, desc)
    return sorted(merged.values(), key=lambda row: row[0].lower())


def render_help(extra: Optional[Iterable] = None,
                modules: Optional[Iterable] = None,
                platform: str = "") -> str:
    """按客户端 Demon Commands 表格排版（列位置 2/27/40）。

    platform="linux" 时给没有 Linux 实现的命令追加「仅 Windows」标记。
    """
    import linux_compat

    lines = [
        "",
        "Demon Commands",
        "==============",
        "",
        "  " + "Command".ljust(25) + "Type".ljust(13) + "Description",
        "  " + "-------".ljust(25) + "-------".ljust(13) + "-----------",
    ]
    for cmd, typ, desc in help_rows(extra, modules):
        if platform == "linux":
            desc += linux_compat.help_marker(cmd)
        lines.append("  " + cmd.ljust(25) + typ.ljust(13) + desc)
    return "\n".join(lines)


def _registered_for_module(module: str) -> list[dict]:
    return [c for c in REGISTERED_COMMANDS if c.get("module") == module]


def _command_help_static(cmd: dict, sub: Optional[str], extra: Optional[Iterable]) -> str:
    name = cmd["command"]
    rows: list[str] = [""]
    if sub and (cmd.get("subcommands") or cmd.get("module")):
        for sc in cmd.get("subcommands", []):
            if sc["command"] == sub:
                rows += [
                    f" - Module        :  {name}",
                    f" - Sub Command   :  {sc['command']}",
                    f" - Description   :  {sub_description_for(name, sc['command'], sc.get('description', ''))}",
                ]
                if sc.get("behavior"):
                    rows.append(f" - Behavior      :  {sc['behavior']}")
                if sc.get("usage"):
                    rows.append(f" - Usage         :  {name} {sc['command']} {sc['usage']}")
                if sc.get("example"):
                    rows.append(f" - Example       :  {name} {sc['command']} {sc['example']}")
                return "\n".join(rows)
        for reg in _registered_for_module(name):
            if reg["command"] == sub:
                rows += [
                    f" - Module        :  {name}",
                    f" - Sub Command   :  {reg['command']}",
                    f" - Description   :  {sub_description_for(name, reg['command'], reg.get('description', ''))}",
                    f" - Usage         :  {name} {reg['command']} {reg.get('usage', '')}",
                    f" - Example       :  {name} {reg['command']} {reg.get('example', '')}",
                ]
                return "\n".join(rows)
        # 动态注册的模块命令
        for reg in _configured_registered(name, sub, extra):
            rows += [
                f" - Module        :  {name}",
                f" - Sub Command   :  {reg['command']}",
                f" - Description   :  {reg.get('description', '')}",
                f" - Usage         :  {name} {reg['command']} {reg.get('usage', '')}",
                f" - Example       :  {name} {reg['command']} {reg.get('example', '')}",
            ]
            return "\n".join(rows)
        return f'\n[-] Couldn\'t find sub command in "{name}": {sub}'

    rows.append(f" - Command       :  {name}")
    rows.append(f" - Description   :  {description_for(name, cmd.get('description', ''))}")
    if cmd.get("behavior"):
        rows.append(f" - Behavior      :  {cmd['behavior']}")
    if cmd.get("usage"):
        rows.append(f" - Usage         :  {name} {cmd['usage']}")
    if cmd.get("example"):
        rows.append(f" - Example       :  {name} {cmd['example']}")
    if cmd.get("usage") and not cmd.get("subcommands"):
        rows.append(f" - Required Args :  {len(str(cmd['usage']).split(' '))}")

    subs = cmd.get("subcommands", [])
    reg_subs = _registered_for_module(name)
    extra_subs = _configured_registered_module(name, extra)
    if subs or cmd.get("module") or reg_subs:
        rows += [
            "",
            "  Command                        Description      ",
            "  ---------                      -------------     ",
        ]
        for sc in subs:
            rows.append("  " + sc["command"].ljust(31)
                        + sub_description_for(name, sc["command"], sc.get("description", "")))
        for reg in reg_subs + extra_subs:
            rows.append("  " + reg["command"].ljust(19) + "       "
                        + sub_description_for(name, reg["command"], reg.get("description", "")))
    return "\n".join(rows)


def _configured_registered(module: str, sub: str, extra: Optional[Iterable]) -> list[dict]:
    out = []
    for item in extra or []:
        if isinstance(item, dict) and item.get("module") == module and item.get("command") == sub:
            out.append(item)
    return out


def _configured_registered_module(module: str, extra: Optional[Iterable]) -> list[dict]:
    out = []
    for item in extra or []:
        if isinstance(item, dict) and item.get("module") == module:
            out.append(item)
    return out


def _command_help_registered(command: str, extra: Optional[Iterable]) -> Optional[str]:
    reg = REGISTERED_BY_KEY.get(command)
    if reg is None:
        for item in extra or []:
            if isinstance(item, dict) and item.get("key") == command and not item.get("module"):
                reg = item
                break
    if reg is None:
        return None
    rows = [
        "",
        f" - Command       :  {reg['command']}",
        f" - Description   :  {description_for(reg['command'], reg.get('description', ''))}",
        f" - Usage         :  {reg.get('usage', '')}".rstrip(),
        f" - Example       :  {reg.get('example', '')}".rstrip(),
    ]
    if reg.get("usage"):
        rows.append(f" - Required Args :  {len(str(reg['usage']).split(' '))}")
    return "\n".join(rows)


def _command_help_module(module: dict, sub: Optional[str], extra: Optional[Iterable]) -> str:
    name = module["name"]
    regs = [c for c in REGISTERED_COMMANDS if c.get("module") == name]
    regs += _configured_registered_module(name, extra)
    if sub:
        for reg in regs:
            if reg["command"] == sub:
                rows = [
                    "",
                    f" - Command       :  {name} {reg['command']}",
                    f" - Description   :  {sub_description_for(name, reg['command'], reg.get('description', ''))}",
                ]
                if module.get("usage"):
                    rows.append(f" - Usage         :  {name} {reg['command']} {reg.get('usage', '')}")
                if reg.get("example"):
                    rows.append(f" - Example       :  {name} {reg['command']} {reg['example']}")
                if reg.get("usage"):
                    rows.append(f" - Required Args :  {len(str(reg['usage']).split(' '))}")
                return "\n".join(rows)
        return f'\n[-] Couldn\'t find sub command in "{name}": {sub}'

    rows = [
        "",
        f" - Command       :  {name}",
        f" - Description   :  {description_for(name, module.get('description', ''))}",
    ]
    if module.get("behavior"):
        rows.append(f" - Behavior      :  {module['behavior']}")
    if module.get("usage"):
        rows.append(f" - Usage         :  {name} {module['usage']}")
    if module.get("example"):
        rows.append(f" - Example       :  {name} {module['example']}")
    if module.get("usage"):
        rows.append(f" - Required Args :  {len(str(module['usage']).split(' '))}")
    rows += [
        "",
        "  Command                   Description      ",
        "  ---------                 -------------     ",
    ]
    for reg in regs:
        rows.append("  " + reg["command"].ljust(19) + "       "
                    + sub_description_for(name, reg["command"], reg.get("description", "")))
    return "\n".join(rows)


def render_command_help(command: str, sub: Optional[str] = None,
                        extra: Optional[Iterable] = None,
                        modules: Optional[Iterable] = None,
                        platform: str = "") -> str:
    """客户端 `help <command> [sub]` 的输出（platform="linux" 追加平台说明）。"""
    text = _render_command_help(command, sub, extra, modules)
    if platform == "linux":
        import linux_compat

        if command.lower() in linux_compat.LINUX_NATIVE:
            text += "\n - Platform      :  Linux 原生支持（或由网关改写为原生命令）"
        else:
            text += "\n - Platform      :  仅 Windows（Linux Demon 无等价实现）"
    return text


def _render_command_help(command: str, sub: Optional[str] = None,
                         extra: Optional[Iterable] = None,
                         modules: Optional[Iterable] = None) -> str:
    """客户端 `help <command> [sub]` 的输出。"""
    key = (command or "").strip()
    if not key:
        return render_help(extra, modules)
    low = key.lower()
    # 静态命令大小写不敏感（Web 友好）；动态命令保留原样匹配
    for name, cmd in STATIC_BY_NAME.items():
        if name.lower() == low:
            return _command_help_static(cmd, sub, extra)
    module = MODULE_BY_NAME.get(key)
    if module is not None:
        return _command_help_module(module, sub, extra)
    for name, mod in MODULE_BY_NAME.items():
        if name.lower() == low:
            return _command_help_module(mod, sub, extra)
    for mod in modules or []:
        if isinstance(mod, dict) and str(mod.get("name", "")).lower() == low:
            return _command_help_module(mod, sub, extra)
    rendered = _command_help_registered(key, extra)
    if rendered is not None:
        return rendered
    # 动态注册的顶层命令
    for item in extra or []:
        if isinstance(item, dict) and item.get("key") == key:
            return _command_help_registered(key, extra) or ""
    return f'\n[-] Couldn\'t find command: {key}'


HELP_TEXT = render_help()


__all__ = [
    "Built", "CommandError", "HELP_TEXT", "SessionRef", "build",
    "help_rows", "parse_argv", "render_command_help", "render_help",
    "resolve_local_file", "read_local_file",
    "PROJECT_ROOT", "UPLOAD_DIR", "MODULE_SCRIPTS",
]
