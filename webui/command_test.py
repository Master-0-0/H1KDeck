#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demon 命令层验收：help 全量表 + 只读命令真实回显 + 文件上传执行。

运行前需要 gateway (:8081) 与 teamserver 已启动，且至少一个健康 Demon。
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

import aiohttp

BASE = "http://127.0.0.1:8081"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def console_text(session, demon: str, limit: int = 400) -> str:
    async with session.get(f"{BASE}/api/sessions/{demon}/console?limit={limit}") as resp:
        data = await resp.json()
    return "\n".join(line.get("text", "") for line in data.get("lines", []))


async def wait_for(session, demon: str, needle: str, timeout: float = 35.0,
                   before: int = 0) -> tuple[bool, str]:
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        text = await console_text(session, demon)
        if needle in text[before:]:
            return True, text
        await asyncio.sleep(1.0)
    return False, text


async def send(session, demon: str, command: str) -> tuple[int, dict]:
    async with session.post(f"{BASE}/api/sessions/{demon}/command",
                            json={"command": command}) as resp:
        return resp.status, await resp.json()


async def main() -> int:
    sys.path.insert(0, str(HERE))
    import commands as command_meta  # noqa: WPS433

    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.post(f"{BASE}/api/login",
                                json={"username": "admin", "password": "havoc1234"}) as resp:
            check("Web 登录", resp.status == 200, f"status={resp.status}")

        # ---------- 数据包构造单元检查（不向目标发送） ----------
        import base64 as _b64
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="havoc-cmdtest-"))
        dll = tmp / "module.dll"
        dll.write_bytes(b"MZ" + b"\x00" * 32)
        sh = tmp / "shellcode.bin"
        sh.write_bytes(b"\x90" * 16)
        obj = tmp / "object.o"
        obj.write_bytes(b"\x00" * 8)
        asm = tmp / "assembly.exe"
        asm.write_bytes(b"MZ" + b"\x00" * 8)
        up = tmp / "upload.txt"
        up.write_bytes(b"hello upload")
        sess = command_meta.SessionRef("TESTDEMON")

        try:
            built = command_meta.build(sess, f"dll inject 1337 {dll} a b", "T1")
            ok = (built.info["CommandID"] == "22" and built.info["PID"] == "1337"
                  and built.info["Arguments"] == "a b"
                  and built.info["Binary"] == _b64.b64encode(dll.read_bytes()).decode())
            built2 = command_meta.build(sess, f"dll spawn {dll} x", "T2")
            ok = ok and built2.info["CommandID"] == "26" and built2.info["Arguments"] == _b64.b64encode(b"x").decode()
            built3 = command_meta.build(sess, f"shellcode inject x64 1337 {sh}", "T3")
            ok = ok and built3.info["Way"] == "Inject" and built3.info["PID"] == "1337" and built3.info["Arch"] == "x64"
            built4 = command_meta.build(sess, f"shellcode spawn x86 {sh}", "T4")
            ok = ok and built4.info["Way"] == "Spawn" and built4.info["Arch"] == "x86"
            built5 = command_meta.build(sess, f"dotnet inline-execute {asm} -group=user", "T5")
            ok = ok and built5.info["CommandID"] == "8193" and built5.info["Arguments"] == "-group=user"
            built6 = command_meta.build(sess, f"inline-execute {obj} hello", "T6")
            ok = ok and built6.info["CommandID"] == "20" and built6.info["Arguments"] == _b64.b64encode(b"hello").decode()
            check("注入/对象文件数据包字段", ok)
        except Exception as exc:
            check("注入/对象文件数据包字段", False, repr(exc))

        try:
            built = command_meta.build(sess, f"upload {up} C:\\Windows\\Temp\\", "T7")
            expected = (_b64.b64encode(b"C:\\Windows\\Temp\\upload.txt").decode() + ";"
                        + _b64.b64encode(b"hello upload").decode())
            check("upload 数据包字段", built.info.get("SubCommand") == "upload"
                  and built.info.get("Arguments") == expected)
        except Exception as exc:
            check("upload 数据包字段", False, repr(exc))

        # 第三方 Agent 通用 AgentCommand 路径（不带 CommandID）
        generic_session = command_meta.SessionRef(
            "SVC01", agent_type="Service",
            commands=[{"Name": "demo", "Params": [
                {"Name": "Arg1", "IsFilePath": False, "IsOptional": False},
                {"Name": "Arg2", "IsFilePath": False, "IsOptional": True},
            ]}])
        try:
            built = command_meta.build(generic_session, "demo hello", "T8")
            check("第三方 Agent 通用命令路径",
                  built.info.get("Command") == "demo" and built.info.get("Arg1") == "hello"
                  and built.info.get("Arg2") == "" and "CommandID" not in built.info)
        except Exception as exc:
            check("第三方 Agent 通用命令路径", False, repr(exc))

        # 动态 RegisterModule / 模块子命令的 help 合并
        dyn_extra = [{"key": "testmod foo", "module": "testmod", "command": "foo",
                      "description": "test command", "usage": "[x]", "example": "1"}]
        dyn_modules = [{"name": "testmod", "description": "test module",
                        "usage": "[cmd] (args)", "example": "", "behavior": ""}]
        dyn_help = command_meta.render_help(dyn_extra, dyn_modules)
        dyn_mod_help = command_meta.render_command_help("testmod", None, dyn_extra, dyn_modules)
        dyn_cmd_help = command_meta.render_command_help("testmod", "foo", dyn_extra, dyn_modules)
        check("动态模块 help 合并",
              "testmod" in dyn_help and "Module" in dyn_help
              and "foo" in dyn_mod_help and "testmod foo" in dyn_cmd_help)

        # ---------- help 全量表 ----------
        async with session.get(f"{BASE}/api/help") as resp:
            help_text = (await resp.json()).get("help", "")
        pattern = re.compile(r"^  (\S+)\s+(Command|Module)\s+", re.M)
        rows = [(m.group(1), m.group(2)) for m in pattern.finditer(help_text)]
        expected = [(row[0], row[1]) for row in command_meta.help_rows()]
        missing = [row for row in expected if row not in rows]
        check("help 含 108 条 Demon 基线命令", len(expected) == 108 and not missing,
              f"expected={len(expected)} actual={len(rows)} missing={missing[:5]}")
        check("help 首条 adcs_enum / 末条 wmi_query",
              rows[:1] == [("adcs_enum", "Command")] and rows[-1:] == [("wmi_query", "Command")],
              f"{rows[:1]} .. {rows[-1:]}")
        check("help Description 中文化",
              "使用 Win32 函数枚举" in help_text and "执行 WMI 查询" in help_text)
        modules = {name for name, typ in rows if typ == "Module"}
        check("help 模块类型", {"config", "dll", "dotnet", "job", "jump-exec", "net", "pivot",
                                "proc", "rportfwd", "shellcode", "socks", "task", "token"} <= modules,
              f"{sorted(modules)}")
        transfer = [typ for name, typ in rows if name == "transfer"]
        check("transfer 显示为 Command", transfer == ["Command"], f"{transfer}")

        async with session.get(f"{BASE}/api/help", params={"command": "config"}) as resp:
            config_help = (await resp.json()).get("help", "")
        check("help config 含子命令与 Usage",
              "inject.spawn64" in config_help and " - Usage" in config_help
              and " - Example" in config_help)
        async with session.get(f"{BASE}/api/help", params={"command": "dotnet", "sub": "execute"}) as resp:
            dotnet_help = (await resp.json()).get("help", "")
        check("help dotnet execute 含客户端 Usage",
              "dotnet execute" in dotnet_help and "/path/to/assembl.exe" in dotnet_help)

        # ---------- 选择健康会话 ----------
        async with session.get(f"{BASE}/api/state") as resp:
            state = await resp.json()
        healthy = [s for s in state.get("sessions", []) if s.get("health") == "healthy"]
        check("存在健康会话", bool(healthy), f"{len(healthy)} 个")
        if not healthy:
            return finish()
        demon = healthy[0]["id"]

        status, body = await send(session, demon, "definitely-not-a-command")
        check("未知命令返回 400", status == 400 and "Command/Module not found" in body.get("message", ""),
              body.get("message", ""))

        status, body = await send(session, demon, "proc help")
        proc_help = body.get("help", "")
        check("模块内 proc help", status == 200 and "Command       :  proc" in proc_help
              and "create" in proc_help)
        status, body = await send(session, demon, "config help inject.spawn64")
        cfg_help = body.get("help", "")
        check("模块内 config help <sub>", status == 200 and "config inject.spawn64" in cfg_help
              and "Usage" in cfg_help)

        # ---------- 静态 FS / 进程 / job ----------
        static_checks = [
            ("pwd", "Current directory"),
            ("dir .", "Directory of"),
            ("ps", "Process List"),
            ("job list", "Job list"),
            ("net users", "Users on"),
            ("pivot list", "pivot"),
            ("socks list", "Socks proxy"),
            ("transfer list", "download"),
            ("token list", "Token Vault"),
            ("luid", "LogonId"),
            ("dotnet list-versions", "assembly versions"),
        ]
        for cmd, needle in static_checks:
            before = len(await console_text(session, demon))
            status, body = await send(session, demon, cmd)
            ok = status == 200
            found = False
            if ok:
                found, _ = await wait_for(session, demon, needle, timeout=25, before=before)
            check(f"静态命令 {cmd}", ok and found, f"status={status}")
            await asyncio.sleep(0.5)

        # ---------- 模块只读 BOF ----------
        module_checks = [
            ("whoami", "S-1-5-21"),
            ("arp", "Interface"),
            ("ipconfig", "Ethernet"),
            ("env", "PATH="),
            ("netstat", "TCP"),
        ]
        for cmd, needle in module_checks:
            before = len(await console_text(session, demon))
            status, body = await send(session, demon, cmd)
            found = False
            if status == 200:
                found, text = await wait_for(session, demon, needle, timeout=40, before=before)
                if not found and needle == "Interface":
                    found = "Inteface" in text[before:]
            check(f"模块命令 {cmd}", status == 200 and found,
                  f"python={body.get('python')} status={status}")
            await asyncio.sleep(0.5)

        # ---------- 上传 + inline-execute ----------
        obj = ROOT / "havoc-src/client/Modules/SituationalAwareness/ObjectFiles/env.x64.o"
        if obj.is_file():
            form = aiohttp.FormData()
            form.add_field("file", obj.read_bytes(), filename=obj.name,
                           content_type="application/octet-stream")
            async with session.post(f"{BASE}/api/sessions/{demon}/upload", data=form) as resp:
                up = await resp.json()
            check("上传对象文件", resp.status == 200 and up.get("ok") and Path(up.get("path", "")).is_file(),
                  up.get("path", ""))
            path = up.get("path", "")
            before = len(await console_text(session, demon))
            status, body = await send(session, demon, f"inline-execute {path}")
            found, _ = await wait_for(session, demon, "Gathering Process Environment Variables",
                                      timeout=40, before=before)
            check("上传文件 inline-execute 回显", status == 200 and found, f"status={status}")
        else:
            check("上传对象文件", False, f"missing {obj}")

    return finish()


def finish() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n== {passed}/{total} 项通过 ==")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
