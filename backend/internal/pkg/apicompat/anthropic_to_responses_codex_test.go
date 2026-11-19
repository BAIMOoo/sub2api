package apicompat

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestAnthropicToResponsesPreservesFixedCodexContent(t *testing.T) {
	raw := json.RawMessage(`{
		"model":"gpt-5.5",
		"max_tokens":16,
		"system":[
			{"type":"text","text":"x-anthropic-billing-header: keep\n"},
			{"type":"text","text":""},
			{"type":"text","text":"  system\tinstructions\n"}
		],
		"messages":[
			{"role":"user","content":[
				{"type":"text","text":"  user\n"},
				{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AQID"}},
				{"type":"text","text":""}
			]},
			{"role":"assistant","content":[
				{"type":"text","text":"before"},
				{"type":"thinking","thinking":"plan","signature":"gAAAA-reasoning"},
				{"type":"tool_use","id":"toolu_1","name":"lookup","input":{"id":9007199254740993}},
				{"type":"text","text":"after"}
			]},
			{"role":"user","content":[
				{"type":"text","text":"before result"},
				{"type":"tool_result","tool_use_id":"toolu_1","is_error":true,"content":[
					{"type":"image","source":{"type":"url","url":"https://example.test/result.png"}},
					{"type":"text","text":"exact result"}
				]},
				{"type":"text","text":"after result"}
			]}
		],
		"tools":[{
			"name":"lookup",
			"description":"Lookup",
			"input_schema":{"type":"object","properties":{"id":{"type":"integer","const":9007199254740993}},"required":["id"]}
		}],
		"tool_choice":{"type":"tool","name":"lookup","disable_parallel_tool_use":true}
	}`)

	var request AnthropicRequest
	require.NoError(t, json.Unmarshal(raw, &request))
	converted, err := AnthropicToResponsesWithOptions(&request, AnthropicToResponsesOptions{
		MinOutputTokens: 16,
		Mode:            AnthropicConversionFixedCodexHTTP,
		OriginalRequest: raw,
	})
	require.NoError(t, err)

	wire, err := json.Marshal(converted)
	require.NoError(t, err)
	require.JSONEq(t, `[
		{"type":"message","role":"developer","content":[
			{"type":"input_text","text":"x-anthropic-billing-header: keep\n"},
			{"type":"input_text","text":""},
			{"type":"input_text","text":"  system\tinstructions\n"}
		]},
		{"type":"message","role":"user","content":[
			{"type":"input_text","text":"  user\n"},
			{"type":"input_image","image_url":"data:image/png;base64,AQID"},
			{"type":"input_text","text":""}
		]},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"before"}]},
		{"type":"reasoning","encrypted_content":"gAAAA-reasoning","summary":[{"type":"summary_text","text":"plan"}]},
		{"type":"function_call","call_id":"toolu_1","name":"lookup","arguments":"{\"id\":9007199254740993}"},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"after"}]},
		{"type":"message","role":"user","content":[{"type":"input_text","text":"before result"}]},
		{"type":"function_call_output","call_id":"toolu_1","output":[
			{"type":"input_text","text":"{\"is_error\":true}"},
			{"type":"input_image","image_url":"https://example.test/result.png"},
			{"type":"input_text","text":"exact result"}
		]},
		{"type":"message","role":"user","content":[{"type":"input_text","text":"after result"}]}
	]`, string(converted.Input))
	require.Contains(t, string(wire), "9007199254740993")
	require.Equal(t, "lookup", gjson.GetBytes(wire, "tools.0.name").String())
	require.Equal(t, "9007199254740993", gjson.GetBytes(wire, "tools.0.parameters.properties.id.const").Raw)
	require.False(t, *converted.ParallelToolCalls)
	require.JSONEq(t, `{"type":"function","name":"lookup"}`, string(converted.ToolChoice))
}

func TestAnthropicToResponsesRejectsUnrepresentableFixedCodexContent(t *testing.T) {
	for _, test := range []struct {
		name string
		raw  string
		path string
	}{
		{
			name: "unknown block",
			raw:  `{"model":"gpt-5.5","messages":[{"role":"user","content":[{"type":"document","source":{"type":"text","data":"lost"}}]}]}`,
			path: "messages[0].content[0]",
		},
		{
			name: "unsigned thinking",
			raw:  `{"model":"gpt-5.5","messages":[{"role":"assistant","content":[{"type":"thinking","thinking":"lost"}]}]}`,
			path: "signature",
		},
		{
			name: "duplicate tool call id",
			raw:  `{"model":"gpt-5.5","messages":[{"role":"assistant","content":[{"type":"tool_use","id":"same","name":"one","input":{}},{"type":"tool_use","id":"same","name":"two","input":{}}]}]}`,
			path: "same",
		},
		{
			name: "orphan tool result",
			raw:  `{"model":"gpt-5.5","messages":[{"role":"user","content":[{"type":"tool_result","tool_use_id":"missing","content":"lost"}]}]}`,
			path: "missing",
		},
		{
			name: "unknown tool field",
			raw:  `{"model":"gpt-5.5","tools":[{"name":"lookup","input_schema":{"type":"object"},"input_examples":[{"id":1}]}]}`,
			path: "input_examples",
		},
		{
			name: "tool choice undeclared",
			raw:  `{"model":"gpt-5.5","messages":[{"role":"user","content":"hi"}],"tool_choice":{"type":"tool","name":"missing"}}`,
			path: "tool_choice.name",
		},
		{
			name: "unknown thinking mode",
			raw:  `{"model":"gpt-5.5","thinking":{"type":"future","budget_tokens":12},"messages":[{"role":"user","content":"hi"}]}`,
			path: "thinking.type",
		},
		{
			name: "unknown output effort",
			raw:  `{"model":"gpt-5.5","output_config":{"effort":"future"},"messages":[{"role":"user","content":"hi"}]}`,
			path: "output_config.effort",
		},
		{
			name: "non object metadata",
			raw:  `{"model":"gpt-5.5","metadata":"lost","messages":[{"role":"user","content":"hi"}]}`,
			path: "metadata",
		},
		{
			name: "non string service tier",
			raw:  `{"model":"gpt-5.5","service_tier":7,"messages":[{"role":"user","content":"hi"}]}`,
			path: "service_tier",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			var request AnthropicRequest
			require.NoError(t, json.Unmarshal([]byte(test.raw), &request))
			_, err := AnthropicToResponsesWithOptions(&request, AnthropicToResponsesOptions{
				Mode:            AnthropicConversionFixedCodexHTTP,
				OriginalRequest: json.RawMessage(test.raw),
			})
			require.Error(t, err)
			require.ErrorContains(t, err, test.path)
		})
	}
}

func TestAnthropicToResponsesKeepsLegacyConversionForCompatibleMode(t *testing.T) {
	request := &AnthropicRequest{
		Model:  "gpt-5.5",
		System: json.RawMessage(`[{"type":"text","text":"x-anthropic-billing-header: task"},{"type":"text","text":"system"}]`),
		Messages: []AnthropicMessage{{
			Role: "assistant",
			Content: json.RawMessage(`[
				{"type":"thinking","thinking":"unsigned"},
				{"type":"text","text":"answer"}
			]`),
		}},
	}

	converted, err := AnthropicToResponses(request)
	require.NoError(t, err)
	require.JSONEq(t, `[
		{"type":"message","role":"developer","content":[{"type":"input_text","text":"system"}]},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"answer"}]}
	]`, string(converted.Input))
}
