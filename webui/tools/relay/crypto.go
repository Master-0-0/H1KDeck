package main

import (
	"bytes"
	"compress/zlib"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"io"
)

var magic = []byte("HTP1")

const (
	handshakeLen = 4 + 8 + 16 + 32
	maxFrame     = 2 * 1024 * 1024
)

func hkdf(ikm, salt, info []byte, n int) []byte {
	if len(salt) == 0 {
		salt = make([]byte, 32)
	}
	prk := hmacSHA256(salt, ikm)
	var okm, block []byte
	for i := byte(1); len(okm) < n; i++ {
		mac := hmac.New(sha256.New, prk)
		mac.Write(block)
		mac.Write(info)
		mac.Write([]byte{i})
		block = mac.Sum(nil)
		okm = append(okm, block...)
	}
	return okm[:n]
}

func hmacSHA256(key, data []byte) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write(data)
	return mac.Sum(nil)
}

func deriveMaster(vkey, salt string) []byte {
	v := sha256.Sum256([]byte(vkey))
	s := sha256.Sum256([]byte(salt))
	return hkdf(v[:], s[:], []byte("havoc-transport-v1"), 32)
}

func sessionKeys(master, sid []byte) ([]byte, []byte) {
	return hkdf(master, sid, []byte("c2s"), 32), hkdf(master, sid, []byte("s2c"), 32)
}

func handshakeMAC(master, sid, nonce, role []byte) []byte {
	data := append([]byte("hello"), sid...)
	data = append(data, nonce...)
	data = append(data, role...)
	return hmacSHA256(master, data)
}

// RecordCodec mirrors the Python RecordCodec.
type RecordCodec struct {
	aead      cipher.AEAD
	direction byte
	compress  bool
	padding   int
	counter   uint64
}

func newRecordCodec(key []byte, direction byte, compress bool, padding int) (*RecordCodec, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	if padding < 0 {
		padding = 0
	}
	if padding > 255 {
		padding = 255
	}
	return &RecordCodec{aead: aead, direction: direction, compress: compress, padding: padding}, nil
}

func (c *RecordCodec) nonce() []byte {
	c.counter++
	n := make([]byte, 12)
	n[0] = c.direction
	binary.BigEndian.PutUint64(n[1:9], c.counter)
	return n
}

func (c *RecordCodec) encode(data []byte) ([]byte, error) {
	flags := byte(0)
	payload := data
	if c.compress && len(data) > 64 {
		var buf bytes.Buffer
		zw := zlib.NewWriter(&buf)
		if _, err := zw.Write(data); err == nil && zw.Close() == nil && buf.Len() < len(data) {
			payload = buf.Bytes()
			flags = 1
		}
	}
	pad := 0
	if c.padding > 0 {
		pad = randInt(c.padding + 1)
	}
	plain := make([]byte, 5, 5+len(payload)+pad)
	plain[0] = flags
	binary.BigEndian.PutUint32(plain[1:5], uint32(len(payload)))
	plain = append(plain, payload...)
	if pad > 0 {
		p := make([]byte, pad)
		_, _ = rand.Read(p)
		plain = append(plain, p...)
	}
	nonce := c.nonce()
	return append(nonce, c.aead.Seal(nil, nonce, plain, []byte("HTP1r"))...), nil
}

func (c *RecordCodec) decode(frame []byte) ([]byte, error) {
	if len(frame) < 12+16+5 {
		return nil, errors.New("frame too short")
	}
	nonce := frame[:12]
	plain, err := c.aead.Open(nil, nonce, frame[12:], []byte("HTP1r"))
	if err != nil {
		return nil, err
	}
	flags := plain[0]
	length := binary.BigEndian.Uint32(plain[1:5])
	if int(5+length) > len(plain) {
		return nil, errors.New("bad record length")
	}
	data := plain[5 : 5+length]
	if flags&1 != 0 {
		zr, err := zlib.NewReader(bytes.NewReader(data))
		if err != nil {
			return nil, err
		}
		out, err := io.ReadAll(zr)
		_ = zr.Close()
		if err != nil {
			return nil, err
		}
		return out, nil
	}
	return data, nil
}

func randInt(n int) int {
	if n <= 1 {
		return 0
	}
	var b [4]byte
	_, _ = rand.Read(b[:])
	return int(binary.BigEndian.Uint32(b[:]) % uint32(n))
}

func randomBytes(n int) []byte {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return b
}
