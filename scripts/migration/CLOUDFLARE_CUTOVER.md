# Fixed API DNS cutover

Status: code and nine offline tests pass. **No DNS apply has been executed by this runner.** The migration owner controls when the readiness, continuity and data-authority gates permit changing DNS.

The offline default reads no credential and makes no network request:

```sh
python3 scripts/migration/cloudflare_cutover.py
python3 -m unittest discover -s scripts/migration -p cloudflare_cutover_test.py -q
```

The only explicit execution entry is `python3 scripts/migration/cloudflare_cutover.py --apply`. It has no host, zone, record, address, TTL, proxy or credential-path override.

| Fixed property | Value |
| --- | --- |
| Hostname | `api.b4im.com` |
| Zone ID | `ee0df4c2b558c519b22bc2dddddca611` |
| Record ID | `e14e7e7d735bb542e19364a36e24199c` |
| Required type | `A` |
| Required current content | `13.231.143.136` |
| Target content | `43.165.177.136` |
| Required proxy state | `false` |
| Required TTL | `60` |

The existing `cloudflare_inspect.py.load_token` parser privately reads only `/Users/mima0000/.config/sub2api-migration/cloudflare.env`. No credential enters output, arguments, environment assignments or evidence. API calls use the standard verified HTTPS transport to the fixed Cloudflare API URLs. Redirects are refused to prevent forwarding an authorization header to another host. Errors expose a controlled reason or HTTP status, without response bodies or credential values.

Execution checks that the fixed zone has exactly one record for the hostname, including across record types/pagination totals. The listed record and direct fixed-ID read must be identical and satisfy every table precondition. It writes the private before snapshot, then immediately re-reads the exact record and compares the complete fingerprint, including its modification timestamp. Any changed identity, source IP, TTL, proxy flag, record count or other field refuses the operation before writing.

The sole mutation is one `PATCH` to the fixed record with the exact body:

```json
{"content":"43.165.177.136"}
```

No TTL, proxy flag, name, type, tags, comments or settings are included in that request. A new authoritative API read must return the expected target content with all fixed properties preserved. A hash of every other record field, excluding only `content` and `modified_on`, must match the before snapshot. An unexpected post-write result causes failure and retains evidence; there is no automatic rollback or second PATCH.

The [official Cloudflare DNS edit API](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/edit/) and [official SDK source](https://github.com/cloudflare/cloudflare-python/blob/main/src/cloudflare/resources/dns/records.py) describe editing the existing record but do not document an atomic `If-Match`/ETag condition for this endpoint. The runner therefore reports `conditional_api_write_proven=false`. Its compare-before-write check detects changes already visible at the final read; it cannot exclude an external edit in the remaining read/write interval. The migration owner must keep other DNS editors idle during this step. This limitation is explicit rather than claiming server-side atomic CAS.

Before sending PATCH, the runner fsyncs the attempted-write intent. If the response is lost or rejected, it performs a readback without repeating PATCH. If readback proves the exact desired state and unchanged attributes, the result records reconciliation. Otherwise its outcome requires read-only owner review. A later invocation that sees the target IP refuses the original source-IP precondition, instead of silently issuing another write.

Evidence is stored under a new mode-0700 directory `~/.codex/backups/sub2api-cloudflare-cutover-<id>/`. `before.json`, `after.json` when a readback is available, and `report.json` use mode 0600 and fsync. Reports contain the non-secret record projection, full/preserved-field hashes, intent, attempt count and readback outcome. They never include the full API record's comments/tags or HTTP authorization. No source/target application, database, routing, firewall, Caddy process or existing DNS-cache connection is changed by this runner.

Tests cover offline default, every fixed precondition, one-field update intent/evidence ordering, concurrent-change refusal, lost receipt reconciliation without retry, failed readback, scope/field restrictions, unique-record enforcement, redaction of API errors, and redirect refusal. Actual API mutation and DNS propagation remain untested until the owner's later authorized cutover.
