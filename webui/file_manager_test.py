#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 文件管理器回归测试（Windows / Linux 会话的增删改查）。

覆盖 REST API：
  list / mkdir / write / read / move / copy / upload(multipart) / download / remove(recursive)

用法：
    cd webui && python3 file_manager_test.py
可选环境变量：
    HAVOC_WEBUI=http://127.0.0.1:8081
    HAVOC_FM_LINUX=<session id>      指定 Linux 会话
    HAVOC_FM_WINDOWS=<session id>    指定 Windows 会话

前置：teamserver、Web 网关、至少一个 Linux 或 Windows Demon 在线。
仅用于已获得明确授权的测试环境。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
try:
    CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
except Exception:
    CONFIG = {}
BASE = os.environ.get("HAVOC_WEBUI", f"http://127.0.0.1:{CONFIG.get('web', {}).get('port', 8081)}")
USER = CONFIG.get("web", {}).get("user", "admin")
PASSWORD = CONFIG.get("web", {}).get("password", "havoc1234")

RESULTS: list[tuple[str, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def stamp() -> str:
    return str(int(time.time() * 1000))[-7:]


class API:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session

    async def get(self, url: str, **params):
        async with self.s.get(BASE + url, params=params) as r:
            return r.status, await r.read(), r.headers

    async def json_get(self, url: str, **params):
        status, body, _ = await self.get(url, **params)
        try:
            return status, json.loads(body)
        except Exception:
            return status, {"raw": body.decode("utf-8", "replace")}

    async def post(self, url: str, body: dict):
        async with self.s.post(BASE + url, json=body) as r:
            try:
                return r.status, await r.json()
            except Exception:
                return r.status, {"raw": await r.text()}

    async def upload(self, sid: str, remote_dir: str, name: str, content: bytes):
        form = aiohttp.FormData()
        form.add_field("path", remote_dir)
        form.add_field("file", content, filename=name, content_type="application/octet-stream")
        async with self.s.post(f"{BASE}/api/sessions/{sid}/files/upload", data=form) as r:
            try:
                return r.status, await r.json()
            except Exception:
                return r.status, {"raw": await r.text()}


def pick_sessions(sessions: list[dict]) -> dict[str, dict]:
    pinned_linux = os.environ.get("HAVOC_FM_LINUX", "")
    pinned_windows = os.environ.get("HAVOC_FM_WINDOWS", "")
    out: dict[str, dict] = {}
    for s in sessions:
        text = f"{s.get('platform') or ''} {s.get('os') or ''}".lower()
        if pinned_linux and s.get("id") == pinned_linux:
            out["linux"] = s
        if pinned_windows and s.get("id") == pinned_windows:
            out["windows"] = s
    if not out:
        for s in sessions:
            if s.get("health") != "healthy":
                continue
            text = f"{s.get('platform') or ''} {s.get('os') or ''}".lower()
            if "linux" in text and "linux" not in out:
                out["linux"] = s
            elif "windows" in text and "windows" not in out:
                out["windows"] = s
    return out


async def run_case(api: API, s: dict, platform: str) -> None:
    sid = s["id"]
    tag = f"{platform}({sid})"
    base = (f"/tmp/havoc-fm-{stamp()}" if platform == "linux"
            else rf"C:\Windows\Temp\havoc-fm-{stamp()}")
    sep = "/" if platform == "linux" else "\\"

    def p(*parts: str) -> str:
        return base + sep + sep.join(parts)

    parent = "/tmp" if platform == "linux" else r"C:\Windows\Temp"

    # 清理可能残留的同名目录
    await api.post(f"/api/sessions/{sid}/files/remove", {"path": base, "recursive": True})

    try:
        status, data = await api.post(f"/api/sessions/{sid}/files/mkdir", {"path": base})
        record(status == 200 and data.get("ok"), f"{tag} 新建目录", f"{status} {data.get('message','')[:80]}")
        status, data = await api.post(f"/api/sessions/{sid}/files/mkdir", {"path": base})
        record(status == 409, f"{tag} 重复新建目录返回 409", str(status))

        status, data = await api.post(f"/api/sessions/{sid}/files/write",
                                      {"path": p("a.txt"), "content": "第一行\nsecond\n", "encoding": "utf-8"})
        record(status == 200 and data.get("size") == len("第一行\nsecond\n".encode()),
               f"{tag} 新建文件", f"{status} {data.get('message','')[:80]}")

        status, data = await api.json_get(f"/api/sessions/{sid}/files/read", path=p("a.txt"))
        record(status == 200 and data.get("content") == "第一行\nsecond\n" and not data.get("binary"),
               f"{tag} 读取文本", f"{status} {str(data.get('content'))[:40]!r}")

        status, data = await api.post(f"/api/sessions/{sid}/files/write",
                                      {"path": p("a.txt"), "content": "edited\n", "encoding": "utf-8"})
        status2, data2 = await api.json_get(f"/api/sessions/{sid}/files/read", path=p("a.txt"))
        record(status == 200 and data2.get("content") == "edited\n", f"{tag} 覆盖编辑", str(status))

        status, data = await api.post(f"/api/sessions/{sid}/files/move",
                                      {"from": p("a.txt"), "to": p("b.txt")})
        status2, data2 = await api.json_get(f"/api/sessions/{sid}/files/read", path=p("b.txt"))
        record(status == 200 and data2.get("content") == "edited\n", f"{tag} 重命名", str(status))

        status, data = await api.post(f"/api/sessions/{sid}/files/copy",
                                      {"from": p("b.txt"), "to": p("c.txt")})
        status2, data2 = await api.json_get(f"/api/sessions/{sid}/files/read", path=p("c.txt"))
        record(status == 200 and data2.get("content") == "edited\n", f"{tag} 复制文件", str(status))

        status, data = await api.post(f"/api/sessions/{sid}/files/move",
                                      {"from": p("b.txt"), "to": p("c.txt")})
        record(status == 409, f"{tag} 重命名到已存在目标返回 409", str(status))

        payload = b"\x00\x01binary\xffdata"
        status, data = await api.upload(sid, base, "u.bin", payload)
        record(status == 200 and data.get("ok"), f"{tag} 浏览器上传", f"{status} {str(data)[:80]}")
        status, data = await api.json_get(f"/api/sessions/{sid}/files/read", path=p("u.bin"))
        record(status == 200 and data.get("binary") is True, f"{tag} 二进制识别", str(data.get("binary")))

        status, body, headers = await api.get(f"/api/sessions/{sid}/files/download", path=p("c.txt"))
        record(status == 200 and body == b"edited\n" and "attachment" in headers.get("Content-Disposition", ""),
               f"{tag} 下载文件", f"{status} {len(body)} bytes")

        status, data = await api.json_get(f"/api/sessions/{sid}/files", path=base)
        names = {e["name"] for e in data.get("entries", [])}
        record(status == 200 and {"c.txt", "u.bin"}.issubset(names), f"{tag} 目录列表", str(sorted(names)))

        status, data = await api.post(f"/api/sessions/{sid}/files/mkdir", {"path": p("sub")})
        status2, data2 = await api.post(f"/api/sessions/{sid}/files/write",
                                        {"path": p("sub", "deep.txt"), "content": "deep"})
        record(status == 200 and status2 == 200, f"{tag} 子目录与文件", f"{status}/{status2}")

        status, data = await api.post(f"/api/sessions/{sid}/files/remove", {"path": base, "recursive": True})
        record(status == 200 and data.get("ok"), f"{tag} 递归删除目录", f"{status} {str(data)[:80]}")
        status, data = await api.json_get(f"/api/sessions/{sid}/files", path=parent)
        names = {e["name"] for e in data.get("entries", [])}
        record(status == 200 and base.split(sep)[-1] not in names, f"{tag} 删除后列表确认", str(status))
    finally:
        try:
            await api.post(f"/api/sessions/{sid}/files/remove", {"path": base, "recursive": True})
        except Exception:
            pass


async def main() -> int:
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.post(BASE + "/api/login", json={"username": USER, "password": PASSWORD}) as r:
            if r.status != 200:
                print(f"登录失败：HTTP {r.status} {await r.text()}", file=sys.stderr)
                return 2
        api = API(session)
        status, data = await api.json_get("/api/sessions")
        sessions = data.get("sessions", [])
        if not sessions:
            print("没有在线会话", file=sys.stderr)
            return 2
        targets = pick_sessions(sessions)
        if not targets:
            print("没有可用于测试的 Linux/Windows 会话", file=sys.stderr)
            return 2
        print("目标会话：" + ", ".join(f"{k}={v['id']}" for k, v in targets.items()))
        for platform, s in targets.items():
            await run_case(api, s, platform)

    passed = sum(1 for x in RESULTS if x[0] == "PASS")
    total = len(RESULTS)
    print(f"\n{passed}/{total} PASS")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
