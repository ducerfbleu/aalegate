package main

import (
	"encoding/json"
	"fmt"
	"strings"
)

// block is one copyable/inspectable section of a record: the readable Prompt, the model's
// Reasoning, the Completion, each Tool Call, and the raw Request Body. Ported from the
// former model.go's `expandable` (title/full).
type block struct {
	title string
	raw   string
}

// completionRecords keeps only LLM inference turns (chat.completions / completions /
// Anthropic messages) — the turns worth listing and drilling into.
func completionRecords(records []Record) []Record {
	var out []Record
	for _, r := range records {
		if isLLMTurn(r.Endpoint) {
			out = append(out, r)
		}
	}
	return out
}

// buildBlocks assembles a record's inspectable sections (ported from buildExpandItems).
func buildBlocks(r Record) []block {
	var items []block

	if prompt := extractPrompt(r.Request); prompt != "" {
		items = append(items, block{title: "Prompt", raw: prompt})
	}
	if r.Reasoning != "" {
		items = append(items, block{title: "Reasoning", raw: r.Reasoning})
	}
	if r.Completion != "" {
		items = append(items, block{title: "Completion", raw: r.Completion})
	}
	if len(r.ToolCalls) > 0 {
		var tcs []struct {
			Name     string          `json:"name"`  // Anthropic
			Input    json.RawMessage `json:"input"` // Anthropic
			Function struct {         // OpenAI
				Name      string `json:"name"`
				Arguments string `json:"arguments"`
			} `json:"function"`
		}
		if json.Unmarshal(r.ToolCalls, &tcs) == nil {
			for i, tc := range tcs {
				name, args := tc.Function.Name, tc.Function.Arguments
				if name == "" && tc.Name != "" { // Anthropic: name + JSON input
					name = tc.Name
					if len(tc.Input) > 0 {
						args = strings.Join(prettyJSON(tc.Input), "\n")
					}
				}
				items = append(items, block{title: fmt.Sprintf("Tool Call %d: %s", i+1, name), raw: args})
			}
		}
	}
	if len(r.Request) > 0 {
		items = append(items, block{title: "Request Body", raw: strings.Join(prettyJSON(r.Request), "\n")})
	}
	return items
}

// extractPrompt renders the request's conversation messages as readable "role:\ncontent"
// text, flattening string or block-array content for both OpenAI and Anthropic shapes.
func extractPrompt(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var req struct {
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
		Prompt json.RawMessage `json:"prompt"` // legacy /completions
	}
	if json.Unmarshal(raw, &req) != nil {
		return ""
	}
	var b strings.Builder
	for _, msg := range req.Messages {
		text := strings.TrimSpace(flattenContent(msg.Content))
		if text == "" {
			continue
		}
		if b.Len() > 0 {
			b.WriteString("\n\n")
		}
		role := msg.Role
		if role == "" {
			role = "message"
		}
		b.WriteString(role + ":\n" + text)
	}
	if b.Len() == 0 && len(req.Prompt) > 0 {
		return strings.TrimSpace(flattenContent(req.Prompt))
	}
	return b.String()
}

// flattenContent turns message content into plain text: a JSON string as-is, or an array
// of content blocks (text / thinking / tool_use / tool_result) joined line-by-line.
func flattenContent(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var blocks []struct {
		Type     string          `json:"type"`
		Text     string          `json:"text"`
		Thinking string          `json:"thinking"`
		Name     string          `json:"name"`
		Input    json.RawMessage `json:"input"`
		Content  json.RawMessage `json:"content"`
	}
	if json.Unmarshal(raw, &blocks) == nil {
		var parts []string
		for _, bl := range blocks {
			switch bl.Type {
			case "thinking", "redacted_thinking":
				if bl.Thinking != "" {
					parts = append(parts, "[thinking] "+bl.Thinking)
				}
			case "tool_use":
				parts = append(parts, fmt.Sprintf("[tool_use: %s] %s", bl.Name, string(bl.Input)))
			case "tool_result":
				parts = append(parts, "[tool_result] "+flattenContent(bl.Content))
			default: // text / input_text / output_text / other-with-text
				if bl.Text != "" {
					parts = append(parts, bl.Text)
				}
			}
		}
		return strings.Join(parts, "\n")
	}
	return string(raw)
}

// prettyJSON indents a raw JSON message into lines (ported from model.go).
func prettyJSON(raw json.RawMessage) []string {
	var obj interface{}
	if json.Unmarshal(raw, &obj) != nil {
		return []string{string(raw)}
	}
	b, err := json.MarshalIndent(obj, "", "  ")
	if err != nil {
		return []string{string(raw)}
	}
	return strings.Split(string(b), "\n")
}
