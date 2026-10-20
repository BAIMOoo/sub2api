# Codex HTTP alignment monitor

## Accepted scope

Detect fork regressions after upstream merges and changes in official Codex stable releases. Report repository and deployed-service results separately, with immutable source, image, configuration, model, provider, and binary-digest provenance. WebSocket is outside this first release.

The production host runs lightweight, serial probes using the owner's selected production OAuth account and fixed SOCKS exit. GitHub Actions runs the full offline regression suite. Test credentials and notification recipients are private configuration, never repository fixtures. A separate gateway probe key identifies synthetic traffic; it does not give the shared OAuth account an independent upstream allowance. Official Codex uses its own login directory, not a writable copy of the gateway's refresh-token state.

## Scheduling and resource controls

- Every day at 09:00 Asia/Shanghai, plus repository/CI changes, production image or binary changes, and private monitor-rule changes.
- Admin account, proxy, group and global application settings do not trigger extra live runs. Their full configuration fingerprint remains in each run's production evidence; scheduled and manual runs check the current settings.
- Same-branch commits have a five-minute quiet window; obsolete alignment workflow runs are cancelled. No daily workflow or live-run count limit.
- Live runs are serialized and repeated triggers coalesce to the latest state. Do not cancel a live provider request merely because another commit arrived.
- Initial configurable watchdog: 60 minutes, solely to terminate a stuck run. Measure phase durations and peak resources in the first acceptance run before tuning.
- Resource exhaustion, a refused proxy, missing identity, an unsupported model, incomplete capture, and deadline expiry mean incomplete/unverified, never a passing comparison.
- No compilation on the production host. Native binaries are downloaded from official releases and verified against published digests.

## Evidence and comparison

Keep the approved baseline immutable; discover the latest stable independently. New candidates never silently overwrite the approved baseline. The existing 0.158.0 fixtures establish body provenance only, not complete OAuth wire approval.

HTTP content captured through a TLS inspection proxy and a separately captured native ClientHello are independent evidence. A proxy's upstream TLS is not native TLS. In official 0.163.0, the custom-CA path explicitly chooses rustls; its handshake is not evidence of the default TLS backend. The same-transaction native direct final HTTP remains unverified until a suitable capture method is actually demonstrated.

Observe gateway HTTP bytes after header ordering and before TLS encryption, only for authenticated synthetic probes. Compare body structure/content, ordered headers, encoding, session relationships, and TLS features separately. Dynamic identities must retain their format and cross-field relationships. Compatibility exceptions are explicit, reviewed rules; preserve unknown fields so new changes cannot disappear in normalization.

Native-forwarding checks preserve the supplied request except those exceptions. Responses, Chat Completions, and enabled Messages-to-Codex bridges have separate semantic contracts; their prompts and tool sets are not expected to equal a CLI's built-in workspace prompt. Record the final model and observed standard/Lite mode rather than inferring mode from a model name.

Normal live requests run daily; tool continuation and compact scenarios rotate. Initial acceptance and official-version changes expand the normal matrix. Inject disconnects, timeouts, 401/429/5xx, account changes, and proxy failures only in an isolated mock environment.

Gate every observable upstream attempt, including retries, before sending. CLI invocations and TCP connections are not request counts. If a path cannot enforce or observe a property, report that boundary explicitly.

## Privacy and reporting

Raw authentication headers, cookies, login state, and unfiltered TRACE output never enter reports, ordinary logs, or CI artifacts. Capture readers sanitize in memory before writing. Raw TRACE is only pre-serialization body evidence. Synthetic sessions use an isolated empty workspace and fixed fixtures.

Retain ordinary sanitized evidence for 30 days, incident evidence for 90 days, and approved baselines indefinitely. Bound local capture/log storage; report incomplete evidence or export it before cleanup rather than silently violating retention. Every report includes per-scenario evidence status and the last verified time.

Notify through the existing WeChat identity and QQ SMTP on a new meaningful issue, a changed issue, recovery, or persistent inability to run. Unchanged/non-actionable runs are quiet; notification failures remain pending for retry. Sender operation must not depend on a desktop or an LLM. SMTP uses implicit TLS on smtp.qq.com:465 and an authorization code stored outside the repository.

The monitor reports only. It must not merge code, repair application behavior, deploy application upgrades, or approve a new baseline automatically.

## Acceptance

1. Offline tests demonstrate detection of body/header drift, session mistakes, retry/failover mistakes, missing evidence, redaction failures, trigger overlap, and notification deduplication/recovery.
2. Validate the native capture modes and strict proxy routing in isolation; record each limitation without presenting a combined full-wire pass.
3. Benchmark on the production host with explicit memory/CPU/storage bounds, one probe at a time, and monitor application health during acceptance.
4. Verify actual exit routing and current model availability. Complete the official Codex login if required; never copy the live gateway's writable refresh state.
5. Verify probe-only production evidence, real provider acceptance, daily and code/deployment-change detection, suppression of admin-only triggers, and both notification deliveries.
6. Leave a runbook, reproducible fixtures, rollback instructions, and an initial report tied to the running image. Unverified capabilities remain visible in that report.

## Primary evidence

- [Existing request contract](CODEX_EXEC_WIRE_CONTRACT.md)
- [Official custom CA implementation](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/http-client/src/custom_ca.rs)
- [Official HTTP transport](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/http-client/src/transport.rs)
- [QQ authorization-code instructions](https://wx.mail.qq.com/list/readtemplate?name=app_intro.html#/agreement/authorizationCode)
