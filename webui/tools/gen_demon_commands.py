#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Havoc 客户端源码生成 webui/demon_commands.py。

数据来源：
  1. client/src/Havoc/Demon/Commands.cc 的 DemonCommandList（35 条静态命令）；
  2. client/config.toml 的 [scripts].files（客户端启动时加载的 Python 模块）；
  3. 这些模块通过 RegisterCommand / RegisterModule 注册的命令。

用法：python3 webui/tools/gen_demon_commands.py
"""
from __future__ import annotations

import importlib.util
import os
import pprint
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "havoc-src" / "client"
OUT = ROOT / "webui" / "demon_commands.py"

RAW_RE = re.compile(r'^(?:u8)?R"(?P<delim>[^()]*)\((?P<body>.*)\)(?P=delim)"$', re.S)


def strip_comments(src: str) -> str:
    out: list[str] = []
    i = 0
    in_str = False
    esc = False
    while i < len(src):
        c = src[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            if j < 0:
                break
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = j + 2 if j >= 0 else len(src)
            continue
        out.append(c)
        i += 1
    return "".join(out)


def clean(value: str) -> str:
    m = RAW_RE.match(value)
    return m.group("body") if m else value


def find_matching(src: str, start: int) -> int:
    depth = 1
    i = start
    in_str = False
    esc = False
    while i < len(src) and depth > 0:
        c = src[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
        i += 1
    return i - 1


def parse_obj(s: str, pos: int) -> tuple[dict, int]:
    assert s[pos] == "{", (pos, s[pos:pos + 30])
    pos += 1
    obj: dict = {}
    subs: list = []
    while pos < len(s):
        while pos < len(s) and s[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(s):
            break
        if s[pos] == "}":
            return obj, pos + 1
        if s[pos] == ".":
            m = re.match(r"\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*", s[pos:])
            if not m:
                raise ValueError(f"field expected at {s[pos:pos+60]!r}")
            name = m.group(1)
            pos += m.end()
            if name == "SubCommands":
                assert s[pos] == "{"
                pos += 1
                while True:
                    while pos < len(s) and s[pos] in " \t\r\n,":
                        pos += 1
                    if s[pos] == "}":
                        pos += 1
                        break
                    if s[pos] == "{":
                        sub, pos = parse_obj(s, pos)
                        subs.append(sub)
                        continue
                    raise ValueError(f"sub expected {s[pos:pos+60]!r}")
                obj["SubCommands"] = subs
            elif s[pos] == "{":
                pos = find_matching(s, pos + 1) + 1
            elif s[pos] == '"':
                pos += 1
                buf: list[str] = []
                while pos < len(s):
                    c = s[pos]
                    if c == "\\":
                        nxt = s[pos + 1]
                        buf.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(nxt, nxt))
                        pos += 2
                        continue
                    if c == '"':
                        pos += 1
                        break
                    buf.append(c)
                    pos += 1
                obj[name] = clean("".join(buf))
            elif s.startswith('R"(', pos):
                end = s.find(')"', pos + 3)
                if end < 0:
                    raise ValueError("unterminated raw string")
                obj[name] = s[pos + 3:end]
                pos = end + 2
            else:
                m = re.match(r"[^,\n}]+", s[pos:])
                obj[name] = m.group(0).strip()
                pos += m.end()
        elif s.startswith("NO_SUBCOMMANDS", pos):
            pos += len("NO_SUBCOMMANDS")
            obj["Module"] = "false"
        else:
            raise ValueError(f"unexpected {s[pos:pos+60]!r}")
    raise ValueError("unterminated object")


def parse_static() -> list[dict]:
    src = strip_comments((CLIENT / "src" / "Havoc" / "Demon" / "Commands.cc").read_text("utf-8"))
    start = src.index("DemonCommandList = {")
    body_start = start + len("DemonCommandList = {")
    body = src[body_start:find_matching(src, body_start)]
    objs: list[dict] = []
    pos = 0
    while True:
        while pos < len(body) and body[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(body):
            break
        if body[pos] != "{":
            raise ValueError(f"top-level unexpected {body[pos:pos+50]!r}")
        obj, pos = parse_obj(body, pos)
        objs.append(obj)
    behavior = {
        "BEHAVIOR_PROCESS_INJECTION": "Process Injection",
        "BEHAVIOR_PROCESS_CREATION": "Process Creation",
        "BEHAVIOR_FORK_AND_RUN": "Fork & Run",
        "BEHAVIOR_API_ONLY": "API Only",
        "BEHAVIOR_TEAMSERVER": "Teamserver side",
    }

    def conv(obj: dict) -> dict:
        out = {
            "command": obj.get("CommandString", ""),
            "description": obj.get("Description", ""),
            "behavior": behavior.get(obj.get("Behavior", ""), obj.get("Behavior", "")),
            "usage": obj.get("Usage", ""),
            "example": obj.get("Example", ""),
            "module": str(obj.get("Module", "")).lower() == "true",
            "subcommands": [],
        }
        for sub in obj.get("SubCommands", []):
            out["subcommands"].append({
                "command": sub.get("CommandString", ""),
                "description": sub.get("Description", ""),
                "behavior": behavior.get(sub.get("Behavior", ""), sub.get("Behavior", "")),
                "usage": sub.get("Usage", ""),
                "example": sub.get("Example", ""),
            })
        return out

    return [conv(o) for o in objs]


def load_modules() -> tuple[list[dict], list[dict], list[str]]:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / "webui" / "pysdk"))
    import havoc  # noqa: WPS433

    configured = tomllib.loads((CLIENT / "config.toml").read_text("utf-8"))["scripts"]["files"]
    all_modules: list[list[str]] = []

    def record_module(*args, **kwargs):
        all_modules.append([str(a) for a in args])

    havoc.RegisterModule = record_module
    rows: list[dict] = []
    scripts: list[str] = []
    for rel in configured:
        path = CLIENT.parent / rel
        scripts.append(str(path.relative_to(ROOT)))
        havoc.REGISTERED.clear()
        sys.path.insert(0, str(path.parent))
        try:
            spec = importlib.util.spec_from_file_location(
                "webui_gen_" + path.stem.replace("-", "_"), path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        for reg in havoc.REGISTERED:
            row = dict(reg)
            row["script"] = str(path.relative_to(ROOT))
            row["behavior"] = int(row.get("behavior") or 0)
            rows.append(row)
    seen: dict[tuple[str, str], dict] = {}
    for row in rows:
        seen[(row.get("module", ""), row.get("command", ""))] = row
    rows = sorted(seen.values(), key=lambda r: (r.get("module", ""), r.get("command", "")))
    modules: list[dict] = []
    for args in all_modules:
        if len(args) < 6:
            continue
        if any(m["name"] == args[0] for m in modules):
            continue
        modules.append({
            "name": args[0], "description": args[1], "behavior": args[2],
            "usage": args[3], "example": args[4],
        })
    return rows, modules, scripts


def main() -> int:
    static = parse_static()
    registered, modules, scripts = load_modules()
    header = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc Demon 命令元数据（自动生成，勿手工编辑）。

生成器：webui/tools/gen_demon_commands.py
数据来源：
- client/src/Havoc/Demon/Commands.cc（DemonCommandList，35 条）
- client/config.toml 中启动时加载的 15 个 Python 模块（RegisterCommand/RegisterModule）

数据只描述命令；数据包构造见 commands.py。
"""
from __future__ import annotations

'''
    with OUT.open("w", encoding="utf-8") as fh:
        fh.write(header)
        for name, value in (
            ("STATIC_COMMANDS", static),
            ("REGISTERED_MODULES", modules),
            ("REGISTERED_COMMANDS", registered),
            ("MODULE_SCRIPTS", scripts),
        ):
            fh.write(f"{name} = ")
            fh.write(pprint.pformat(value, width=120, sort_dicts=False))
            fh.write("\n\n")
        fh.write('''STATIC_BY_NAME = {c["command"]: c for c in STATIC_COMMANDS}
REGISTERED_BY_KEY = {
    (c["module"] + " " + c["command"]) if c["module"] else c["command"]: c
    for c in REGISTERED_COMMANDS
}
MODULE_BY_NAME = {m["name"]: m for m in REGISTERED_MODULES}
''')
    print(f"wrote {OUT}: static={len(static)} modules={len(modules)} "
          f"registered={len(registered)} scripts={len(scripts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
