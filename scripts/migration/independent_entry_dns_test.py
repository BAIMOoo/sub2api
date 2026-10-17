#!/usr/bin/env python3
"""Offline, fixed-scope DNS transaction checks; no production access."""
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("independent_entry_dns",
    pathlib.Path(__file__).with_name("independent_entry_dns.py"))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def proof():
    return {"v": 1, "observed_at": time.time(), "target_address": M.TARGET,
        "pg_authority": "TENCENT", "redis_authority": "TENCENT", "application_ready": True,
        "runtime_endpoints": {"postgres": "target", "redis": "target"},
        "persisted_endpoints": {"postgres": "target", "redis": "target"},
        "domestic_entry": {"address": M.TARGET, "domain": M.NAME, "https_status": 200,
            "tls_verify_result": 0, "no_proxy": True, "pinned_ip": True},
        "web_bootstrap_passed": True, "stream_canary_passed": True}


def record():
    return {"id": M.RECORD, "name": M.NAME, "type": "A", "content": M.SOURCE,
        "ttl": M.TTL, "proxied": False, "modified_on": "2026-10-17T00:00:00Z",
        "created_on": "2026-01-01T00:00:00Z", "settings": {}, "comment": None, "tags": []}


class FakeClient:
    def __init__(self, folder):
        self.folder, self.live = folder, record()
        self.reads = self.patches = self.list_reads = 0
        self.conflict = self.lost_response = self.alter_other = self.readback_fail = False
        self.reject_list = self.write_not_applied = False

    def unique_record(self):
        self.list_reads += 1
        if self.reject_list:
            raise M.Refusal("hostname-does-not-have-exactly-one-record")
        return copy.deepcopy(self.live)

    def record(self):
        self.reads += 1
        if self.readback_fail and self.patches:
            raise RuntimeError("secret-never-report")
        row = copy.deepcopy(self.live)
        if self.conflict and self.reads == 2:
            row["modified_on"] = "2026-10-17T00:00:01Z"
        return row

    def update_content_once(self):
        report = json.loads((self.folder / "report.json").read_text())
        assert report["mutation_attempted"] and report["patch_count"] == 1 and report["write_intent_durable"]
        assert json.loads((self.folder / "intent.json").read_text())["body"] == {"content": M.TARGET}
        assert json.loads((self.folder / "before.json").read_text())["preflight"]["pg_authority"] == "TENCENT"
        self.patches += 1
        if not self.write_not_applied:
            self.live.update(content=M.TARGET, modified_on="2026-10-17T00:00:02Z")
        if self.alter_other:
            self.live["comment"] = "secret-never-report"
        if self.lost_response:
            raise RuntimeError("secret-never-report")
        return {"success": True}


class IndependentEntryTests(unittest.TestCase):
    def run_transaction(self, configure=None, callback=proof):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary).resolve()
            folder.chmod(0o700)
            client = FakeClient(folder)
            if configure:
                configure(client)
            report = M.transaction(client, folder, callback)
            saved = {item.name: json.loads(item.read_text()) for item in folder.iterdir()}
            for item in folder.iterdir():
                self.assertEqual(item.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("secret-never-report", json.dumps(saved))
            return report, client, saved

    def test_default_offline_and_apply_refuses_without_orchestrator(self):
        with mock.patch.object(M, "load_private_token", side_effect=AssertionError("credential")), \
             mock.patch.object(M, "Client", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
            self.assertFalse(json.loads(out.getvalue())["dns_changed"])
            with self.assertRaisesRegex(M.Refusal, "proof-callback-required"):
                M.main(["--apply"])

    def test_fresh_proof_one_patch_and_preserved_attributes(self):
        report, client, saved = self.run_transaction()
        self.assertTrue(report["ok"])
        self.assertEqual(client.patches, 1)
        self.assertEqual(report["after"]["record"]["content"], M.TARGET)
        self.assertEqual(report["before"]["preserved_attributes_sha256"], report["after"]["preserved_attributes_sha256"])
        self.assertEqual(set(saved), {"report.json", "before.json", "intent.json", "after.json"})

    def test_explicit_clock_fresh_boundary_stale_future_and_invalid_times(self):
        value = proof()
        value["observed_at"] = 1000
        self.assertEqual(M.validate_preflight(value, now=1120), value)
        for now in (1120.001, 999):
            with self.assertRaises(M.Refusal):
                M.validate_preflight(value, now=now)
        for observed in (float("nan"), float("inf"), True, "1000"):
            value["observed_at"] = observed
            with self.assertRaises(M.Refusal):
                M.validate_preflight(value, now=1000)

    def test_invalid_or_stale_proof_has_no_dns_access(self):
        mutations = [lambda p: p.update(pg_authority="AWS"), lambda p: p.update(redis_authority="AWS"),
            lambda p: p.update(application_ready=False), lambda p: p.update(observed_at=time.time() - 121),
            lambda p: p.update(target_address=M.SOURCE), lambda p: p["runtime_endpoints"].update(redis="source"),
            lambda p: p["persisted_endpoints"].update(postgres="source"),
            lambda p: p["domestic_entry"].update(tls_verify_result=1),
            lambda p: p["domestic_entry"].update(no_proxy=False), lambda p: p["domestic_entry"].update(pinned_ip=False),
            lambda p: p["domestic_entry"].update(https_status=503),
            lambda p: p.update(web_bootstrap_passed=False), lambda p: p.update(stream_canary_passed=False),
            lambda p: p.update(secret="secret-never-report")]
        for mutation in mutations:
            value = proof()
            mutation(value)
            report, client, saved = self.run_transaction(callback=lambda: value)
            self.assertFalse(report["ok"])
            self.assertEqual((client.reads, client.list_reads, client.patches), (0, 0, 0))
            self.assertEqual(set(saved), {"report.json"})

    def test_secret_callback_exception_is_masked(self):
        def callback():
            raise M.Refusal("independent-secret-never-report")
        report, client, _ = self.run_transaction(callback=callback)
        self.assertFalse(report["ok"])
        self.assertEqual(client.patches, 0)

    def test_proof_expiring_during_dns_checks_refuses_before_intent(self):
        value = proof()
        now = value["observed_at"]
        with mock.patch.object(M.time, "time", side_effect=[now, now + 121]):
            report, client, saved = self.run_transaction(callback=lambda: value)
        self.assertFalse(report["ok"])
        self.assertEqual(client.patches, 0)
        self.assertNotIn("intent.json", saved)

    def test_wrong_current_dns_data_refuses_before_patch(self):
        for change in ({"content": M.TARGET}, {"type": "AAAA"}, {"ttl": 300}, {"proxied": True},
                       {"id": "other-record"}, {"modified_on": "secret-never-report"}):
            report, client, _ = self.run_transaction(lambda c: c.live.update(change))
            self.assertFalse(report["ok"])
            self.assertEqual(client.patches, 0)

    def test_extra_hostname_record_refuses_before_patch(self):
        report, client, _ = self.run_transaction(lambda c: setattr(c, "reject_list", True))
        self.assertFalse(report["ok"])
        self.assertEqual(client.patches, 0)

    def test_compare_before_write_conflict_refuses_without_intent(self):
        report, client, saved = self.run_transaction(lambda c: setattr(c, "conflict", True))
        self.assertFalse(report["ok"])
        self.assertEqual(client.patches, 0)
        self.assertNotIn("intent.json", saved)

    def test_lost_write_response_uses_readback_without_retry(self):
        report, client, _ = self.run_transaction(lambda c: setattr(c, "lost_response", True))
        self.assertTrue(report["ok"])
        self.assertTrue(report["reconciled_lost_write_response"])
        self.assertEqual(client.patches, 1)

    def test_lost_response_without_mutation_does_not_retry(self):
        def configure(client):
            client.lost_response = client.write_not_applied = True
        report, client, _ = self.run_transaction(configure)
        self.assertFalse(report["ok"])
        self.assertEqual(client.patches, 1)
        self.assertEqual(report["outcome"], "write-outcome-needs-read-only-review")

    def test_changed_unrequested_attribute_refuses_readback(self):
        report, client, _ = self.run_transaction(lambda c: setattr(c, "alter_other", True))
        self.assertFalse(report["ok"])
        self.assertEqual(client.patches, 1)

    def test_failed_readback_does_not_retry_write(self):
        report, client, _ = self.run_transaction(lambda c: setattr(c, "readback_fail", True))
        self.assertFalse(report["ok"])
        self.assertEqual(client.patches, 1)

    def test_existing_evidence_and_repeated_transaction_refuse_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary).resolve()
            folder.chmod(0o700)
            client = FakeClient(folder)
            self.assertTrue(M.transaction(client, folder, proof)["ok"])
            original = (folder / "report.json").read_bytes()
            with self.assertRaisesRegex(M.Refusal, "existing-evidence"):
                M.transaction(client, folder, proof)
            self.assertEqual((folder / "report.json").read_bytes(), original)
            self.assertEqual(client.patches, 1)

    def test_nonprivate_and_symlink_evidence_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary).resolve()
            folder.chmod(0o755)
            with self.assertRaisesRegex(M.Refusal, "directory-not-private"):
                M.transaction(FakeClient(folder), folder, proof)
            folder.chmod(0o700)
            alias = folder / "alias"
            alias.symlink_to(folder, target_is_directory=True)
            with self.assertRaisesRegex(M.Refusal, "symlink"):
                M.transaction(FakeClient(alias), alias, proof)

    def test_http_client_only_allows_fixed_dns_gets_and_exact_content_patch(self):
        with mock.patch.object(M.urllib.request, "build_opener") as opener:
            client = M.Client("private-token")
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'{"success":true,"result":{}}'
            client.update_content_once()
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, M.API)
            self.assertEqual(json.loads(request.data), {"content": M.TARGET})
            opener.return_value.open.reset_mock()
            for method, url, body in [("PATCH", M.API, {"content": M.SOURCE}),
                ("PATCH", M.API, {"content": M.TARGET, "ttl": 60}), ("PUT", M.API, {"content": M.TARGET}),
                ("PATCH", M.LIST_API, {"content": M.TARGET}), ("GET", "https://other.example", None),
                ("GET", M.API, {"content": M.TARGET})]:
                with self.assertRaises(M.Refusal):
                    client.request(method, url, body)
            opener.return_value.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
