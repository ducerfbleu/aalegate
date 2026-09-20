package main

import (
	"fmt"
	"path/filepath"
	"strings"
	"unicode/utf8"
)

// Small pure helpers ported verbatim from the former model.go (which moved to the rich
// plugin). No third-party imports.

// shortModel reduces a model path/name to a compact label for the feed.
func shortModel(name string) string {
	name = filepath.Base(name)
	for _, suf := range []string{".gguf", ".bin"} {
		name = strings.TrimSuffix(name, suf)
	}
	if len(name) > 22 {
		name = name[:19] + "..."
	}
	return name
}

// fmtInt formats n with thousands separators.
func fmtInt(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	s := fmt.Sprintf("%d", n)
	if len(s) > 3 {
		var parts []string
		for len(s) > 3 {
			parts = append([]string{s[len(s)-3:]}, parts...)
			s = s[:len(s)-3]
		}
		parts = append([]string{s}, parts...)
		s = strings.Join(parts, ",")
	}
	if neg {
		return "-" + s
	}
	return s
}

// wrapLines soft-wraps text to a display width, breaking at spaces when possible. Operates
// on raw (uncolored) text — highlighters wrap first, then apply SGR — and measures in
// terminal cells (wide/CJK runes = 2) via runeCellWidth, so CJK content wraps correctly.
// Single forward pass: each chunk scan stops at `width` cells, so it is O(len(text)).
func wrapLines(text string, width int) []string {
	if width < 20 {
		width = 20
	}
	var out []string
	for _, line := range strings.Split(text, "\n") {
		for {
			cut, cw, lastSpace := 0, 0, -1
			fits := true
			for cut < len(line) {
				r, size := utf8.DecodeRuneInString(line[cut:])
				if size < 1 {
					size = 1
				}
				rw := runeCellWidth(r)
				if cw+rw > width {
					fits = false
					break
				}
				if r == ' ' {
					lastSpace = cut
				}
				cut += size
				cw += rw
			}
			if fits {
				out = append(out, line)
				break
			}
			// prefer a space break past a third of the width; else hard-break at `cut`
			if lastSpace > 0 && dispWidth(line[:lastSpace]) > width/3 {
				cut = lastSpace + 1
			}
			if cut == 0 { // safety: a single rune wider than width — never stall
				_, size := utf8.DecodeRuneInString(line)
				if size < 1 {
					size = 1
				}
				cut = size
			}
			out = append(out, line[:cut])
			line = line[cut:]
		}
	}
	return out
}

// truncStr truncates s to at most max bytes with an ellipsis.
func truncStr(s string, max int) string {
	if len(s) <= max {
		return s
	}
	if max < 3 {
		return s[:max]
	}
	return s[:max-3] + "..."
}
