# Fixed 0.158.0 Responses envelope facts

These facts apply to `build_responses_request` for the captured ordinary HTTP
and Responses V2 compaction paths. They do **not** establish the legacy
`/responses/compact` endpoint's schema or compression.

All source references are from the official `openai/codex` tag
`rust-v0.158.0`. `source-references.json` records fetched source digests.
The root fixtures are actual synthetic-OAuth request bodies, not fabricated
minimal requests. Empty caller-content cases below are source-derived behavior
of the builder; an official CLI run normally supplies its own prompts and tools.

| Field | Fixed builder behavior when the caller has no corresponding setting | Evidence |
| --- | --- | --- |
| `model` | Final selected model's slug; no universal model constant. | `codex-rs/core/src/client.rs:1000` |
| `input` | Always serialized, using supplied conversation items. Lite prefixes a tool carrier and, only if nonempty, a developer message for base instructions. | `client.rs:905`, `:924`, `:932`; `codex-rs/codex-api/src/common.rs:282` |
| `instructions` | Standard uses supplied base instructions, and **omits** the top-level field if empty. Lite has no top-level instructions; it moves nonempty supplied text into a developer input message. This is not a reason to fill absent caller instructions with Codex's prompt. | `client.rs:912-949`; `common.rs:280-281` |
| `tools` | Standard sets `Some(serialized supplied tools)`, so an empty supplied list is `tools:[]`. Lite omits top-level tools and always creates a developer `additional_tools` carrier using the supplied tool list, including an empty list. No source justification to add Codex's tool set to an empty caller tool set. | `client.rs:919-947`; `common.rs:283-284` |
| `tool_choice` | Always `"auto"` in this builder; not conditional on tool count. | `client.rs:1004` |
| `parallel_tool_calls` | Always serialized. Normal exec prompt construction sets true; builder emits `prompt.parallel_tool_calls && !use_responses_lite`. Thus standard gpt-5.5 is true and Lite gpt-6-astra false. It is not derived from whether tools are empty. The generic `Prompt::default()` is false and must not be mistaken for normal exec's prompt construction. | `codex-rs/core/src/session/turn.rs:1552-1562`; `client.rs:1005`; `codex-rs/core/src/client_common.rs:44-53` |
| `reasoning` | Builder always sets `Some(reasoning)`; object members are conditional. Effort uses the explicit value or model default, then model effort resolution. `summary` appears only when supported and configured other than None. Standard omits `context`; Lite sets `"all_turns"`. An unknown model need not have an effort default; this is not a universal medium or low constant. | `client.rs:873-891`, `:962`, `:1006`; `common.rs:157-166` |
| `store` / `stream` | Always false / true. | `client.rs:1007-1008` |
| `include` | Always exactly `["reasoning.encrypted_content"]` in this builder. | `client.rs:969`, `:1010` |
| `prompt_cache_key` | Builder sets a generated/scoped session value. Preserve required association to the request session; do not reuse the fixture's literal UUID. | `client.rs:986`, `:1012` |
| `text` | Model-dependent. Verbosity is explicit config or model default if supported. With neither verbosity nor output schema, text is omitted; otherwise the selected controls are serialized. No invented JSON schema or unconditional verbosity for unknown models. | `client.rs:970-985`; `common.rs:392-409` |
| `client_metadata` | Builder supplies computed request metadata. Its contents vary with session, request kind, include-internal destination and contributors. The fixture map is not a static default to copy. | `client.rs:998`, `:1014` |
| `stream_options` | Only present when concurrent reasoning summaries are enabled, the provider is OpenAI, and reasoning summary is present. Absent in all selected captures. | `client.rs:963-968`; `common.rs:290-291` |
| `service_tier` | Resolved from model and supplied tier; absent in selected no-tier captures, not a new default tier field. | `client.rs:987-992`; `common.rs:293-294` |
| `access_programs` | Builder sets None, so omitted. | `client.rs:1015`; `common.rs:301-302` |

The normal empty-settings defaults for the **two verified models** are:

| Model | Mode | `parallel_tool_calls` | `reasoning` | `text` |
| --- | --- | --- | --- | --- |
| gpt-5.5 | standard | true | `{"effort":"medium"}` | `{"verbosity":"low"}` |
| gpt-6-astra | Lite | false | `{"effort":"low","context":"all_turns"}` | `{"verbosity":"low"}` |

Both captures omit reasoning summary despite the bundled models supporting the
summary parameter. Support alone does not request a summary. Bundled catalog:
`codex-rs/models-manager/models.json:4,6-7,23,41,174` (gpt-6-astra) and
`:1172,1174-1175,1191,1209,1294` (gpt-5.5).

The standard source-derived empty-content envelope therefore has no instructions,
has `tools:[]`, and retains `tool_choice:"auto"`, `parallel_tool_calls:true`,
the selected model's reasoning/text controls, `store:false`, `stream:true`,
the fixed include array and scoped metadata/cache key. Lite instead carries
the empty supplied tool set in `input` as `additional_tools`, omits top-level
instructions/tools, and forces parallel false. Caller instructions, history,
tool definitions, tool IDs/results and supported controls still need semantic
preservation; the captured built-in agent prompt/tool text is not a protocol
default. Exact ID derivation and namespace conversion are separate from the
default scalar values documented here.

## Compaction and request encoding

`codex-rs/core/src/compact_remote_v2_attempt.rs:69-87` constructs a Prompt from
history, appends `CompactionTrigger`, uses the existing tool router, sets
parallel true and output schema None. `codex-rs/core/src/compact_remote_v2.rs:386-419`
sends this through `ModelClientSession::stream`, hence the ordinary Responses
builder. `compact.json` is the actual request, not a legacy compact shape.

`codex-rs/core/src/client.rs:1627-1637` selects zstd only when request compression
is enabled, auth uses the Codex backend, and the provider is OpenAI.
`codex-rs/features/src/lib.rs:1272-1275` makes request compression enabled by
default. `codex-rs/codex-api/src/endpoint/responses.rs:123-141` passes the selected
compression to the HTTP transport.

Observed **Responses V2 compact** encoding:

- Synthetic ChatGPT/OAuth branch: zstd, including before/trigger/after requests.
- Fake API-key branch: identity, with Content-Encoding absent.
- Distinct `/responses/compact` endpoint: **not observed; no encoding or header
  sequence conclusion is supported by these captures**.

The frame properties are in `http/compact.encoding.json` and the original entity
bytes in `http/compact.body.bin`; see README for the independent native TLS layer.
