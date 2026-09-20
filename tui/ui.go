package main

import (
	"fmt"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"
)

type pageID int

const (
	pageOverview pageID = iota
	pageStats
	pageDetail
)

const (
	overviewHints = "↑/↓ move · pgup/pgdn page · enter open · tab stats · q quit"
	statsHints    = "tab/esc back · q quit"
	detailHints   = "←/→ block/record · ↑/↓ scroll · c copy · s save · esc back"
)

// ui is the read-only reader: a synchronous state machine over an in-memory snapshot of a
// run. No goroutines drive the model (only a signal handler for terminal restore); it
// redraws solely in response to keystrokes.
type ui struct {
	records []Record // every record loaded
	recs    []Record // LLM turns only (what the overview lists / detail drills into)
	stats   Stats
	runID   string
	model   string
	path    string

	page        pageID
	rowCursor   int // index into recs (overview)
	rowScroll   int
	blocks      []block // current record's sections (detail)
	blockCursor int
	bodyScroll  int

	flash      string
	flashUntil time.Time

	w, h int
	fd   int
}

func newUI(records []Record, path string) *ui {
	u := &ui{records: records, path: path}
	u.recs = completionRecords(records)
	for i := range records {
		u.stats.Update(&records[i])
	}
	for _, r := range u.recs {
		if r.RunID != "" {
			u.runID = r.RunID
		}
		if r.Model != "" {
			u.model = shortModel(r.Model)
		}
	}
	return u
}

// Run drives the interactive reader. Returns a process exit code.
func (u *ui) Run() int {
	u.fd = stdinFD()
	if !isTTY(u.fd) || !isTTY(stdoutFD()) {
		return u.dumpNonTTY() // piped: emit a plain highlighted dump (great with `less -R`)
	}

	st, err := rawMode(u.fd)
	if err != nil {
		return u.dumpNonTTY()
	}
	var once sync.Once
	cleanup := func() {
		once.Do(func() {
			os.Stdout.WriteString(cursorShow + altScreenOff)
			restore(u.fd, st)
		})
	}
	defer cleanup()

	// Restore the terminal on external signals (Ctrl-C arrives as byte 0x03 in raw mode,
	// so this mainly covers SIGTERM/SIGHUP).
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	go func() {
		<-sigCh
		cleanup()
		os.Exit(130)
	}()

	// And on panic, so a bug never leaves the shell wedged.
	defer func() {
		if r := recover(); r != nil {
			cleanup()
			panic(r)
		}
	}()

	os.Stdout.WriteString(altScreenOn + cursorHide)
	u.w, u.h = termSize(u.fd)
	u.draw()

	buf := make([]byte, 64)
	for {
		n, err := os.Stdin.Read(buf)
		if err != nil {
			return 1
		}
		if n == 0 {
			continue
		}
		for _, ev := range decode(buf[:n]) {
			if u.handleKey(ev) {
				return 0
			}
		}
		u.draw()
	}
}

// ---- input handling ---------------------------------------------------------

func (u *ui) handleKey(ev keyEvent) (quit bool) {
	if ev.kind == keyQuit {
		return true
	}
	switch u.page {
	case pageOverview:
		return u.keyOverview(ev)
	case pageStats:
		u.keyStats(ev)
	case pageDetail:
		u.keyDetail(ev)
	}
	return false
}

func (u *ui) keyOverview(ev keyEvent) bool {
	switch ev.kind {
	case keyEsc:
		return true
	case keyUp:
		u.moveRow(-1)
	case keyDown:
		u.moveRow(1)
	case keyPgUp:
		u.moveRow(-u.feedViewport())
	case keyPgDn:
		u.moveRow(u.feedViewport())
	case keyHome:
		u.rowCursor = 0
	case keyEnd:
		u.moveRow(len(u.recs))
	case keyTab:
		u.page = pageStats
	case keyEnter:
		u.openDetail()
	case keyRune:
		switch ev.r {
		case 'q', 'Q':
			return true
		case 'k':
			u.moveRow(-1)
		case 'j':
			u.moveRow(1)
		case 'g':
			u.rowCursor = 0
		case 'G':
			u.moveRow(len(u.recs))
		}
	}
	return false
}

func (u *ui) keyStats(ev keyEvent) {
	if ev.kind == keyTab || ev.kind == keyEsc ||
		(ev.kind == keyRune && (ev.r == 'q' || ev.r == 'Q')) {
		u.page = pageOverview
	}
}

func (u *ui) keyDetail(ev keyEvent) {
	switch ev.kind {
	case keyEsc, keyEnter:
		u.page = pageOverview
	case keyLeft:
		u.moveBlock(-1)
	case keyRight:
		u.moveBlock(1)
	case keyUp:
		u.bodyScroll--
	case keyDown:
		u.bodyScroll++
	case keyPgUp:
		u.bodyScroll -= u.bodyViewport()
	case keyPgDn:
		u.bodyScroll += u.bodyViewport()
	case keyHome:
		u.bodyScroll = 0
	case keyEnd:
		u.bodyScroll = 1 << 30 // clamped against content in draw
	case keyRune:
		switch ev.r {
		case 'q', 'Q':
			u.page = pageOverview
		case 'h':
			u.moveBlock(-1)
		case 'l':
			u.moveBlock(1)
		case 'k':
			u.bodyScroll--
		case 'j':
			u.bodyScroll++
		case 'g':
			u.bodyScroll = 0
		case 'G':
			u.bodyScroll = 1 << 30
		case 'c':
			u.copyBlock()
		case 's':
			u.saveBlock()
		}
	}
	if u.bodyScroll < 0 {
		u.bodyScroll = 0
	}
}

func (u *ui) moveRow(d int) {
	u.rowCursor += d
	if u.rowCursor < 0 {
		u.rowCursor = 0
	}
	if u.rowCursor >= len(u.recs) {
		u.rowCursor = len(u.recs) - 1
	}
	if u.rowCursor < 0 {
		u.rowCursor = 0
	}
}

// moveBlock steps through the current record's blocks; stepping past the first block rolls to
// the previous record's LAST block, and past the last block to the next record's FIRST block —
// so ←/→ read every record's blocks as one continuous sequence. Clamps at the very ends.
func (u *ui) moveBlock(d int) {
	nb := u.blockCursor + d
	switch {
	case nb < 0: // ← past the first block -> previous record, its last block
		if u.rowCursor > 0 {
			u.rowCursor--
			u.loadBlocks()
			u.blockCursor = len(u.blocks) - 1
			if u.blockCursor < 0 {
				u.blockCursor = 0
			}
			u.bodyScroll = 0
		}
	case nb >= len(u.blocks): // → past the last block -> next record, its first block
		if u.rowCursor < len(u.recs)-1 {
			u.rowCursor++
			u.loadBlocks()
			u.blockCursor = 0
			u.bodyScroll = 0
		}
	default:
		u.blockCursor = nb
		u.bodyScroll = 0
	}
}

// loadBlocks (re)builds the block list for the record under rowCursor.
func (u *ui) loadBlocks() {
	if u.rowCursor >= 0 && u.rowCursor < len(u.recs) {
		u.blocks = buildBlocks(u.recs[u.rowCursor])
	} else {
		u.blocks = nil
	}
}

func (u *ui) openDetail() {
	if u.rowCursor < 0 || u.rowCursor >= len(u.recs) {
		return
	}
	u.loadBlocks()
	u.blockCursor = 0
	u.bodyScroll = 0
	u.page = pageDetail
}

func (u *ui) copyBlock() {
	if u.blockCursor < 0 || u.blockCursor >= len(u.blocks) {
		return
	}
	u.setFlash(copyToClipboard(u.blocks[u.blockCursor].raw).message())
}

// saveBlock persists the selected block to $AALGT_AUDIT_ROOT/clips/ with a datetime stamp.
func (u *ui) saveBlock() {
	if u.rowCursor < 0 || u.rowCursor >= len(u.recs) || u.blockCursor < 0 || u.blockCursor >= len(u.blocks) {
		return
	}
	if path, err := saveClip(u.recs[u.rowCursor], u.blocks[u.blockCursor]); err != nil {
		u.setFlash("save failed: " + err.Error())
	} else {
		u.setFlash("saved to " + path)
	}
}

func (u *ui) setFlash(msg string) {
	u.flash = msg
	u.flashUntil = time.Now().Add(4 * time.Second)
}

// ---- viewports --------------------------------------------------------------

// feedViewport is the number of feed rows visible on the overview (chrome: header + blank
// + 3 stat lines + blank + column header + footer = 8 lines).
func (u *ui) feedViewport() int {
	if v := u.h - 8; v > 0 {
		return v
	}
	return 1
}

// bodyViewport is the number of body lines visible on the detail page (chrome: title +
// block bar + blank + footer = 4 lines).
func (u *ui) bodyViewport() int {
	if v := u.h - 4; v > 0 {
		return v
	}
	return 1
}

// ---- rendering --------------------------------------------------------------

func (u *ui) draw() {
	u.w, u.h = termSize(u.fd)
	var lines []string
	switch u.page {
	case pageStats:
		lines = u.drawStats()
	case pageDetail:
		lines = u.drawDetail()
	default:
		lines = u.drawOverview()
	}
	u.paint(lines)
}

// paint clears the screen and writes exactly h lines (each clipped to width), joined with
// CRLF (raw mode has OPOST off, so a bare \n would not return the carriage).
func (u *ui) paint(lines []string) {
	var b strings.Builder
	b.Grow(u.w * (u.h + 1))
	b.WriteString(clearHome)
	for i := 0; i < u.h; i++ {
		if i < len(lines) {
			b.WriteString(truncDisp(lines[i], u.w))
		}
		if i < u.h-1 {
			b.WriteString("\r\n")
		}
	}
	os.Stdout.WriteString(b.String())
}

func (u *ui) headerLine() string {
	label := func(s string) string { return sgr(s, cDim) }
	val := func(s string) string { return sgr(s, cWhite, sgrBold) }
	parts := []string{sgr("aalegate-tui", cAccent, sgrBold)}
	if u.runID != "" {
		parts = append(parts, label("run ")+val(u.runID))
	}
	if u.model != "" {
		parts = append(parts, label("model ")+val(u.model))
	}
	parts = append(parts, label("turns ")+val(fmt.Sprintf("%d", len(u.recs))))
	return " " + strings.Join(parts, sgr("  ·  ", cDim))
}

func (u *ui) footer(hints string) string {
	if u.flash != "" && time.Now().Before(u.flashUntil) {
		return " " + sgr(u.flash, cGreen, sgrBold) + sgr("   "+hints, cDim)
	}
	return " " + sgr(hints, cDim)
}

func (u *ui) drawOverview() []string {
	lines := []string{u.headerLine(), ""}
	lines = append(lines, u.statsStrip()...)
	lines = append(lines, "", u.feedHeaderLine())

	vp := u.feedViewport()
	u.clampRowScroll(vp)
	if len(u.recs) == 0 {
		lines = append(lines, sgr("  (no LLM turns in this run)", cDim))
	}
	for i := u.rowScroll; i < u.rowScroll+vp && i < len(u.recs); i++ {
		lines = append(lines, u.feedRow(u.recs[i], i == u.rowCursor))
	}
	for len(lines) < u.h-1 {
		lines = append(lines, "")
	}
	lines = append(lines, u.footer(overviewHints))
	return lines
}

func (u *ui) clampRowScroll(vp int) {
	if u.rowCursor < u.rowScroll {
		u.rowScroll = u.rowCursor
	}
	if u.rowCursor >= u.rowScroll+vp {
		u.rowScroll = u.rowCursor - vp + 1
	}
	maxScroll := len(u.recs) - vp
	if maxScroll < 0 {
		maxScroll = 0
	}
	if u.rowScroll > maxScroll {
		u.rowScroll = maxScroll
	}
	if u.rowScroll < 0 {
		u.rowScroll = 0
	}
}

func (u *ui) feedHeaderLine() string {
	return "  " + sgr(fmt.Sprintf("%-5s %-22s %8s %-12s %-5s %-10s  %-6s",
		"SEQ", "MODEL", "LATENCY", "TOKENS", "TOOLS", "DECODE", "FINISH"), cDim, sgrBold)
}

func (u *ui) feedRow(r Record, selected bool) string {
	lat := r.TotalMS
	if lat == 0 {
		lat = r.LatencyMS
	}
	latStr := fmt.Sprintf("%dms", lat)

	tok, dec := "", ""
	if ts := tokenStats(&r); ts != nil {
		tok = fmt.Sprintf("%d/%d", ts.Input, ts.Output)
		if ts.DecodeTkS != nil {
			dec = fmt.Sprintf("%.1ftk/s", *ts.DecodeTkS)
		}
	}
	tc := ""
	if n := toolCallCount(&r); n > 0 {
		tc = fmt.Sprintf("%d", n)
	}
	isErr := r.Status >= 400 || r.Error != ""
	finish := r.FinishReason
	if isErr {
		if finish == "" {
			finish = "ERR"
		} else {
			finish += " ERR"
		}
	}

	if selected {
		plain := fmt.Sprintf("%-5d %-22s %8s %-12s %-5s %-10s  %s",
			r.Seq, shortModel(r.Model), latStr, tok, tc, dec, finish)
		return "  " + sgr(plain, sgrReverse)
	}

	seqCell := fmt.Sprintf("%-5d ", r.Seq)
	modelCell := fmt.Sprintf("%-22s ", shortModel(r.Model))
	latCell := padLeft(sgr(latStr, latencyANSI(lat)), 8) + " "
	tokCell := fmt.Sprintf("%-12s ", tok)
	tcCell := fmt.Sprintf("%-5s ", tc)
	decCell := fmt.Sprintf("%-10s  ", dec)
	finCell := finish
	if isErr {
		finCell = sgr(finish, cRed)
	}
	return "  " + seqCell + modelCell + latCell + tokCell + tcCell + decCell + finCell
}

func (u *ui) statsStrip() []string {
	s := &u.stats
	lbl := func(x string) string { return sgr(x, cDim) }
	v := func(x string) string { return sgr(x, cWhite, sgrBold) }
	l1 := fmt.Sprintf("  %s %s    %s %s    %s %s %s %s",
		lbl("turns"), v(fmt.Sprintf("%d", s.Turns)),
		lbl("errors"), u.errVal(),
		lbl("input"), v(fmtInt(s.InputTokens)), lbl("cache"), v(fmtInt(s.CacheTokens)))
	l2 := fmt.Sprintf("  %s %s = %s ~%s + %s ~%s",
		lbl("output"), v(fmtInt(s.OutputTokens)),
		lbl("reasoning"), v(fmtInt(s.ReasoningTokens)),
		lbl("writing"), v(fmtInt(s.WritingTokens)))
	l3 := fmt.Sprintf("  %s %s    %s %s    %s %s    %s %s",
		lbl("avg lat"), v(msStr(s.AvgLatencyMS())),
		lbl("ttft"), v(msStr(s.AvgTTFTMS())),
		lbl("prefill"), v(tksStr(s.PrefillTkS())),
		lbl("decode"), v(tksStr(s.DecodeTkS())))
	if sp := s.Sparkline(); sp != "" {
		l3 += "    " + lbl("lat") + " " + sgr(sp, cAccent)
	}
	return []string{l1, l2, l3}
}

func (u *ui) errVal() string {
	s := &u.stats
	if s.Errors == 0 {
		return sgr("0", cWhite, sgrBold)
	}
	pct := 0.0
	if s.Turns > 0 {
		pct = float64(s.Errors) / float64(s.Turns) * 100
	}
	return sgr(fmt.Sprintf("%d (%.0f%%)", s.Errors, pct), cRed, sgrBold)
}

func (u *ui) drawStats() []string {
	s := &u.stats
	v := func(x string) string { return sgr(x, cWhite, sgrBold) }
	lines := []string{sgr("  Run statistics", cAccent, sgrBold), ""}
	row := func(label, value string) {
		lines = append(lines, "  "+padRight(sgr(label, cDim), 18)+value)
	}
	row("turns", v(fmt.Sprintf("%d", s.Turns)))
	row("errors", u.errVal())
	row("input (fresh)", v(fmtInt(s.InputTokens)))
	row("cache (reused)", v(fmtInt(s.CacheTokens)))
	row("output", v(fmtInt(s.OutputTokens))+sgr(fmt.Sprintf("  = reasoning ~%s + writing ~%s",
		fmtInt(s.ReasoningTokens), fmtInt(s.WritingTokens)), cDim))
	row("avg latency", v(msStr(s.AvgLatencyMS())))
	row("avg ttft", v(msStr(s.AvgTTFTMS())))
	row("avg prefill", v(tksStr(s.PrefillTkS())))
	row("avg decode", v(tksStr(s.DecodeTkS())))
	if sp := s.Sparkline(); sp != "" {
		row("latency", sgr(sp, cAccent))
	}
	for len(lines) < u.h-1 {
		lines = append(lines, "")
	}
	lines = append(lines, u.footer(statsHints))
	return lines
}

func (u *ui) drawDetail() []string {
	if u.rowCursor < 0 || u.rowCursor >= len(u.recs) {
		return []string{u.footer(detailHints)}
	}
	r := u.recs[u.rowCursor]
	lat := r.TotalMS
	if lat == 0 {
		lat = r.LatencyMS
	}
	title := "  " + sgr(fmt.Sprintf("Record #%d", r.Seq), cAccent, sgrBold) +
		sgr("  "+r.Endpoint, cWhite) + sgr("  "+shortModel(r.Model), cDim) +
		sgr(fmt.Sprintf("  status %d  %dms", r.Status, lat), cDim)
	lines := []string{title, u.blockBar(), ""}

	if len(u.blocks) == 0 {
		lines = append(lines, sgr("  (no content blocks in this record)", cDim))
	} else {
		bl := u.blocks[u.blockCursor]
		body := highlightContent(bl.raw, bl.title, u.w-2)
		vp := u.bodyViewport()
		if max := len(body) - vp; u.bodyScroll > max {
			if max < 0 {
				max = 0
			}
			u.bodyScroll = max
		}
		if u.bodyScroll < 0 {
			u.bodyScroll = 0
		}
		for i := u.bodyScroll; i < u.bodyScroll+vp && i < len(body); i++ {
			lines = append(lines, "  "+body[i])
		}
	}
	for len(lines) < u.h-1 {
		lines = append(lines, "")
	}

	hint := detailHints
	if len(u.blocks) > 0 {
		hint = fmt.Sprintf("block %d/%d · %s chars · %s",
			u.blockCursor+1, len(u.blocks), fmtInt(int64(len(u.blocks[u.blockCursor].raw))), detailHints)
	}
	lines = append(lines, u.footer(hint))
	return lines
}

func (u *ui) blockBar() string {
	if len(u.blocks) == 0 {
		return sgr("  —", cDim)
	}
	parts := make([]string, len(u.blocks))
	for i, b := range u.blocks {
		if i == u.blockCursor {
			parts[i] = sgr(" "+b.title+" ", sgrReverse)
		} else {
			parts[i] = sgr(b.title, cDim)
		}
	}
	return "  " + strings.Join(parts, sgr(" | ", cBorder))
}

// dumpNonTTY prints a plain, highlighted transcript to stdout (no raw mode) for piping to a
// pager such as `less -R`. Reuses the same highlighter and block builder as the interactive view.
func (u *ui) dumpNonTTY() int {
	const width = 100
	var b strings.Builder
	head := fmt.Sprintf("=== aalegate run %s", u.runID)
	if u.model != "" {
		head += " (" + u.model + ")"
	}
	head += fmt.Sprintf(" — %d LLM turns ===", len(u.recs))
	b.WriteString(sgr(head, cAccent, sgrBold) + "\n\n")
	for _, l := range u.statsStrip() {
		b.WriteString(strings.TrimRight(l, " ") + "\n")
	}
	b.WriteString("\n")
	for _, r := range u.recs {
		lat := r.TotalMS
		if lat == 0 {
			lat = r.LatencyMS
		}
		b.WriteString(sgr(fmt.Sprintf("── Record #%d  %s  status %d  %dms",
			r.Seq, r.Endpoint, r.Status, lat), cAccent, sgrBold) + "\n")
		for _, bl := range buildBlocks(r) {
			b.WriteString(sgr("• "+bl.title, cGreen, sgrBold) + "\n")
			for _, line := range highlightContent(bl.raw, bl.title, width) {
				b.WriteString("  " + line + "\n")
			}
		}
		b.WriteString("\n")
	}
	os.Stdout.WriteString(b.String())
	return 0
}

// ---- small formatters -------------------------------------------------------

func msStr(v float64) string {
	if v <= 0 {
		return "n/a"
	}
	return fmt.Sprintf("%.0f ms", v)
}

func tksStr(v float64) string {
	if v <= 0 {
		return "n/a"
	}
	return fmt.Sprintf("%.0f tk/s", v)
}
