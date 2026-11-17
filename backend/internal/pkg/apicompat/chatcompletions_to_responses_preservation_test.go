package apicompat

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
)

func preserveChatRequest(t *testing.T, body string) (*ResponsesRequest, []map[string]json.RawMessage) {
	t.Helper()
	var req ChatCompletionsRequest
	require.NoError(t, json.Unmarshal([]byte(body), &req))
	before, err := json.Marshal(req)
	require.NoError(t, err)
	out, err := ChatCompletionsToResponsesWithOptions(&req, 0, ChatCompletionsToResponsesOptions{PreserveCodexContent: true})
	require.NoError(t, err)
	after, err := json.Marshal(req)
	require.NoError(t, err)
	require.Equal(t, string(before), string(after), "conversion must not mutate reusable ingress state")
	var items []map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(out.Input, &items))
	return out, items
}

func TestChatCompletionsToResponsesPreservation_ZeroOptionsKeepLegacyBehavior(t *testing.T) {
	var req ChatCompletionsRequest
	require.NoError(t, json.Unmarshal([]byte(`{
		"model":"gpt-4o", "max_tokens":1,
		"messages":[
			{"role":"mystery","content":"task"},
			{"role":"assistant","reasoning_content":"plan","content":[{"type":"thinking","thinking":"think"},{"type":"text","text":"answer"}],"tool_calls":[{"id":"call_1","type":"function","function":{"name":"lookup","arguments":""}}]},
			{"role":"tool","tool_call_id":"call_1","content":""}
		],
		"tools":[{"type":"unsupported"}]
	}`), &req))
	for _, floor := range []int{0, 16, DefaultMinMaxOutputTokens} {
		legacy, err := ChatCompletionsToResponsesWithMinOutputTokens(&req, floor)
		require.NoError(t, err)
		zero, err := ChatCompletionsToResponsesWithOptions(&req, floor, ChatCompletionsToResponsesOptions{})
		require.NoError(t, err)
		require.Equal(t, legacy, zero)
		if floor == DefaultMinMaxOutputTokens {
			public, err := ChatCompletionsToResponses(&req)
			require.NoError(t, err)
			require.Equal(t, public, zero)
		}
		require.JSONEq(t, `[
			{"type":"message","role":"user","content":"task"},
			{"type":"message","role":"assistant","content":[{"type":"output_text","text":"<thinking>plan</thinking>\n<thinking>think</thinking>answer"}]},
			{"type":"function_call","call_id":"call_1","name":"lookup","arguments":"{}"},
			{"type":"function_call_output","call_id":"call_1","output":"(empty)"}
		]`, string(zero.Input))
		require.Empty(t, zero.Tools)
	}
}

func TestChatCompletionsToResponsesPreservation_PreservesConversationAndTools(t *testing.T) {
	out, _ := preserveChatRequest(t, `{
		"model":"gpt-4o", "instructions":"  caller instructions\n", "parallel_tool_calls":false,
		"messages":[
			{"role":"system","content":"  system\n"},
			{"role":"developer","content":"developer"},
			{"role":"user","content":"\t task <thinking>literal</thinking>\n"},
			{"role":"assistant","content":[{"type":"text","text":"first\n"},{"type":"text","text":""},{"type":"text","text":" second"}],"tool_calls":[
				{"type":"function","id":"call_B","function":{"name":"lookup","arguments":" {\"key\": \"B\"} "}},
				{"type":"function","id":"call_A","function":{"name":"lookup","arguments":""}}
			]},
			{"role":"tool","tool_call_id":"call_A","content":""},
			{"role":"tool","tool_call_id":"call_B","content":"  result\n"},
			{"role":"assistant","content":" done\n"}
		],
		"tools":[{"type":"function","function":{"name":"lookup","description":"  original description\n","parameters":{"type":"object","properties":{"key":{"type":"string"}},"additionalProperties":false}}}],
		"tool_choice":{"type":"function","function":{"name":"lookup"}}
	}`)
	require.Equal(t, "  caller instructions\n", out.Instructions)
	require.NotNil(t, out.ParallelToolCalls)
	require.False(t, *out.ParallelToolCalls)
	require.JSONEq(t, `[
		{"type":"message","role":"system","content":"  system\n"},
		{"type":"message","role":"developer","content":"developer"},
		{"type":"message","role":"user","content":"\t task <thinking>literal</thinking>\n"},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"first\n"},{"type":"output_text","text":""},{"type":"output_text","text":" second"}]},
		{"type":"function_call","call_id":"call_B","name":"lookup","arguments":" {\"key\": \"B\"} "},
		{"type":"function_call","call_id":"call_A","name":"lookup","arguments":""},
		{"type":"function_call_output","call_id":"call_A","output":""},
		{"type":"function_call_output","call_id":"call_B","output":"  result\n"},
		{"type":"message","role":"assistant","content":" done\n"}
	]`, string(out.Input))
	require.Len(t, out.Tools, 1)
	require.Equal(t, "lookup", out.Tools[0].Name)
	require.Equal(t, "  original description\n", out.Tools[0].Description)
	require.JSONEq(t, `{"type":"object","properties":{"key":{"type":"string"}},"additionalProperties":false}`, string(out.Tools[0].Parameters))
	require.NotNil(t, out.Tools[0].Strict)
	require.False(t, *out.Tools[0].Strict)
	require.JSONEq(t, `{"type":"function","name":"lookup"}`, string(out.ToolChoice))
}

func TestChatCompletionsToResponsesPreservation_PreservesInputMedia(t *testing.T) {
	for _, role := range []string{"user", "system", "developer"} {
		t.Run(role, func(t *testing.T) {
			_, items := preserveChatRequest(t, `{"model":"gpt-4o","messages":[{"role":"`+role+`","content":[
				{"type":"text","text":"","prompt_cache_breakpoint":{"type":"ephemeral"}},
				{"type":"image_url","image_url":{"url":"https://example.com/picture.png","detail":"high"}},
				{"type":"text","text":" middle\n"},
				{"type":"file","file":{"filename":"notes.pdf","file_data":"data:application/pdf;base64,AQID"}},
				{"type":"file","file":{"file_id":"file_original"}},
				{"type":"image_url","image_url":{"url":"data:image/png;base64,AQID","detail":"low"},"prompt_cache_breakpoint":{"type":"ephemeral"}}
			]}]}`)
			require.Len(t, items, 1)
			require.JSONEq(t, `[
				{"type":"input_text","text":"","prompt_cache_breakpoint":{"type":"ephemeral"}},
				{"type":"input_image","image_url":"https://example.com/picture.png","detail":"high"},
				{"type":"input_text","text":" middle\n"},
				{"type":"input_file","filename":"notes.pdf","file_data":"data:application/pdf;base64,AQID"},
				{"type":"input_file","file_id":"file_original"},
				{"type":"input_image","image_url":"data:image/png;base64,AQID","detail":"low","prompt_cache_breakpoint":{"type":"ephemeral"}}
			]`, string(items[0]["content"]))
		})
	}
}

func TestChatCompletionsToResponsesPreservation_RejectsLossyMessages(t *testing.T) {
	tests := []struct {
		name, message, want string
	}{
		{"unknown role", `{"role":"observer","content":"task"}`, "role"},
		{"reasoning content", `{"role":"assistant","reasoning_content":"private plan","content":"answer"}`, "reasoning"},
		{"reasoning alias", `{"role":"assistant","reasoning":"private plan","content":"answer"}`, "reasoning"},
		{"thinking part", `{"role":"assistant","content":[{"type":"thinking","thinking":"private plan"}]}`, "thinking"},
		{"reasoning part", `{"role":"assistant","content":[{"type":"reasoning","text":"private plan"}]}`, "reasoning"},
		{"assistant image", `{"role":"assistant","content":[{"type":"image_url","image_url":{"url":"https://example.com/a.png"}}]}`, "image_url"},
		{"tool image", `{"role":"tool","tool_call_id":"call_1","content":[{"type":"text","text":"result"},{"type":"image_url","image_url":{"url":"https://example.com/a.png"}}]}`, "image_url"},
		{"assistant file", `{"role":"assistant","content":[{"type":"file","file":{"file_id":"file_1"}}]}`, "file"},
		{"tool file", `{"role":"tool","tool_call_id":"call_1","content":[{"type":"file","file":{"file_id":"file_1"}}]}`, "file"},
		{"user audio", `{"role":"user","content":[{"type":"input_audio","input_audio":{"data":"AQID","format":"wav"}}]}`, "input_audio"},
		{"unknown text kind", `{"role":"assistant","content":[{"type":"unknown","text":"must not discard"}]}`, "unknown"},
		{"unknown part field", `{"role":"user","content":[{"type":"text","text":"task","extra":"instruction"}]}`, "extra"},
		{"unknown image field", `{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://example.com/a.png","extra":"crop"}}]}`, "extra"},
		{"empty image", `{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,"}}]}`, "image"},
		{"empty file", `{"role":"system","content":[{"type":"file","file":{"filename":"missing.pdf"}}]}`, "file"},
		{"invalid content", `{"role":"assistant","content":{"text":"task"}}`, "content"},
		{"nonstring text", `{"role":"user","content":[{"type":"text","text":42}]}`, "text"},
		{"unknown call", `{"role":"assistant","tool_calls":[{"type":"custom","id":"call_1","function":{"name":"lookup","arguments":"{}"}}]}`, "type"},
		{"missing call id", `{"role":"assistant","tool_calls":[{"type":"function","function":{"name":"lookup","arguments":"{}"}}]}`, "id"},
		{"missing output id", `{"role":"tool","content":"result"}`, "tool_call_id"},
		{"unpaired legacy result", `{"role":"function","name":"lookup","content":"result"}`, "function"},
		{"named message", `{"role":"user","name":"agent_A","content":"task"}`, "name"},
		{"misplaced calls", `{"role":"user","tool_calls":[{"type":"function","id":"call_1","function":{"name":"lookup","arguments":"{}"}}]}`, "tool_calls"},
		{"misplaced legacy call", `{"role":"user","function_call":{"name":"lookup","arguments":"{}"}}`, "function_call"},
		{"misplaced result id", `{"role":"assistant","tool_call_id":"call_1","content":"answer"}`, "tool_call_id"},
		{"misplaced refusal", `{"role":"user","refusal":"refusal"}`, "refusal"},
		{"misplaced refusal part", `{"role":"tool","tool_call_id":"call_1","content":[{"type":"refusal","refusal":"refusal"}]}`, "refusal"},
		{"missing call type", `{"role":"assistant","tool_calls":[{"id":"call_1","function":{"name":"lookup","arguments":"{}"}}]}`, "type"},
		{"missing call name", `{"role":"assistant","tool_calls":[{"type":"function","id":"call_1","function":{"arguments":"{}"}}]}`, "name"},
		{"streaming call", `{"role":"assistant","tool_calls":[{"index":0,"type":"function","id":"call_1","function":{"name":"lookup","arguments":"{}"}}]}`, "index"},
		{"ambiguous calls", `{"role":"assistant","function_call":{"name":"lookup","arguments":"{}"},"tool_calls":[{"type":"function","id":"call_1","function":{"name":"lookup","arguments":"{}"}}]}`, "both"},
		{"conflicting part payload", `{"role":"user","content":[{"type":"text","text":"task","image_url":{"url":"https://example.com/a.png"}}]}`, "image_url"},
		{"missing part text", `{"role":"assistant","content":[{"type":"text"}]}`, "text"},
		{"null part text", `{"role":"assistant","content":[{"type":"text","text":null}]}`, "text"},
		{"null part", `{"role":"user","content":[null]}`, "content"},
		{"missing image", `{"role":"user","content":[{"type":"image_url"}]}`, "image_url"},
		{"invalid image detail", `{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://example.com/a.png","detail":42}}]}`, "detail"},
		{"unknown file field", `{"role":"user","content":[{"type":"file","file":{"file_id":"file_1","extra":"instruction"}}]}`, "extra"},
		{"invalid file payload", `{"role":"user","content":[{"type":"file","file":{"file_data":42}}]}`, "file_data"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var req ChatCompletionsRequest
			require.NoError(t, json.Unmarshal([]byte(`{"model":"gpt-4o","messages":[`+tt.message+`]}`), &req))
			out, err := ChatCompletionsToResponsesWithOptions(&req, 0, ChatCompletionsToResponsesOptions{PreserveCodexContent: true})
			require.ErrorContains(t, err, tt.want)
			require.ErrorContains(t, err, "messages[0]")
			require.Nil(t, out)
			require.NotContains(t, err.Error(), "private plan")
		})
	}
}

func TestChatCompletionsToResponsesPreservation_EmptyContent(t *testing.T) {
	for _, role := range []string{"user", "system", "developer", "assistant", "tool"} {
		for _, content := range []string{`""`, `null`, `[]`, `[{"type":"text","text":""}]`} {
			t.Run(role+"/"+content, func(t *testing.T) {
				id := ""
				if role == "tool" {
					id = `,"tool_call_id":"call_empty"`
				}
				_, items := preserveChatRequest(t, `{"model":"gpt-4o","messages":[{"role":"`+role+`","content":`+content+id+`}]}`)
				require.Len(t, items, 1)
				field := "content"
				if role == "tool" {
					field = "output"
				}
				want := content
				switch content {
				case `null`:
					want = `""`
				case `[{"type":"text","text":""}]`:
					want = `[{"type":"input_text","text":""}]`
					if role == "assistant" {
						want = `[{"type":"output_text","text":""}]`
					}
				}
				require.Contains(t, items[0], field, "empty content must be present on the wire")
				require.JSONEq(t, want, string(items[0][field]))
			})
		}
	}
	_, items := preserveChatRequest(t, `{"model":"gpt-4o","messages":[{"role":"tool","tool_call_id":"call_empty"}]}`)
	require.JSONEq(t, `""`, string(items[0]["output"]))
	out, _ := preserveChatRequest(t, `{"model":"gpt-4o","messages":[]}`)
	require.JSONEq(t, `[]`, string(out.Input))
}

func TestChatCompletionsToResponsesPreservation_ToolTextPartsAndRefusals(t *testing.T) {
	out, _ := preserveChatRequest(t, `{"model":"gpt-4o","messages":[
		{"role":"tool","tool_call_id":"call_1","content":[{"type":"text","text":"first\n"},{"type":"text","text":" second"}]},
		{"role":"assistant","content":[{"type":"text","text":"answer"},{"type":"refusal","refusal":"cannot continue"}]},
		{"role":"assistant","content":null,"refusal":"original refusal"},
		{"role":"assistant","content":"explanation\n","refusal":"remaining refusal"}
	]}`)
	require.JSONEq(t, `[
		{"type":"function_call_output","call_id":"call_1","output":[{"type":"input_text","text":"first\n"},{"type":"input_text","text":" second"}]},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"answer"},{"type":"refusal","refusal":"cannot continue"}]},
		{"type":"message","role":"assistant","content":[{"type":"refusal","refusal":"original refusal"}]},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"explanation\n"},{"type":"refusal","refusal":"remaining refusal"}]}
	]`, string(out.Input))
}

func TestChatCompletionsToResponsesPreservation_LegacyPairingAndNoToolInference(t *testing.T) {
	body := `{"model":"gpt-4o","messages":[
		{"role":"assistant","content":null,"function_call":{"name":"lookup","arguments":" {\"key\":\"one\"} "}},
		{"role":"function","name":"lookup","content":""},
		{"role":"assistant","function_call":{"name":"lookup","arguments":""}},
		{"role":"function","name":"lookup","content":" second\n"},
		{"role":"assistant","tool_calls":[{"id":"call_legacy_1","type":"function","function":{"name":"historical","arguments":"{}"}}]},
		{"role":"tool","tool_call_id":"call_legacy_1","content":null}
	]} `
	out, _ := preserveChatRequest(t, body)
	repeated, _ := preserveChatRequest(t, body)
	require.Equal(t, string(out.Input), string(repeated.Input))
	require.Empty(t, out.Tools, "historical calls do not declare tools for the next turn")
	require.JSONEq(t, `[
		{"type":"function_call","call_id":"call_legacy_2","name":"lookup","arguments":" {\"key\":\"one\"} "},
		{"type":"function_call_output","call_id":"call_legacy_2","output":""},
		{"type":"function_call","call_id":"call_legacy_3","name":"lookup","arguments":""},
		{"type":"function_call_output","call_id":"call_legacy_3","output":" second\n"},
		{"type":"function_call","call_id":"call_legacy_1","name":"historical","arguments":"{}"},
		{"type":"function_call_output","call_id":"call_legacy_1","output":""}
	]`, string(out.Input))
}

func TestChatCompletionsToResponsesPreservation_ToolPolicies(t *testing.T) {
	for _, strict := range []string{"true", "false"} {
		for _, choice := range []string{`"none"`, `"auto"`, `"required"`, `{"type":"function","name":"lookup"}`, `{"type":"function","function":{"name":"lookup"}}`} {
			t.Run(strict+"/"+choice, func(t *testing.T) {
				out, _ := preserveChatRequest(t, `{"model":"gpt-4o","tools":[{"type":"function","function":{"name":"lookup","strict":`+strict+`,"parameters":{"type":"object","properties":{"n":{"const":9007199254740993}}}}}],"tool_choice":`+choice+`}`)
				want := choice
				if choice == `{"type":"function","function":{"name":"lookup"}}` {
					want = `{"type":"function","name":"lookup"}`
				}
				require.JSONEq(t, want, string(out.ToolChoice))
				require.Equal(t, strict == "true", *out.Tools[0].Strict)
				require.JSONEq(t, `{"type":"object","properties":{"n":{"const":9007199254740993}}}`, string(out.Tools[0].Parameters))
			})
		}
	}
	for _, choice := range []string{`"auto"`, `"none"`, `{"name":"legacy"}`} {
		out, _ := preserveChatRequest(t, `{"model":"gpt-4o","functions":[{"name":"legacy","description":"legacy description","parameters":{"type":"object"}}],"function_call":`+choice+`}`)
		want := choice
		if choice == `{"name":"legacy"}` {
			want = `{"type":"function","name":"legacy"}`
		}
		require.JSONEq(t, want, string(out.ToolChoice))
		require.Len(t, out.Tools, 1)
		require.Equal(t, "legacy description", out.Tools[0].Description)
		require.False(t, *out.Tools[0].Strict)
	}
	out, _ := preserveChatRequest(t, `{"model":"gpt-4o","tools":[{"type":"function","function":{"name":"exact_name"}}],"functions":[{"name":"legacy_name"}],"tool_choice":null}`)
	require.Len(t, out.Tools, 2)
	require.Equal(t, "exact_name", out.Tools[0].Name)
	require.Equal(t, "legacy_name", out.Tools[1].Name)
	require.Empty(t, out.ToolChoice)
}

func TestChatCompletionsToResponsesPreservation_RejectsLossyToolsAndChoices(t *testing.T) {
	for _, fields := range []string{
		`"tools":[{"type":"custom"}]`,
		`"tools":[{"type":"unknown"}]`,
		`"tools":[{"type":"web_search"}]`,
		`"tools":[{"type":"x_search","allowed_x_handles":["example"]}]`,
		`"tools":[{"type":"code_execution"}]`,
		`"tools":[{"type":"function"}]`,
		`"tools":[{"type":"function","function":{"name":""}}]`,
		`"tools":[{"type":"function","function":{"name":"lookup"},"allowed_x_handles":["example"]}]`,
		`"tools":[{"type":"function","function":{"name":"lookup","parameters":null}}]`,
		`"tools":[{"type":"function","function":{"name":"lookup","parameters":[]}}]`,
		`"tools":[{"type":"function","function":{"name":"lookup"}}],"functions":[{"name":"lookup"}]`,
		`"functions":[{"name":""}]`,
		`"tool_choice":"sometimes"`,
		`"tool_choice":"required"`,
		`"tool_choice":{"type":"function","name":"undeclared"}`,
		`"tool_choice":{"type":"unknown"}`,
		`"tool_choice":{"type":"allowed_tools","mode":"required","tools":[]}`,
		`"tools":[{"type":"function","function":{"name":"lookup"}}],"tool_choice":{"type":"function","function":{"name":"lookup","extra":"restrict"}}`,
		`"tools":[{"type":"function","function":{"name":"lookup"}}],"tool_choice":{"type":"function","name":"lookup","extra":"restrict"}`,
		`"tools":[{"type":"function","function":{"name":"lookup"}}],"tool_choice":{"type":"function","name":"lookup","function":{"name":"other"}}`,
		`"tools":[{"type":"function","function":{"name":"lookup"}}],"tool_choice":{"type":"function","function":{}}`,
		`"tool_choice":42`,
		`"tool_choice":[]`,
		`"tool_choice":"auto","function_call":"none"`,
		`"function_call":{"name":"undeclared"}`,
		`"function_call":{}`,
		`"function_call":"required"`,
		`"functions":[{"name":"lookup"}],"function_call":{"name":"lookup","extra":"restrict"}`,
	} {
		t.Run(fields, func(t *testing.T) {
			var req ChatCompletionsRequest
			require.NoError(t, json.Unmarshal([]byte(`{"model":"gpt-4o",`+fields+`}`), &req))
			out, err := ChatCompletionsToResponsesWithOptions(&req, 0, ChatCompletionsToResponsesOptions{PreserveCodexContent: true})
			require.Error(t, err)
			require.Nil(t, out)
		})
	}
}

func TestChatCompletionsToResponsesPreservation_RejectsAmbiguousCallAssociations(t *testing.T) {
	for _, messages := range []string{
		`[{"role":"assistant","tool_calls":[{"id":"same","type":"function","function":{"name":"one","arguments":"{}"}},{"id":"same","type":"function","function":{"name":"two","arguments":"{}"}}]}]`,
		`[{"role":"assistant","function_call":{"name":"one","arguments":"{}"}},{"role":"function","name":"two","tool_call_id":"call_legacy_2","content":"result"}]`,
	} {
		var req ChatCompletionsRequest
		require.NoError(t, json.Unmarshal([]byte(`{"model":"gpt-4o","messages":`+messages+`}`), &req))
		out, err := ChatCompletionsToResponsesWithOptions(&req, 0, ChatCompletionsToResponsesOptions{PreserveCodexContent: true})
		require.Error(t, err)
		require.Nil(t, out)
	}
}
