# Havoc Web 控制台

Havoc Framework 的浏览器控制台实现：用浏览器替代 Qt 客户端，复用 Teamserver 的 WSS 协议，
在不修改 Teamserver / Demon 的前提下提供会话管理、命令执行、文件管理、载荷生成、监听器、
会话拓扑、战利品、扩展商店、Python 脚本与 MCP 外部调用接口。

上游项目：[HavocFramework/Havoc](https://github.com/HavocFramework/Havoc)（部署版本 **0.7 / Bites The Dust**）。

![Web 控制台主界面](webui/docs/main.png)

| 模块 | 能力 |
| --- | --- |
| 会话 | 会话表、控制台、右键菜单、拓扑图、标记 Alive/Dead |
| 文件 | 目录树 + 列表、上传/下载、在线编辑、重命名/复制/移动、递归删除 |
| 进程 | PPID 进程树 + 进程表、结束进程 |
| 监听器 | HTTP/HTTPS/SMB/External + TCP/KCP/UDP/WS/DNS/DoH/DoT/OSS 加密传输 + 服务端插件监听器 |
| 载荷 | 与 Qt 客户端同款的生成对话框，Teamserver 侧交叉编译 exe/dll/shellcode/service，新增 Linux Exe |
| 其它 | 团队聊天、事件查看器、战利品、扩展商店、Python 脚本 / 持久 REPL、团队服务器日志 |
| 集成 | 内置 MCP 服务（69 个工具 / 15 个资源 / 3 个提示词），stdio 与 HTTP 两种接入方式 |

---

## 1. 功能截图

> **说明：本章截图全部来自本仓库的 Web 控制台（浏览器界面），不是 Qt 客户端。**
> 两者 UI 为刻意对齐而风格接近，但属于两个独立程序；Qt 客户端截图位于 `logs/client-shots/`，
> 该目录不纳入版本库。

### 1.1 会话总览

左侧为会话表（ID / 外部地址 / 内部地址 / 用户 / 计算机 / 系统 / 进程 / PID / 最后心跳 / 状态），
右侧 dock 为事件查看器，底部可开团队聊天、监听器、扩展商店、脚本、Python 控制台等标签页。

![会话总览](webui/docs/main.png)

### 1.2 会话控制台

双击会话进入控制台；`help` 为 Demon 命令表，Linux 会话会标注「仅 Windows」命令，
底部状态行显示 `[用户/计算机] 进程/PID 架构`。

![会话控制台](webui/docs/console.png)

### 1.3 文件管理器

每个会话独立标签，左侧目录树、右侧列表（名称 / 大小 / 修改时间），支持新建、上传（多选/拖拽）、
下载、重命名、复制、移动、删除与文本文件在线编辑，Windows / Linux 共用同一套界面。

![文件管理器](webui/docs/files.png)

### 1.4 进程列表

左侧为 PPID 进程树，右侧为进程表（名称 / PID / PPID / 会话 / 架构 / 用户），当前进程红色标注，
右键可结束进程。

![进程列表](webui/docs/procs.png)

### 1.5 会话拓扑图

`视图 → 会话视图 → 拓扑图`：防火墙图标为 Teamserver 根节点，按 PivotParent 渲染树形结构，
直属会话绿线、枢轴蓝线、断开的枢轴红线；支持滚轮缩放、拖拽平移，点击节点进入控制台。

![会话拓扑图](webui/docs/graph.png)

### 1.6 载荷生成

`攻击 → 载荷生成`：选择 Agent、监听器、架构与格式，配置 Sleep/Jitter、间接系统调用、栈欺骗、
Sleep 技术、代理加载、AMSI/ETW Patch、注入方式等；下方构建控制台实时显示 Teamserver 的交叉编译日志，
生成完成后可直接下载；产物保存在 `webui/data/payloads/<id>/`，网关重启后仍可下载。

![载荷生成](webui/docs/payload.png)

### 1.7 监听器

原生 HTTP/HTTPS/SMB/External 与八种加密传输监听器统一管理，可查看连接数、启停、编辑、删除、
下载 Go 中继器、发起一键上线。SMB 仅需填写管道名，External 在 40056 上注册 POST 端点。

![监听器](webui/docs/listeners.png)

![加密传输监听器](webui/docs/transport-listeners.png)

### 1.8 战利品

按 Agent 与类型查看截图、下载文件，支持大图预览；截图与文件落盘，网关重启后恢复。

![战利品](webui/docs/loot.png)

### 1.9 扩展商店与 Python

扩展商店拉取 `p4p1/havoc-store` 目录（GitHub 失败时走镜像与本地回退），支持 gist / 仓库安装；
Python 页可查看源码、加载脚本并注册命令，Python 控制台为持久 REPL，SDK 兼容
`Demon` / `RegisterCommand` / BOF 回调。

![扩展商店](webui/docs/store.png)

![Python 控制台](webui/docs/pyconsole.png)

### 1.10 登录、聊天与右键菜单

![登录页](webui/docs/login.png)

![团队聊天](webui/docs/chat.png)

![会话右键菜单](webui/docs/session-menu.png)

> 全部截图位于 `webui/docs/`，内容均为 Web 控制台界面。与 Qt 客户端的逐屏对照图见
> `webui/docs/ui-compare-*.png`（每张图上方标注 `WEB`、下方标注 `CLIENT`，上为 Web 控制台、
> 下为 Qt 客户端）；`ui-parity-sheet.png` 为 13 屏功能总览拼图（同为 Web 控制台）。

---

## 2. 快速开始

### 2.1 前置条件

本仓库只跟踪 Web 控制台、实验脚本与启停脚本；以下内容体积大或为独立仓库，**未纳入版本库**，需自行准备：

| 组件 | 位置 | 说明 |
| --- | --- | --- |
| Havoc 源码与二进制 | `havoc-src/` | 独立 git 仓库，需编译出 `havoc-src/havoc`（Teamserver）与 `client/Havoc`（Qt 客户端） |
| 本地工具链 | `toolchain/` | Go、CMake、Qt5、Python venv，仅在重新编译时需要 |
| 运行数据 | `webui/data/`、`webui/run/` | 战利品、载荷、MCP 令牌与密钥，首次启动自动生成 |
| 载荷产物 | `payloads-out/` | 一键上线投递目录 |

系统要求：Linux（Kali 验证）、Python 3.11+、`aiohttp`（见 `webui/requirements.txt`）。

### 2.2 一键启动

```bash
cd /home/kali/桌面/Havoc
./start-all.sh              # 后台启动 Teamserver + Web 控制台 + 暂存服务
./start-all.sh --status     # 查看三个服务的运行状态（全部运行返回 0）
./stop-all.sh               # 全部停止
```

- 脚本幂等：已在运行的服务直接复用，不会重复拉起或抢占端口；
- 就绪检查：等待 Teamserver 端口 `40056` 监听、Web `/api/health` 通过后才报告成功；
- 按需跳过：`--no-webui`、`--no-stager`、`--no-teamserver`；
- 日志：`logs/teamserver.log`、`logs/webui.log`、`logs/webui-console.log`、`logs/stager.log`。

### 2.3 分别启动（调试）

```bash
./start-teamserver.sh       # 前台运行 Teamserver，Ctrl+C 停止
./start-client.sh           # 启动 Qt 客户端（可选）
cd webui && ./start-webui.sh  # 后台启动 Web 控制台（含健康检查）
```

| 服务 | 地址 | 凭据 |
| --- | --- | --- |
| Web 控制台 | <http://127.0.0.1:8081/> | `admin` / `havoc1234`（`webui/config.json`） |
| Teamserver | `wss://127.0.0.1:40056` | Qt 客户端 `5pider` / `password1234` |
| 一键上线服务 | <http://127.0.0.1:9901/> | 无 |

> 用户名区分大小写；网关连接 Teamserver 使用 `Neo`，Qt 客户端使用 `5pider`，两边可同时在线不互踢。
> **默认口令仅用于本地演示，正式使用前请修改** `havoc-src/profiles/havoc.yaotl` 与 `webui/config.json`。

### 2.4 基本使用流程

1. **登录** Web 控制台，主界面出现会话表与事件查看器。
2. **新建监听器**：`视图 → 监听器 → 添加`，选择模式并保存；加密传输监听器还需「下载中继」并在目标侧运行。
3. **生成载荷**：`攻击 → 载荷生成`，选择在线监听器与格式，点击「生成」，完成后下载。
4. **上线与会话操作**：会话出现后双击打开控制台执行命令；右键可打开文件浏览器、进程列表、标记 Alive/Dead。
5. **一键上线**：`攻击 → 一键上线`使用 9901 的 `/slt`（Linux）与 `/swt`（Windows）投递脚本。

---

## 3. MCP 外部调用

网关内置 MCP 服务，把 Web 的全部能力暴露给外部程序（共 69 个工具）：

```bash
# HTTP（Streamable HTTP）
curl -s http://127.0.0.1:8081/mcp \
  -H "Authorization: Bearer $(cat webui/run/mcp.token)" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"havoc_list_sessions","arguments":{}}}'

# stdio（由本机 MCP 客户端拉起）
python3 webui/mcp_server.py --stdio

# 查看工具清单
python3 webui/mcp_server.py --list-tools
```

- 令牌：首次启动生成 `webui/run/mcp.token`（0600），也可在 `webui/config.json → mcp.token` 固定；
- 管理页：菜单 `MCP → MCP 管理`，可查看运行态、启停 `/mcp`、复制或轮换令牌、修改监听端口（无需重启网关）；
- 独立端口只提供 `/mcp`；停止后所有 `/mcp` 请求返回 503，浏览器 REST/WS 与投递链不受影响；
- 令牌等同操作员权限，请勿外泄。完整工具表见 [webui/README.md](webui/README.md) 第 10 节。

---

## 4. 目录结构

```
Havoc/
├── webui/                     # Web 控制台（本仓库主体）
│   ├── gateway.py             # aiohttp 网关：登录 / REST / WS / 静态页 / MCP 挂载
│   ├── havoc_client.py        # Teamserver WSS 客户端（自动重连）
│   ├── state.py               # 协议解码与状态缓存
│   ├── commands.py            # 命令构造与命令矩阵
│   ├── demon_commands.py      # Demon 命令定义与帮助
│   ├── transports.py          # 八种加密传输监听器
│   ├── linux_compat.py        # Linux 会话命令改写与 help 标注
│   ├── mcp_server.py          # MCP stdio 入口
│   ├── mcp_tools.py           # 69 工具 / 15 资源 / 3 提示词注册表
│   ├── filemanager.py         # 文件管理器后端
│   ├── static/                # 前端（深色主题，复刻 Qt 客户端）
│   ├── docs/                  # 设计文档、协议说明与界面截图
│   └── *_test.py              # 验收与回归脚本
├── stager/stager_server.py    # 9901 一键上线暂存服务
├── lab/                       # 受控实验环境（execd、交付链验证产物）
├── start-all.sh               # 一键启动（幂等 + 就绪检查）
├── stop-all.sh                # 一键停止
├── start-teamserver.sh        # 单独启动 Teamserver
├── start-client.sh            # 启动 Qt 客户端
├── start-stager.sh            # 单独启动暂存服务
├── README-DEPLOY.md           # 完整部署记录（编译、兼容性、验证结果）
└── HANDOFF-*.md               # 开发交接说明
```

版本库不包含：`havoc-src/`、`toolchain/`、`logs/`、`payloads-out/`、`webui/data/`、`webui/run/`、
编译产物与缓存，规则见 [.gitignore](.gitignore)。

---

## 5. 测试与验收

```bash
cd webui

python3 acceptance.py          # 端到端验收：登录 / 会话 / shell / 审计 / 重连 / 9901
python3 command_test.py        # 命令矩阵与协议构造
python3 ui_feature_test.py     # CDP 无头浏览器 UI 回归
python3 mcp_test.py            # MCP 协议与工具（--live 追加在线只读检查）
python3 mcp_admin_test.py      # MCP 管理页
python3 service_listener_test.py
python3 linux_agent_test.py
python3 transport_test.py
```

| 项目 | 结果 |
| --- | --- |
| `acceptance.py` | 15 / 15 |
| `ui_feature_test.py` | 15 / 15 |
| `command_test.py` | 34 / 34 |
| `mcp_test.py` | 离线 40 项；`--live` 47 项 |
| `mcp_admin_test.py` | 92 项 |
| `service_listener_test.py` | 25 / 25 |
| `linux_agent_test.py` | 20 / 20 |

> 标注「验收」的脚本需要 Teamserver 与 Web 控制台在运行，部分用例依赖 `/etc/hosts` 或本机网络环境。

---

## 6. 已知限制

- **Linux Demon**：不支持 SMB pivot、令牌窃取、注入、BOF、.NET、WMI、Kerberos 等 Windows 专有语义，
  `help` 中对应命令标注「仅 Windows」；二进制约 6 MB，未做 UPX 与休眠混淆。
- **服务端插件监听器**：不写入 Teamserver 数据库，删除不通知插件；创建时若插件未注册会返回 502。
- **扩展商店**：图形化（havocui）扩展可安装查看，但运行仍需 Qt 客户端。
- **拓扑图**：若 Qt 客户端长期在线，可能因错过 `NewSession` 只显示部分会话，重连即可对齐。
- **安全**：网关默认监听 `0.0.0.0:8081` 仅为方便同网段访问，**不要暴露到办公网或公网**；
  需要加密时配置 `web.tls_cert` / `web.tls_key`。

---

## 7. 更多文档

| 文档 | 内容 |
| --- | --- |
| [README-DEPLOY.md](README-DEPLOY.md) | 编译、工具链兼容、中文二开、一键上线、Linux Demon 全量验证 |
| [webui/README.md](webui/README.md) | 架构、配置、REST/WS 接口、Python SDK、MCP 工具表、限制 |
| [webui/docs/TRANSPORTS.md](webui/docs/TRANSPORTS.md) | 加密传输协议、字段与模式边界 |
| [webui/docs/command-matrix.md](webui/docs/command-matrix.md) | 命令矩阵与平台差异 |
| [HANDOFF-WEBUI.md](HANDOFF-WEBUI.md) | Web 控制台开发交接 |
| [lab/README.md](lab/README.md) | 实验环境搭建与交付链验证 |

---

> 本项目仅供**已获得明确书面授权的安全测试**与自有环境研究使用。请遵守当地法律法规，
> 不得用于未授权的目标。
