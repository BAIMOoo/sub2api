package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

// 官方 Codex 按 Responses Lite 模式选择工具布局：Lite 使用 input.additional_tools，
// 标准模式使用顶层 tools。这里显式验证 Lite 的第三方函数工具兼容变换。
func TestApplyCodexOAuthTransform_PromotesTopLevelToolsToAdditionalToolsNamespace(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-sol",
		"input": []any{
			map[string]any{"type": "message", "role": "user", "content": "hello"},
		},
		"tools": []any{
			map[string]any{
				"type": "function",
				"function": map[string]any{
					"name":        "get_weather",
					"description": "Look up the weather.",
					"parameters":  map[string]any{"type": "object", "properties": map[string]any{}},
					"strict":      true,
				},
			},
		},
		"tool_choice": "auto",
	}

	result := applyCodexOAuthTransformWithOptions(reqBody, codexOAuthTransformOptions{ResponsesLite: true})
	require.True(t, result.Modified)

	_, hasTopLevelTools := reqBody["tools"]
	require.False(t, hasTopLevelTools, "Lite 函数工具应进入 additional_tools")

	input, ok := reqBody["input"].([]any)
	require.True(t, ok)
	require.Len(t, input, 2)
	first, ok := input[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "additional_tools", first["type"])
	require.Equal(t, "developer", first["role"])
	message, ok := input[1].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "message", message["type"], "工具的声明项放在 input 最前")

	namespaces, ok := first["tools"].([]any)
	require.True(t, ok)
	require.Len(t, namespaces, 1)
	namespace, ok := namespaces[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "namespace", namespace["type"])
	require.Equal(t, codexAdditionalToolsNamespaceName, namespace["name"])
	require.NotEmpty(t, namespace["description"], "上游要求命名空间描述非空")

	entries, ok := namespace["tools"].([]any)
	require.True(t, ok)
	require.Len(t, entries, 1)
	entry, ok := entries[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "function", entry["type"])
	require.Equal(t, "get_weather", entry["name"], "命名空间内的函数工具是平铺形态")
	require.Equal(t, "Look up the weather.", entry["description"])
	require.Equal(t, true, entry["strict"])
	require.NotContains(t, entry, "function", "不再保留 function 嵌套")
	require.Contains(t, entry, "parameters")
}

// Chat Completions 遗留的 functions 先被转成顶层 tools，再被搬进命名空间：两步都生效。
func TestApplyCodexOAuthTransform_PromotesLegacyFunctionsIntoNamespace(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-sol",
		"input": []any{map[string]any{"type": "message", "role": "user", "content": "hi"}},
		"functions": []any{
			map[string]any{"name": "lookup", "description": "Look something up.", "parameters": map[string]any{"type": "object"}},
		},
	}

	applyCodexOAuthTransformWithOptions(reqBody, codexOAuthTransformOptions{ResponsesLite: true})

	require.NotContains(t, reqBody, "functions")
	require.NotContains(t, reqBody, "tools")
	input, ok := reqBody["input"].([]any)
	require.True(t, ok)
	first, ok := input[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "additional_tools", first["type"])
	namespaces, ok := first["tools"].([]any)
	require.True(t, ok)
	namespace, ok := namespaces[0].(map[string]any)
	require.True(t, ok)
	entries, ok := namespace["tools"].([]any)
	require.True(t, ok)
	entry, ok := entries[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "lookup", entry["name"])
}

// 出现非 function 工具（如内置的 web_search）时，搬迁函数保持原样，不丢工具。
func TestApplyCodexOAuthTransform_KeepsTopLevelToolsWhenNotAllFunctions(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-sol",
		"input": []any{map[string]any{"type": "message", "role": "user", "content": "hi"}},
		"tools": []any{
			map[string]any{"type": "function", "function": map[string]any{"name": "a", "description": "A."}},
			map[string]any{"type": "web_search"},
		},
	}

	applyCodexOAuthTransformWithOptions(reqBody, codexOAuthTransformOptions{ResponsesLite: true})

	tools, ok := reqBody["tools"].([]any)
	require.True(t, ok, "混合工具类型时保持顶层 tools")
	require.Len(t, tools, 2)
	input, ok := reqBody["input"].([]any)
	require.True(t, ok)
	message, ok := input[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "message", message["type"], "不插入 additional_tools")
}

// 缺 description 时保持原样（上游对命名空间内工具要求描述非空）。
func TestApplyCodexOAuthTransform_KeepsTopLevelToolsWithoutDescription(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-sol",
		"input": []any{map[string]any{"type": "message", "role": "user", "content": "hi"}},
		"tools": []any{
			map[string]any{"type": "function", "function": map[string]any{"name": "no_desc"}},
		},
	}

	applyCodexOAuthTransformWithOptions(reqBody, codexOAuthTransformOptions{ResponsesLite: true})

	require.Contains(t, reqBody, "tools")
	require.NotContains(t, reqBody, "input.additional_tools")
	input, ok := reqBody["input"].([]any)
	require.True(t, ok)
	message, ok := input[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "message", message["type"])
}

// input 不是数组（例如老的字符串 prompt）时不动，避免把用户输入顶掉。
// 直接测搬迁函数本身：完整变换会在别的步骤把字符串 prompt 归一化成消息数组，
// 那条路径与"搬迁时的防御"不是一回事。
func TestPromoteTopLevelToolsToAdditionalTools_SkipsNonArrayInput(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-sol",
		"input": "just a string prompt",
		"tools": []any{
			map[string]any{"type": "function", "function": map[string]any{"name": "a", "description": "A."}},
		},
	}

	require.False(t, promoteTopLevelToolsToAdditionalTools(reqBody, false))

	require.Contains(t, reqBody, "tools")
	require.Equal(t, "just a string prompt", reqBody["input"])
}

// 客户端点名某个函数时，搬进命名空间后仍要认得它，不能把 tool_choice 悄悄降级成 auto。
func TestApplyCodexOAuthTransform_KeepsNamedFunctionToolChoiceAfterPromotion(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-sol",
		"input": []any{map[string]any{"type": "message", "role": "user", "content": "hi"}},
		"tools": []any{
			map[string]any{"type": "function", "function": map[string]any{"name": "get_weather", "description": "Weather."}},
		},
		"tool_choice": map[string]any{"type": "function", "name": "get_weather"},
	}

	applyCodexOAuthTransformWithOptions(reqBody, codexOAuthTransformOptions{ResponsesLite: true})

	choice, ok := reqBody["tool_choice"].(map[string]any)
	require.True(t, ok, "点名函数时不应被降级为字符串 auto")
	require.Equal(t, "get_weather", choice["name"])
	require.NotContains(t, reqBody, "tools")
}

// compact 端点形态不同，不做搬迁。
func TestApplyCodexOAuthTransform_CompactKeepsTopLevelTools(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-sol",
		"input": []any{map[string]any{"type": "message", "role": "user", "content": "hi"}},
		"tools": []any{
			map[string]any{"type": "function", "function": map[string]any{"name": "a", "description": "A."}},
		},
	}

	applyCodexOAuthTransformWithOptions(reqBody, codexOAuthTransformOptions{ResponsesLite: true, IsCompact: true})

	require.Contains(t, reqBody, "tools")
}

func TestApplyCodexOAuthTransform_StandardKeepsNativeTopLevelTools(t *testing.T) {
	tool := map[string]any{
		"type": "function", "name": "echo", "description": "Echo a value.",
		"parameters": map[string]any{"type": "object"}, "strict": true,
	}
	reqBody := map[string]any{
		"model": "gpt-5.6-sol", "instructions": "Caller base instructions.",
		"input": []any{map[string]any{"type": "message", "role": "user", "content": "hello"}},
		"tools": []any{tool}, "tool_choice": "auto",
	}
	result := applyCodexOAuthTransformWithOptions(reqBody, codexOAuthTransformOptions{SkipDefaultInstructions: true})
	require.NoError(t, result.Error)
	require.Equal(t, []any{tool}, reqBody["tools"])
	input, ok := reqBody["input"].([]any)
	require.True(t, ok)
	require.Len(t, input, 1, "standard tools must not add a Lite carrier")
	require.Equal(t, "Caller base instructions.", reqBody["instructions"])
}

func TestApplyCodexOAuthTransform_LiteKeepsNativeAdditionalTools(t *testing.T) {
	tools := []any{map[string]any{
		"type": "namespace", "name": "functions", "description": "Function tools.",
		"tools": []any{map[string]any{"type": "function", "name": "echo", "description": "Echo a value."}},
	}}
	reqBody := map[string]any{
		"model": "gpt-5.6-sol", "instructions": "", "tool_choice": "auto",
		"input": []any{
			map[string]any{"type": "additional_tools", "id": "at_native", "role": "developer", "tools": tools},
			map[string]any{"type": "message", "id": "msg_native", "role": "developer", "content": "Caller base instructions."},
		},
	}
	result := applyCodexOAuthTransformWithOptions(reqBody, codexOAuthTransformOptions{
		ResponsesLite: true, KeepSystemMessagesInInput: true, SkipDefaultInstructions: true,
	})
	require.NoError(t, result.Error)
	require.NotContains(t, reqBody, "tools")
	require.Equal(t, "", reqBody["instructions"], "native empty instructions must not acquire defaults")
	input, ok := reqBody["input"].([]any)
	require.True(t, ok)
	require.Len(t, input, 2, "native Lite carrier must not be duplicated")
	carrier, ok := input[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "additional_tools", carrier["type"])
	require.Equal(t, "at_native", carrier["id"])
	require.Equal(t, tools, carrier["tools"])
	message, ok := input[1].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "msg_native", message["id"])
}

func TestApplyCodexOAuthTransform_StandardKeepsNativeInstructions(t *testing.T) {
	reqBody := map[string]any{
		"model": "gpt-5.6-sol", "instructions": "Caller base instructions.",
		"input": []any{map[string]any{"type": "message", "role": "developer", "content": "Caller developer message."}},
		"tools": []any{}, "tool_choice": "auto",
	}
	result := applyCodexOAuthTransformWithOptions(reqBody, codexOAuthTransformOptions{SkipDefaultInstructions: true})
	require.NoError(t, result.Error)
	require.Equal(t, "Caller base instructions.", reqBody["instructions"])
	require.Len(t, reqBody["input"], 1)
}
