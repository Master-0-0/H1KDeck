#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTP/1 参考客户端（Python）。

用途：
1. 作为传输层自检 / 验收工具，验证 TCP、KCP、UDP、WebSocket、DNS、DoH、
   DoT、OSS 八种监听器的真实收发与加密；
2. 在 Linux 跳板上作为本地中继：``python3 htp_client.py --config x.json
   --listen 127.0.0.1:1080``，把本地 TCP 连接转发到加密传输监听器。

Windows 目标请使用 ``/api/listeners/<name>/relay`` 生成的 Go 中继器。
仅用于已获得明确书面授权的测试环境。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import ssl
import struct
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from transports import (  # noqa: E402
    HANDSHAKE_LEN, MAGIC, MAX_FRAME, PKT_ACK, PKT_DATA, PKT_FIN, PKT_HEADER,
    PKT_HELLO, PKT_HELLO_ACK, ArqSession, Channel, RecordCodec,
    _b32d, _b32e, _b64d, _b64e, derive_master, handshake_mac, session_keys,
)

try:
    import dns.message
    import dns.rdatatype
    import dns.rrset
except Exception:  # pragma: no cover
    dns = None

log = logging.getLogger("htp-client")


def _split_labels(text: str, size: int = 60) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or ["0"]


class HtpClient:
    def __init__(self, cfg: dict):
        self.cfg = dict(cfg)
        self.mode = str(cfg.get("mode", "tcp")).lower()
        self.master = derive_master(str(cfg.get("vkey", "")), str(cfg.get("salt", "")))
        self.sid = secrets.token_bytes(8)
        self.sid_int = struct.unpack(">Q", self.sid)[0]
        self.c2s, self.s2c = session_keys(self.master, self.sid)
        self.enc = RecordCodec(self.c2s, 1, bool(cfg.get("compress", True)), int(cfg.get("padding", 0)))
        self.dec = RecordCodec(self.s2c, 2, bool(cfg.get("compress", True)), int(cfg.get("padding", 0)))
        self.channel: Optional[Channel] = None
        self.arq: Optional[ArqSession] = None
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._http: Optional[aiohttp.ClientSession] = None
        self._writer = None
        self._reader = None
        self._ws = None
        self._transport = None
        self._protocol = None
        self._exchange_lock = asyncio.Lock()
        self._dot_reader = None
        self._dot_writer = None

    # ------------------------------------------------------------------ 连接
    async def connect(self, timeout: float = 10.0) -> None:
        mode = self.mode
        if mode == "tcp":
            await self._connect_tcp(timeout)
        elif mode == "ws":
            await self._connect_ws(timeout)
        elif mode in ("udp", "kcp"):
            await self._connect_udp(timeout)
        elif mode == "dns":
            await self._connect_poll(timeout, self._dns_exchange)
        elif mode == "doh":
            await self._connect_poll(timeout, self._doh_exchange)
        elif mode == "dot":
            await self._connect_poll(timeout, self._dot_exchange)
        elif mode == "oss":
            await self._connect_poll(timeout, self._oss_exchange)
        else:
            raise ValueError(f"unsupported mode {mode}")

    def _hello(self) -> bytes:
        nonce = os.urandom(16)
        return MAGIC + self.sid + nonce + handshake_mac(self.master, self.sid, nonce, b"client")

    def _verify_ack(self, ack: bytes) -> None:
        if len(ack) != HANDSHAKE_LEN or ack[:4] != MAGIC or ack[4:12] != self.sid:
            raise ConnectionError("handshake rejected")
        nonce, mac = ack[12:28], ack[28:60]
        if not secrets.compare_digest(mac, handshake_mac(self.master, self.sid, nonce, b"server")):
            raise ConnectionError("handshake mac mismatch")

    # ---- TCP
    async def _connect_tcp(self, timeout: float) -> None:
        host = self._host()
        port = self._port()
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        self._writer.write(self._hello())
        await self._writer.drain()
        ack = await asyncio.wait_for(self._reader.readexactly(HANDSHAKE_LEN), timeout=timeout)
        self._verify_ack(ack)

        async def send_record(rec: bytes) -> None:
            self._writer.write(struct.pack(">I", len(rec)) + rec)
            await self._writer.drain()

        async def recv_record() -> Optional[bytes]:
            head = await self._reader.readexactly(4)
            size = struct.unpack(">I", head)[0]
            if size <= 0 or size > MAX_FRAME:
                raise ValueError("bad frame")
            return await self._reader.readexactly(size)

        self.channel = Channel(send_record, recv_record, self.enc, self.dec, self._record_size())

    # ---- WebSocket
    async def _connect_ws(self, timeout: float) -> None:
        self._http = aiohttp.ClientSession()
        url = f"ws://{self._host()}:{self._port()}{self.cfg.get('ws_path') or '/ws'}"
        self._ws = await asyncio.wait_for(self._http.ws_connect(url, ssl=False, max_msg_size=MAX_FRAME),
                                          timeout=timeout)
        await self._ws.send_bytes(self._hello())
        msg = await asyncio.wait_for(self._ws.receive(), timeout=timeout)
        if msg.type != aiohttp.WSMsgType.BINARY:
            raise ConnectionError("ws handshake rejected")
        self._verify_ack(msg.data)

        async def send_record(rec: bytes) -> None:
            await self._ws.send_bytes(rec)

        async def recv_record() -> Optional[bytes]:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.BINARY:
                return msg.data
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
                return None
            return b""

        self.channel = Channel(send_record, recv_record, self.enc, self.dec, self._record_size())

    # ---- UDP / KCP
    async def _connect_udp(self, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        incoming: asyncio.Queue[bytes] = asyncio.Queue()

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                incoming.put_nowait(data)

        self._transport, self._protocol = await loop.create_datagram_endpoint(
            Proto, remote_addr=(self._host(), self._port()))

        hello = PKT_HEADER.pack(self.sid_int, PKT_HELLO, 0) + self._hello()[12:]
        deadline = time.time() + timeout
        ack = None
        while time.time() < deadline:
            self._transport.sendto(hello)
            try:
                ack = await asyncio.wait_for(incoming.get(), timeout=0.4)
            except asyncio.TimeoutError:
                continue
            if len(ack) >= PKT_HEADER.size:
                _, typ, _ = PKT_HEADER.unpack(ack[:PKT_HEADER.size])
                if typ == PKT_HELLO_ACK:
                    break
                ack = None
        if ack is None:
            raise ConnectionError("udp handshake timeout")
        body = ack[PKT_HEADER.size:]
        if len(body) < 48:
            raise ConnectionError("bad hello ack")
        nonce, mac = body[:16], body[16:48]
        if not secrets.compare_digest(mac, handshake_mac(self.master, self.sid, nonce, b"server")):
            raise ConnectionError("hello ack mac mismatch")

        self.arq = ArqSession(self.sid_int, window=int(self.cfg.get("window", 32)),
                              rto=float(self.cfg.get("rto", 0.25)))
        self.channel = Channel(self._arq_send, self._arq_recv, self.enc, self.dec, self._record_size())
        self._tasks.append(asyncio.create_task(self._udp_loop(incoming)))

    async def _udp_loop(self, incoming: asyncio.Queue) -> None:
        while not self._closed:
            now = time.time()
            for _ in range(64):
                pkt = self.arq.take_packet(now)
                if pkt is None:
                    break
                self._transport.sendto(pkt)
            try:
                data = await asyncio.wait_for(incoming.get(), timeout=0.01)
            except asyncio.TimeoutError:
                continue
            self.arq.on_packet(data)

    async def _arq_send(self, rec: bytes) -> None:
        self.arq.queue(rec)

    async def _arq_recv(self) -> Optional[bytes]:
        return await self.arq.reader.get()

    # ---- 轮询类（DNS/DoH/DoT/OSS）
    async def _connect_poll(self, timeout: float, exchange) -> None:
        hello = PKT_HEADER.pack(self.sid_int, PKT_HELLO, 0) + self._hello()[12:]
        deadline = time.time() + timeout
        ack = None
        while time.time() < deadline:
            resp = await exchange(hello)
            if resp and len(resp) >= PKT_HEADER.size:
                _, typ, _ = PKT_HEADER.unpack(resp[:PKT_HEADER.size])
                if typ == PKT_HELLO_ACK:
                    ack = resp
                    break
            await asyncio.sleep(0.2)
        if ack is None:
            raise ConnectionError("poll handshake timeout")
        body = ack[PKT_HEADER.size:]
        nonce, mac = body[:16], body[16:48]
        if not secrets.compare_digest(mac, handshake_mac(self.master, self.sid, nonce, b"server")):
            raise ConnectionError("hello ack mac mismatch")
        self.arq = ArqSession(self.sid_int, window=1, rto=float(self.cfg.get("rto", 0.35)))
        self.channel = Channel(self._arq_send, self._arq_recv, self.enc, self.dec, self._record_size())
        self._exchange = exchange
        self._tasks.append(asyncio.create_task(self._poll_loop()))

    async def _poll_loop(self) -> None:
        interval = float(self.cfg.get("poll_interval", 0.02))
        while not self._closed:
            pkt = self.arq.take_packet()
            if pkt is None:
                pkt = PKT_HEADER.pack(self.sid_int, PKT_ACK, self.arq.rx_next - 1)
            try:
                async with self._exchange_lock:
                    resp = await self._exchange(pkt)
            except Exception as exc:
                log.debug("poll exchange failed: %s", exc)
                resp = None
            if resp:
                self.arq.on_packet(resp)
            await asyncio.sleep(interval)

    def _dns_qname(self, packet: bytes) -> str:
        domain = str(self.cfg.get("dns_domain") or "").strip(".")
        labels = _split_labels(_b32e(packet))
        return ".".join(labels + ([domain] if domain else []))

    async def _dns_exchange(self, packet: bytes) -> Optional[bytes]:
        if self._transport is None:
            loop = asyncio.get_running_loop()
            incoming: asyncio.Queue[bytes] = asyncio.Queue()

            class Proto(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    incoming.put_nowait(data)

            self._transport, _ = await loop.create_datagram_endpoint(
                Proto, remote_addr=(self._host(), self._port()))
            self._dns_incoming = incoming
        qname = self._dns_qname(packet)
        query = dns.message.make_query(qname, dns.rdatatype.TXT)
        self._transport.sendto(query.to_wire())
        try:
            wire = await asyncio.wait_for(self._dns_incoming.get(), timeout=2.0)
        except asyncio.TimeoutError:
            return None
        return self._dns_parse(wire)

    @staticmethod
    def _dns_parse(wire: bytes) -> Optional[bytes]:
        try:
            msg = dns.message.from_wire(wire)
            chunks = []
            for rrset in msg.answer:
                if rrset.rdtype != dns.rdatatype.TXT:
                    continue
                for item in rrset:
                    for part in item.strings:
                        chunks.append(part.decode())
            text = "".join(chunks)
            return _b32d(text) if text else b""
        except Exception as exc:
            log.warning("DNS parse failed: %r", exc)
            return None

    def _txt_query(self, packet: bytes) -> bytes:
        """QNAME 短标签 + additional TXT 承载 base32 载荷（DoH/DoT）。"""
        domain = str(self.cfg.get("dns_domain") or "").strip(".")
        qname = (("a." + domain) if domain else "a") + "."
        query = dns.message.make_query(qname, dns.rdatatype.TXT)
        text = _b32e(packet)
        chunks = [text[i:i + 250] for i in range(0, len(text), 250)] or [""]
        query.additional.append(dns.rrset.from_text(
            qname, 0, "IN", "TXT", " ".join('"%s"' % c for c in chunks)))
        return query.to_wire()

    async def _doh_exchange(self, packet: bytes) -> Optional[bytes]:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        url = self.cfg.get("doh_url") or f"http://{self._host()}:{self._port()}/dns-query"
        query = self._txt_query(packet)
        async with self._http.post(url, data=query,
                                   headers={"Content-Type": "application/dns-message"}) as resp:
            body = await resp.read()
            if resp.status != 200:
                return None
            return self._dns_parse(body)

    async def _dot_exchange(self, packet: bytes) -> Optional[bytes]:
        if self._dot_writer is None:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._dot_reader, self._dot_writer = await asyncio.open_connection(
                self._host(), self._port(), ssl=ctx)
        query = self._txt_query(packet)
        self._dot_writer.write(struct.pack(">H", len(query)) + query)
        await self._dot_writer.drain()
        head = await asyncio.wait_for(self._dot_reader.readexactly(2), timeout=3)
        size = struct.unpack(">H", head)[0]
        wire = await self._dot_reader.readexactly(size)
        return self._dns_parse(wire)

    async def _oss_exchange(self, packet: bytes) -> Optional[bytes]:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        base = (self.cfg.get("oss_url") or f"http://{self._host()}:{self._port()}").rstrip("/")
        prefix = str(self.cfg.get("oss_prefix") or "havoc").strip("/")
        sid_hex = self.sid.hex()
        seq = getattr(self, "_oss_seq", 0) + 1
        self._oss_seq = seq
        url_up = f"{base}/{prefix}/{sid_hex}/up/{seq}"
        url_down = f"{base}/{prefix}/{sid_hex}/down/{seq}"
        async with self._http.put(url_up, data=packet,
                                  headers={"Content-Type": "application/octet-stream"}) as resp:
            if resp.status not in (200, 201, 204):
                return None
        async with self._http.get(url_down) as resp:
            if resp.status == 204:
                return b""
            if resp.status != 200:
                return None
            return await resp.read()

    # ------------------------------------------------------------------ 数据
    def _host(self) -> str:
        return str(self.cfg.get("server_host") or self.cfg.get("connect_host")
                   or self.cfg.get("host") or "127.0.0.1")

    def _port(self) -> int:
        return int(self.cfg.get("server_port") or self.cfg.get("connect_port")
                   or self.cfg.get("port") or 0)

    def _record_size(self) -> int:
        mode = self.mode
        return {"tcp": 16384, "ws": 16384, "udp": 900, "kcp": 1200,
                "dns": max(64, 90 - int(self.cfg.get("padding", 0))),
                "doh": 900, "dot": 900, "oss": 1200}.get(mode, 900)

    async def write(self, data: bytes) -> None:
        if self.channel is None:
            raise ConnectionError("not connected")
        await self.channel.write(data)

    async def read(self) -> Optional[bytes]:
        if self.channel is None:
            return None
        return await self.channel.read()

    async def send_keepalive(self) -> None:
        if self.channel is not None:
            await self.channel.send_keepalive()

    async def close(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self.channel is not None:
            self.channel.closed = True
        try:
            if self._ws is not None:
                await self._ws.close()
            if self._writer is not None:
                self._writer.close()
                try:
                    await self._writer.wait_closed()
                except Exception:
                    pass
            if self._dot_writer is not None:
                self._dot_writer.close()
            if self._transport is not None:
                self._transport.close()
            if self._http is not None:
                await self._http.close()
        except Exception:
            pass


# ---------------------------------------------------------------------- 本地中继

async def run_proxy(cfg: dict, listen_host: str, listen_port: int) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = HtpClient(cfg)
        peer = "%s:%s" % writer.get_extra_info("peername")[:2]
        try:
            await client.connect()
        except Exception as exc:
            log.warning("transport connect failed for %s: %s", peer, exc)
            writer.close()
            return
        log.info("relay %s established", peer)
        await _pipe(client, reader, writer)
        await client.close()

    server = await asyncio.start_server(handle, listen_host, listen_port)
    log.info("HTP relay listening on %s:%s", listen_host, listen_port)
    async with server:
        await server.serve_forever()


async def _pipe(client: HtpClient, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    async def local_to_remote() -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            await client.write(data)

    async def remote_to_local() -> None:
        while True:
            data = await client.read()
            if data is None:
                break
            writer.write(data)
            await writer.drain()

    try:
        await asyncio.gather(local_to_remote(), remote_to_local())
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="HTP/1 reference client / local relay")
    parser.add_argument("--config", required=True, help="JSON config file")
    parser.add_argument("--listen", default="", help="local listen addr, e.g. 127.0.0.1:1080")
    parser.add_argument("--probe", action="store_true", help="send HELLO and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname).1s %(name)s: %(message)s")
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))

    if args.listen:
        host, _, port = args.listen.rpartition(":")
        return asyncio.run(run_proxy(cfg, host or "127.0.0.1", int(port)))

    async def probe() -> int:
        client = HtpClient(cfg)
        await client.connect()
        await client.write(b"PING")
        data = await asyncio.wait_for(client.read(), timeout=10)
        print("PROBE", "OK" if data else "EMPTY", data)
        await client.close()
        return 0

    return asyncio.run(probe())


if __name__ == "__main__":
    raise SystemExit(main())
