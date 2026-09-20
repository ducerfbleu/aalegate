package main

import (
	"encoding/base64"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

type copyResult struct {
	method string
	err    error
}

func copyToClipboard(text string) copyResult {
	// 1) native clipboard tools — reliable when a display server is reachable (local session).
	for _, tool := range []struct {
		name string
		args []string
	}{
		{"wl-copy", nil},
		{"xclip", []string{"-selection", "clipboard"}},
		{"xsel", []string{"--clipboard", "--input"}},
		{"pbcopy", nil},
	} {
		path, err := exec.LookPath(tool.name)
		if err != nil {
			continue
		}
		cmd := exec.Command(path, tool.args...)
		cmd.Stdin = strings.NewReader(text)
		if err := cmd.Run(); err == nil {
			return copyResult{method: tool.name}
		}
		// installed but failed (e.g. no $DISPLAY/$WAYLAND_DISPLAY over SSH) -> try the next.
	}

	// 2) OSC 52 — hand the text to the terminal emulator's own clipboard. Needs no display
	//    server, so it works over SSH / headless / tmux. Written to /dev/tty (the controlling
	//    terminal) so it reaches the emulator even mid-alt-screen.
	if f, err := os.OpenFile("/dev/tty", os.O_WRONLY, 0); err == nil {
		_, werr := io.WriteString(f, osc52(text))
		_ = f.Close()
		if werr == nil {
			return copyResult{method: "osc52"}
		}
	}

	// 3) last resort: write to a temp file.
	dir := os.TempDir()
	name := "aalegate-copy-" + time.Now().Format("20060102150405.000") + ".txt"
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		return copyResult{err: fmt.Errorf("no clipboard available and file write failed: %w", err)}
	}
	return copyResult{method: "file:" + path}
}

// osc52 builds an OSC 52 "set clipboard" sequence for text: ESC ] 52 ; c ; <base64> BEL.
// Inside tmux ($TMUX set) it is wrapped in tmux's DCS passthrough — with every ESC in the
// payload doubled — so tmux forwards it to the outer terminal. That path requires
// `set -g allow-passthrough on` in tmux 3.3+ (or `set -g set-clipboard on`, which lets tmux
// interpret a raw OSC 52 itself). The outer terminal must support OSC 52 (iTerm2, kitty,
// WezTerm, foot, Ghostty, Windows Terminal, …).
func osc52(text string) string {
	seq := "\033]52;c;" + base64.StdEncoding.EncodeToString([]byte(text)) + "\007"
	if os.Getenv("TMUX") != "" {
		return "\033Ptmux;" + strings.ReplaceAll(seq, "\033", "\033\033") + "\033\\"
	}
	return seq
}

func (r copyResult) message() string {
	switch {
	case r.err != nil:
		return "copy failed: " + r.err.Error()
	case r.method == "osc52":
		return "copied to clipboard (OSC 52)"
	case strings.HasPrefix(r.method, "file:"):
		return "saved to " + strings.TrimPrefix(r.method, "file:")
	default:
		return "copied to clipboard (" + r.method + ")"
	}
}
