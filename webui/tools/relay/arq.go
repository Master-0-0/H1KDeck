package main

import (
	"encoding/binary"
	"sync"
	"time"
)

const (
	pktHello    = 1
	pktHelloAck = 2
	pktData     = 3
	pktAck      = 4
	pktFin      = 5
	pktHeader   = 13
)

func packPacket(sid uint64, typ byte, seq uint32, body []byte) []byte {
	out := make([]byte, pktHeader+len(body))
	binary.BigEndian.PutUint64(out[0:8], sid)
	out[8] = typ
	binary.BigEndian.PutUint32(out[9:13], seq)
	copy(out[13:], body)
	return out
}

func unpackPacket(data []byte) (uint64, byte, uint32, []byte, bool) {
	if len(data) < pktHeader {
		return 0, 0, 0, nil, false
	}
	sid := binary.BigEndian.Uint64(data[0:8])
	typ := data[8]
	seq := binary.BigEndian.Uint32(data[9:13])
	return sid, typ, seq, data[13:], true
}

type unacked struct {
	pkt   []byte
	ts    time.Time
	tries int
}

// arq provides an ordered reliable byte-record stream over an unreliable
// datagram / request-response transport.
type arq struct {
	mu      sync.Mutex
	sid     uint64
	window  int
	rto     time.Duration
	txq     [][]byte
	unacked map[uint32]*unacked
	outbox  [][]byte
	nextSeq uint32
	rxNext  uint32
	rxBuf   map[uint32][]byte
	ack     uint32
	needAck bool
	out     chan []byte
	closed  bool
}

func newARQ(sid uint64, window int, rto time.Duration) *arq {
	if window < 1 {
		window = 1
	}
	return &arq{
		sid: sid, window: window, rto: rto,
		unacked: map[uint32]*unacked{},
		rxBuf:   map[uint32][]byte{},
		nextSeq: 1, rxNext: 1,
		out: make(chan []byte, 2048),
	}
}

func (a *arq) queue(record []byte) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed {
		return
	}
	cp := append([]byte(nil), record...)
	a.txq = append(a.txq, cp)
	a.fillLocked()
}

func (a *arq) fillLocked() {
	for !a.closed && len(a.unacked) < a.window && len(a.txq) > 0 {
		seq := a.nextSeq
		a.nextSeq++
		rec := a.txq[0]
		a.txq = a.txq[1:]
		pkt := packPacket(a.sid, pktData, seq, rec)
		a.unacked[seq] = &unacked{pkt: pkt, ts: time.Now()}
		a.outbox = append(a.outbox, pkt)
	}
}

func (a *arq) pollLocked(now time.Time) {
	if a.needAck {
		a.outbox = append(a.outbox, packPacket(a.sid, pktAck, a.ack, nil))
		a.needAck = false
	}
	for _, u := range a.unacked {
		backoff := a.rto * time.Duration(1<<minInt(u.tries, 3))
		if now.Sub(u.ts) >= backoff {
			u.ts = now
			u.tries++
			a.outbox = append(a.outbox, u.pkt)
		}
	}
}

func (a *arq) take(now time.Time) []byte {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.pollLocked(now)
	if len(a.outbox) == 0 {
		return nil
	}
	pkt := a.outbox[0]
	a.outbox = a.outbox[1:]
	return pkt
}

func (a *arq) onPacket(pkt []byte) {
	sid, typ, seq, body, ok := unpackPacket(pkt)
	if !ok || sid != a.sid {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed {
		return
	}
	switch typ {
	case pktData:
		if seq == a.rxNext {
			a.deliverLocked(body)
			a.rxNext++
			for {
				next, ok := a.rxBuf[a.rxNext]
				if !ok {
					break
				}
				delete(a.rxBuf, a.rxNext)
				a.deliverLocked(next)
				a.rxNext++
			}
		} else if seq > a.rxNext {
			if len(a.rxBuf) < 4096 {
				if _, exists := a.rxBuf[seq]; !exists {
					a.rxBuf[seq] = append([]byte(nil), body...)
				}
			}
		}
		a.ack = a.rxNext - 1
		a.needAck = true
	case pktAck:
		for k := range a.unacked {
			if k <= seq {
				delete(a.unacked, k)
			}
		}
		a.fillLocked()
	case pktFin:
		a.closeLocked()
	}
}

func (a *arq) deliverLocked(body []byte) {
	cp := append([]byte(nil), body...)
	select {
	case a.out <- cp:
	default:
	}
}

func (a *arq) read() ([]byte, bool) {
	rec, ok := <-a.out
	return rec, ok
}

func (a *arq) close() {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.closeLocked()
}

func (a *arq) closeLocked() {
	if a.closed {
		return
	}
	a.closed = true
	close(a.out)
}

func (a *arq) finPacket() []byte {
	return packPacket(a.sid, pktFin, 0, nil)
}

func (a *arq) ackPacket() []byte {
	a.mu.Lock()
	defer a.mu.Unlock()
	return packPacket(a.sid, pktAck, a.rxNext-1, nil)
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
