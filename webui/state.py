#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Havoc WebUI 网关的共享状态：会话 / 监听器 / 任务 / 控制台 / 事件。

只做协议解码与缓存，不直接触碰网络；网络由 havoc_client.py 负责。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("havoc-webui")

# 事件常量（client/src/Havoc/Packager.cc）
EV_INIT = 1
EV_LISTENER = 2
EV_CHAT = 4
EV_GATE = 5
EV_SESSION = 7
EV_SERVICE = 9
EV_TEAMSERVER = 0x10

SUB_INIT_SUCCESS = 1
SUB_INIT_ERROR = 2
SUB_INIT_LOGIN = 3

SUB_LISTENER_ADD = 1
SUB_LISTENER_EDIT = 2
SUB_LISTENER_REMOVE = 3
SUB_LISTENER_MARK = 4
SUB_LISTENER_ERROR = 5

SUB_CHAT_MESSAGE = 1
SUB_CHAT_LISTENER = 2
SUB_CHAT_SESSION = 3
SUB_CHAT_USER = 4
SUB_CHAT_USER_DISCONNECT = 5

SUB_SESSION_NEW = 1
SUB_SESSION_REMOVE = 2
SUB_SESSION_SEND = 3
SUB_SESSION_RECEIVE = 4
SUB_SESSION_MARK = 5

SUB_TS_LOG = 1
SUB_TS_PROFILE = 2

CMD_CALLBACK = 10
CMD_CONSOLE = 128


def now_ts() -> float:
    return time.time()


def parse_havoc_time(value: str) -> Optional[float]:
    """Havoc 的时间格式混用 '/' 与 '-'，两种都尝试。"""
    if not value:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    return None


def local_time_str(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else now_ts()))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes", "alive")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return default


class Task:
    __slots__ = ("task_id", "demon", "command", "command_id", "status", "created",
                 "updated", "output", "note", "completed")

    def __init__(self, task_id: str, demon: str, command: str, command_id: str, note: str = ""):
        self.task_id = task_id
        self.demon = demon
        self.command = command
        self.command_id = command_id
        self.status = "queued"
        self.created = now_ts()
        self.updated = self.created
        self.output: list[str] = []
        self.note = note
        self.completed = False

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "demon": self.demon,
            "command": self.command,
            "command_id": self.command_id,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
            "output": "".join(self.output),
            "note": self.note,
            "completed": self.completed,
        }


class AppState:
    def __init__(self, events_limit: int = 3000, console_limit: int = 3000,
                 task_limit_per_demon: int = 400, data_dir: Optional[Path] = None):
        self.events_limit = events_limit
        self.console_limit = console_limit
        self.task_limit = task_limit_per_demon
        self.data_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parent / "data"
        self.loot_dir = self.data_dir / "loot"
        self.payload_dir = self.data_dir / "payloads"
        # teamserver 的下载落地目录：<havoc>/havoc-src/data/loot/<会话>/agents/<demon>/Download
        self.teamserver_loot_dir = self.data_dir.parent.parent / "havoc-src" / "data" / "loot"
        self.pending_downloads: dict[str, list] = {}

        self.sessions: dict[str, dict] = {}
        self.session_raw: dict[str, dict] = {}
        # 操作员本地移除的会话（仅本连接有效，网关重连时清空）
        self.hidden_sessions: set[str] = set()
        self.listeners: dict[str, dict] = {}
        # 服务/插件注册的自定义监听器协议（EV_SERVICE SubEvent=2）
        self.service_listeners: dict[str, dict] = {}
        self.profile: dict = {}

        # 团队聊天 / 战利品 / 文件与进程缓存
        self.chat: deque[dict] = deque(maxlen=600)
        self.chat_seq = 0
        self.loot: deque[dict] = deque(maxlen=400)
        self.loot_seq = 0
        self.files: dict[str, dict] = {}
        self.processes: dict[str, dict] = {}

        # 载荷构建（Gate Stageless）与 Python 脚本注册表
        self.payload_builds: dict[str, dict] = {}
        self.payload_seq = 0
        self.active_build: Optional[str] = None
        self.python_commands: dict[str, dict] = {}
        self.python_modules: dict[str, dict] = {}
        self.python_scripts: dict[str, dict] = {}
        self.python_bof_results: dict[str, dict] = {}

        # 投递链（webui 本地注解，不写 teamserver 的 PivotParent）：
        # child session ID -> {parent, command, task_id, source, ts}
        self.delivery_edges_path = self.data_dir / "delivery_edges.json"
        self.delivery_edges: dict[str, dict] = {}

        self._load_loot()
        self._load_payloads()
        self._load_delivery_edges()

        self.events: deque[dict] = deque(maxlen=events_limit)
        self.event_seq = 0
        self.event_dedupe: deque[str] = deque(maxlen=events_limit)
        self.console: dict[str, deque] = defaultdict(lambda: deque(maxlen=console_limit))
        self.tasks: dict[str, Task] = {}
        self.tasks_by_demon: dict[str, deque] = defaultdict(lambda: deque(maxlen=task_limit_per_demon))
        self.pending: dict[str, deque] = defaultdict(deque)
        self.active_task: dict[str, Optional[str]] = {}

        # 连接状态（由 havoc_client 更新）
        self.connection = {
            "state": "starting",           # starting | connecting | connected | auth_failed | disconnected
            "detail": "",
            "since": None,
            "user": "",
            "teamserver": "",
            "last_message_ts": None,
            "reconnects": 0,
        }
        self.started_at = now_ts()

        self.subscribers: set[asyncio.Queue] = set()
        self._last_session_publish: dict[str, float] = {}
        # 新会话事件钩子（gateway 用于触发 Python RegisterCallback/OnNewSession）
        self.new_session_hook = None

    def _load_loot(self) -> None:
        """网关重启后从 data/loot 恢复历史战利品（截图/下载）。"""
        if not self.loot_dir.is_dir():
            return
        image_ext = (".png", ".jpg", ".jpeg", ".bmp", ".gif")
        try:
            files = [p for p in self.loot_dir.rglob("*")
                     if p.is_file() and ".thumbs" not in p.parts]
            files.sort(key=lambda p: p.stat().st_mtime)
            for path in files[-200:]:
                self.loot_seq += 1
                stat = path.stat()
                self.loot.append({
                    "id": self.loot_seq,
                    "kind": "screenshot" if path.suffix.lower() in image_ext else "download",
                    "demon": path.parent.name,
                    "name": path.name,
                    "size": str(stat.st_size),
                    "path": str(path),
                    "ts": stat.st_mtime,
                    "time": local_time_str(stat.st_mtime),
                })
        except OSError:
            pass

    def _load_payloads(self) -> None:
        """网关重启后从 data/payloads 恢复历史载荷条目。"""
        if not self.payload_dir.is_dir():
            return
        try:
            entries = []
            for d in self.payload_dir.iterdir():
                if not d.is_dir() or not d.name.isdigit():
                    continue
                files = [p for p in d.iterdir() if p.is_file()]
                if not files:
                    continue
                path = max(files, key=lambda p: p.stat().st_mtime)
                stat = path.stat()
                entries.append((int(d.name), path, stat))
            entries.sort(key=lambda e: e[0])
            for bid, path, stat in entries[-20:]:
                self.payload_seq = max(self.payload_seq, bid)
                self.payload_builds[f"{bid:04d}"] = {
                    "id": f"{bid:04d}",
                    "status": "done",
                    "started": stat.st_mtime,
                    "finished": stat.st_mtime,
                    "agent": "Demon",
                    "listener": "",
                    "arch": "x64" if ".x64" in path.name else "x86",
                    "format": "Windows Exe" if path.suffix == ".exe" else (path.suffix.lstrip(".") or "bin"),
                    "logs": [],
                    "filename": path.name,
                    "path": str(path),
                    "size": stat.st_size,
                    "error": "",
                }
        except OSError:
            pass

    # ---------------- 广播 ----------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=800)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, message: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # 浏览器消费太慢：丢弃该条，不让网关阻塞
                pass

    # ---------------- 事件日志 ----------------

    def add_event(self, text: str, level: str = "info", ts: Optional[float] = None,
                  source: str = "teamserver", dedupe: bool = True) -> Optional[dict]:
        if not text:
            return None
        text = text.rstrip("\r\n")
        if not text.strip():
            return None
        stamp = ts if ts is not None else now_ts()
        key = f"{round(stamp, 3)}|{level}|{text}"
        if dedupe:
            if key in self.event_dedupe:
                return None
            self.event_dedupe.append(key)
        self.event_seq += 1
        ev = {
            "id": self.event_seq,
            "ts": stamp,
            "time": local_time_str(stamp),
            "level": level,
            "text": text,
            "source": source,
        }
        self.events.append(ev)
        self.publish({"type": "event", "event": ev})
        return ev

    def parse_logger_line(self, text: str, ts: Optional[float]) -> None:
        level = "info"
        upper = text.upper()
        if "[GOOD]" in upper or "[+]" in text:
            level = "good"
        elif any(tag in upper for tag in ("[ERROR]", "[ERRO]", "[-]", "[!]")):
            level = "error"
        elif "[WARN]" in upper or "[WARNING]" in upper:
            level = "warn"
        elif "[DEBUG]" in upper:
            level = "debug"
        self.add_event(text, level=level, ts=ts, source="teamserver")

    # ---------------- 控制台 ----------------

    def console_append(self, demon: str, kind: str, text: str, task_id: Optional[str] = None) -> None:
        if not text:
            return
        if len(text) > 200_000:
            text = text[:200_000] + f"\n…（输出过长已截断，原始长度 {len(text)} 字符）"
        line = {
            "ts": now_ts(),
            "time": local_time_str(),
            "kind": kind,
            "text": text,
            "task_id": task_id,
        }
        self.console[demon].append(line)
        self.publish({"type": "console", "demon": demon, "line": line})

    # ---------------- 任务 ----------------

    def new_task(self, demon: str, command: str, command_id: str, task_id: str, note: str = "") -> Task:
        task = Task(task_id, demon, command, command_id, note)
        self.tasks[task_id] = task
        self.tasks_by_demon[demon].append(task_id)
        self.pending[demon].append(task_id)
        self.console_append(demon, "prompt", command, task_id)
        self.publish({"type": "task", "task": task.to_dict()})
        # 清理旧任务字典，避免无限增长
        while len(self.tasks_by_demon[demon]) >= self.task_limit:
            old = self.tasks_by_demon[demon][0]
            if old in self.tasks and self.tasks[old].completed:
                self.tasks.pop(old, None)
            break
        return task

    def mark_task_sent(self, demon: str) -> None:
        if not self.pending[demon]:
            return
        task_id = self.pending[demon].popleft()
        task = self.tasks.get(task_id)
        if not task:
            return
        task.status = "sent"
        task.updated = now_ts()
        self.active_task[demon] = task_id
        self.publish({"type": "task", "task": task.to_dict()})

    def append_task_output(self, demon: str, text: str) -> None:
        task_id = self.active_task.get(demon)
        task = self.tasks.get(task_id) if task_id else None
        if not task:
            return
        if sum(len(chunk) for chunk in task.output) > 400_000:
            return
        task.output.append(text)
        task.updated = now_ts()
        task.status = "done"
        task.completed = True
        self.publish({"type": "task", "task": task.to_dict()})

    def fail_active_task(self, demon: str, message: str) -> None:
        task_id = self.active_task.get(demon)
        task = self.tasks.get(task_id) if task_id else None
        if task:
            task.status = "error"
            task.completed = True
            task.updated = now_ts()
            task.output.append(message)
            self.publish({"type": "task", "task": task.to_dict()})

    def tasks_for(self, demon: str, limit: int = 50) -> list[dict]:
        out = []
        for tid in reversed(self.tasks_by_demon.get(demon, ())):
            task = self.tasks.get(tid)
            if task:
                out.append(task.to_dict())
            if len(out) >= limit:
                break
        return out

    # ---------------- 会话 ----------------

    def session_from_info(self, info: dict) -> dict:
        sid = info.get("NameID", "")
        last_call = info.get("LastCallIn", "")
        last_ts = parse_havoc_time(last_call)
        sleep = _as_int(info.get("SleepDelay"), 0)
        jitter = _as_int(info.get("SleepJitter"), 0)
        session = {
            "id": sid,
            "host": info.get("Hostname", ""),
            "user": info.get("Username", ""),
            "domain": info.get("DomainName", ""),
            "os": info.get("OSVersion", ""),
            "platform": info.get("Platform", ""),
            "os_build": info.get("OSBuild", ""),
            "os_arch": info.get("OSArch", ""),
            "process": info.get("ProcessName", ""),
            "pid": str(info.get("ProcessPID", "")),
            "ppid": str(info.get("ProcessPPID", "")),
            "proc_arch": info.get("ProcessArch", ""),
            "internal_ip": info.get("InternalIP", ""),
            "external_ip": info.get("ExternalIP", ""),
            "listener": info.get("Listener", ""),
            "elevated": _as_bool(info.get("Elevated", False)),
            "active": _as_bool(info.get("Active", True)),
            "sleep": sleep,
            "jitter": jitter,
            "kill_date": _as_int(info.get("KillDate"), 0),
            "working_hours": _as_int(info.get("WorkingHours"), 0),
            "first_call": info.get("FirstCallIn", ""),
            "last_call": last_call,
            "last_seen_ts": last_ts,
            "tasked_once": _as_bool(info.get("TaskedOnce", False)),
            "pivot_parent": info.get("PivotParent", ""),
            "pivot_disconnected": False,
            "delivery_parent": "",
            "delivery": None,
            "magic": info.get("MagicValue", ""),
        }
        session["health"] = self.compute_health(session)
        return session

    @staticmethod
    def compute_health(session: dict) -> str:
        if not session.get("active"):
            return "dead"
        last = session.get("last_seen_ts")
        if not last:
            return "unknown"
        sleep = max(1, session.get("sleep") or 1)
        jitter = session.get("jitter") or 0
        grace = max(30.0, sleep * 3 + sleep * jitter / 100.0 * 3 + 10)
        if now_ts() - last <= grace:
            return "healthy"
        return "stale"

    def upsert_session(self, info: dict) -> Optional[dict]:
        session = self.session_from_info(info)
        sid = session["id"]
        edge = self.delivery_edges.get(sid)
        if edge:
            session["delivery_parent"] = edge.get("parent", "")
            session["delivery"] = edge
        if sid in self.hidden_sessions:
            # 操作员已「移除会话」：同一连接内 teamserver 重放/心跳只更新 raw，
            # 不再出现在表格与拓扑中；网关重连时清空 hidden，重放后重新可见。
            if sid:
                self.session_raw[sid] = info
            return None
        self.sessions[sid] = session
        self.session_raw[sid] = info
        self.publish({"type": "session", "action": "update", "session": session})
        return session

    # ---------------- 投递链（webui 本地注解） ----------------

    def _load_delivery_edges(self) -> None:
        path = self.delivery_edges_path
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("load delivery edges failed: %s", exc)
            return
        if not isinstance(data, dict):
            return
        for child, record in data.items():
            if not isinstance(record, dict):
                continue
            parent = str(record.get("parent") or "").strip()
            child = str(child or "").strip()
            if not child or not parent or child == parent:
                continue
            self.delivery_edges[child] = {
                "parent": parent,
                "command": str(record.get("command") or "")[:1000],
                "task_id": str(record.get("task_id") or ""),
                "source": str(record.get("source") or "manual"),
                "ts": float(record.get("ts") or now_ts()),
            }

    def _save_delivery_edges(self) -> None:
        path = self.delivery_edges_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self.delivery_edges, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            log.warning("save delivery edges failed: %s", exc)

    def get_delivery_edge(self, sid: str) -> Optional[dict]:
        return self.delivery_edges.get(sid)

    def _delivery_would_cycle(self, child: str, parent: str) -> bool:
        seen: set[str] = set()
        current = parent
        while current and current not in seen:
            if current == child:
                return True
            seen.add(current)
            record = self.delivery_edges.get(current) or {}
            current = str(record.get("parent") or "")
        return False

    def set_delivery_edge(self, child: str, parent: str, *, command: str = "",
                          task_id: str = "", source: str = "manual") -> dict:
        """设置 Web 投递链（不修改 teamserver 的 PivotParent）。"""
        child = str(child or "").strip()
        parent = str(parent or "").strip()
        if not child or not parent or child == parent:
            raise ValueError("父会话/子会话无效")
        if child not in self.sessions or parent not in self.sessions:
            raise ValueError("会话不存在")
        if self._delivery_would_cycle(child, parent):
            raise ValueError("投递链不能形成环")
        record = {
            "parent": parent,
            "command": str(command or "")[:1000],
            "task_id": str(task_id or ""),
            "source": str(source or "manual"),
            "ts": now_ts(),
        }
        self.delivery_edges[child] = record
        self._save_delivery_edges()
        session = self.sessions.get(child)
        if session is not None:
            session["delivery_parent"] = parent
            session["delivery"] = record
            self.publish({"type": "session", "action": "update", "session": session})
        return record

    def clear_delivery_edge(self, child: str) -> bool:
        child = str(child or "").strip()
        existed = self.delivery_edges.pop(child, None) is not None
        if not existed:
            return False
        self._save_delivery_edges()
        session = self.sessions.get(child)
        if session is not None:
            session["delivery_parent"] = ""
            session["delivery"] = None
            self.publish({"type": "session", "action": "update", "session": session})
        return True

    @staticmethod
    def _reparent_children(children_source: dict, sid: str, publish) -> None:
        for child in children_source.values():
            if child.get("pivot_parent") == sid:
                child["pivot_parent"] = ""
                publish({"type": "session", "action": "update", "session": child})

    def remove_session(self, sid: str) -> None:
        self.hidden_sessions.discard(sid)
        self.sessions.pop(sid, None)
        self.session_raw.pop(sid, None)
        self.publish({"type": "session", "action": "remove", "id": sid})
        # 父会话被移除后，原来的枢轴子会话回到根节点，避免拓扑里留下指向不存在节点的边。
        self._reparent_children(self.sessions, sid, self.publish)

    def hide_session(self, sid: str) -> bool:
        """操作员本地移除会话（不向 teamserver 发送踢下线命令）。

        与 remove_session 的区别：不触发 teamserver 的会话移除语义，而是把 sid
        放进 hidden_sessions，屏蔽后续同连接内的重放/心跳；清理该会话的控制台、
        文件/进程缓存与任务。子会话仍回到根节点。
        """
        existed = sid in self.sessions
        self.hidden_sessions.add(sid)
        self.sessions.pop(sid, None)
        self.session_raw.pop(sid, None)
        self.console.pop(sid, None)
        self.files.pop(sid, None)
        self.processes.pop(sid, None)
        self.active_task.pop(sid, None)
        self.pending.pop(sid, None)
        self._last_session_publish.pop(sid, None)
        self.publish({"type": "session", "action": "remove", "id": sid})
        self._reparent_children(self.sessions, sid, self.publish)
        return existed

    def set_pivot_link(self, child: str, parent: Optional[str] = None,
                       disconnected: Optional[bool] = None) -> None:
        """更新枢轴拓扑。parent 为 None 表示不改父节点，disconnected 为 None 表示不改断开状态。"""
        child = (child or "").strip().lower()
        session = self.sessions.get(child)
        if not session:
            return
        changed = False
        if parent is not None:
            parent = parent.strip().lower()
            if session.get("pivot_parent") != parent:
                session["pivot_parent"] = parent
                changed = True
        if disconnected is not None and session.get("pivot_disconnected") != disconnected:
            session["pivot_disconnected"] = disconnected
            changed = True
        if changed:
            self.publish({"type": "session", "action": "update", "session": session})

    def touch_session_from_callback(self, demon: str, payload: dict) -> None:
        session = self.sessions.get(demon)
        if not session:
            return
        last = payload.get("Last", "")
        ts = parse_havoc_time(last) or now_ts()
        session["last_call"] = last or session.get("last_call", "")
        session["last_seen_ts"] = ts
        session["sleep"] = _as_int(payload.get("Sleep"), session.get("sleep", 0))
        session["jitter"] = _as_int(payload.get("Jitter"), session.get("jitter", 0))
        session["kill_date"] = _as_int(payload.get("KillDate"), session.get("kill_date", 0))
        session["working_hours"] = _as_int(payload.get("WorkingHours"), session.get("working_hours", 0))
        session["active"] = True
        session["health"] = self.compute_health(session)
        # 每秒都可能来，节流到 2 秒
        last_pub = self._last_session_publish.get(demon, 0)
        if now_ts() - last_pub >= 2:
            self._last_session_publish[demon] = now_ts()
            self.publish({"type": "session", "action": "update", "session": session})

    # ---------------- 监听器 ----------------

    def upsert_listener(self, info: dict) -> dict:
        previous = self.listeners.get(info.get("Name", ""), {})
        # 服务/插件监听器的 ListenerStart 回执把自定义字段放在 Info（JSON 字符串）里；
        # 原生 HTTP/SMB/External 没有这个字段。
        service_fields: dict = {}
        raw_service = info.get("Info")
        if isinstance(raw_service, str) and raw_service.strip():
            try:
                parsed = json.loads(raw_service)
                if isinstance(parsed, dict):
                    service_fields = parsed
            except Exception:
                service_fields = {}
        elif isinstance(raw_service, dict):
            service_fields = dict(raw_service)
        host_bind = (info.get("HostBind") or info.get("Host")
                     or service_fields.get("HostBind") or service_fields.get("Host") or "")
        port_bind = (info.get("PortBind") or info.get("Port")
                     or service_fields.get("PortBind") or service_fields.get("Port") or "")
        listener = {
            "name": info.get("Name", ""),
            "protocol": info.get("Protocol") or service_fields.get("Protocol", ""),
            "status": info.get("Status", ""),
            "host_bind": str(host_bind),
            "port_bind": str(port_bind),
            "port_conn": str(info.get("PortConn") or service_fields.get("PortConn") or ""),
            "hosts": info.get("Hosts", ""),
            "host_rotation": info.get("HostRotation", ""),
            "host_header": info.get("HostHeader", ""),
            "headers": info.get("Headers", ""),
            "uris": info.get("Uris", ""),
            "user_agent": info.get("UserAgent", ""),
            "secure": info.get("Secure", "false"),
            "proxy_enabled": info.get("Proxy Enabled", "false"),
            "proxy_type": info.get("Proxy Type", ""),
            "proxy_host": info.get("Proxy Host", ""),
            "proxy_port": info.get("Proxy Port", ""),
            "proxy_username": info.get("Proxy Username", ""),
            "proxy_password": info.get("Proxy Password", ""),
            "pipe_name": info.get("PipeName", ""),
            "endpoint": info.get("Endpoint", ""),
            "kill_date": info.get("KillDate", ""),
            "working_hours": info.get("WorkingHours", ""),
            "error": info.get("Error", ""),
            "service": bool(service_fields),
            "service_fields": service_fields,
        }
        if not listener["status"]:
            listener["status"] = previous.get("status") or "Online"
        if not listener["error"]:
            listener["error"] = previous.get("error", "")
        self.listeners[listener["name"]] = listener
        self.publish({"type": "listener", "action": "update", "listener": listener})
        return listener

    def find_service_listener(self, protocol: str) -> Optional[dict]:
        """按协议名查找服务端注册的自定义监听器（先精确、再忽略大小写）。"""
        name = str(protocol or "").strip()
        if not name:
            return None
        if name in self.service_listeners:
            return self.service_listeners[name]
        lowered = name.lower()
        for item in self.service_listeners.values():
            if str(item.get("name", "")).lower() == lowered:
                return item
        return None

    def register_service_listener(self, data: dict, pkg_time: Optional[float] = None) -> Optional[dict]:
        """处理 EV_SERVICE/SubEvent=2：服务端下发的自定义监听器协议定义。"""
        if not isinstance(data, dict):
            return None
        name = str(data.get("Name", "")).strip()
        if not name:
            return None
        items = []
        for raw in data.get("Items") or []:
            if not isinstance(raw, dict):
                continue
            field_name = str(raw.get("name", "")).strip()
            if not field_name:
                continue
            items.append({
                "name": field_name,
                "text": str(raw.get("text", "") or field_name),
                "placeholder": str(raw.get("placeholder", "") or ""),
                "object": str(raw.get("object", "input") or "input"),
                "required": _as_bool(raw.get("required", False)),
                "editable": _as_bool(raw.get("editable", True)),
            })
        listener = {
            "name": name,
            "agent": str(data.get("Agent", "") or ""),
            "items": items,
        }
        existed = name in self.service_listeners
        self.service_listeners[name] = listener
        self.publish({"type": "service_listener", "action": "update" if existed else "add",
                      "listener": listener})
        self.add_event(
            f"服务端注册自定义监听器协议：{name}"
            + (f"（Agent: {listener['agent']}，{len(items)} 个字段）" if items else ""),
            "info", pkg_time, source="service")
        return listener


    def remove_listener(self, name: str) -> None:
        if name in self.listeners:
            self.listeners.pop(name, None)
            self.publish({"type": "listener", "action": "remove", "name": name})

    # ---------------- 协议分发 ----------------

    def handle_package(self, pkg: dict) -> None:
        try:
            head = pkg.get("Head") or {}
            body = pkg.get("Body") or {}
            event = _as_int(head.get("Event"), 0)
            sub = _as_int(body.get("SubEvent"), 0)
            info = body.get("Info") or {}
            head_user = head.get("User") or ""
            pkg_time = parse_havoc_time(head.get("Time", ""))
            self.connection["last_message_ts"] = now_ts()

            if event == EV_INIT and sub == SUB_INIT_SUCCESS:
                self.connection["state"] = "connected"
                self.connection["detail"] = info.get("Message", "已连接")
                self.connection["since"] = now_ts()
                self.publish({"type": "connection", "connection": self.connection_view()})
                return

            if event == EV_INIT:
                # Event=1 SubEvent=5: teamserver profile（Demon 配置 + 本机 IP 列表）
                if sub == 5:
                    self.profile = info
                    self.publish({"type": "profile", "profile": info})
                return

            if event == EV_GATE and sub == 2:
                self._handle_gate(info, pkg_time)
                return

            if event == EV_SESSION:
                self._handle_session(sub, info, head_user, pkg_time)
            elif event == EV_LISTENER:
                self._handle_listener(sub, info, pkg_time)
            elif event == EV_CHAT:
                self._handle_chat(sub, info, head_user, pkg_time)
            elif event == EV_TEAMSERVER:
                self._handle_teamserver(sub, info, pkg_time)
            elif event == EV_SERVICE:
                self._handle_service(sub, info, pkg_time)
        except Exception as exc:  # 单条包解析失败不能拖垮网关
            self.add_event(f"解析数据包失败：{exc}", "error", source="gateway")

    def _handle_service(self, sub: int, info: dict, pkg_time: Optional[float]) -> None:
        if sub == 1:
            # 服务型 agent 注册（本环境用不到），仅记录
            name = ""
            try:
                agent = json.loads(info.get("Agent", "{}"))
                name = agent.get("Name", "")
            except Exception:
                pass
            if name:
                self.add_event(f"服务端 Agent 已注册：{name}", "info", pkg_time, source="service")
        elif sub == 2:
            # Service.ListenerRegister：Info["Listener"] 是 ListenerService.Json() 字符串
            raw = info.get("Listener", "")
            data: Any = raw
            if isinstance(raw, str):
                try:
                    data = json.loads(raw)
                except Exception:
                    data = None
            if isinstance(data, dict):
                self.register_service_listener(data, pkg_time)


    # ---------------- 载荷构建 ----------------

    def begin_payload_build(self, meta: dict) -> dict:
        self.payload_seq += 1
        build_id = f"{self.payload_seq:04d}"
        build = {
            "id": build_id,
            "status": "building",
            "started": now_ts(),
            "finished": None,
            "agent": meta.get("agent", "Demon"),
            "listener": meta.get("listener", ""),
            "arch": meta.get("arch", "x64"),
            "format": meta.get("format", "Windows Exe"),
            "logs": [],
            "filename": "",
            "path": "",
            "size": 0,
            "error": "",
        }
        self.payload_builds[build_id] = build
        while len(self.payload_builds) > 20:
            oldest = next(iter(self.payload_builds))
            if oldest == self.active_build:
                break
            self.payload_builds.pop(oldest, None)
        self.active_build = build_id
        self.publish({"type": "payload", "action": "start", "build": build})
        self.add_event(f"开始生成载荷：{build['format']} {build['arch']}（监听器 {build['listener']}）",
                       "info", source="payload")
        return build

    def _handle_gate(self, info: dict, pkg_time: Optional[float]) -> None:
        build_id = self.active_build
        build = self.payload_builds.get(build_id) if build_id else None
        if not build:
            return
        payload_b64 = info.get("PayloadArray") or ""
        if payload_b64:
            try:
                raw = base64.b64decode(payload_b64)
            except Exception as exc:
                build["status"] = "error"
                build["error"] = f"载荷解码失败：{exc}"
                self.publish({"type": "payload", "action": "error", "build": build})
                self.active_build = None
                return
            filename = info.get("FileName") or "demon.bin"
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
            target_dir = self.payload_dir / build_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / safe
            try:
                target.write_bytes(raw)
                if build.get("format") == "Linux Exe":
                    target.chmod(0o755)
            except OSError as exc:
                build["status"] = "error"
                build["error"] = f"载荷保存失败：{exc}"
                self.publish({"type": "payload", "action": "error", "build": build})
                self.active_build = None
                return
            build["status"] = "done"
            build["finished"] = now_ts()
            build["filename"] = safe
            build["path"] = str(target)
            build["size"] = len(raw)
            self.publish({"type": "payload", "action": "done", "build": build})
            self.add_event(f"载荷生成完成：{safe}（{len(raw)} 字节）", "good", pkg_time, source="payload")
            self.active_build = None
            return

        msg_type = info.get("MessageType")
        if msg_type:
            text = info.get("Message", "")
            build["logs"].append({"type": msg_type, "text": text, "time": local_time_str()})
            if len(build["logs"]) > 500:
                build["logs"] = build["logs"][-500:]
            self.publish({"type": "payload", "action": "log", "build": build,
                          "log": build["logs"][-1]})

    def fail_payload_build(self, reason: str) -> None:
        build_id = self.active_build
        build = self.payload_builds.get(build_id) if build_id else None
        if not build:
            return
        build["status"] = "error"
        build["error"] = reason
        build["finished"] = now_ts()
        self.publish({"type": "payload", "action": "error", "build": build})
        self.add_event(f"载荷生成失败：{reason}", "error", source="payload")
        self.active_build = None

    # ---------------- 会话 ----------------

    def _handle_session(self, sub: int, info: dict, head_user: str, pkg_time: Optional[float]) -> None:
        since = self.connection.get("since")
        # 登录成功后 teamserver 会重放历史事件（EventsList），这些帧的 Head.Time 早于本次连接，
        # 不能当作实时输出写进控制台，否则会重复/串台。
        is_history = bool(pkg_time and since and pkg_time < since - 3)
        if sub == SUB_SESSION_NEW:
            existed = info.get("NameID", "") in self.sessions
            session = self.upsert_session(info)
            if session is not None and not existed:
                self.add_event(
                    f"会话上线：{session['id']} · {session['user']}@{session['host']}"
                    f"（{session['internal_ip']} / {session['os']}）",
                    "good", pkg_time, source="session")
                if self.new_session_hook and not is_history:
                    try:
                        self.new_session_hook(session["id"])
                    except Exception:
                        pass
        elif sub == SUB_SESSION_REMOVE:
            sid = info.get("NameID") or info.get("DemonID") or info.get("AgentID") or ""
            if sid:
                # agent 真正退出/下线：解除本地隐藏状态，允许下次上线重新显示。
                self.hidden_sessions.discard(sid)
                self.remove_session(sid)
                self.add_event(f"会话下线：{sid}", "error", pkg_time, source="session")
        elif sub == SUB_SESSION_MARK:
            sid = info.get("AgentID", "")
            marked = info.get("Marked", "")
            session = self.sessions.get(sid)
            if session:
                session["active"] = marked == "Alive"
                session["health"] = self.compute_health(session)
                self.publish({"type": "session", "action": "update", "session": session})
            if marked == "Dead" and sid:
                self.add_event(f"会话失联：{sid}", "warn", pkg_time, source="session")
        elif sub == SUB_SESSION_SEND:
            if is_history:
                return
            # teamserver 的 EventBroadcast 对命令帧按用户名排除（实际按 ClientID），
            # 会把网关自己发出的命令再回传一次；本地已记录，直接忽略。
            if head_user and head_user == self.connection.get("user"):
                return
            # 其他操作员（Qt 客户端）发出的命令
            demon = info.get("DemonID", "")
            command = info.get("CommandLine", "")
            if demon and command and head_user:
                self.console_append(demon, "prompt", command)
                self.add_event(f"[{head_user}] 在 {demon} 执行：{command}", "info", pkg_time, source="operator")
        elif sub == SUB_SESSION_RECEIVE:
            if is_history:
                return
            self._handle_output(info, pkg_time)

    def touch_session_activity(self, demon: str, ts: Optional[float] = None) -> None:
        """任意任务输出都视为会话活动。

        SMB/命名管道 pivot 子会话的心跳由父 Demon 转发，teamserver 不会为它
        单独发 callback 包；如果不在这里刷新，UI 会错误显示 unresponsive。
        """
        session = self.sessions.get(demon)
        if not session:
            return
        ts = ts or now_ts()
        session["last_seen_ts"] = max(float(session.get("last_seen_ts") or 0), ts)
        session["last_call"] = time.strftime("%d-%m-%Y %H:%M:%S", time.localtime(ts))
        session["active"] = True
        health = self.compute_health(session)
        if health != session.get("health"):
            session["health"] = health
            self.publish({"type": "session", "action": "update", "session": session})

    def _handle_output(self, info: dict, pkg_time: Optional[float]) -> None:
        demon = info.get("DemonID", "")
        if not demon:
            return
        self.touch_session_activity(demon, pkg_time)
        command_id = str(info.get("CommandID", ""))
        raw = info.get("Output", "") or ""
        try:
            decoded = base64.b64decode(raw) if raw else b""
        except Exception:
            decoded = b""

        if command_id == str(CMD_CALLBACK):
            try:
                payload = json.loads(decoded.decode("utf-8", "replace"))
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                self.touch_session_from_callback(demon, payload)
            return

        if command_id == "129":  # BOF_CALLBACK
            try:
                payload = json.loads(decoded.decode("utf-8", "replace"))
            except Exception:
                payload = {}
            task_id = str(payload.get("TaskID", ""))
            if task_id:
                self.python_bof_results[task_id] = {
                    "worked": payload.get("Worked", ""),
                    "output": payload.get("Output", ""),
                    "error": payload.get("Error", ""),
                    "ts": now_ts(),
                }
                while len(self.python_bof_results) > 200:
                    self.python_bof_results.pop(next(iter(self.python_bof_results)), None)
                self.publish({"type": "bof_result", "task_id": task_id})
            return

        if command_id == str(CMD_CONSOLE):
            try:
                message = json.loads(decoded.decode("utf-8", "replace"))
            except Exception:
                message = {}
            if not isinstance(message, dict):
                return
            text = message.get("Message", "") or ""
            output = message.get("Output", "") or ""
            mtype = (message.get("Type") or "").lower()
            kind = "info"
            if mtype in ("good",):
                kind = "good"
            elif mtype in ("error", "erro"):
                kind = "error"
            elif mtype in ("warning", "warn"):
                kind = "warn"

            if text:
                low = text.lower()
                if low.startswith("send task to agent"):
                    self.mark_task_sent(demon)
                    self.console_append(demon, "info", text)
                elif low.startswith("failed to create task"):
                    self.fail_active_task(demon, text)
                    self.console_append(demon, "error", text)
                elif low.startswith("finished download of file:"):
                    self.console_append(demon, "good", text)
                    remote = text.split(":", 1)[1].strip()
                    self._finish_download(demon, remote, pkg_time)
                elif low.startswith("download has been removed:"):
                    self.console_append(demon, "info", text)
                    pending = self.pending_downloads.get(demon) or []
                    if pending:
                        pending.pop(0)
                    if not pending:
                        self.pending_downloads.pop(demon, None)
                elif low.startswith("received output"):
                    self.console_append(demon, "info", text)
                else:
                    self.console_append(demon, kind, text)

            if output:
                self.console_append(demon, "output", output)
                self.append_task_output(demon, output)

            misc = message.get("MiscType") or ""
            if misc:
                self._handle_misc(demon, misc, message, pkg_time)
            return

        # 其它 CommandID（FS / PROC / PROC_LIST / SCREENSHOT ...）统一按文本处理
        text = decoded.decode("utf-8", "replace") if decoded else ""
        if text:
            self.console_append(demon, "output", text)
            self.append_task_output(demon, text)

    def _handle_misc(self, demon: str, misc: str, message: dict, pkg_time: Optional[float]) -> None:
        if misc == "FileExplorer":
            try:
                data = base64.b64decode(message.get("MiscData", "")).decode("utf-8", "replace")
                parsed = json.loads(data)
                path = base64.b64decode(parsed.get("Path") or "").decode("utf-8", "replace")
                # Windows: "C:\dir\*"；Linux: "/dir/*"（Go agent 直接回 pattern）
                if path.endswith("\\*"):
                    path = path[:-2]
                elif path.endswith("/*"):
                    path = path[:-2] or "/"
                entries = []
                for item in (parsed.get("Files") or []):
                    if not isinstance(item, dict):
                        continue
                    entries.append({
                        "type": item.get("Type") or "file",
                        "name": item.get("Name", ""),
                        "size": item.get("Size", ""),
                        "modified": item.get("Modified", ""),
                    })
                self.files[demon] = {"path": path, "entries": entries, "ts": now_ts()}
                self.publish({"type": "fileexplorer", "demon": demon, "path": path, "entries": entries})
            except Exception as exc:
                self.add_event(f"文件列表解析失败：{exc}", "error", source="gateway")
        elif misc == "ProcessUI":
            try:
                data = base64.b64decode(message.get("MiscData", "")).decode("utf-8", "replace")
                parsed = json.loads(data)
                rows = []
                for proc in parsed if isinstance(parsed, list) else []:
                    if not isinstance(proc, dict):
                        continue
                    rows.append({
                        "name": proc.get("Name", ""),
                        "pid": str(proc.get("PID", "")),
                        "ppid": str(proc.get("PPID", "")),
                        "session": str(proc.get("Session", "")),
                        "arch": "x86" if proc.get("IsWow") else "x64",
                        "user": proc.get("User", ""),
                    })
                self.processes[demon] = {"entries": rows, "ts": now_ts()}
                self.publish({"type": "processui", "demon": demon, "entries": rows})
            except Exception as exc:
                self.add_event(f"进程列表解析失败：{exc}", "error", source="gateway")
        elif misc == "screenshot":
            name = message.get("MiscData2", "") or f"screenshot-{int(now_ts())}.png"
            self._save_loot_file(demon, "screenshot", name, message.get("MiscData", ""), pkg_time)
            self.console_append(demon, "info", f"收到截图：{name}")
        elif misc == "download":
            info = message.get("MiscData2", "") or ""
            parts = info.split(";")
            try:
                name = base64.b64decode(parts[0]).decode("utf-8", "replace") if parts[0] else "unknown"
            except Exception:
                name = parts[0] or "unknown"
            size = parts[1] if len(parts) > 1 else ""
            self.pending_downloads.setdefault(demon, []).append(
                {"name": name, "size": size, "ts": now_ts()})
            self.publish({"type": "download", "action": "start", "demon": demon,
                          "name": name, "size": size})
            self.console_append(demon, "info", f"下载任务：{name}（{size} 字节）")
        elif misc == "disconnect":
            # teamserver: MiscData = "<child agent id>"，对应 Qt GraphPivotNodeDisconnect
            self.set_pivot_link(message.get("MiscData", ""), disconnected=True)
        elif misc == "reconnect":
            # teamserver: MiscData = "<parent agent id>;<child agent id>"，对应 Qt GraphPivotNodeReconnect
            parts = (message.get("MiscData") or "").split(";")
            if len(parts) == 2:
                self.set_pivot_link(parts[1], parent=parts[0], disconnected=False)

    def _finish_download(self, demon: str, remote_path: str,
                         pkg_time: Optional[float] = None) -> None:
        """teamserver 已把文件写到 havoc-src/data/loot/.../Download/，复制到 Web 战利品目录。"""
        queue = self.pending_downloads.get(demon) or []
        remote_base = remote_path.replace("\\", "/").rsplit("/", 1)[-1]
        pending = None
        for item in list(queue):
            if item.get("name", "").replace("\\", "/").rsplit("/", 1)[-1] == remote_base:
                pending = item
                queue.remove(item)
                break
        if pending is None and queue:
            pending = queue.pop(0)
        if not queue:
            self.pending_downloads.pop(demon, None)
        pending = pending or {}
        name = pending.get("name") or remote_base
        source = self._find_teamserver_download(demon, remote_path)
        if source is None:
            self.console_append(demon, "warn", f"下载完成，但未找到落地文件：{remote_path}")
            return
        target_name = name.replace("\\", "/").rsplit("/", 1)[-1] or "download.bin"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", target_name) or "download.bin"
        target_dir = self.loot_dir / re.sub(r"[^A-Za-z0-9._-]+", "_", demon)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe
        if target.exists():
            target = target_dir / f"{int(now_ts())}-{safe}"
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            self.console_append(demon, "error", f"下载文件复制失败：{exc}")
            return
        size = target.stat().st_size
        self._register_loot(demon, "download", name, str(size), str(target), pkg_time)
        self.publish({"type": "download", "action": "done", "demon": demon,
                      "name": name, "size": size, "path": str(target)})
        self.console_append(demon, "good", f"下载完成并保存：{name}（{size} 字节）")

    def _find_teamserver_download(self, demon: str, remote_path: str) -> Optional[Path]:
        root = self.teamserver_loot_dir
        if not root.is_dir():
            return None
        rel = remote_path.replace("\\", "/").strip().lstrip("/")
        basename = rel.rsplit("/", 1)[-1]
        for download_dir in root.glob(f"*/agents/{demon}/Download"):
            for candidate in (download_dir / rel, download_dir / basename):
                try:
                    if candidate.is_file():
                        return candidate
                except OSError:
                    continue
            try:
                for candidate in download_dir.rglob(basename):
                    if candidate.is_file():
                        return candidate
            except OSError:
                continue
        return None

    def _save_loot_file(self, demon: str, kind: str, name: str, b64data: str,
                        pkg_time: Optional[float]) -> None:
        try:
            raw = base64.b64decode(b64data)
        except Exception:
            self.add_event(f"战利品数据解码失败：{name}", "error", source="loot")
            return
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or f"{kind}-{int(now_ts())}"
        target_dir = self.loot_dir / re.sub(r"[^A-Za-z0-9._-]+", "_", demon)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe
        if target.exists():
            target = target_dir / f"{int(now_ts())}-{safe}"
        try:
            target.write_bytes(raw)
        except OSError as exc:
            self.add_event(f"战利品保存失败：{exc}", "error", source="loot")
            return
        self._register_loot(demon, kind, name, str(len(raw)), str(target), pkg_time)

    def _register_loot(self, demon: str, kind: str, name: str, size: str,
                       path: Optional[str], pkg_time: Optional[float]) -> None:
        self.loot_seq += 1
        item = {
            "id": self.loot_seq,
            "kind": kind,
            "demon": demon,
            "name": name,
            "size": size,
            "path": path,
            "ts": pkg_time or now_ts(),
            "time": local_time_str(pkg_time),
        }
        self.loot.append(item)
        self.publish({"type": "loot", "action": "add", "item": item})
        self.add_event(f"战利品（{kind}）：{demon} · {name}", "info", pkg_time, source="loot")

    def _handle_listener(self, sub: int, info: dict, pkg_time: Optional[float]) -> None:
        if sub == SUB_LISTENER_ADD:
            existed = info.get("Name", "") in self.listeners
            listener = self.upsert_listener(info)
            if not existed:
                self.add_event(
                    f"监听器上线：{listener['name']}（{listener['protocol']} "
                    f"{listener['host_bind']}:{listener['port_bind']}）",
                    "good", pkg_time, source="listener")
        elif sub == SUB_LISTENER_EDIT:
            listener = self.upsert_listener(info)
            self.add_event(f"监听器更新：{listener['name']}（{listener['status']}）",
                           "info", pkg_time, source="listener")
        elif sub == SUB_LISTENER_REMOVE:
            name = info.get("Name", "")
            self.remove_listener(name)
            self.add_event(f"监听器移除：{name}", "warn", pkg_time, source="listener")
        elif sub == SUB_LISTENER_ERROR:
            name = info.get("Name", "")
            error = info.get("Error", "")
            listener = self.listeners.get(name)
            if listener:
                listener["status"] = "Offline"
                listener["error"] = error
                self.publish({"type": "listener", "action": "update", "listener": listener})
            self.add_event(f"监听器错误：{name} - {error}", "error", pkg_time, source="listener")

    def _handle_chat(self, sub: int, info: dict, head_user: str, pkg_time: Optional[float]) -> None:
        if sub == SUB_CHAT_USER:
            self.add_event(f"{info.get('User', '')} 已连接到团队服务器", "good", pkg_time, source="chat")
        elif sub == SUB_CHAT_USER_DISCONNECT:
            self.add_event(f"{info.get('User', '')} 已断开与团队服务器的连接", "error", pkg_time, source="chat")
        elif sub == SUB_CHAT_SESSION:
            self.add_event(f"新会话（聊天）：{info.get('Name', '')}", "good", pkg_time, source="chat")
        elif sub == SUB_CHAT_LISTENER:
            self.add_event(f"新监听器（聊天）：{info.get('Name', '')}", "info", pkg_time, source="chat")
        elif sub == SUB_CHAT_MESSAGE:
            # Info 形如 { "<user>": "<base64 text>" }
            for user, encoded in info.items():
                text = ""
                try:
                    text = base64.b64decode(str(encoded)).decode("utf-8", "replace")
                except Exception:
                    text = str(encoded)
                if not text:
                    continue
                self.chat_seq += 1
                message = {
                    "id": self.chat_seq,
                    "ts": pkg_time or now_ts(),
                    "time": time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(pkg_time or now_ts())),
                    "user": user,
                    "text": text,
                    "self": user == self.connection.get("user"),
                }
                self.chat.append(message)
                self.publish({"type": "chat", "message": message})

    def _handle_teamserver(self, sub: int, info: dict, pkg_time: Optional[float]) -> None:
        if sub == SUB_TS_LOG:
            self.parse_logger_line(info.get("Text", ""), pkg_time)
        elif sub == SUB_TS_PROFILE:
            self.profile = info

    # ---------------- 快照 ----------------

    def connection_view(self) -> dict:
        view = dict(self.connection)
        view["uptime"] = now_ts() - self.started_at
        return view

    def snapshot(self) -> dict:
        # 保持到达顺序（与 Qt 客户端 SessionTable/SessionGraph 的插入顺序一致），
        # 不要按字段排序，否则拓扑图的兄弟节点顺序会与客户端不同。
        sessions = list(self.sessions.values())
        return {
            "type": "snapshot",
            "server_time": now_ts(),
            "connection": self.connection_view(),
            "sessions": sessions,
            "listeners": list(self.listeners.values()),
            "service_listeners": list(self.service_listeners.values()),
            "events": list(self.events)[-400:],
            "chat": list(self.chat)[-200:],
            "loot": list(self.loot)[-100:],
            "profile": self.profile,
            "payloads": list(self.payload_builds.values())[-10:],
            "python_commands": list(self.python_commands.values()),
            "counts": self.counts(),
        }

    def counts(self) -> dict:
        healthy = sum(1 for s in self.sessions.values() if s.get("health") == "healthy")
        return {
            "sessions": len(self.sessions),
            "healthy": healthy,
            "listeners": len(self.listeners),
            "service_listeners": len(self.service_listeners),
            "events": len(self.events),
        }
