# Real model stream and exact billing canary

This runner sends one small billable model request for an explicitly selected route. The default invocation is offline. Source baseline and target acceptance each have their own durable journal, and a journal that already exists prevents another POST even if the prior SSH result was lost. No retry loop sends model requests.

Use local Python 3.11 or newer for the standard-library TOML parser:

```sh
python3.11 scripts/migration/model_canary.py
python3.11 -m unittest discover -s scripts/migration -p model_canary_test.py -q
python3.11 scripts/migration/model_canary.py --apply source
python3.11 scripts/migration/model_canary.py --inspect source
```

Run `--apply target` only after the migration owner has confirmed the live target application is ready. That route has not been approved merely because this document or runner exists.

| Lane | HTTP endpoint reached from existing AWS SSH session | Data authority checked |
| --- | --- | --- |
| `source` | `127.0.0.1:8081/v1/responses` | Existing AWS PostgreSQL primary |
| `target` | HTTPS directly to `43.165.177.136:443/v1/responses`, using SNI/Host `api.b4im.com`; the bridge at `127.0.0.1:18081` is separately checked with `/health` | Same AWS PostgreSQL primary, before data handover |

Both lanes are explicitly for acceptance **before** the data authority changes. The database client refuses a stopped/replaced source container or changed primary/system identity. Post-promotion verification requires the migration owner's separate authoritative-target check; this runner never guesses or automatically changes database authority.

## Request and secret transport

The local process privately reads `model_providers.custom.experimental_bearer_token` from `~/.codex/config.toml`, requiring a private file owned by the local user. It does not print that configuration, read another user's key, write the token to an artifact, or put it in command arguments or environment variables. The fixed worker and in-memory payload travel through the existing SSH stdin to `sudo -n python3 -` on the AWS alias `sub2api`.

The source lane uses direct loopback HTTP; the target lane uses the verified HTTPS transport below. The Bearer header is constructed only in remote process memory. The worker first checks `/health`. The only model POST uses `gpt-6.1-sol`, a fixed short plain-text prompt requesting `MIGRATION_OK`, `stream=true`, `store=false`, reasoning effort `low`, and `max_output_tokens=128`. No user conversation text is copied. It records metadata and byte counts/hashes; model output text and raw SSE events are not retained.

For the target lane, the same AWS worker connects to the fixed Tencent public IP while Python HTTPS uses `api.b4im.com` for both HTTP Host and TLS SNI. The standard trust store verifies the certificate chain and hostname before any Bearer header is transmitted. It also requires the reviewed leaf DER SHA-256 `d85ae437e821b8ad794a19e001019d21fcbd8ca6b54cfa658385652fd33c4d09`. The target entry owner computed that pin from the staged certificate and verified its hostname and expiry. `/health` is checked through both the bridge and target TLS; the single model POST uses the actual Caddy TLS route. Evidence records peer IP, TLS version, verified hostname/chain and leaf hash. Public DNS remains unchanged throughout this acceptance.

Each request carries independently generated UUID client correlation and unique `X-Request-ID` headers. The baseline `ClientRequestID` middleware generates a new UUID instead of consuming the incoming client header. Therefore the runner records both supplied and **response-returned** `X-Client-Request-ID`, requires the unique `X-Request-ID` echo, and looks up billing under `client:<returned UUID>`. This matches `resolveUsageBillingRequestID`; using the supplied client header would query the wrong row. Response IDs must remain consistent throughout the stream.

## Stream acceptance

Acceptance requires HTTP 200 with SSE content type, at least one nonempty output-text delta, a `response.completed` event with `status=completed` and consistent nonnegative input/output/total token counts, followed by a clean HTTP body EOF. Error/failed/incomplete events, truncated frames, invalid JSON/UTF-8, mismatched response IDs, duplicate completion, and text after completion fail. Per-line/frame/stream/event limits and connection/total timeouts bound resource use.

Evidence includes first output-text delta time, completion time, total stream time, response ID, server correlation ID, stream token counts, and a hash/size of the stream. A completed response without a clean stream EOF does not pass.

## Billing evidence and its limits

Every SQL operation first checks the immutable AWS PostgreSQL container ID `c99ec20ad4468b29698083cdc6827c853141717f0cf0a7a98bfed32e788414ee`, its reviewed image, and running state. Initial preflight verifies primary role and system identifier `7626048206782038050`. Queries use PostgreSQL read-only transactions with a two-second statement timeout. SQL and any private key comparison travel through Docker stdin, never arguments; raw PostgreSQL stderr is discarded.

After streaming, the runner waits up to 30 seconds for exactly one `usage_logs` row and exactly one committed `usage_billing_dedup` row for **only** the response-returned request ID and this API key. The model row must preserve the requested model and `stream=true`; output tokens must match SSE usage. Ordinary input plus cached-read and cached-creation buckets must equal the SSE input count, matching the source code's mutually exclusive OpenAI token buckets. The recorded positive actual cost must match total cost times the recorded rate multiplier within database precision. The report includes the eight-decimal charge produced by the production monetary rounding rule.

The exact billing dedup row is committed in the same normal-path transaction as balance/subscription/quota effects. The runner also checks the legacy `billing_usage_entries` row for this exact usage-log ID if one exists. Current normal billing code does not populate that legacy ledger, so its absence is reported explicitly and does not imply a lost charge. A per-request balance delta is proved only when a matching applied balance ledger entry exists. This canary does not claim such an independent balance proof from a shared user's before/after balance.

The pre/post API-key quota counter delta is reported separately, including whether a quota is enabled. The same key may serve the current chat concurrently, so that aggregate delta is explicitly **not** attributed to this canary. No full user-balance delta or other requests' usage rows are read. This preserves the distinction between unique request accounting evidence and independently observed per-request wallet movement.

## No-repeat and evidence

The remote root-owned private directory is `/var/lib/sub2api-migration-20261016/model-canary/`. It reserves `source.json` or `target.json` with exclusive creation, then fsyncs an attempted-POST intent before sending. Subsequent status replaces are atomic and fsynced. A lost response, worker failure, or existing failed journal cannot trigger an automatic replacement request. Use `--inspect source|target` to recover evidence without reading the key or issuing HTTP/SQL requests. Reviewing or retiring a journal requires a separate owner decision.

Local mode-0600 request/report artifacts are stored below `~/.codex/backups/sub2api-model-canary-<lane>-<id>/`; the request artifact excludes the token. Remote and local reports contain no raw model output, user identity, authorization values, full headers, or SQL errors. Route, application lifecycle, data authority, firewall and DNS changes have no mutation path in the runner.

These source/target canaries verify normal real-model streaming and request accounting at their observed times. They do not by themselves prove an active model stream survives a later production cutover, the cumulative ten-second migration wait budget, or performance under peak live load.

## Verification

- Fourteen offline tests pass, covering offline default, stream completion/error/disconnection refusal, token accounting/cost consistency, exact billing lookups, fixed routes and single POST behavior, target TLS IP/SNI/hostname/chain/leaf-pin checks, separate bridge/TLS health checks, private SQL/SSH stdin, token exclusion from arguments/artifacts, durable attempted-POST intent/no-repeat after an ambiguous request, and generated remote source compilation.
- A read-only source preflight succeeded: configured key active, API-key quota disabled, primary and immutable source identity matched; nonexistent exact-ID probe returned zero usage and dedup rows. It issued no model request.
- Source baseline passed with exactly one POST: first output-text delta **1.603s**, full clean stream **1.635s**, `response.completed`, 13 events, and 24 input / 8 output / 32 total tokens. Exact request `client:1f1a80cd-2045-4e10-bee8-00c5b3e79c04` has one usage row and one committed billing-dedup row. Total cost **USD 0.0001280000**, rate multiplier **0.2**, actual/quantized charge **USD 0.00002560**. API-key quota is disabled; legacy per-request ledger absent and independent balance-delta attribution explicitly false.
- Source evidence: AWS `/var/lib/sub2api-migration-20261016/model-canary/source.json`; local mode-0600 `/Users/mima0000/.codex/backups/sub2api-model-canary-source-7161e20b6c3e4496/report.json`, canonical report SHA-256 `7b058ce7957ec2a20ac17c75c5cf80fdf287db921f1eaa4ac0940228b4ba43f0`.
- After the migration owner confirmed application readiness and the entry owner verified target Caddy TLS/health/WebSocket behavior, the single target model canary passed. It connected directly from AWS to Tencent port 443 with `api.b4im.com` SNI/Host, verified the system trust chain/hostname and reviewed leaf pin, and used TLSv1.3. Bridge port 18081 health and actual TLS health both returned 200. The model stream first returned output text at **1.916s** and ended cleanly at **2.036s**, with 13 events, `response.completed`, and 24 input / 8 output / 32 total tokens. Exact request `client:c35a3439-5be0-4ca5-b355-39e219a06c7e` has one usage row and one committed billing-dedup row. Its **USD 0.00002560** actual/quantized charge matches the source baseline's observed charge; independent wallet-delta proof remains false.
- Target evidence: AWS `/var/lib/sub2api-migration-20261016/model-canary/target.json`; local mode-0600 `/Users/mima0000/.codex/backups/sub2api-model-canary-target-9f8dbb9e5cfd4a6e/report.json`, canonical report SHA-256 `a7e04495c3efd7f39de6dafc266967cf5f08e550bb3ed6e4c34d72dd0ca5bdb1`. Both lanes are complete and reserved against repeat POSTs. Public DNS, application/data authority and production route selection were unchanged by these canaries.
