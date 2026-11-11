#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc 服务端插件（Service）最小客户端 —— 仅用于已授权测试环境的联调/验收。

它连接 teamserver 的 /service-endpoint，注册一个自定义监听器协议
``Svc-Demo``（字段 Host / PortBind / Token），并在 teamserver 下发
``ListenerStart`` 时真正绑定 TCP 端口、回执 ``ListenerStart``。

用法：
    python3 webui/tools/service_listener_demo.py                 # 默认 wss://127.0.0.1:40056/service-endpoint
    python3 webui/tools/service_listener_demo.py --name Svc-Demo --port 0

注意：
- 协议与字段结构来自 teamserver/pkg/service（HeadListener / BodyListenerAdd）。
- 删除监听器时 teamserver 不会通知插件（ListenerRemove 只处理 HTTP/External），
  本客户端会继续占用端口，直到进程退出；这是 teamserver 的既有行为。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ssl
import sys
from typing import Optional

import websockets

log = logging.getLogger("service-demo")

DEFAULT_ITEMS = [
    {
        "name": "Host",
        "text": "监听地址",
        "placeholder": "0.0.0.0",
        "object": "input",
        "required": True,
        "editable": True,
    },
    {
        "name": "PortBind",
        "text": "监听端口",
        "placeholder": "9100",
        "object": "input",
        "required": True,
        "editable": True,
    },
    {
        "name": "Token",
        "text": "接入令牌",
        "placeholder": "plugin-token",
        "object": "input",
        "required": False,
        "editable": False,
    },
]


class DemoService:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.servers: dict[str, asyncio.AbstractServer] = {}
        self.registered = False

    # ---------------------------------------------------------------- 网络

    def _ssl(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def run_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._session()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("service session ended: %s", exc)
            finally:
                await self._close_servers()
                self.registered = False
            log.info("reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)
            delay = min(15.0, delay * 1.8)

    async def _session(self) -> None:
        log.info("connecting %s", self.args.url)
        async with websockets.connect(
            self.args.url,
            ssl=self._ssl() if self.args.url.startswith("wss://") else None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
            max_size=4 * 1024 * 1024,
        ) as ws:
            await ws.send(json.dumps({
                "Head": {"Type": "Register"},
                "Body": {"Password": self.args.password},
            }))
            auth = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if not (auth.get("Head", {}).get("Type") == "Register"
                    and auth.get("Body", {}).get("Success")):
                raise RuntimeError(f"authentication failed: {auth}")
            log.info("authenticated as service client")

            await self._register(ws)
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    log.warning("non-JSON message ignored")
                    continue
                head = msg.get("Head", {})
                body = msg.get("Body", {})
                if head.get("Type") == "Listener" and body.get("Type") == "ListenerStart":
                    await self._handle_start(ws, body.get("Listener") or {})
                else:
                    log.debug("message ignored: %s", raw[:200])

    async def _register(self, ws) -> None:
        payload = {
            "Head": {"Type": "Listener"},
            "Body": {
                "Type": "ListenerAdd",
                "Listener": {
                    "Name": self.args.name,
                    "Agent": self.args.agent,
                    "Items": DEFAULT_ITEMS,
                },
            },
        }
        await ws.send(json.dumps(payload))
        self.registered = True
        log.info("registered listener protocol %s (agent=%s)", self.args.name, self.args.agent)

    # ------------------------------------------------------------ 监听器

    async def _handle_start(self, ws, info: dict) -> None:
        name = str(info.get("Name", "")).strip()
        host = str(info.get("HostBind") or info.get("Host") or "0.0.0.0").strip() or "0.0.0.0"
        try:
            port = int(info.get("PortBind") or info.get("Port") or 0)
        except (TypeError, ValueError):
            port = 0
        error = ""
        status = "Online"

        if not name:
            name = self.args.name
            error = "listener name missing"
            status = "Offline"
        else:
            await self._close_server(name)
            if port < 1 or port > 65535:
                error = "invalid PortBind"
                status = "Offline"
            else:
                try:
                    server = await asyncio.start_server(
                        lambda r, w: self._echo(r, w), host, port)
                    self.servers[name] = server
                    sockets = server.sockets or []
                    if sockets:
                        port = sockets[0].getsockname()[1]
                    log.info("listener %s bound on %s:%s", name, host, port)
                except Exception as exc:
                    error = str(exc)
                    status = "Offline"
                    log.warning("bind %s:%s failed: %s", host, port, exc)

        reply_info = dict(info)
        reply_info["Host"] = host
        reply_info["PortBind"] = str(port)
        reply = {
            "Head": {"Type": "Listener"},
            "Body": {
                "Type": "ListenerStart",
                "Listener": {
                    "Name": name,
                    "Protocol": str(info.get("Protocol") or self.args.name),
                    "Host": host,
                    "PortBind": str(port),
                    "PortConn": str(info.get("PortConn") or port),
                    "Error": error,
                    "Status": status,
                    "Info": json.dumps(reply_info, ensure_ascii=False),
                },
            },
        }
        await ws.send(json.dumps(reply))
        log.info("replied ListenerStart name=%s status=%s", name, status)

    @staticmethod
    async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _close_server(self, name: str) -> None:
        server = self.servers.pop(name, None)
        if server is None:
            return
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass

    async def _close_servers(self) -> None:
        for name in list(self.servers):
            await self._close_server(name)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Minimal Havoc service listener plugin")
    ap.add_argument("--url", default="wss://127.0.0.1:40056/service-endpoint")
    ap.add_argument("--password", default="service-password")
    ap.add_argument("--name", default="Svc-Demo")
    ap.add_argument("--agent", default="Demon")
    ap.add_argument("--debug", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = DemoService(args)
    try:
        asyncio.run(service.run_forever())
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
