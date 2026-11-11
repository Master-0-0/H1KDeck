# 交接：Havoc Web 控制台 —— 回归问题修复与测试加固

> 给下一个会话的 AI/开发者。工作目录 `/home/kali/桌面/Havoc`。**仅用于已获得明确书面授权的测试环境。**
> 上一轮已完成「移除 Qt 客户端」，本轮不要再动那部分；目标是把全面回归暴露的 4 个问题修掉，让测试全绿。

---

## 0. 一句话目标

在不重启 teamserver、不改动 Demon/协议的前提下，修复下列 4 个问题（1 个陈旧测试、1 个测试选会话、1 个 MCP 完成判定、1 个 Linux 兼容层缺口），并让 18 个套件 + 2 个平台命令矩阵保持/恢复全绿。

---

## 1. 现状（已验证，不要重做）

- **Qt 客户端已移除**：`havoc-src/client/` 只剩 `Modules/`（Python 模块 + 156 个 BOF `.o`）。**这是 Web 端 77 条模块命令的运行时来源，绝对不能删。**
- 移除前的完整备份：`backup/qt-client-20261111.tar.gz`（含改动过的 QSS/中文界面源码）；Qt5 前缀可随时用 `toolchain/debs/*.deb` 重新解包。
- `havoc-src` 已有本地提交 `362146f`（删 client 本体、改 makefile、删 `havoc client` 子命令、profile 只留 `Neo`）。**不要再回滚或重加 `5pider`。**
- 命令表来源固化在 `webui/reference/`（`Commands.cc`、`client-config.toml`），生成器：`python3 webui/tools/gen_demon_commands.py`。help 表口径：**108 行 = 35 静态 + 73 模块**；`REGISTERED_COMMANDS` 定义 77 条（5 条子命令不单列，`jump-exec` 模块组单列一行）。
- 服务与凭据（当前在线）：

| 项 | 值 |
|---|---|
| teamserver | pid `1979989`，`wss://0.0.0.0:40056`，**跑的是替换前的 inode，尽量不要重启** |
| webui | `http://127.0.0.1:8081`，`admin` / `havoc1234`（`webui/config.json`） |
| teamserver 操作员 | 仅 `Neo` / `password1234` |
| stager | `http://127.0.0.1:9901` |
| 会话 | 18 个（13 healthy，含 5 个 Windows / 多个 Linux），会随 lab 变化 |
| 监听器 | `LAN-Test`(HTTP 8080) / `Hop2` / `Hop3` / `HopKeep` |
| 健康靶机会话 | Windows `2c67a896`，Linux `791a44ae`（测试优先用这两个） |
| CDP | `http://127.0.0.1:9222`（headless Chromium 已就绪，UI 测试直接可用） |

- 本轮回归结论：**409 项断言通过**；命令矩阵 Linux/Windows 各 112 条实测；webui 日志零新增错误。唯一 FAIL 是 `disconnect_test.py`（见 P0-1）。

---

## 2. 问题清单（按优先级，含定位与验收）

### P0-1 `disconnect_test.py` 断言了不存在的 DOM（当前 FAIL）

- 位置：`webui/disconnect_test.py:19-25`（`state()` 读 `#conn-text`/`#conn-pill`/`#status-ws`/`#status-conn`/`#console-input`）、`:75-78`（`ok` 判断）。
- 证据：这些 id **在本仓库 `index.html`（commit `5543138` 起）里从未存在**；UI 复刻 Qt 时按客户端设计去掉了底部状态栏（`app.js:506-515` 的 `updateConn()` 只更新用户名）。
- 现状替代观测：停网关时页面 `fetch('/api/health')` 失败；重启后会话表恢复、`/api/me` 200、cookie 仍有效、不弹登录（已用临时探针验证通过）。
- 做法（二选一）：
  1. 重写 `state()`/`ok`：不用状态栏 id，改用 `fetch('/api/health')`、`fetch('/api/me')`、`#sessions-body tr` 行数、`#login-view` 是否隐藏；
  2. 删除该测试，在 `webui/README.md`「验收记录」注明「断线 UX 已由 reconnect_test + acceptance 覆盖」。
- 验收：`python3 disconnect_test.py` → PASS，且不新增依赖 DOM 的断言。

### P0-2 MCP `havoc_run_command` 的完成判定不可靠

- 位置：`webui/mcp_tools.py:486-520`（handler 轮询 `/api/sessions/{sid}/tasks`，只看 `item["completed"]`）。
- 现象：命令实际执行成功（控制台有回显，如 `mcp-probe`），但工具返回 `completed=false`、`output=""`。
- 根因：Console 回包不带 TaskID，任务归因是启发式的（`webui/README.md` 已知限制 #3）；`shell`/模块命令常停在 `status=sent`。
- 做法建议：
  1. `wait` 循环里同时轮询 `/api/sessions/{sid}/console`，记录发起时刻后的新行；
  2. 若 task 未 `completed` 但已有新 console 输出，以 console 文本兜底并标记 `completed=true`（或新增 `completed_by="console"`）；
  3. 在工具描述和 README 注明该语义。
- 验收：MCP 调 `havoc_run_command`（`sid=2c67a896`, `command="shell echo probe"`, `wait_seconds=15`）返回 `completed=true` 且 `output` 含 `probe`；`mcp_test.py --live` 不回归（47/47）。

### P1-3 `file_manager_test.py` 会话选择偶发 504

- 位置：`webui/file_manager_test.py:84-100`（`pick_sessions` 只按 `health != healthy` 过滤）。
- 现象：默认选中 `health=healthy` 但心跳滞后的 `6fb41d56` → 连续 504/超时（首次跑 4 FAIL 后 exit 124）。指定 `HAVOC_FM_WINDOWS=2c67a896 HAVOC_FM_LINUX=791a44ae` 后 30/30。
- 做法：按最近心跳时间过滤（如 `last_seen` 距今 < 60s），或首条命令 504 时自动切到下一个候选会话；保留 `HAVOC_FM_*` 覆盖。
- 验收：不 pin 会话连跑 2 次均 30/30。

### P1-4 Linux 会话上 4 条 Windows 原生命令静默占满超时

- 位置：`havoc-src/client/Modules/SituationalAwareness.py` 中 `netshares` / `cacls` / `netLocalGroupList` / `netLclGrpLstMmbrs`（走 `Packer` + `demon.Command(...)` 的原生 CommandID，而非 `InlineExecute`）。
- 现象：Linux Demon 对这些 CommandID 不回包，矩阵 12s 超时记「无响应」；**会话 1 秒内即可恢复**（`shell echo` 正常），不损坏会话。
- 对比：走 `InlineExecute` 的命令（`ipconfig`/`adcs_enum`/`locale` 等）会快速返回 `command 0x14 is not supported by the Linux demon`。
- 做法：在 `webui/linux_compat.py` 增加「Windows 原生指令」集合，`plan()` 在 Linux 会话直接返回客户端侧错误（不下发）；或在网关 `dispatch` 对 Linux 会话 + Windows-only CommandID 短路。注意别影响 `LINUX_NATIVE` 里已有的改写命令。
- 验收：矩阵 Linux 这 4 条变为 `不支持`（<1s），且会话不再被占用；`linux_agent_test.py` 仍 20/20。

### P2-5 口径一致性 / 已知边界（不要当回归修）

- 命令表口径已统一为「108 行 = 35 + 73」，`command_test.py` 里的 `108` 断言不要改。
- 矩阵结果留档：`logs/cmd-matrix-20261111-linux.json`、`logs/cmd-matrix-20261111-windows.json`；历史基线 `webui/data/cmd_matrix.json`，diff 脚本见本轮对话。
- 以下为**设计边界**，不是 bug：
  1. havocui 图形扩展永远不能在 Web 运行（`pysdk/havocui.py` 直接 `ImportError`）；
  2. 第三方 Agent 的 `Agent.Command` 未实现；
  3. 多浏览器共用同一个 `Neo` 连接与同一个常驻 Python 宿主；
  4. 长 BOF（`netview` ~46s、`bofbelt`）会占住会话，矩阵靠轮换会话规避。

---

## 3. 环境红线（务必遵守）

1. **不要重启 teamserver**（pid `1979989`）。它跑的是被替换的旧 inode，重启会重连全部 18 个会话；需要重编只听 `make ts-build`，二进制替换不影响运行中进程。
2. **不要删 `havoc-src/client/Modules`**、不要 `git clean`、不要删 `backup/`；`client/Modules` 是 gitignore 的独立 clone。
3. **不要执行矩阵标记为「跳过」的破坏性命令**（27 条 Windows / 14 条 Linux：改注册表/服务/账号/进程注入等）。
4. 端口 `40056`/`8081`/`9901` 已被占用；测试监听器请用高位端口（`transport_test` 已如此）。
5. 改 `webui/*` 后重启 webui 即可（`./webui/stop-webui.sh && ./webui/start-webui.sh`）；**不要**为了测试重启 teamserver。
6. 开跑前先 `./start-all.sh --status`；收尾确认服务仍在。

---

## 4. 验收命令（改完逐个跑，全绿再交付）

```bash
cd /home/kali/桌面/Havoc/webui
python3 acceptance.py                 # 15/15
python3 command_test.py               # 34/34
python3 cmd_help_test.py              # 6/6
python3 ui_feature_test.py            # 15/15（需要 CDP :9222）
python3 gap_features_test.py          # 42/42
HAVOC_FM_WINDOWS=2c67a896 HAVOC_FM_LINUX=791a44ae python3 file_manager_test.py   # 30/30
python3 file_manager_ui_test.py       # 13/13
python3 mcp_test.py --live            # 47/47
python3 mcp_admin_test.py             # 92/92
python3 sdk_contract_test.py          # 35/35
python3 delivery_chain_test.py        # 19/19
python3 service_listener_test.py      # 25/25
python3 transport_test.py             # 8/8
python3 transport_relay_test.py       # 8/8
python3 linux_agent_test.py           # 20/20（会构建并上线一个临时 Linux demon）
python3 reconnect_test.py             # PASS
python3 disconnect_test.py            # 修好后应 PASS
```

命令矩阵（耗时：Linux ~9 分钟，Windows ~20 分钟，可用 `--timeout` 控制）：

```bash
python3 cmd_matrix_test.py --only lin --timeout 10 --out /tmp/lin.json
python3 cmd_matrix_test.py --only win --win 2c67a896,2d96d638,332dce4a,7f207ab4 --timeout 12 --out /tmp/win.json
```

判定：Windows「可用」≥73、无 `失败`；Linux「不支持」≈42（Windows 专有，预期）、「无响应」应为 0（P1-4 修好后）。

---

## 5. 交付物与边界

- 只改 `webui/`（测试与网关逻辑）与必要的 `webui/README.md`；**不要动** `havoc-src/teamserver` 业务代码、`payloads/`、`profiles/`。
- 根仓库当前有未提交改动（12 个文件 + `webui/reference/`）；如要提交，请与用户确认后一起提交。
- 改完在 `webui/README.md`「已知限制 / 验收记录」补一行说明。

---

## 6. 快速排障提示

- webui 日志：`logs/webui-console.log`（本轮基线：除 01:21 一条历史 ERROR 外无新增异常）。
- teamserver 日志：`logs/teamserver.log`；服务状态：`./start-all.sh --status`。
- 会话挑选：`/api/state` 看 `health`，但 health 有滞后，**以最近心跳为准**。
- 命令表生成：`python3 tools/gen_demon_commands.py`（读 `webui/reference/`，需 `havoc-src/client/Modules` 在位）。
- 矩阵对比：`webui/data/cmd_matrix.json` 是删客户端前的基线；新结果在 `logs/cmd-matrix-20261111-*.json`。
- MCP 令牌：`webui/run/mcp.token`；MCP 端点：`http://127.0.0.1:8081/mcp`（`Authorization: Bearer <token>`）。
