package main

import (
	"bufio"
	"bytes"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Config is embedded (or loaded from --config) by the relay.
type Config struct {
	Mode         string `json:"mode"`
	ServerHost   string `json:"server_host"`
	ServerPort   int    `json:"server_port"`
	WSPath       string `json:"ws_path"`
	DNSDomain    string `json:"dns_domain"`
	OssURL       string `json:"oss_url"`
	DoHURL       string `json:"doh_url"`
	OssPrefix    string `json:"oss_prefix"`
	VKey         string `json:"vkey"`
	Salt         string `json:"salt"`
	Compress     bool   `json:"compress"`
	Padding      int    `json:"padding"`
	LocalListen  string `json:"local_listen"`
	PingInterval int    `json:"ping_interval"`
	PollMs       int    `json:"poll_interval_ms"`
	UpstreamHint string `json:"upstream,omitempty"`
}

func (c *Config) normalize() {
	if c.Mode == "" {
		c.Mode = "tcp"
	}
	if c.ServerHost == "" {
		c.ServerHost = "127.0.0.1"
	}
	if c.ServerPort == 0 {
		c.ServerPort = 9001
	}
	if c.WSPath == "" {
		c.WSPath = "/ws"
	}
	if c.OssPrefix == "" {
		c.OssPrefix = "havoc"
	}
	if c.LocalListen == "" {
		c.LocalListen = "127.0.0.1:1080"
	}
	if c.PollMs <= 0 {
		c.PollMs = 20
	}
	if c.PingInterval <= 0 {
		c.PingInterval = 10
	}
}

func (c *Config) addr() string {
	return net.JoinHostPort(c.ServerHost, strconv.Itoa(c.ServerPort))
}

func (c *Config) recordSize() int {
	switch c.Mode {
	case "tcp", "ws":
		return 16384
	case "udp":
		return 900
	case "kcp":
		return 1200
	case "dns":
		n := 80 - c.Padding
		if n < 64 {
			n = 64
		}
		return n
	case "doh", "dot":
		return 900
	case "oss":
		return 1200
	}
	return 900
}

// Channel turns a plaintext byte stream into encrypted records.
type Channel struct {
	enc     *RecordCodec
	dec     *RecordCodec
	send    func([]byte) error
	recv    func() ([]byte, error)
	maxData int
}

func (ch *Channel) Write(data []byte) error {
	for off := 0; off < len(data); off += ch.maxData {
		end := off + ch.maxData
		if end > len(data) {
			end = len(data)
		}
		frame, err := ch.enc.encode(data[off:end])
		if err != nil {
			return err
		}
		if err := ch.send(frame); err != nil {
			return err
		}
	}
	return nil
}

func (ch *Channel) Read() ([]byte, error) {
	for {
		frame, err := ch.recv()
		if err != nil {
			return nil, err
		}
		if len(frame) == 0 {
			continue
		}
		data, err := ch.dec.decode(frame)
		if err != nil {
			continue
		}
		if len(data) == 0 {
			continue
		}
		return data, nil
	}
}

type transportLink struct {
	ch     *Channel
	closer func()
}

func (l *transportLink) Close() {
	if l.closer != nil {
		l.closer()
	}
}

// ------------------------------------------------------------------ 握手

func makeHello(master []byte) ([]byte, []byte) {
	sid := randomBytes(8)
	nonce := randomBytes(16)
	hello := make([]byte, 0, handshakeLen)
	hello = append(hello, magic...)
	hello = append(hello, sid...)
	hello = append(hello, nonce...)
	hello = append(hello, handshakeMAC(master, sid, nonce, []byte("client"))...)
	return sid, hello
}

func verifyAck(master, sid, ack []byte) error {
	if len(ack) != handshakeLen || !bytes.Equal(ack[:4], magic) ||
		!bytes.Equal(ack[4:12], sid) {
		return errors.New("handshake rejected")
	}
	nonce := ack[12:28]
	mac := ack[28:60]
	good := handshakeMAC(master, sid, nonce, []byte("server"))
	if !hmacEqual(mac, good) {
		return errors.New("handshake mac mismatch")
	}
	return nil
}

func hmacEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	var v byte
	for i := range a {
		v |= a[i] ^ b[i]
	}
	return v == 0
}

func newCodecs(master, sid []byte, cfg *Config) (*RecordCodec, *RecordCodec, error) {
	c2s, s2c := sessionKeys(master, sid)
	enc, err := newRecordCodec(c2s, 1, cfg.Compress, cfg.Padding)
	if err != nil {
		return nil, nil, err
	}
	dec, err := newRecordCodec(s2c, 2, cfg.Compress, cfg.Padding)
	if err != nil {
		return nil, nil, err
	}
	return enc, dec, nil
}

// ------------------------------------------------------------------ TCP

func dialTCP(cfg *Config) (*transportLink, error) {
	master := deriveMaster(cfg.VKey, cfg.Salt)
	sid, hello := makeHello(master)
	conn, err := net.DialTimeout("tcp", cfg.addr(), 8*time.Second)
	if err != nil {
		return nil, err
	}
	_ = conn.SetDeadline(time.Now().Add(12 * time.Second))
	if _, err := conn.Write(hello); err != nil {
		conn.Close()
		return nil, err
	}
	buf := make([]byte, handshakeLen)
	if _, err := io.ReadFull(conn, buf); err != nil {
		conn.Close()
		return nil, err
	}
	if err := verifyAck(master, sid, buf); err != nil {
		conn.Close()
		return nil, err
	}
	_ = conn.SetDeadline(time.Time{})
	enc, dec, err := newCodecs(master, sid, cfg)
	if err != nil {
		conn.Close()
		return nil, err
	}
	var writeMu sync.Mutex
	send := func(frame []byte) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		var head [4]byte
		binary.BigEndian.PutUint32(head[:], uint32(len(frame)))
		if _, err := conn.Write(head[:]); err != nil {
			return err
		}
		_, err := conn.Write(frame)
		return err
	}
	recv := func() ([]byte, error) {
		var head [4]byte
		if _, err := io.ReadFull(conn, head[:]); err != nil {
			return nil, err
		}
		size := binary.BigEndian.Uint32(head[:])
		if size == 0 || size > maxFrame {
			return nil, errors.New("bad frame size")
		}
		buf := make([]byte, size)
		_, err := io.ReadFull(conn, buf)
		return buf, err
	}
	return &transportLink{
		ch:     &Channel{enc: enc, dec: dec, send: send, recv: recv, maxData: cfg.recordSize()},
		closer: func() { conn.Close() },
	}, nil
}

// ------------------------------------------------------------------ WebSocket

type wsConn struct {
	conn    net.Conn
	reader  *bufio.Reader
	writeMu sync.Mutex
}

func dialWS(cfg *Config) (*transportLink, error) {
	addr := cfg.addr()
	conn, err := net.DialTimeout("tcp", addr, 8*time.Second)
	if err != nil {
		return nil, err
	}
	_ = conn.SetDeadline(time.Now().Add(12 * time.Second))
	key := base64.StdEncoding.EncodeToString(randomBytes(16))
	path := cfg.WSPath
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	req := fmt.Sprintf("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n",
		path, addr, key)
	if _, err := conn.Write([]byte(req)); err != nil {
		conn.Close()
		return nil, err
	}
	reader := bufio.NewReader(conn)
	status, err := reader.ReadString('\n')
	if err != nil || !strings.Contains(status, "101") {
		conn.Close()
		return nil, fmt.Errorf("ws upgrade failed: %s", strings.TrimSpace(status))
	}
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			conn.Close()
			return nil, err
		}
		if line == "\r\n" || line == "\n" {
			break
		}
	}
	ws := &wsConn{conn: conn, reader: reader}

	master := deriveMaster(cfg.VKey, cfg.Salt)
	sid, hello := makeHello(master)
	if err := ws.writeBinary(hello); err != nil {
		conn.Close()
		return nil, err
	}
	ack, err := ws.readBinary()
	if err != nil {
		conn.Close()
		return nil, err
	}
	if err := verifyAck(master, sid, ack); err != nil {
		conn.Close()
		return nil, err
	}
	_ = conn.SetDeadline(time.Time{})
	enc, dec, err := newCodecs(master, sid, cfg)
	if err != nil {
		conn.Close()
		return nil, err
	}
	return &transportLink{
		ch: &Channel{enc: enc, dec: dec, send: ws.writeBinary, recv: ws.readBinary,
			maxData: cfg.recordSize()},
		closer: func() { conn.Close() },
	}, nil
}

func (w *wsConn) writeBinary(payload []byte) error {
	w.writeMu.Lock()
	defer w.writeMu.Unlock()
	header := []byte{0x82}
	n := len(payload)
	switch {
	case n < 126:
		header = append(header, byte(n)|0x80)
	case n <= 0xffff:
		header = append(header, 126|0x80, byte(n>>8), byte(n))
	default:
		header = append(header, 127|0x80, 0, 0, 0, 0,
			byte(n>>24), byte(n>>16), byte(n>>8), byte(n))
	}
	mask := randomBytes(4)
	frame := append(header, mask...)
	masked := make([]byte, n)
	for i := 0; i < n; i++ {
		masked[i] = payload[i] ^ mask[i%4]
	}
	frame = append(frame, masked...)
	_, err := w.conn.Write(frame)
	return err
}

func (w *wsConn) readBinary() ([]byte, error) {
	for {
		first, err := w.reader.ReadByte()
		if err != nil {
			return nil, err
		}
		opcode := first & 0x0f
		second, err := w.reader.ReadByte()
		if err != nil {
			return nil, err
		}
		masked := second&0x80 != 0
		length := uint64(second & 0x7f)
		switch length {
		case 126:
			var ext [2]byte
			if _, err := io.ReadFull(w.reader, ext[:]); err != nil {
				return nil, err
			}
			length = uint64(binary.BigEndian.Uint16(ext[:]))
		case 127:
			var ext [8]byte
			if _, err := io.ReadFull(w.reader, ext[:]); err != nil {
				return nil, err
			}
			length = binary.BigEndian.Uint64(ext[:])
		}
		var mask [4]byte
		if masked {
			if _, err := io.ReadFull(w.reader, mask[:]); err != nil {
				return nil, err
			}
		}
		if length > maxFrame {
			return nil, errors.New("ws frame too large")
		}
		payload := make([]byte, length)
		if _, err := io.ReadFull(w.reader, payload); err != nil {
			return nil, err
		}
		if masked {
			for i := range payload {
				payload[i] ^= mask[i%4]
			}
		}
		switch opcode {
		case 0x2, 0x1:
			return payload, nil
		case 0x8:
			return nil, io.EOF
		case 0x9:
			_ = w.writeControl(0xA, payload)
		case 0xA:
			continue
		}
	}
}

func (w *wsConn) writeControl(opcode byte, payload []byte) error {
	w.writeMu.Lock()
	defer w.writeMu.Unlock()
	mask := randomBytes(4)
	frame := []byte{0x80 | opcode, byte(len(payload)) | 0x80}
	frame = append(frame, mask...)
	for i := range payload {
		frame = append(frame, payload[i]^mask[i%4])
	}
	_, err := w.conn.Write(frame)
	return err
}

// ------------------------------------------------------------------ UDP / KCP

func dialUDP(cfg *Config) (*transportLink, error) {
	master := deriveMaster(cfg.VKey, cfg.Salt)
	sid, hello := makeHello(master)
	sidInt := binary.BigEndian.Uint64(sid)
	raddr, err := net.ResolveUDPAddr("udp", cfg.addr())
	if err != nil {
		return nil, err
	}
	conn, err := net.DialUDP("udp", nil, raddr)
	if err != nil {
		return nil, err
	}
	helloPkt := packPacket(sidInt, pktHello, 0, hello[12:])
	deadline := time.Now().Add(10 * time.Second)
	buf := make([]byte, 65535)
	var ackPacket []byte
	for time.Now().Before(deadline) {
		_, _ = conn.Write(helloPkt)
		_ = conn.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
		n, err := conn.Read(buf)
		if err != nil {
			continue
		}
		psid, typ, _, body, ok := unpackPacket(buf[:n])
		if ok && psid == sidInt && typ == pktHelloAck && len(body) >= 48 {
			ackPacket = append([]byte(nil), body...)
			break
		}
	}
	if ackPacket == nil {
		conn.Close()
		return nil, errors.New("udp handshake timeout")
	}
	if len(ackPacket) != 48 || !hmacEqual(ackPacket[16:48],
		handshakeMAC(master, sid, ackPacket[:16], []byte("server"))) {
		conn.Close()
		return nil, errors.New("udp hello ack mismatch")
	}
	_ = conn.SetReadDeadline(time.Time{})
	enc, dec, err := newCodecs(master, sid, cfg)
	if err != nil {
		conn.Close()
		return nil, err
	}
	window := 32
	if cfg.Mode == "kcp" {
		window = 64
	}
	a := newARQ(sidInt, window, 250*time.Millisecond)
	done := make(chan struct{})
	go func() {
		for {
			buf := make([]byte, 65535)
			n, err := conn.Read(buf)
			if err != nil {
				select {
				case <-done:
				default:
					a.close()
				}
				return
			}
			a.onPacket(buf[:n])
		}
	}()
	go func() {
		ticker := time.NewTicker(10 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case now := <-ticker.C:
				for i := 0; i < 64; i++ {
					pkt := a.take(now)
					if pkt == nil {
						break
					}
					if _, err := conn.Write(pkt); err != nil {
						return
					}
				}
			}
		}
	}()
	ch := &Channel{enc: enc, dec: dec, maxData: cfg.recordSize()}
	ch.send = func(frame []byte) error { a.queue(frame); return nil }
	ch.recv = func() ([]byte, error) {
		rec, ok := a.read()
		if !ok {
			return nil, io.EOF
		}
		return rec, nil
	}
	return &transportLink{ch: ch, closer: func() {
		close(done)
		conn.Close()
		a.close()
	}}, nil
}

// ------------------------------------------------------------------ 轮询（DNS/DoH/DoT/OSS）

type exchangeFunc func([]byte) ([]byte, error)

func dialPoll(cfg *Config, exchange exchangeFunc) (*transportLink, error) {
	master := deriveMaster(cfg.VKey, cfg.Salt)
	sid, hello := makeHello(master)
	sidInt := binary.BigEndian.Uint64(sid)
	helloPkt := packPacket(sidInt, pktHello, 0, hello[12:])
	deadline := time.Now().Add(12 * time.Second)
	var ack []byte
	for time.Now().Before(deadline) {
		resp, err := exchange(helloPkt)
		if err != nil {
			log.Printf("poll hello: %v", err)
		} else if len(resp) >= pktHeader {
			psid, typ, _, body, ok := unpackPacket(resp)
			if ok && psid == sidInt && typ == pktHelloAck && len(body) >= 48 {
				ack = body
				break
			}
			log.Printf("poll hello resp typ=%d sid=%x len=%d", typ, psid, len(resp))
		} else {
			log.Printf("poll hello short resp %d", len(resp))
		}
		time.Sleep(250 * time.Millisecond)
	}
	if ack == nil {
		return nil, errors.New("poll handshake timeout")
	}
	if !hmacEqual(ack[16:48], handshakeMAC(master, sid, ack[:16], []byte("server"))) {
		return nil, errors.New("poll hello ack mismatch")
	}
	enc, dec, err := newCodecs(master, sid, cfg)
	if err != nil {
		return nil, err
	}
	a := newARQ(sidInt, 1, 350*time.Millisecond)
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(time.Duration(cfg.PollMs) * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case now := <-ticker.C:
				pkt := a.take(now)
				if pkt == nil {
					pkt = a.ackPacket()
				}
				resp, err := exchange(pkt)
				if err == nil && len(resp) > 0 {
					a.onPacket(resp)
				}
			}
		}
	}()
	ch := &Channel{enc: enc, dec: dec, maxData: cfg.recordSize()}
	ch.send = func(frame []byte) error { a.queue(frame); return nil }
	ch.recv = func() ([]byte, error) {
		rec, ok := a.read()
		if !ok {
			return nil, io.EOF
		}
		return rec, nil
	}
	return &transportLink{ch: ch, closer: func() {
		close(done)
		a.close()
	}}, nil
}

// ------------------------------------------------------------------ DNS

type dnsExchange struct {
	cfg  *Config
	conn *net.UDPConn
}

func newDNSExchange(cfg *Config) (*dnsExchange, error) {
	raddr, err := net.ResolveUDPAddr("udp", cfg.addr())
	if err != nil {
		return nil, err
	}
	conn, err := net.DialUDP("udp", nil, raddr)
	if err != nil {
		return nil, err
	}
	return &dnsExchange{cfg: cfg, conn: conn}, nil
}

func (d *dnsExchange) do(packet []byte) ([]byte, error) {
	text := b32encode(packet)
	labels := splitLabels(text, 55)
	domain := strings.Trim(d.cfg.DNSDomain, ".")
	qname := strings.Join(labels, ".")
	if domain != "" {
		qname += "." + domain
	}
	query := buildDNSQuery(qname, uint16(randInt(65536)))
	if _, err := d.conn.Write(query); err != nil {
		return nil, err
	}
	buf := make([]byte, 65535)
	_ = d.conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	n, err := d.conn.Read(buf)
	if err != nil {
		return nil, err
	}
	return parseDNSTXT(buf[:n])
}

func (d *dnsExchange) close() {
	if d.conn != nil {
		d.conn.Close()
	}
}

// ------------------------------------------------------------------ DoH

func newDoHExchange(cfg *Config) (*httpExchange, error) {
	base := cfg.DoHURL
	if base == "" {
		base = "http://" + cfg.addr() + "/dns-query"
	} else if !strings.Contains(base, "/dns-query") {
		base = strings.TrimRight(base, "/") + "/dns-query"
	}
	return &httpExchange{url: base, mode: "doh", cfg: cfg}, nil
}

type httpExchange struct {
	url  string
	mode string
	cfg  *Config
}

func (h *httpExchange) do(packet []byte) ([]byte, error) {
	query := buildTXTQuery(packet, h.cfg.DNSDomain)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Post(h.url, "application/dns-message", bytes.NewReader(query))
	if err != nil {
		log.Printf("doh post %s: %v", h.url, err)
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		log.Printf("doh status %d", resp.StatusCode)
		return nil, fmt.Errorf("doh status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	return parseDNSTXT(body)
}

// ------------------------------------------------------------------ DoT

type dotExchange struct {
	cfg  *Config
	conn *tls.Conn
}

func newDoTExchange(cfg *Config) (*dotExchange, error) {
	conn, err := tls.Dial("tcp", cfg.addr(), &tls.Config{InsecureSkipVerify: true})
	if err != nil {
		return nil, err
	}
	return &dotExchange{cfg: cfg, conn: conn}, nil
}

func (d *dotExchange) do(packet []byte) ([]byte, error) {
	query := buildTXTQuery(packet, d.cfg.DNSDomain)
	_ = d.conn.SetDeadline(time.Now().Add(5 * time.Second))
	var head [2]byte
	binary.BigEndian.PutUint16(head[:], uint16(len(query)))
	if _, err := d.conn.Write(append(head[:], query...)); err != nil {
		return nil, err
	}
	if _, err := io.ReadFull(d.conn, head[:]); err != nil {
		return nil, err
	}
	size := binary.BigEndian.Uint16(head[:])
	body := make([]byte, size)
	if _, err := io.ReadFull(d.conn, body); err != nil {
		return nil, err
	}
	return parseDNSTXT(body)
}

func (d *dotExchange) close() {
	if d.conn != nil {
		d.conn.Close()
	}
}

// ------------------------------------------------------------------ OSS

type ossExchange struct {
	base   string
	prefix string
	sid    string
	seq    int
	client *http.Client
}

func newOSSExchange(cfg *Config) (*ossExchange, error) {
	base := strings.TrimRight(cfg.OssURL, "/")
	if base == "" {
		base = "http://" + cfg.addr()
	}
	return &ossExchange{
		base: base, prefix: strings.Trim(cfg.OssPrefix, "/"),
		client: &http.Client{Timeout: 5 * time.Second},
	}, nil
}

func (o *ossExchange) setSID(sid []byte) {
	o.sid = fmt.Sprintf("%x", sid)
}

func (o *ossExchange) do(packet []byte) ([]byte, error) {
	o.seq++
	up := fmt.Sprintf("%s/%s/%s/up/%d", o.base, o.prefix, o.sid, o.seq)
	req, _ := http.NewRequest(http.MethodPut, up, bytes.NewReader(packet))
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := o.client.Do(req)
	if err != nil {
		return nil, err
	}
	_ = resp.Body.Close()
	if resp.StatusCode >= 300 {
		return nil, fmt.Errorf("oss put status %d", resp.StatusCode)
	}
	down := fmt.Sprintf("%s/%s/%s/down/%d", o.base, o.prefix, o.sid, o.seq)
	resp, err = o.client.Get(down)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == 204 {
		return nil, nil
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("oss get status %d", resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, 1<<20))
}

func (o *ossExchange) close() {}

// ------------------------------------------------------------------ 统一拨号

func dialTransport(cfg *Config) (*transportLink, error) {
	switch cfg.Mode {
	case "tcp":
		return dialTCP(cfg)
	case "ws":
		return dialWS(cfg)
	case "udp", "kcp":
		return dialUDP(cfg)
	case "dns":
		ex, err := newDNSExchange(cfg)
		if err != nil {
			return nil, err
		}
		link, err := dialPoll(cfg, ex.do)
		if link != nil {
			old := link.closer
			link.closer = func() { old(); ex.close() }
		}
		return link, err
	case "doh":
		ex, err := newDoHExchange(cfg)
		if err != nil {
			return nil, err
		}
		return dialPoll(cfg, ex.do)
	case "dot":
		ex, err := newDoTExchange(cfg)
		if err != nil {
			return nil, err
		}
		link, err := dialPoll(cfg, ex.do)
		if link != nil {
			old := link.closer
			link.closer = func() { old(); ex.close() }
		}
		return link, err
	case "oss":
		ex, err := newOSSExchange(cfg)
		if err != nil {
			return nil, err
		}
		sid := randomBytes(8)
		ex.setSID(sid)
		// OSS 的 sid 由服务端在 HELLO 中返回？这里使用本地 sid 作为对象路径；
		// 服务端按 HELLO 包内的 sid 关联，无需一致对象前缀。重新生成握手。
		link, err := dialPollOSS(cfg, ex)
		return link, err
	}
	return nil, fmt.Errorf("unsupported mode %s", cfg.Mode)
}

// dialPollOSS is dialPoll with the OSS sid known before HELLO.
func dialPollOSS(cfg *Config, ex *ossExchange) (*transportLink, error) {
	master := deriveMaster(cfg.VKey, cfg.Salt)
	sid, hello := makeHello(master)
	ex.setSID(sid)
	sidInt := binary.BigEndian.Uint64(sid)
	helloPkt := packPacket(sidInt, pktHello, 0, hello[12:])
	deadline := time.Now().Add(12 * time.Second)
	var ack []byte
	for time.Now().Before(deadline) {
		resp, err := ex.do(helloPkt)
		if err != nil {
			log.Printf("poll hello: %v", err)
		} else if len(resp) >= pktHeader {
			psid, typ, _, body, ok := unpackPacket(resp)
			if ok && psid == sidInt && typ == pktHelloAck && len(body) >= 48 {
				ack = body
				break
			}
			log.Printf("poll hello resp typ=%d sid=%x len=%d", typ, psid, len(resp))
		} else {
			log.Printf("poll hello short resp %d", len(resp))
		}
		time.Sleep(250 * time.Millisecond)
	}
	if ack == nil {
		return nil, errors.New("oss handshake timeout")
	}
	if !hmacEqual(ack[16:48], handshakeMAC(master, sid, ack[:16], []byte("server"))) {
		return nil, errors.New("oss hello ack mismatch")
	}
	enc, dec, err := newCodecs(master, sid, cfg)
	if err != nil {
		return nil, err
	}
	a := newARQ(sidInt, 1, 350*time.Millisecond)
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(time.Duration(cfg.PollMs) * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case now := <-ticker.C:
				pkt := a.take(now)
				if pkt == nil {
					pkt = a.ackPacket()
				}
				resp, err := ex.do(pkt)
				if err == nil && len(resp) > 0 {
					a.onPacket(resp)
				}
			}
		}
	}()
	ch := &Channel{enc: enc, dec: dec, maxData: cfg.recordSize()}
	ch.send = func(frame []byte) error { a.queue(frame); return nil }
	ch.recv = func() ([]byte, error) {
		rec, ok := a.read()
		if !ok {
			return nil, io.EOF
		}
		return rec, nil
	}
	return &transportLink{ch: ch, closer: func() {
		close(done)
		ex.close()
		a.close()
	}}, nil
}
