#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加载一个 Havoc Python 脚本并注册其中 RegisterCommand 的命令。"""
import importlib.util
import os
import sys
import traceback

import havoc
import re

sys.dont_write_bytecode = True


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: register.py <script.py>", file=sys.stderr)
        return 2
    path = os.path.abspath(sys.argv[1])
    if not os.path.isfile(path):
        print(f"script not found: {path}", file=sys.stderr)
        return 2
    sys.path.insert(0, os.path.dirname(path))
    spec = importlib.util.spec_from_file_location("havoc_user_script", path)
    if spec is None or spec.loader is None:
        print("cannot load script", file=sys.stderr)
        return 2
    module = importlib.util.module_from_spec(spec)
    # 客户端把所有脚本放在同一个解释器里，Packer/re 等全局名字是共享的。
    module.__dict__.setdefault("Packer", havoc.Packer)
    module.__dict__.setdefault("re", re)
    try:
        spec.loader.exec_module(module)
    except Exception:
        traceback.print_exc()
        return 1

    if not havoc.REGISTERED and not havoc.REGISTERED_MODULES:
        print("脚本已加载，但没有注册命令。")
        return 0
    try:
        resp = havoc._req("POST", "/api/python/register",
                          {"script": path, "commands": havoc.REGISTERED,
                           "modules": havoc.REGISTERED_MODULES})
    except Exception as exc:
        print(f"注册失败：{exc}", file=sys.stderr)
        return 1
    for cmd in resp.get("commands", []):
        print(f"注册命令：{cmd['key']} — {cmd.get('description', '')}")
    for mod in resp.get("modules", []):
        print(f"注册模块：{mod['name']} — {mod.get('description', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
