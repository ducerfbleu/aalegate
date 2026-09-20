package main

// Markdown + JSON syntax highlighting for the reader's detail view. Ported from the former
// lipgloss version: identical parsing, but it emits raw ANSI (sgr()) against the fixed
// palette instead of lipgloss styles — no third-party dependency. This is the one parser on
// the trusted, transcript-reading surface, so it is fuzzed (highlight_fuzz_test.go).

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

func highlightContent(text, title string, width int) []string {
	lower := strings.ToLower(title)
	switch {
	case strings.Contains(lower, "request body"),
		strings.Contains(lower, "raw"),
		strings.Contains(lower, "tool call"):
		trimmed := strings.TrimSpace(text)
		if len(trimmed) > 0 && (trimmed[0] == '{' || trimmed[0] == '[') {
			return highlightJSON(text, width)
		}
		return highlightMarkdown(text, width)
	case strings.Contains(lower, "reasoning"),
		strings.Contains(lower, "completion"):
		return highlightMarkdown(text, width)
	default:
		trimmed := strings.TrimSpace(text)
		if len(trimmed) > 0 && (trimmed[0] == '{' || trimmed[0] == '[') {
			return highlightJSON(text, width)
		}
		return highlightMarkdown(text, width)
	}
}

// ---- markdown ---------------------------------------------------------------

func highlightMarkdown(text string, width int) []string {
	var out []string
	inCodeBlock := false

	for _, rawLine := range strings.Split(text, "\n") {
		// fenced code blocks
		if strings.HasPrefix(strings.TrimSpace(rawLine), "```") {
			inCodeBlock = !inCodeBlock
			out = append(out, sgr(strings.TrimSpace(rawLine), cYellow))
			continue
		}
		if inCodeBlock {
			for _, wl := range wrapLines(rawLine, width) {
				out = append(out, sgr(wl, cYellow))
			}
			continue
		}

		trimmed := strings.TrimSpace(rawLine)

		// headings
		if strings.HasPrefix(trimmed, "### ") {
			for _, wl := range wrapLines(trimmed, width) {
				out = append(out, sgr(wl, cYellow, sgrBold))
			}
			continue
		}
		if strings.HasPrefix(trimmed, "## ") {
			for _, wl := range wrapLines(trimmed, width) {
				out = append(out, sgr(wl, cGreen, sgrBold))
			}
			continue
		}
		if strings.HasPrefix(trimmed, "# ") {
			for _, wl := range wrapLines(trimmed, width) {
				out = append(out, sgr(wl, cAccent, sgrBold))
			}
			continue
		}

		// horizontal rule
		if trimmed == "---" || trimmed == "***" || trimmed == "___" {
			out = append(out, sgr(strings.Repeat("─", width), cBorder))
			continue
		}

		// blockquote
		if strings.HasPrefix(trimmed, "> ") {
			content := strings.TrimPrefix(trimmed, "> ")
			for _, wl := range wrapLines("▎ "+content, width) {
				out = append(out, sgr(wl, cDim, sgrItalic))
			}
			continue
		}

		// bullet lists
		if strings.HasPrefix(trimmed, "- ") || strings.HasPrefix(trimmed, "* ") {
			indent := len(rawLine) - len(strings.TrimLeft(rawLine, " "))
			content := trimmed[2:]
			prefix := strings.Repeat(" ", indent) + "• "
			for i, wl := range wrapLines(content, width-indent-2) {
				if i == 0 {
					out = append(out, sgr(prefix, cAccent, sgrBold)+highlightInline(wl))
				} else {
					out = append(out, strings.Repeat(" ", indent+2)+highlightInline(wl))
				}
			}
			continue
		}

		// numbered lists
		if len(trimmed) > 2 && unicode.IsDigit(rune(trimmed[0])) {
			dotIdx := strings.Index(trimmed, ". ")
			if dotIdx > 0 && dotIdx < 4 {
				indent := len(rawLine) - len(strings.TrimLeft(rawLine, " "))
				num := trimmed[:dotIdx+2]
				content := trimmed[dotIdx+2:]
				prefix := strings.Repeat(" ", indent) + num
				for i, wl := range wrapLines(content, width-indent-len(num)) {
					if i == 0 {
						out = append(out, sgr(prefix, cAccent, sgrBold)+highlightInline(wl))
					} else {
						out = append(out, strings.Repeat(" ", indent+len(num))+highlightInline(wl))
					}
				}
				continue
			}
		}

		// regular paragraph with inline formatting
		if trimmed == "" {
			out = append(out, "")
			continue
		}
		for _, wl := range wrapLines(rawLine, width) {
			out = append(out, highlightInline(wl))
		}
	}

	return out
}

// highlightInline colors inline code, bold, and links; plain runs are emitted in one SGR.
func highlightInline(line string) string {
	var out, plain strings.Builder
	flush := func() {
		if plain.Len() > 0 {
			out.WriteString(sgr(plain.String(), cWhite))
			plain.Reset()
		}
	}
	i, n := 0, len(line)
	for i < n {
		// inline code `...`
		if line[i] == '`' {
			if end := strings.Index(line[i+1:], "`"); end >= 0 {
				flush()
				out.WriteString(sgr(line[i:i+2+end], cYellow))
				i += end + 2
				continue
			}
		}
		// bold **...**
		if i+1 < n && line[i] == '*' && line[i+1] == '*' {
			if end := strings.Index(line[i+2:], "**"); end >= 0 {
				flush()
				out.WriteString(sgr(line[i+2:i+2+end], cWhite, sgrBold))
				i += end + 4
				continue
			}
		}
		// bold __...__
		if i+1 < n && line[i] == '_' && line[i+1] == '_' {
			if end := strings.Index(line[i+2:], "__"); end >= 0 {
				flush()
				out.WriteString(sgr(line[i+2:i+2+end], cWhite, sgrBold))
				i += end + 4
				continue
			}
		}
		// links [text](url)
		if line[i] == '[' {
			closeBracket := strings.Index(line[i:], "](")
			if closeBracket > 0 {
				closeParen := strings.Index(line[i+closeBracket:], ")")
				if closeParen > 0 {
					flush()
					text := line[i+1 : i+closeBracket]
					url := line[i+closeBracket+2 : i+closeBracket+closeParen]
					out.WriteString(sgr(text, cAccent, sgrUnder) + sgr(" ("+url+")", cWhite))
					i += closeBracket + closeParen + 1
					continue
				}
			}
		}

		_, size := utf8.DecodeRuneInString(line[i:])
		if size < 1 {
			size = 1
		}
		plain.WriteString(line[i : i+size])
		i += size
	}
	flush()
	return out.String()
}

// ---- JSON (jq-style) --------------------------------------------------------

func highlightJSON(raw string, width int) []string {
	var out []string
	for _, rawLine := range strings.Split(raw, "\n") {
		if len(rawLine) > width {
			for _, wl := range wrapLines(rawLine, width) {
				out = append(out, colorizeJSONLine(wl))
			}
		} else {
			out = append(out, colorizeJSONLine(rawLine))
		}
	}
	return out
}

func colorizeJSONLine(line string) string {
	var result strings.Builder
	i, n := 0, len(line)
	isKey := true // first string on a line, if followed by ':', is a key

	for i < n {
		ch := line[i]
		switch {
		case ch == ' ' || ch == '\t':
			result.WriteByte(ch)
			i++

		case ch == '{' || ch == '}' || ch == '[' || ch == ']':
			result.WriteString(sgr(string(ch), cDim))
			i++

		case ch == ':':
			result.WriteString(sgr(":", cDim))
			isKey = false
			i++

		case ch == ',':
			result.WriteString(sgr(",", cDim))
			isKey = true
			i++

		case ch == '"':
			end := i + 1
			for end < n {
				if line[end] == '\\' {
					end += 2
					continue
				}
				if line[end] == '"' {
					end++
					break
				}
				end++
			}
			if end > n {
				end = n
			}
			tok := line[i:end]
			rest := strings.TrimSpace(line[end:])
			if isKey && (strings.HasPrefix(rest, ":") || strings.HasPrefix(rest, "\":")) {
				result.WriteString(sgr(tok, cAccent, sgrBold))
			} else {
				result.WriteString(sgr(tok, cGreen))
			}
			i = end

		case ch == 't' && i+4 <= n && line[i:i+4] == "true":
			result.WriteString(sgr("true", cYellow, sgrBold))
			i += 4

		case ch == 'f' && i+5 <= n && line[i:i+5] == "false":
			result.WriteString(sgr("false", cYellow, sgrBold))
			i += 5

		case ch == 'n' && i+4 <= n && line[i:i+4] == "null":
			result.WriteString(sgr("null", cRed))
			i += 4

		case ch == '-' || (ch >= '0' && ch <= '9'):
			end := i + 1
			for end < n && (line[end] == '.' || line[end] == 'e' || line[end] == 'E' ||
				line[end] == '+' || line[end] == '-' || (line[end] >= '0' && line[end] <= '9')) {
				end++
			}
			result.WriteString(sgr(line[i:end], cYellow))
			i = end

		default:
			result.WriteByte(ch)
			i++
		}
	}

	return result.String()
}
