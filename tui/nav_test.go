package main

import "testing"

// TestBlockNavRollover verifies ←/→ walk blocks within a record and roll across records at
// the ends: → past the last block goes to the next record's first block, ← past the first
// goes to the previous record's last block; the very ends clamp.
func TestBlockNavRollover(t *testing.T) {
	r0 := Record{Endpoint: "chat.completions", Completion: "yo",
		Request: []byte(`{"messages":[{"role":"user","content":"hi"}]}`)} // -> Prompt, Completion, Request Body
	r1 := Record{Endpoint: "chat.completions", Completion: "bye"} // -> Completion
	u := newUI([]Record{r0, r1}, "")
	u.openDetail() // record 0, block 0

	if len(u.blocks) != 3 {
		t.Fatalf("record 0 blocks = %d, want 3 (Prompt/Completion/Request Body)", len(u.blocks))
	}

	// forward through record 0's blocks
	u.moveBlock(1)
	u.moveBlock(1)
	if u.rowCursor != 0 || u.blockCursor != 2 {
		t.Fatalf("after 2×→: row=%d block=%d, want 0/2", u.rowCursor, u.blockCursor)
	}
	// → past the last block -> next record, first block
	u.moveBlock(1)
	if u.rowCursor != 1 || u.blockCursor != 0 || len(u.blocks) != 1 {
		t.Fatalf("→ rollover: row=%d block=%d nblocks=%d, want 1/0/1", u.rowCursor, u.blockCursor, len(u.blocks))
	}
	// → at the very end clamps
	u.moveBlock(1)
	if u.rowCursor != 1 || u.blockCursor != 0 {
		t.Fatalf("→ at end should clamp: row=%d block=%d", u.rowCursor, u.blockCursor)
	}
	// ← past the first block -> previous record, LAST block
	u.moveBlock(-1)
	if u.rowCursor != 0 || u.blockCursor != 2 {
		t.Fatalf("← rollback: row=%d block=%d, want 0/2", u.rowCursor, u.blockCursor)
	}
	// ← to the very start then clamp
	u.moveBlock(-1)
	u.moveBlock(-1)
	if u.rowCursor != 0 || u.blockCursor != 0 {
		t.Fatalf("expected 0/0 at start, got %d/%d", u.rowCursor, u.blockCursor)
	}
	u.moveBlock(-1)
	if u.rowCursor != 0 || u.blockCursor != 0 {
		t.Fatalf("← at start should clamp: row=%d block=%d", u.rowCursor, u.blockCursor)
	}
}
