package service

import (
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestSearchDeclarationOrderByTransport(t *testing.T) {
	const history = `{"input":[{"type":"web_search_call","id":"ws_example"},{"type":"message","role":"user","content":"continue"},{"type":"compaction_trigger"}]}`
	for _, test := range []struct {
		name   string
		layout webSearchHistoryLayout
		want   []string
	}{
		{"HTTP prefix", webSearchHistoryHTTP, []string{"additional_tools", "web_search_call", "message", "compaction_trigger"}},
		{"WS first frame or reconnect", webSearchHistoryWS, []string{"web_search_call", "message", "additional_tools", "compaction_trigger"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			body, changed, err := declareWebSearchForHistory([]byte(history), true, test.layout)
			require.NoError(t, err)
			require.True(t, changed)
			items := gjson.GetBytes(body, "input").Array()
			var got []string
			for _, item := range items {
				got = append(got, item.Get("type").String())
			}
			require.Equal(t, test.want, got)
			again, changed, err := declareWebSearchForHistory(body, true, test.layout)
			require.NoError(t, err)
			require.False(t, changed)
			require.Equal(t, body, again)
		})
	}
	const explicit = `{"input":[{"type":"web_search_call"},{"type":"additional_tools","role":"developer","tools":[{"type":"web_search"}]},{"type":"message","role":"user","content":"continue"}]}`
	body, changed, err := declareWebSearchForHistory([]byte(explicit), true, webSearchHistoryWS)
	require.NoError(t, err)
	require.False(t, changed)
	require.Equal(t, explicit, string(body))
}

func TestSearchHistoryExistingInputToolsInStandardMode(t *testing.T) {
	const declared = `{"input":[{"type":"web_search_call"},{"type":"additional_tools","tools":[{"type":"web_search"}]}]}`
	body, changed, err := declareWebSearchForHistory([]byte(declared), false, webSearchHistoryWS)
	require.NoError(t, err)
	require.False(t, changed, "upstream WS respects an existing input declaration even outside Lite")
	require.Equal(t, declared, string(body))
	body, changed, err = ensureOpenAIOAuthWebSearchToolForHistoryBody([]byte(declared), false)
	require.NoError(t, err)
	require.True(t, changed, "the fork's standard HTTP path declares the tool at the top level")
	require.Equal(t, "web_search", gjson.GetBytes(body, "tools.0.type").String())
}

func TestSearchHistoryWSAppendsAfterOrdinaryItems(t *testing.T) {
	const message = `{"type":"message","role":"user","content":"<user data>&","opaque_id":9007199254740993}`
	const history = `{"input":[{"type":"web_search_call"},` + message + `]}`
	body, changed, err := declareWebSearchForHistory([]byte(history), true, webSearchHistoryWS)
	require.NoError(t, err)
	require.True(t, changed)
	require.Equal(t, message, gjson.GetBytes(body, "input.1").Raw)
	require.Equal(t, "additional_tools", gjson.GetBytes(body, "input.2.type").String())
	require.Equal(t, "none", gjson.GetBytes(body, "tool_choice").String())
}
