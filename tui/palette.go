package main

import (
	"strings"
	"unicode/utf8"

	"golang.org/x/text/width"
)

// Attribute SGR codes (palette-independent).
const (
	sgrBold    = "1"
	sgrItalic  = "3"
	sgrUnder   = "4"
	sgrReverse = "7"

	sgrReset = "\033[0m"
)

// palette maps the reader's seven semantic color roles to SGR color codes (256-color, or
// truecolor "38;2;r;g;b"). Themes are dependency-free — just code strings.
type palette struct {
	green, yellow, red, dim, accent, white, border string
}

// Active color codes. setPalette copies the chosen palette into these package vars, so the
// renderer/highlighter keep referencing cGreen…cBorder with no per-call lookup — selecting a
// theme is one assignment. init() sets the default so they're never empty (tests included).
var (
	cGreen  string
	cYellow string
	cRed    string
	cDim    string
	cAccent string
	cWhite  string
	cBorder string
)

// themeOrder is the display order for -theme help / $AALE_TUI_THEME.
var themeOrder = []string{"nocturnal", "dracula", "gruvbox", "nord", "solarized", "ansi"}

// palettes: nocturnal is the original look (unchanged); the four popular schemes are
// 256-color approximations chosen to read well under tmux-256color; ansi uses the base 16
// ANSI colors so the terminal's own scheme decides the hues (a "match my terminal" theme).
var palettes = map[string]palette{
	"nocturnal": {green: "38;5;46", yellow: "38;5;226", red: "38;5;196", dim: "38;5;243", accent: "38;5;75", white: "38;5;255", border: "38;5;238"},
	"dracula":   {green: "38;5;84", yellow: "38;5;228", red: "38;5;203", dim: "38;5;61", accent: "38;5;141", white: "38;5;255", border: "38;5;238"},
	"gruvbox":   {green: "38;5;142", yellow: "38;5;214", red: "38;5;167", dim: "38;5;245", accent: "38;5;208", white: "38;5;223", border: "38;5;239"},
	"nord":      {green: "38;5;108", yellow: "38;5;222", red: "38;5;131", dim: "38;5;60", accent: "38;5;110", white: "38;5;253", border: "38;5;238"},
	"solarized": {green: "38;5;100", yellow: "38;5;136", red: "38;5;160", dim: "38;5;240", accent: "38;5;33", white: "38;5;247", border: "38;5;235"},
	// ansi: base 16-color SGR — the terminal's palette defines the hues. white=39 (default fg).
	"ansi": {green: "32", yellow: "33", red: "31", dim: "90", accent: "36", white: "39", border: "90"},
}

// setPalette activates the named theme, returning false if unknown (caller keeps the current).
func setPalette(name string) bool {
	p, ok := palettes[name]
	if !ok {
		return false
	}
	cGreen, cYellow, cRed, cDim, cAccent, cWhite, cBorder = p.green, p.yellow, p.red, p.dim, p.accent, p.white, p.border
	return true
}

// themeNames lists the available themes in display order.
func themeNames() []string { return themeOrder }

func init() { setPalette("nocturnal") }

// colorOff disables all SGR output (set from $NO_COLOR in main). Off by default.
var colorOff bool

// sgr wraps s in the given SGR codes plus a reset. With no codes — or when color is
// disabled — it returns s unchanged.
func sgr(s string, codes ...string) string {
	if colorOff || len(codes) == 0 {
		return s
	}
	return "\033[" + strings.Join(codes, ";") + "m" + s + sgrReset
}

// latencyANSI maps a latency in ms to a color code (mirrors style.go latencyColor).
func latencyANSI(ms int64) string {
	switch {
	case ms < 500:
		return cGreen
	case ms < 2000:
		return cYellow
	default:
		return cRed
	}
}

// runeCellWidth returns the terminal cell width of a rune: 2 for East-Asian Wide /
// Fullwidth (CJK, fullwidth forms), 1 otherwise. ASCII is fast-pathed. This uses the
// Go-team golang.org/x/text/width table — the same EastAsianWidth basis charm's stack
// uses; it covers CJK correctly (grapheme-cluster emoji width would need x/text-external
// segmentation, which we intentionally don't pull in).
func runeCellWidth(r rune) int {
	if r < 0x80 {
		return 1
	}
	switch width.LookupRune(r).Kind() {
	case width.EastAsianWide, width.EastAsianFullwidth:
		return 2
	default:
		return 1
	}
}

// dispWidth returns the printable cell width of s, skipping SGR escape sequences
// (\033[...m) and measuring wide runes as 2 cells.
func dispWidth(s string) int {
	w := 0
	for i := 0; i < len(s); {
		if s[i] == 0x1b {
			i = skipEsc(s, i)
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		if size < 1 {
			size = 1
		}
		w += runeCellWidth(r)
		i += size
	}
	return w
}

// skipEsc returns the index just past an escape sequence starting at s[i]==0x1b.
func skipEsc(s string, i int) int {
	j := i + 1
	if j < len(s) && s[j] == '[' {
		j++
		for j < len(s) && !(s[j] >= 0x40 && s[j] <= 0x7e) {
			j++
		}
		if j < len(s) {
			j++ // consume the final byte
		}
		return j
	}
	return i + 1
}

// padRight pads s with spaces to display width n (SGR-aware).
func padRight(s string, n int) string {
	if d := dispWidth(s); d < n {
		return s + strings.Repeat(" ", n-d)
	}
	return s
}

// padLeft right-aligns s to display width n (SGR-aware).
func padLeft(s string, n int) string {
	if d := dispWidth(s); d < n {
		return strings.Repeat(" ", n-d) + s
	}
	return s
}

// truncDisp truncates s to display width n, preserving SGR sequences and closing any
// still-open SGR with a reset. Escape bytes don't count toward n.
func truncDisp(s string, n int) string {
	if dispWidth(s) <= n {
		return s
	}
	var b strings.Builder
	w, open := 0, false
	for i := 0; i < len(s); {
		if s[i] == 0x1b {
			j := skipEsc(s, i)
			seq := s[i:j]
			b.WriteString(seq)
			open = seq != sgrReset
			i = j
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		if size < 1 {
			size = 1
		}
		rw := runeCellWidth(r)
		if w+rw > n {
			break
		}
		b.WriteString(s[i : i+size])
		w += rw
		i += size
	}
	if open {
		b.WriteString(sgrReset)
	}
	return b.String()
}
