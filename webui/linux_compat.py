#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linux 会话的「原生等价命令」层。

Windows Demon 的很多命令由客户端模块以 BOF/.NET 形式下发（whoami/arp/ipconfig...），
Linux Demon 无法执行 COFF 对象，因此在网关侧把语义等价、在 Linux 上真实存在的命令
改写为原生 shell 或 agent 协议命令：

    whoami      -> id
    env         -> env
    ipconfig    -> ip -brief address + ip route
    netstat     -> ss -tunap
    arp         -> ip neigh
    routeprint  -> ip route
    listdns     -> resolvectl status / /etc/resolv.conf
    locale      -> locale
    uptime      -> uptime -p + /proc/uptime
    resources   -> df -h + free -h
    userenum    -> getent passwd
    netuser     -> getent passwd <user>
    sessions / enumlocalsessions / get-netsession / quser -> who
    nslookup    -> getent hosts <host>
    windowlist  -> wmctrl / xdotool
    tasklist    -> proc list（agent 协议，解析 /proc）
    powershell / powerpick -> pwsh（若安装）
    net users/localgroup/sessions/domain -> getent / who / hostname -d

其余 Windows 专有命令（AD/Kerberos/registry/service/token/injection/.NET/BOF/WMI…）
没有 Linux 语义，help 中会标注「仅 Windows」，下发时由 agent 回显不支持。
其中 netshares / cacls / netLocalGroupList / netLclGrpLstMmbrs 对应的 CommandID 在
Linux Demon 上完全不回包（会话会一直等到超时），因此由网关直接短路（`WINDOWS_ONLY`）。
"""
from __future__ import annotations

import shlex
from typing import Optional

# Windows 原生命令：语义只存在于 Windows（SMB 共享 / NTFS ACL / NetLocalGroup API），
# Linux Demon 收到对应 CommandID 后不会回包，客户端只能一直等到超时并占住会话。
# 这 4 条在网关侧直接短路（不下发），返回「not supported by the Linux demon」，
# 与 agent 对其他 Windows 专有命令的回显口径一致。
WINDOWS_ONLY = {
    "netshares",
    "cacls",
    "netlocalgrouplist",
    "netlclgrplstmmbrs",
}

# help 中在 Linux 上“有真实实现”的命令（agent 原生或由本层改写）
LINUX_NATIVE = {
    "arp", "cat", "cd", "checkin", "cp", "dir", "download", "env", "enumlocalsessions",
    "exit", "get-netsession", "help", "ipconfig", "listdns", "locale", "mkdir", "mv",
    "net", "netstat", "netsession", "netuser", "nslookup", "powerpick", "powershell",
    "rportfwd", "socks",
    "proc", "pwd", "quser", "remove", "resources", "routeprint", "screenshot", "sessions",
    "shell", "sleep", "task", "tasklist", "uptime", "upload", "userenum", "whoami",
    "windowlist", "netuptime",
}


def is_linux(session: dict) -> bool:
    if not isinstance(session, dict):
        return False
    if str(session.get("platform", "")).lower() == "linux":
        return True
    return str(session.get("os", "")).lower().startswith("linux")


def _join(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)


def _net_command(args: list[str]) -> Optional[str]:
    if len(args) < 2:
        return None
    sub = args[1].lower()
    rest = args[2:]
    if sub in ("users", "user"):
        return "getent passwd"
    if sub in ("localgroup", "group", "groups"):
        return "getent group"
    if sub in ("sessions", "session"):
        return "who -u 2>/dev/null || w"
    if sub == "domain":
        return "hostname -d 2>/dev/null; hostname -f 2>/dev/null"
    if sub in ("share", "shares"):
        return ("echo 'Linux 无 SMB 共享枚举等价命令'; "
                "command -v smbclient >/dev/null && smbclient -L //127.0.0.1 -N 2>/dev/null")
    return None


def _pwsh_command(args: list[str]) -> Optional[str]:
    if len(args) < 2:
        return None
    script = _join(args[1:])
    return f"command -v pwsh >/dev/null 2>&1 && pwsh -NoProfile -Command {shlex.quote(script)} || echo 'pwsh 未安装：请使用 shell'"


def shell_command(line: str, argv: list[str]) -> Optional[str]:
    """把一条 Windows 语义命令改写为 Linux shell 命令；无等价实现返回 None。"""
    cmd = argv[0].lower() if argv else ""
    rest = args_after = argv[1:]

    if cmd == "whoami":
        return "id"
    if cmd == "env":
        return "env"
    if cmd == "ipconfig":
        return "ip -brief address 2>/dev/null || ifconfig -a; echo; ip route 2>/dev/null || route -n"
    if cmd == "netstat":
        return "ss -tunap 2>/dev/null || netstat -tunap 2>/dev/null"
    if cmd == "arp":
        return "ip neigh 2>/dev/null || cat /proc/net/arp"
    if cmd == "routeprint":
        return "ip route 2>/dev/null || route -n"
    if cmd == "listdns":
        return "resolvectl status 2>/dev/null || cat /etc/resolv.conf"
    if cmd == "locale":
        return "locale"
    if cmd in ("uptime", "netuptime"):
        return "uptime -p 2>/dev/null; cat /proc/uptime"
    if cmd == "resources":
        return "df -h 2>/dev/null; echo; free -h 2>/dev/null || head -5 /proc/meminfo"
    if cmd == "userenum":
        return "getent passwd 2>/dev/null || cat /etc/passwd"
    if cmd == "netuser":
        if len(rest) >= 1:
            return f"getent passwd {shlex.quote(rest[0])} 2>/dev/null || grep -E '^{shlex.quote(rest[0])}:' /etc/passwd"
        return "getent passwd 2>/dev/null || cat /etc/passwd"
    if cmd in ("sessions", "enumlocalsessions", "get-netsession", "quser", "netsession"):
        return "who -u 2>/dev/null || w"
    if cmd == "nslookup":
        if len(rest) >= 1:
            return f"getent hosts {shlex.quote(rest[0])} 2>/dev/null; command -v dig >/dev/null && dig +short {shlex.quote(rest[0])}"
        return None
    if cmd == "windowlist":
        return ("wmctrl -l 2>/dev/null || "
                "xdotool search --onlyvisible --name . getwindowname %@ 2>/dev/null || "
                "echo 'windowlist 需要 wmctrl 或 xdotool'")
    if cmd == "net":
        return _net_command(argv)
    if cmd in ("powershell", "powerpick"):
        return _pwsh_command(argv)
    return None


def unsupported_message(command: str) -> str:
    """Linux 会话上 Windows 原生命令的统一回显（口径同 agent 的 0x14 回包）。"""
    name = (command or "").strip() or "command"
    return (f"{name}: Windows 原生命令，Linux Demon 无等价实现"
            " (not supported by the Linux demon)")


def plan(session: dict, line: str) -> Optional[dict]:
    """返回 {"kind": "shell"|"proc"|"error", "line": ..., "note": ..., "message": ...}。"""
    if not is_linux(session):
        return None
    text = (line or "").strip()
    if not text:
        return None
    try:
        argv = shlex.split(text)
    except ValueError:
        return None
    if not argv:
        return None
    cmd = argv[0].lower()

    # Windows 原生命令：网关侧短路，不下发（否则 agent 不回包，会话被占满超时）
    if cmd in WINDOWS_ONLY:
        message = unsupported_message(argv[0])
        return {"kind": "error", "line": line, "note": message, "message": message}

    if cmd == "tasklist":
        return {"kind": "proc", "line": "proc list", "note": "Linux：proc list（解析 /proc）",
                "message": "Tasked demon to enumerate processes (Linux /proc)"}

    shell = shell_command(text, argv)
    if shell is None:
        return None
    return {"kind": "shell", "line": "shell " + shell,
            "note": f"Linux 原生等价命令：{shell}",
            "message": f"Tasked demon to run Linux equivalent: {shell}"}


def help_marker(name: str) -> str:
    """help 表格里给 Linux 会话追加的平台说明。"""
    if name.lower() in LINUX_NATIVE:
        return ""
    return " 〔仅 Windows〕"


def help_note() -> str:
    return (
        "[Linux 会话] 这些命令走原生实现：\n"
        "  whoami/env/ipconfig/netstat/arp/routeprint/listdns/locale/uptime/resources/\n"
        "  userenum/netuser/sessions/enumlocalsessions/get-netsession/quser/nslookup/\n"
        "  windowlist/tasklist/net users|localgroup|sessions|domain/powershell(pwsh)\n"
        "  socks/rportfwd（COMMAND_SOCKET：teamserver 侧 SOCKS5 / 目标侧监听转发）\n"
        "  以及 shell/cat/cd/cp/dir/download/upload/mkdir/mv/pwd/remove/sleep/checkin/\n"
        "  exit/screenshot/proc/help/task。\n"
        "  标注〔仅 Windows〕的命令（AD/Kerberos/注册表/服务/令牌/注入/.NET/BOF/WMI）\n"
        "  在 Linux 上没有等价语义。\n"
    )
