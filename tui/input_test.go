package main

import "testing"

func TestDecode(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want []keyEvent
	}{
		{"rune", "a", []keyEvent{{keyRune, 'a'}}},
		{"two runes", "jk", []keyEvent{{keyRune, 'j'}, {keyRune, 'k'}}},
		{"multibyte", "é", []keyEvent{{keyRune, 'é'}}},
		{"enter cr", "\r", []keyEvent{{keyEnter, 0}}},
		{"enter lf", "\n", []keyEvent{{keyEnter, 0}}},
		{"tab", "\t", []keyEvent{{keyTab, 0}}},
		{"backspace del", "\x7f", []keyEvent{{keyBackspace, 0}}},
		{"ctrl-c", "\x03", []keyEvent{{keyQuit, 0}}},
		{"ctrl-d", "\x04", []keyEvent{{keyQuit, 0}}},
		{"bare esc", "\x1b", []keyEvent{{keyEsc, 0}}},
		{"up", "\x1b[A", []keyEvent{{keyUp, 0}}},
		{"down", "\x1b[B", []keyEvent{{keyDown, 0}}},
		{"right", "\x1b[C", []keyEvent{{keyRight, 0}}},
		{"left", "\x1b[D", []keyEvent{{keyLeft, 0}}},
		{"home csi", "\x1b[H", []keyEvent{{keyHome, 0}}},
		{"end csi", "\x1b[F", []keyEvent{{keyEnd, 0}}},
		{"home tilde", "\x1b[1~", []keyEvent{{keyHome, 0}}},
		{"end tilde", "\x1b[4~", []keyEvent{{keyEnd, 0}}},
		{"pgup", "\x1b[5~", []keyEvent{{keyPgUp, 0}}},
		{"pgdn", "\x1b[6~", []keyEvent{{keyPgDn, 0}}},
		{"ss3 up", "\x1bOA", []keyEvent{{keyUp, 0}}},
		{"two arrows", "\x1b[A\x1b[B", []keyEvent{{keyUp, 0}, {keyDown, 0}}},
		{"arrow then rune", "\x1b[Aq", []keyEvent{{keyUp, 0}, {keyRune, 'q'}}},
		{"unknown csi dropped", "\x1b[3~", nil}, // Delete: unhandled -> dropped
		{"unknown csi then rune", "\x1b[3~x", []keyEvent{{keyRune, 'x'}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := decode([]byte(c.in))
			if len(got) != len(c.want) {
				t.Fatalf("decode(%q) = %v, want %v", c.in, got, c.want)
			}
			for i := range got {
				if got[i] != c.want[i] {
					t.Fatalf("decode(%q)[%d] = %v, want %v", c.in, i, got[i], c.want[i])
				}
			}
		})
	}
}

func TestDecodeNeverPanics(t *testing.T) {
	// A partial/garbled escape sequence must not panic or loop.
	for _, s := range []string{"\x1b[", "\x1b[5", "\x1bO", "\x1b[999999999~", "\x1b]", "\xff\xfe"} {
		_ = decode([]byte(s))
	}
}
