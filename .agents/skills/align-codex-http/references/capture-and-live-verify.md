# Capture, Live Verification, and Sandboxed Acceptance

Procedures and host gotchas for Phases 1, 3, and 5 of `align-codex-http`.

## 1. Official Linux x86_64 fixture capture

Keep HTTP capture and native TLS capture as two independent runs against the exact same official `rust-v<X.Y.Z>` Linux `x86_64` musl release binary (verify published release SHA-256 first):

- **HTTP / headers / body / zstd**: run `codex exec` inside an isolated Alpine Linux x86_64 container with a local inspection CA and synthetic credentials; reject WebSocket upgrades locally so `codex exec` takes the HTTP fallback path. Capture `http.json`, `standard.json`, `lite.json`, `compact.json`, `provenance.json`, and `README.md` under `backend/internal/service/testdata/codex_exec_<ver>_linux_amd64/`.
- **Native TLS ClientHello**: run the same binary without custom CA or proxy against a local TLS listener that records only the HTTP-fallback ClientHello (skip the initial WebSocket attempt). Save `backend/internal/pkg/tlsfingerprint/testdata/codex_exec_<ver>_linux_amd64_clienthello.json`.
- Never claim the two captures came from a single direct transaction.

## 2. Live model discovery before `codexlive`

Before running `codexlive`, query `/backend-api/codex/models?client_version=<target>` with the approved test OAuth token (never printing the token) to inspect current `slug`, `visibility`, `use_responses_lite`, and `minimal_client_version`. Do not assume retired slugs from older baselines (such as `gpt-5.5`) still work on `prolite` accounts.

Verify live coverage on at least:
- Active Lite models (e.g. `gpt-6.2-sol`, `gpt-6.2-astra`)
- Active Standard-wire models (e.g. `gpt-6-sol`, `gpt-5.6-sol`)

## 3. Cross-compiled `codexlive` execution

Compile on macOS (never on `sub2api-tencent`) and run serially on the approved host:

```bash
cd backend
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go test -tags=codexlive -c \
  -o /tmp/codexlive.test ./internal/integration
scp /tmp/codexlive.test sub2api-tencent:/tmp/codexlive.test
```

On the test host:
- Place a `0600` JSON file outside any repo containing `credential_type`, `access_token`, `model`, optional `chatgpt_account_id`, and optional `proxy_url`.
- Run serially per model and credential type:
  ```bash
  SUB2API_CODEX_LIVE_CONFIG=/tmp/live-test.json \
    /tmp/codexlive.test -test.v -test.run '^TestCodexHTTPPinnedLive$' -test.count=1
  ```
- Confirm all 6 routes (`responses`, `passthrough`, `chat-completions`, `messages`, `compact`, `tool-continuation`) report `response.completed` and `tool-continuation` reports `wire_replayed_reasoning=true` (decoded from zstd wire bytes).
- Delete `/tmp/codexlive.test` and `/tmp/live-test.json` immediately after verification (`trap 'rm -f ...' EXIT`).
- Record unavailable credential types (`pat`, `setup-token`) as `unverified` unless actually tested live.

## 4. Host native login and sandboxed `acceptance`

Check `/var/lib/sub2api-alignment/native-home/auth.json` using non-secret metadata only: file mode `0600`, non-expired JWT `exp`, and `sha256(chatgpt_account_id)` matching `/etc/sub2api-alignment/config.json`'s `oauth_account_sha256`. If expired, renew via:

```bash
ssh sub2api-tencent 'sudo -u sub2api-alignment bash -c "cd /opt/sub2api-alignment && /opt/sub2api-alignment/venv/bin/python -u -m tools.codex_alignment.login"'
```

Run a manual live acceptance patrol under the exact production systemd/AppArmor/bubblewrap sandbox (always redirect stdin from `/dev/null` when invoking over SSH):

```bash
ssh -n sub2api-tencent 'sudo systemd-run --pipe --wait \
  -p User=sub2api-alignment -p Group=sub2api-alignment \
  -p WorkingDirectory=/opt/sub2api-alignment -p UMask=0077 \
  -p CPUQuota=50% -p MemoryHigh=384M -p MemoryMax=512M -p TasksMax=128 \
  -p NoNewPrivileges=true -p AppArmorProfile=codex-alignment \
  -p PrivateTmp=true -p ProtectSystem=strict -p ProtectHome=true \
  -p ReadWritePaths=/var/lib/sub2api-alignment \
  -p "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK" \
  -p IPAddressDeny= -p IPAddressAllow=localhost \
  /opt/sub2api-alignment/venv/bin/python -m tools.codex_alignment \
  --config /etc/sub2api-alignment/config.json acceptance' </dev/null
```

Verify after the run:
- Read `/var/lib/sub2api-alignment/latest.md` and `/var/lib/sub2api-alignment/latest.json` (both are sanitized).
- Check `/var/lib/sub2api-alignment/observer/<run>/*-http-*.json` `account_id` counts to confirm all configured `account_ids` succeed with zero `502`.
- Confirm `0.171.0` (or the new approved baseline) shows zero `body_diff`, `header_order_equal=true`, `compression_equal=true`, `tls_diff=[]`, and zero `user-agent` false drift across container Alpine vs host Ubuntu.

## 5. Environment gotchas

- **macOS search**: `rg` may not be installed; use `git grep` or `grep -rn`.
- **macOS Python tests**: run `uv run --with-requirements tools/codex_alignment/requirements.txt python -m unittest discover -s tools/codex_alignment -p 'test_*.py'`.
- **Production PostgreSQL**: inside `sub2api-migration-pg-replica-20261016`, run `gosu postgres psql -h /tmp -p 25432 -U sub2api -d sub2api`.
- **Tarballs over SSH**: stage via `scp` to a temp path with `trap 'rm -f ...' EXIT` instead of piping `tar` into `ssh ... 'bash -s' << 'EOF'`, which conflicts on stdin.
- **Private JSON reads**: always select an explicit allowlist of non-secret keys (e.g. `account_id`, `account_ids`, `approved_version`, `direct_egress`, `proxy_id`, `models`, `oauth_account_sha256`); never rely on an exclusion list.
