#!/usr/bin/env python3
"""Stage/review by default; --apply changes only the fixed API A-record content.

This file has not been authorized to run --apply during preparation. It never
changes proxy status, TTL, other DNS names, application processes or data roles.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import sys
import urllib.error
import urllib.request
import uuid

ZONE = "ee0df4c2b558c519b22bc2dddddca611"
RECORD = "e14e7e7d735bb542e19364a36e24199c"
NAME = "api.b4im.com"
SOURCE = "13.231.143.136"
TARGET = "43.165.177.136"
TTL = 60
API = "https://api.cloudflare.com/client/v4/zones/" + ZONE + "/dns_records/" + RECORD
LIST_API = "https://api.cloudflare.com/client/v4/zones/" + ZONE + "/dns_records?name=" + NAME + "&per_page=100"
CREDENTIAL = pathlib.Path("/Users/mima0000/.config/sub2api-migration/cloudflare.env")
FIELDS = ("id", "name", "type", "content", "ttl", "proxied", "modified_on")


class Refusal(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def no_symlinks(path):
    need(not any(p.is_symlink() for p in [path, *path.parents]), "dns-evidence-symlink-refused")


def private_write(path, value, *, replace=False):
    no_symlinks(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp") if replace else path
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(canonical(value) + b"\n"); out.flush(); os.fsync(out.fileno())
    if replace:
        os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_private_token():
    # Reuse only the reviewed credential parser; do not invoke inspect.main().
    path = pathlib.Path(__file__).with_name("cloudflare_inspect.py")
    spec = importlib.util.spec_from_file_location("cloudflare_cutover_private", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.load_token(CREDENTIAL)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refusal("cloudflare-api-redirect-refused")


class Client:
    def __init__(self, token):
        self._token = token
        self._opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, url, body=None):
        need((method == "GET" and url in {API, LIST_API} and body is None) or
             (method == "PATCH" and url == API and body == {"content": TARGET}), "dns-operation-outside-fixed-scope")
        request = urllib.request.Request(url, method=method, data=None if body is None else canonical(body),
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

    def unique_record(self):
        value = self.request("GET", LIST_API)
        rows = value.get("result")
        need(isinstance(rows, list) and len(rows) == 1 and value.get("result_info", {}).get("total_count") == 1,
             "hostname-does-not-have-exactly-one-record")
        return rows[0]

    def record(self):
        value = self.request("GET", API).get("result")
        need(isinstance(value, dict), "fixed-record-result-invalid")
        return value

    def update_content_once(self):
        return self.request("PATCH", API, {"content": TARGET})


def validate(row, address):
    need(isinstance(row, dict), "dns-record-not-object")
    expected = {"id": RECORD, "name": NAME, "type": "A", "content": address, "ttl": TTL}
    need(all(row.get(k) == v for k, v in expected.items()) and type(row.get("ttl")) is int and
         row.get("proxied") is False and isinstance(row.get("modified_on"), str) and row["modified_on"],
         "dns-fixed-record-precondition-or-readback-mismatch")
    return row


def safe_record(row):
    return {key: row.get(key) for key in FIELDS}


def preserved_digest(row):
    return sha({key: value for key, value in row.items() if key not in {"content", "modified_on"}})


def cutover(client, folder):
    no_symlinks(folder)
    need(folder.is_dir() and stat.S_IMODE(folder.stat().st_mode) == 0o700, "dns-evidence-directory-not-private")
    report = {"v": 1, "zone_id": ZONE, "record_id": RECORD, "name": NAME, "ok": False,
        "expected_source": SOURCE, "target": TARGET, "ttl": TTL, "proxied": False,
        "mutation_attempted": False, "patch_count": 0, "automatic_retry": False,
        "conditional_api_write_proven": False,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    path = folder / "report.json"
    private_write(path, report)
    try:
        listed = validate(client.unique_record(), SOURCE)
        before = validate(client.record(), SOURCE)
        need(sha(listed) == sha(before), "dns-record-changed-between-list-and-read")
        snapshot = {"record": safe_record(before), "full_record_sha256": sha(before),
                    "preserved_attributes_sha256": preserved_digest(before)}
        private_write(folder / "before.json", snapshot)
        report.update(before=snapshot, before_evidence_sha256=sha(snapshot))
        private_write(path, report, replace=True)
        # Re-read immediately before recording intent. Cloudflare does not
        # document an If-Match CAS for this endpoint; never claim atomicity.
        current = validate(client.record(), SOURCE)
        need(sha(current) == sha(before), "dns-compare-before-write-conflict")
        report.update(mutation_attempted=True, patch_count=1, patch_body={"content": TARGET},
                      write_intent_durable=True)
        private_write(path, report, replace=True)
        patch_error = None
        try:
            client.update_content_once()
            report["patch_response_observed"] = True
        except Exception as exc:
            patch_error = str(exc) if isinstance(exc, Refusal) else "dns-write-result-unknown-no-retry"
            report.update(patch_response_observed=False, patch_response_error=patch_error)
        # An absent receipt is reconciled with a read; PATCH is never repeated.
        after = client.record()
        after_snapshot = {"record": safe_record(after), "full_record_sha256": sha(after),
                          "preserved_attributes_sha256": preserved_digest(after)}
        private_write(folder / "after.json", after_snapshot)
        report.update(after=after_snapshot, after_evidence_sha256=sha(after_snapshot))
        validate(after, TARGET)
        need(preserved_digest(after) == preserved_digest(before), "dns-unrequested-record-attributes-changed")
        report.update(ok=True, target_readback_confirmed=True, outcome="target-confirmed",
                      reconciled_lost_write_response=patch_error is not None)
    except Exception as exc:
        report["error"] = str(exc) if isinstance(exc, Refusal) else "dns-cutover-failed-no-secret-output"
        report["outcome"] = "write-outcome-needs-read-only-review" if report["mutation_attempted"] else "refused-before-write"
    report["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    private_write(path, report, replace=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"dry_run": True, "network_access": False, "dns_changed": False, "zone_id": ZONE,
            "record_id": RECORD, "name": NAME, "type": "A", "source": SOURCE, "target": TARGET,
            "required_ttl": TTL, "required_proxied": False, "only_patch_body": {"content": TARGET},
            "no_atomic_conditional_api_write_claim": True, "automatic_retry": False}))
        return 0
    token = load_private_token()
    folder = pathlib.Path.home() / ".codex/backups" / ("sub2api-cloudflare-cutover-" + uuid.uuid4().hex[:16])
    no_symlinks(folder); folder.mkdir(mode=0o700, parents=True)
    report = cutover(Client(token), folder)
    need(token not in canonical(report).decode(), "dns-secret-in-evidence-refused")
    print(json.dumps({**report, "local_evidence": str(folder), "report_sha256": sha(report)}, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refusal) else "dns-operation-failed-no-secret-output"}), file=sys.stderr)
        raise SystemExit(1)
