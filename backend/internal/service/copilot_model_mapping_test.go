//go:build unit

package service

import "testing"

func TestNormalizeCopilotModel(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		// date suffixes must be stripped AND version converted to dot notation
		{"claude-haiku-4-5-20251001", "claude-haiku-4.5"},
		{"claude-sonnet-4-5-20250929", "claude-sonnet-4.5"},
		{"claude-opus-4-5-20251101", "claude-opus-4.5"},
		{"claude-sonnet-4-6-20260101", "claude-sonnet-4.6"},

		// hyphen version → dot notation (no date suffix)
		{"claude-haiku-4-5", "claude-haiku-4.5"},
		{"claude-sonnet-4-6", "claude-sonnet-4.6"},
		{"claude-opus-4-7", "claude-opus-4.7"},

		// already dot notation — unchanged
		{"claude-opus-4.5", "claude-opus-4.5"},
		{"claude-sonnet-4.6", "claude-sonnet-4.6"},

		// non-claude models — unchanged
		{"gpt-4o", "gpt-4o"},
		{"gpt-4o-mini", "gpt-4o-mini"},
		{"o1", "o1"},

		// edge: suffix-like but not 8 digits — no date strip, but version conversion applies
		{"claude-haiku-4-5-2025100", "claude-haiku-4-5-2025100"},    // 7 digits → not a date suffix, last segment too long → unchanged
		{"claude-haiku-4-5-202510011", "claude-haiku-4-5-202510011"}, // 9 digits → not a date, last segment too long → unchanged
		{"claude-haiku-4-5-2025abcd", "claude-haiku-4-5-2025abcd"},  // non-digits → no conversion

		// edge: base too short (< 2 chars) — date stripping skipped
		{"a-20251001", "a-20251001"}, // 10 chars total, base would be 1 char — skipped
		{"ab-20251001", "ab"},        // 11 chars total, base is 2 chars — date stripped, no version digits
		{"", ""},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := normalizeCopilotModel(tt.input)
			if got != tt.want {
				t.Errorf("normalizeCopilotModel(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}
