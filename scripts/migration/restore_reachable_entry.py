#!/usr/bin/env python3
"""Offline by default; callable one-shot recovery of the fixed DNS entry only.

Business authority stays Tencent. The existing AWS HTTPS bridge is a temporary
network entry, never a fallback database. A reviewed read-only proof callback is
required by recover(); this standalone CLI deliberately cannot produce it.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import pathlib
import stat
import sys
import time
import urllib.error
import urllib.request

_PATH = pathlib.Path(__file__).with_name("cloudflare_cutover.py")
_SPEC = importlib.util.spec_from_file_location("reachable_entry_cloudflare", _PATH)
CF = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CF)
Refusal = CF.Refusal
need = CF.need
canonical = CF.canonical
sha = CF.sha
private_write = CF.private_write
no_symlinks = CF.no_symlinks
safe_record = CF.safe_record
preserved_digest = CF.preserved_digest
load_private_token = CF.load_private_token
NoRedirect = CF.NoRedirect
ZONE, RECORD, NAME = CF.ZONE, CF.RECORD, CF.NAME
SOURCE, TARGET, TTL = CF.SOURCE, CF.TARGET, CF.TTL
API, LIST_API, CREDENTIAL = CF.API, CF.LIST_API, CF.CREDENTIAL
RUN = pathlib.Path.home() / ".codex/backups/sub2api-reachable-entry-20261017"
MAX_PROOF_AGE = 120
SOURCE_CONTAINERS = ("sub2api", "sub2api-postgres", "sub2api-redis")


class Client(CF.Client):
    """Only fixed GETs and exactly one type of reverse entry-content PATCH."""

    def request(self, method, url, body=None):
        need((method == "GET" and url in {API, LIST_API} and body is None) or
             (method == "PATCH" and url == API and body == {"content": SOURCE}),
             "entry-dns-operation-outside-fixed-scope")
        request = urllib.request.Request(url, method=method,
            data=None if body is None else canonical(body),
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=20) as response:
                raw = response.read(1024 * 1024 + 1)
            need(len(raw) <= 1024 * 1024, "cloudflare-api-response-too-large")
            value = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise Refusal("cloudflare-api-http-" + str(exc.code) + "-no-secret-output") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise Refusal("cloudflare-api-result-unavailable-no-retry") from None
        need(isinstance(value, dict) and value.get("success") is True, "cloudflare-api-request-rejected")
        return value

    def update_content_once(self):
        return self.request("PATCH", API, {"content": SOURCE})


def validate_preflight(proof, now=None):
    """Validate the exact, credential-free evidence contract before any PATCH.

    The caller owns observing these facts via read-only commands. Unknown fields
    are rejected so arbitrary command output or credentials cannot be archived.
    """
    need(type(proof) is dict, "entry-proof-not-object")
    expected = {"v", "observed_at", "target_address", "pg_authority", "redis_authority",
                "runtime_endpoints", "persisted_endpoints", "application_ready",
                "source_containers", "source_entry"}
    need(set(proof) == expected, "entry-proof-fields-invalid")
    need(type(proof["v"]) is int and proof["v"] == 1, "entry-proof-version-invalid")
    observed = proof["observed_at"]
    need(type(observed) in (int, float), "entry-proof-time-invalid")
    age = (time.time() if now is None else now) - observed
    need(0 <= age <= MAX_PROOF_AGE, "entry-proof-stale-or-future")
    need(proof["target_address"] == TARGET and proof["pg_authority"] == "TENCENT" and
         proof["redis_authority"] == "TENCENT" and proof["application_ready"] is True,
         "entry-proof-target-authority-or-readiness-failed")
    endpoints = {"postgres": "target", "redis": "target"}
    need(proof["runtime_endpoints"] == endpoints and proof["persisted_endpoints"] == endpoints,
         "entry-proof-runtime-or-persisted-endpoints-failed")
    containers = proof["source_containers"]
    need(type(containers) is dict and set(containers) == set(SOURCE_CONTAINERS),
         "entry-proof-source-container-set-invalid")
    for value in containers.values():
        need(type(value) is dict and set(value) == {"running", "restart_policy"} and
             value["running"] is False and value["restart_policy"] == "no",
             "entry-proof-source-business-not-retired")
    entry = proof["source_entry"]
    need(type(entry) is dict and set(entry) == {"address", "domain", "https_status", "tls_verify_result",
         "bridge_target", "bridge_verified", "caddy_active", "relay_active", "channel_active"},
         "entry-proof-source-entry-fields-invalid")
    need(entry["address"] == SOURCE and entry["domain"] == NAME and
         type(entry["https_status"]) is int and entry["https_status"] == 200 and
         type(entry["tls_verify_result"]) is int and entry["tls_verify_result"] == 0 and
         entry["bridge_target"] == TARGET and
         all(entry[key] is True for key in ("bridge_verified", "caddy_active", "relay_active", "channel_active")),
         "entry-proof-source-entry-not-verified")
    # Copy out of the callback's mutable dictionary before evidence is recorded.
    return json.loads(canonical(proof))


def recover(client, folder, proof_callback):
    """Record proof, then perform at most one DNS PATCH; never retry a write.

    Caller must create a new private directory without exist_ok. Existing
    evidence is refused rather than adopted, resumed or overwritten.
    """
    folder = pathlib.Path(folder)
    no_symlinks(folder)
    need(folder.is_dir() and stat.S_IMODE(folder.stat().st_mode) == 0o700,
         "entry-evidence-directory-not-private")
    need(not any(folder.iterdir()), "entry-existing-evidence-refused")
    need(callable(proof_callback), "entry-reviewed-proof-callback-required")
    report = {"v": 1, "zone_id": ZONE, "record_id": RECORD, "name": NAME, "ok": False,
        "expected_current_entry": TARGET, "temporary_entry": SOURCE, "business_data_authority": "TENCENT",
        "temporary_aws_entry_dependency": True, "data_rollback": False, "ttl": TTL, "proxied": False,
        "mutation_attempted": False, "patch_count": 0, "automatic_retry": False,
        "conditional_api_write_proven": False,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    path = folder / "report.json"
    private_write(path, report)
    try:
        try:
            observed_proof = proof_callback()
        except Exception:
            raise Refusal("entry-proof-callback-failed-no-secret-output") from None
        proof = validate_preflight(observed_proof)
        listed = CF.validate(client.unique_record(), TARGET)
        before = CF.validate(client.record(), TARGET)
        need(sha(listed) == sha(before), "entry-dns-record-changed-between-list-and-read")
        snapshot = {"record": safe_record(before), "full_record_sha256": sha(before),
                    "preserved_attributes_sha256": preserved_digest(before), "preflight": proof,
                    "preflight_sha256": sha(proof)}
        private_write(folder / "before.json", snapshot)
        report.update(before=snapshot, before_evidence_sha256=sha(snapshot))
        private_write(path, report, replace=True)
        # This is a best-effort compare-before-write; Cloudflare supplies no
        # documented atomic If-Match contract for this fixed operation.
        current = CF.validate(client.record(), TARGET)
        need(sha(current) == sha(before), "entry-dns-compare-before-write-conflict")
        validate_preflight(proof)
        intent = {"v": 1, "method": "PATCH", "url": API, "body": {"content": SOURCE},
                  "before_evidence_sha256": sha(snapshot), "preflight_sha256": sha(proof),
                  "business_data_authority": "TENCENT", "data_rollback": False}
        private_write(folder / "intent.json", intent)
        report.update(mutation_attempted=True, patch_count=1, patch_body={"content": SOURCE},
                      write_intent_durable=True, intent_sha256=sha(intent))
        private_write(path, report, replace=True)
        patch_error = None
        try:
            client.update_content_once()
            report["patch_response_observed"] = True
        except Exception:
            # Never expose arbitrary exception payloads; they may contain tokens.
            patch_error = "entry-dns-write-result-unknown-no-retry"
            report.update(patch_response_observed=False, patch_response_error=patch_error)
        after = client.record()
        after_snapshot = {"record": safe_record(after), "full_record_sha256": sha(after),
                          "preserved_attributes_sha256": preserved_digest(after)}
        private_write(folder / "after.json", after_snapshot)
        report.update(after=after_snapshot, after_evidence_sha256=sha(after_snapshot))
        CF.validate(after, SOURCE)
        need(preserved_digest(after) == preserved_digest(before), "entry-dns-unrequested-record-attributes-changed")
        report.update(ok=True, source_entry_readback_confirmed=True, outcome="temporary-aws-entry-confirmed",
                      reconciled_lost_write_response=patch_error is not None)
    except Exception as exc:
        # All callback exceptions were masked at the boundary above.
        allowed = str(exc) if isinstance(exc, Refusal) else "entry-recovery-failed-no-secret-output"
        report["error"] = allowed if allowed.startswith(("entry-", "dns-", "cloudflare-", "hostname-", "fixed-")) else "entry-recovery-failed-no-secret-output"
        report["outcome"] = "write-outcome-needs-read-only-review" if report["mutation_attempted"] else "refused-before-write"
    report["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    private_write(path, report, replace=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply:
        raise Refusal("entry-reviewed-proof-callback-required-use-reviewed-orchestrator")
    print(json.dumps({"dry_run": True, "network_access": False, "dns_changed": False,
        "name": NAME, "expected_current_entry": TARGET, "temporary_entry": SOURCE,
        "business_data_authority": "TENCENT", "data_rollback": False,
        "only_patch_body": {"content": SOURCE}, "required_ttl": TTL, "required_proxied": False,
        "run_scope": str(RUN), "reviewed_proof_callback_required": True, "automatic_retry": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"ok": False, "error": "entry-reviewed-proof-callback-required-use-reviewed-orchestrator"}), file=sys.stderr)
        raise SystemExit(1)
