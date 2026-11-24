# Codex HTTP Version Bump Touchpoint Checklist

Consult during Phase 2 and Phase 5 whenever bumping the pinned `codex_exec` version (e.g. `0.171.0` -> `0.172.0+`). Account for every row; do not leave stale version constants behind.

## 1. Captured Fixtures

- `backend/internal/service/testdata/codex_exec_<ver>_linux_amd64/` (`http.json`, `standard.json`, `lite.json`, `compact.json`, `provenance.json`, `README.md`)
- `backend/internal/pkg/tlsfingerprint/testdata/codex_exec_<ver>_linux_amd64_clienthello.json`
- Retire superseded egress fixture directories unless explicitly retained for legacy inbound compatibility tests (such as `codex_exec_0158` for top-level `instructions` conversion).

## 2. Backend Go Code

- `backend/internal/service/openai_codex_native_http.go` — pinned `codex_exec` version & User-Agent string (keep upstream fallback `codexCLIVersion = "0.146.0"` untouched for WebSocket path).
- `backend/internal/service/openai_codex_exec_lite.go` — Lite / Standard body layout rules and `instructions` / `additional_tools` / `tools` placement.
- `backend/internal/service/openai_codex_http_mode.go` — mode selection (`baseline_selected` vs `unverified`) and logged `codex_version`.
- `backend/internal/service/openai_codex_version_cooldown_guard.go` — version-mismatch 400 cooldown guard.
- `backend/internal/pkg/openai/codex_bundled_models.go` — bundled Codex model catalog and `minimal_client_version` floor.
- `backend/internal/pkg/tlsfingerprint/httporder.go` & `profiles.go` — ordered header list (including `accept-encoding` policy) and `OpenAICodexLinuxProfile`.
- `backend/internal/pkg/codexprobe/probe.go`, `wire.go` & `backend/internal/repository/codex_alignment_probe.go` — probe `direct_egress`, `account_ids` allowlist, and `user_agent_contract` if UA structure changes.

## 3. Backend Unit & Reference Tests

- `backend/internal/service/openai_codex_<ver>_reference_test.go`
- `backend/internal/pkg/tlsfingerprint/codex_<ver>_reference_test.go`
- `backend/internal/service/openai_codex_version_floor_test.go` & `openai_codex_minimal_client_version_test.go`
- `backend/internal/service/openai_codex_exec_wire_contract_test.go` & `openai_instructions_fork_contract_test.go`
- `backend/internal/pkg/tlsfingerprint/httporder_test.go`
- `backend/internal/pkg/codexprobe/probe_test.go`
- `backend/internal/repository/http_upstream_codex_decompression_test.go` & `upstream_cookie_jar_test.go`

## 4. Frontend Catalog

- `frontend/src/api/codex.ts`
- `frontend/src/api/__tests__/codex.spec.ts`
- `frontend/src/components/keys/__tests__/UseKeyModal.spec.ts`

## 5. CI & Alignment Monitor Code

- `.github/workflows/codex-alignment.yml` (`approved_body_fixture_version`)
- `tools/codex_alignment/evidence.py`, `runner.py`, `capture.py`, `test_monitor.py`, `test_reporting.py`

## 6. Docs & Fork Contract

- `FORK_CONTRACT.md` (contract target + implementation status table; update status only after verified integration)
- `GLOSSARY.md` (if new domain terms are introduced)
- `docs/CODEX_EXEC_WIRE_CONTRACT.md`
- `docs/CODEX_ALIGNMENT_MONITOR.md` & `docs/CODEX_ALIGNMENT_RUNBOOK.md`

## 7. Production Host (Phase 5 only — after production rollout passes)

- Sync `/opt/sub2api-alignment` + `/opt/sub2api-alignment/build.json` via `scripts/sync_alignment_host.sh --execute`.
- Back up and update `/etc/sub2api-alignment/config.json` (`approved_version`, `models` if needed).
- Verify `/var/lib/sub2api-alignment-deployment-20261018/observer.json` (`direct_egress`, `account_ids`, `proxy_id: 0`, `proxy_sha256: ""`) and `/etc/systemd/system/codex-alignment.service.d/egress.conf`.
- Verify `/var/lib/sub2api-alignment/native-home/auth.json` is non-expired and matches `oauth_account_sha256`.
