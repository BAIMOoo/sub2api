# Official codex exec 0.158.0 Linux x86_64 local reference

Actual captures made on 2026-11-17 using the unmodified official
`codex-x86_64-unknown-linux-musl.tar.gz` binary and its verified code-mode
companion. This directory is new evidence; the older arm64 fixtures are unchanged.

The fixed runtime is Alpine Linux **3.24.4**, kernel `6.12.76-linuxkit`,
`linux/amd64`, running through Docker Desktop amd64 emulation on an arm64 host.
The image is pinned by digest in `provenance.json`. The Docker daemon was usable.
This capture does not establish an Ubuntu identity.

Exact observed identity, in every selected inference request:

```text
version: 0.158.0
originator: codex_exec
user-agent: codex_exec/0.158.0 (Alpine Linux 3.24.4; x86_64) unknown (codex_exec; 0.158.0)
```

## What to consume

- `standard.json`, `lite.json`: complete decoded request bodies from the
  **synthetic OAuth** client branch. Models are `gpt-5.5` and `gpt-6-astra`.
- `compact.json`: actual automatic Responses V2 compaction request, with
  `input[].type = "compaction_trigger"`. `compact-before.json` and
  `compact-after.json` retain its surrounding requests and identity relationships.
- `http/*.request.http`: exact HTTP/1.1 application bytes, including raw headers
  and the original compressed body. These are decrypted application bytes from
  the local custom-CA mock, not TLS packets.
- `http/*.headers.raw`: original request line and CRLF header block.
  `*.headers.json` preserves the ordered name/value pairs and their hashes.
- `http/*.body.bin`: original request entity bytes; `*.decoded.json` is the
  exact decompressed JSON byte stream. The root JSON files only add indentation.
- `http/*.encoding.json`: measured encoding, frame properties, hashes and shape.
- `api-key/`: corresponding fake API-key local plaintext HTTP captures. These
  carry **no Content-Encoding header** and their body is already JSON.
- `native-tls/profile.json`: structural native-default HTTPS ClientHello profile,
  independently captured without custom CA, proxy, or TLS interception.
  `native-tls/{standard,lite}/clienthello-*.bin` preserves original TLS records.
- `comparison.json`: mechanical comparisons with the repository's existing
  `profiles.go` and `httporder.go`, including hashes of the compared source files.
- `ENVELOPE_DEFAULTS.md`, `envelope-defaults.json`: fixed-version builder facts,
  separating protocol constants, caller content and model-dependent defaults.
- `runs/`: exact invocation/environment, native CLI output, request indexes,
  mock Responses replies and bootstrap responses. Synthetic OAuth auth files are
  deliberately fake and are included only to explain the local code path.
- `release.json`, binary/companion/decoder provenance and `SHA256SUMS` establish
  release, extraction, decoder and artifact identities.

## Verified binaries

Release: `https://github.com/openai/codex/releases/tag/rust-v0.158.0`
(`release_id = 397962047`). Both archive digests were checked against the
official GitHub release asset digest before execution. Version output was
`codex-cli 0.158.0`.

| Component | Archive SHA256 | Extracted binary SHA256 |
| --- | --- | --- |
| codex x86_64 Linux musl | `af9f5aa6e6662accf9d707cef0d9ca083880a173a9c2b6c22947edb7830e5778` | `167c0148a849d2444f1b5a7fb5f8bb2de1de5ae13a2a504b833fc765980f5cd9` |
| codex-code-mode-host x86_64 Linux musl | `5455c64be4ba371444710895aff6d74d653985a3bfc454a8b7fe42d6e6d11e3d` | `a58bd9853daa3b4d51d54e66083d0b3e3e531bd70287fe2463b995d7356d1d44` |

The executable archives themselves are not vendored here. The temporary capture
harness path and digest are retained in provenance; no repository code was used
as the client under test.

## Isolation and provenance boundaries

Each invocation used a new HOME, CODEX_HOME and working directory, explicit
environment variables, `--ignore-user-config --ignore-rules --ephemeral`, and
fake auth. No user's real home, auth, repository, SSH configuration, provider,
monitoring endpoint or deployment was mounted or accessed by the container.
Docker used `--network none`, a read-only root filesystem and only the temporary
capture directory as a bind mount. `/etc/hosts` mapped `chatgpt.com` to
`127.0.0.1`; listeners bound only to loopback, with no published host ports.

All selected runs used prompt `Reply only OK`. Apps/plugins and analytics were
disabled; models came from the binary's bundled catalog. The synthetic OAuth
bootstrap mock returned an empty models list, one invented account with backend
origin `https://chatgpt.com` and `NO_CONSTRAINT` routing, and empty settings.
The unsigned fake JWT, fake access/refresh strings and account UUID in `runs/`
have never represented real credentials. Original temporary UUIDs and times
are retained verbatim; no body/header sanitization or recompression was applied.

The client first attempted WebSockets. The HTTP mocks returned 503 to those
upgrades, causing the official client to select its HTTP fallback. The CLI's
fallback diagnostic says "HTTPS transport" even for the plaintext API-key mock.
All six selected HTTP runs finished with exit code 0 and the mock answer `OK`.

OAuth workspace routing in this version requires an HTTPS origin. Therefore the
synthetic OAuth captures use a temporary CA plus a separate leaf certificate,
with `CODEX_CA_CERTIFICATE` and `SSL_CERT_FILE` set explicitly. The public CA and
leaf are retained in `mock/`; the private key is not included. **Their TLS is
not the native TLS evidence.** This layer proves HTTP construction/compression
for the synthetic branch, not a live authenticated account or default TLS.

## HTTP order and encoding

The synthetic OAuth standard and compaction requests have exactly this order:

```text
version
x-codex-beta-features
x-codex-window-id
x-codex-turn-metadata
x-codex-routing-hint
x-client-request-id
session-id
thread-id
accept
content-encoding
content-type
authorization
chatgpt-account-id
originator
user-agent
host
content-length
```

Lite inserts `x-openai-internal-codex-responses-lite` immediately after
`x-codex-turn-metadata`. All inference header names are lowercase. Neither layer
sent `Accept-Encoding`; no Cookie or returned `x-codex-turn-state` was exercised.
The observed subsequences match `OpenAIResponsesHeaderOrder()` exactly.

All five synthetic OAuth inference bodies, including the actual compaction
request and its continuation, have `Content-Encoding: zstd`. Measured properties:

| Property | Observed value |
| --- | --- |
| Frame magic / first six bytes | `28b52ffd` / `28b52ffd0058` |
| Frame descriptor | `00` |
| Window | 2,097,152 bytes (2 MiB) |
| Single segment | false |
| Declared decompressed content size | absent |
| Dictionary ID / checksum | 0 / absent |
| Frame count | 1 |
| Blocks in these samples | one final compressed block |
| Trailing bytes | 0 |

All frames were decoded to EOF and checked against the saved exact decoded JSON
and Content-Length. Block size and compressed bytes depend on content; these
samples do not make either a universal constant. Byte-identical compression is
not required. Fake API-key standard/Lite/compaction requests were uncompressed.

## Native default TLS

Two fresh executions, one per model, connected directly to a loopback TCP
listener with the hostname `chatgpt.com`. The listener recorded a complete
ClientHello and closed the connection, without replying with ServerHello or a
certificate. These invocations had no custom CA or proxy environment variables.

Each execution produced seven WebSocket TLS attempts followed by two HTTPS
HTTP fallback attempts. The stderr/stdout logs retain the fallback transition.
The first seven offers use a different, permuted extension list and **must not
be used as the HTTP profile**. Both HTTP attempts in both executions match:

```text
JA3: 0b85eb0d4981e69064e40753e4f0ac5f
record version: 0x0301; ClientHello legacy version: 0x0303
session ID length: 32
cipher suites: 30
extension order: 65281,0,11,10,35,22,23,13,43,45,51
supported groups: 4588,29,23,30,24,25,256,257
signature algorithms: 26
supported versions: 772,771
key shares: 4588 (1216 bytes), 29 (32 bytes)
ALPN: absent; GREASE: absent
```

All compared structural arrays equal the existing `OpenAICodexLinuxProfile()`.
This is evidence of the original binary's offer, not a completed TLS negotiation
or a single end-to-end packet capture joining TLS and the HTTP bodies above.

## Compaction distinction and remaining limits

Compaction was triggered with `model_auto_compact_token_limit=1` and a mock
response containing a harmless `get_goal` tool call plus synthetic high token
usage. The official client sent `compaction_trigger` through the regular
`/backend-api/codex/responses` endpoint. The mock returned a synthetic compaction
item, which the client carried into `compact-after.json` with the next window ID.

**No `/backend-api/codex/responses/compact` request was observed.** The zstd and
header order evidence above belongs to Responses V2 compaction. It cannot justify
the encoding or order of the distinct legacy compact endpoint. Fixed source
references for this behavior are in `ENVELOPE_DEFAULTS.md`.

These artifacts do not prove real OAuth, PAT or SetupToken authentication,
upstream acceptance, cookie behavior, resumed turn-state, every model capability,
other Linux distributions/terminals, proxy compatibility, or gateway output.
Official prompts and tools inside complete body fixtures are capture data, not
instructions to inject those prompts/tools into callers' traffic.

Initial unsuccessful local attempts (ignored environment endpoint override,
missing synthetic routing information, and an unsuitable self-signed leaf) were
kept only in the temporary capture root; none is represented as a successful
fixture. The selected Lite runs include the verified code-mode companion and
have no missing-host diagnostic.

## Bounded legacy-path follow-up

`legacy-compact-check/README.md` and `result.json` add pinned source proof and one
explicit `features.remote_compaction_v2=false` run. The key is removed and ignored;
OpenAI automatic and manual compaction dispatches to V2. The flag-off run again
emitted zstd compaction through `/responses`, not `/responses/compact`. Thus the
legacy endpoint is not a reachable supported compaction path of this fixed
client, rather than merely a missing capture caused by Docker. No manual endpoint
emission is represented as official evidence; earlier captures remain unchanged.
