#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行 Python 脚本注册的命令：run_command.py <script> <function> <demon> [args...]"""
import importlib.util
import os
import sys
import traceback

import havoc
import re

sys.dont_write_bytecode = True


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: run_command.py <script> <function> <demon> [args...]", file=sys.stderr)
        return 2
    script, function, demon_id = sys.argv[1], sys.argv[2], sys.argv[3]
    params = sys.argv[4:]
    path = os.path.abspath(script)
    if not os.path.isfile(path):
        print(f"script not found: {path}", file=sys.stderr)
        return 2
    os.environ["HAVOC_SCRIPT"] = path
    havoc.SCRIPT = path
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
    fn = getattr(module, function, None)
    if fn is None:
        print(f"function not found: {function}", file=sys.stderr)
        return 1
    try:
        result = fn(demon_id, *params)
    except NotImplementedError as exc:
        print(f"该脚本使用了 Web SDK 未实现的 API：{exc}", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
