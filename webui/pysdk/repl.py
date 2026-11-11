#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持久 Python 控制台：从 stdin 读取 JSON {code}，在捕获 stdout 后以 JSON 行回复。"""
import contextlib
import io
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import havoc  # noqa: E402

NAMESPACE = {
    "havoc": havoc,
    "Demon": havoc.Demon,
    "RegisterCommand": havoc.RegisterCommand,
    "list_sessions": havoc.list_sessions,
    "get_session": havoc.get_session,
    "chat": havoc.chat,
    "__name__": "__console__",
}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            continue
        code = request.get("code", "")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    value = eval(compile(code, "<console>", "eval"), NAMESPACE)
                except SyntaxError:
                    exec(compile(code, "<console>", "exec"), NAMESPACE)
                    value = None
                if value is not None:
                    print(repr(value))
            response = {"ok": True, "output": out.getvalue()}
        except Exception:
            response = {"ok": False, "output": out.getvalue() + traceback.format_exc()}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
