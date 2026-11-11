#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话文件管理器（Windows / Linux Demon）。

只使用 Demon 原生 FS 命令（dir/mkdir/remove/mv/cp/upload/download）：
  * 增：mkdir、write（新建/覆盖）、upload
  * 删：remove；目录可用递归删除（Windows PowerShell / Linux rm -rf）
  * 改：move（重命名/移动）、write（编辑内容）
  * 查：list、read（文本预览）、download

teamserver 的 FS 回包没有 TaskID，这里按会话串行化操作，用控制台回执 +
目标目录复查来判定结果；下载则等待 teamserver 落盘后的 download done 事件。

仅用于已获得明确授权的测试环境。
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import ntpath
import posixpath
import re
import secrets
import time
from pathlib import Path
from typing import Optional

# 编辑/预览上限；FS upload 一次性把内容放进命令包，过大会拖慢 agent。
EDIT_MAX_BYTES = 1024 * 1024
UPLOAD_MAX_BYTES = 32 * 1024 * 1024
READ_HARD_MAX = 4 * 1024 * 1024

PS_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
CMD_EXE = r"c:\windows\system32\cmd.exe"


class FileOpError(Exception):
    """文件操作失败；status 直接映射到 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail or ""


def _b64_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _token(n: int = 4) -> str:
    return secrets.token_hex(n)


class FileManager:
    def __init__(self, gateway):
        self.gw = gateway
        self.locks: dict[str, asyncio.Lock] = {}
        self.queues: dict[str, asyncio.Queue] = {}
        self.cwd_cache: dict[str, tuple[str, float]] = {}
        self.bus_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self.bus_task is None:
            self.bus_task = asyncio.create_task(self._bus_loop())

    async def stop(self) -> None:
        if self.bus_task:
            self.bus_task.cancel()
            try:
                await self.bus_task
            except (asyncio.CancelledError, Exception):
                pass
            self.bus_task = None

    async def _bus_loop(self) -> None:
        q = self.gw.state.subscribe()
        try:
            while True:
                msg = await q.get()
                if msg.get("type") not in ("console", "fileexplorer", "download"):
                    continue
                demon = str(msg.get("demon") or "")
                bq = self.queues.get(demon)
                if bq is None:
                    continue
                try:
                    bq.put_nowait(msg)
                except asyncio.QueueFull:
                    pass
        finally:
            self.gw.state.unsubscribe(q)

    # ------------------------------------------------------------------ 基础
    def _lock(self, sid: str) -> asyncio.Lock:
        lock = self.locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[sid] = lock
        return lock

    def _queue(self, sid: str) -> asyncio.Queue:
        q = self.queues.get(sid)
        if q is None:
            q = asyncio.Queue(maxsize=2000)
            self.queues[sid] = q
        return q

    @staticmethod
    def _drain(q: asyncio.Queue) -> None:
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                return

    def session(self, sid: str) -> dict:
        sess = self.gw.state.sessions.get(sid)
        if not sess:
            raise FileOpError("会话不存在", 404)
        return sess

    @staticmethod
    def is_linux(sess: dict) -> bool:
        text = f"{sess.get('platform') or ''} {sess.get('os') or ''}".lower()
        return "linux" in text

    def _pmod(self, sess: dict):
        return posixpath if self.is_linux(sess) else ntpath

    def _op_timeout(self, sess: dict, minimum: float = 15.0) -> float:
        try:
            sleep = float(sess.get("sleep") or 0)
        except (TypeError, ValueError):
            sleep = 0.0
        return min(180.0, max(minimum, sleep * 2 + 10))

    def _check_connected(self) -> None:
        if not self.gw.client.connected:
            raise FileOpError("网关未连接 teamserver", 503)

    async def _send_fs(self, sid: str, sub: str, args: str, command_line: str) -> str:
        self._check_connected()
        task_id = f"{secrets.randbits(32):08X}"
        info = {
            "TaskID": task_id,
            "CommandLine": command_line,
            "DemonID": sid,
            "AgentType": "Demon",
            "CommandID": "15",
            "SubCommand": sub,
            "Arguments": args,
        }
        try:
            await self.gw.client.send_command(info)
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一转 502
            raise FileOpError(f"下发失败：{exc}", 502) from exc
        return task_id

    # ------------------------------------------------------------------ 路径
    def clean_path(self, sess: dict, raw: str) -> str:
        path = str(raw or "").strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in ("'", '"'):
            path = path[1:-1].strip()
        if any(ch in path for ch in ("\x00", "\n", "\r")):
            raise FileOpError("路径包含非法字符", 400)
        if not path:
            path = "/" if self.is_linux(sess) else "."
        if not self.is_linux(sess):
            path = path.replace("/", "\\")
        return path

    def split_path(self, sess: dict, path: str) -> tuple[str, str]:
        """返回 (父目录, 名称)；根路径的名称为空。"""
        mod = self._pmod(sess)
        p = path
        if self.is_linux(sess):
            if p not in ("/",):
                p = p.rstrip("/") or "/"
            if p == "/":
                return "/", ""
            parent = mod.dirname(p)
            if not parent:
                # 相对路径（agent CWD 下的 "dir/file"）父目录是 "."，不能当成根。
                parent = "/" if p.startswith("/") else "."
            return parent, mod.basename(p)
        drive, tail = mod.splitdrive(p)
        stripped = p.rstrip("\\")
        if not stripped or stripped == drive:
            return (stripped or drive or "\\"), ""
        if stripped.endswith(":\\"):
            return stripped, ""
        parent = mod.dirname(stripped) or (drive + "\\" if drive else ".")
        name = mod.basename(stripped)
        return parent, name

    def join_path(self, sess: dict, base: str, name: str) -> str:
        mod = self._pmod(sess)
        if not base or base == ".":
            return name
        return mod.join(base, name)

    def is_root(self, sess: dict, path: str) -> bool:
        parent, name = self.split_path(sess, path)
        return name == ""

    def _assert_mutable(self, sess: dict, path: str) -> None:
        if self.is_root(sess, path):
            raise FileOpError("不能对根目录执行该操作", 400)

    def _is_absolute(self, sess: dict, path: str) -> bool:
        if self.is_linux(sess):
            return path.startswith("/")
        return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")

    def invalidate_cwd(self, sid: str) -> None:
        self.cwd_cache.pop(sid, None)

    async def _pwd(self, sid: str, sess: dict) -> str:
        cached = self.cwd_cache.get(sid)
        if cached and time.monotonic() - cached[1] < 10.0:
            return cached[0]
        q = self._queue(sid)
        self._drain(q)
        await self._send_fs(sid, "pwd", "", "pwd")
        text = await self._wait_console(q, self._op_timeout(sess))
        low = text.lower()
        if "current directory:" in low:
            value = text.split(":", 1)[1].strip()
            if value:
                self.cwd_cache[sid] = (value, time.monotonic())
                return value
        return ""

    async def _abs_path(self, sid: str, sess: dict, path: str) -> str:
        """把 UI 传入的 "." / 相对路径解析成绝对路径，便于文件树显示与后续操作。

        旧版 Linux agent 的 dir 回包是 "./*" 这类相对 pattern，Web 端如果一直用
        相对路径，文件树会出现 "." 根节点；这里向 agent 询问 pwd 后拼接。
        """
        if not path or path == ".":
            cwd = await self._pwd(sid, sess)
            return cwd or path or ("/" if self.is_linux(sess) else ".")
        if self._is_absolute(sess, path):
            return path
        cwd = await self._pwd(sid, sess)
        if not cwd:
            return path
        return self.join_path(sess, cwd, path)

    @staticmethod
    def _same_name(sess_linux: bool, a: str, b: str) -> bool:
        return a == b if sess_linux else a.lower() == b.lower()

    def _find_entry(self, sess: dict, entries: list[dict], name: str) -> Optional[dict]:
        linux = self.is_linux(sess)
        for entry in entries:
            if self._same_name(linux, str(entry.get("name") or ""), name):
                return entry
        return None

    @staticmethod
    def _entry_is_dir(entry: Optional[dict]) -> bool:
        return bool(entry) and str(entry.get("type") or "").lower() == "dir"

    # ------------------------------------------------------------------ 消息等待
    async def _next(self, q: asyncio.Queue, deadline: float) -> dict:
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise FileOpError("操作超时（目标可能离线或 sleep 较长）", 504)
        try:
            return await asyncio.wait_for(q.get(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise FileOpError("操作超时（目标可能离线或 sleep 较长）", 504) from exc

    async def _collect_error(self, q: asyncio.Queue, timeout: float = 0.8) -> str:
        """CALLBACK_ERROR 的详细文本在随后的 output 行里。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        parts: list[str] = []
        while loop.time() < deadline:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=max(0.01, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            if msg.get("type") != "console":
                continue
            line = msg.get("line") or {}
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            parts.append(text)
            if line.get("kind") in ("error", "good", "info", "warn"):
                break
        return "\n".join(parts).strip()

    async def _wait_console(self, q: asyncio.Queue, timeout: float) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            msg = await self._next(q, deadline)
            if msg.get("type") != "console":
                continue
            line = msg.get("line") or {}
            kind = str(line.get("kind") or "").lower()
            text = str(line.get("text") or "").strip()
            if kind == "prompt" or not text:
                continue
            low = text.lower()
            if low.startswith("send task to agent") or low.startswith("tasked demon"):
                continue
            if kind in ("error", "erro"):
                detail = await self._collect_error(q)
                raise FileOpError(detail or text, 502, text)
            if low.startswith("received output"):
                # 某些 FS 错误以 Error 类型 + Output 文本返回
                detail = await self._collect_error(q)
                raise FileOpError(detail or text, 502, text)
            if kind in ("info", "good", "warn"):
                return text
            # 其他类型（如 output）继续等

    async def _wait_explorer(self, q: asyncio.Queue, timeout: float,
                             expected: str = "") -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            msg = await self._next(q, deadline)
            if msg.get("type") == "fileexplorer":
                if expected and not self._paths_equal(msg.get("path") or "", expected):
                    # 上一条超时的 dir 回包迟到，别当成当前请求的结果
                    continue
                return msg
            if msg.get("type") != "console":
                continue
            line = msg.get("line") or {}
            kind = str(line.get("kind") or "").lower()
            text = str(line.get("text") or "").strip()
            if kind in ("error", "erro"):
                detail = await self._collect_error(q)
                raise FileOpError(detail or text or "目录读取失败", 404)
            if kind == "prompt" or not text:
                continue
            low = text.lower()
            if low.startswith("send task to agent") or low.startswith("tasked demon"):
                continue
            if low.startswith("received output"):
                detail = await self._collect_error(q)
                raise FileOpError(detail or text or "目录读取失败", 404)

    @staticmethod
    def _paths_equal(a: str, b: str) -> bool:
        a = str(a or "").replace("\\", "/").rstrip("/")
        b = str(b or "").replace("\\", "/").rstrip("/")
        if not a or not b:
            return a == b
        return a.lower() == b.lower()

    # ------------------------------------------------------------------ 查
    async def list_dir(self, sid: str, raw_path: str = ".") -> dict:
        sess = self.session(sid)
        path = self.clean_path(sess, raw_path)
        async with self._lock(sid):
            path = await self._abs_path(sid, sess, path)
            entries, actual = await self._list_core(sess, sid, path)
            return {"path": actual, "entries": entries}

    async def _list_core(self, sess: dict, sid: str, path: str,
                         depth: int = 0) -> tuple[list[dict], str]:
        """列目录；兼容 Linux agent 对空目录返回失败的情况。"""
        q = self._queue(sid)
        self._drain(q)
        await self._send_fs(sid, "dir;ui", path, f"dir {path}")
        try:
            msg = await self._wait_explorer(q, self._op_timeout(sess), expected=path)
            return msg.get("entries") or [], msg.get("path") or path
        except FileOpError as exc:
            # Windows Demon 对空目录返回 Success=false 且 teamserver 不回包（超时）；
            # 旧版 Linux agent 对空目录直接回 “Failed to enumerate”。两种都退回父目录判断。
            text = str(exc).lower()
            if depth >= 4 or not ("failed to enumerate" in text or exc.status == 504):
                raise
            parent, name = self.split_path(sess, path)
            if not name or parent == path:
                raise
            entries, _actual = await self._list_core(sess, sid, parent, depth + 1)
            item = self._find_entry(sess, entries, name)
            if self._entry_is_dir(item):
                return [], path
            raise

    async def _list_entries(self, sess: dict, sid: str, path: str) -> list[dict]:
        # 调用方必须已持有会话锁。
        entries, _actual = await self._list_core(sess, sid, path)
        return entries

    async def _entry(self, sid: str, raw_path: str) -> tuple[dict, list[dict], Optional[dict]]:
        """返回 (父目录, 父目录条目, 目标条目)；目标不存在时条目为 None。"""
        sess = self.session(sid)
        path = await self._abs_path(sid, sess, self.clean_path(sess, raw_path))
        parent, name = self.split_path(sess, path)
        if not name:
            return parent, [], {"type": "dir", "name": parent}
        entries = await self._list_entries(sess, sid, parent)
        return parent, entries, self._find_entry(sess, entries, name)

    # ------------------------------------------------------------------ 增
    async def mkdir(self, sid: str, raw_path: str) -> dict:
        sess = self.session(sid)
        path = self.clean_path(sess, raw_path)
        self._assert_mutable(sess, path)
        parent, _name = self.split_path(sess, path)
        async with self._lock(sid):
            # 不先列父目录：新建目录通常发生在空目录里，直接下发更快。
            q = self._queue(sid)
            self._drain(q)
            await self._send_fs(sid, "mkdir", path, f"mkdir {path}")
            try:
                text = await self._wait_console(q, self._op_timeout(sess))
            except FileOpError as exc:
                if "exist" in str(exc).lower() or "already" in str(exc).lower():
                    raise FileOpError("目标已存在", 409, exc.detail) from exc
                raise
            if "created directory" not in text.lower():
                raise FileOpError(text or "创建目录失败", 502)
            return {"path": path, "parent": parent, "message": text}

    async def write(self, sid: str, raw_path: str, data: bytes,
                    overwrite: bool = True) -> dict:
        sess = self.session(sid)
        if len(data) > UPLOAD_MAX_BYTES:
            raise FileOpError(f"文件超过 {UPLOAD_MAX_BYTES // (1024 * 1024)} MiB 上限", 413)
        path = self.clean_path(sess, raw_path)
        self._assert_mutable(sess, path)
        parent, _name = self.split_path(sess, path)
        async with self._lock(sid):
            existing: Optional[dict] = None
            if not overwrite:
                _p, _e, existing = await self._entry(sid, path)
                if self._entry_is_dir(existing):
                    raise FileOpError("目标是目录，不能写入文件", 400)
                if existing is not None:
                    raise FileOpError("目标已存在", 409)
            try:
                # 新文件最快路径：直接上传。Windows 对已存在文件返回 ERROR_FILE_EXISTS。
                text = await self._upload_locked(sid, sess, path, data)
                return {"path": path, "parent": parent, "size": len(data),
                        "message": text, "backup": None, "warning": None}
            except FileOpError as exc:
                _p, _e, existing = await self._entry(sid, path)
                if self._entry_is_dir(existing):
                    raise FileOpError("目标是目录，不能写入文件", 400, exc.detail) from exc
                if existing is None:
                    raise
            # 覆盖已有文件：先改名备份，失败回滚，成功后删除备份。
            backup = path + ".havocbak-" + _token()
            await self._move_locked(sid, sess, path, backup, overwrite=False)
            try:
                text = await self._upload_locked(sid, sess, path, data)
            except FileOpError:
                try:
                    await self._move_locked(sid, sess, backup, path, overwrite=False)
                except FileOpError:
                    pass
                raise
            warning = ""
            try:
                await self._remove_locked(sid, sess, backup, recursive=False)
            except FileOpError as exc:
                warning = f"旧文件已备份为 {backup}，删除备份失败：{exc}"
            return {"path": path, "parent": parent, "size": len(data),
                    "message": text, "backup": backup,
                    "warning": warning or None}

    async def _upload_locked(self, sid: str, sess: dict, path: str, data: bytes) -> str:
        q = self._queue(sid)
        self._drain(q)
        args = _b64_text(path) + ";" + base64.b64encode(data).decode("ascii")
        await self._send_fs(sid, "upload", args, f"upload {path}")
        text = await self._wait_console(q, max(90.0, self._op_timeout(sess, 90.0)))
        if "uploaded file" not in text.lower():
            raise FileOpError(text or "上传失败", 502)
        return text

    # ------------------------------------------------------------------ 改
    async def move(self, sid: str, raw_from: str, raw_to: str,
                   overwrite: bool = False) -> dict:
        sess = self.session(sid)
        async with self._lock(sid):
            return await self._move_locked(sid, sess, self.clean_path(sess, raw_from),
                                           self.clean_path(sess, raw_to), overwrite)

    async def _move_locked(self, sid: str, sess: dict, src: str, dst: str,
                           overwrite: bool) -> dict:
        self._assert_mutable(sess, src)
        self._assert_mutable(sess, dst)
        if self._same_name(self.is_linux(sess), src, dst):
            raise FileOpError("源路径与目标路径相同", 400)
        _p1, _e1, src_entry = await self._entry(sid, src)
        if src_entry is None:
            raise FileOpError("源文件或目录不存在", 404)
        _p2, _e2, dst_entry = await self._entry(sid, dst)
        if dst_entry is not None and not overwrite:
            raise FileOpError("目标已存在；如需覆盖请显式选择覆盖", 409)
        q = self._queue(sid)
        self._drain(q)
        args = _b64_text(src) + ";" + _b64_text(dst)
        await self._send_fs(sid, "mv", args, f"mv {src} {dst}")
        text = await self._wait_console(q, self._op_timeout(sess))
        if "successful moved" not in text.lower():
            raise FileOpError(text or "移动/重命名失败", 502)
        return {"from": src, "to": dst, "message": text}

    async def copy(self, sid: str, raw_from: str, raw_to: str,
                   overwrite: bool = False) -> dict:
        sess = self.session(sid)
        async with self._lock(sid):
            src = self.clean_path(sess, raw_from)
            dst = self.clean_path(sess, raw_to)
            self._assert_mutable(sess, src)
            self._assert_mutable(sess, dst)
            if self._same_name(self.is_linux(sess), src, dst):
                raise FileOpError("源路径与目标路径相同", 400)
            _p1, _e1, src_entry = await self._entry(sid, src)
            if src_entry is None:
                raise FileOpError("源文件或目录不存在", 404)
            _p2, _e2, dst_entry = await self._entry(sid, dst)
            if dst_entry is not None and not overwrite:
                raise FileOpError("目标已存在；如需覆盖请显式选择覆盖", 409)
            if self._entry_is_dir(src_entry):
                if self._is_within(sess, dst, src):
                    raise FileOpError("不能把目录复制到其自身内部", 400)
                await self._copy_tree_locked(sid, sess, src, dst)
                return {"from": src, "to": dst, "directory": True}
            q = self._queue(sid)
            self._drain(q)
            args = _b64_text(src) + ";" + _b64_text(dst)
            await self._send_fs(sid, "cp", args, f"cp {src} {dst}")
            text = await self._wait_console(q, self._op_timeout(sess))
            if "successful copied" not in text.lower():
                raise FileOpError(text or "复制失败", 502)
            return {"from": src, "to": dst, "directory": False, "message": text}

    def _is_within(self, sess: dict, child: str, parent: str) -> bool:
        mod = self._pmod(sess)
        try:
            child_n = mod.normcase(mod.normpath(child))
            parent_n = mod.normcase(mod.normpath(parent))
        except (TypeError, ValueError):
            return False
        if child_n == parent_n:
            return True
        sep = "/" if self.is_linux(sess) else "\\"
        return child_n.startswith(parent_n.rstrip(sep) + sep)

    # ------------------------------------------------------------------ 删
    async def remove(self, sid: str, raw_path: str, recursive: bool = False) -> dict:
        sess = self.session(sid)
        path = self.clean_path(sess, raw_path)
        self._assert_mutable(sess, path)
        async with self._lock(sid):
            if recursive:
                _parent, _entries, entry = await self._entry(sid, path)
                if entry is None:
                    raise FileOpError("文件或目录不存在", 404)
                if self._entry_is_dir(entry):
                    await self._remove_tree_locked(sid, sess, path)
                    return {"path": path, "directory": True, "recursive": True}
            return await self._remove_locked(sid, sess, path, recursive=False)

    async def _remove_locked(self, sid: str, sess: dict, path: str,
                             recursive: bool) -> dict:
        q = self._queue(sid)
        self._drain(q)
        await self._send_fs(sid, "remove", path, f"remove {path}")
        text = await self._wait_console(q, self._op_timeout(sess))
        low = text.lower()
        if "removed file" not in low and "removed directory" not in low:
            raise FileOpError(text or "删除失败", 502)
        return {"path": path, "directory": "removed directory" in low,
                "recursive": recursive, "message": text}

    # ------------------------------------------------------------------ 下载 / 读
    async def download(self, sid: str, raw_path: str) -> dict:
        sess = self.session(sid)
        path = self.clean_path(sess, raw_path)
        async with self._lock(sid):
            _parent, _entries, entry = await self._entry(sid, path)
            if entry is None:
                raise FileOpError("文件不存在", 404)
            if self._entry_is_dir(entry):
                raise FileOpError("不能下载目录，请先打包或逐个下载文件", 400)
            q = self._queue(sid)
            self._drain(q)
            await self._send_fs(sid, "download", _b64_text(path), f"download {path}")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(120.0, self._op_timeout(sess, 60.0) * 2)
            base = self.split_path(sess, path)[1]
            while True:
                msg = await self._next(q, deadline)
                mtype = msg.get("type")
                if mtype == "console":
                    line = msg.get("line") or {}
                    kind = str(line.get("kind") or "").lower()
                    text = str(line.get("text") or "").strip()
                    if kind in ("error", "erro"):
                        detail = await self._collect_error(q)
                        raise FileOpError(detail or text or "下载失败", 502)
                    if "未找到落地文件" in text:
                        raise FileOpError(text, 502)
                    continue
                if mtype != "download":
                    continue
                if msg.get("action") != "done":
                    continue
                name = str(msg.get("name") or "").replace("\\", "/").rsplit("/", 1)[-1]
                if base and name and not self._same_name(self.is_linux(sess), name, base):
                    # 串台保护：不是本次请求的文件就继续等
                    continue
                local = msg.get("path") or ""
                if not local or not Path(local).is_file():
                    raise FileOpError("下载完成但本地文件不存在", 502)
                return {"path": path, "local_path": local,
                        "name": Path(local).name, "size": Path(local).stat().st_size}

    async def read_text(self, sid: str, raw_path: str, limit: int = EDIT_MAX_BYTES) -> dict:
        limit = max(1, min(int(limit or EDIT_MAX_BYTES), READ_HARD_MAX))
        result = await self.download(sid, raw_path)
        local = Path(result["local_path"])
        size = local.stat().st_size
        if size > limit:
            return {"path": result["path"], "size": size, "too_large": True,
                    "limit": limit, "binary": False, "content": "", "encoding": ""}
        raw = local.read_bytes()
        binary, text, encoding = self._decode_text(raw)
        return {"path": result["path"], "size": size, "too_large": False,
                "binary": binary, "content": text, "encoding": encoding,
                "limit": limit}

    @staticmethod
    def _decode_text(raw: bytes) -> tuple[bool, str, str]:
        if raw.startswith(codecs.BOM_UTF8):
            return False, raw.decode("utf-8-sig", "replace"), "utf-8-sig"
        if raw.startswith(codecs.BOM_UTF16_LE):
            return False, raw.decode("utf-16-le", "replace").lstrip("\ufeff"), "utf-16-le"
        if raw.startswith(codecs.BOM_UTF16_BE):
            return False, raw.decode("utf-16-be", "replace").lstrip("\ufeff"), "utf-16-be"
        sample = raw[:8192]
        if b"\x00" in sample:
            return True, "", ""
        try:
            return False, raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            try:
                return False, raw.decode("gbk"), "gbk"
            except UnicodeDecodeError:
                return False, raw.decode("utf-8", "replace"), "utf-8"

    def encode_text(self, text: str, encoding: str) -> bytes:
        enc = (encoding or "utf-8").lower()
        if enc in ("utf-8-sig", "utf8-sig"):
            return codecs.BOM_UTF8 + str(text).encode("utf-8")
        if enc in ("utf-16-le", "utf-16le"):
            return codecs.BOM_UTF16_LE + str(text).encode("utf-16-le")
        if enc in ("utf-16-be", "utf-16be"):
            return codecs.BOM_UTF16_BE + str(text).encode("utf-16-be")
        if enc == "gbk":
            try:
                return str(text).encode("gbk")
            except UnicodeEncodeError as exc:
                raise FileOpError(f"内容包含 GBK 无法编码的字符：{exc}", 400) from exc
        return str(text).encode("utf-8")

    # ------------------------------------------------------------------ 目录递归（shell）
    async def _send_shell(self, sid: str, sess: dict, script: str) -> None:
        self._check_connected()
        if self.is_linux(sess):
            program = "/bin/sh"
            args = "-c " + script
        else:
            program = PS_EXE
            payload = "$ProgressPreference='SilentlyContinue';" + script
            args = ("-NoProfile -NonInteractive -EncodedCommand "
                    + base64.b64encode(payload.encode("utf-16-le")).decode("ascii"))
        info = {
            "TaskID": f"{secrets.randbits(32):08X}",
            "CommandLine": "file-manager recursive op",
            "DemonID": sid,
            "AgentType": "Demon",
            "CommandID": "4112",
            "ProcCommand": "4",
            "Args": "0;FALSE;TRUE;" + program + ";" + _b64_text(args),
        }
        try:
            await self.gw.client.send_command(info)
        except Exception as exc:  # noqa: BLE001
            raise FileOpError(f"下发失败：{exc}", 502) from exc

    async def _wait_marker(self, q: asyncio.Queue, timeout: float,
                           marker: str = "HAVOC_FM_DONE") -> str:
        """等待 shell 脚本自己打印的完成标记，避免反复列目录轮询。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        chunks: list[str] = []
        while True:
            msg = await self._next(q, deadline)
            if msg.get("type") != "console":
                continue
            line = msg.get("line") or {}
            text = str(line.get("text") or "")
            if marker in text:
                return "\n".join(chunks)
            kind = str(line.get("kind") or "").lower()
            if kind in ("output", "info", "error", "erro", "warn") and text.strip():
                chunks.append(text.strip())
                if len(chunks) > 40:
                    chunks = chunks[-40:]
                if kind in ("error", "erro") and "win32 error" in text.lower():
                    raise FileOpError("\n".join(chunks), 502)

    async def _remove_tree_locked(self, sid: str, sess: dict, path: str) -> None:
        q = self._queue(sid)
        self._drain(q)
        if self.is_linux(sess):
            quoted = "'" + path.replace("'", "'\\''") + "'"
            script = f"rm -rf -- {quoted}; echo HAVOC_FM_DONE"
            await self._send_shell(sid, sess, script)
            try:
                await self._wait_marker(q, max(30.0, self._op_timeout(sess, 30.0)))
            except FileOpError:
                raise
            _parent, _entries, entry = await self._entry(sid, path)
            if entry is None:
                return
            raise FileOpError("递归删除超时或失败（目录仍存在）", 502)

        quoted = "'" + path.replace("'", "''") + "'"
        script = (f"Remove-Item -LiteralPath {quoted} -Recurse -Force -ErrorAction Stop; "
                  "Write-Output HAVOC_FM_DONE")
        await self._send_shell(sid, sess, script)
        completed = False
        try:
            await self._wait_marker(q, max(40.0, self._op_timeout(sess, 40.0)))
            completed = True
        except FileOpError:
            completed = False
        if completed:
            _parent, _entries, entry = await self._entry(sid, path)
            if entry is None:
                return
        # PowerShell 被策略限制或失败时退回 cmd rd
        if '"' not in path:
            self._drain(q)
            args = f'/c rd /s /q "{path}" & echo HAVOC_FM_DONE'
            info = {
                "TaskID": f"{secrets.randbits(32):08X}",
                "CommandLine": "file-manager rd",
                "DemonID": sid,
                "AgentType": "Demon",
                "CommandID": "4112",
                "ProcCommand": "4",
                "Args": "0;FALSE;TRUE;" + CMD_EXE + ";" + _b64_text(args),
            }
            try:
                await self.gw.client.send_command(info)
            except Exception as exc:  # noqa: BLE001
                raise FileOpError(f"下发失败：{exc}", 502) from exc
            await self._wait_marker(q, max(30.0, self._op_timeout(sess, 30.0)))
            _parent, _entries, entry = await self._entry(sid, path)
            if entry is None:
                return
        raise FileOpError("递归删除超时或失败（目录仍存在）", 502)

    async def _copy_tree_locked(self, sid: str, sess: dict, src: str, dst: str) -> None:
        q = self._queue(sid)
        self._drain(q)
        if self.is_linux(sess):
            qsrc = "'" + src.replace("'", "'\\''") + "'"
            qdst = "'" + dst.replace("'", "'\\''") + "'"
            script = f"cp -a -- {qsrc} {qdst}; echo HAVOC_FM_DONE"
        else:
            qsrc = "'" + src.replace("'", "''") + "'"
            qdst = "'" + dst.replace("'", "''") + "'"
            script = (f"Copy-Item -LiteralPath {qsrc} -Destination {qdst} "
                      "-Recurse -Force -ErrorAction Stop; Write-Output HAVOC_FM_DONE")
        await self._send_shell(sid, sess, script)
        await self._wait_marker(q, max(45.0, self._op_timeout(sess, 45.0)))
        _parent, _entries, entry = await self._entry(sid, dst)
        if entry is None:
            raise FileOpError("目录复制超时或失败（目标未出现）", 502)
