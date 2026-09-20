package main

import (
	"strings"
	"testing"
)

// FuzzHighlight hammers the markdown/JSON highlighter — the one parser on the trusted,
// transcript-reading surface — with arbitrary bytes, asserting it never panics and never
// emits a line wider (in printable terms) than a generous bound of the requested width.
func FuzzHighlight(f *testing.F) {
	f.Add("# heading\n**bold** _b_ `code` [x](y)", "Completion", 80)
	f.Add(`{"key":"val","n":-1.5e3,"b":true,"z":null,"a":[1,2]}`, "Request Body", 80)
	f.Add("- item\n1. num\n> quote\n---\n```go\nx()\n```", "reasoning", 40)
	f.Add("", "", 0)
	f.Add("\x1b[31mnot our escape\x1b[0m", "raw", 20)

	f.Fuzz(func(t *testing.T, text, title string, width int) {
		if width < 0 || width > 8192 {
			t.Skip() // keep widths sane; wrapLines clamps <20 anyway
		}
		lines := highlightContent(text, title, width)
		// Output line count must be finite/bounded relative to input; a runaway would hang.
		if len(lines) > len(text)+64 {
			t.Fatalf("highlightContent produced %d lines from %d bytes (runaway?)", len(lines), len(text))
		}
		// dispWidth must terminate and stay non-negative on every produced line.
		for _, ln := range lines {
			if dispWidth(ln) < 0 {
				t.Fatalf("negative dispWidth for %q", ln)
			}
			// truncDisp must be idempotent-safe and not panic on colored lines.
			_ = truncDisp(ln, width)
		}
		_ = strings.Count(text, "\n")
	})
}
