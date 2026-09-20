package main

import (
	"bufio"
	"encoding/json"
	"os"
)

// loadRun reads a plane1.jsonl file fully into memory: one Record per line. Malformed
// lines are skipped (the reader is read-only and tolerant). This is a one-shot snapshot —
// live tailing is the rich add-on's job.
func loadRun(path string) ([]Record, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var recs []Record
	sc := bufio.NewScanner(f)
	// plane1 lines can be large (full request bodies); raise the scanner limit to 32 MiB.
	sc.Buffer(make([]byte, 0, 64*1024), 32*1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var r Record
		if json.Unmarshal(line, &r) != nil {
			continue // tolerate a malformed line rather than aborting the whole run
		}
		recs = append(recs, r)
	}
	if err := sc.Err(); err != nil {
		return recs, err
	}
	return recs, nil
}
