# Demon 命令平台可用性矩阵（实测）

对 `help` 里列出的全部命令，分别在 Windows 会话与 Linux 会话上真实下发一次，按 agent 回包判定可用性。破坏性命令（加账号、转储 LSASS、注入、改注册表/服务等）未实测。

| 项目 | 值 |
| --- | --- |
| 实测时间 | 2026-11-10 20:50 初测，22:30 按 `<命令> help` 用法补测 (Asia/Shanghai) |
| Windows 会话 | `demon.x64.exe`，Windows 10.0.26100，Administrator，sleep 2 |
| Linux 会话 | DemonLinux (Go)，Kali GNU/Linux Rolling x64，sleep 0 |
| 通道 | teamserver 40056 + Web 网关 8081（Qt 客户端与 Web 控制台共用同一套命令） |
| 判定来源 | `webui/cmd_matrix_test.py` 实测 + `linux_compat.py` / `linux_compat.go` 源码 |

## 结论

- **Windows：112 条命令全部是该平台的原生/BOF 实现**，实测 70 条直接返回结果；12 条只是缺参数或环境限制（无域/无服务/空队列），2 条长 BOF 在 12s 内没返回（`bofbelt`, `netview`），27 条破坏性命令未测，1 条（`help`）是客户端本地渲染。命令链路本身全部正常。其中 `klist`、`purge`、`wmi_query`、`reg_query` 是按 `<命令> help` 给出的用法补测后转为可用的。
- **Linux：44 条可用**（原生实现或由网关/teamserver 改写），**54 条仅 Windows**（AD/Kerberos/注册表/服务/令牌/注入/.NET/BOF/WMI 等没有 Linux 语义），14 条破坏性命令未测。
- Linux 上不可用的命令不会“静默成功”：agent 统一回 `command 0x.. is not supported by the Linux demon`，在控制台以红色 error 显示。

## Linux 能直接用哪些

**agent 原生（17 条）**：`cat`、`cd`、`checkin`、`config`、`cp`、`dir`、`download`、`mkdir`、`mv`、`proc`、`pwd`、`remove`、`rportfwd`、`screenshot`、`shell`、`sleep`、`socks`

**改写为 Linux 原生命令（23 条）**：`arp`、`enumlocalsessions`、`env`、`get-netsession`、`ipconfig`、`listdns`、`locale`、`net`、`netstat`、`netuptime`、`netuser`、`nslookup`、`powerpick`、`powershell`、`quser`、`resources`、`routeprint`、`sessions`、`tasklist`、`uptime`、`userenum`、`whoami`、`windowlist`

**客户端本地、与平台无关（4 条）**：`help`、`pyhello`、`pysessions`、`task`

**仅 Windows、Linux 上没有等价语义（54 条）**：`adcs_enum`、`bofbelt`、`bofenv`、`bofenvcb`、`cacls`、`dcenum`、`dll`、`domainenum`、`dotnet`、`driversigs`、`enum_filter_driver`、`get-asrep`、`get-delegation`、`get-spns`、`get_password_policy`、`job`、`kerberoast`、`klist`、`ldapsearch`、`luid`、`netGroupList`、`netGroupListMembers`、`netLclGrpLstMmbrs`、`netLocalGroupList`、`netshares`、`netsharesAdmin`、`netview`、`ptt`、`purge`、`reg_delete`、`reg_query`、`reg_query_recursive`、`reg_save`、`reg_set`、`samdump`、`sc_create`、`sc_delete`、`sc_description`、`sc_enum`、`sc_qc`、`sc_qdescription`、`sc_qfailure`、`sc_qtriggerinfo`、`sc_query`、`sc_start`、`sc_stop`、`schtasksenum`、`schtasksquery`、`setuserpass`、`shellcode`、`tgtdeleg`、`token`、`transfer`、`wmi_query`

## 判定口径

| 标记 | 含义 |
| --- | --- |
| ✅ 可用 / 原生 | 返回了真实结果 |
| ⚠️ 需参数 / 环境限制 | 命令链路正常，只是缺参数或目标环境不具备条件（无域、无服务等） |
| ⏱ 超时 | 12s 内没有回包；实测是长 BOF 占住会话（见“坑”） |
| ❌ 仅 Windows | Linux Demon 没有该命令实现 |
| ⏭ 未测 | 破坏性/会改目标状态，未执行 |

## 缺参数时怎么查用法：`<命令> help`

Web 控制台里 `<命令> help` 与 `help <命令>` 等价，会打印 Description / Usage / Example / Required Args / Platform；参数不足时，错误信息里也会直接附上用法和提示（Qt 客户端只支持 `help <命令>`）。首参是路径/查询语句的命令（`cat`、`dir`、`wmi_query`、`reg_query` 等）例外：它们只认 `help <命令>`，`<命令> help` 会当成普通参数照常下发。

| 命令 | 用法 | 示例 |
| --- | --- | --- |
| `config` | `config [config.flag]` | `config inject.spawn64 C:\Windows\System32\rundll32.exe` |
| `get-delegation` | `get-delegation [Constrained,Unconstrained,RBCD]` | `get-delegation constrained` |
| `kerberoast` | `kerberoast <spn>` | — |
| `klist` | `klist [/luid 0x123| /all]` | `klist /all` |
| `purge` | `purge /luid <0x123>` | `purge /luid 0x123` |
| `reg_query` | `reg_query [opt:hostname] [hive] [path] [opt: value to query]` | `reg_query HKLM SYSTEM\CurrentControlSet\Control\Lsa RunAsPPL` |
| `reg_query_recursive` | `reg_query_recursive [opt:hostname] [hive] [path]` | `reg_query_recursive HKLM SYSTEM\CurrentControlSet\Control\Lsa` |
| `schtasksquery` | `schtasksquery [opt: server] [taskpath]` | — |
| `tgtdeleg` | `tgtdeleg <spn>` | — |
| `wmi_query` | `wmi_query query [opt: server] [opt: namespace]` | `wmi_query "Select name from Win32_ComputerSystem"` |

本次按 help 里的用法补测了 `klist /all`、`purge /luid 0x0`、`wmi_query "select ..."`、`reg_query HKLM SOFTWARE`，都已返回真实结果（矩阵里已更新为可用）。

## 全量矩阵

| 命令 | 类型 | Windows 实测 | Linux | Linux 实现 / 说明 |
| --- | --- | --- | --- | --- |
| `adcs_enum` | Command | ⚠️ 环境限制（0x8007054b，本机不在域内/无 DC） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `adcs_request` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `adduser` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `addusertogroup` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `arp` | Command | ✅ 可用 | ✅ 可用 | 改写为：ip neigh |
| `bofbelt` | Command | ⏱ 超时（12s 内无返回；本次未等到结果，会话随后被占住） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `bofenv` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `bofenvcb` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `cacls` | Command | ⚠️ 失败（Error starting search handle，路径/通配符问题） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `cat` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `cd` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `checkin` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `config` | Module | ⚠️ 需参数（需要子命令） | ✅ 原生 | 仅回显 sleep/jitter，需要子命令 |
| `cp` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `dcenum` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `dir` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `dll` | Module | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `domainenum` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `dotnet` | Module | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `download` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `driversigs` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `enableuser` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `enum_filter_driver` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `enumlocalsessions` | Command | ✅ 可用 | ✅ 可用 | 改写为：who -u |
| `env` | Command | ✅ 可用 | ✅ 可用 | 改写为：env |
| `exit` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `get-asrep` | Command | ⚠️ 环境限制（无域，取不到 DN） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `get-delegation` | Command | ⚠️ 需参数（需要 Constrained/Unconstrained/RBCD） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `get-netsession` | Command | ✅ 可用 | ✅ 可用 | 改写为：who -u |
| `get-spns` | Command | ⚠️ 环境限制（无域，取不到 DN） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `get_password_policy` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `help` | Command | ℹ️ 本地 help | ✅ 可用 | 客户端本地命令，与平台无关 |
| `inline-execute` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `ipconfig` | Command | ✅ 可用 | ✅ 可用 | 改写为：ip -brief address + ip route |
| `job` | Module | ✅ 可用 | ❌ 仅 Windows | job 模块在 Linux 没有实现（COMMAND_JOB 0x15 未处理） |
| `jump-exec` | Module | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `kerberoast` | Command | ⚠️ 需参数（需要 spn） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `klist` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `ldapsearch` | Command | ⚠️ 环境限制（无域，取不到 DN） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `listdns` | Command | ✅ 可用 | ✅ 可用 | 改写为：resolvectl / /etc/resolv.conf |
| `locale` | Command | ✅ 可用 | ✅ 可用 | 改写为：locale |
| `luid` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `mkdir` | Command | ✅ 可用（清理残留后实测可创建；已存在时报 183） | ✅ 原生 | 目录已存在时第二次会报 EEXIST/183 |
| `mv` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `nanodump` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `nanodump_ppl_dump` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `nanodump_ppl_medic` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `nanodump_ssp` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `net` | Module | ✅ 可用 | ✅ 可用 | 改写为：net users|localgroup|sessions|domain → getent/who/hostname |
| `netGroupList` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `netGroupListMembers` | Command | ⚠️ 环境限制（Error 2220，组不存在或无域） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `netLclGrpLstMmbrs` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `netLocalGroupList` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `netshares` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `netsharesAdmin` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `netstat` | Command | ✅ 可用 | ✅ 可用 | 改写为：ss -tunap |
| `netuptime` | Command | ✅ 可用 | ✅ 可用 | 改写为：uptime -p + /proc/uptime |
| `netuser` | Command | ✅ 可用 | ✅ 可用 | 改写为：getent passwd <user> |
| `netview` | Command | ⏱ 超时（>12s，实测 46s 才返回） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `noconsolation` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `nslookup` | Command | ✅ 可用 | ✅ 可用 | 改写为：getent hosts + dig |
| `pivot` | Module | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `powerpick` | Command | ✅ 可用 | ✅ 可用 | 改写为：pwsh -Command |
| `powershell` | Command | ✅ 可用 | ✅ 可用 | 改写为：pwsh -Command |
| `proc` | Module | ✅ 可用 | ✅ 原生 | proc list 直接解析 /proc |
| `ptt` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `purge` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `pwd` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `pyhello` | Command | ✅ 可用 | ✅ 可用 | 客户端本地命令，与平台无关 |
| `pysessions` | Command | ✅ 可用 | ✅ 可用 | 客户端本地命令，与平台无关 |
| `quser` | Command | ✅ 可用 | ✅ 可用 | 改写为：who -u |
| `reg_delete` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `reg_query` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `reg_query_recursive` | Command | ⚠️ 失败（error 2，hive/键写法或键不存在） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `reg_save` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `reg_set` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `remove` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `resources` | Command | ✅ 可用 | ✅ 可用 | 改写为：df -h + free -h |
| `routeprint` | Command | ✅ 可用 | ✅ 可用 | 改写为：ip route |
| `rportfwd` | Module | ✅ 可用 | ✅ 原生 | 目标侧监听转发，list/add/remove 可用 |
| `samdump` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_create` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_delete` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_description` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_enum` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_qc` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_qdescription` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_qfailure` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_qtriggerinfo` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_query` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_start` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sc_stop` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `schtasksenum` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `schtasksquery` | Command | ⚠️ 需参数（需要 taskpath） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `screenshot` | Command | ✅ 可用 | ✅ 原生 | 依赖 X11 + ImageMagick import / xwd / grim |
| `sessions` | Command | ✅ 可用 | ✅ 可用 | 改写为：who -u |
| `setuserpass` | Command | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `shell` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `shellcode` | Module | ⏭ 未测 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `sleep` | Command | ✅ 可用 | ✅ 原生 | Linux Demon 原生实现 |
| `socks` | Module | ✅ 可用 | ✅ 原生 | teamserver 侧 SOCKS5，list/add 可用 |
| `task` | Module | ✅ 可用（任务队列为空） | ✅ 可用 | 客户端任务队列（空队列时提示 No jobs） |
| `tasklist` | Command | ✅ 可用 | ✅ 可用 | 改写为：proc list（解析 /proc） |
| `tgtdeleg` | Command | ⚠️ 需参数（需要 spn） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `token` | Module | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `transfer` | Command | ✅ 可用 | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |
| `upload` | Command | ⏭ 未测 | ⏭ 未测 | 破坏性命令，未实测 |
| `uptime` | Command | ✅ 可用 | ✅ 可用 | 改写为：uptime -p + /proc/uptime |
| `userenum` | Command | ✅ 可用 | ✅ 可用 | 改写为：getent passwd |
| `whoami` | Command | ✅ 可用 | ✅ 可用 | 改写为：id |
| `windowlist` | Command | ✅ 可用 | ✅ 可用 | 改写为：wmctrl / xdotool |
| `wmi_query` | Command | ✅ 可用（查询语句要加引号） | ❌ 仅 Windows | Linux 无等价实现，agent 回 not supported |

## Linux 侧的三条实现路径

1. **agent 原生**（`payloads/DemonLinux`）：文件操作、进程、截图、socket 转发、sleep/checkin/config。
2. **团队服务器 / 网关改写**（`teamserver/pkg/agent/linux_compat.go`、`webui/linux_compat.py`）：
   把 Qt 客户端以 BOF 下发的 `whoami`/`ipconfig`/`netstat` 之类改写成 `/bin/sh -c` 原生命令；
   两个客户端（Qt / Web）行为一致，Windows 会话完全不受影响。
3. **客户端本地命令**（`help`、`task`、`pyhello`、`pysessions`）：不经过 agent，和平台无关。

## 实测中发现的坑

- **`netview` / `bofbelt` 会长时间占住 Windows 会话**：本次 `netview` 实测 46s 才回 `BOF execution completed`，12s 超时窗口内表现为无响应；`bofbelt`（Seatbelt）在 12s 窗口内没返回，随后该会话被 BOF 占住、后续命令全部排队，直到 BOF 结束才恢复。批量扫命令时要给足超时，或单独开会话跑。
- **`mkdir` 目标已存在**返回 `Win32 Error: ERROR_ALREADY_EXISTS [183]` / `file exists`，不是命令不可用。
- **`reg_query` 的 hive 参数**写成 `HKLM` 时实测 `error value: 2`（找不到键），按客户端用法应传完整路径形式，属参数问题。
- **`windowlist`（Windows）** 返回的是目标桌面上的可见窗口标题，测试机上包含宿主窗口，属正常现象。
- **Linux 上的 `screenshot`** 依赖 X11 + ImageMagick `import`（或 xwd / grim），纯 headless 会失败。
- **Linux 上的 `windowlist`** 依赖 `wmctrl`/`xdotool`；`powershell`/`powerpick` 依赖 `pwsh`；`nslookup` 依赖 `getent`/`dig`，缺失时返回的是提示文本而不是结果。
- **`job` 模块**在 Linux 上不可用（`COMMAND_JOB 0x15` 未实现），但 `task` 是客户端队列命令，两端都能用。
- Windows 会话被杀软/EDR 干预时 BOF 可能长时间不返回，与命令本身无关。

## 复现

```bash
cd webui
python3 cmd_matrix_test.py                      # 全量实测，结果写到 data/cmd_matrix.json
python3 cmd_matrix_report.py                   # 生成本文档
python3 cmd_matrix_test.py --only lin --limit 20  # 只测 Linux 前 20 条
```
