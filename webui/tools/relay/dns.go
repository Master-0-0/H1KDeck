package main

import (
	"encoding/base32"
	"encoding/binary"
	"errors"
	"strings"
)

var b32 = base32.StdEncoding.WithPadding(base32.NoPadding)

func b32encode(data []byte) string {
	return b32.EncodeToString(data)
}

func b32decode(text string) ([]byte, error) {
	return b32.DecodeString(strings.ToUpper(strings.TrimSpace(text)))
}

func splitLabels(text string, size int) []string {
	if text == "" {
		return []string{"0"}
	}
	var out []string
	for len(text) > size {
		out = append(out, text[:size])
		text = text[size:]
	}
	out = append(out, text)
	return out
}

// buildDNSQuery builds a minimal DNS query with one TXT question.
func buildDNSQuery(qname string, id uint16) []byte {
	buf := make([]byte, 12)
	binary.BigEndian.PutUint16(buf[0:], id)
	binary.BigEndian.PutUint16(buf[2:], 0x0100) // RD
	binary.BigEndian.PutUint16(buf[4:], 1)      // QDCOUNT
	for _, label := range strings.Split(strings.Trim(qname, "."), ".") {
		if len(label) > 63 {
			label = label[:63]
		}
		buf = append(buf, byte(len(label)))
		buf = append(buf, label...)
	}
	buf = append(buf, 0)
	buf = append(buf, 0, 16, 0, 1) // TXT IN
	return buf
}

func skipName(msg []byte, off int) int {
	for off < len(msg) {
		l := int(msg[off])
		if l == 0 {
			return off + 1
		}
		if l&0xc0 == 0xc0 {
			return off + 2
		}
		off += 1 + l
	}
	return off
}

// parseDNSTXT extracts and concatenates every TXT string in the answer
// section, then base32-decodes the result.
func parseDNSTXT(msg []byte) ([]byte, error) {
	if len(msg) < 12 {
		return nil, errors.New("short dns message")
	}
	qd := int(binary.BigEndian.Uint16(msg[4:6]))
	an := int(binary.BigEndian.Uint16(msg[6:8]))
	off := 12
	for i := 0; i < qd; i++ {
		off = skipName(msg, off)
		off += 4
		if off > len(msg) {
			return nil, errors.New("bad question")
		}
	}
	var text strings.Builder
	for i := 0; i < an; i++ {
		off = skipName(msg, off)
		if off+10 > len(msg) {
			break
		}
		typ := binary.BigEndian.Uint16(msg[off:])
		rdlen := int(binary.BigEndian.Uint16(msg[off+8:]))
		off += 10
		if off+rdlen > len(msg) {
			break
		}
		if typ == 16 {
			p := off
			end := off + rdlen
			for p < end {
				l := int(msg[p])
				p++
				if p+l > end {
					break
				}
				text.Write(msg[p : p+l])
				p += l
			}
		}
		off += rdlen
	}
	str := text.String()
	if str == "" {
		return []byte{}, nil
	}
	return b32decode(str)
}

// buildTXTQuery carries the packet in an additional TXT record, so the
// QNAME stays short (DoH/DoT can move ~1 KB per message).
func buildTXTQuery(packet []byte, domain string) []byte {
	name := "a"
	if d := strings.Trim(domain, "."); d != "" {
		name = "a." + d
	}
	buf := make([]byte, 12)
	binary.BigEndian.PutUint16(buf[4:], 1) // QDCOUNT
	binary.BigEndian.PutUint16(buf[10:], 1) // ARCOUNT
	for _, label := range strings.Split(name, ".") {
		buf = append(buf, byte(len(label)))
		buf = append(buf, label...)
	}
	buf = append(buf, 0, 0, 16, 0, 1)

	text := b32encode(packet)
	var rdata []byte
	for len(text) > 0 {
		n := len(text)
		if n > 250 {
			n = 250
		}
		rdata = append(rdata, byte(n))
		rdata = append(rdata, text[:n]...)
		text = text[n:]
	}
	buf = append(buf, 0xc0, 0x0c, 0, 16, 0, 1, 0, 0, 0, 0,
		byte(len(rdata)>>8), byte(len(rdata)))
	buf = append(buf, rdata...)
	return buf
}
