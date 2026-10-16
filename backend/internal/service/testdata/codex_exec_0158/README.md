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
test pins the separately configured canonical Ubuntu/x86_64 identity; the capture
itself ran on Alpine/aarch64. WebSocket parity is outside this work's scope.

The gateway intentionally scopes session identities and removes input-item
`internal_chat_message_metadata_passthrough` for ChatGPT compatibility, based on
https://github.com/Wei-Shaw/sub2api/issues/7066. Official 0.158.0 source retains
this field when the provider name is `OpenAI`; the API-key capture does not prove
its acceptance by current ChatGPT. The test explicitly asserts this exception
and compares every other body field, including prompts, tools, and native IDs.

Official layout source:
https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/core/src/client.rs#L912
