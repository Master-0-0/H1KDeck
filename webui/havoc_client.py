#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc teamserver WebSocket 客户端（网关侧）。

- 以配置的操作员身份登录 wss://host:port/havoc/
- 断线自动重连（指数退避）
- 收包交给 AppState 解析
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
from typing import Optional

import websockets

from state import AppState, now_ts


class AuthError(Exception):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class HavocClient:
    def __init__(self, state: AppState, host: str, port: int, user: str, password: str,
                 path: str = "/havoc/"):
        self.state = state
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.path = path
        self.url = f"wss://{host}:{port}{path}"
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        # 手动断开：paused 置位后 _run 不再发起重连；resume 清位并立即重连。
        self._paused = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()
        self._reconnect = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.last_recv = 0.0
        self.messages = 0

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="havoc-connection")

    async def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def connected(self) -> bool:
        return self.ws is not None and self._connected.is_set() and not self._paused.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    async def pause(self) -> None:
        """手动断开 teamserver：关闭当前 ws 并暂停自动重连（不设置 _stop）。"""
        self._paused.set()
        self._resume.clear()
        self._connected.clear()
        self._set_state("disconnected", "已手动断开 teamserver 连接")
        ws = self.ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        self.state.add_event("操作员手动断开 teamserver 连接（自动重连已暂停）", "warn", source="gateway")

    async def resume(self) -> None:
        """恢复 teamserver 连接：清除暂停并立即重连。"""
        was_paused = self._paused.is_set()
        self._paused.clear()
        self._resume.set()
        await self.force_reconnect()
        if was_paused:
            self.state.add_event("操作员恢复 teamserver 连接", "info", source="gateway")

    async def force_reconnect(self) -> None:
        """主动断开当前连接，让重连循环重新登录（菜单「重新连接 teamserver」）。"""
        ws = self.ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        # 唤醒退避等待，避免菜单点了还要等满 20 秒
        self._reconnect.set()

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state.connection["state"] = state
        self.state.connection["detail"] = detail
        self.state.connection["user"] = self.user
        self.state.connection["teamserver"] = self.url
        self.state.connection["manual"] = self._paused.is_set()
        self.state.publish({"type": "connection", "connection": self.state.connection_view()})

    # ---------------- 主循环 ----------------

    async def _run(self) -> None:
        backoff = 1.0
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        first = True

        while not self._stop.is_set():
            if self._paused.is_set():
                self._set_state("disconnected", "已手动断开 teamserver 连接")
                await self._wait_resume()
                if self._stop.is_set():
                    break
                backoff = 1.0
                first = False
                self._reconnect.clear()
                continue
            self._set_state("connecting", "正在连接 teamserver…")
            try:
                async with websockets.connect(
                    self.url,
                    ssl=ctx,
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    open_timeout=10,
                ) as ws:
                    self.ws = ws
                    self.last_recv = now_ts()
                    await self._login(ws)
                    backoff = 1.0
                    self.state.connection["reconnects"] = 0 if first else self.state.connection.get("reconnects", 0)
                    first = False
                    self._connected.set()
                    self.state.add_event("网关已连接 teamserver 并完成登录", "good", source="gateway")
                    await self._read_loop(ws)
            except AuthError as exc:
                self._connected.clear()
                if exc.transient:
                    # “User already exits”：上一次断开的连接还没被 teamserver 回收，
                    # 属于正常的重启窗口，几秒后重试即可。
                    self._set_state("connecting", f"等待 teamserver 释放旧连接：{exc}")
                    self.state.add_event(f"teamserver 暂未释放旧会话：{exc}", "warn", source="gateway")
                    backoff = 4.0
                else:
                    self._set_state("auth_failed", str(exc))
                    self.state.add_event(f"teamserver 登录失败：{exc}", "error", source="gateway")
                    backoff = 30.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected.clear()
                if self._paused.is_set():
                    # 手动断开导致的 ws 关闭：不当作网络故障记录
                    pass
                else:
                    detail = self._describe_error(exc)
                    self._set_state("disconnected", detail)
                    self.state.add_event(f"与 teamserver 的连接断开：{detail}", "error", source="gateway")
            finally:
                self._connected.clear()
                self.ws = None

            if self._stop.is_set():
                break

            if self._paused.is_set():
                # 回到循环顶部进入暂停等待，不增加重连计数
                continue

            if self.state.connection["state"] != "auth_failed":
                self.state.connection["reconnects"] = self.state.connection.get("reconnects", 0) + 1
            delay = backoff
            self._set_state("disconnected", f"{delay:.0f} 秒后重连…")
            self._reconnect.clear()
            waits = [asyncio.create_task(self._stop.wait()),
                     asyncio.create_task(self._paused.wait()),
                     asyncio.create_task(self._reconnect.wait())]
            try:
                await asyncio.wait(waits, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waits:
                    task.cancel()
            if self._stop.is_set():
                break
            if self._paused.is_set():
                continue
            if self._reconnect.is_set():
                self._reconnect.clear()
                backoff = 1.0
            else:
                backoff = min(backoff * 2, 20.0)
            first = False

        self._set_state("stopped", "网关已停止")

    async def _wait_resume(self) -> None:
        """暂停状态下等待 resume 或 stop；唤醒后由主循环继续。"""
        waits = [asyncio.create_task(self._stop.wait()),
                 asyncio.create_task(self._resume.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        name = type(exc).__name__
        text = str(exc) or name
        if isinstance(exc, (ConnectionRefusedError, OSError)) and "refused" in text.lower():
            return "teamserver 未运行或端口不可达"
        if isinstance(exc, websockets.InvalidStatus):
            return f"WebSocket 握手被拒绝（{exc}）"
        if isinstance(exc, asyncio.TimeoutError):
            return "连接/登录超时"
        return f"{name}: {text}"

    async def _login(self, ws) -> None:
        password_hash = hashlib.sha3_256(self.password.encode()).hexdigest()
        package = {
            "Head": {"Event": 1, "User": self.user, "Time": ""},
            "Body": {"SubEvent": 3, "Info": {"User": self.user, "Password": password_hash}},
        }
        await ws.send(json.dumps(package, separators=(",", ":")).encode())
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 15
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AuthError("登录无响应（超时）")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            try:
                pkg = json.loads(raw)
            except Exception:
                continue
            head = pkg.get("Head") or {}
            body = pkg.get("Body") or {}
            if head.get("Event") == 1 and body.get("SubEvent") in (1, 2):
                info = body.get("Info") or {}
                if body.get("SubEvent") == 2:
                    message = info.get("Message") or "凭据被拒绝"
                    raise AuthError(message, transient="already" in message.lower())
                self.messages += 1
                # 新连接会重放历史会话；操作员的本地「移除」只在同一连接内有效。
                self.state.hidden_sessions.clear()
                self.state.handle_package(pkg)
                return

    async def _read_loop(self, ws) -> None:
        loop = asyncio.get_event_loop()
        while not self._stop.is_set() and not self._paused.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
            except asyncio.TimeoutError:
                # 网关侧静默超过 90 秒（teamserver 正常时每 2 秒都有回调）→ 主动重连
                if now_ts() - self.last_recv > 90:
                    raise ConnectionError("teamserver 90 秒无数据，主动重连")
                continue
            self.last_recv = now_ts()
            self.messages += 1
            try:
                pkg = json.loads(raw)
            except Exception:
                continue
            self.state.handle_package(pkg)

    # ---------------- 发送 ----------------

    async def send_package(self, event: int, sub_event: int, info: dict,
                           one_time: bool = False) -> None:
        if not self.connected or self.ws is None:
            raise ConnectionError("网关当前未连接 teamserver")
        package = {
            "Head": {
                "Event": event,
                "User": self.user,
                "Time": time.strftime("%d/%m/%Y %H:%M:%S"),
                "OneTime": "true" if one_time else "",
            },
            "Body": {"SubEvent": sub_event, "Info": info},
        }
        data = json.dumps(package, separators=(",", ":"), ensure_ascii=False).encode()
        async with self._send_lock:
            await self.ws.send(data)

    async def send_command(self, info: dict) -> None:
        await self.send_package(7, 3, info)
