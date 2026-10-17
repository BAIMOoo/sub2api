#!/usr/bin/env python3
"""Offline by default; one fixed DNS change with a reviewed fresh proof callback.

The orchestrator owns live observations. This helper changes only api.b4im.com's
A content from the temporary AWS bridge to the independently reachable Tencent
entry. It does not drain connections or remove the old bridge.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import math
import pathlib
import stat
import sys
import time
import urllib.error
import urllib.request

_SPEC = importlib.util.spec_from_file_location("independent_entry_cloudflare",
    pathlib.Path(__file__).with_name("cloudflare_cutover.py"))
CF = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CF)
Refusal, need = CF.Refusal, CF.need
canonical, sha = CF.canonical, CF.sha
no_symlinks, private_write = CF.no_symlinks, CF.private_write
safe_record, preserved_digest = CF.safe_record, CF.preserved_digest
load_private_token, NoRedirect = CF.load_private_token, CF.NoRedirect
ZONE, RECORD, NAME, SOURCE, TTL = CF.ZONE, CF.RECORD, CF.NAME, CF.SOURCE, CF.TTL
API, LIST_API = CF.API, CF.LIST_API
TARGET = "43.165.182.52"
MAX_AGE = 120


class Client(CF.Client):
    """GET fixed DNS resources, or PATCH only the new Tencent address."""

    def request(self, method, url, body=None):
        need((method == "GET" and url in {API, LIST_API} and body is None) or
             (method == "PATCH" and url == API and body == {"content": TARGET}),
             "independent-dns-operation-outside-fixed-scope")
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
        return self.request("PATCH", API, {"content": TARGET})


def validate_preflight(proof, now=None):
    """Accept only fixed, credential-free assertions; observed_at is UTC epoch."""
    expected = {"v", "observed_at", "target_address", "pg_authority", "redis_authority",
                "runtime_endpoints", "persisted_endpoints", "application_ready",
                "domestic_entry", "web_bootstrap_passed", "stream_canary_passed"}
    need(type(proof) is dict and set(proof) == expected, "independent-proof-fields-invalid")
    need(type(proof["v"]) is int and proof["v"] == 1, "independent-proof-version-invalid")
    observed = proof["observed_at"]
    need(type(observed) in (int, float) and math.isfinite(observed), "independent-proof-time-invalid")
    age = (time.time() if now is None else now) - observed
    need(0 <= age <= MAX_AGE, "independent-proof-stale-or-future")
    need(proof["target_address"] == TARGET and proof["pg_authority"] == "TENCENT" and
         proof["redis_authority"] == "TENCENT" and proof["application_ready"] is True,
         "independent-proof-authority-or-readiness-failed")
    endpoints = {"postgres": "target", "redis": "target"}
    need(proof["runtime_endpoints"] == endpoints and proof["persisted_endpoints"] == endpoints,
         "independent-proof-runtime-or-persisted-endpoints-failed")
    entry = proof["domestic_entry"]
    expected_entry = {"address": TARGET, "domain": NAME, "https_status": 200,
                      "tls_verify_result": 0, "no_proxy": True, "pinned_ip": True}
    need(type(entry) is dict and entry == expected_entry and type(entry["https_status"]) is int and
         type(entry["tls_verify_result"]) is int and entry["no_proxy"] is True and entry["pinned_ip"] is True,
         "independent-proof-domestic-tls-health-failed")
    need(proof["web_bootstrap_passed"] is True and proof["stream_canary_passed"] is True,
         "independent-proof-web-or-stream-canary-failed")
    return json.loads(canonical(proof))


def validate_record(row, address):
    CF.validate(row, address)
    # Only strict timestamps and the existing fixed DNS fields enter receipts.
    # Unknown record attributes are preserved through a digest, never printed.
    modified = row["modified_on"]
    need(20 <= len(modified) <= 35, "independent-dns-record-time-invalid")
    try:
        stamp = datetime.datetime.fromisoformat(modified.replace("Z", "+00:00"))
    except ValueError:
        raise Refusal("independent-dns-record-time-invalid") from None
    need(stamp.tzinfo is not None, "independent-dns-record-time-invalid")
    return row


def transaction(client, folder, proof_callback):
    """Use a new empty 0700 directory; durable intent permits at most one PATCH.

    A lost PATCH response is reconciled by GET only. No rollback or write retry
    is attempted. Cloudflare provides no documented atomic compare-and-set here.
    Every exception is represented by a fixed stage code, never exception text.
    """
    folder = pathlib.Path(folder)
    no_symlinks(folder)
    need(folder.is_dir() and stat.S_IMODE(folder.stat().st_mode) == 0o700,
         "independent-dns-evidence-directory-not-private")
    need(not any(folder.iterdir()), "independent-dns-existing-evidence-refused")
    need(callable(proof_callback), "independent-dns-reviewed-proof-callback-required")
    report = {"v": 1, "zone_id": ZONE, "record_id": RECORD, "name": NAME,
        "expected_source": SOURCE, "target": TARGET, "ttl": TTL, "proxied": False,
        "business_data_authority": "TENCENT", "ok": False, "mutation_attempted": False,
        "patch_count": 0, "automatic_retry": False, "conditional_api_write_proven": False,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    path = folder / "report.json"
    private_write(path, report)
    stage = "preflight"
    try:
        proof = validate_preflight(proof_callback())
        stage = "initial-dns-read"
        listed = validate_record(client.unique_record(), SOURCE)
        before = validate_record(client.record(), SOURCE)
        need(sha(listed) == sha(before), "independent-dns-list-read-conflict")
        snapshot = {"record": safe_record(before), "full_record_sha256": sha(before),
            "preserved_attributes_sha256": preserved_digest(before), "preflight": proof,
            "preflight_sha256": sha(proof)}
        private_write(folder / "before.json", snapshot)
        report.update(before=snapshot, before_evidence_sha256=sha(snapshot))
        private_write(path, report, replace=True)
        stage = "compare-before-write"
        current = validate_record(client.record(), SOURCE)
        need(sha(current) == sha(before), "independent-dns-compare-before-write-conflict")
        validate_preflight(proof)
        intent = {"v": 1, "method": "PATCH", "url": API, "body": {"content": TARGET},
            "before_evidence_sha256": sha(snapshot), "preflight_sha256": sha(proof)}
        private_write(folder / "intent.json", intent)
        report.update(mutation_attempted=True, patch_count=1, write_intent_durable=True,
                      intent_sha256=sha(intent), patch_body={"content": TARGET})
        private_write(path, report, replace=True)
        try:
            client.update_content_once()
            report["patch_response_observed"] = True
        except Exception:
            report["patch_response_observed"] = False
        stage = "write-readback"
        after = validate_record(client.record(), TARGET)
        after_snapshot = {"record": safe_record(after), "full_record_sha256": sha(after),
            "preserved_attributes_sha256": preserved_digest(after)}
        private_write(folder / "after.json", after_snapshot)
        report.update(after=after_snapshot, after_evidence_sha256=sha(after_snapshot))
        need(preserved_digest(after) == preserved_digest(before), "independent-dns-unrequested-attributes-changed")
        report.update(ok=True, outcome="independent-tencent-entry-confirmed", target_readback_confirmed=True,
                      reconciled_lost_write_response=not report["patch_response_observed"])
    except Exception:
        report.update(error="independent-dns-" + stage + "-failed-no-secret-output",
            outcome="write-outcome-needs-read-only-review" if report["mutation_attempted"] else "refused-before-write")
    report["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    private_write(path, report, replace=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply:
        raise Refusal("independent-dns-reviewed-proof-callback-required-use-reviewed-orchestrator")
    print(json.dumps({"dry_run": True, "network_access": False, "dns_changed": False,
        "name": NAME, "source": SOURCE, "target": TARGET, "required_ttl": TTL,
        "required_proxied": False, "only_patch_body": {"content": TARGET},
        "reviewed_proof_callback_required": True, "automatic_retry": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"ok": False, "error": "independent-dns-operation-refused-no-secret-output"}), file=sys.stderr)
        raise SystemExit(1)
