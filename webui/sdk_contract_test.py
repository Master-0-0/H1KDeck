#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Python SDK 契约测试（离线，不依赖运行中的网关/teamserver）。

覆盖：
  - 客户端同名 API 是否存在且签名对齐
  - GetDemons / GetListeners / GetAgents
  - GeneratePayload 的 base64 回调、409 busy 异常
  - LoadScript 的 True/False/403 语义
  - Demon.DllInject / DotnetInlineExecute / ProcessCreate / ShellcodeSpawn 命令构造
  - ProcessCreate 字段顺序与 teamserver 解析一致（state;verbose;piped;app;b64(cmdline)）
  - Agent 元数据 / ConsoleWrite / Command 明确 NotImplementedError
  - RegisterCallback / Event.OnNewSession
  - register.py 上报流程（本地 HTTP stub）
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYSDK = HERE / "pysdk"
sys.path.insert(0, str(PYSDK))
sys.path.insert(0, str(HERE))

# 必须在 import havoc 之前设置，避免真实联网
os.environ.setdefault("HAVOC_WEB_URL", "http://127.0.0.1:9")
os.environ.setdefault("HAVOC_WEB_TOKEN", "contract-test-token")

import havoc  # noqa: E402
import commands as cmdmeta  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class MockHTTP:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.routes: dict[tuple[str, str], object] = {}
        self.binary = b""

    def req(self, method: str, path: str, payload=None, timeout: float = 30.0):
        self.calls.append((method, path, payload))
        key = (method, path.split("?")[0])
        for (m, prefix), value in self.routes.items():
            if m == method and path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value(payload) if callable(value) else value
        raise AssertionError(f"unexpected request {method} {path}")

    def req_bytes(self, method: str, path: str, timeout: float = 60.0) -> bytes:
        self.calls.append((method, path, None))
        return self.binary

    def find(self, method: str, prefix: str):
        for m, p, body in reversed(self.calls):
            if m == method and p.startswith(prefix):
                return body
        return None


def test_signatures() -> None:
    import inspect
    expected = {
        "LoadScript": ["path"],
        "GetDemons": [],
        "GetListeners": [],
        "GetAgents": [],
        "GeneratePayload": ["callback", "agent", "listener", "arch", "format", "config"],
        "RegisterCallback": ["callback"],
    }
    for name, params in expected.items():
        fn = getattr(havoc, name, None)
        ok = callable(fn)
        actual = list(inspect.signature(fn).parameters) if ok else []
        check(f"SDK 属性 {name}", ok and actual == params, str(actual))
    for name, params in {
        "DllInject": ["self", "task_id", "pid", "dll_path", "dll_args"],
        "DotnetInlineExecute": ["self", "task_id", "path", "arguments"],
        "ProcessCreate": ["self", "task_id", "app", "cmdline", "suspended", "piped", "verbose"],
        "ShellcodeSpawn": ["self", "task_id", "technique", "arch", "path", "args"],
    }.items():
        fn = getattr(havoc.Demon, name, None)
        actual = list(inspect.signature(fn).parameters) if callable(fn) else []
        check(f"Demon.{name} 签名", callable(fn) and actual == params, str(actual))
    check("Event.OnNewSession 存在", callable(getattr(havoc.Event, "OnNewSession", None)))


def test_globals() -> None:
    mock = MockHTTP()
    havoc._req = mock.req  # type: ignore[assignment]
    mock.routes[("GET", "/api/sessions")] = {"sessions": [{"id": "AAA"}, {"id": "BBB"}]}
    check("GetDemons", havoc.GetDemons() == ["AAA", "BBB"])
    mock.routes[("GET", "/api/listeners")] = {"listeners": [{"name": "LAN-Test"}, {"name": "Hop2"}]}
    check("GetListeners", havoc.GetListeners() == ["LAN-Test", "Hop2"])
    check("GetAgents 本批只返回 Demon", havoc.GetAgents() == ["Demon"])


def test_load_script() -> None:
    mock = MockHTTP()
    havoc._req = mock.req  # type: ignore[assignment]
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "contract.py"
        script.write_text("x = 1\n", encoding="utf-8")
        mock.routes[("POST", "/api/scripts/load")] = {"ok": True, "commands": []}
        check("LoadScript 成功返回 True", havoc.LoadScript(str(script)) is True)

        mock.routes[("POST", "/api/scripts/load")] = havoc.HavocError("HTTP 400: bad")
        check("LoadScript 普通失败返回 False", havoc.LoadScript(str(script)) is False)

        mock.routes[("POST", "/api/scripts/load")] = havoc.HavocError("HTTP 403: forbidden")
        raised = False
        try:
            havoc.LoadScript(str(script))
        except havoc.HavocError as exc:
            raised = "路径不允许" in str(exc)
        check("LoadScript 越界抛明确异常", raised)

        check("LoadScript 文件不存在返回 False", havoc.LoadScript(str(script) + ".nope") is False)


def test_generate_payload() -> None:
    mock = MockHTTP()
    havoc._req = mock.req  # type: ignore[assignment]
    havoc._req_bytes = mock.req_bytes  # type: ignore[assignment]
    mock.binary = b"\x7fELF-payload"
    states = [{"builds": [{"id": "0007", "status": "building"}]},
              {"builds": [{"id": "0007", "status": "done", "path": "/tmp/x", "filename": "x"}]}]

    def payloads(_payload=None):
        return states.pop(0) if states else {"builds": [{"id": "0007", "status": "done"}]}

    mock.routes[("POST", "/api/payloads/build")] = {"ok": True, "build": {"id": "0007"}}
    mock.routes[("GET", "/api/payloads")] = payloads
    got = []
    havoc.GeneratePayload(lambda b64: got.append(b64), "Demon", "LAN-Test", "x64",
                          "Windows Exe", '{"sleep":"2"}')
    check("GeneratePayload 回调收到 base64", len(got) == 1 and
          base64.b64decode(got[0]) == b"\x7fELF-payload", str(len(got)))
    build_call = mock.find("POST", "/api/payloads/build")
    check("GeneratePayload config 字符串转对象", isinstance(build_call, dict) and
          build_call.get("config") == {"sleep": "2"} and build_call.get("agent") == "Demon")

    mock2 = MockHTTP()
    havoc._req = mock2.req  # type: ignore[assignment]
    mock2.routes[("POST", "/api/payloads/build")] = havoc.HavocError("HTTP 409: busy")
    raised = False
    try:
        havoc.GeneratePayload(lambda _b: None, "Demon", "L", "x64", "Windows Exe", "{}")
    except havoc.HavocError as exc:
        raised = "409" in str(exc)
    check("GeneratePayload 忙时明确异常", raised)

    raised = False
    try:
        havoc.GeneratePayload(lambda _b: None, "AgentX", "L", "x64", "Windows Exe", "{}")
    except havoc.HavocError as exc:
        raised = "Demon" in str(exc)
    check("GeneratePayload 非 Demon 明确报错", raised)


def _demo_session() -> dict:
    return {
        "id": "DEMO01", "listener": "LAN-Test", "external_ip": "1.2.3.4",
        "internal_ip": "10.0.0.1", "user": "Administrator", "host": "ADMIN",
        "domain": "", "os": "Windows 10", "os_build": "19045", "os_arch": "x64",
        "process": "demon.exe", "pid": "1234", "proc_arch": "x64",
        "magic": "deadbeef", "elevated": True, "sleep": 2, "jitter": 15,
        "last_call": "10-11-2026 23:00:00", "active": True,
    }


def test_demon_commands() -> None:
    mock = MockHTTP()
    havoc._req = mock.req  # type: ignore[assignment]
    mock.routes[("GET", "/api/sessions")] = {"session": _demo_session()}
    mock.routes[("POST", "/api/python/command")] = {"ok": True}
    d = havoc.Demon("DEMO01")
    check("Demon 元数据", d.User == "Administrator" and d.ProcessArch == "x64")

    d.DllInject("T1", "1337", "/tmp/a.dll", "arg1 arg2")
    body = mock.find("POST", "/api/python/command")
    check("DllInject 命令", body and body.get("command") == "dll inject 1337 /tmp/a.dll arg1 arg2",
          str(body))

    d.DotnetInlineExecute("T2", "/tmp/a.exe", "/k whoami")
    body = mock.find("POST", "/api/python/command")
    check("DotnetInlineExecute 命令", body and
          body.get("command") == "dotnet inline-execute /tmp/a.exe /k whoami", str(body))

    d.ShellcodeSpawn("T3", "default", "x64", "/tmp/sc.bin", b"hello args")
    body = mock.find("POST", "/api/python/command")
    check("ShellcodeSpawn 命令带 Args", body and
          body.get("command") == "shellcode spawn x64 /tmp/sc.bin hello args", str(body))

    mock.routes[("POST", "/api/python/proc-create")] = {"ok": True, "task_id": "T4"}
    out = d.ProcessCreate("T4", r"C:\Windows\System32\cmd.exe", "cmd.exe /c whoami",
                          True, True, False)
    body = mock.find("POST", "/api/python/proc-create")
    check("ProcessCreate API", out == "T4" and body and body.get("app").endswith("cmd.exe") and
          body.get("suspended") is True and body.get("verbose") is False)

    ref = cmdmeta.SessionRef("DEMO01")
    info = cmdmeta.build_proc_create(ref, "T4", r"C:\app.exe", 'app.exe "x y"',
                                     suspended=True, piped=True, verbose=False)
    parts = info["Args"].split(";")
    expected_b64 = base64.b64encode('app.exe "x y"'.encode()).decode()
    check("ProcessCreate 字段顺序", parts[:4] == ["4", "FALSE", "TRUE", r"C:\app.exe"] and
          parts[4] == expected_b64, info["Args"])
    info2 = cmdmeta.build_proc_create(ref, "T5", "app.exe", "app.exe", False, False, True)
    parts2 = info2["Args"].split(";")
    check("ProcessCreate 布尔映射", parts2[:3] == ["0", "TRUE", "FALSE"], info2["Args"])


def test_agent_and_callbacks() -> None:
    mock = MockHTTP()
    havoc._req = mock.req  # type: ignore[assignment]
    mock.routes[("GET", "/api/sessions")] = {"session": _demo_session()}
    mock.routes[("POST", "/api/python/console")] = {"ok": True}
    agent = havoc.Agent("DEMO01")
    check("Agent 元数据", agent.AgentID == "DEMO01" and agent.ProcessArch == "x64")
    agent.ConsoleWrite(agent.CONSOLE_INFO, "hello")
    body = mock.find("POST", "/api/python/console")
    check("Agent.ConsoleWrite", body and body.get("text") == "hello")
    raised = False
    try:
        agent.Command("T", "shell", b"x")
    except NotImplementedError as exc:
        raised = "第三方 Agent" in str(exc)
    check("Agent.Command 明确 NotImplementedError", raised)

    havoc.EVENT_CALLBACKS["new_session"].clear()
    seen = []
    havoc.RegisterCallback(lambda demon: seen.append(demon))
    havoc.Event.OnNewSession(lambda demon: seen.append("event:" + demon))
    for cb in list(havoc.EVENT_CALLBACKS["new_session"]):
        cb("D1")
    check("RegisterCallback/OnNewSession", seen == ["D1", "event:D1"], str(seen))


class _RegisterStub(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode())
        _RegisterStub.received.append(body)
        commands = []
        for item in body.get("commands", []):
            entry = dict(item)
            entry["key"] = (f"{entry.get('module')} {entry.get('command')}"
                            if entry.get("module") else entry.get("command"))
            commands.append(entry)
        data = json.dumps({"ok": True, "commands": commands,
                           "modules": body.get("modules", [])}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


def test_register_py() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RegisterStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "contract_script.py"
            script.write_text(
                "from havoc import RegisterCommand\n"
                "def contract_fn(demonID, *params):\n"
                "    return True\n"
                "RegisterCommand(contract_fn, '', 'contractcmd', 'contract test', 0, '', '')\n",
                encoding="utf-8")
            env = dict(os.environ)
            env["HAVOC_WEB_URL"] = f"http://127.0.0.1:{server.server_port}"
            env["HAVOC_WEB_TOKEN"] = "stub-token"
            env["PYTHONPATH"] = str(PYSDK) + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.run(
                [sys.executable, str(PYSDK / "register.py"), str(script)],
                env=env, capture_output=True, text=True, timeout=30)
            check("register.py 退出码", proc.returncode == 0,
                  (proc.stdout + proc.stderr)[-200:])
        payload = _RegisterStub.received[-1] if _RegisterStub.received else {}
        keys = [c.get("key", c.get("command")) for c in payload.get("commands", [])]
        check("register.py 上报 commands", "contractcmd" in keys, str(keys))
    finally:
        server.shutdown()


def main() -> int:
    test_signatures()
    test_globals()
    test_load_script()
    test_generate_payload()
    test_demon_commands()
    test_agent_and_callbacks()
    test_register_py()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n== {passed}/{len(RESULTS)} 项通过 ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
