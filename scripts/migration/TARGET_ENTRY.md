# Tencent API entry, run 20261016

`target_entry.py` reuses the exact `CONFIG` and `UNIT` strings from
`stage_target_entry.py`. It owns only the dedicated Tencent
`sub2api-tencent-entry.service`; the API-only upstream is `127.0.0.1:8082` with
`flush_interval -1` and Caddy admin disabled. Default invocation is offline.
The driver contains no source Caddy lifecycle, source routing, DNS, firewall,
application restart, database, or Redis mutation operation.

## Read-only staging evidence

The target was inspected over its existing pinned SSH path. The dedicated
unit was initially `not-found/inactive/MainPID=0`. After the leader explicitly
authorized lifecycle actions, install/start and TLS/WS checks passed; see the
execution evidence below. Staging pins:

| Artifact | SHA-256 |
| --- | --- |
| Caddy 2.6.2 executable | `9681d054b6e5137d53f6d027188b349e0603fd12ea6334bd0d49da6755432f1b` |
| Caddyfile | `077de5dac0181a7319c724e9131727bca6b0357e86762442f27308b8eaf56f45` |
| Dedicated unit | `0ea178533c2522b39136eb17da18471896201b15930a4b8e35a38c4788e6e893` |
| Certificate PEM file, including chain | `3bc3fa3eb21690d6d5ab8dffd15f82e42e2db09a181a379c062e9fa5795d6d4f` |
| Private-key file digest only | `c686fa6ae9e6b691ea9c9f6934509dc1ee61195795e12068fdd22a181a6a1e90` |
| Caddy certificate metadata | `34e8e6ce462bf405898e69ee93ef2d0bff51f4cf7fad9092dae982fead76147f` |
| TLS leaf certificate DER | `d85ae437e821b8ad794a19e001019d21fcbd8ca6b54cfa658385652fd33c4d09` |

The certificate covers `api.b4im.com` and expires 2026-11-24 06:51:30 UTC.
No private-key content is printed, copied to local evidence, or sent in an
argument. Before either lifecycle action, the driver rechecks each exact
hash, owner/mode, hardlink count and all ancestors for symlinks/untrusted
write access; verifies key/certificate pairing and at least 7 days validity.

## Leader execution commands

Run from `/Users/mima0000/.codex/worktrees/sub2api-live-migration/sub2api`.
The commands below describe the executed sequence. Install/start are retained
for review and reconciliation; do not repeat them as an automatic retry.

```sh
python3 scripts/migration/target_entry.py
python3 -m unittest discover -s scripts/migration -p target_entry_test.py -q
python3 scripts/migration/target_entry.py --inspect
python3 scripts/migration/target_entry.py --apply install
python3 scripts/migration/target_entry.py --apply start
python3.11 scripts/migration/target_entry.py --apply verify
```

`install` only creates the exact root-owned 0644 dedicated unit (or accepts
identical existing bytes), runs target `daemon-reload`, and proves that it
remains inactive. A different preexisting unit is preserved and rejected.
It does not enable or start the unit.

`start` requires that exact loaded unit with no drop-ins, pending unit job or
reload, a 200 loopback upstream health response, and no existing TCP or UDP
listener on 80/443 on any address. It issues only `systemctl start` for the
fixed Tencent unit. An already healthy active unit is inspected without a
second start. It never restarts a failed or uncertain unit automatically.
If a reply is lost, use `--inspect`; do not issue a blind restart or alter
AWS entry. Existing artifact bytes must remain available for reconciliation.

`verify` runs via the existing AWS SSH session, then opens native sockets
straight to `43.165.177.136:443`; it does not use DNS resolution or HTTP proxy
environment variables. Both TLS connections use SNI and Host `api.b4im.com`,
the system trust store with normal certificate-chain/hostname verification,
and the exact leaf DER pin. The first connection requires health HTTP 200.
The second privately sends the existing API key in a Responses WebSocket
Upgrade request, requires HTTP 101 and the exact WebSocket accept digest,
exchanges one masked ping with matching pong, and completes normal close
code 1000. Only control frames are allowed; `response.create`/text/data
frames have no send path. This verifies authenticated proxy transport and
creates no model response; the ingress lease is held only for this short
connection. It does not establish a real upstream model WebSocket session.

Verification reads the private local provider token through the existing
`model_canary.load_token` routine (local Python 3.11+), sends it only inside
SSH stdin, and never retains it or model/user output. Each successful command
saves a mode-0600 report under `~/.codex/backups/sub2api-entry-<mode>-<time>/`.
The printed `report_sha256` is SHA-256 of the exact saved report bytes,
including its newline. Errors use fixed codes and omit raw subprocess output.

## Acceptance before production routing

All of these must hold before accepting `entry_tls_sha256` or changing routing:

1. Install and start reports confirm exact pinned entry artifacts and a
   running dedicated Tencent unit. AWS shared `caddy.service` is unchanged.
2. Driver verification proves external target-IP TLS health, full trust and
   domain verification, pinned leaf, authenticated WebSocket 101, matching
   pong and normal 1000 close with zero model messages.
3. The leader's separate **single** TLS Responses SSE canary reaches AWS →
   Tencent `43.165.177.136:443` with Host/SNI `api.b4im.com`, receives text
   deltas plus `response.completed` and clean EOF, and proves exactly one
   usage row and committed billing-dedup row in the currently authoritative
   AWS PostgreSQL. That canary belongs to the live-runtime owner; this driver
   deliberately does not reserve or send a second model request.
4. The existing loopback bridge `127.0.0.1:18081` independently remains healthy.
   TLS entry success alone is insufficient to accept the AWS bridge.

If any check fails, leave AWS customer entry unchanged and investigate the
fixed Tencent resources. This script never reloads the source shared Caddy,
replaces existing live connections, promotes a database, or initiates DNS
cutover. Domain certificate renewal after migration requires retaining this
Caddy data store and allowing the future public validation path; the initial
hash pins intentionally refuse silently adopting a different certificate.

## Executed entry evidence

The leader explicitly authorized target entry install/start after the live
application was ready on source endpoints. These actions and the single
non-model WS probe completed successfully:

| Action | Local report directory suffix | Raw report SHA-256 |
| --- | --- | --- |
| Inspect before installation | `sub2api-entry-inspect-1792166935267086000` | `cebc1fd33ba78d5de94c334bfa88033b1de62c1e64520225fad5448a43a65811` |
| Inert install | `sub2api-entry-install-1792167036817075000` | `4c7257ec70e14886063570c2f45c5a381d499895c6d85ffb3df12b3400194f75` |
| Start target dedicated unit | `sub2api-entry-start-1792167055991854000` | `08247ce976ae1daa4c25297a7d30188c709ff2e1b3b293c7875e1ff6eebf812b` |
| AWS direct TLS health and idle WS | `sub2api-entry-verify-1792167073995319000` | `26add0c38d7ad65b2688959deacaa39b10003e2fe7162c4b9ae5a6158d1e9400` |

Directories are beneath `/Users/mima0000/.codex/backups/`; each `report.json`
is mode 0600. TLS health returned HTTP 200 with full chain/hostname verification
and the pinned leaf. The authenticated WS returned 101, matching pong, and
normal close 1000. No source Caddy or DNS change occurred. The target unit is
started but has not been enabled for boot by this driver.

The WS probe has one known log effect: because it deliberately sends no
`response.create`, the gateway logs `openai.websocket_read_first_message_failed`
after the client closes normally. A read-only, bounded target log review
correlated ingress start at `2026-10-16T16:11:13.884634960Z` and this warning at
`2026-10-16T16:11:13.893534654Z`, both request ID
`67cafaec-9d64-42ba-a3d1-96407ce8496e`, client request ID
`8bd55211-f5a7-480f-b475-2030d008e296`, AWS client IP `13.231.143.136`, and
`1000(StatusNormalClosure)`/empty close reason. These precede the successful
probe report completion at Unix `1792167073.8943298`. This warning records
the intentional absent first model frame; it is not a failed model request.
Sanitized metadata is retained separately in the verify directory as
`websocket-probe-log.json`; the original verify report/hash remains unchanged.
No repeated WS probe or model request was sent during this log review.

After ENTRYREADY, the live-runtime owner reported its one separate TLS SSE
canary passed (text deltas, completed event, clean EOF, exact single usage and
billing-dedup rows). Its evidence is separately owned at
`/Users/mima0000/.codex/backups/sub2api-model-canary-target-9f8dbb9e5cfd4a6e/report.json`,
canonical report SHA-256
`a7e04495c3efd7f39de6dafc266967cf5f08e550bb3ed6e4c34d72dd0ca5bdb1`.
That owner also verified the distinct bridge 18081 health response was 200.
