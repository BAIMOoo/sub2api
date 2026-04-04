package service

// normalizeCopilotModel strips Anthropic-style date version suffixes from
// model names so they are compatible with GitHub Copilot's naming conventions.
//
// Anthropic publishes models with trailing 8-digit date stamps
// (e.g. claude-haiku-4-5-20251001), but GitHub Copilot only recognises the
// canonical short names (e.g. claude-haiku-4-5).  Sending a date-versioned
// name results in an immediate HTTP 400 from the Copilot API.
//
// Examples:
//
//	claude-haiku-4-5-20251001  → claude-haiku-4-5
//	claude-sonnet-4-5-20250929 → claude-sonnet-4-5
//	claude-opus-4-5-20251101   → claude-opus-4-5
//	claude-sonnet-4-6          → claude-sonnet-4-6  (unchanged)
//	gpt-4o                     → gpt-4o             (unchanged)
func normalizeCopilotModel(model string) string {
	// A date suffix is exactly "-YYYYMMDD": a hyphen followed by 8 digits.
	// Require the base portion to be at least 2 chars ("o1" is the shortest
	// real model name), so the minimum total length to attempt stripping is
	// 2 + 9 = 11.
	if len(model) <= 10 {
		return model
	}
	// Check the candidate suffix position.
	pos := len(model) - 9
	if model[pos] != '-' {
		return model
	}
	suffix := model[pos+1:] // 8 chars
	for _, c := range suffix {
		if c < '0' || c > '9' {
			return model
		}
	}
	return model[:pos]
}
