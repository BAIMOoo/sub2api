package apicompat

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestChatCompletionsToResponsesPreservesCodexContent(t *testing.T) {
	const body = `{
		"model":"gpt-5.5",
		"instructions":"KEEP TOP INSTRUCTIONS",
		"messages":[
			{"role":"system","content":""},
			{"role":"developer","content":[{"type":"text","text":"KEEP DEVELOPER"}]},
			{"role":"user","content":[
				{"type":"image_url","image_url":{"url":"data:image/png;base64,aGVsbG8=","detail":"high"}},
				{"type":"file","file":{"filename":"notes.txt","file_data":"data:text/plain;base64,aGk="}}
			]},
			{"role":"assistant","content":"","tool_calls":[{"id":"call_read","type":"function","function":{"name":"read_file","arguments":"{\"path\":\"README.md\"}"}}]},
			{"role":"tool","tool_call_id":"call_read","content":""}
		],
		"tools":[{"type":"function","function":{"name":"read_file","description":"Read","parameters":{"type":"object","properties":{"id":{"type":"integer","const":9007199254740993}}}}}],
		"tool_choice":{"type":"function","function":{"name":"read_file"}}
	}`

	var request ChatCompletionsRequest
	require.NoError(t, json.Unmarshal([]byte(body), &request))
	converted, err := ChatCompletionsToResponsesWithOptions(
		&request,
		DefaultMinMaxOutputTokens,
		ChatCompletionsToResponsesOptions{Mode: ChatCompletionsConversionFixedCodexHTTP},
	)
	require.NoError(t, err)
	wire, err := json.Marshal(converted)
	require.NoError(t, err)

	require.Equal(t, "KEEP TOP INSTRUCTIONS", gjson.GetBytes(wire, "instructions").String())
	require.Equal(t, "", gjson.GetBytes(wire, "input.0.content").String())
	require.Equal(t, "KEEP DEVELOPER", gjson.GetBytes(wire, "input.1.content.0.text").String())
	require.Equal(t, "data:image/png;base64,aGVsbG8=", gjson.GetBytes(wire, "input.2.content.0.image_url").String())
	require.Equal(t, "high", gjson.GetBytes(wire, "input.2.content.0.detail").String())
	require.Equal(t, "notes.txt", gjson.GetBytes(wire, "input.2.content.1.filename").String())
	require.Equal(t, "call_read", gjson.GetBytes(wire, `input.#(type=="function_call").call_id`).String())
	require.JSONEq(t, `{"path":"README.md"}`, gjson.GetBytes(wire, `input.#(type=="function_call").arguments`).String())
	require.Equal(t, "", gjson.GetBytes(wire, `input.#(type=="function_call_output").output`).String())
	require.Equal(t, "9007199254740993", gjson.GetBytes(wire, "tools.0.parameters.properties.id.const").Raw)
	require.Equal(t, "read_file", gjson.GetBytes(wire, "tool_choice.name").String())
}

func TestChatCompletionsToResponsesRejectsUnrepresentableCodexContent(t *testing.T) {
	for _, test := range []struct {
		name string
		body string
	}{
		{
			name: "reasoning text",
			body: `{"model":"gpt-5.5","messages":[{"role":"assistant","reasoning_content":"opaque","content":""}]}`,
		},
		{
			name: "unsupported content part",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":[{"type":"audio","audio":{"id":"a1"}}]}]}`,
		},
		{
			name: "duplicate tool call",
			body: `{"model":"gpt-5.5","messages":[{"role":"assistant","content":"","tool_calls":[{"id":"call_1","type":"function","function":{"name":"one","arguments":"{}"}},{"id":"call_1","type":"function","function":{"name":"two","arguments":"{}"}}]}]}`,
		},
		{
			name: "orphan legacy result",
			body: `{"model":"gpt-5.5","messages":[{"role":"function","name":"lookup","content":"result"}]}`,
		},
		{
			name: "orphan tool result",
			body: `{"model":"gpt-5.5","messages":[{"role":"tool","tool_call_id":"missing","content":"result"}]}`,
		},
		{
			name: "ambiguous legacy function calls",
			body: `{"model":"gpt-5.5","messages":[{"role":"assistant","content":null,"function_call":{"name":"lookup","arguments":"{}"}},{"role":"assistant","content":null,"function_call":{"name":"lookup","arguments":"{}"}},{"role":"function","name":"lookup","content":"result"}]}`,
		},
		{
			name: "tool choice without declaration",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":"hi"}],"tool_choice":{"type":"function","function":{"name":"missing"}}}`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			var request ChatCompletionsRequest
			require.NoError(t, json.Unmarshal([]byte(test.body), &request))
			_, err := ChatCompletionsToResponsesWithOptions(
				&request,
				DefaultMinMaxOutputTokens,
				ChatCompletionsToResponsesOptions{Mode: ChatCompletionsConversionFixedCodexHTTP},
			)
			require.Error(t, err)
		})
	}
}

func TestChatCompletionsToResponsesPreservesHostedTools(t *testing.T) {
	const body = `{
		"model":"gpt-5.5",
		"messages":[{"role":"user","content":"search"}],
		"tools":[
			{"type":"web_search"},
			{"type":"code_execution"},
			{"type":"x_search","allowed_x_handles":["openai"],"enable_image_understanding":true}
		]
	}`
	var request ChatCompletionsRequest
	require.NoError(t, json.Unmarshal([]byte(body), &request))
	converted, err := ChatCompletionsToResponsesWithOptions(
		&request,
		DefaultMinMaxOutputTokens,
		ChatCompletionsToResponsesOptions{Mode: ChatCompletionsConversionFixedCodexHTTP},
	)
	require.NoError(t, err)
	wire, err := json.Marshal(converted)
	require.NoError(t, err)
	require.Equal(t, "web_search", gjson.GetBytes(wire, "tools.0.type").String())
	require.Equal(t, "code_execution", gjson.GetBytes(wire, "tools.1.type").String())
	require.Equal(t, "x_search", gjson.GetBytes(wire, "tools.2.type").String())
	require.True(t, gjson.GetBytes(wire, "tools.2.enable_image_understanding").Bool())
	require.Equal(t, "openai", gjson.GetBytes(wire, "tools.2.allowed_x_handles.0").String())
}

func TestChatCompletionsToResponsesKeepsCompatibleWireDefaults(t *testing.T) {
	const body = `{
		"model":"gpt-4o",
		"messages":[
			{"role":"assistant","content":"","tool_calls":[
				{"id":"same","type":"function","function":{"name":"one","arguments":""}},
				{"id":"same","type":"function","function":{"name":"two","arguments":""}}
			]}
		],
		"tools":[{"type":"Web_Search"}]
	}`
	var request ChatCompletionsRequest
	require.NoError(t, json.Unmarshal([]byte(body), &request))
	converted, err := ChatCompletionsToResponses(&request)
	require.NoError(t, err)
	wire, err := json.Marshal(converted)
	require.NoError(t, err)
	var calls []gjson.Result
	for _, item := range gjson.GetBytes(wire, "input").Array() {
		if item.Get("type").String() == "function_call" {
			calls = append(calls, item)
		}
	}
	require.Len(t, calls, 2)
	require.Equal(t, "{}", calls[0].Get("arguments").String())
	require.Equal(t, "web_search", gjson.GetBytes(wire, "tools.0.type").String())
}

func TestChatCompletionsToResponsesPreservesLegacyFunctionPair(t *testing.T) {
	const body = `{
		"model":"gpt-5.5",
		"messages":[
			{"role":"assistant","content":null,"function_call":{"name":"lookup","arguments":"{\"id\":1}"}},
			{"role":"function","name":"lookup","content":""}
		]
	}`
	var request ChatCompletionsRequest
	require.NoError(t, json.Unmarshal([]byte(body), &request))
	converted, err := ChatCompletionsToResponsesWithOptions(
		&request,
		DefaultMinMaxOutputTokens,
		ChatCompletionsToResponsesOptions{Mode: ChatCompletionsConversionFixedCodexHTTP},
	)
	require.NoError(t, err)
	wire, err := json.Marshal(converted)
	require.NoError(t, err)
	callID := gjson.GetBytes(wire, `input.#(type=="function_call").call_id`).String()
	require.NotEmpty(t, callID)
	require.Equal(t, callID, gjson.GetBytes(wire, `input.#(type=="function_call_output").call_id`).String())
	require.Equal(t, "", gjson.GetBytes(wire, `input.#(type=="function_call_output").output`).String())
}
