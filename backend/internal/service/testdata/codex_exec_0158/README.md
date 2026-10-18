# Official codex exec 0.158.0 HTTP fixtures

`standard.json` and `lite.json` are complete, pretty-printed inference request
bodies captured from the official Linux arm64 `codex exec` binary. Standard uses
`gpt-5.5`; Lite uses `gpt-6-astra`. The prompt was `Reply only OK`.

Source: https://github.com/openai/codex/releases/tag/rust-v0.158.0

Asset: `codex-aarch64-unknown-linux-musl.tar.gz`. Its SHA256 was checked against
the release digest:
`91e3fafe5ff845a8f685a05d04919146fb30fdc9245af1a42a2f7f239959e90c`.

The binary ran in a temporary `redis:8.4-alpine` container with `--network none`,
an isolated HOME/CODEX_HOME, and fake API-key credentials. A loopback HTTP mock
returned an empty completed Responses SSE stream. No real provider credentials,
user configuration, hooks, or MCP servers were used. Request identities and
timestamps are temporary capture values. These files contain no auth headers.

Fixture SHA256:

- `standard.json`: `4c2cf04d8747aca5d4d9cef5ecd61351ef96e3a32974a622b9dfabd5b7f4c3c4`
- `lite.json`: `56090c42adc284dc4dc40f55207bca418fc88bc30f2e6eb156482bfe862af18b`

These API-key/local HTTP captures establish body layouts, not ChatGPT OAuth TLS,
Cookie, compression, or byte-for-byte network equivalence. The HTTP forwarding
test uses a separately specified Ubuntu/x86_64 identity; the capture itself ran
on Alpine/aarch64. WebSocket parity is outside this work's scope.

The HTTP forwarding test now deliberately configures a different canonical
identity (0.163.0 / xterm-256color) from the incoming native identity
(0.158.0 / unknown), and requires the incoming identity to survive. These header
test values are separate from the captured body provenance above.

The gateway scopes session identities. Native `codex exec` HTTP preserves
input-item `internal_chat_message_metadata_passthrough`; live alignment run
`20261112T010000Z-88612991` observed official OAuth requests carrying this field
and completing successfully upstream. Other compatibility paths, TUI, Desktop
and IDE clients retain the legacy cleanup based on
https://github.com/Wei-Shaw/sub2api/issues/7066. The HTTP forwarding tests assert
both policies and compare the complete body, prompts, tools, and native IDs.

Official layout source:
https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/core/src/client.rs#L912
