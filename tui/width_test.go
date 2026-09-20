package main

import (
	"strings"
	"testing"
)

func TestRuneCellWidth(t *testing.T) {
	cases := []struct {
		r rune
		w int
	}{
		{'A', 1}, {' ', 1}, {'é', 1}, {'~', 1},
		{'中', 2}, {'あ', 2}, {'한', 2},
		{'Ａ', 2}, // fullwidth 'A'
		{'　', 2}, // ideographic (fullwidth) space
	}
	for _, c := range cases {
		if got := runeCellWidth(c.r); got != c.w {
			t.Errorf("runeCellWidth(%q) = %d, want %d", c.r, got, c.w)
		}
	}
}

func TestDispWidthWideAndSGR(t *testing.T) {
	if got := dispWidth("A中B"); got != 4 { // 1 + 2 + 1
		t.Errorf("dispWidth(A中B) = %d, want 4", got)
	}
	if got := dispWidth(sgr("中", cRed)); got != 2 { // SGR escapes don't count
		t.Errorf("dispWidth(colored 中) = %d, want 2", got)
	}
}

func TestTruncDispWide(t *testing.T) {
	// "A中B中" is 6 cells; truncating to 3 must keep "A中" (3) and not split the wide rune.
	got := truncDisp("A中B中", 3)
	if dispWidth(got) > 3 {
		t.Fatalf("truncDisp width %d > 3: %q", dispWidth(got), got)
	}
	if got != "A中" {
		t.Fatalf("truncDisp(A中B中,3) = %q, want A中", got)
	}
}

func TestWrapLinesRespectsCells(t *testing.T) {
	// 60 CJK chars = 120 cells; at width 40 every wrapped line must be <= 40 cells.
	line := strings.Repeat("中", 60)
	wl := wrapLines(line, 40)
	if len(wl) < 3 {
		t.Fatalf("expected >=3 wrapped lines for 120 cells at width 40, got %d", len(wl))
	}
	for _, l := range wl {
		if dispWidth(l) > 40 {
			t.Fatalf("wrapped line width %d > 40: %q", dispWidth(l), l)
		}
	}
	// Rejoining the wrapped pieces must reproduce the original (no runes lost).
	if strings.Join(wl, "") != line {
		t.Fatal("wrapLines lost or added runes")
	}
}
