package agent

import (
	"strconv"
	"strings"

	"Havoc/pkg/common"
	"Havoc/pkg/win32"
)

// Linux 会话的命令兼容层。
//
// Qt 客户端把 whoami/env/ipconfig 等命令实现为 BOF（COMMAND_INLINEEXECUTE），
// Linux Demon 无法执行 COFF 对象。Web 控制台已经在网关侧用 linux_compat.py 做过
// 同样的改写；这里在 teamserver 再做一层，这样任何客户端（Qt / 第三方 SDK / Web）
// 对 Linux 会话下发这些命令时都能得到 Linux 原生结果，而 Windows 会话完全不受影响。
//
// 语义与 webui/linux_compat.py 保持一致：
//
//	whoami           -> id
//	env              -> env
//	ipconfig         -> ip -brief address + ip route
//	netstat          -> ss -tunap
//	arp              -> ip neigh
//	routeprint       -> ip route
//	listdns          -> resolvectl status / /etc/resolv.conf
//	locale           -> locale
//	uptime/netuptime -> uptime -p + /proc/uptime
//	resources        -> df -h + free -h
//	userenum         -> getent passwd
//	netuser          -> getent passwd <user>
//	sessions/enumlocalsessions/get-netsession/quser/netsession -> who -u
//	nslookup         -> getent hosts + dig
//	windowlist       -> wmctrl / xdotool
//	tasklist         -> agent 的 proc list（/proc）
//	net users|localgroup|sessions|domain -> getent / who / hostname -d

func (a *Agent) isLinuxAgent() bool {
	if a == nil || a.Info == nil {
		return false
	}
	if strings.EqualFold(a.Info.Platform, "linux") {
		return true
	}
	// teamserver 重启后会话从 TS_Agents 恢复，但该表没有 Platform 列；
	// Linux Demon 注册时写入的 OSVersion 形如 "Kali GNU/Linux Rolling"，
	// 用它兜底判断，Windows 的系统名不会包含 linux。
	return strings.Contains(strings.ToLower(a.Info.OSVersion), "linux")
}

// shellQuote 把参数安全地包进单引号，供 /bin/sh -c 使用。
func shellQuote(s string) string {
	if s == "" {
		return "''"
	}
	return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'"
}

func linuxShellEquivalent(name string, args []string) (string, bool) {
	switch name {
	case "whoami":
		return "id", true
	case "env":
		return "env", true
	case "ipconfig":
		return "ip -brief address 2>/dev/null || ifconfig -a; echo; ip route 2>/dev/null || route -n", true
	case "netstat":
		return "ss -tunap 2>/dev/null || netstat -tunap 2>/dev/null", true
	case "arp":
		return "ip neigh 2>/dev/null || cat /proc/net/arp", true
	case "routeprint":
		return "ip route 2>/dev/null || route -n", true
	case "listdns":
		return "resolvectl status 2>/dev/null || cat /etc/resolv.conf", true
	case "locale":
		return "locale", true
	case "uptime", "netuptime":
		return "uptime -p 2>/dev/null; cat /proc/uptime", true
	case "resources":
		return "df -h 2>/dev/null; echo; free -h 2>/dev/null || head -5 /proc/meminfo", true
	case "userenum":
		return "getent passwd 2>/dev/null || cat /etc/passwd", true
	case "netuser":
		if len(args) >= 1 {
			user := shellQuote(args[0])
			return "getent passwd " + user + " 2>/dev/null || grep -E '^" + args[0] + ":' /etc/passwd", true
		}
		return "getent passwd 2>/dev/null || cat /etc/passwd", true
	case "sessions", "enumlocalsessions", "get-netsession", "quser", "netsession":
		return "who -u 2>/dev/null || w", true
	case "nslookup":
		if len(args) >= 1 {
			host := shellQuote(args[0])
			return "getent hosts " + host + " 2>/dev/null; command -v dig >/dev/null && dig +short " + host, true
		}
		return "", false
	case "windowlist":
		return "wmctrl -l 2>/dev/null || xdotool search --onlyvisible --name . getwindowname %@ 2>/dev/null || " +
			"echo 'windowlist 需要 wmctrl 或 xdotool'", true
	}
	return "", false
}

// linuxNetEquivalent 把 Qt 的 `net <sub>` 数值子命令（COMMAND_NET）改写为 Linux 命令。
// 返回 ok=false 表示该子命令在 Linux 上没有等价语义，保持原样让 agent 回显不支持。
func linuxNetEquivalent(netCommand int, param string) (string, bool) {
	switch netCommand {
	case DEMON_NET_COMMAND_DOMAIN:
		return "hostname -d 2>/dev/null; hostname -f 2>/dev/null", true
	case DEMON_NET_COMMAND_SESSIONS:
		return "who -u 2>/dev/null || w", true
	case DEMON_NET_COMMAND_LOCALGROUP:
		return "getent group 2>/dev/null || cat /etc/group", true
	case DEMON_NET_COMMAND_USERS:
		return "getent passwd 2>/dev/null || cat /etc/passwd", true
	}
	return "", false
}

// linuxProcCreateJob 构造与 Windows 客户端 `shell` 相同的 PROC::Create 任务
// （SubCommand=4，进程 /bin/sh，参数 "-c <cmd>"，piped=verbose=1）。
func linuxProcCreateJob(job *Job, command string) *Job {
	job.Command = uint32(COMMAND_PROC)
	job.Data = []interface{}{
		DEMON_COMMAND_PROC_CREATE,
		0,
		common.EncodeUTF16("/bin/sh"),
		common.EncodeUTF16("-c " + command),
		1,
		1,
	}
	return job
}

// linuxRewriteTask 在 TaskPrepare 的通用分支之前调用：识别 Linux 会话上的
// Windows 专属模块命令并改写为 Linux 原生任务。命中返回 (job, true)。
func (a *Agent) linuxRewriteTask(job *Job, command int, optional map[string]interface{}) (*Job, bool) {
	if !a.isLinuxAgent() {
		return nil, false
	}

	line := strings.TrimSpace(job.CommandLine)
	if line == "" {
		return nil, false
	}

	switch command {

	case COMMAND_INLINEEXECUTE:
		// 带回调的 BOF（python 模块要收回输出）不能改写，否则回调永远不触发。
		if v, ok := optional["HasCallback"].(string); ok && strings.EqualFold(v, "true") {
			return nil, false
		}

		fields := strings.Fields(line)
		if len(fields) == 0 {
			return nil, false
		}
		name := strings.ToLower(fields[0])

		// tasklist -> agent 原生 proc list（解析 /proc），与 Web 端一致
		if name == "tasklist" {
			job.Command = uint32(COMMAND_PROC_LIST)
			job.Data = []interface{}{win32.FALSE}
			return job, true
		}

		shell, ok := linuxShellEquivalent(name, fields[1:])
		if !ok {
			return nil, false
		}
		return linuxProcCreateJob(job, shell), true

	case COMMAND_NET:
		val, ok := optional["NetCommand"].(string)
		if !ok {
			return nil, false
		}
		netCommand, err := strconv.Atoi(val)
		if err != nil {
			return nil, false
		}
		param := ""
		if v, ok := optional["Param"].(string); ok {
			param = v
		}
		shell, ok := linuxNetEquivalent(netCommand, param)
		if !ok {
			return nil, false
		}
		return linuxProcCreateJob(job, shell), true
	}

	return nil, false
}
