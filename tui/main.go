package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

func main() {
	// Opt into the bubbletea "rich"/live add-on BEFORE parsing flags, so every arg passes
	// through untouched (rich owns -sse/-gateway/-theme/-probe/-api-key). syscall.Exec replaces
	// this process image; it only returns on failure.
	if os.Getenv("AALE_TUI") == "rich" {
		if bin, err := exec.LookPath("aalegate-tui-rich"); err == nil {
			_ = syscall.Exec(bin, append([]string{bin}, os.Args[1:]...), os.Environ())
			fmt.Fprintln(os.Stderr, "aalegate-tui: exec aalegate-tui-rich failed; using built-in reader")
		} else {
			fmt.Fprintln(os.Stderr, "aalegate-tui: AALE_TUI=rich but aalegate-tui-rich not on PATH; using built-in reader")
		}
	}

	file := flag.String("file", "", "path to a plane1.jsonl to read")
	runID := flag.String("run", "", "run id to read (resolved under the audit root)")
	rootFlag := flag.String("root", "", "audit root (default $AALE_AUDIT_ROOT or ~/.local/state/aalegate)")
	pick := flag.Bool("pick", false, "choose from recent runs")
	theme := flag.String("theme", "", "color theme: "+strings.Join(themeNames(), ", ")+" (or $AALE_TUI_THEME)")
	// Accepted for arg-compat with the rich add-on; ignored by the reader.
	_ = flag.String("probe", "", "backend probe (rich add-on only; ignored)")
	_ = flag.String("api-key", "", "backend probe key (rich add-on only; ignored)")
	sse := flag.String("sse", "", "live SSE endpoint (rich add-on only)")
	gateway := flag.String("gateway", "", "live gateway host:port (rich add-on only)")
	flag.Usage = usage
	flag.Parse()

	colorOff = os.Getenv("NO_COLOR") != ""

	// theme: -theme flag > $AALE_TUI_THEME > nocturnal (init default). Unknown -> warn + keep default.
	if name := firstNonEmpty(*theme, os.Getenv("AALE_TUI_THEME")); name != "" && !setPalette(name) {
		fmt.Fprintf(os.Stderr, "aalegate-tui: unknown theme %q; using nocturnal (available: %s)\n",
			name, strings.Join(themeNames(), ", "))
	}

	if *sse != "" || *gateway != "" {
		fmt.Fprintln(os.Stderr, "aalegate-tui reads finished runs. For live monitoring, install the rich add-on and run:\n  AALE_TUI=rich aalegate-tui -gateway <host:port>")
		os.Exit(2)
	}

	root := *rootFlag
	if root == "" {
		root = auditRootDir()
	}

	var path string
	switch {
	case *file != "":
		path = *file
	case *pick:
		p, err := pickRun(root)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		path = p
	default:
		id := *runID
		if id == "" {
			id = flag.Arg(0)
		}
		if id != "" {
			if path = findRun(root, id); path == "" {
				fmt.Fprintf(os.Stderr, "aalegate-tui: run %q not found under %s\n", id, root)
				os.Exit(1)
			}
		} else if path = latestLogIn(root); path == "" {
			fmt.Fprintf(os.Stderr, "aalegate-tui: no runs found under %s\n", root)
			usage()
			os.Exit(1)
		}
	}

	recs, err := loadRun(path)
	if err != nil {
		fmt.Fprintf(os.Stderr, "aalegate-tui: reading %s: %v\n", path, err)
		os.Exit(1)
	}
	os.Exit(newUI(recs, path).Run())
}

func usage() {
	fmt.Fprint(os.Stderr, `aalegate-tui — read-only viewer for aalegate audit runs.

usage:
  aalegate-tui                 # latest run under the audit root
  aalegate-tui -pick           # choose from recent runs
  aalegate-tui -run <RUN_ID>   # a specific run
  aalegate-tui -file <plane1.jsonl>
  aalegate-tui <RUN_ID>        # positional shorthand for -run

keys: ↑/↓ move · pgup/pgdn page · enter open · c copy block · tab stats · q quit
themes: -theme <name> or $AALE_TUI_THEME  (nocturnal, dracula, gruvbox, nord, solarized, ansi)
live monitoring: install the rich add-on and use  AALE_TUI=rich aalegate-tui ...
`)
}

// ---- run selection ----------------------------------------------------------

// auditRootDir mirrors recorder.py's DEFAULT_AUDIT_ROOT: $AALE_AUDIT_ROOT (tilde-expanded)
// or ~/.local/state/aalegate.
func auditRootDir() string {
	if v := os.Getenv("AALE_AUDIT_ROOT"); v != "" {
		return expandTilde(v)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".local", "state", "aalegate")
}

func expandTilde(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, p[1:])
		}
	}
	return p
}

type runInfo struct {
	path  string
	label string
	mod   int64
}

// recentRuns returns runs under root (nested <handle>/<run_id>/plane1.jsonl, plus legacy
// flat <run_id>/plane1.jsonl), newest first, capped at limit.
func recentRuns(root string, limit int) []runInfo {
	if root == "" {
		return nil
	}
	var runs []runInfo
	for _, pat := range []string{
		filepath.Join(root, "*", "*", "plane1.jsonl"),
		filepath.Join(root, "*", "plane1.jsonl"),
	} {
		matches, _ := filepath.Glob(pat)
		for _, p := range matches {
			fi, err := os.Stat(p)
			if err != nil || fi.Size() == 0 {
				continue
			}
			rel, err := filepath.Rel(root, filepath.Dir(p))
			if err != nil {
				rel = filepath.Dir(p)
			}
			label := fmt.Sprintf("%-44s  %s", rel, fi.ModTime().Format("2006-01-02 15:04"))
			runs = append(runs, runInfo{p, label, fi.ModTime().UnixNano()})
		}
	}
	sort.Slice(runs, func(i, j int) bool { return runs[i].mod > runs[j].mod })
	if limit > 0 && len(runs) > limit {
		runs = runs[:limit]
	}
	return runs
}

func latestLogIn(root string) string {
	if runs := recentRuns(root, 1); len(runs) > 0 {
		return runs[0].path
	}
	return ""
}

// findRun resolves a run id (or a direct path / run dir) to a plane1.jsonl path.
func findRun(root, id string) string {
	if strings.Contains(id, "/") || strings.HasSuffix(id, ".jsonl") {
		if fi, err := os.Stat(id); err == nil && !fi.IsDir() {
			return id
		}
		if p := filepath.Join(id, "plane1.jsonl"); fileExists(p) {
			return p
		}
	}
	for _, pat := range []string{
		filepath.Join(root, "*", id, "plane1.jsonl"),
		filepath.Join(root, id, "plane1.jsonl"),
	} {
		if m, _ := filepath.Glob(pat); len(m) > 0 {
			return m[0]
		}
	}
	return ""
}

func fileExists(p string) bool {
	fi, err := os.Stat(p)
	return err == nil && !fi.IsDir()
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

// pickRun lists recent runs and reads a 1-based choice from stdin (cooked mode; called
// before the reader enters raw mode).
func pickRun(root string) (string, error) {
	runs := recentRuns(root, 15)
	if len(runs) == 0 {
		return "", fmt.Errorf("aalegate-tui: no runs found under %s", root)
	}
	fmt.Fprintf(os.Stderr, "recent runs under %s:\n", root)
	for i, r := range runs {
		fmt.Fprintf(os.Stderr, "  %2d) %s\n", i+1, r.label)
	}
	fmt.Fprint(os.Stderr, "choose [1]: ")
	sc := bufio.NewScanner(os.Stdin)
	choice := 1
	if sc.Scan() {
		if s := strings.TrimSpace(sc.Text()); s != "" {
			n, err := strconv.Atoi(s)
			if err != nil || n < 1 || n > len(runs) {
				return "", fmt.Errorf("aalegate-tui: invalid choice %q", s)
			}
			choice = n
		}
	}
	return runs[choice-1].path, nil
}
