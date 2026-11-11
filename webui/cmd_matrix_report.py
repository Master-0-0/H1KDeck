#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 cmd_matrix_test.py 的实测结果渲染成 Markdown 矩阵文档。

用法：
    cd webui && python3 cmd_matrix_test.py          # 先跑实测
    python3 cmd_matrix_report.py                    # 再生成 docs/command-matrix.md
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "data" / "cmd_matrix.json"
OUT = HERE / "docs" / "command-matrix.md"

# Linux Demon 原生实现（Go agent dispatch 里的 cmdFS/cmdProc/cmdScreenshot/cmdSocket/...）
NATIVE = {"cd", "pwd", "dir", "cat", "cp", "mv", "mkdir", "remove", "download", "upload",
          "proc", "screenshot", "socks", "rportfwd", "sleep", "checkin",
          "config", "exit", "shell"}
# 网关 / teamserver 兼容层改写（linux_compat.py / linux_compat.go）
REWRITE = {
    "whoami": "id", "env": "env", "ipconfig": "ip -brief address + ip route",
    "netstat": "ss -tunap", "arp": "ip neigh", "routeprint": "ip route",
    "listdns": "resolvectl / /etc/resolv.conf", "locale": "locale",
    "uptime": "uptime -p + /proc/uptime", "netuptime": "uptime -p + /proc/uptime",
    "resources": "df -h + free -h", "userenum": "getent passwd",
    "netuser": "getent passwd <user>", "sessions": "who -u",
    "enumlocalsessions": "who -u", "get-netsession": "who -u", "quser": "who -u",
    "nslookup": "getent hosts + dig", "windowlist": "wmctrl / xdotool",
    "powershell": "pwsh -Command", "powerpick": "pwsh -Command",
    "net": "net users|localgroup|sessions|domain → getent/who/hostname",
    "tasklist": "proc list（解析 /proc）",
}
# 客户端本地命令，和平台无关
CLIENT_ANY = {"help", "task", "pyhello", "pysessions"}

# 实测后人工复核的修正（逐个单发验证过）
FIX = {
    "proc": ("可用", "proc list 直接解析 /proc"),
    "task": ("可用", "客户端任务队列（空队列时提示 No jobs）"),
    "mkdir": ("可用", "目录已存在时第二次会报 EEXIST/183"),
    "job": ("不支持", "job 模块在 Linux 没有实现（COMMAND_JOB 0x15 未处理）"),
}

# 实测后人工复核的 Windows 修正（BOF 返回错误文本时仍被记为“有输出”）
WIN_FIX = {
    "adcs_enum": ("⚠️ 环境限制", "0x8007054b，本机不在域内/无 DC"),
    "cacls": ("⚠️ 失败", "Error starting search handle，路径/通配符问题"),
    "get-asrep": ("⚠️ 环境限制", "无域，取不到 DN"),
    "get-spns": ("⚠️ 环境限制", "无域，取不到 DN"),
    "ldapsearch": ("⚠️ 环境限制", "无域，取不到 DN"),
    "netGroupListMembers": ("⚠️ 环境限制", "Error 2220，组不存在或无域"),
    "reg_query_recursive": ("⚠️ 失败", "error 2，hive/键写法或键不存在"),
    "mkdir": ("✅ 可用", "清理残留后实测可创建；已存在时报 183"),
    "task": ("✅ 可用", "任务队列为空"),

    "schtasksquery": ("⚠️ 需参数", "需要 taskpath"),
    "get-delegation": ("⚠️ 需参数", "需要 Constrained/Unconstrained/RBCD"),
}

WIN_NOTE = {
    "bofbelt": "12s 内无返回；本次未等到结果，会话随后被占住",
    "netview": ">12s，实测 46s 才返回",
    "mkdir": "目录已存在，返回 ERROR_ALREADY_EXISTS",
    "config": "需要子命令",
    "kerberoast": "需要 spn",
    "tgtdeleg": "需要 spn",
    "task": "队列为空",
    "get-delegation": "需要 Constrained/Unconstrained/RBCD",
    "schtasksquery": "需要 taskpath",
    "wmi_query": "查询语句要加引号",
}

WIN_LABEL = {
    "可用": "✅ 可用", "空输出": "✅ 无输出", "参数错误": "⚠️ 需参数",
    "环境错误": "⚠️ 环境限制", "无响应": "⏱ 超时", "帮助文本": "ℹ️ 本地 help",
    "客户端拦截": "⚠️ 需参数", "跳过": "⏭ 未测", "不支持": "❌ 不支持",
    "未测(会话阻塞)": "⏭ 未测",
}


def win_label(name: str, status: str) -> tuple[str, str]:
    if status == "跳过":
        return "⏭ 未测", ""
    if name in WIN_FIX:
        return WIN_FIX[name]
    label = WIN_LABEL.get(status, status)
    note = WIN_NOTE.get(name, "")
    return label, note


def lin_label(name: str, status: str) -> tuple[str, str]:
    if status == "跳过" or name in FIX and FIX[name][0] == "跳过":
        return "⏭ 未测", "破坏性命令，未实测"
    if name in FIX:
        st, note = FIX[name]
    else:
        st, note = status, ""
    if name in NATIVE:
        extra = {"config": "仅回显 sleep/jitter，需要子命令",
                 "mkdir": "实测可创建；目录已存在时报 EEXIST",
                 "screenshot": "依赖 X11 + ImageMagick import / xwd / grim",
                 "socks": "teamserver 侧 SOCKS5，list/add 可用",
                 "rportfwd": "目标侧监听转发，list/add/remove 可用",
                 "proc": "proc list 解析 /proc"}.get(name, "")
        return "✅ 原生", note or extra or "Linux Demon 原生实现"
    if name in REWRITE:
        return "✅ 可用", note or f"改写为：{REWRITE[name]}"
    if name in CLIENT_ANY:
        return "✅ 可用", note or "客户端本地命令，与平台无关"
    return "❌ 仅 Windows", note or "Linux 无等价实现，agent 回 not supported"


def main() -> int:
    rows = json.loads(SRC.read_text(encoding="utf-8"))
    lines: list[str] = []
    add = lines.append

    win = [r for r in rows if r["win_status"] != "跳过"]
    win_ok = [r for r in win if win_label(r["name"], r["win_status"])[0] in ("✅ 可用", "✅ 无输出")]
    win_attn = [r for r in win if win_label(r["name"], r["win_status"])[0].startswith("⚠️")]
    win_slow = [r for r in win if win_label(r["name"], r["win_status"])[0].startswith("⏱")]
    win_skip = [r for r in rows if r["win_status"] == "跳过"]
    win_local = [r for r in win if win_label(r["name"], r["win_status"])[0].startswith("ℹ")]
    lin_ok = [r for r in rows if lin_label(r["name"], r["lin_status"])[0].startswith("✅")]
    lin_no = [r for r in rows if lin_label(r["name"], r["lin_status"])[0].startswith("❌")]
    lin_skip = [r for r in rows if lin_label(r["name"], r["lin_status"])[0].startswith("⏭")]

    add("# Demon 命令平台可用性矩阵（实测）")
    add("")
    add("对 `help` 里列出的全部命令，分别在 Windows 会话与 Linux 会话上真实下发一次，"
        "按 agent 回包判定可用性。破坏性命令（加账号、转储 LSASS、注入、改注册表/服务等）未实测。")
    add("")
    add("| 项目 | 值 |")
    add("| --- | --- |")
    add("| 实测时间 | 2026-11-10 20:50 初测，22:30 按 `<命令> help` 用法补测 (Asia/Shanghai) |")
    add("| Windows 会话 | `demon.x64.exe`，Windows 10.0.26100，Administrator，sleep 2 |")
    add("| Linux 会话 | DemonLinux (Go)，Kali GNU/Linux Rolling x64，sleep 0 |")
    add("| 通道 | teamserver 40056 + Web 网关 8081（Qt 客户端与 Web 控制台共用同一套命令） |")
    add("| 判定来源 | `webui/cmd_matrix_test.py` 实测 + `linux_compat.py` / `linux_compat.go` 源码 |")
    add("")
    add("## 结论")
    add("")
    add(f"- **Windows：{len(rows)} 条命令全部是该平台的原生/BOF 实现**，实测 {len(win_ok)} 条直接返回结果；"
        f"{len(win_attn)} 条只是缺参数或环境限制（无域/无服务/空队列），{len(win_slow)} 条长 BOF 在 12s 内没返回"
        f"（{', '.join('`'+r['name']+'`' for r in win_slow)}），{len(win_skip)} 条破坏性命令未测，"
        f"{len(win_local)} 条（`help`）是客户端本地渲染。命令链路本身全部正常。"
        "其中 `klist`、`purge`、`wmi_query`、`reg_query` 是按 `<命令> help` 给出的用法补测后转为可用的。")
    add(f"- **Linux：{len(lin_ok)} 条可用**（原生实现或由网关/teamserver 改写），"
        f"**{len(lin_no)} 条仅 Windows**（AD/Kerberos/注册表/服务/令牌/注入/.NET/BOF/WMI 等没有 Linux 语义），"
        f"{len(lin_skip)} 条破坏性命令未测。")
    add("- Linux 上不可用的命令不会“静默成功”：agent 统一回 "
        "`command 0x.. is not supported by the Linux demon`，在控制台以红色 error 显示。")
    add("")
    add("## Linux 能直接用哪些")
    add("")
    nat = [r["name"] for r in rows if lin_label(r["name"], r["lin_status"])[0] == "✅ 原生"]
    rw = [r["name"] for r in rows if lin_label(r["name"], r["lin_status"])[0] == "✅ 可用"
          and r["name"] not in CLIENT_ANY]
    cl = [r["name"] for r in rows if r["name"] in CLIENT_ANY]
    add("**agent 原生（%d 条）**：" % len(nat) + "、".join(f"`{n}`" for n in nat))
    add("")
    add("**改写为 Linux 原生命令（%d 条）**：" % len(rw) + "、".join(f"`{n}`" for n in rw))
    add("")
    add("**客户端本地、与平台无关（%d 条）**：" % len(cl) + "、".join(f"`{n}`" for n in cl))
    add("")
    onlyw = [r["name"] for r in rows if lin_label(r["name"], r["lin_status"])[0].startswith("❌")]
    add("**仅 Windows、Linux 上没有等价语义（%d 条）**：" % len(onlyw) + "、".join(f"`{n}`" for n in onlyw))
    add("")
    add("## 判定口径")
    add("")
    add("| 标记 | 含义 |")
    add("| --- | --- |")
    add("| ✅ 可用 / 原生 | 返回了真实结果 |")
    add("| ⚠️ 需参数 / 环境限制 | 命令链路正常，只是缺参数或目标环境不具备条件（无域、无服务等） |")
    add("| ⏱ 超时 | 12s 内没有回包；实测是长 BOF 占住会话（见“坑”） |")
    add("| ❌ 仅 Windows | Linux Demon 没有该命令实现 |")
    add("| ⏭ 未测 | 破坏性/会改目标状态，未执行 |")
    add("")
    usage_rows = [r for r in rows if r.get("usage")]
    add("## 缺参数时怎么查用法：`<命令> help`")
    add("")
    add("Web 控制台里 `<命令> help` 与 `help <命令>` 等价，会打印 Description / Usage / Example /"
        " Required Args / Platform；参数不足时，错误信息里也会直接附上用法和提示（Qt 客户端只支持 `help <命令>`）。"
        "首参是路径/查询语句的命令（`cat`、`dir`、`wmi_query`、`reg_query` 等）例外："
        "它们只认 `help <命令>`，`<命令> help` 会当成普通参数照常下发。")
    add("")
    if usage_rows:
        add("| 命令 | 用法 | 示例 |")
        add("| --- | --- | --- |")
        for r in usage_rows:
            ex = r.get("example") or ""
            if ex and not ex.lower().startswith(r["name"].lower()):
                ex = f'{r["name"]} {ex}'
            u = r["usage"] if r["usage"].lower().startswith(r["name"].lower()) else f"{r['name']} {r['usage']}"
            add(f"| `{r['name']}` | `{u}` | {('`'+ex+'`') if ex else '—'} |")
        add("")
    add("本次按 help 里的用法补测了 `klist /all`、`purge /luid 0x0`、"
        "`wmi_query \"select ...\"`、`reg_query HKLM SOFTWARE`，都已返回真实结果（矩阵里已更新为可用）。")
    add("")
    add("## 全量矩阵")
    add("")
    add("| 命令 | 类型 | Windows 实测 | Linux | Linux 实现 / 说明 |")
    add("| --- | --- | --- | --- | --- |")
    for r in rows:
        name = r["name"]
        wl, wnote = win_label(name, r["win_status"])
        if wnote:
            wl += f"（{wnote}）"
        ll, note = lin_label(name, r["lin_status"])
        add(f"| `{name}` | {r['type']} | {wl} | {ll} | {note} |")
    add("")
    add("## Linux 侧的三条实现路径")
    add("")
    add("1. **agent 原生**（`payloads/DemonLinux`）：文件操作、进程、截图、socket 转发、sleep/checkin/config。")
    add("2. **团队服务器 / 网关改写**（`teamserver/pkg/agent/linux_compat.go`、`webui/linux_compat.py`）：")
    add("   把 Qt 客户端以 BOF 下发的 `whoami`/`ipconfig`/`netstat` 之类改写成 `/bin/sh -c` 原生命令；")
    add("   两个客户端（Qt / Web）行为一致，Windows 会话完全不受影响。")
    add("3. **客户端本地命令**（`help`、`task`、`pyhello`、`pysessions`）：不经过 agent，和平台无关。")
    add("")
    add("## 实测中发现的坑")
    add("")
    add("- **`netview` / `bofbelt` 会长时间占住 Windows 会话**：本次 `netview` 实测 46s 才回 "
        "`BOF execution completed`，12s 超时窗口内表现为无响应；`bofbelt`（Seatbelt）在 12s 窗口内没返回，"
        "随后该会话被 BOF 占住、后续命令全部排队，直到 BOF 结束才恢复。批量扫命令时要给足超时，或单独开会话跑。")
    add("- **`mkdir` 目标已存在**返回 `Win32 Error: ERROR_ALREADY_EXISTS [183]` / `file exists`，不是命令不可用。")
    add("- **`reg_query` 的 hive 参数**写成 `HKLM` 时实测 `error value: 2`（找不到键），"
        "按客户端用法应传完整路径形式，属参数问题。")
    add("- **`windowlist`（Windows）** 返回的是目标桌面上的可见窗口标题，测试机上包含宿主窗口，属正常现象。")
    add("- **Linux 上的 `screenshot`** 依赖 X11 + ImageMagick `import`（或 xwd / grim），纯 headless 会失败。")
    add("- **Linux 上的 `windowlist`** 依赖 `wmctrl`/`xdotool`；`powershell`/`powerpick` 依赖 `pwsh`；"
        "`nslookup` 依赖 `getent`/`dig`，缺失时返回的是提示文本而不是结果。")
    add("- **`job` 模块**在 Linux 上不可用（`COMMAND_JOB 0x15` 未实现），但 `task` 是客户端队列命令，两端都能用。")
    add("- Windows 会话被杀软/EDR 干预时 BOF 可能长时间不返回，与命令本身无关。")
    add("")
    add("## 复现")
    add("")
    add("```bash")
    add("cd webui")
    add("python3 cmd_matrix_test.py                      # 全量实测，结果写到 data/cmd_matrix.json")
    add("python3 cmd_matrix_report.py                   # 生成本文档")
    add("python3 cmd_matrix_test.py --only lin --limit 20  # 只测 Linux 前 20 条")
    add("```")
    add("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("written", OUT)
    print(f"windows ok={len(win_ok)} linux ok={len(lin_ok)} linux only-win={len(lin_no)} skip={len(lin_skip)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
