# Once-only postcutover Tencent model and billing acceptance

`postcutover_canary.py` sends at most one small billable Responses request for
this migration's independent `postcutover` lane. No arguments is entirely
offline. This tool has been implemented and tested locally; implementation
does not execute the live canary. The migration leader invokes it after the
data cutover and background activation have completed.

```sh
python3.11 scripts/migration/postcutover_canary.py
python3.11 scripts/migration/postcutover_canary.py --apply
python3.11 scripts/migration/postcutover_canary.py --inspect
```

`--apply` is a single paid acceptance request. `--inspect` reads the saved
remote report without reading the local provider token or performing HTTP,
SQL, resource inspection or target RPC calls. A failed, ambiguous or successful
attempt all reserve the same lane. No automatic repeat request exists.

## Target and authority prerequisites

The worker runs as root over the existing Tencent SSH route from
`prepare_channel.py`, using strict host-key checking and the already configured
key/proxy settings. It does not change SSH configuration. The local process
privately reads the existing custom-provider token using `model_canary`'s
private TOML reader. The token travels only inside SSH stdin to
`sudo -n python3 -I -`; it is not placed in command arguments, environment
variables, reports or local request artifacts.

The worker verifies root-owned, non-writable, non-symlink ancestors and the
reviewed installed `target_rpc.py` hash before importing it. The exact accepted
hash is `RPC_SHA256` in the script; an RPC upgrade requires explicitly reviewing
and updating that pin. The RPC's own config, program/machine checks and resource
inspection checks remain active. Accepted pins are:

| Resource | Required identity |
| --- | --- |
| RPC/app owner | `30bda011defad8149621c2630a2bd89c` |
| Original Tencent PG | `ac9f494b2fd0ec87257a9a7308a6ad7b270051d1dc4da33b33fea9af2885418c` |
| PG image | `sha256:ff4a089dadda5e50a5f09604b2e2bc16f97d5befb6b05c328fc21159da96c602` |
| PG system identifier | `7626048206782038050` |
| Original Tencent app | `cd5c29cf86b2595fffc41fc068e024d3f790f69bb8bdaaacc63eaba1ac9bcd82` |
| App image | `sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a` |

Before sending the request, the worker requires both RPC ledger authorities to
be `TENCENT`, both persisted app endpoints to be `target`, and no incomplete
mutation operations. It reads the existing ledger directly and never admits a
new RPC operation. The PG authority manifest must be `PRIMARY` with the same
accepted promotion intent as the ledger. Fresh PG sampling must prove the
original system identity, writable primary role, reviewed version/network and
an advanced timeline. The original target Redis must be a stable master.

The app must retain its original identity and be ready/serving without drain.
Its SQL route must be target and unpaused; Redis must be target port26379,
unpaused with pubsub/cache ready; writers and ingress must be open. Background
workers must be enabled, activated, no longer activating, with a nonempty
started list, an empty pending list and no failures. The worker pins the app
instance ID/PID and checks the same stable evidence before and after streaming
and billing. It does not activate background workers itself.

## HTTPS request and reused acceptance logic

The model request connects directly to `43.165.177.136:443` with HTTP Host and
TLS SNI `api.b4im.com`, regardless of current DNS. Python verifies the normal
certificate chain and hostname, then the existing reviewed leaf certificate
pin from `model_canary.py`. The worker checks `/health` over this same HTTPS
route first. It does not disable TLS checks or use the AWS bridge port18081.

The script reuses the reviewed `model_canary.py` implementation, bound to
SHA256 `b8d6096172abd798865aaf0263267bd66e4ebf74cfae6227caa648da4e17ed1f`.
It embeds that reviewed source in the SSH stdin worker rather than requiring
another remote installation. Request construction, direct verified target TLS,
SSE parsing and exact-request billing validation are the original functions.
Only the reusable module's SQL provider is replaced with the pinned Tencent
read-only implementation. There is no source database fallback.

The fixed request uses `gpt-6.1-sol`, `stream=true`, `store=false`, low reasoning,
128 maximum output tokens and the same short `MIGRATION_OK` prompt. It records
the server-returned client UUID, consistent response ID, timing, token counts
and stream hash without retaining generated text or raw events. Acceptance
requires text output, a valid completed response and a clean HTTP stream EOF;
failed/incomplete/truncated streams fail without another POST.

## Exact accounting against the promoted Tencent database

Every SQL read re-proves the fixed target primary and current target authority.
It runs only through:

```text
docker exec --user postgres -i <fixed-target-PG-ID> psql ... -h /tmp -p 25432 -U sub2api -d sub2api
```

Port25432 is explicit: this target uses host networking and does not listen on
the default5432. SQL, including the private key comparison, travels through
Docker stdin in `BEGIN READ ONLY` with a two-second statement timeout. The
existing target backend discards command stderr from public evidence.

The inherited exact-request lookup waits up to30 seconds for one usage row and
one committed billing-dedup row for this API key and the response-returned
`client:<UUID>`. It checks model/stream identity, uncached plus cached input
buckets against stream usage, output tokens, positive cost, rate multiplier,
rounding and any exact legacy billing ledger row. Shared key quota movement is
reported separately and is not attributed entirely to this request. An absent
legacy per-request balance ledger does not become an independent wallet-delta
claim. This is the same evidence standard as the earlier pre-cutover canaries.

## Durable once-only record

The independent root-private scope is:

```text
/var/lib/sub2api-migration-20261016/postcutover-canary/target.json
```

The directory is root0700; its report is root0600. An exclusive initial journal
creation reserves the lane before preflight. The worker fsyncs
`request_attempted=true` and `post_intent_durable=true` before the sole model
POST. Later updates use atomic replace, file fsync and directory fsync. The
token is excluded from evidence and the final report is checked for accidental
token inclusion. Existing source/target model-canary journals are untouched.

Lost SSH/model receipts, failed preconditions, filesystem errors or an existing
journal do not authorize another request. Preserve the record and use
`--inspect`. A crash between durable intent and POST can leave an attempted
marker without a request; the conservative lane reservation still holds.

Local private request/report artifacts are created under
`~/.codex/backups/sub2api-postcutover-canary-<id>/`. The emitted JSON includes
the local report path and canonical report SHA256. A local timeout may leave a
remote worker finishing its journal, so recover that journal instead of
reissuing `--apply`.

This tool changes no DNS/Cloudflare records, routing, application lifecycle,
runner state or data authority. Its accepted effects are its own evidence
files, read-only inspection and the single normal billable request. A passing
result proves the observed target route and accounting; it does not establish
peak-load capacity, survival of earlier streams, the cutover wait budget or
completion of the separate AWS retirement/keeper acceptance.

## Offline checks

```sh
python3 -B -m unittest discover -s scripts/migration -p postcutover_canary_test.py -q
python3 -B -m unittest discover -s scripts/migration -p model_canary_test.py -q
```

The new tests cover default offline behavior, reviewed-source/hash binding,
generated worker compilation, exact resource/primary/authority checks, nondefault
SQL port/stdin, background readiness, pre/post instance changes, exclusive
journaling, fsync-before-POST, ambiguous receipts, duplicate billing refusal,
verified HTTPS health, token exclusion and read-only inspection. All tests use
fakes or private temporary files and send no model/network/SQL/SSH request.
