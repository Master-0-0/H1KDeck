package main

import (
	"net"
	"sort"
	"strings"
)

// netDialUDP 返回本机的“默认出口”地址。这里仅创建 UDP socket（不发送任何数据），
// 在无默认路由时回退到枚举网卡。
func netDialUDP() (net.Conn, error) {
	conn, err := net.Dial("udp", "1.1.1.1:53")
	if err == nil {
		return conn, nil
	}
	return net.Dial("udp", "127.0.0.1:53")
}

// internalIP 的网卡回退方案。
func internalIPFromInterfaces() string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return "127.0.0.1"
	}
	var candidates []string
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			if ipnet, ok := addr.(*net.IPNet); ok && ipnet.IP.To4() != nil {
				candidates = append(candidates, ipnet.IP.String())
			}
		}
	}
	sort.Strings(candidates)
	if len(candidates) > 0 {
		return candidates[0]
	}
	return "127.0.0.1"
}

var _ = strings.TrimSpace
