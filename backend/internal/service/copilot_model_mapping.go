package service

import "strings"

// normalizeCopilotModel strips Anthropic-style date version suffixes from
// model names and converts hyphen-separated version numbers to dot-separated
// notation so they are compatible with GitHub Copilot's naming conventions.
//
// Anthropic publishes models with trailing 8-digit date stamps
// (e.g. claude-haiku-4-5-20251001), but GitHub Copilot only recognises the
// canonical short names.  Copilot also uses dot-separated version numbers
// (e.g. claude-sonnet-4.6) whereas Anthropic uses hyphens (claude-sonnet-4-6).
//
// Examples:
//
//	claude-haiku-4-5-20251001  → claude-haiku-4.5
//	claude-sonnet-4-5-20250929 → claude-sonnet-4.5
//	claude-opus-4-5-20251101   → claude-opus-4.5
//	claude-sonnet-4-6          → claude-sonnet-4.6
//	claude-opus-4.5            → claude-opus-4.5   (already dot-notation, unchanged)
//	gpt-4o                     → gpt-4o            (unchanged)
func normalizeCopilotModel(model string) string {
	// Step 1: strip date suffix "-YYYYMMDD" (hyphen + 8 digits).
	// Require the base portion to be at least 2 chars ("o1" is the shortest
	// real model name), so the minimum total length to attempt stripping is
	// 2 + 9 = 11.
	if len(model) > 10 {
		pos := len(model) - 9
		if model[pos] == '-' {
			suffix := model[pos+1:] // 8 chars
			allDigits := true
			for _, c := range suffix {
				if c < '0' || c > '9' {
					allDigits = false
					break
				}
			}
			if allDigits {
				model = model[:pos]
			}
		}
	}

	// Step 2: convert trailing hyphen-separated version numbers to dot notation.
	// Only converts when BOTH the last and second-to-last hyphen-separated segments
	// are short digit strings (1-2 digits), matching version patterns like "4-6".
	// Long digit strings (e.g. "2025100") are NOT treated as version numbers.
	// e.g. "claude-sonnet-4-6"  → "claude-sonnet-4.6"
	//      "claude-haiku-4-5"   → "claude-haiku-4.5"
	//      "gpt-4o"             → "gpt-4o"  (unchanged, "4o" not all-digits)
	//      "claude-haiku-4-5-2025100" → unchanged (last segment too long)
	lastHyphen := strings.LastIndex(model, "-")
	if lastHyphen >= 0 {
		minor := model[lastHyphen+1:]
		if len(minor) >= 1 && len(minor) <= 2 && allASCIIDigits(minor) {
			before := model[:lastHyphen]
			prevHyphen := strings.LastIndex(before, "-")
			if prevHyphen >= 0 {
				major := before[prevHyphen+1:]
				if len(major) >= 1 && len(major) <= 2 && allASCIIDigits(major) {
					model = before[:prevHyphen+1] + major + "." + minor
				}
			}
		}
	}

	return model
}

// allASCIIDigits returns true if s is non-empty and contains only ASCII digit characters.
func allASCIIDigits(s string) bool {
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
