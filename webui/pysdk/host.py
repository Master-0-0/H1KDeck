#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc Web 常驻 Python 宿主（由 gateway 懒启动）。

协议：stdin/stdout 上的 JSONL，每条请求一行：
    {"op":"ping"}
    {"op":"exec","code":"..."}
    {"op":"load","script":"/abs/path.py"}
    {"op":"reload","script":"/abs/path.py"}
    {"op":"unload","script":"/abs/path.py"}
    {"op":"invoke","script":"...","function":"...","demon":"<id>","args":["..."]}
    {"op":"event","name":"new_session","demon":"<id>"}

与 Qt 客户端一致：控制台与脚本共享同一个解释器上下文。控制台代码在 CONSOLE
命名空间执行；脚本函数在其模块命名空间执行，调用前后与控制台命名空间双向同步
普通全局变量。所有 stdout 由本进程捕获后随响应返回，协议写 sys.__stdout__。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import traceback
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import havoc  # noqa: E402

# 协议独占原始 stdout fd；把 fd 1 重定向到 stderr，防止脚本/子进程直接写 stdout
# 污染 JSONL 协议（网关会 drain stderr 到日志）。
_PROTOCOL_FD = os.dup(sys.stdout.fileno())
os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
PROTOCOL = os.fdopen(_PROTOCOL_FD, "w", encoding="utf-8", buffering=1)

CONSOLE: dict = {
    "__name__": "__console__",
    "havoc": havoc,
    "Demon": havoc.Demon,
    "Agent": getattr(havoc, "Agent", None),
    "Event": getattr(havoc, "Event", None),
    "Packer": havoc.Packer,
    "re": re,
    "RegisterCommand": havoc.RegisterCommand,
    "RegisterModule": havoc.RegisterModule,
    "RegisterCallback": havoc.RegisterCallback,
    "LoadScript": havoc.LoadScript,
    "GetDemons": havoc.GetDemons,
    "GetListeners": havoc.GetListeners,
    "GetAgents": havoc.GetAgents,
    "GeneratePayload": havoc.GeneratePayload,
    "list_sessions": havoc.list_sessions,
    "get_session": havoc.get_session,
    "chat": havoc.chat,
}
# script path -> module namespace dict
MODULES: dict[str, dict] = {}
# script path -> {registered function name: callable}
SCRIPT_FUNCTIONS: dict[str, dict] = {}


def reply(payload: dict) -> None:
    PROTOCOL.write(json.dumps(payload, ensure_ascii=False) + "\n")
    PROTOCOL.flush()


def _capture(fn, show_value: bool = True):
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            value = fn()
        text = out.getvalue()
        if value is not None and show_value:
            text += repr(value) + "\n"
        return True, text
    except SystemExit as exc:
        return False, out.getvalue() + f"SystemExit: {exc}\n"
    except BaseException:
        return False, out.getvalue() + traceback.format_exc()


def _public(items):
    return {k: v for k, v in items.items() if not str(k).startswith("__")}


def _sync(src: dict, dst: dict) -> None:
    for key, value in list(src.items()):
        if str(key).startswith("__"):
            continue
        dst[key] = value


def _module_namespace(script: str) -> dict:
    ns = {
        "__name__": "__havoc_script__",
        "__file__": script,
        "havoc": havoc,
        "Demon": havoc.Demon,
        "Agent": getattr(havoc, "Agent", None),
        "Packer": havoc.Packer,
        "re": re,
        "RegisterCommand": havoc.RegisterCommand,
        "RegisterModule": havoc.RegisterModule,
        "RegisterCallback": havoc.RegisterCallback,
        "list_sessions": havoc.list_sessions,
        "get_session": havoc.get_session,
        "chat": havoc.chat,
    }
    for key, value in _public(CONSOLE).items():
        ns.setdefault(key, value)
    return ns


def _reset_registrations() -> None:
    havoc.REGISTERED.clear()
    havoc.REGISTERED_MODULES.clear()
    havoc.REGISTERED_OBJECTS.clear()


def _report_registrations(script: str, replace: bool) -> dict:
    """把 RegisterCommand/RegisterModule 的收集结果上报网关。"""
    payload = {
        "script": script,
        "commands": list(havoc.REGISTERED),
        "modules": list(havoc.REGISTERED_MODULES),
        "replace": bool(replace),
    }
    if not payload["commands"] and not payload["modules"] and not replace:
        return {"registered": 0, "modules": 0}
    try:
        havoc._req("POST", "/api/python/register", payload, timeout=30)
    except Exception as exc:
        return {"registered": 0, "modules": 0, "register_error": str(exc)}
    return {"registered": len(payload["commands"]), "modules": len(payload["modules"])}


def _functions_from_registrations(ns: dict) -> dict:
    fns = {}
    for entry in list(havoc.REGISTERED):
        name = str(entry.get("function", ""))
        obj = havoc.REGISTERED_OBJECTS.get(name) or ns.get(name)
        if callable(obj):
            fns[name] = obj
    return fns


def _chdir_for(script: str):
    try:
        old = os.getcwd()
    except OSError:
        old = None
    try:
        os.chdir(os.path.dirname(script))
    except OSError:
        pass
    return old


def _restore_cwd(old) -> None:
    if old:
        try:
            os.chdir(old)
        except OSError:
            pass


def load_script(script: str, replace: bool = True, report: bool = True) -> dict:
    script = str(Path(script).resolve())
    path = Path(script)
    if not path.is_file():
        return {"ok": False, "error": f"script not found: {script}"}
    try:
        source = path.read_text("utf-8", "replace")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    # 可能是控制台代码里调用 havoc.LoadScript：保存外层未上报的注册，导入完成后恢复。
    saved = (list(havoc.REGISTERED), list(havoc.REGISTERED_MODULES),
             dict(havoc.REGISTERED_OBJECTS))
    ns = _module_namespace(script)
    _reset_registrations()
    old_script = havoc.SCRIPT
    havoc.SCRIPT = script
    os.environ["HAVOC_SCRIPT"] = script
    old_cwd = _chdir_for(script)
    ok, text = _capture(lambda: exec(compile(source, script, "exec"), ns, ns))
    _restore_cwd(old_cwd)
    havoc.SCRIPT = old_script
    if not ok:
        _reset_registrations()
        havoc.REGISTERED[:] = saved[0]
        havoc.REGISTERED_MODULES[:] = saved[1]
        havoc.REGISTERED_OBJECTS.update(saved[2])
        return {"ok": False, "error": text, "output": text}
    MODULES[script] = ns
    SCRIPT_FUNCTIONS[script] = _functions_from_registrations(ns)
    if report:
        info = _report_registrations(script, replace=replace)
    else:
        # 为执行内置模块命令而隐式 import 时，不能把这些命令重新注册一遍：
        # 内置命令的中文 help 来自 demon_help_zh，覆盖会退化成脚本里的英文描述。
        info = {"registered": 0, "modules": 0}
    _reset_registrations()
    havoc.REGISTERED[:] = saved[0]
    havoc.REGISTERED_MODULES[:] = saved[1]
    havoc.REGISTERED_OBJECTS.update(saved[2])
    return {"ok": True, "output": text, "script": script,
            "functions": sorted(SCRIPT_FUNCTIONS[script]), **info}


def unload_script(script: str) -> dict:
    script = str(Path(script).resolve())
    MODULES.pop(script, None)
    SCRIPT_FUNCTIONS.pop(script, None)
    return {"ok": True, "script": script}


# 让 havoc.LoadScript 在宿主内直接导入，避免 HTTP 回调等待自身（死锁）。
havoc.LOAD_SCRIPT_LOCAL = load_script


def do_exec(code: str) -> dict:
    _reset_registrations()
    ns = CONSOLE
    ns.setdefault("__name__", "__console__")

    def run():
        try:
            return eval(compile(code, "<console>", "eval"), ns, ns)
        except SyntaxError:
            exec(compile(code, "<console>", "exec"), ns, ns)
            return None

    ok, text = _capture(run)
    info = {"registered": 0, "modules": 0}
    if havoc.REGISTERED or havoc.REGISTERED_MODULES:
        fns = SCRIPT_FUNCTIONS.setdefault("<console>", {})
        for name, obj in havoc.REGISTERED_OBJECTS.items():
            if callable(obj):
                fns[name] = obj
        info = _report_registrations("<console>", replace=False)
        _reset_registrations()
    return {"ok": ok, "output": text, **info}


def do_invoke(script: str, function: str, demon: str, args: list) -> dict:
    script = str(Path(script).resolve()) if script != "<console>" else "<console>"
    if script == "<console>":
        ns = CONSOLE
        fns = SCRIPT_FUNCTIONS.setdefault("<console>", {})
    else:
        if script not in MODULES:
            loaded = load_script(script, replace=False, report=False)
            if not loaded.get("ok"):
                return {"ok": False, "output": loaded.get("error", "script load failed")}
        ns = MODULES[script]
        fns = SCRIPT_FUNCTIONS.setdefault(script, {})
        # 控制台里设置的全局变量在脚本函数内可见（与 Qt 客户端共享解释器一致）。
        _sync(CONSOLE, ns)
    fn = fns.get(function) or ns.get(function)
    if not callable(fn):
        return {"ok": False, "output": f"function not found: {script}::{function}"}
    old_script = havoc.SCRIPT
    havoc.SCRIPT = script if script != "<console>" else ""
    old_cwd = _chdir_for(script) if script != "<console>" else None
    try:
        ok, text = _capture(lambda: fn(demon, *args), show_value=False)
    finally:
        _restore_cwd(old_cwd)
        havoc.SCRIPT = old_script
    if script != "<console>":
        # 脚本写入的全局变量回传给控制台
        _sync(ns, CONSOLE)
    return {"ok": ok, "output": text, "script": script, "function": function}


def do_event(name: str, demon: str) -> dict:
    callbacks = list(havoc.EVENT_CALLBACKS.get(name, []))
    outputs = []
    ok = True
    for callback in callbacks:
        try:
            _ok, text = _capture(lambda cb=callback: cb(demon))
            ok = ok and _ok
            if text:
                outputs.append(text)
        except BaseException:
            ok = False
            outputs.append(traceback.format_exc())
    return {"ok": ok, "output": "\n".join(outputs), "callbacks": len(callbacks)}


def dispatch(request: dict) -> dict:
    op = str(request.get("op", ""))
    if op == "ping":
        return {"ok": True, "pid": os.getpid()}
    if op == "exec":
        return do_exec(str(request.get("code", "")))
    if op == "load":
        return load_script(str(request.get("script", "")), replace=True)
    if op == "reload":
        return load_script(str(request.get("script", "")), replace=True)
    if op == "unload":
        return unload_script(str(request.get("script", "")))
    if op == "invoke":
        raw_args = request.get("args") or []
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        return do_invoke(str(request.get("script", "")), str(request.get("function", "")),
                         str(request.get("demon", "")), args)
    if op == "event":
        return do_event(str(request.get("name", "")), str(request.get("demon", "")))
    return {"ok": False, "error": f"unknown op: {op}"}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            response = dispatch(request)
        except BaseException:
            response = {"ok": False, "error": traceback.format_exc()}
        reply(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
