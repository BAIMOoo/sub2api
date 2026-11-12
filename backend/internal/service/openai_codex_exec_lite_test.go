//go:build unit

package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestCanonicalizeCodexExecLiteRequestMovesInstructionsAndAllToolsToPrefix(t *testing.T) {
	reqBody := map[string]any{
		"instructions": "Base instructions.",
		"tools": []any{
			map[string]any{"type": "function", "name": "lookup"},
			map[string]any{"type": "custom", "name": "exec"},
			map[string]any{"type": "tool_search"},
			map[string]any{"type": "namespace", "name": "collaboration"},
			"custom shorthand",
		},
		"input": []any{
			map[string]any{"type": "message", "role": "user", "content": "hello"},
			map[string]any{
				"type": "additional_tools", "id": "at_existing", "role": "developer",
				"tools": []any{map[string]any{"type": "function", "name": "existing"}},
			},
		},
	}

	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	require.NoError(t, err)
	require.True(t, changed)
	require.NotContains(t, reqBody, "instructions")
	require.NotContains(t, reqBody, "tools")

	input := reqBody["input"].([]any)
	require.Len(t, input, 3)
	carrier := input[0].(map[string]any)
	require.Equal(t, "additional_tools", carrier["type"])
	require.Equal(t, "at_existing", carrier["id"])
	tools := carrier["tools"].([]any)
	require.Len(t, tools, 6)
	require.Equal(t, "existing", tools[0].(map[string]any)["name"])
	require.Equal(t, "function", tools[1].(map[string]any)["type"])
	require.Equal(t, "custom", tools[2].(map[string]any)["type"])
	require.Equal(t, "tool_search", tools[3].(map[string]any)["type"])
	require.Equal(t, "namespace", tools[4].(map[string]any)["type"])
	require.Equal(t, "custom shorthand", tools[5])

	instructions := input[1].(map[string]any)
	require.Equal(t, "message", instructions["type"])
	require.Equal(t, "developer", instructions["role"])
	require.Equal(t, "Base instructions.", instructions["content"].([]any)[0].(map[string]any)["text"])
	require.Equal(t, []any{"model.base_instructions"},
		instructions["internal_chat_message_metadata_passthrough"].(map[string]any)["content_item_kinds"])
	require.Equal(t, "user", input[2].(map[string]any)["role"])

	changed, err = canonicalizeCodexExecLiteRequest(reqBody)
	require.NoError(t, err)
	require.False(t, changed)
}
