#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按需交叉编译 HTP/1 Go 中继器（Windows/Linux）。

每个传输监听器生成一个内嵌配置的独立二进制：
- 目标上运行后监听 ``local_listen``（默认 127.0.0.1:1080）；
- Demon 载荷的 HTTP 代理指向该地址，C2 流量即走加密传输；
- 配置（vkey/salt/模式/地址）通过 ``-X main.embeddedConfigB64`` 内嵌，
  运行时可被 ``--config``/``--listen``/``--server`` 覆盖。

仅用于已获得明确书面授权的测试环境。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("havoc-webui.relay")

GOOS_ARCH = {
    ("windows", "amd64"): ("windows", "amd64", ".exe"),
    ("windows", "386"): ("windows", "386", ".exe"),
    ("windows", "arm64"): ("windows", "arm64", ".exe"),
    ("linux", "amd64"): ("linux", "amd64", ""),
    ("linux", "386"): ("linux", "386", ""),
    ("linux", "arm64"): ("linux", "arm64", ""),
}


class RelayBuilder:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.source = self.root / "tools" / "relay"
        self.output = self.root / "data" / "relays"
        self.go = self.root.parent / "toolchain" / "go" / "bin" / "go"
        self.gocache = self.root.parent / "toolchain" / "gocache"
        self._lock = asyncio.Lock()

    # ---- 配置
    @staticmethod
    def relay_config(cfg: dict, local_listen: str = "127.0.0.1:1080") -> dict:
        host = cfg.get("connect_host") or cfg.get("listen_host") or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        port = int(cfg.get("connect_port") or cfg.get("listen_port") or 0)
        oss_url = cfg.get("oss_url") or ""
        if cfg.get("mode") == "oss" and not oss_url:
            oss_url = f"http://{host}:{port}"
        return {
            "mode": cfg.get("mode", "tcp"),
            "server_host": host,
            "server_port": port,
            "ws_path": cfg.get("ws_path") or "/ws",
            "dns_domain": cfg.get("dns_domain") or "",
            "oss_url": oss_url,
            "oss_prefix": cfg.get("oss_prefix") or "havoc",
            "doh_url": cfg.get("doh_url") or "",
            "vkey": cfg.get("vkey", ""),
            "salt": cfg.get("salt", ""),
            "compress": bool(cfg.get("compress", True)),
            "padding": int(cfg.get("padding") or 0),
            "local_listen": local_listen or "127.0.0.1:1080",
            "poll_interval_ms": 20,
            "ping_interval": int(cfg.get("ping_interval") or 10),
        }

    def _binary_path(self, name: str, goos: str, goarch: str, payload: dict) -> Path:
        digest = hashlib.sha256(
            json.dumps({"cfg": payload, "os": goos, "arch": goarch},
                       sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        ext = GOOS_ARCH[(goos, goarch)][2]
        return self.output / safe / f"relay-{goos}-{goarch}-{digest}{ext}"

    # ---- 编译
    def build_sync(self, name: str, cfg: dict, goos: str, goarch: str,
                   local_listen: str = "127.0.0.1:1080") -> Path:
        if (goos, goarch) not in GOOS_ARCH:
            raise ValueError("不支持的平台/架构")
        if not self.go.exists():
            raise RuntimeError(f"未找到 Go 工具链：{self.go}")
        payload = self.relay_config(cfg, local_listen)
        target = self._binary_path(name, goos, goarch, payload)
        if target.exists() and target.stat().st_size > 1024:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()).decode().rstrip("=")
        env = dict(os.environ)
        goos_v, goarch_v, _ = GOOS_ARCH[(goos, goarch)]
        env.update({
            "GOOS": goos_v, "GOARCH": goarch_v, "CGO_ENABLED": "0",
            "GOPROXY": "off", "GOFLAGS": "-mod=mod",
            "GOCACHE": str(self.gocache), "HOME": env.get("HOME", "/tmp"),
        })
        if self.gocache.exists():
            env["GOCACHE"] = str(self.gocache)
        cmd = [str(self.go), "build", "-trimpath",
               "-ldflags", f"-s -w -X main.embeddedConfigB64={encoded}",
               "-o", str(target), "."]
        log.info("building relay %s (%s/%s) -> %s", name, goos, goarch, target)
        started = time.time()
        proc = subprocess.run(cmd, cwd=str(self.source), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace")[-1200:]
            raise RuntimeError(f"中继器编译失败：{detail or proc.returncode}")
        try:
            os.chmod(target, 0o755)
        except OSError:
            pass
        log.info("relay built in %.1fs: %s (%d bytes)", time.time() - started,
                 target, target.stat().st_size if target.exists() else -1)
        return target

    async def build(self, name: str, cfg: dict, goos: str, goarch: str,
                    local_listen: str = "127.0.0.1:1080") -> Path:
        async with self._lock:
            return await asyncio.to_thread(self.build_sync, name, cfg, goos, goarch, local_listen)
