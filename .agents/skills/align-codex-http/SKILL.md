---
name: align-codex-http
description: >-
  Upgrade or maintain the pinned Codex direct HTTP egress baseline and host alignment monitor in BAIMOoo/sub2api.
  Use when bumping the pinned codex_exec version (e.g. 0.171.0 to 0.172.0+), capturing official Linux x86_64 HTTP/TLS fixtures,
  running opt-in codexlive supplier tests, syncing /opt/sub2api-alignment, or diagnosing codex-alignment drift/unverified reports.
---

# Codex Direct HTTP Alignment Workflow

Runbook for upgrading BAIMOoo/sub2api's pinned direct HTTP `codex_exec` egress baseline and keeping the production host alignment monitor synchronized.

## Before starting

Read [FORK_CONTRACT.md](../../../FORK_CONTRACT.md) and [GLOSSARY.md](../../../GLOSSARY.md). Record three states separately and keep them distinct throughout the session:

1. **Contract target**: what the owner approved in `FORK_CONTRACT.md`.
2. **`main` state**: what is merged and verified on `origin/main`.
3. **Production state**: what `sub2api-app` and `/etc/sub2api-alignment/config.json` actually run right now.

Also keep [CODEX_EXEC_WIRE_CONTRACT.md](../../../docs/CODEX_EXEC_WIRE_CONTRACT.md) and [CODEX_ALIGNMENT_RUNBOOK.md](../../../docs/CODEX_ALIGNMENT_RUNBOOK.md) as the single sources of truth for wire details and host operations; do not restate them here.

## Phases

### 1. Capture official Linux x86_64 evidence

Follow [references/capture-and-live-verify.md](references/capture-and-live-verify.md) to capture two strictly separate layers for target `rust-v<X.Y.Z>`:

- Isolated Alpine/Linux x86_64 HTTP capture (headers, header order, Standard/Lite/Compact V2 bodies, zstd framing, release SHA-256 provenance).
- Independent native TLS ClientHello capture (no custom CA, no proxy, HTTP fallback leg only).

**Done when**: new fixture files and `provenance.json` are committed on a `codex/*` branch with release digests, and every header, body, compression, and TLS delta against the previous baseline is listed.

### 2. Update codebase and verify offline

Walk every touchpoint in [references/version-bump-checklist.md](references/version-bump-checklist.md). Keep upstream `codexCLIVersion` fallback (`0.146.0`) and the full WebSocket path untouched.

Run local checks serially with bounded resources:

- Python monitor suite via `uv run --with-requirements tools/codex_alignment/requirements.txt python -m unittest discover -s tools/codex_alignment -p 'test_*.py' -v`.
- Single-package serial Go tests (`GOMAXPROCS=1 go test -p 1 -parallel 1 ...`), `gofmt`, and `go vet`.

**Done when**: every item in [references/version-bump-checklist.md](references/version-bump-checklist.md) is accounted for, all local tests pass, and `git diff` contains no unrelated edits or secrets.

### 3. Pass remote CI and layered live verification

Push the candidate branch to SSH remote `target` (`git@github.com:BAIMOoo/sub2api.git`) and fetch `origin`. Wait for all three workflows (`CI`, `Codex HTTP alignment`, `Security Scan`) to pass green on the exact candidate SHA.

When the owner authorizes live supplier testing, follow the cross-compile `codexlive` procedure in [references/capture-and-live-verify.md](references/capture-and-live-verify.md): query `/backend-api/codex/models?client_version=<target>` first to pick live Lite and Standard models, run serially per available credential type (`oauth`, `pat`, `setup-token`), and verify `wire_replayed_reasoning=true` on two-turn tool continuation.

**Done when**: all three CI workflows pass on the exact candidate SHA, every available credential type has per-route `response.completed` evidence, every unavailable credential type is explicitly marked `unverified`, and all temporary binaries/configs on the test host are removed.

### 4. Three-gate merge, release, and production rollout

Require explicit owner approval at each independent gate:

1. **Merge to `main`**: `--no-ff` merge onto `origin/main` (fork as first parent), refresh `FORK_CONTRACT.md` status, push to `target`, and wait for all three `main` workflows to pass green on `tag_sha`.
2. **Publish new release tag**: create a new annotated tag `vX.Y.Z` on `tag_sha` (preserve all historical tags intact), push to `target`, wait for `Release` workflow green, and record `tag_sha`, `post_release_main_sha`, and the exact `linux/amd64` `image_amd64_digest`.
3. **Production downtime upgrade**: follow `ssh-fleet` remote sync check and `update-sub2api-server` (audit, private checksummed backup, `--network none` image check, short downtime cutover, restart recovery, public `/health` & homepage, `audit_prod.sh ok=true`). Update `/var/lib/sub2api-alignment-deployment-20261018/observer.json` atomically before recreating `sub2api-app` when probe egress or `account_ids` changes.

**Done when**: production runs the exact `linux/amd64` digest with `audit_prod.sh` returning `ok=true` and `observer.json` matching the approved probe policy.

### 5. Sync host alignment monitor and run sandboxed acceptance

Only after Phase 4 passes on production:

1. Sync `/opt/sub2api-alignment` cleanly from `tag_sha` using [scripts/sync_alignment_host.sh](scripts/sync_alignment_host.sh) (dry-run first, then `--execute`).
2. Switch `approved_version` in `/etc/sub2api-alignment/config.json` to `<target>` if promoting the baseline (back up before edit).
3. Run one sandboxed `acceptance` patrol (`</dev/null`) following [references/capture-and-live-verify.md](references/capture-and-live-verify.md) and verify `codex-alignment.timer` + `codex-alignment-snapshot.timer` are `active/enabled`.

**Done when**: host unit tests pass, `build.json` matches `tag_sha` with zero stray files, and `acceptance` reports zero unexpected drift across `body`, `header_order`, `compression`, `tls`, and `user_agent_contract` on every allowed probe account.

### 6. Closeout

Update `update-sub2api-server` state/history references (in `/Users/mima0000/skills` SSOT and installed copy), post exact SHAs and layered evidence boundaries on tracking issues, and close completed tickets.

## Guardrails

- **Allowlist-only inspection of private files**: when inspecting `/etc/sub2api-alignment/config.json`, `observer.json`, or `auth.json`, extract an explicit allowlist of non-secret keys (never print tokens, `secret`, `gateway_key`, passwords, or raw inspect output).
- **Separate evidence layers**: offline HTTP/zstd, separate native TLS, live supplier checks, CI, and production deployment prove only themselves; missing evidence is always `unverified`.
- **Fail-closed monitor policy**: `direct_egress: true` and SOCKS proxy mode are mutually exclusive; never fall back silently from a broken proxy or widen probe `account_ids` beyond explicitly approved accounts.
- **Build off-host**: compile Go binaries locally or in GitHub Actions, never on the small-RAM production server.
