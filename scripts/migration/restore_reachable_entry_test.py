#!/usr/bin/env python3
"""Offline entry-only recovery checks; no credentials or production access."""
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

PATH = pathlib.Path(__file__).with_name("restore_reachable_entry.py")
SPEC = importlib.util.spec_from_file_location("restore_reachable_entry", PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def proof():
    return {"v": 1, "observed_at": time.time(), "target_address": M.TARGET,
        "pg_authority": "TENCENT", "redis_authority": "TENCENT", "application_ready": True,
        "runtime_endpoints": {"postgres": "target", "redis": "target"},
        "persisted_endpoints": {"postgres": "target", "redis": "target"},
        "source_containers": {name: {"running": False, "restart_policy": "no"} for name in M.SOURCE_CONTAINERS},
        "source_entry": {"address": M.SOURCE, "domain": M.NAME, "https_status": 200, "tls_verify_result": 0,
            "bridge_target": M.TARGET, "bridge_verified": True, "caddy_active": True,
            "relay_active": True, "channel_active": True}}


def record():
    return {"id": M.RECORD, "name": M.NAME, "type": "A", "content": M.TARGET, "ttl": 60,
        "proxied": False, "modified_on": "2026-10-17T00:00:00Z", "created_on": "2026-01-01T00:00:00Z",
        "settings": {}, "comment": None, "tags": []}


class FakeClient:
    def __init__(self, folder):
        self.folder = folder
        self.live = record()
        self.reads = self.patches = 0
        self.change_before = self.lost_response = self.alter_other = self.readback_fail = False

    def unique_record(self):
        return copy.deepcopy(self.live)

    def record(self):
        self.reads += 1
        if self.readback_fail and self.patches:
            raise M.Refusal("cloudflare-api-result-unavailable-no-retry")
        result = copy.deepcopy(self.live)
        if self.change_before and self.reads == 2:
            result["modified_on"] = "2026-10-17T00:00:01Z"
        return result

    def update_content_once(self):
        report = json.loads((self.folder / "report.json").read_text())
        assert report["mutation_attempted"] and report["patch_count"] == 1 and report["write_intent_durable"]
        assert json.loads((self.folder / "intent.json").read_text())["body"] == {"content": M.SOURCE}
        assert json.loads((self.folder / "before.json").read_text())["preflight"]["pg_authority"] == "TENCENT"
        self.patches += 1
        self.live.update(content=M.SOURCE, modified_on="2026-10-17T00:00:02Z")
        if self.alter_other:
            self.live["comment"] = "changed"
        if self.lost_response:
            raise RuntimeError("secret-never-report")
        return {"success": True}


class RecoveryTests(unittest.TestCase):
    def run_recovery(self, configure=None, callback=proof):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary).resolve()
            folder.chmod(0o700)
            client = FakeClient(folder)
            if configure:
                configure(client)
            result = M.recover(client, folder, callback)
            saved = {item.name: json.loads(item.read_text()) for item in folder.iterdir()}
            for item in folder.iterdir():
                self.assertEqual(item.stat().st_mode & 0o777, 0o600)
            return result, client, saved

    def test_default_offline_and_apply_requires_reviewed_orchestration(self):
        with mock.patch.object(M, "load_private_token", side_effect=AssertionError("credential")), \
             mock.patch.object(M, "Client", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
            self.assertFalse(json.loads(out.getvalue())["dns_changed"])
            with self.assertRaisesRegex(M.Refusal, "proof-callback-required"):
                M.main(["--apply"])

    def test_success_one_patch_preserves_all_attributes_and_tencent_authority(self):
        result, client, saved = self.run_recovery()
        self.assertTrue(result["ok"])
        self.assertEqual(client.patches, 1)
        self.assertEqual(result["after"]["record"]["content"], M.SOURCE)
        self.assertEqual(result["before"]["preserved_attributes_sha256"], result["after"]["preserved_attributes_sha256"])
        self.assertEqual(result["business_data_authority"], "TENCENT")
        self.assertTrue(result["temporary_aws_entry_dependency"])
        self.assertFalse(result["data_rollback"])
        self.assertEqual(set(saved), {"report.json", "before.json", "intent.json", "after.json"})

    def test_old_evidence_refused_without_overwrite_or_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary).resolve()
            folder.chmod(0o700)
            prior = folder / "report.json"
            prior.write_text("historical-evidence")
            client = FakeClient(folder)
            with self.assertRaisesRegex(M.Refusal, "existing-evidence"):
                M.recover(client, folder, proof)
            self.assertEqual(prior.read_text(), "historical-evidence")
            self.assertEqual(client.patches, 0)

    def test_failed_proof_refuses_without_cloudflare_reads_or_patch(self):
        value = proof()
        value["pg_authority"] = "AWS"
        result, client, saved = self.run_recovery(callback=lambda: value)
        self.assertFalse(result["ok"])
        self.assertEqual(client.reads, 0)
        self.assertEqual(client.patches, 0)
        self.assertEqual(set(saved), {"report.json"})

    def test_callback_exception_is_masked_even_if_refusal_has_secret(self):
        def rejected():
            raise M.Refusal("entry-secret-never-report")
        result, client, saved = self.run_recovery(callback=rejected)
        self.assertFalse(result["ok"])
        self.assertEqual(client.patches, 0)
        self.assertNotIn("secret-never-report", json.dumps(saved))

    def test_each_required_preflight_assertion_refuses(self):
        mutations = [lambda p: p.update(redis_authority="AWS"), lambda p: p.update(application_ready=False),
            lambda p: p.update(observed_at=time.time() - 121), lambda p: p.update(observed_at=time.time() + 30),
            lambda p: p["persisted_endpoints"].update(postgres="source"),
            lambda p: p["runtime_endpoints"].update(redis="source"),
            lambda p: p["source_containers"]["sub2api"].update(running=True),
            lambda p: p["source_containers"]["sub2api-postgres"].update(restart_policy="always"),
            lambda p: p["source_entry"].update(tls_verify_result=1),
            lambda p: p["source_entry"].update(bridge_target=M.SOURCE),
            lambda p: p["source_entry"].update(channel_active=False),
            lambda p: p.update(secret="must-not-archive")]
        for mutation in mutations:
            value = proof()
            mutation(value)
            with self.assertRaises(M.Refusal):
                M.validate_preflight(value)

    def test_compare_before_write_conflict_has_zero_patches(self):
        result, client, saved = self.run_recovery(lambda c: setattr(c, "change_before", True))
        self.assertFalse(result["ok"])
        self.assertEqual(client.patches, 0)
        self.assertNotIn("intent.json", saved)

    def test_preflight_expiring_during_dns_reads_refuses_before_intent(self):
        value = proof()
        now = value["observed_at"]
        with mock.patch.object(M.time, "time", side_effect=[now, now + 121]):
            result, client, saved = self.run_recovery(callback=lambda: value)
        self.assertFalse(result["ok"])
        self.assertEqual(client.patches, 0)
        self.assertNotIn("intent.json", saved)

    def test_wrong_current_ip_ttl_or_proxy_status_refuses_before_patch(self):
        for change in [{"content": M.SOURCE}, {"ttl": 300}, {"proxied": True}]:
            result, client, _ = self.run_recovery(lambda c: c.live.update(change))
            self.assertFalse(result["ok"])
            self.assertEqual(client.patches, 0)

    def test_lost_response_readback_confirms_without_repeating_patch(self):
        result, client, saved = self.run_recovery(lambda c: setattr(c, "lost_response", True))
        self.assertTrue(result["ok"])
        self.assertTrue(result["reconciled_lost_write_response"])
        self.assertEqual(client.patches, 1)
        self.assertNotIn("secret-never-report", json.dumps(saved))

    def test_other_attribute_change_fails_after_one_patch(self):
        result, client, _ = self.run_recovery(lambda c: setattr(c, "alter_other", True))
        self.assertFalse(result["ok"])
        self.assertIn("unrequested-record-attributes", result["error"])
        self.assertEqual(client.patches, 1)

    def test_lost_readback_never_retries_write(self):
        result, client, _ = self.run_recovery(lambda c: setattr(c, "readback_fail", True))
        self.assertFalse(result["ok"])
        self.assertEqual(client.patches, 1)
        self.assertEqual(result["outcome"], "write-outcome-needs-read-only-review")

    def test_http_scope_only_accepts_fixed_source_content_patch(self):
        with mock.patch.object(M.urllib.request, "build_opener") as opener:
            client = M.Client("private-token")
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'{"success":true,"result":{}}'
            client.update_content_once()
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, M.API)
            self.assertEqual(json.loads(request.data), {"content": M.SOURCE})
            opener.return_value.open.reset_mock()
            for method, url, body in [("PATCH", M.API, {"content": M.TARGET}),
                ("PATCH", M.API, {"content": M.SOURCE, "ttl": 60}), ("PUT", M.API, {"content": M.SOURCE}),
                ("PATCH", M.LIST_API, {"content": M.SOURCE}), ("GET", "https://other.example", None)]:
                with self.assertRaises(M.Refusal):
                    client.request(method, url, body)
            opener.return_value.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
