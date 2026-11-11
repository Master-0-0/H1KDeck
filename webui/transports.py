#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc Web 控制台 —— 加密传输监听器（VShell 风格）。

本模块实现 Web 网关自有的传输层：

    TCP / KCP(可靠 UDP) / UDP / WebSocket / DNS / DoH / DoT / OSS

设计目标
--------
1. 真实监听套接字（TCP/UDP/TLS/WebSocket/DNS），不是前端假数据；
2. 统一加密：vkey 握手（HMAC-SHA256）+ AES-256-GCM 逐记录加密，
   EncryptSalt 派生会话密钥，可选 zlib 压缩与随机填充（流量加密）；
3. 每个传输会话解密后作为一条 TCP 隧道转发到 ``upstream``（默认
   ``127.0.0.1:8080``，即 Havoc 的 HTTP 监听器），因此未改动的 Demon
   载荷只要把 HTTP 代理指向中继器的本地端口，就能把 C2 流量跑在这些
   传输之上（见 docs/TRANSPORTS.md）；
4. 配套的 Go 中继器（relay）由 :mod:`relay_builder` 按监听器配置交叉编译，
   支持 windows/linux 的 amd64/386/arm64。

协议（HTP/1）
-------------
明文握手（60 字节）::

    "HTP1" | sid(8) | nonce(16) | HMAC-SHA256(master, "hello"|sid|nonce|role)(32)

记录（加密后）::

    nonce(12) | AES-256-GCM(flags(1)|len(4)|data|padding)

流模式（tcp/ws）在记录前加 4 字节大端长度；数据报模式（kcp/udp）
使用带序号/ACK 的可靠层；DNS/DoH/DoT/OSS 把同样的可靠层包在
请求/响应消息里。

仅用于已获得明确书面授权的测试环境。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import socket
import ssl
import struct
import time
import zlib
from datetime import datetime, timedelta
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from aiohttp import web

try:  # 可选：DoT 证书
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.x509.oid import NameOID
    _CRYPTO_OK = True
except Exception:  # pragma: no cover - 仅在缺依赖时触发
    _CRYPTO_OK = False

try:
    import dns.exception
    import dns.flags
    import dns.message
    import dns.rdataclass
    import dns.rdatatype
    import dns.rrset
    _DNS_OK = True
except Exception:  # pragma: no cover
    _DNS_OK = False

log = logging.getLogger("havoc-webui.transport")

MAGIC = b"HTP1"
HANDSHAKE_LEN = 4 + 8 + 16 + 32
MASTER_INFO = b"havoc-transport-v1"
MAX_FRAME = 2 * 1024 * 1024

PKT_HELLO = 1
PKT_HELLO_ACK = 2
PKT_DATA = 3
PKT_ACK = 4
PKT_FIN = 5
PKT_HEADER = struct.Struct(">QBI")  # sid, type, seq

# 传输模式元数据（供 UI 渲染与校验）
TRANSPORT_MODES: dict[str, dict[str, Any]] = {
    "tcp": {
        "label": "TCP", "kind": "stream", "default_port": 9001,
        "fields": ["listen", "connect", "crypto"],
        "desc": "原始 TCP 隧道，记录级 AES-256-GCM 加密",
    },
    "kcp": {
        "label": "KCP", "kind": "reliable-udp", "default_port": 9002,
        "fields": ["listen", "connect", "crypto"],
        "desc": "KCP 风格可靠 UDP（ARQ：序号/ACK/重传/窗口）",
    },
    "udp": {
        "label": "UDP", "kind": "reliable-udp", "default_port": 9003,
        "fields": ["listen", "connect", "crypto"],
        "desc": "UDP 上的可靠有序字节流（ARQ）",
    },
    "ws": {
        "label": "WebSocket", "kind": "ws", "default_port": 8084,
        "fields": ["listen", "connect", "ws", "crypto"],
        "desc": "WebSocket 二进制消息承载加密记录",
    },
    "dns": {
        "label": "DNS", "kind": "dns", "default_port": 5353,
        "fields": ["listen", "connect", "dns", "crypto"],
        "desc": "DNS 隧道：QNAME 上行 / TXT 下行，Base32 编码",
    },
    "doh": {
        "label": "DoH", "kind": "doh", "default_port": 5443,
        "fields": ["listen", "connect", "dns", "crypto"],
        "desc": "DNS over HTTPS（application/dns-message）",
    },
    "dot": {
        "label": "DoT", "kind": "dot", "default_port": 853,
        "fields": ["listen", "connect", "dns", "crypto"],
        "desc": "DNS over TLS（853，自签证书）",
    },
    "oss": {
        "label": "OSS", "kind": "oss", "default_port": 9000,
        "fields": ["listen", "connect", "oss", "crypto"],
        "desc": "对象存储隧道：PUT 上行 / GET 下行（OSS/S3 兼容端点）",
    },
}

MODE_ALIASES = {
    "http": "http", "https": "https", "https ": "https",
    "tcp": "tcp", "kcp": "kcp", "udp": "udp", "ws": "ws",
    "websocket": "ws", "dns": "dns", "doh": "doh", "dot": "dot",
    "oss": "oss", "objectstorage": "oss",
}


def normalize_mode(value: str) -> str:
    return MODE_ALIASES.get(str(value or "").strip().lower(), "")


# ------------------------------------------------------------------ 基础工具

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _b32e(raw: bytes) -> str:
    return base64.b32encode(raw).decode().rstrip("=")


def _b32d(text: str) -> bytes:
    text = text.strip().upper()
    pad = "=" * ((8 - len(text) % 8) % 8)
    return base64.b32decode(text + pad)


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive_master(vkey: str, salt: str) -> bytes:
    return hkdf_sha256(hashlib.sha256(vkey.encode()).digest(),
                       hashlib.sha256(salt.encode()).digest(), MASTER_INFO, 32)


def handshake_mac(master: bytes, sid: bytes, nonce: bytes, role: bytes) -> bytes:
    return hmac.new(master, b"hello" + sid + nonce + role, hashlib.sha256).digest()


def session_keys(master: bytes, sid: bytes) -> tuple[bytes, bytes]:
    return hkdf_sha256(master, sid, b"c2s", 32), hkdf_sha256(master, sid, b"s2c", 32)


class RecordCodec:
    """单方向记录编解码：压缩 / 填充 / AES-256-GCM。"""

    def __init__(self, key: bytes, direction: int, compress: bool = True, padding: int = 0):
        self._gcm = AESGCM(key)
        self.direction = direction  # 1=client->server, 2=server->client
        self.compress = compress
        self.padding = max(0, min(255, int(padding)))
        self.counter = 0

    def _nonce(self) -> bytes:
        self.counter += 1
        return bytes([self.direction]) + self.counter.to_bytes(8, "big") + b"\x00\x00\x00"

    def encode(self, data: bytes) -> bytes:
        flags = 0
        if self.compress and len(data) > 64:
            packed = zlib.compress(data, 6)
            if len(packed) < len(data):
                data, flags = packed, flags | 1
        pad = random.randint(0, self.padding) if self.padding else 0
        plain = struct.pack(">BI", flags, len(data)) + data + os.urandom(pad)
        nonce = self._nonce()
        return nonce + self._gcm.encrypt(nonce, plain, b"HTP1r")

    def decode(self, frame: bytes) -> bytes:
        if len(frame) < 12 + 16 + 5:
            raise ValueError("frame too short")
        nonce, cipher = frame[:12], frame[12:]
        plain = self._gcm.decrypt(nonce, cipher, b"HTP1r")
        flags, length = struct.unpack(">BI", plain[:5])
        if 5 + length > len(plain):
            raise ValueError("bad record length")
        data = plain[5:5 + length]
        if flags & 1:
            data = zlib.decompress(data)
        return data


# ------------------------------------------------------------------ ARQ（可靠 UDP / 轮询）

class ArqSession:
    """在不可靠数据报上提供有序可靠字节流（序号 + 累积 ACK + 重传 + 窗口）。"""

    def __init__(self, sid: int, window: int = 32, rto: float = 0.3, on_send: Optional[Callable[[bytes], None]] = None):
        self.sid = sid
        self.window = max(1, int(window))
        self.rto = max(0.05, float(rto))
        self.on_send = on_send
        self.txq: deque[bytes] = deque()
        self.unacked: "OrderedDict[int, list]" = OrderedDict()
        self.outbox: deque[bytes] = deque()
        self.next_seq = 1
        self.rx_next = 1
        self.rx_buf: dict[int, bytes] = {}
        self.ack = 0
        self.need_ack = False
        self.reader: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self.closed = False
        self.last_rx = time.time()
        self.last_tx = time.time()
        self._fin_sent = False

    # ---- 发送侧
    def queue(self, record: bytes) -> None:
        if self.closed:
            return
        self.txq.append(record)
        self._fill()

    def _fill(self) -> None:
        while not self.closed and len(self.unacked) < self.window and self.txq:
            seq = self.next_seq
            self.next_seq += 1
            body = self.txq.popleft()
            pkt = PKT_HEADER.pack(self.sid, PKT_DATA, seq) + body
            self.unacked[seq] = [pkt, time.time(), 0]
            self.outbox.append(pkt)

    def poll(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        if self.need_ack:
            self.outbox.append(PKT_HEADER.pack(self.sid, PKT_ACK, self.ack))
            self.need_ack = False
        for seq, item in list(self.unacked.items()):
            pkt, ts, tries = item
            if now - ts >= self.rto * (2 ** min(tries, 3)):
                item[1] = now
                item[2] = tries + 1
                self.outbox.append(pkt)

    def take_packet(self, now: Optional[float] = None) -> Optional[bytes]:
        self.poll(now)
        if self.outbox:
            return self.outbox.popleft()
        return None

    def ack_packet(self, ack: int) -> bytes:
        return PKT_HEADER.pack(self.sid, PKT_ACK, ack)

    # ---- 接收侧
    def on_packet(self, pkt: bytes) -> None:
        if len(pkt) < PKT_HEADER.size:
            return
        sid, typ, seq = PKT_HEADER.unpack(pkt[:PKT_HEADER.size])
        if sid != self.sid:
            return
        self.last_rx = time.time()
        body = pkt[PKT_HEADER.size:]
        if typ == PKT_DATA:
            if seq == self.rx_next:
                self._deliver(body)
                self.rx_next += 1
                while self.rx_next in self.rx_buf:
                    self._deliver(self.rx_buf.pop(self.rx_next))
                    self.rx_next += 1
            elif seq > self.rx_next:
                if len(self.rx_buf) < 4096:
                    self.rx_buf.setdefault(seq, body)
            self.ack = self.rx_next - 1
            self.need_ack = True
        elif typ == PKT_ACK:
            ack = seq
            for key in [k for k in self.unacked if k <= ack]:
                self.unacked.pop(key, None)
            self._fill()
        elif typ == PKT_FIN:
            self.close()

    def _deliver(self, body: bytes) -> None:
        if not self.closed:
            self.reader.put_nowait(body)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.reader.put_nowait(None)

    def fin(self) -> None:
        if not self._fin_sent:
            self._fin_sent = True
            self.outbox.append(PKT_HEADER.pack(self.sid, PKT_FIN, 0))


# ------------------------------------------------------------------ 通道

class Channel:
    """把一条明文字节流拆成加密记录，通过底层 transport 收发。"""

    def __init__(self, send_record: Callable[[bytes], Awaitable[None]],
                 recv_record: Callable[[], Awaitable[Optional[bytes]]],
                 enc: RecordCodec, dec: RecordCodec, max_data: int):
        self._send_record = send_record
        self._recv_record = recv_record
        self.enc = enc
        self.dec = dec
        self.max_data = max(64, int(max_data))
        self.bytes_in = 0
        self.bytes_out = 0
        self.closed = False

    async def write(self, data: bytes) -> None:
        if self.closed:
            return
        for off in range(0, len(data), self.max_data):
            chunk = data[off:off + self.max_data]
            try:
                await self._send_record(self.enc.encode(chunk))
            except Exception:
                self.closed = True
                raise
            self.bytes_in += len(chunk)

    async def read(self) -> Optional[bytes]:
        while not self.closed:
            try:
                frame = await self._recv_record()
            except Exception:
                self.closed = True
                return None
            if frame is None:
                self.closed = True
                return None
            if not frame:
                continue
            try:
                data = self.dec.decode(frame)
            except Exception as exc:
                log.debug("record decode failed (%d bytes): %r", len(frame), exc)
                continue
            if not data:
                continue  # 保活
            self.bytes_out += len(data)
            return data
        return None

    async def send_keepalive(self) -> None:
        try:
            await self._send_record(self.enc.encode(b""))
        except Exception:
            pass



class HttpRequestRewriter:
    """把 HTTP 代理的 absolute-form 请求行改写为 origin-form。

    Demon 通过 WinHTTP 代理回连时会发送::

        POST http://192.168.121.132:18080/api/v1/status HTTP/1.1

    Havoc 的 HTTP 监听器只接受 origin-form（``/api/v1/status``）。网关在
    解密后的客户端方向做一次无损改写，响应方向保持原样。
    """

    def __init__(self):
        self.state = "line"
        self.buf = b""
        self.remaining = 0

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        self.buf += data
        while True:
            if self.state == "body":
                take = min(self.remaining, len(self.buf))
                out += self.buf[:take]
                self.buf = self.buf[take:]
                self.remaining -= take
                if self.remaining == 0:
                    self.state = "line"
                if not self.buf:
                    break
                continue
            if self.state == "line":
                idx = self.buf.find(b"\r\n")
                if idx < 0:
                    break
                line, self.buf = self.buf[:idx], self.buf[idx + 2:]
                out += self._rewrite(line) + b"\r\n"
                self.state = "headers"
                continue
            idx = self.buf.find(b"\r\n\r\n")
            if idx < 0:
                break
            block, self.buf = self.buf[:idx + 4], self.buf[idx + 4:]
            out += block
            import re as _re
            m = _re.search(rb"(?im)^content-length:\s*(\d+)\s*$", block)
            self.remaining = int(m.group(1)) if m else 0
            self.state = "body" if self.remaining else "line"
        return bytes(out)

    @staticmethod
    def _rewrite(line: bytes) -> bytes:
        import re as _re
        m = _re.match(rb"^([A-Z]{3,10}) (https?)://[^/\s]+(/[^\s]*)? (HTTP/1\.[01])$", line)
        if not m:
            return line
        path = m.group(3) or b"/"
        return m.group(1) + b" " + path + b" " + m.group(4)

async def run_tunnel(server: "BaseServer", peer: str, channel: Channel,
                     session_id: int) -> None:
    """把解密后的字节流转发到 upstream（Havoc HTTP 监听器）。"""
    cfg = server.cfg
    up_host = cfg.get("upstream_host") or "127.0.0.1"
    up_port = int(cfg.get("upstream_port") or 8080)
    writer = None
    try:
        up_reader, writer = await asyncio.wait_for(
            asyncio.open_connection(up_host, up_port), timeout=8)
    except Exception as exc:
        server.note_error(f"上游 {up_host}:{up_port} 连接失败：{exc}")
        channel.closed = True
        return

    server.stats["connections"] += 1
    server.stats["active"] += 1
    log.info("[%s] 传输会话 %08x 接入 %s -> %s:%s", cfg.get("name"), session_id,
             peer, up_host, up_port)
    rewriter = HttpRequestRewriter() if cfg.get("http_rewrite", True) else None

    async def c2u() -> None:
        while True:
            data = await channel.read()
            if data is None:
                break
            if rewriter is not None:
                data = rewriter.feed(data)
                if not data:
                    continue
            writer.write(data)
            await writer.drain()
            server.stats["bytes_in"] += len(data)

    async def u2c() -> None:
        while True:
            data = await up_reader.read(65536)
            if not data:
                break
            await channel.write(data)
            server.stats["bytes_out"] += len(data)

    c2u_task = asyncio.create_task(c2u())
    u2c_task = asyncio.create_task(u2c())
    try:
        done, pending = await asyncio.wait({c2u_task, u2c_task},
                                           return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    except (ConnectionResetError, BrokenPipeError):
        pass
    except Exception as exc:
        server.note_error(str(exc))
    finally:
        channel.closed = True
        try:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
        except Exception:
            pass
        server.stats["active"] = max(0, server.stats["active"] - 1)
        log.info("[%s] 传输会话 %08x 结束", cfg.get("name"), session_id)


# ------------------------------------------------------------------ 服务基类

class BaseServer:
    mode = ""
    max_record = 16 * 1024

    def __init__(self, cfg: dict, manager: "TransportManager"):
        self.cfg = dict(cfg)
        self.manager = manager
        self.name = self.cfg.get("name", "")
        self.master = derive_master(self.cfg.get("vkey", ""), self.cfg.get("salt", ""))
        self.stats: dict[str, Any] = {
            "active": 0, "connections": 0, "bytes_in": 0, "bytes_out": 0,
            "started_at": 0, "last_error": "",
        }
        self._runner: Optional[web.AppRunner] = None
        self._site = None
        self._server = None
        self._tasks: set[asyncio.Task] = set()
        self._sessions: dict[int, Any] = {}
        self._seen_sids: "OrderedDict[bytes, float]" = OrderedDict()
        self._hello_cache: "OrderedDict[bytes, bytes]" = OrderedDict()

    # ---- 生命周期
    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        for sess in list(self._sessions.values()):
            try:
                sess.close()
            except Exception:
                pass
        self._sessions.clear()
        if self._runner is not None:
            try:
                await asyncio.wait_for(self._runner.cleanup(), timeout=5)
            except Exception:
                pass
            self._runner = None
        if self._server is not None:
            try:
                self._server.close()
                await asyncio.wait_for(self._server.wait_closed(), timeout=3)
            except Exception:
                pass
            self._server = None

    async def _aiohttp_app(self, app: web.Application) -> None:
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.cfg.get("listen_host") or "0.0.0.0",
                                 int(self.cfg.get("listen_port")))
        await self._site.start()

    # ---- 工具
    def note_error(self, message: str) -> None:
        self.stats["last_error"] = message[:400]
        log.warning("[%s] %s", self.name, message)
        try:
            self.manager.on_error(self.name, message)
        except Exception:
            pass

    def _remember_sid(self, sid: bytes) -> bool:
        now = time.time()
        if sid in self._seen_sids:
            return False
        self._seen_sids[sid] = now
        while len(self._seen_sids) > 4096:
            self._seen_sids.popitem(last=False)
        return True

    def _hello_reply(self, sid: bytes, body: bytes, sid_int: int) -> Optional[bytes]:
        """校验 HELLO 并返回 HELLO_ACK；重复 HELLO 返回缓存的 ACK（丢包重试）。"""
        cached = self._hello_cache.get(sid)
        if cached is not None:
            return cached
        if len(body) < 48:
            return None
        nonce, mac = body[:16], body[16:48]
        if not hmac.compare_digest(mac, handshake_mac(self.master, sid, nonce, b"client")):
            return None
        if not self._remember_sid(sid):
            return None
        reply = os.urandom(16)
        ack = PKT_HEADER.pack(sid_int, PKT_HELLO_ACK, 0) + reply + \
            handshake_mac(self.master, sid, reply, b"server")
        self._hello_cache[sid] = ack
        while len(self._hello_cache) > 2048:
            self._hello_cache.popitem(last=False)
        return ack

    def _codecs(self, sid: bytes) -> tuple[RecordCodec, RecordCodec]:
        # 服务端：发送方向 2（s2c），接收方向 1（c2s）
        c2s, s2c = session_keys(self.master, sid)
        return (RecordCodec(s2c, 2, bool(self.cfg.get("compress", True)), int(self.cfg.get("padding", 0))),
                RecordCodec(c2s, 1, bool(self.cfg.get("compress", True)), int(self.cfg.get("padding", 0))))

    def _spawn(self, coro: Awaitable) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def summary(self) -> dict:
        cfg = dict(self.cfg)
        return {
            "name": self.name, "mode": self.mode, "kind": "transport",
            "status": "online" if self.stats["started_at"] else "offline",
            "protocol": TRANSPORT_MODES[self.mode]["label"],
            "listen": f"{self.cfg.get('listen_host')}:{self.cfg.get('listen_port')}",
            "connect": self.connect_addr(),
            "cipher": "AES-256-GCM",
            "connections": self.stats["connections"],
            "active": self.stats["active"],
            "bytes_in": self.stats["bytes_in"],
            "bytes_out": self.stats["bytes_out"],
            "started_at": self.stats["started_at"],
            "last_error": self.stats["last_error"],
            "config": cfg,
        }

    def connect_addr(self) -> str:
        host = self.cfg.get("connect_host") or self.cfg.get("listen_host") or "127.0.0.1"
        port = self.cfg.get("connect_port") or self.cfg.get("listen_port")
        if self.mode == "ws":
            path = self.cfg.get("ws_path") or "/ws"
            return f"ws://{host}:{port}{path}"
        if self.mode in ("doh", "oss"):
            return f"http://{host}:{port}/"
        if self.mode == "dot":
            return f"tls://{host}:{port}"
        return f"{self.mode}://{host}:{port}"


# ------------------------------------------------------------------ TCP / WebSocket

class StreamSession:
    def __init__(self, server: BaseServer, sid: bytes, peer: str):
        self.server = server
        self.sid = sid
        self.peer = peer
        self.enc, self.dec = server._codecs(sid)

    async def serve_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        hello: bytes = b"") -> None:
        try:
            if not hello:
                hello = await asyncio.wait_for(reader.readexactly(HANDSHAKE_LEN), timeout=15)
            if not self._verify(hello):
                writer.close()
                return
            writer.write(self._ack())
            await writer.drain()
        except Exception:
            writer.close()
            return

        async def send_record(rec: bytes) -> None:
            writer.write(struct.pack(">I", len(rec)) + rec)
            await writer.drain()

        async def recv_record() -> Optional[bytes]:
            head = await reader.readexactly(4)
            size = struct.unpack(">I", head)[0]
            if size <= 0 or size > MAX_FRAME:
                raise ValueError("bad frame size")
            return await reader.readexactly(size)

        channel = Channel(send_record, recv_record, self.enc, self.dec, self.server.max_record)
        try:
            await run_tunnel(self.server, self.peer, channel, self.sid_int)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def serve_ws(self, ws: web.WebSocketResponse, hello: bytes = b"") -> None:
        try:
            if not hello:
                msg = await asyncio.wait_for(ws.receive(), timeout=15)
                if msg.type != aiohttp.WSMsgType.BINARY:
                    await ws.close()
                    return
                hello = msg.data
            if not self._verify(hello):
                await ws.close()
                return
            await ws.send_bytes(self._ack())
        except Exception:
            await ws.close()
            return

        async def send_record(rec: bytes) -> None:
            await ws.send_bytes(rec)

        async def recv_record() -> Optional[bytes]:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.BINARY:
                return msg.data
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
                return None
            return b""

        channel = Channel(send_record, recv_record, self.enc, self.dec, self.server.max_record)
        await run_tunnel(self.server, self.peer, channel, self.sid_int)

    @property
    def sid_int(self) -> int:
        return struct.unpack(">Q", self.sid)[0]

    def _verify(self, hello: bytes) -> bool:
        if len(hello) != HANDSHAKE_LEN or hello[:4] != MAGIC:
            return False
        sid, nonce, mac = hello[4:12], hello[12:28], hello[28:60]
        if sid != self.sid or not self.server._remember_sid(sid):
            return False
        good = handshake_mac(self.server.master, sid, nonce, b"client")
        return hmac.compare_digest(mac, good)

    def _ack(self) -> bytes:
        nonce = os.urandom(16)
        return MAGIC + self.sid + nonce + handshake_mac(self.server.master, self.sid, nonce, b"server")


class TcpServer(BaseServer):
    mode = "tcp"

    def __init__(self, cfg: dict, manager: "TransportManager"):
        super().__init__(cfg, manager)
        self._writers: set[asyncio.StreamWriter] = set()

    async def stop(self) -> None:
        for writer in list(self._writers):
            try:
                writer.close()
            except Exception:
                pass
        self._writers.clear()
        await super().stop()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.cfg.get("listen_host") or "0.0.0.0",
            int(self.cfg.get("listen_port")), reuse_address=True)
        self.stats["started_at"] = time.time()
        log.info("[%s] TCP 传输监听 %s", self.name, self.summary()["listen"])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = "%s:%s" % writer.get_extra_info("peername")[:2]
        try:
            data = await asyncio.wait_for(reader.readexactly(HANDSHAKE_LEN), timeout=15)
        except Exception:
            writer.close()
            return
        if len(data) != HANDSHAKE_LEN or data[:4] != MAGIC:
            writer.close()
            return
        sid = data[4:12]
        self._writers.add(writer)
        sess = StreamSession(self, sid, peer)
        try:
            await sess.serve_tcp(reader, writer, data)
        finally:
            self._writers.discard(writer)


class WsServer(BaseServer):
    mode = "ws"

    def __init__(self, cfg: dict, manager: "TransportManager"):
        super().__init__(cfg, manager)
        self._ws_set: set[web.WebSocketResponse] = set()

    async def stop(self) -> None:
        for ws in list(self._ws_set):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_set.clear()
        await super().stop()

    async def start(self) -> None:
        app = web.Application()
        path = self.cfg.get("ws_path") or "/ws"
        app.router.add_get(path, self._handle)
        await self._aiohttp_app(app)
        self.stats["started_at"] = time.time()
        log.info("[%s] WebSocket 传输监听 %s%s", self.name,
                 self.cfg.get("listen_host"), path)

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=30)
        await ws.prepare(request)
        self._ws_set.add(ws)
        peer = request.remote or "?"
        try:
            msg = await ws.receive()
            if msg.type != aiohttp.WSMsgType.BINARY or len(msg.data) != HANDSHAKE_LEN:
                await ws.close()
                return ws
            sid = msg.data[4:12]
            sess = StreamSession(self, sid, peer)
            await sess.serve_ws(ws, msg.data)
        finally:
            self._ws_set.discard(ws)
        return ws


# ------------------------------------------------------------------ UDP / KCP

class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: "UdpServer"):
        self.server = server
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.server.on_datagram(data, addr)

    def error_received(self, exc):
        log.debug("udp error: %s", exc)


class UdpSession:
    def __init__(self, server: "UdpServer", sid: bytes, addr):
        self.server = server
        self.sid = sid
        self.sid_int = struct.unpack(">Q", sid)[0]
        self.addr = addr
        self.enc, self.dec = server._codecs(sid)
        self.arq = ArqSession(self.sid_int, window=server.window, rto=server.rto)
        self.channel = Channel(self._send_record, self._recv_record, self.enc, self.dec,
                               server.max_record)
        self.task: Optional[asyncio.Task] = None
        self.last_seen = time.time()
        self.closed = False

    def start(self) -> None:
        self.task = self.server._spawn(self._run())

    async def _run(self) -> None:
        await run_tunnel(self.server, "%s:%s" % self.addr[:2], self.channel, self.sid_int)
        self.close()

    async def _send_record(self, rec: bytes) -> None:
        self.arq.queue(rec)
        self._drain()

    async def _recv_record(self) -> Optional[bytes]:
        return await self.arq.reader.get()

    def _drain(self) -> None:
        now = time.time()
        for _ in range(64):
            pkt = self.arq.take_packet(now)
            if pkt is None:
                break
            self.server.send_packet(pkt, self.addr)

    def on_packet(self, pkt: bytes) -> None:
        self.last_seen = time.time()
        self.arq.on_packet(pkt)
        self._drain()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.arq.close()
        self.server._drop_session(self.sid_int)


class UdpServer(BaseServer):
    mode = "udp"
    window = 16
    rto = 0.3
    max_record = 900

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self),
            local_addr=(self.cfg.get("listen_host") or "0.0.0.0", int(self.cfg.get("listen_port"))))
        self.stats["started_at"] = time.time()
        self._spawn(self._janitor())
        log.info("[%s] %s 传输监听 %s", self.name, self.mode.upper(), self.summary()["listen"])

    async def stop(self) -> None:
        try:
            if getattr(self, "_transport", None) is not None:
                self._transport.close()
        except Exception:
            pass
        await super().stop()

    async def _janitor(self) -> None:
        timeout = max(10, int(self.cfg.get("disconnect_timeout") or 30))
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for sess in list(self._sessions.values()):
                if now - sess.last_seen > timeout:
                    sess.close()

    def send_packet(self, pkt: bytes, addr) -> None:
        try:
            self._transport.sendto(pkt, addr)
        except Exception:
            pass

    def _drop_session(self, sid_int: int) -> None:
        self._sessions.pop(sid_int, None)

    def on_datagram(self, data: bytes, addr) -> None:
        if len(data) < PKT_HEADER.size:
            return
        sid, typ, seq = PKT_HEADER.unpack(data[:PKT_HEADER.size])
        sid_b = struct.pack(">Q", sid)
        if typ not in (PKT_HELLO, PKT_HELLO_ACK, PKT_DATA, PKT_ACK, PKT_FIN):
            log.debug("bad packet header %s typ=%d len=%d", data[:16].hex(), typ, len(data))
        if typ == PKT_HELLO:
            body = data[PKT_HEADER.size:]
            ack = self._hello_reply(sid_b, body, sid)
            if ack is None:
                return
            self.send_packet(ack, addr)
            if sid in self._sessions:
                return
            sess = UdpSession(self, sid_b, addr)
            self._sessions[sid] = sess
            sess.start()
            return
        sess = self._sessions.get(sid)
        if sess is not None:
            sess.on_packet(data)


class KcpServer(UdpServer):
    mode = "kcp"
    window = 64
    rto = 0.2
    max_record = 1200


# ------------------------------------------------------------------ DNS / DoH / DoT（轮询）

class PollSession:
    """请求/响应式传输上的可靠会话（DNS/DoH/DoT/OSS）。"""

    def __init__(self, server: BaseServer, sid: bytes):
        self.server = server
        self.sid = sid
        self.sid_int = struct.unpack(">Q", sid)[0]
        self.enc, self.dec = server._codecs(sid)
        self.arq = ArqSession(self.sid_int, window=1, rto=0.35)
        self.channel = Channel(self._send_record, self._recv_record, self.enc, self.dec,
                               server.max_record)
        self.last_seen = time.time()
        self.task: Optional[asyncio.Task] = None
        self.closed = False
        self.queued: deque[bytes] = deque()

    def start(self) -> None:
        self.task = self.server._spawn(run_tunnel(
            self.server, "-", self.channel, self.sid_int))

    async def _send_record(self, rec: bytes) -> None:
        self.arq.queue(rec)
        self.server.poll_sessions[self.sid] = self

    async def _recv_record(self) -> Optional[bytes]:
        return await self.arq.reader.get()

    def on_packet(self, pkt: bytes) -> Optional[bytes]:
        self.last_seen = time.time()
        self.arq.on_packet(pkt)
        return self.arq.take_packet()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.arq.close()
        self.server._drop_session(self.sid_int)


class PollServer(BaseServer):
    """提供 packet_in -> packet_out 的请求/响应传输。"""

    poll_sessions: dict[bytes, PollSession]
    max_record = 400

    def __init__(self, cfg: dict, manager: "TransportManager"):
        super().__init__(cfg, manager)
        self.poll_sessions = {}

    def _drop_session(self, sid_int: int) -> None:
        self.poll_sessions.pop(struct.pack(">Q", sid_int), None)

    def _janitor(self) -> asyncio.Task:
        async def loop():
            timeout = max(10, int(self.cfg.get("disconnect_timeout") or 30))
            while True:
                await asyncio.sleep(5)
                now = time.time()
                for sess in list(self.poll_sessions.values()):
                    if now - sess.last_seen > timeout:
                        sess.close()
        return self._spawn(loop())

    async def stop(self) -> None:
        for sess in list(self.poll_sessions.values()):
            try:
                sess.close()
            except Exception:
                pass
        self.poll_sessions.clear()
        await super().stop()

    def handle_packet(self, data: bytes) -> bytes:
        """处理一个传输包，返回要回给客户端的包（可能为空）。"""
        if len(data) < PKT_HEADER.size:
            return b""
        sid, typ, seq = PKT_HEADER.unpack(data[:PKT_HEADER.size])
        sid_b = struct.pack(">Q", sid)
        if typ not in (PKT_HELLO, PKT_HELLO_ACK, PKT_DATA, PKT_ACK, PKT_FIN):
            log.debug("bad packet header %s typ=%d len=%d", data[:16].hex(), typ, len(data))
        if typ == PKT_HELLO:
            body = data[PKT_HEADER.size:]
            ack = self._hello_reply(sid_b, body, sid)
            if ack is None:
                return b""
            if sid_b not in self.poll_sessions:
                sess = PollSession(self, sid_b)
                self.poll_sessions[sid_b] = sess
                sess.start()
            return ack
        sess = self.poll_sessions.get(sid_b)
        if sess is None:
            log.warning("poll packet for unknown sid type=%d len=%d", typ, len(data))
            return b""
        return sess.on_packet(data) or b""


# --- DNS 编解码

def dns_packet_from_query(wire: bytes, domain: str) -> tuple[Optional[bytes], Any, str]:
    """从 DNS 查询中解出传输包；返回 (packet, query, qname)。"""
    if not _DNS_OK:
        return None, None, ""
    try:
        query = dns.message.from_wire(wire)
        if not query.question:
            return None, None, ""
        qname = str(query.question[0].name).rstrip(".")
        # DoH/DoT：载荷放在 additional TXT（QNAME 只保留短标签）
        for rrset in query.additional:
            if rrset.rdtype == dns.rdatatype.TXT:
                text = "".join(part.decode() for item in rrset for part in item.strings)
                if text:
                    try:
                        return _b32d(text), query, qname
                    except Exception as exc:
                        log.warning("DoH/TXT decode failed: %s (%d chars)", exc, len(text))
                        return None, None, ""
        labels = qname.split(".")
        dom_labels = [x for x in (domain or "").strip(".").split(".") if x]
        if dom_labels:
            if len(labels) <= len(dom_labels) or \
                    [x.lower() for x in labels[-len(dom_labels):]] != [x.lower() for x in dom_labels]:
                return None, query, qname
            labels = labels[:-len(dom_labels)]
        b32 = "".join(labels)
        if not b32:
            return b"", query, qname
        return _b32d(b32), query, qname
    except Exception as exc:
        log.warning("DNS query parse failed: %r", exc)
        return None, None, ""


def dns_response_wire(query: Any, qname: str, payload: bytes) -> bytes:
    try:
        resp = dns.message.make_response(query)
        resp.flags |= dns.flags.AA
        if payload:
            text = _b32e(payload)
            chunks = [text[i:i + 250] for i in range(0, len(text), 250)] or [""]
            rr = dns.rrset.from_text(qname + ".", 0, "IN", "TXT",
                                     " ".join('"%s"' % c for c in chunks))
            resp.answer.append(rr)
        else:
            resp.answer.append(dns.rrset.from_text(qname + ".", 0, "IN", "TXT", '""'))
        return resp.to_wire()
    except Exception:
        return b""


class DnsServer(PollServer):
    mode = "dns"
    max_record = 110

    async def start(self) -> None:
        self.max_record = max(64, 90 - int(self.cfg.get("padding", 0)))
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DnsUdpProtocol(self),
            local_addr=(self.cfg.get("listen_host") or "0.0.0.0", int(self.cfg.get("listen_port"))))
        self._server = await asyncio.start_server(
            self._handle_tcp, self.cfg.get("listen_host") or "0.0.0.0",
            int(self.cfg.get("listen_port")), reuse_address=True)
        self.stats["started_at"] = time.time()
        self._janitor()
        log.info("[%s] DNS 传输监听 %s (udp+tcp)", self.name, self.summary()["listen"])

    async def stop(self) -> None:
        try:
            if getattr(self, "_transport", None) is not None:
                self._transport.close()
        except Exception:
            pass
        await super().stop()

    def domain(self) -> str:
        return (self.cfg.get("dns_domain") or "").strip(".")

    def answer(self, wire: bytes) -> bytes:
        pkt, query, qname = dns_packet_from_query(wire, self.domain())
        if pkt is None or query is None:
            return b""
        reply = self.handle_packet(pkt)
        return dns_response_wire(query, qname, reply)

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readexactly(2), timeout=10)
            size = struct.unpack(">H", head)[0]
            if size <= 0 or size > 8192:
                writer.close()
                return
            wire = await reader.readexactly(size)
            resp = self.answer(wire)
            if resp:
                writer.write(struct.pack(">H", len(resp)) + resp)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()


class _DnsUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: DnsServer):
        self.server = server
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        resp = self.server.answer(data)
        if resp:
            try:
                self.transport.sendto(resp, addr)
            except Exception:
                pass


class DohServer(PollServer):
    mode = "doh"
    max_record = 900

    async def start(self) -> None:
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_post("/dns-query", self._post)
        app.router.add_get("/dns-query", self._get)
        app.router.add_get("/", self._index)
        await self._aiohttp_app(app)
        self.stats["started_at"] = time.time()
        self._janitor()
        log.info("[%s] DoH 传输监听 %s/dns-query", self.name, self.summary()["listen"])

    async def _index(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "mode": "doh", "endpoint": "/dns-query"})

    async def _post(self, request: web.Request) -> web.Response:
        body = await request.read()
        resp = self.answer(body)
        if not resp:
            log.warning("DoH bad request: %d bytes: %s", len(body), body[:80].hex())
            return web.Response(status=400, text="bad dns message")
        return web.Response(body=resp, content_type="application/dns-message")

    async def _get(self, request: web.Request) -> web.Response:
        raw = request.query.get("dns", "")
        try:
            wire = _b64d(raw)
        except Exception:
            return web.Response(status=400, text="bad dns parameter")
        resp = self.answer(wire)
        if not resp:
            return web.Response(status=400, text="bad dns message")
        return web.Response(body=resp, content_type="application/dns-message")

    def answer(self, wire: bytes) -> bytes:
        pkt, query, qname = dns_packet_from_query(wire, self.domain())
        if pkt is None or query is None:
            return b""
        reply = self.handle_packet(pkt)
        return dns_response_wire(query, qname, reply)

    def domain(self) -> str:
        return (self.cfg.get("dns_domain") or "").strip(".")


class DotServer(PollServer):
    mode = "dot"
    max_record = 900

    async def start(self) -> None:
        ctx = self._tls_context()
        self._server = await asyncio.start_server(
            self._handle, self.cfg.get("listen_host") or "0.0.0.0",
            int(self.cfg.get("listen_port")), ssl=ctx, reuse_address=True)
        self.stats["started_at"] = time.time()
        self._janitor()
        log.info("[%s] DoT 传输监听 %s (tls)", self.name, self.summary()["listen"])

    def _tls_context(self) -> ssl.SSLContext:
        cert_dir = self.manager.cert_dir
        cert_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.name)
        cert_path = cert_dir / f"{safe}.crt"
        key_path = cert_dir / f"{safe}.key"
        if not cert_path.exists() or not key_path.exists():
            self._generate_cert(cert_path, key_path)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_path), str(key_path))
        ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
        return ctx

    @staticmethod
    def _generate_cert(cert_path: Path, key_path: Path) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "havoc-transport-dot"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Havoc Lab"),
        ])
        cert = (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(issuer)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.utcnow())
                .not_valid_after(datetime.utcnow() + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        os.chmod(key_path, 0o600)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await asyncio.wait_for(reader.readexactly(2), timeout=120)
                size = struct.unpack(">H", head)[0]
                if size <= 0 or size > 65535:
                    break
                wire = await reader.readexactly(size)
                resp = self.answer(wire)
                if resp:
                    writer.write(struct.pack(">H", len(resp)) + resp)
                    await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    def answer(self, wire: bytes) -> bytes:
        pkt, query, qname = dns_packet_from_query(wire, self.domain())
        if pkt is None or query is None:
            return b""
        reply = self.handle_packet(pkt)
        return dns_response_wire(query, qname, reply)

    def domain(self) -> str:
        return (self.cfg.get("dns_domain") or "").strip(".")


# ------------------------------------------------------------------ OSS（对象存储）

class OssServer(PollServer):
    mode = "oss"
    max_record = 1200

    async def start(self) -> None:
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_put("/{prefix}/{sid}/up/{seq}", self._put)
        app.router.add_get("/{prefix}/{sid}/down/{ack}", self._get)
        app.router.add_post("/{prefix}/{sid}/poll", self._poll)
        app.router.add_delete("/{prefix}/{sid}", self._delete)
        app.router.add_get("/", self._index)
        await self._aiohttp_app(app)
        self.stats["started_at"] = time.time()
        self._janitor()
        log.info("[%s] OSS 传输监听 %s", self.name, self.summary()["listen"])

    async def _index(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "mode": "oss", "bucket": "havoc-transport"})

    def _sid(self, raw: str) -> Optional[bytes]:
        try:
            return bytes.fromhex(raw)
        except Exception:
            return None

    async def _put(self, request: web.Request) -> web.Response:
        sid = self._sid(request.match_info["sid"])
        if sid is None or len(sid) != 8:
            return web.Response(status=400)
        body = await request.read()
        reply = self.handle_packet(body)
        self._pending[sid] = reply
        return web.json_response({"ok": True, "len": len(body)})

    async def _get(self, request: web.Request) -> web.Response:
        sid = self._sid(request.match_info["sid"])
        if sid is None:
            return web.Response(status=400)
        reply = self._pending.pop(sid, None)
        if not reply:
            return web.Response(status=204)
        return web.Response(body=reply, content_type="application/octet-stream")

    async def _poll(self, request: web.Request) -> web.Response:
        sid = self._sid(request.match_info["sid"])
        if sid is None:
            return web.Response(status=400)
        body = await request.read()
        reply = self.handle_packet(body)
        if not reply:
            return web.Response(status=204)
        return web.Response(body=reply, content_type="application/octet-stream")

    async def _delete(self, request: web.Request) -> web.Response:
        sid = self._sid(request.match_info["sid"])
        if sid:
            self.poll_sessions.pop(sid, None)
            self._pending.pop(sid, None)
        return web.json_response({"ok": True})

    _pending: dict[bytes, bytes]

    def __init__(self, cfg: dict, manager: "TransportManager"):
        super().__init__(cfg, manager)
        self._pending = {}


# ------------------------------------------------------------------ 管理器

SERVER_CLASSES = {
    "tcp": TcpServer, "kcp": KcpServer, "udp": UdpServer, "ws": WsServer,
    "dns": DnsServer, "doh": DohServer, "dot": DotServer, "oss": OssServer,
}


class TransportManager:
    def __init__(self, data_dir: Path, publish: Optional[Callable[[dict], None]] = None,
                 event: Optional[Callable[[str, str], None]] = None):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "transports.json"
        self.cert_dir = self.data_dir / "transports" / "certs"
        self.publish = publish or (lambda _msg: None)
        self.event = event or (lambda _text, _level: None)
        self.configs: dict[str, dict] = {}
        self.servers: dict[str, BaseServer] = {}
        self._load()

    # ---- 持久化
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("listeners", []):
                mode = normalize_mode(item.get("mode", ""))
                if mode in TRANSPORT_MODES and item.get("name"):
                    self.configs[item["name"]] = self._normalize(item, mode)
        except Exception as exc:
            log.warning("读取 transports.json 失败：%s", exc)

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"listeners": list(self.configs.values())}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _normalize(raw: dict, mode: str) -> dict:
        cfg = {
            "name": str(raw.get("name", "")).strip(),
            "mode": mode,
            "listen_host": str(raw.get("listen_host") or "0.0.0.0").strip(),
            "listen_port": int(raw.get("listen_port") or TRANSPORT_MODES[mode]["default_port"]),
            "connect_host": str(raw.get("connect_host") or "").strip(),
            "connect_port": int(raw.get("connect_port") or raw.get("listen_port") or
                                TRANSPORT_MODES[mode]["default_port"]),
            "ws_path": str(raw.get("ws_path") or "/ws").strip() or "/ws",
            "dns_domain": str(raw.get("dns_domain") or "").strip().strip("."),
            "public_dns": str(raw.get("public_dns") or "").strip(),
            "oss_url": str(raw.get("oss_url") or "").strip(),
            "oss_prefix": str(raw.get("oss_prefix") or "havoc").strip().strip("/") or "havoc",
            "upstream_host": str(raw.get("upstream_host") or "127.0.0.1").strip(),
            "upstream_port": int(raw.get("upstream_port") or 8080),
            "vkey": str(raw.get("vkey") or secrets.token_hex(12)),
            "salt": str(raw.get("salt") or secrets.token_hex(8)),
            "ping_interval": max(1, int(raw.get("ping_interval") or 10)),
            "disconnect_timeout": max(5, int(raw.get("disconnect_timeout") or 30)),
            "compress": bool(raw.get("compress", True)),
            "padding": max(0, min(255, int(raw.get("padding") or 0))),
            "http_rewrite": bool(raw.get("http_rewrite", True)),
            "remark": str(raw.get("remark") or "")[:200],
            "autostart": bool(raw.get("autostart", True)),
        }
        return cfg

    # ---- CRUD
    def get(self, name: str) -> Optional[dict]:
        return self.configs.get(name)

    def add(self, raw: dict) -> dict:
        mode = normalize_mode(raw.get("mode") or raw.get("protocol", ""))
        if mode not in TRANSPORT_MODES:
            raise ValueError("不支持的传输模式")
        name = str(raw.get("name", "")).strip()
        if not name or len(name) > 48:
            raise ValueError("名称必填（<=48 字符）")
        if name in self.configs:
            raise ValueError("同名传输监听器已存在")
        cfg = self._normalize(raw, mode)
        self._validate(cfg)
        self.configs[name] = cfg
        self._save()
        self.event(f"传输监听器已创建：{name}（{TRANSPORT_MODES[mode]['label']}）", "info")
        return cfg

    def update(self, name: str, raw: dict) -> dict:
        old = self.configs.get(name)
        if not old:
            raise ValueError("监听器不存在")
        mode = normalize_mode(raw.get("mode") or old["mode"])
        if mode not in TRANSPORT_MODES:
            raise ValueError("不支持的传输模式")
        cfg = self._normalize({**old, **raw, "name": name}, mode)
        self._validate(cfg)
        self.configs[name] = cfg
        self._save()
        return cfg

    async def remove(self, name: str) -> None:
        cfg = self.configs.pop(name, None)
        if cfg is None:
            raise ValueError("监听器不存在")
        server = self.servers.pop(name, None)
        if server is not None:
            server.stats["started_at"] = 0
            log.info("[%s] removing transport: stopping server", name)
            await server.stop()
            log.info("[%s] transport stopped, saving", name)
        self._save()
        self.event(f"传输监听器已删除：{name}", "warn")

    @staticmethod
    def _validate(cfg: dict) -> None:
        port = cfg["listen_port"]
        if not (1 <= port <= 65535):
            raise ValueError("监听端口无效")
        if not (1 <= cfg["upstream_port"] <= 65535):
            raise ValueError("上游端口无效")
        if len(cfg["vkey"]) < 8:
            raise ValueError("通信密钥 vkey 至少 8 个字符")
        if len(cfg["salt"]) < 8:
            raise ValueError("加密盐 salt 至少 8 个字符")
        if cfg["mode"] in ("dns", "doh", "dot") and not cfg["dns_domain"]:
            raise ValueError("DNS/DoH/DoT 模式需要填写隧道域名")

    # ---- 启停
    async def start(self, name: str) -> dict:
        cfg = self.configs.get(name)
        if not cfg:
            raise ValueError("监听器不存在")
        if name in self.servers:
            return self.summary(name)
        cls = SERVER_CLASSES[cfg["mode"]]
        server = cls(cfg, self)
        await server.start()
        self.servers[name] = server
        self.publish({"type": "transport", "action": "start", "listener": server.summary()})
        self.event(f"传输监听器上线：{name}（{cfg['mode']} "
                   f"{cfg['listen_host']}:{cfg['listen_port']}）", "good")
        return self.summary(name)

    async def stop(self, name: str) -> dict:
        server = self.servers.pop(name, None)
        if server is not None:
            await server.stop()
        self.publish({"type": "transport", "action": "stop", "name": name})
        self.event(f"传输监听器停止：{name}", "warn")
        return self.summary(name)

    async def start_all(self) -> None:
        for name, cfg in self.configs.items():
            if not cfg.get("autostart", True):
                continue
            try:
                await self.start(name)
            except Exception as exc:
                log.error("启动传输监听器 %s 失败：%s", name, exc)
                self.event(f"传输监听器启动失败：{name} - {exc}", "error")

    async def stop_all(self) -> None:
        for name in list(self.servers):
            try:
                await self.stop(name)
            except Exception:
                pass

    # ---- 查询
    def summary(self, name: str) -> dict:
        cfg = self.configs.get(name, {})
        server = self.servers.get(name)
        if server is not None:
            return server.summary()
        mode = cfg.get("mode", "")
        item = {
            "name": name, "mode": mode, "kind": "transport", "status": "offline",
            "protocol": TRANSPORT_MODES.get(mode, {}).get("label", mode),
            "listen": f"{cfg.get('listen_host')}:{cfg.get('listen_port')}",
            "connect": "", "cipher": "AES-256-GCM",
            "connections": 0, "active": 0, "bytes_in": 0, "bytes_out": 0,
            "started_at": 0, "last_error": "", "config": cfg,
        }
        if cfg:
            host = cfg.get("connect_host") or cfg.get("listen_host") or "127.0.0.1"
            port = cfg.get("connect_port") or cfg.get("listen_port")
            if mode == "ws":
                item["connect"] = f"ws://{host}:{port}{cfg.get('ws_path', '/ws')}"
            elif mode in ("doh", "oss"):
                item["connect"] = f"http://{host}:{port}/"
            elif mode == "dot":
                item["connect"] = f"tls://{host}:{port}"
            else:
                item["connect"] = f"{mode}://{host}:{port}"
        return item

    def list(self) -> list[dict]:
        return [self.summary(name) for name in self.configs]

    def on_error(self, name: str, message: str) -> None:
        self.publish({"type": "transport", "action": "error", "name": name,
                      "message": message})
        self.event(f"传输监听器错误：{name} - {message}", "error")
