package main

// Terminal handling for the read-only reader. The single genuinely error-prone part
// (putting the terminal into raw mode and restoring it) is delegated to golang.org/x/term
// (Go-team maintained); everything else in this binary is pure stdlib + our own logic.

import (
	"os"

	"golang.org/x/term"
)

// Screen-control escape sequences (VT100 / xterm), emitted directly — no dependency needed.
const (
	altScreenOn  = "\033[?1049h" // enter alternate screen buffer
	altScreenOff = "\033[?1049l" // leave it (restores the user's scrollback)
	cursorHide   = "\033[?25l"
	cursorShow   = "\033[?25h"
	clearHome    = "\033[H\033[2J" // cursor home + clear screen
)

// rawMode puts fd into raw mode and returns the prior state for restore().
func rawMode(fd int) (*term.State, error) { return term.MakeRaw(fd) }

// restore returns the terminal fd to a previously saved state (no-op on nil).
func restore(fd int, st *term.State) {
	if st != nil {
		_ = term.Restore(fd, st)
	}
}

// termSize returns fd's size in cells, falling back to 80x24 (pipes, detached ttys).
func termSize(fd int) (w, h int) {
	w, h, err := term.GetSize(fd)
	if err != nil || w <= 0 || h <= 0 {
		return 80, 24
	}
	return w, h
}

// isTTY reports whether fd is a terminal.
func isTTY(fd int) bool { return term.IsTerminal(fd) }

// stdinFD / stdoutFD are the descriptors the reader drives.
func stdinFD() int  { return int(os.Stdin.Fd()) }
func stdoutFD() int { return int(os.Stdout.Fd()) }
