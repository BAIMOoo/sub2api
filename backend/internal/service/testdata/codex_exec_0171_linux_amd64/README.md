# Official codex exec 0.171.0 Linux x86_64 Layered Reference

Captured on 2026-11-24 in an isolated `linux/amd64` Alpine Linux 3.24.4 container (`--network none`, `--read-only`, `--add-host chatgpt.com:127.0.0.1`, no repository mount, no real credentials or network access) using official SHA256-verified `rust-v0.171.0` binaries (`codex-cli 0.171.0`).

## Verified Binaries (`rust-v0.171.0`, release ID `452122665`)

| Component | Archive SHA256 | Extracted Binary SHA256 |
| --- | --- | --- |
| `codex-x86_64-unknown-linux-musl.tar.gz` | `01baa2e47809f8597be5b2f8abd4e4dfe3fb5bcb6e504c2fbaaad97056c15e8d` | `b76a027d6b210f9a8da7aaf321dd235c700f0ef7a794d4a5314b9af6c134f0f0` |
| `codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz` | `d78619030c1b8d0a4d9c9c67667c1bfa23ad9d9edc63d87ffeaa86796f3d868c` | `f49bd4f0c0cc73cf1ec3a719009e07ce5b6e100520d023c760cc203fb37e5c6f` |

## Layer 1: Synthetic OAuth Local Custom-CA HTTP Reference

- Exact observed identity:
  - `version: 0.171.0`
  - `originator: codex_exec`
  - `user-agent: codex_exec/0.171.0 (Alpine Linux 3.24.4; x86_64) unknown (codex_exec; 0.171.0)`
  - `accept-encoding: zstd,gzip` (positioned immediately after `user-agent` and immediately before `cookie` / `host`, confirmed across Standard, Lite, Compact V2, and a separate Set-Cookie order probe)
- Exact zstd request body frame header prefix: `28b52ffd0058` (`content-encoding: zstd`, 2 MiB window, no declared decompressed content size, no dictionary or checksum).
- Observed body layout changes relative to `0.158.0`:
  - **Standard (`standard.json`, `gpt-5.5`)**: top-level `instructions` is removed; base instructions are emitted as `input[0]` (`role: developer`, `content_item_kinds: ["model.base_instructions"]`), while top-level `tools` is retained.
  - **Lite (`lite.json`, `gpt-6.2-sol`)**: neither top-level `instructions` nor top-level `tools`; declared tools are placed in leading `input[0]` (`type: additional_tools`), followed by `developer` base instructions.
  - **Compact V2 (`compact.json`, `gpt-5.5`)**: sent to `/backend-api/codex/responses` with `x-codex-beta-features: remote_compaction_v2`, trailing `{"type": "compaction_trigger"}`, and no top-level `instructions`.
- **Boundary**: This layer uses a temporary inspection CA (`CODEX_CA_CERTIFICATE` / `SSL_CERT_FILE`) and offline synthetic auth; it is strictly HTTP/header/body/zstd evidence and is **not** native TLS or live provider evidence.

## Layer 2: Independent Native TLS ClientHello Reference

- Captured in separate runs with **no custom CA and no proxy** against a loopback TCP listener on `127.0.0.1:443` recording raw ClientHello records and closing before ServerHello (`gpt-5.5` and `gpt-6.2-sol`).
- Each run emitted 7 permuted WebSocket TLS attempts followed by 30 identical HTTP-fallback ClientHello attempts (60 total HTTP-fallback samples across both models).
- Structural fixture: `backend/internal/pkg/tlsfingerprint/testdata/codex_exec_0171_linux_amd64_clienthello.json`.
- JA3 MD5: `0b85eb0d4981e69064e40753e4f0ac5f` — structurally identical to the `0.158.0` native Linux ClientHello reference and `OpenAICodexLinuxProfile()`.
- **Boundary**: Native ClientHello capture is strictly separate from Layer 1 HTTP capture; the two layers do not constitute a single end-to-end native wire transaction.
