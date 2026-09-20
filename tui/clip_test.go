package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSanitizeName(t *testing.T) {
	cases := map[string]string{
		"Completion":             "Completion",
		"Request Body":           "Request_Body",
		"Tool Call 1: read_file": "Tool_Call_1__read_file",
		"":                       "block",
		"   ":                    "block",
	}
	for in, want := range cases {
		if got := sanitizeName(in); got != want {
			t.Errorf("sanitizeName(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestClipFileName(t *testing.T) {
	got := clipFileName(3, "Completion", "20260919-153045.123")
	want := "20260919-153045.123-rec3-Completion.txt"
	if got != want {
		t.Fatalf("clipFileName = %q, want %q", got, want)
	}
}

func TestSaveClipWrites(t *testing.T) {
	root := "clip-test-tmp"
	t.Setenv("AALGT_AUDIT_ROOT", root)
	defer os.RemoveAll(root)

	path, err := saveClip(Record{Seq: 7}, block{title: "Completion", raw: "hello clip"})
	if err != nil {
		t.Fatalf("saveClip: %v", err)
	}
	if !strings.HasPrefix(path, filepath.Join(root, "clips")+string(os.PathSeparator)) {
		t.Fatalf("clip path %q not under %s/clips", path, root)
	}
	if !strings.HasSuffix(path, "-rec7-Completion.txt") {
		t.Fatalf("clip name unexpected: %q", path)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "hello clip" {
		t.Fatalf("clip content = %q, err=%v", string(data), err)
	}
}
