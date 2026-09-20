package main

import "unicode/utf8"

// keyKind enumerates the keys the reader reacts to. keyNone is an internal sentinel
// (unrecognized/partial escape) that the decode loop drops.
type keyKind int

const (
	keyNone keyKind = iota
	keyRune
	keyUp
	keyDown
	keyLeft
	keyRight
	keyPgUp
	keyPgDn
	keyHome
	keyEnd
	keyEnter
	keyEsc
	keyBackspace
	keyTab
	keyQuit // Ctrl-C / Ctrl-D
)

type keyEvent struct {
	kind keyKind
	r    rune // valid when kind == keyRune
}

// decode parses a chunk of bytes read from a raw-mode terminal into key events.
// A single read may hold a whole escape sequence (arrow keys arrive as ESC [ A, etc.),
// several keystrokes (paste), or one rune. This is a pure function — see input_test.go.
func decode(b []byte) []keyEvent {
	var evs []keyEvent
	for i, n := 0, len(b); i < n; {
		c := b[i]
		switch {
		case c == 0x03, c == 0x04: // Ctrl-C, Ctrl-D
			evs = append(evs, keyEvent{kind: keyQuit})
			i++
		case c == '\r', c == '\n':
			evs = append(evs, keyEvent{kind: keyEnter})
			i++
		case c == '\t':
			evs = append(evs, keyEvent{kind: keyTab})
			i++
		case c == 0x7f, c == 0x08:
			evs = append(evs, keyEvent{kind: keyBackspace})
			i++
		case c == 0x1b: // ESC — possibly a CSI/SS3 sequence
			ev, adv := decodeEsc(b[i:])
			if ev.kind != keyNone {
				evs = append(evs, ev)
			}
			if adv < 1 {
				adv = 1
			}
			i += adv
		case c < 0x20: // other control bytes — ignore
			i++
		default:
			r, size := utf8.DecodeRune(b[i:])
			if r == utf8.RuneError && size <= 1 {
				i++ // skip a stray/invalid byte
				continue
			}
			evs = append(evs, keyEvent{kind: keyRune, r: r})
			i += size
		}
	}
	return evs
}

// decodeEsc parses an escape sequence beginning at b[0]==0x1b, returning the event and
// the number of bytes consumed. A bare ESC (nothing after) is keyEsc. Unrecognized CSI
// sequences are consumed through their final byte and reported as keyNone (dropped).
func decodeEsc(b []byte) (keyEvent, int) {
	if len(b) == 1 {
		return keyEvent{kind: keyEsc}, 1
	}
	if b[1] != '[' && b[1] != 'O' {
		// ESC followed by a normal byte: treat ESC as standalone; the next byte decodes on its own.
		return keyEvent{kind: keyEsc}, 1
	}
	// CSI (ESC [ ...) or SS3 (ESC O ...)
	if len(b) >= 3 {
		switch b[2] {
		case 'A':
			return keyEvent{kind: keyUp}, 3
		case 'B':
			return keyEvent{kind: keyDown}, 3
		case 'C':
			return keyEvent{kind: keyRight}, 3
		case 'D':
			return keyEvent{kind: keyLeft}, 3
		case 'H':
			return keyEvent{kind: keyHome}, 3
		case 'F':
			return keyEvent{kind: keyEnd}, 3
		case '1', '4', '5', '6': // ESC [ N ~  -> Home/End/PgUp/PgDn
			if len(b) >= 4 && b[3] == '~' {
				switch b[2] {
				case '1':
					return keyEvent{kind: keyHome}, 4
				case '4':
					return keyEvent{kind: keyEnd}, 4
				case '5':
					return keyEvent{kind: keyPgUp}, 4
				case '6':
					return keyEvent{kind: keyPgDn}, 4
				}
			}
		}
	}
	// Unrecognized/partial CSI: consume through the final byte (0x40..0x7e) and drop it.
	j := 2
	for j < len(b) && !(b[j] >= 0x40 && b[j] <= 0x7e) {
		j++
	}
	if j < len(b) {
		j++ // include the final byte
	}
	return keyEvent{kind: keyNone}, j
}
