package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// saveClip writes a block's raw text to a datetime-stamped file under the audit root's
// clips/ dir ($AALGT_AUDIT_ROOT/clips, default ~/.local/state/aalegate/clips) and returns
// the path. It's a scratch space for pulling a prompt / completion / tool-call out of a run
// without touching the recorded plane files. Distinct from `c` (clipboard): `s` always
// persists to disk.
func saveClip(r Record, b block) (string, error) {
	root := auditRootDir()
	if root == "" {
		return "", fmt.Errorf("no audit root ($AALGT_AUDIT_ROOT unset and no home dir)")
	}
	dir := filepath.Join(root, "clips")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	path := filepath.Join(dir, clipFileName(r.Seq, b.title, time.Now().Format("20060102-150405.000")))
	if err := os.WriteFile(path, []byte(b.raw), 0o644); err != nil {
		return "", err
	}
	return path, nil
}

// clipFileName builds "<stamp>-rec<seq>-<sanitized title>.txt".
func clipFileName(seq uint64, title, stamp string) string {
	return fmt.Sprintf("%s-rec%d-%s.txt", stamp, seq, sanitizeName(title))
}

// sanitizeName reduces a block title to a filename-safe token (letters/digits/'-' kept,
// everything else -> '_'), capped at 40 chars.
func sanitizeName(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
	}
	name := b.String()
	if len(name) > 40 {
		name = name[:40]
	}
	name = strings.Trim(name, "_")
	if name == "" {
		name = "block"
	}
	return name
}
