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
			map[string]any{"type": "function", "name": "lookup", "description": "Lookup"},
			map[string]any{"type": "custom", "name": "exec"},
			map[string]any{"type": "tool_search"},
			map[string]any{
				"type": "function",
				"function": map[string]any{
					"name": "nested", "description": "Nested function",
				},
			},
			map[string]any{"type": "namespace", "name": "collaboration"},
			"custom shorthand",
		},
		"input": []any{
			map[string]any{"type": "message", "role": "user", "content": "hello"},
			map[string]any{
				"type": "additional_tools", "id": "at_existing", "role": "developer",
				"tools": []any{
					map[string]any{"type": "function", "name": "existing", "description": "Existing"},
				},
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
	require.Len(t, tools, 2)
	functionsNamespace := tools[0].(map[string]any)
	require.Equal(t, "namespace", functionsNamespace["type"])
	require.Equal(t, "functions", functionsNamespace["name"])
	functionTools := functionsNamespace["tools"].([]any)
	require.Len(t, functionTools, 6)
	require.Equal(t, "existing", functionTools[0].(map[string]any)["name"])
	require.Equal(t, "function", functionTools[1].(map[string]any)["type"])
	require.Equal(t, "custom", functionTools[2].(map[string]any)["type"])
	require.Equal(t, "tool_search", functionTools[3].(map[string]any)["type"])
	require.NotContains(t, functionTools[4].(map[string]any), "function")
	require.Equal(t, "nested", functionTools[4].(map[string]any)["name"])
	require.Equal(t, "custom", functionTools[5].(map[string]any)["type"])
	require.Equal(t, "custom shorthand", functionTools[5].(map[string]any)["name"])
	require.Equal(t, "collaboration", tools[1].(map[string]any)["name"])

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

func TestCanonicalizeCodexExecLiteRequestReusesExistingBaseInstructions(t *testing.T) {
	tests := []struct {
		name      string
		topLevel  string
		existing  string
		wantTexts []string
		wantKinds []any
	}{
		{
			name:      "duplicate text",
			topLevel:  "Base instructions.",
			existing:  "Base instructions.",
			wantTexts: []string{"Base instructions."},
			wantKinds: []any{"model.base_instructions"},
		},
		{
			name:      "additional text",
			topLevel:  "Additional instructions.",
			existing:  "Existing base instructions.",
			wantTexts: []string{"Existing base instructions.", "Additional instructions."},
			wantKinds: []any{"model.base_instructions", "model.base_instructions"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reqBody := map[string]any{
				"instructions": tt.topLevel,
				"input": []any{
					map[string]any{
						"type": "message",
						"role": "developer",
						"content": []any{
							map[string]any{"type": "input_text", "text": tt.existing},
						},
						"internal_chat_message_metadata_passthrough": map[string]any{
							"content_item_kinds": []any{"model.base_instructions"},
						},
					},
					map[string]any{"type": "message", "role": "user", "content": "hello"},
				},
			}

			changed, err := canonicalizeCodexExecLiteRequest(reqBody)
			require.NoError(t, err)
			require.True(t, changed)
			require.NotContains(t, reqBody, "instructions")

			input := reqBody["input"].([]any)
			require.Len(t, input, 2)
			base := input[0].(map[string]any)
			require.Equal(t, tt.wantKinds,
				base["internal_chat_message_metadata_passthrough"].(map[string]any)["content_item_kinds"])
			content := base["content"].([]any)
			texts := make([]string, 0, len(content))
			for _, raw := range content {
				texts = append(texts, raw.(map[string]any)["text"].(string))
			}
			require.Equal(t, tt.wantTexts, texts)
		})
	}
}

func TestCanonicalizeCodexExecLiteRequestNormalizesExistingCarrier(t *testing.T) {
	reqBody := map[string]any{
		"input": []any{
			map[string]any{
				"type": "additional_tools",
				"role": "developer",
				"tools": []any{
					map[string]any{
						"type": "function", "name": "lookup", "description": "Lookup",
					},
					map[string]any{"type": "web_search"},
				},
			},
			map[string]any{"type": "message", "role": "user", "content": "hello"},
		},
	}

	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	require.NoError(t, err)
	require.True(t, changed)

	input := reqBody["input"].([]any)
	tools := input[0].(map[string]any)["tools"].([]any)
	require.Len(t, tools, 2)
	require.Equal(t, "namespace", tools[0].(map[string]any)["type"])
	require.Equal(t, "web_search", tools[1].(map[string]any)["type"])
}

func TestCanonicalizeCodexExecLiteRequestKeepsUndocumentedFunctionFlat(t *testing.T) {
	reqBody := map[string]any{
		"tools": []any{
			map[string]any{"type": "function", "name": "missing_description"},
		},
		"input": []any{
			map[string]any{"type": "message", "role": "user", "content": "hello"},
		},
	}

	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	require.NoError(t, err)
	require.True(t, changed)

	tools := reqBody["input"].([]any)[0].(map[string]any)["tools"].([]any)
	require.Len(t, tools, 1)
	require.Equal(t, "function", tools[0].(map[string]any)["type"])
}

func TestCanonicalizeCodexExecLiteRequestKeepsUnnamedCustomToolFlat(t *testing.T) {
	reqBody := map[string]any{
		"tools": []any{
			map[string]any{"type": "custom"},
		},
		"input": []any{
			map[string]any{"type": "message", "role": "user", "content": "hello"},
		},
	}

	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	require.NoError(t, err)
	require.True(t, changed)

	tools := reqBody["input"].([]any)[0].(map[string]any)["tools"].([]any)
	require.Len(t, tools, 1)
	require.Equal(t, "custom", tools[0].(map[string]any)["type"])
}

func TestCanonicalizeCodexExecLiteRequestRejectsEmptyCustomShorthand(t *testing.T) {
	reqBody := map[string]any{
		"tools": []any{""},
		"input": []any{
			map[string]any{"type": "message", "role": "user", "content": "hello"},
		},
	}

	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	require.ErrorContains(t, err, "custom tool must not be empty")
	require.False(t, changed)
}

func TestCanonicalizeCodexExecLiteRequestReusesExistingFunctionsNamespace(t *testing.T) {
	reqBody := map[string]any{
		"tools": []any{
			map[string]any{
				"type": "function", "name": "new_tool",
				"description": "New tool",
				"parameters":  map[string]any{"type": "object"},
			},
		},
		"input": []any{
			map[string]any{
				"type": "additional_tools",
				"role": "developer",
				"tools": []any{
					map[string]any{
						"type": "namespace", "name": "functions",
						"description": "Tools available in this session.",
						"tools": []any{
							map[string]any{"type": "function", "name": "existing_tool"},
						},
					},
				},
			},
			map[string]any{"type": "message", "role": "user", "content": "hello"},
		},
	}

	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	require.NoError(t, err)
	require.True(t, changed)

	input := reqBody["input"].([]any)
	carrier := input[0].(map[string]any)
	tools := carrier["tools"].([]any)
	require.Len(t, tools, 1)
	namespace := tools[0].(map[string]any)
	require.Equal(t, "functions", namespace["name"])
	namespaceTools := namespace["tools"].([]any)
	require.Len(t, namespaceTools, 2)
	require.Equal(t, "existing_tool", namespaceTools[0].(map[string]any)["name"])
	require.Equal(t, "new_tool", namespaceTools[1].(map[string]any)["name"])
}

func TestCanonicalizeCodexExecLiteRequestNormalizesFunctionsNamespaceChildren(t *testing.T) {
	reqBody := map[string]any{
		"tools": []any{
			map[string]any{
				"type": "function", "name": "new_tool", "description": "New tool",
			},
		},
		"input": []any{
			map[string]any{
				"type": "additional_tools",
				"tools": []any{
					map[string]any{
						"type": "namespace", "name": "functions",
						"children": []any{
							map[string]any{
								"type": "function",
								"function": map[string]any{
									"name": "nested", "description": "Nested tool",
								},
							},
						},
					},
				},
			},
			map[string]any{"type": "message", "role": "user", "content": "hello"},
		},
	}

	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	require.NoError(t, err)
	require.True(t, changed)

	input := reqBody["input"].([]any)
	namespace := input[0].(map[string]any)["tools"].([]any)[0].(map[string]any)
	require.NotContains(t, namespace, "children")
	namespaceTools := namespace["tools"].([]any)
	require.Len(t, namespaceTools, 2)
	require.NotContains(t, namespaceTools[0].(map[string]any), "function")
	require.Equal(t, "nested", namespaceTools[0].(map[string]any)["name"])
	require.Equal(t, "new_tool", namespaceTools[1].(map[string]any)["name"])
}

func TestCanonicalizeCodexExecLiteRequestConvertsEmptyChildrenToToolsArray(t *testing.T) {
	reqBody := map[string]any{
		"input": []any{
			map[string]any{
				"type": "additional_tools",
				"tools": []any{
					map[string]any{
						"type": "namespace", "name": "functions", "children": []any{},
					},
				},
			},
		},
	}

	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	require.NoError(t, err)
	require.True(t, changed)

	namespace := reqBody["input"].([]any)[0].(map[string]any)["tools"].([]any)[0].(map[string]any)
	tools, ok := namespace["tools"].([]any)
	require.True(t, ok)
	require.NotNil(t, tools)
	require.Empty(t, tools)
	require.NotContains(t, namespace, "children")
}

func TestCanonicalizeCodexExecLiteRequestMergesMovedFunctionsNamespace(t *testing.T) {
	reqBody := map[string]any{
		"tools": []any{
			map[string]any{
				"type": "namespace", "name": "functions",
				"description": "Caller-provided namespace.",
				"tools": []any{
					map[string]any{"type": "custom", "name": "existing_tool"},
				},
			},
			map[string]any{
				"type": "function", "name": "new_tool",
				"description": "New tool",
				"parameters":  map[string]any{"type": "object"},
			},
		},
		"input": []any{
			map[string]any{"type": "message", "role": "user", "content": "hello"},
		},
	}

	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	require.NoError(t, err)
	require.True(t, changed)

	input := reqBody["input"].([]any)
	carrier := input[0].(map[string]any)
	tools := carrier["tools"].([]any)
	require.Len(t, tools, 1)
	namespace := tools[0].(map[string]any)
	require.Equal(t, "Caller-provided namespace.", namespace["description"])
	namespaceTools := namespace["tools"].([]any)
	require.Len(t, namespaceTools, 2)
	require.Equal(t, "existing_tool", namespaceTools[0].(map[string]any)["name"])
	require.Equal(t, "new_tool", namespaceTools[1].(map[string]any)["name"])
}

func TestCanonicalizeCodexExecLiteRequestOmitsEmptyCarrierWhenNoToolsDeclared(t *testing.T) {
	t.Run("no tools field", func(t *testing.T) {
		reqBody := map[string]any{
			"instructions": "Base instructions.",
			"input": []any{
				map[string]any{"type": "message", "role": "user", "content": "hello"},
			},
		}
		changed, err := canonicalizeCodexExecLiteRequest(reqBody)
		require.NoError(t, err)
		require.True(t, changed)
		require.NotContains(t, reqBody, "instructions")
		require.NotContains(t, reqBody, "tools")
		input := reqBody["input"].([]any)
		require.Len(t, input, 2)
		require.Equal(t, "developer", input[0].(map[string]any)["role"])
		require.Equal(t, "user", input[1].(map[string]any)["role"])
	})

	t.Run("empty top-level tools array", func(t *testing.T) {
		reqBody := map[string]any{
			"tools": []any{},
			"input": []any{
				map[string]any{"type": "message", "role": "user", "content": "hello"},
			},
		}
		changed, err := canonicalizeCodexExecLiteRequest(reqBody)
		require.NoError(t, err)
		require.True(t, changed)
		require.NotContains(t, reqBody, "tools")
		input := reqBody["input"].([]any)
		require.Len(t, input, 1)
		require.Equal(t, "user", input[0].(map[string]any)["role"])
	})

	t.Run("id-less empty additional_tools carrier omitted", func(t *testing.T) {
		reqBody := map[string]any{
			"input": []any{
				map[string]any{"type": "additional_tools", "role": "developer", "tools": []any{}},
				map[string]any{"type": "message", "role": "user", "content": "hello"},
			},
		}
		changed, err := canonicalizeCodexExecLiteRequest(reqBody)
		require.NoError(t, err)
		require.True(t, changed)
		input := reqBody["input"].([]any)
		require.Len(t, input, 1)
		require.Equal(t, "user", input[0].(map[string]any)["role"])
	})

	t.Run("referenced empty additional_tools carrier with id preserved", func(t *testing.T) {
		reqBody := map[string]any{
			"input": []any{
				map[string]any{"type": "additional_tools", "id": "at_keep", "role": "developer", "tools": []any{}},
				map[string]any{"type": "item_reference", "id": "at_keep"},
			},
		}
		changed, err := canonicalizeCodexExecLiteRequest(reqBody)
		require.NoError(t, err)
		require.False(t, changed)
		input := reqBody["input"].([]any)
		require.Len(t, input, 2)
		require.Equal(t, "at_keep", input[0].(map[string]any)["id"])
	})
}

func TestPromoteCodexInstructionsToDeveloperInputHandlesEmptyAndInvalidTypes(t *testing.T) {
	t.Run("deletes empty string without injecting message", func(t *testing.T) {
		reqBody := map[string]any{
			"instructions": "   ",
			"input":        []any{map[string]any{"type": "message", "role": "user", "content": "hello"}},
		}
		changed, err := promoteCodexInstructionsToDeveloperInput(reqBody)
		require.NoError(t, err)
		require.True(t, changed)
		require.NotContains(t, reqBody, "instructions")
		require.Len(t, reqBody["input"].([]any), 1)
	})

	t.Run("deletes null without injecting message", func(t *testing.T) {
		reqBody := map[string]any{
			"instructions": nil,
			"input":        []any{map[string]any{"type": "message", "role": "user", "content": "hello"}},
		}
		changed, err := promoteCodexInstructionsToDeveloperInput(reqBody)
		require.NoError(t, err)
		require.True(t, changed)
		require.NotContains(t, reqBody, "instructions")
		require.Len(t, reqBody["input"].([]any), 1)
	})

	t.Run("rejects non-string instructions with 400 validation error", func(t *testing.T) {
		reqBody := map[string]any{
			"instructions": 123,
			"input":        []any{map[string]any{"type": "message", "role": "user", "content": "hello"}},
		}
		changed, err := promoteCodexInstructionsToDeveloperInput(reqBody)
		require.False(t, changed)
		var validationErr *openAIResponsesLiteValidationError
		require.ErrorAs(t, err, &validationErr)
		require.Equal(t, "instructions", validationErr.param)
	})
}
