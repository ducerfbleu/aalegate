package main

import (
	"encoding/json"
	"math"
)

type Record struct {
	RunID            string          `json:"run_id"`
	Seq              uint64          `json:"seq"`
	TSRequest        string          `json:"ts_request"`
	TSResponse       string          `json:"ts_response,omitempty"`
	LatencyMS        int64           `json:"latency_ms"`
	TSFirstToken     string          `json:"ts_first_token,omitempty"`
	TSEnd            string          `json:"ts_end,omitempty"`
	TTFTMs           int64           `json:"ttft_ms,omitempty"`
	DecodeMS         int64           `json:"decode_ms,omitempty"`
	TotalMS          int64           `json:"total_ms,omitempty"`
	Method           string          `json:"method"`
	Path             string          `json:"path"`
	Endpoint         string          `json:"endpoint"`
	Model            string          `json:"model,omitempty"`
	Stream           bool            `json:"stream"`
	Status           int             `json:"status"`
	RequestBytes     int             `json:"request_bytes"`
	ResponseBytes    int             `json:"response_bytes"`
	Request          json.RawMessage `json:"request,omitempty"`
	MessagesOffset   int             `json:"messages_offset,omitempty"`
	Completion       string          `json:"completion,omitempty"`
	Reasoning        string          `json:"reasoning,omitempty"`
	ToolCalls        json.RawMessage `json:"tool_calls,omitempty"`
	Usage            json.RawMessage `json:"usage,omitempty"`
	Timings          json.RawMessage `json:"timings,omitempty"`
	FinishReason     string          `json:"finish_reason,omitempty"`
	PromptSHA256     string          `json:"prompt_sha256,omitempty"`
	CompletionSHA256 string          `json:"completion_sha256,omitempty"`
	ToolCallsSHA256  string          `json:"tool_calls_sha256,omitempty"`
	Truncated        bool            `json:"truncated,omitempty"`
	ParseError       string          `json:"parse_error,omitempty"`
	Error            string          `json:"error,omitempty"`
	PrevHash         string          `json:"prev_hash"`
	RecordHash       string          `json:"record_hash"`
}

type usageJSON struct {
	// OpenAI / llama.cpp
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
	PromptDetails    *struct {
		CachedTokens int `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
	CompletionDetails *struct {
		ReasoningTokens *int `json:"reasoning_tokens"`
	} `json:"completion_tokens_details"`
	// Anthropic Messages API (input_tokens is cache-EXCLUDED; fold the cached counts back in)
	InputTokens              *int `json:"input_tokens"`
	OutputTokens             *int `json:"output_tokens"`
	CacheReadInputTokens     int  `json:"cache_read_input_tokens"`
	CacheCreationInputTokens int  `json:"cache_creation_input_tokens"`
}

// isLLMTurn reports whether a record is a model inference turn worth showing/counting:
// OpenAI chat/completions, or the Anthropic Messages API (Claude Code).
func isLLMTurn(endpoint string) bool {
	return endpoint == "chat.completions" || endpoint == "completions" || endpoint == "messages"
}

type timingsJSON struct {
	PromptMS           *float64 `json:"prompt_ms"`
	PredictedMS        *float64 `json:"predicted_ms"`
	PromptPerSecond    *float64 `json:"prompt_per_second"`
	PredictedPerSecond *float64 `json:"predicted_per_second"`
}

type TokenStats struct {
	Ctx       int
	Cache     int
	Input     int
	Output    int
	Reasoning int
	Writing   int
	Est       bool

	PrefillTkS *float64
	DecodeTkS  *float64
}

func tokenStats(r *Record) *TokenStats {
	if len(r.Usage) == 0 {
		return nil
	}
	var u usageJSON
	if json.Unmarshal(r.Usage, &u) != nil {
		return nil
	}

	// Normalize both usage schemas to (pt = cache-inclusive prompt, cache, out). llama.cpp/OpenAI
	// report prompt_tokens (cache-INCLUSIVE); Anthropic reports input_tokens (cache-EXCLUDED) plus
	// cache_read/creation_input_tokens, so fold the cached counts back into pt.
	var pt, cache, out, reasoning int
	est := true
	switch {
	case u.PromptTokens > 0: // OpenAI / llama.cpp
		pt = u.PromptTokens
		if u.PromptDetails != nil {
			cache = u.PromptDetails.CachedTokens
		}
		out = u.CompletionTokens
		if u.CompletionDetails != nil && u.CompletionDetails.ReasoningTokens != nil {
			reasoning = *u.CompletionDetails.ReasoningTokens
			est = false
		}
	case u.InputTokens != nil || u.OutputTokens != nil: // Anthropic Messages API
		cache = u.CacheReadInputTokens + u.CacheCreationInputTokens
		if u.InputTokens != nil {
			pt = *u.InputTokens + cache
		} else {
			pt = cache
		}
		if u.OutputTokens != nil {
			out = *u.OutputTokens
		}
		// Anthropic reports no reasoning-token count -> char-ratio estimate below
	default:
		return nil
	}

	inp := pt - cache
	if inp < 0 {
		inp = 0
	}

	if est {
		// char-ratio estimate (matches show-log.py); handle both OpenAI and Anthropic tool shapes
		rchars := len(r.Reasoning)
		wchars := len(r.Completion)
		if len(r.ToolCalls) > 0 {
			var tcs []struct {
				Name     string          `json:"name"`
				Input    json.RawMessage `json:"input"`
				Function struct {
					Name      string `json:"name"`
					Arguments string `json:"arguments"`
				} `json:"function"`
			}
			if json.Unmarshal(r.ToolCalls, &tcs) == nil {
				for _, tc := range tcs {
					if tc.Function.Name != "" || tc.Function.Arguments != "" { // OpenAI
						wchars += len(tc.Function.Name) + len(tc.Function.Arguments)
					} else { // Anthropic: name + JSON(input)
						wchars += len(tc.Name) + len(tc.Input)
					}
				}
			}
		}
		total := rchars + wchars
		if total > 0 {
			reasoning = int(math.Round(float64(out) * float64(rchars) / float64(total)))
		}
	}

	writing := out - reasoning
	if writing < 0 {
		writing = 0
	}

	ts := &TokenStats{
		Ctx: pt, Cache: cache, Input: inp,
		Output: out, Reasoning: reasoning, Writing: writing, Est: est,
	}

	// throughput: prefer backend timings, fall back to gateway timestamps
	var tim timingsJSON
	if len(r.Timings) > 0 {
		json.Unmarshal(r.Timings, &tim)
	}

	if tim.PromptPerSecond != nil {
		ts.PrefillTkS = tim.PromptPerSecond
	} else if tim.PromptMS != nil && *tim.PromptMS > 0 {
		v := float64(inp) / (*tim.PromptMS / 1000.0)
		ts.PrefillTkS = &v
	} else if r.TTFTMs > 0 {
		v := float64(inp) / (float64(r.TTFTMs) / 1000.0)
		ts.PrefillTkS = &v
	}

	if tim.PredictedPerSecond != nil {
		ts.DecodeTkS = tim.PredictedPerSecond
	} else if tim.PredictedMS != nil && *tim.PredictedMS > 0 {
		v := float64(out) / (*tim.PredictedMS / 1000.0)
		ts.DecodeTkS = &v
	} else if r.DecodeMS > 0 {
		v := float64(out) / (float64(r.DecodeMS) / 1000.0)
		ts.DecodeTkS = &v
	}

	return ts
}

func toolCallCount(r *Record) int {
	if len(r.ToolCalls) == 0 {
		return 0
	}
	var tcs []json.RawMessage
	if json.Unmarshal(r.ToolCalls, &tcs) != nil {
		return 0
	}
	return len(tcs)
}

const sparklineSize = 40

type Stats struct {
	Turns           int
	Errors          int
	InputTokens     int64
	CacheTokens     int64
	OutputTokens    int64
	ReasoningTokens int64
	WritingTokens   int64

	SumLatencyMS  int64
	SumTTFTMS     int64
	TTFTCount     int
	SumPrefillMS  int64
	PrefillTokens int64
	SumDecodeMS   int64
	DecodeTokens  int64

	LatencyRing [sparklineSize]int64
	RingPos     int
	RingFull    bool
}

func (s *Stats) Update(r *Record) {
	if !isLLMTurn(r.Endpoint) {
		return
	}
	s.Turns++
	if r.Status >= 400 || r.Error != "" {
		s.Errors++
	}

	lat := r.TotalMS
	if lat == 0 {
		lat = r.LatencyMS
	}
	s.SumLatencyMS += lat
	s.LatencyRing[s.RingPos] = lat
	s.RingPos = (s.RingPos + 1) % sparklineSize
	if s.RingPos == 0 {
		s.RingFull = true
	}

	if r.TTFTMs > 0 {
		s.SumTTFTMS += r.TTFTMs
		s.TTFTCount++
	}

	ts := tokenStats(r)
	if ts == nil {
		return
	}
	s.InputTokens += int64(ts.Input)
	s.CacheTokens += int64(ts.Cache)
	s.OutputTokens += int64(ts.Output)
	s.ReasoningTokens += int64(ts.Reasoning)
	s.WritingTokens += int64(ts.Writing)

	if ts.PrefillTkS != nil && ts.Input > 0 {
		s.PrefillTokens += int64(ts.Input)
		if len(r.Timings) > 0 {
			var tim timingsJSON
			if json.Unmarshal(r.Timings, &tim) == nil && tim.PromptMS != nil {
				s.SumPrefillMS += int64(*tim.PromptMS)
			}
		} else if r.TTFTMs > 0 {
			s.SumPrefillMS += r.TTFTMs
		}
	}

	if ts.DecodeTkS != nil && ts.Output > 0 {
		s.DecodeTokens += int64(ts.Output)
		if len(r.Timings) > 0 {
			var tim timingsJSON
			if json.Unmarshal(r.Timings, &tim) == nil && tim.PredictedMS != nil {
				s.SumDecodeMS += int64(*tim.PredictedMS)
			}
		} else if r.DecodeMS > 0 {
			s.SumDecodeMS += r.DecodeMS
		}
	}
}

func (s *Stats) AvgLatencyMS() float64 {
	if s.Turns == 0 {
		return 0
	}
	return float64(s.SumLatencyMS) / float64(s.Turns)
}

func (s *Stats) AvgTTFTMS() float64 {
	if s.TTFTCount == 0 {
		return 0
	}
	return float64(s.SumTTFTMS) / float64(s.TTFTCount)
}

func (s *Stats) PrefillTkS() float64 {
	if s.SumPrefillMS == 0 {
		return 0
	}
	return float64(s.PrefillTokens) / (float64(s.SumPrefillMS) / 1000.0)
}

func (s *Stats) DecodeTkS() float64 {
	if s.SumDecodeMS == 0 {
		return 0
	}
	return float64(s.DecodeTokens) / (float64(s.SumDecodeMS) / 1000.0)
}

func (s *Stats) Sparkline() string {
	blocks := []rune("▁▂▃▄▅▆▇█")
	n := sparklineSize
	if !s.RingFull {
		n = s.RingPos
	}
	if n == 0 {
		return ""
	}

	var vals []int64
	if s.RingFull {
		for i := 0; i < sparklineSize; i++ {
			vals = append(vals, s.LatencyRing[(s.RingPos+i)%sparklineSize])
		}
	} else {
		vals = s.LatencyRing[:n]
	}

	lo, hi := vals[0], vals[0]
	for _, v := range vals[1:] {
		if v < lo {
			lo = v
		}
		if v > hi {
			hi = v
		}
	}

	out := make([]rune, len(vals))
	span := hi - lo
	for i, v := range vals {
		if span == 0 {
			out[i] = blocks[0]
		} else {
			idx := int(float64(v-lo) / float64(span) * float64(len(blocks)-1))
			if idx >= len(blocks) {
				idx = len(blocks) - 1
			}
			out[i] = blocks[idx]
		}
	}
	return string(out)
}
