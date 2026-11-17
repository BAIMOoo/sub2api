package apicompat

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func marshalAnthropicReplayJSON(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.Marshal(value)
	require.NoError(t, err)
	return raw
}

func TestAnthropicToResponsesWithOptions_ReasoningReplay(t *testing.T) {
	response := &ResponsesResponse{
		ID: "resp_replay", Model: "gpt-5.5", Status: "completed",
		Output: []ResponsesOutput{
			{
				Type: "reasoning", ID: "rs_summary", EncryptedContent: "gAAAA-codex-summary",
				Summary: []ResponsesSummary{
					{Type: "summary_text", Text: "Inspect first.\n"},
					{Type: "summary_text", Text: "Then call the tool."},
				},
			},
			{Type: "reasoning", ID: "rs_signature", EncryptedContent: "gAAAA-codex-signature-only"},
			{Type: "message", Role: "assistant", Content: []ResponsesContentPart{{Type: "output_text", Text: "Checking."}}},
			{Type: "function_call", CallID: "call_lookup", Name: "lookup", Arguments: `{"key":"answer"}`},
		},
	}

	t.Run("non_stream", func(t *testing.T) {
		// Serialize the response as a client would receive it before replaying
		// its content as the next request's assistant message.
		wire := marshalAnthropicReplayJSON(t, ResponsesToAnthropic(response, "client-model"))
		var message AnthropicResponse
		require.NoError(t, json.Unmarshal(wire, &message))
		assertCodexReasoningReplay(t, message.Content)
	})

	t.Run("stream", func(t *testing.T) {
		events := []ResponsesStreamEvent{
			{Type: "response.created", Response: &ResponsesResponse{ID: response.ID, Model: response.Model}},
			{Type: "response.output_item.added", OutputIndex: 0, Item: &ResponsesOutput{Type: "reasoning", ID: "rs_summary"}},
			{Type: "response.reasoning_summary_text.delta", OutputIndex: 0, Delta: "Inspect first.\n"},
			{Type: "response.reasoning_summary_text.delta", OutputIndex: 0, SummaryIndex: 1, Delta: "Then call the tool."},
			// The signature arrives after summary.done, not in the initial item.
			{Type: "response.reasoning_summary_text.done", OutputIndex: 0, SummaryIndex: 1},
			{Type: "response.output_item.done", OutputIndex: 0, Item: &response.Output[0]},
			{Type: "response.output_item.added", OutputIndex: 1, Item: &ResponsesOutput{Type: "reasoning", ID: "rs_signature"}},
			{Type: "response.output_item.done", OutputIndex: 1, Item: &response.Output[1]},
			{Type: "response.output_item.added", OutputIndex: 2, Item: &ResponsesOutput{Type: "message", Role: "assistant"}},
			{Type: "response.output_text.delta", OutputIndex: 2, Delta: "Checking."},
			{Type: "response.output_item.done", OutputIndex: 2, Item: &response.Output[2]},
			{Type: "response.output_item.added", OutputIndex: 3, Item: &ResponsesOutput{Type: "function_call", CallID: "call_lookup", Name: "lookup"}},
			{Type: "response.function_call_arguments.delta", OutputIndex: 3, Delta: `{"key":`},
			{Type: "response.function_call_arguments.delta", OutputIndex: 3, Delta: `"answer"}`},
			{Type: "response.output_item.done", OutputIndex: 3, Item: &response.Output[3]},
			{Type: "response.completed", Response: response},
		}
		assertCodexReasoningReplay(t, collectReasoningReplayMessage(t, events))
	})
}

func assertCodexReasoningReplay(t *testing.T, blocks []AnthropicContentBlock) {
	t.Helper()
	require.Len(t, blocks, 4)
	require.Equal(t, "gAAAA-codex-summary", blocks[0].Signature)
	require.Equal(t, "Inspect first.\nThen call the tool.", blocks[0].Thinking)
	require.Equal(t, "gAAAA-codex-signature-only", blocks[1].Signature)
	require.Empty(t, blocks[1].Thinking)

	req := &AnthropicRequest{
		Model: "gpt-5.5", MaxTokens: 1,
		System: json.RawMessage(`"Keep the caller's instructions."`),
		Messages: []AnthropicMessage{
			{Role: "user", Content: json.RawMessage(`"Look up the answer."`)},
			{Role: "assistant", Content: marshalAnthropicReplayJSON(t, blocks)},
			{Role: "user", Content: json.RawMessage(`[{"type":"tool_result","tool_use_id":"call_lookup","content":"42"}]`)},
		},
	}
	before := marshalAnthropicReplayJSON(t, req)
	out, err := AnthropicToResponsesWithOptions(req, 16, AnthropicToResponsesOptions{PreserveCodexContent: true})
	require.NoError(t, err)
	require.JSONEq(t, `[
		{"type":"message","role":"developer","content":[{"type":"input_text","text":"Keep the caller's instructions."}]},
		{"type":"message","role":"user","content":[{"type":"input_text","text":"Look up the answer."}]},
		{"type":"reasoning","encrypted_content":"gAAAA-codex-summary","summary":[{"type":"summary_text","text":"Inspect first.\nThen call the tool."}]},
		{"type":"reasoning","encrypted_content":"gAAAA-codex-signature-only","summary":[]},
		{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Checking."}]},
		{"type":"function_call","call_id":"call_lookup","name":"lookup","arguments":"{\"key\":\"answer\"}"},
		{"type":"function_call_output","call_id":"call_lookup","output":"42"}
	]`, string(out.Input))
	require.NotNil(t, out.MaxOutputTokens)
	require.Equal(t, 16, *out.MaxOutputTokens)
	require.Equal(t, before, marshalAnthropicReplayJSON(t, req), "conversion must leave the request reusable for another channel")

	legacy, err := AnthropicToResponses(req)
	require.NoError(t, err)
	require.NotContains(t, string(legacy.Input), "gAAAA", "opt-in must not affect a later default conversion")
}

// collectReasoningReplayMessage reconstructs content from the serialized SSE
// events a Messages client sees, including signature deltas before block stop.
func collectReasoningReplayMessage(t *testing.T, events []ResponsesStreamEvent) []AnthropicContentBlock {
	t.Helper()
	state := NewResponsesEventToAnthropicState()
	var blocks []AnthropicContentBlock
	open := make(map[int]bool)
	toolArgs := make(map[int]string)
	messageStopped := false
	for _, event := range events {
		for _, converted := range ResponsesEventToAnthropicEvents(&event, state) {
			wire, err := ResponsesAnthropicEventToSSE(converted)
			require.NoError(t, err)
			_, data, ok := strings.Cut(wire, "\ndata: ")
			require.True(t, ok)
			var clientEvent AnthropicStreamEvent
			require.NoError(t, json.Unmarshal([]byte(strings.TrimSpace(data)), &clientEvent))
			switch clientEvent.Type {
			case "content_block_start":
				require.NotNil(t, clientEvent.Index)
				require.NotNil(t, clientEvent.ContentBlock)
				require.Equal(t, len(blocks), *clientEvent.Index)
				blocks = append(blocks, *clientEvent.ContentBlock)
				open[*clientEvent.Index] = true
			case "content_block_delta":
				require.NotNil(t, clientEvent.Index)
				require.NotNil(t, clientEvent.Delta)
				idx := *clientEvent.Index
				require.True(t, open[idx], "deltas must arrive before the block closes")
				switch clientEvent.Delta.Type {
				case "thinking_delta":
					blocks[idx].Thinking += clientEvent.Delta.Thinking
				case "signature_delta":
					blocks[idx].Signature += clientEvent.Delta.Signature
				case "text_delta":
					blocks[idx].Text += clientEvent.Delta.Text
				case "input_json_delta":
					toolArgs[idx] += clientEvent.Delta.PartialJSON
				}
			case "content_block_stop":
				require.NotNil(t, clientEvent.Index)
				idx := *clientEvent.Index
				require.True(t, open[idx])
				delete(open, idx)
				if args, ok := toolArgs[idx]; ok {
					require.True(t, json.Valid([]byte(args)))
					blocks[idx].Input = json.RawMessage(args)
				}
			case "message_stop":
				messageStopped = true
			}
		}
	}
	require.True(t, messageStopped)
	require.Empty(t, open)
	return blocks
}

func TestAnthropicToResponsesWithOptions_DefaultReasoningBehavior(t *testing.T) {
	req := &AnthropicRequest{
		Model: "gpt-5.5", MaxTokens: 1,
		Messages: []AnthropicMessage{{Role: "assistant", Content: json.RawMessage(`[
			{"type":"thinking","thinking":"Codex summary","signature":"gAAAA-codex"},
			{"type":"thinking","thinking":"Other summary","signature":"  enc-provider  "},
			{"type":"thinking","thinking":"Unsigned summary","signature":""},
			{"type":"thinking","thinking":"Placeholder summary","signature":" \t\n"},
			{"type":"text","text":"Answer"}
		]`)}},
	}
	for _, tc := range []struct {
		name    string
		convert func(*AnthropicRequest) (*ResponsesRequest, error)
		floor   int
	}{
		{"default", AnthropicToResponses, DefaultMinMaxOutputTokens},
		{"configured_floor", func(req *AnthropicRequest) (*ResponsesRequest, error) {
			return AnthropicToResponsesWithMinOutputTokens(req, 16)
		}, 16},
		{"zero_options", func(req *AnthropicRequest) (*ResponsesRequest, error) {
			return AnthropicToResponsesWithOptions(req, 16, AnthropicToResponsesOptions{})
		}, 16},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out, err := tc.convert(req)
			require.NoError(t, err)
			require.JSONEq(t, `[
				{"type":"reasoning","encrypted_content":"enc-provider"},
				{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Answer"}]}
			]`, string(out.Input))
			require.NotNil(t, out.MaxOutputTokens)
			require.Equal(t, tc.floor, *out.MaxOutputTokens)
		})
	}

	t.Run("opt_in_preserves_all_nonblank_signatures", func(t *testing.T) {
		valid := *req
		valid.Messages = []AnthropicMessage{{Role: "assistant", Content: json.RawMessage(`[
			{"type":"thinking","thinking":"Codex summary","signature":"gAAAA-codex"},
			{"type":"thinking","thinking":"Other summary","signature":"  enc-provider  "},
			{"type":"text","text":"Answer"}
		]`)}}
		out, err := AnthropicToResponsesWithOptions(&valid, 16, AnthropicToResponsesOptions{PreserveCodexContent: true})
		require.NoError(t, err)
		require.JSONEq(t, `[
			{"type":"reasoning","encrypted_content":"gAAAA-codex","summary":[{"type":"summary_text","text":"Codex summary"}]},
			{"type":"reasoning","encrypted_content":"  enc-provider  ","summary":[{"type":"summary_text","text":"Other summary"}]},
			{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Answer"}]}
		]`, string(out.Input))
	})
}
