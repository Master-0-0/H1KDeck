// HTP/1 relay: 在目标上把本地 TCP（如 Demon 的 HTTP 代理）转发到加密传输监听器。
// 仅用于已获得明确书面授权的测试环境。
package main

import (
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"strings"
	"sync"
	"time"
)

// 由 -ldflags "-X main.embeddedConfigB64=..." 注入（base64url JSON）。
var embeddedConfigB64 = ""

func main() {
	var (
		configPath = flag.String("config", "", "JSON config file (overrides embedded config)")
		listen     = flag.String("listen", "", "local listen address, e.g. 127.0.0.1:1080")
		server     = flag.String("server", "", "transport server host:port")
		probe      = flag.Bool("probe", false, "connect once and exit")
		verbose    = flag.Bool("v", false, "verbose logging")
	)
	flag.Parse()

	log.SetFlags(log.LstdFlags)
	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	cfg.normalize()
	if *server != "" {
		host, port, err := net.SplitHostPort(*server)
		if err != nil {
			log.Fatalf("bad --server: %v", err)
		}
		cfg.ServerHost = host
		if _, err := fmt.Sscanf(port, "%d", &cfg.ServerPort); err != nil {
			log.Fatalf("bad --server port: %v", err)
		}
	}
	if *listen != "" {
		cfg.LocalListen = *listen
	}
	if !*verbose {
		log.SetOutput(io.Discard)
	}

	if *probe {
		link, err := dialTransport(cfg)
		if err != nil {
			fmt.Fprintf(os.Stderr, "PROBE FAIL %v\n", err)
			os.Exit(1)
		}
		_ = link.ch.Write([]byte("PING"))
		link.Close()
		fmt.Println("PROBE OK", cfg.Mode, cfg.addr())
		return
	}

	ln, err := net.Listen("tcp", cfg.LocalListen)
	if err != nil {
		log.Fatalf("listen %s: %v", cfg.LocalListen, err)
	}
	log.Printf("HTP relay %s -> %s (local %s)", cfg.Mode, cfg.addr(), cfg.LocalListen)
	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Printf("accept: %v", err)
			continue
		}
		go handleLocal(conn, cfg)
	}
}

func loadConfig(path string) (*Config, error) {
	var raw []byte
	if path != "" {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, err
		}
		raw = data
	} else {
		if embeddedConfigB64 == "" {
			return nil, fmt.Errorf("no embedded config; pass --config")
		}
		data, err := base64.RawURLEncoding.DecodeString(strings.TrimSpace(embeddedConfigB64))
		if err != nil {
			return nil, err
		}
		raw = data
	}
	var cfg Config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}

func handleLocal(local net.Conn, cfg *Config) {
	defer local.Close()
	link, err := dialTransport(cfg)
	if err != nil {
		log.Printf("transport dial: %v", err)
		return
	}
	defer link.Close()

	var once sync.Once
	closeBoth := func() {
		once.Do(func() {
			_ = local.Close()
			link.Close()
		})
	}

	go func() {
		_, _ = io.Copy(chWriter{link.ch}, local)
		closeBoth()
	}()
	_, _ = io.Copy(local, &chReader{ch: link.ch})
	closeBoth()
}

type chWriter struct{ ch *Channel }

func (w chWriter) Write(p []byte) (int, error) {
	if err := w.ch.Write(p); err != nil {
		return 0, err
	}
	return len(p), nil
}

type chReader struct {
	ch  *Channel
	buf []byte
}

func (r *chReader) Read(p []byte) (int, error) {
	for len(r.buf) == 0 {
		data, err := r.ch.Read()
		if err != nil {
			return 0, err
		}
		r.buf = data
	}
	n := copy(p, r.buf)
	r.buf = r.buf[n:]
	return n, nil
}

var _ = time.Second
