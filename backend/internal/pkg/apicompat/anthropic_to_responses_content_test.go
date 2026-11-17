package apicompat

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestAnthropicToResponsesWithOptions_PreservesCodexContent(t *testing.T) {
	var req AnthropicRequest
	raw := json.RawMessage(`{
		"model":"gpt-5.5","max_tokens":16,
		"system":[
			{"type":"text","text":"x-anthropic-billing-header: retain this task text\n","cache_control":{"type":"ephemeral","ttl":"1h"}},
			{"type":"text","text":""},
			{"type":"text","text":"  Keep\tspacing.\n"}
		],
		"messages":[
			{"role":"user","content":[
				{"type":"text","text":"  Look\n","cache_control":{"type":"ephemeral"}},
				{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AQID"}},
				{"type":"text","text":""}
			]},
			{"role":"assistant","content":[
				{"type":"text","text":"Before "},{"type":"text","text":"\tafter\n"},
				{"type":"thinking","thinking":" Plan\n","signature":"gAAAA-replay"},
				{"type":"tool_use","id":"call_1","name":"lookup","input":{"q":" x\n\t"}},
				{"type":"text","text":"Pending."}
			]},
			{"role":"user","content":[
				{"type":"text","text":"Before result."},
				{"type":"tool_result","tool_use_id":"call_1","is_error":true,"content":" Exact error\n\t"},
				{"type":"text","text":"After result."}
			]}
		],
		"tools":[{"name":"lookup","description":" exact\ndescription ","input_schema":{"type":"object","properties":{"q":{"type":"string","description":" exact\tproperty "}},"required":["q"]},"cache_control":{"type":"ephemeral"}}],
		"tool_choice":{"type":"tool","name":"lookup","disable_parallel_tool_use":true}
	}`)
	require.NoError(t, json.Unmarshal(raw, &req))
	before := marshalAnthropicReplayJSON(t, req)
	out, err := AnthropicToResponsesWithOptions(&req, 16, AnthropicToResponsesOptions{PreserveCodexContent: true, OriginalRequest: raw})
	require.NoError(t, err)
	require.JSONEq(t, `[
		{"type":"message","role":"developer","content":[
			{"type":"input_text","text":"x-anthropic-billing-header: retain this task text\n"},
			{"type":"input_text","text":""},{"type":"input_text","text":"  Keep\tspacing.\n"}
		]},
		{"type":"message","role":"user","content":[
			{"type":"input_text","text":"  Look\n"},
			{"type":"input_image","image_url":"data:image/png;base64,AQID"},
			{"type":"input_text","text":""}
		]},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Before "},{"type":"output_text","text":"\tafter\n"}]},
		{"type":"reasoning","encrypted_content":"gAAAA-replay","summary":[{"type":"summary_text","text":" Plan\n"}]},
		{"type":"function_call","call_id":"call_1","name":"lookup","arguments":"{\"q\":\" x\\n\\t\"}"},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Pending."}]},
		{"type":"message","role":"user","content":[{"type":"input_text","text":"Before result."}]},
		{"type":"function_call_output","call_id":"call_1","output":[{"type":"input_text","text":"{\"is_error\":true}"},{"type":"input_text","text":" Exact error\n\t"}]},
		{"type":"message","role":"user","content":[{"type":"input_text","text":"After result."}]}
	]`, string(out.Input))
	require.Len(t, out.Tools, 1)
	require.Equal(t, "lookup", out.Tools[0].Name)
	require.Equal(t, req.Tools[0].Description, out.Tools[0].Description)
	require.JSONEq(t, string(req.Tools[0].InputSchema), string(out.Tools[0].Parameters))
	require.JSONEq(t, `{"type":"function","name":"lookup"}`, string(out.ToolChoice))
	require.NotNil(t, out.ParallelToolCalls)
	require.False(t, *out.ParallelToolCalls)
	require.NotContains(t, string(marshalAnthropicReplayJSON(t, out)), "cache_control")
	require.NotContains(t, string(marshalAnthropicReplayJSON(t, out)), "ephemeral")
	require.Equal(t, before, marshalAnthropicReplayJSON(t, req))
}

func TestAnthropicToResponsesWithOptions_SystemString(t *testing.T) {
	req := &AnthropicRequest{Model: "gpt-5.5", System: json.RawMessage(`"x-anthropic-billing-header: task\n"`)}
	out, err := AnthropicToResponsesWithOptions(req, 16, AnthropicToResponsesOptions{PreserveCodexContent: true})
	require.NoError(t, err)
	require.JSONEq(t, `[{"type":"message","role":"developer","content":[{"type":"input_text","text":"x-anthropic-billing-header: task\n"}]}]`, string(out.Input))
	legacy, err := AnthropicToResponses(req)
	require.NoError(t, err)
	require.JSONEq(t, `null`, string(legacy.Input))
}

func TestAnthropicToResponsesWithOptions_ToolResultContent(t *testing.T) {
	for _, tc := range []struct{ name, block, output string }{
		{"empty", `"content":""`, `""`},
		{"missing", `"is_error":false`, `""`},
		{"empty_blocks", `"content":[]`, `[]`},
		{"text", `"content":"  exact\n\t  "`, `"  exact\n\t  "`},
		{"text_blocks", `"content":[{"type":"text","text":"a\n"},{"type":"text","text":"\tb"}]`, `[{"type":"input_text","text":"a\n"},{"type":"input_text","text":"\tb"}]`},
		{"image_and_text", `"content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AQID"}},{"type":"text","text":"keep order"}]`, `[{"type":"input_image","image_url":"data:image/png;base64,AQID"},{"type":"input_text","text":"keep order"}]`},
		{"error_image", `"is_error":true,"content":[{"type":"image","source":{"type":"url","url":"https://example.com/error.png?q=1%202"}}]`, `[{"type":"input_text","text":"{\"is_error\":true}"},{"type":"input_image","image_url":"https://example.com/error.png?q=1%202"}]`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := &AnthropicRequest{Model: "gpt-5.5", Messages: []AnthropicMessage{{Role: "user", Content: json.RawMessage(`[{"type":"tool_result","tool_use_id":"call_1",` + tc.block + `}]`)}}}
			out, err := AnthropicToResponsesWithOptions(req, 16, AnthropicToResponsesOptions{PreserveCodexContent: true})
			require.NoError(t, err)
			require.JSONEq(t, `[{"type":"function_call_output","call_id":"call_1","output":`+tc.output+`}]`, string(out.Input))
		})
	}
}

func TestAnthropicToResponsesWithOptions_RejectsUnsupportedContent(t *testing.T) {
	for _, tc := range []struct{ name, request, path string }{
		{"role", `{"messages":[{"role":"developer","content":"private instructions"}]}`, "messages[0].role"},
		{"system_block", `{"system":[{"type":"image","source":{"type":"base64","data":"AQID"}}]}`, "system[0]"},
		{"system_field", `{"system":[{"type":"text","text":"ok","citations":[{"title":"evidence"}]}]}`, "system[0].citations"},
		{"document", `{"messages":[{"role":"user","content":[{"type":"document","source":{"type":"text","data":"private task"}}]}]}`, "messages[0].content[0]"},
		{"assistant_image", `{"messages":[{"role":"assistant","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AQID"}}]}]}`, "messages[0].content[0]"},
		{"user_thinking", `{"messages":[{"role":"user","content":[{"type":"thinking","thinking":"private task","signature":"gAAAA"}]}]}`, "messages[0].content[0]"},
		{"unsigned_thinking", `{"messages":[{"role":"assistant","content":[{"type":"thinking","thinking":"private task"}]}]}`, "signature"},
		{"blank_signature", `{"messages":[{"role":"assistant","content":[{"type":"thinking","thinking":"private task","signature":" \n\t"}]}]}`, "signature"},
		{"null_thinking", `{"messages":[{"role":"assistant","content":[{"type":"thinking","thinking":null,"signature":"gAAAA"}]}]}`, "thinking"},
		{"redacted_thinking", `{"messages":[{"role":"assistant","content":[{"type":"redacted_thinking","data":"encrypted"}]}]}`, "messages[0].content[0]"},
		{"extra_block_field", `{"messages":[{"role":"user","content":[{"type":"text","text":"ok","extra_task":"private task"}]}]}`, "extra_task"},
		{"missing_text", `{"messages":[{"role":"user","content":[{"type":"text"}]}]}`, "text"},
		{"null_text", `{"messages":[{"role":"user","content":[{"type":"text","text":null}]}]}`, "text"},
		{"null_content", `{"messages":[{"role":"user","content":null}]}`, "messages[0].content"},
		{"null_block", `{"messages":[{"role":"user","content":[null]}]}`, "messages[0].content[0]"},
		{"missing_image", `{"messages":[{"role":"user","content":[{"type":"image"}]}]}`, "source"},
		{"unsupported_image_source", `{"messages":[{"role":"user","content":[{"type":"image","source":{"type":"file","file_id":"file_1"}}]}]}`, "source"},
		{"missing_media_type", `{"messages":[{"role":"user","content":[{"type":"image","source":{"type":"base64","data":"AQID"}}]}]}`, "media_type"},
		{"missing_tool_id", `{"messages":[{"role":"assistant","content":[{"type":"tool_use","name":"lookup","input":{}}]}]}`, "id"},
		{"missing_tool_name", `{"messages":[{"role":"assistant","content":[{"type":"tool_use","id":"call_1","input":{}}]}]}`, "name"},
		{"missing_tool_input", `{"messages":[{"role":"assistant","content":[{"type":"tool_use","id":"call_1","name":"lookup"}]}]}`, "input"},
		{"nonobject_tool_input", `{"messages":[{"role":"assistant","content":[{"type":"tool_use","id":"call_1","name":"lookup","input":[]}]}]}`, "input"},
		{"missing_result_id", `{"messages":[{"role":"user","content":[{"type":"tool_result","content":"ok"}]}]}`, "tool_use_id"},
		{"malformed_result", `{"messages":[{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":{"private_task":"lost"}}]}]}`, "content"},
		{"null_result_error", `{"messages":[{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":"ok","is_error":null}]}]}`, "is_error"},
		{"result_document", `{"messages":[{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":[{"type":"document"}]}]}]}`, "content[0]"},
		{"server_tool", `{"tools":[{"type":"web_search_20250305","name":"web_search"}]}`, "tools[0].type"},
		{"unknown_tool", `{"tools":[{"type":"computer_20990101","name":"computer"}]}`, "tools[0].type"},
		{"missing_tool_definition_name", `{"tools":[{"input_schema":{"type":"object"}}]}`, "tools[0].name"},
		{"missing_schema", `{"tools":[{"name":"lookup"}]}`, "input_schema"},
		{"null_schema", `{"tools":[{"name":"lookup","input_schema":null}]}`, "input_schema"},
		{"nonobject_schema", `{"tools":[{"name":"lookup","input_schema":[]}]}`, "input_schema"},
		{"wrong_schema_type", `{"tools":[{"name":"lookup","input_schema":{"type":"string"}}]}`, "input_schema"},
		{"bad_properties", `{"tools":[{"name":"lookup","input_schema":{"type":"object","properties":[]}}]}`, "properties"},
		{"bad_required", `{"tools":[{"name":"lookup","input_schema":{"type":"object","required":"q"}}]}`, "required"},
		{"lost_tool_examples", `{"tools":[{"name":"lookup","input_schema":{"type":"object"},"input_examples":[{"q":"private task"}]}]}`, "input_examples"},
		{"duplicate_tools", `{"tools":[{"name":"lookup","input_schema":{"type":"object"}},{"name":"lookup","input_schema":{"type":"object"}}]}`, "tools[1].name"},
		{"unknown_tool_choice", `{"tool_choice":{"type":"future_mode"}}`, "tool_choice.type"},
		{"null_tool_choice", `{"tool_choice":null}`, "tool_choice"},
		{"null_parallel_choice", `{"tool_choice":{"type":"auto","disable_parallel_tool_use":null}}`, "disable_parallel_tool_use"},
		{"unnamed_tool_choice", `{"tool_choice":{"type":"tool"}}`, "tool_choice.name"},
		{"undeclared_tool_choice", `{"tool_choice":{"type":"tool","name":"lookup"}}`, "tool_choice.name"},
		{"tool_choice_field", `{"tool_choice":{"type":"auto","private_task":"lost"}}`, "tool_choice.private_task"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var req AnthropicRequest
			require.NoError(t, json.Unmarshal([]byte(tc.request), &req))
			req.Model = "gpt-5.5"
			before := marshalAnthropicReplayJSON(t, req)
			out, err := AnthropicToResponsesWithOptions(&req, 16, AnthropicToResponsesOptions{PreserveCodexContent: true, OriginalRequest: json.RawMessage(tc.request)})
			require.Nil(t, out)
			require.ErrorContains(t, err, "codex content incompatible")
			require.ErrorContains(t, err, tc.path)
			require.NotContains(t, err.Error(), "private task")
			require.Equal(t, before, marshalAnthropicReplayJSON(t, req))
			_, err = AnthropicToResponses(&req)
			require.NoError(t, err, "legacy callers must retain their existing permissive behavior")
		})
	}
}

func TestAnthropicToResponsesWithOptions_LegacyContentUnchanged(t *testing.T) {
	req := &AnthropicRequest{
		Model:  "gpt-5.5",
		System: json.RawMessage(`[{"type":"text","text":"x-anthropic-billing-header: task"},{"type":"text","text":"system"}]`),
		Messages: []AnthropicMessage{
			{Role: "unknown", Content: json.RawMessage(`"role fallback"`)},
			{Role: "assistant", Content: json.RawMessage(`[{"type":"text","text":"a"},{"type":"thinking","thinking":"unsigned"},{"type":"text","text":"b"},{"type":"document"}]`)},
			{Role: "user", Content: json.RawMessage(`[{"type":"text","text":"next"},{"type":"tool_result","tool_use_id":"call_1","is_error":true,"content":""}]`)},
		},
		Tools:      []AnthropicTool{{Type: "web_search_20250305", Name: "search"}, {Name: "lookup"}},
		ToolChoice: json.RawMessage(`{"type":"auto","disable_parallel_tool_use":true}`),
	}
	for _, tc := range []struct {
		name    string
		convert func(*AnthropicRequest) (*ResponsesRequest, error)
	}{
		{"default", AnthropicToResponses},
		{"configured_floor", func(req *AnthropicRequest) (*ResponsesRequest, error) {
			return AnthropicToResponsesWithMinOutputTokens(req, 16)
		}},
		{"disabled_option", func(req *AnthropicRequest) (*ResponsesRequest, error) {
			// OriginalRequest is also irrelevant to excluded channels.
			return AnthropicToResponsesWithOptions(req, 16, AnthropicToResponsesOptions{OriginalRequest: json.RawMessage(`invalid`)})
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out, err := tc.convert(req)
			require.NoError(t, err)
			require.JSONEq(t, `[
			{"type":"message","role":"developer","content":[{"type":"input_text","text":"system"}]},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"role fallback"}]},
			{"type":"message","role":"assistant","content":[{"type":"output_text","text":"a\n\nb"}]},
			{"type":"function_call_output","call_id":"call_1","output":"(empty)"},
			{"type":"message","role":"user","content":[{"type":"input_text","text":"next"}]}
			]`, string(out.Input))
			require.Equal(t, "web_search", out.Tools[0].Type)
			require.JSONEq(t, `{"type":"object","properties":{}}`, string(out.Tools[1].Parameters))
			require.True(t, *out.ParallelToolCalls)
		})
	}
}

func TestAnthropicToResponsesWithOptions_ToolChoice(t *testing.T) {
	for _, tc := range []struct {
		choice, want string
		parallel     bool
	}{
		{`{"type":"auto"}`, `"auto"`, true},
		{`{"type":"any","disable_parallel_tool_use":true}`, `"required"`, false},
		{`{"type":"none"}`, `"none"`, true},
		{`{"type":"tool","name":"lookup","disable_parallel_tool_use":false}`, `{"type":"function","name":"lookup"}`, true},
	} {
		t.Run(tc.choice, func(t *testing.T) {
			req := &AnthropicRequest{
				Model:      "gpt-5.5",
				Tools:      []AnthropicTool{{Type: "custom", Name: "lookup", InputSchema: json.RawMessage(`{"type":"object"}`)}},
				ToolChoice: json.RawMessage(tc.choice),
			}
			out, err := AnthropicToResponsesWithOptions(req, 16, AnthropicToResponsesOptions{PreserveCodexContent: true})
			require.NoError(t, err)
			require.JSONEq(t, tc.want, string(out.ToolChoice))
			require.Equal(t, tc.parallel, *out.ParallelToolCalls)
			require.Len(t, out.Tools, 1)
			require.Equal(t, "function", out.Tools[0].Type)
			require.JSONEq(t, `{"type":"object","properties":{}}`, string(out.Tools[0].Parameters))
		})
	}
}

func TestAnthropicToResponsesWithOptions_MalformedToolSchema(t *testing.T) {
	req := &AnthropicRequest{Model: "gpt-5.5", Tools: []AnthropicTool{{Name: "lookup", InputSchema: json.RawMessage(`{"type":`)}}}
	out, err := AnthropicToResponsesWithOptions(req, 16, AnthropicToResponsesOptions{PreserveCodexContent: true})
	require.Nil(t, out)
	require.ErrorContains(t, err, "codex content incompatible: tools[0].input_schema")
	_, err = AnthropicToResponses(req)
	require.NoError(t, err, "the legacy converter still passes malformed schemas through")
}
