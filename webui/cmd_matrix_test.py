#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows / Linux 会话命令可用性矩阵实测。

对 help 里列出的每一条命令，分别在
  * 一个 Windows 会话（demon.x64.exe）
  * 一个 Linux 会话（DemonLinux，Go 版）
上真实下发一次（只使用只读 / 无副作用参数），按 agent 回包判定：

    可用       有实际输出
    空输出     回包成功但没有文本（checkin / sleep / config 之类）
    参数错误   客户端或 agent 报用法错误（链路正常，只缺参数）
    环境错误   因为缺少 AD/域/服务/桌面等环境而失败
    不支持     agent 明确回 "not supported by the Linux demon"
    帮助文本   客户端本地渲染的 help
    无响应     超时（多为长 BOF 占住会话，见 JAM 处理）
    跳过       有破坏性，未实测

用法:
    cd webui && python3 cmd_matrix_test.py            # 两个平台全量
    python3 cmd_matrix_test.py --only lin             # 只测 Linux
    python3 cmd_matrix_test.py --limit 10             # 只测前 10 条

前置：teamserver(40056) + webui(8081) 在线，且有 Windows / Linux 会话。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
BASE = f"http://127.0.0.1:{CONFIG['web'].get('port', 8081)}"
USER = CONFIG["web"].get("user", "admin")
PASS = CONFIG["web"].get("password", "havoc1234")

# (name, type, win line, linux line, speed)   line=None 表示跳过
# speed: 0 快 / 1 一般（BOF 或查询） / 2 重（Seatbelt、AD、驱动枚举等，可能长时间占用会话）
CASES: list[tuple[str, str, str | None, str | None, int]] = [
    ("adcs_enum", "Command", "adcs_enum", "adcs_enum", 2),
    ("adcs_request", "Command", None, None, 2),
    ("adduser", "Command", None, None, 2),
    ("addusertogroup", "Command", None, None, 2),
    ("arp", "Command", "arp", "arp", 1),
    ("bofbelt", "Command", "bofbelt", "bofbelt", 3),
    ("bofenv", "Command", "bofenv", "bofenv", 1),
    ("bofenvcb", "Command", "bofenvcb", "bofenvcb", 1),
    ("cacls", "Command", r"cacls C:\Windows\win.ini", "cacls /etc/hostname", 1),
    ("cat", "Command", r"cat C:\Windows\win.ini", "cat /etc/hostname", 0),
    ("cd", "Command", r"cd C:\Windows", "cd /tmp", 0),
    ("checkin", "Command", "checkin", "checkin", 0),
    ("config", "Module", "config", "config", 0),
    ("cp", "Command", r"cp C:\Windows\win.ini C:\Windows\Temp\hv_cp.ini", "cp /etc/hostname /tmp/hv_cp.txt", 0),
    ("dcenum", "Command", "dcenum", "dcenum", 2),
    ("dir", "Command", r"dir C:\Windows\Temp", "dir /tmp", 0),
    ("dll", "Module", None, "dll help", 2),
    ("domainenum", "Command", "domainenum", "domainenum", 2),
    ("dotnet", "Module", "dotnet list-versions", "dotnet list-versions", 1),
    ("download", "Command", r"download C:\Windows\win.ini", "download /etc/hostname", 0),
    ("driversigs", "Command", "driversigs", "driversigs", 2),
    ("enableuser", "Command", None, None, 2),
    ("enum_filter_driver", "Command", "enum_filter_driver", "enum_filter_driver", 2),
    ("enumlocalsessions", "Command", "enumlocalsessions", "enumlocalsessions", 1),
    ("env", "Command", "env", "env", 1),
    ("exit", "Command", None, None, 2),
    ("get-asrep", "Command", "get-asrep", "get-asrep", 2),
    ("get-delegation", "Command", "get-delegation Constrained", "get-delegation Constrained", 2),
    ("get-netsession", "Command", "get-netsession", "get-netsession", 1),
    ("get-spns", "Command", "get-spns", "get-spns", 2),
    ("get_password_policy", "Command", "get_password_policy", "get_password_policy", 2),
    ("help", "Command", "help", "help", 0),
    ("inline-execute", "Command", None, None, 2),
    ("ipconfig", "Command", "ipconfig", "ipconfig", 1),
    ("job", "Module", "job list", "job list", 0),
    ("jump-exec", "Module", None, None, 2),
    ("kerberoast", "Command", "kerberoast", "kerberoast", 2),
    ("klist", "Command", "klist /all", "klist /all", 1),
    ("ldapsearch", "Command", 'ldapsearch "(objectClass=user)"', 'ldapsearch "(objectClass=user)"', 2),
    ("listdns", "Command", "listdns", "listdns", 1),
    ("locale", "Command", "locale", "locale", 0),
    ("luid", "Command", "luid", "luid", 0),
    ("mkdir", "Command", r"mkdir C:\Windows\Temp\hvtest", "mkdir /tmp/hvtest", 0),
    ("mv", "Command", r"mv C:\Windows\Temp\hvtest C:\Windows\Temp\hvtest2", "mv /tmp/hvtest /tmp/hvtest2", 0),
    ("nanodump", "Command", None, None, 2),
    ("nanodump_ppl_dump", "Command", None, None, 2),
    ("nanodump_ppl_medic", "Command", None, None, 2),
    ("nanodump_ssp", "Command", None, None, 2),
    ("net", "Module", "net users", "net users", 1),
    ("netGroupList", "Command", "netGroupList", "netGroupList", 2),
    ("netGroupListMembers", "Command", "netGroupListMembers Users", "netGroupListMembers Users", 2),
    ("netLclGrpLstMmbrs", "Command", "netLclGrpLstMmbrs Administrators", "netLclGrpLstMmbrs Administrators", 1),
    ("netLocalGroupList", "Command", "netLocalGroupList", "netLocalGroupList", 1),
    ("netshares", "Command", "netshares", "netshares", 1),
    ("netsharesAdmin", "Command", "netsharesAdmin", "netsharesAdmin", 2),
    ("netstat", "Command", "netstat", "netstat", 1),
    ("netuptime", "Command", "netuptime", "netuptime", 1),
    ("netuser", "Command", "netuser Administrator", "netuser root", 1),
    ("netview", "Command", "netview", "netview", 2),
    ("noconsolation", "Command", None, None, 2),
    ("nslookup", "Command", "nslookup example.com", "nslookup example.com", 1),
    ("pivot", "Module", None, None, 2),
    ("powerpick", "Command", "powerpick Get-Date", "powerpick Get-Date", 1),
    ("powershell", "Command", "powershell whoami", "powershell whoami", 1),
    ("proc", "Module", "proc list", "proc list", 0),
    ("ptt", "Command", None, "ptt", 2),
    ("purge", "Command", "purge /luid 0x0", "purge /luid 0x0", 1),
    ("pwd", "Command", "pwd", "pwd", 0),
    ("pyhello", "Command", "pyhello", "pyhello", 0),
    ("pysessions", "Command", "pysessions", "pysessions", 0),
    ("quser", "Command", "quser", "quser", 1),
    ("reg_delete", "Command", None, "reg_delete HKLM SOFTWARE\\hvtestnovalue", 2),
    ("reg_query", "Command", "reg_query HKLM SOFTWARE",
     "reg_query HKLM SOFTWARE", 1),
    ("reg_query_recursive", "Command", r"reg_query_recursive HKLM SOFTWARE\Microsoft\Windows\CurrentVersion",
     "reg_query_recursive HKLM SOFTWARE", 1),
    ("reg_save", "Command", None, "reg_save HKLM SOFTWARE /tmp/hv.reg", 2),
    ("reg_set", "Command", None, "reg_set HKLM SOFTWARE\\hvtest X REG_SZ 1", 2),
    ("remove", "Command", r"remove C:\Windows\Temp\hv_cp.ini", "remove /tmp/hv_cp.txt", 0),
    ("resources", "Command", "resources", "resources", 1),
    ("routeprint", "Command", "routeprint", "routeprint", 1),
    ("rportfwd", "Module", "rportfwd list", "rportfwd list", 0),
    ("samdump", "Command", None, "samdump /tmp", 2),
    ("sc_create", "Command", None, "sc_create hvsvc hvsvc /bin/true d 1 2", 2),
    ("sc_delete", "Command", None, "sc_delete hvsvc", 2),
    ("sc_description", "Command", None, "sc_description Spooler test", 2),
    ("sc_enum", "Command", "sc_enum", "sc_enum", 2),
    ("sc_qc", "Command", "sc_qc Spooler", "sc_qc Spooler", 1),
    ("sc_qdescription", "Command", "sc_qdescription Spooler", "sc_qdescription Spooler", 1),
    ("sc_qfailure", "Command", "sc_qfailure Spooler", "sc_qfailure Spooler", 1),
    ("sc_qtriggerinfo", "Command", "sc_qtriggerinfo Spooler", "sc_qtriggerinfo Spooler", 1),
    ("sc_query", "Command", "sc_query Spooler", "sc_query Spooler", 1),
    ("sc_start", "Command", None, "sc_start hvsvc", 2),
    ("sc_stop", "Command", None, "sc_stop hvsvc", 2),
    ("schtasksenum", "Command", "schtasksenum", "schtasksenum", 2),
    ("schtasksquery", "Command", "schtasksquery", "schtasksquery", 2),
    ("screenshot", "Command", "screenshot", "screenshot", 1),
    ("sessions", "Command", "sessions", "sessions", 1),
    ("setuserpass", "Command", None, "setuserpass hvuser P@ssw0rd!", 2),
    ("shell", "Command", "shell echo hv-hello", "shell echo hv-hello", 0),
    ("shellcode", "Module", None, "shellcode help", 2),
    ("sleep", "Command", "sleep 2 0", "sleep 0 0", 0),
    ("socks", "Module", "socks list", "socks list", 0),
    ("task", "Module", "task list", "task list", 0),
    ("tasklist", "Command", "tasklist", "tasklist", 0),
    ("tgtdeleg", "Command", "tgtdeleg", "tgtdeleg", 2),
    ("token", "Module", "token list", "token list", 1),
    ("transfer", "Command", "transfer list", "transfer list", 0),
    ("upload", "Command", None, None, 2),
    ("uptime", "Command", "uptime", "uptime", 1),
    ("userenum", "Command", "userenum", "userenum", 1),
    ("whoami", "Command", "whoami", "whoami", 1),
    ("windowlist", "Command", "windowlist", "windowlist", 1),
    ("wmi_query", "Command", 'wmi_query "select Caption,Version from Win32_OperatingSystem"',
     'wmi_query "select Caption,Version from Win32_OperatingSystem"', 2),
]

SKIP_REASON = {
    "adcs_request": "会真实申请/注册证书",
    "adduser": "会创建账户",
    "addusertogroup": "会修改组成员",
    "dll": "DLL 注入/派生（Linux 侧仅能取到帮助文本）",
    "enableuser": "会启用账户",
    "exit": "会结束当前会话",
    "inline-execute": "需要本地 BOF 文件，且会执行代码",
    "jump-exec": "横向移动",
    "nanodump": "转储 LSASS",
    "nanodump_ppl_dump": "绕 PPL 转储 LSASS",
    "nanodump_ppl_medic": "绕 PPL 转储 LSASS",
    "nanodump_ssp": "向 LSASS 加载 SSP",
    "noconsolation": "内存执行 PE",
    "pivot": "建立代理链路",
    "reg_delete": "会删除注册表",
    "reg_save": "会导出注册表 hive 到目标磁盘",
    "reg_set": "会改注册表",
    "samdump": "转储 SAM/SECURITY/SYSTEM",
    "sc_create": "会创建服务",
    "sc_delete": "会删除服务",
    "sc_description": "会改服务描述",
    "sc_start": "会启动服务",
    "sc_stop": "会停止服务",
    "setuserpass": "会改账户口令",
    "shellcode": "shellcode 注入（Linux 侧仅能取到帮助文本）",
    "upload": "需要网关本地文件并写入目标",
}


def classify(kind: str | None, text: str) -> str:
    if kind is None:
        return "无响应"
    low = text.lower()
    if "not supported by the linux demon" in low:
        return "不支持"
    if kind == "local":
        return "帮助文本"
    if kind == "client":
        if "not enough arguments" in low or "specify a sub command" in low:
            return "参数错误"
        return "客户端拦截"
    if kind == "error":
        if any(k in low for k in ("usage", "argument", "requires", "specify a sub command",
                                  "invalid", "wrong", "unknown parameter", "missing")):
            return "参数错误"
        return "环境错误"
    if not text.strip():
        return "空输出"
    return "可用"


class Runner:
    def __init__(self, s: aiohttp.ClientSession, sids: list[str], timeout: float):
        self.s = s
        self.sids = list(sids)
        self.sid = self.sids[0]
        self.timeout = timeout
        self.last_ts = 0.0
        self.jammed = False

    async def _console(self) -> list[dict]:
        async with self.s.get(f"{BASE}/api/sessions/{self.sid}/console?limit=400") as r:
            return (await r.json())["lines"]

    async def sync(self) -> None:
        lines = await self._console()
        self.last_ts = max((l["ts"] for l in lines), default=0.0)

    async def switch(self) -> bool:
        if len(self.sids) <= 1:
            return False
        self.jammed = True
        self.sids.pop(0)
        self.sid = self.sids[0]
        await self.sync()
        return True

    async def _poll(self, t0: float, timeout: float) -> tuple[str | None, str]:
        first, chunks, quiet = None, [], 0.0
        while time.time() - t0 < timeout:
            await asyncio.sleep(0.2)
            try:
                lines = await self._console()
            except Exception:
                continue
            new = [l for l in lines if l["ts"] > self.last_ts]
            if new:
                self.last_ts = new[-1]["ts"]
                quiet = 0.0
                for l in new:
                    k, t = l["kind"], l.get("text") or ""
                    if k in ("output", "error", "good", "warn"):
                        first = first or k
                        chunks.append(t)
                    elif k == "info" and not t.startswith(("[*]", "Send Task", "Received Output", "Tasked")):
                        first = first or k
                        chunks.append(t)
            elif first is not None:
                quiet += 0.2
                if quiet >= 0.6:
                    break
        return first, "\n".join(chunks).strip()

    async def send(self, line: str) -> tuple[str, str]:
        await self.sync()
        t0 = time.time()
        async with self.s.post(f"{BASE}/api/sessions/{self.sid}/command", json={"command": line}) as r:
            body = await r.json()
            if r.status != 200:
                return "client", str(body.get("message") or body)
            if body.get("help"):
                return "local", body["help"]
        return await self._poll(t0, self.timeout)

    async def alive(self) -> bool:
        k, _ = await self.send("pwd")
        return k is not None


async def sweep(runner: Runner, cases, key: str, rows: dict, lock: asyncio.Lock) -> None:
    for name, typ, wline, lline, speed in cases:
        line = wline if key == "win" else lline
        row = rows[name]
        if not line:
            row[f"{key}_status"] = "跳过"
            row[f"{key}_out"] = SKIP_REASON.get(name, "破坏性/不适用")
            continue
        if runner.jammed and not await runner.alive():
            row[f"{key}_status"] = "未测(会话阻塞)"
            row[f"{key}_out"] = "前一条命令把会话占住，未继续"
            continue
        t0 = time.time()
        kind, text = await runner.send(line)
        status = classify(kind, text)
        row.update({f"{key}_status": status, f"{key}_out": text[:700],
                    f"{key}_kind": kind or "", f"{key}_secs": round(time.time() - t0, 2)})
        if status == "无响应":
            # 长 BOF 占住会话时，换下一个会话继续
            if await runner.switch():
                row[f"{key}_out"] = (row.get(f"{key}_out") or "") + f" [会话 {runner.sids[0]} 阻塞，已切换]"
        async with lock:
            print(f"{name:24s} {key}={status:10s} {row.get(f'{key}_secs', 0):5.1f}s "
                  f"{(text or '')[:70]!r}", flush=True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--win", help="Windows 会话 id，可逗号分隔（备用会话）")
    ap.add_argument("--lin", help="Linux 会话 id")
    ap.add_argument("--only", choices=["win", "lin", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--out", default=str(HERE / "data" / "cmd_matrix.json"))
    args = ap.parse_args()

    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as s:
        async with s.post(f"{BASE}/api/login", json={"username": USER, "password": PASS}) as r:
            if r.status != 200:
                print("login failed", r.status)
                return 2
        async with s.get(f"{BASE}/api/sessions") as r:
            sessions = (await r.json())["sessions"]

        win_ids = ([x.strip() for x in args.win.split(",")] if args.win else
                   [x["id"] for x in sessions
                    if not x.get("platform") and "Linux" not in str(x.get("os", ""))])
        lin_ids = ([args.lin] if args.lin else
                   [x["id"] for x in sessions if str(x.get("platform", "")).lower() == "linux"])
        if not win_ids or not lin_ids:
            raise SystemExit("需要至少一个 Windows 与一个 Linux 会话")
        print(f"windows={win_ids} linux={lin_ids}", flush=True)

        cases = sorted(CASES, key=lambda c: (c[4], c[0]))
        if args.limit:
            cases = cases[:args.limit]
        rows = {c[0]: {"name": c[0], "type": c[1], "win_line": c[2], "lin_line": c[3]}
                for c in cases}
        lock = asyncio.Lock()
        jobs = []
        if args.only in ("win", "both"):
            jobs.append(sweep(Runner(s, win_ids, args.timeout), cases, "win", rows, lock))
        if args.only in ("lin", "both"):
            jobs.append(sweep(Runner(s, lin_ids, args.timeout), cases, "lin", rows, lock))
        await asyncio.gather(*jobs)

        out = [rows[c[0]] for c in CASES if c[0] in rows]
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print("written", args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
