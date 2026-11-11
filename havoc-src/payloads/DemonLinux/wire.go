package main

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"unicode/utf16"
)

// ---------------------------------------------------------------------------
// 密码学：Havoc Demon 使用 AES-256-CTR，每个包都从同一个 (key, iv) 重新初始化。
// ---------------------------------------------------------------------------

func aesCTR(key, iv, data []byte) []byte {
	block, err := aes.NewCipher(key)
	if err != nil {
		panic(err)
	}
	out := make([]byte, len(data))
	cipher.NewCTR(block, iv).XORKeyStream(out, data)
	return out
}

func randomBytes(n int) []byte {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return b
}

// ---------------------------------------------------------------------------
// 打包器：agent -> teamserver 的整数为大端（与 C Demon 的 Int32ToBuffer 一致）。
// ---------------------------------------------------------------------------

type packer struct {
	b []byte
}

func newPacker() *packer { return &packer{} }

func (p *packer) u32(v uint32) *packer {
	var tmp [4]byte
	binary.BigEndian.PutUint32(tmp[:], v)
	p.b = append(p.b, tmp[:]...)
	return p
}

func (p *packer) i32(v int32) *packer { return p.u32(uint32(v)) }

func (p *packer) i64(v int64) *packer {
	var tmp [8]byte
	binary.BigEndian.PutUint64(tmp[:], uint64(v))
	p.b = append(p.b, tmp[:]...)
	return p
}

func (p *packer) u64(v uint64) *packer {
	var tmp [8]byte
	binary.BigEndian.PutUint64(tmp[:], v)
	p.b = append(p.b, tmp[:]...)
	return p
}

func (p *packer) boolean(v bool) *packer {
	if v {
		return p.i32(1)
	}
	return p.i32(0)
}

func (p *packer) bytes(b []byte) *packer {
	p.u32(uint32(len(b)))
	p.b = append(p.b, b...)
	return p
}

// str 写入原始 UTF-8 字节（C 侧为 PackageAddString / PackageAddBytes）。
func (p *packer) str(s []byte) *packer { return p.bytes(s) }

func (p *packer) text(s string) *packer { return p.bytes([]byte(s)) }

// wstr 写入 UTF-16LE 字节（不追加 null，C 侧 PackageAddWString）。
func (p *packer) wstr(s string) *packer { return p.bytes(utf16le(s)) }

func utf16le(s string) []byte {
	u := utf16.Encode([]rune(s))
	out := make([]byte, 0, len(u)*2)
	for _, r := range u {
		out = append(out, byte(r), byte(r>>8))
	}
	return out
}

func fromUTF16(b []byte) string {
	if len(b) < 2 {
		return ""
	}
	u := make([]uint16, 0, len(b)/2)
	for i := 0; i+1 < len(b); i += 2 {
		u = append(u, binary.LittleEndian.Uint16(b[i:i+2]))
	}
	// 去掉 C 字符串末尾的 null
	for len(u) > 0 && u[len(u)-1] == 0 {
		u = u[:len(u)-1]
	}
	return string(utf16.Decode(u))
}

// ---------------------------------------------------------------------------
// 解析器：teamserver -> agent 的任务包为小端（与 BuildPayloadMessage 一致）。
// ---------------------------------------------------------------------------

type parser struct {
	b   []byte
	pos int
}

func newParser(b []byte) *parser { return &parser{b: b} }

func (p *parser) remaining() int { return len(p.b) - p.pos }

func (p *parser) byte() byte {
	if p.remaining() < 1 {
		p.pos = len(p.b)
		return 0
	}
	v := p.b[p.pos]
	p.pos++
	return v
}

func (p *parser) u16() uint16 {
	if p.remaining() < 2 {
		p.pos = len(p.b)
		return 0
	}
	v := binary.LittleEndian.Uint16(p.b[p.pos:])
	p.pos += 2
	return v
}

func (p *parser) i32() int32 {
	if p.remaining() < 4 {
		p.pos = len(p.b)
		return 0
	}
	v := int32(binary.LittleEndian.Uint32(p.b[p.pos:]))
	p.pos += 4
	return v
}

func (p *parser) i64() int64 {
	if p.remaining() < 8 {
		p.pos = len(p.b)
		return 0
	}
	v := int64(binary.LittleEndian.Uint64(p.b[p.pos:]))
	p.pos += 8
	return v
}

func (p *parser) boolean() bool { return p.i32() != 0 }

func (p *parser) bytes() []byte {
	if p.remaining() < 4 {
		p.pos = len(p.b)
		return nil
	}
	n := int(binary.LittleEndian.Uint32(p.b[p.pos:]))
	p.pos += 4
	if n < 0 || p.pos+n > len(p.b) {
		n = len(p.b) - p.pos
	}
	out := p.b[p.pos : p.pos+n]
	p.pos += n
	return out
}

func (p *parser) text() string { return string(bytes.Trim(p.bytes(), "\x00")) }

func (p *parser) wstr() string { return fromUTF16(p.bytes()) }

func itoa(v int) string { return fmt.Sprintf("%d", v) }
