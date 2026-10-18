# Codex HTTP alignment operations

The accepted scope is in [the design](CODEX_ALIGNMENT_MONITOR.md). The monitor reports differences and missing evidence. It never fixes the gateway, promotes a baseline, merges code, or deploys an upgrade.

## Installed services

| Unit | Purpose |
| --- | --- |
| `codex-alignment.timer` | Checks the queue every minute; daily due time is 09:00 Asia/Shanghai |
| `codex-alignment.service` | Runs one serial probe matrix, retries pending notifications |
| `codex-alignment-snapshot.timer` | Refreshes a sanitized production receipt every minute |
| `codex-alignment-snapshot.service` | Root-only Docker/DB reads; the runner has neither Docker access nor DB credentials |

The runner lives at `/opt/sub2api-alignment`, configuration at `/etc/sub2api-alignment`, and state at `/var/lib/sub2api-alignment`. All authentication, SMTP, proxy and WeChat values remain in private files. Do not paste those files, native CLI logs, Docker inspections or database rows into issues.

```sh
sudo systemctl list-timers 'codex-alignment*'
sudo systemctl show codex-alignment.service -p ActiveState -p Result -p ExecMainStatus
sudo -u sub2api-alignment /opt/sub2api-alignment/venv/bin/python \
  -m tools.codex_alignment --config /etc/sub2api-alignment/config.json status
```

`latest.json` and `runs/<run>/report.json` contain sanitized results and immutable per-run provenance. `progress.json` records completed phases if the watchdog interrupts a run. `outbox/` retains undelivered notifications separately for each channel. Never treat an old successful result as proof of current health.

Each completed run also writes `latest.md` and `runs/<run>/report.md`: a Chinese report with a plain-language conclusion, request completion counts, scenario results, explained differences, coverage limits, and suggested next steps. It distinguishes successful responses from exact request alignment and exposes incomplete checks even when differences were also found. It includes every difference location already recorded in the JSON report, grouping repeats without truncating the document.

Email includes the entire report in its body and a UTF-8 Markdown attachment. WeChat sends one native `.md` file attachment containing the entire report, with the run ID in its filename; it sends no long text chunks. The file uses iLink’s AES-128-ECB/PKCS#7 media protocol and the WeChat CDN through the same fixed SOCKS exit. Only the exact HTTPS CDN upload endpoint is accepted, and bot authorization stays on the API host. A failed file delivery remains queued; it does not fall back to long text or resend an already delivered email. Legacy partially sent text notices finish by sending the complete document as a file. Delivery is complete only after both channels finish. A process crash after remote acceptance but before saving the local receipt can cause a retry; the message ID remains stable.

The runner command `notify-latest` regenerates and resends the last report, explicitly labeled as an existing result. It makes no model calls and leaves the original JSON and probe schedule unchanged. Run it using the installed service's usual user, working directory, and network restrictions; restore the normal `tick` command afterward. This is useful when improving report wording or checking notification delivery without paying for another live probe.

## What a run does

1. Acquires a process lock and verifies fresh production health, account identity and proxy binding.
2. Reads the exact `main` SHA and its matching Actions conclusion. Missing/failed CI remains separate from production findings.
3. Discovers official stable Codex, verifies published digests for both the CLI and its code-mode host, and retains the approved 0.158.0 binary separately. Existing 0.158.0 fixtures approve body structure only.
4. Captures a separate local default ClientHello offer, then ordinary CLI requests through a local inspection CA and the selected SOCKS exit. Local WebSocket upgrades are rejected so the CLI falls back to HTTP. Apps, plugins and analytics are disabled in the synthetic CLI workspace.
5. Replays native HTTP payloads through the dedicated loopback gateway key and captures the actual gateway HTTP and ClientHello. Ordinary requests run every day; tools and compaction rotate. Acceptance and official version changes run both. Compaction V2 is observed through `compaction_trigger`, provider compaction output and subsequent replay.
6. Exercises Responses, Chat Completions and Messages independently. These bridge tests check successful completion and converted request semantics, not equality with the CLI workspace prompt.
7. Saves differences, incomplete checks, versions, hashes, timings and resource measurements; sends changed failures, recoveries and daily reminders of persistent inability through QQ SMTP and WeChat.

Install the distribution `bubblewrap` package so official shell tools can keep their read-only sandbox. On Ubuntu 24.04, install `deploy/codex-alignment.apparmor` as `/etc/apparmor.d/codex-alignment` and load it with `apparmor_parser -r`; the systemd unit explicitly selects this profile to permit user namespaces. Host-wide AppArmor settings stay unchanged. Tool/compact probes must read a random sentinel from `probe.txt` and return it exactly; a successful CLI exit after a tool failure is insufficient.

The initial watchdog is 60 minutes; systemd permits 65 minutes for setup/cleanup. Probes are serial. The CLI request budget is shared across scenarios, and the signed gateway budget counts every observed upstream HTTP attempt, including retries. Neither is a strict money limit. OAuth refresh uses separately bounded encrypted tunnels through the same exit; credentials are never inspected. The systemd network policy denies external addresses except the configured proxy IP.

## Interpreting results

- `pass`: the specific observed check succeeded within its stated scope.
- `drift`: captured comparable fields differ. Paths, not private field values, identify changes.
- `unverified`: missing evidence, unsupported model, unavailable CI, provider rejection, stale snapshot, timeout or resource failure. This is never a pass.

Each report lists boundaries. Native inspected HTTP and the independent default TLS offer are separate measurements; they do not prove same-transaction direct wire equality. Gateway probes use fresh connections. Scoped identity values differ intentionally. The current observer verifies session/body and metadata/header relationships, but opaque turn-metadata JSON contents and the UUID/window-suffix relationship remain unverified. Unknown request fields outside documented exceptions remain in comparisons.

On 2026-10-18 the selected account's visible model catalog offered Lite models only. The configured live model is `gpt-6-astra`; standard-mode coverage remains offline and is explicitly unavailable live. A newly visible standard model raises a selection-review finding. Do not change the approved baseline to silence a result.

## Triggers and changes

Actions waits five minutes on pushes and cancels superseded runs on the same branch/PR; it has no daily count cap. The server coalesces repository, CI, image and relevant configuration changes into the latest pending generation. A live request is never cancelled by a commit. The daily run remains a backstop.

Changing `watchdog_seconds`, models or request budgets requires updating the private runner configuration; gateway attempt limits additionally live in its private observer configuration. If changing the watchdog beyond 60 minutes, review the signed-ticket validity limit and systemd timeout together. A proxy change also requires updating the observer's exact URL hash and the private systemd IP allow drop-in. Unmatched changes fail closed.

## Login, notifications and storage

Use the existing local login helper to initiate the official server login. Its credential directory is independent of the production account's refresh state. Do not copy gateway OAuth tokens into it. Re-run the private QQ setup helper when rotating the authorization code. Notification failures stay queued; do not delete the outbox to hide them.

Ordinary evidence expires after 30 days, incident evidence after 90 days, and approved baselines never expire. Interrupted evidence stays for diagnosis. The app-owned observer spool is cleaned by the privileged snapshot helper. At the storage limit the monitor refuses new probes rather than deleting unexpired evidence. Binary cache keeps the approved and current versions and is excluded from the evidence limit; their digests remain in reports.

## Pause and rollback

```sh
sudo systemctl disable --now codex-alignment.timer
```

This prevents new scheduled runs. Let an active probe finish unless it is stuck; stopping its unit kills that isolated probe process group and the next run reports interruption. The snapshot timer is read-only and can also be disabled if retiring the monitor.

The application observer is inactive for ordinary requests. The initial production deployment retains the original application container and a private deployment receipt. The private deployment helper provides an explicit rollback action; review its current phase before running it, require a quiet application and never force-kill live streams. Keep PostgreSQL, Redis, the existing entry route and completed migration proofs intact. The keeper adapter changes only the reviewed application identity; it does not rewrite migration history.

## Offline verification

```sh
python -m pip install -r tools/codex_alignment/requirements.txt
python -m unittest discover -s tools/codex_alignment -p 'test_*.py' -v
cd backend
go test -race ./internal/pkg/codexprobe
go test -tags=unit ./internal/pkg/tlsfingerprint ./internal/repository \
  ./internal/service ./internal/server/middleware \
  -run 'Codex|Zstd|Compact|OpenAI.*(Retry|Failover|Timeout|401|429|5xx)|HTTPUpstream.*(Disconnect|Timeout)|Alignment' -count=1
```

Fault injection is local/mock only. CI receives no production secrets and makes no live account calls.
