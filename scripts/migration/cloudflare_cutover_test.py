#!/usr/bin/env python3
"""Narrow offline DNS update tests; no Cloudflare credential or network use."""
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

PATH = pathlib.Path(__file__).with_name("cloudflare_cutover.py")
SPEC = importlib.util.spec_from_file_location("cloudflare_cutover", PATH)
M = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)


def record(address=M.SOURCE):
    return {"id": M.RECORD, "name": M.NAME, "type": "A", "content": address, "ttl": 60,
        "proxied": False, "modified_on": "2026-10-16T00:00:00Z", "created_on": "2026-01-01T00:00:00Z",
        "settings": {}, "comment": None, "tags": []}


class FakeClient:
    def __init__(self, folder):
        self.folder = folder; self.live = record(); self.reads = 0; self.patches = 0
        self.change_before_write = False; self.unknown_write = False; self.alter_ttl = False

    def unique_record(self):
        return copy.deepcopy(self.live)

    def record(self):
        self.reads += 1
        value = copy.deepcopy(self.live)
        if self.change_before_write and self.reads == 2:
            value["modified_on"] = "2026-10-16T00:01:00Z"
        return value

    def update_content_once(self):
        evidence = json.loads((self.folder / "report.json").read_text())
        assert evidence["write_intent_durable"] and evidence["mutation_attempted"] and evidence["patch_count"] == 1
        assert (self.folder / "before.json").stat().st_mode & 0o777 == 0o600
        self.patches += 1; self.live["content"] = M.TARGET; self.live["modified_on"] = "2026-10-16T00:02:00Z"
        if self.alter_ttl:
            self.live["ttl"] = 300
        if self.unknown_write:
            raise M.Refusal("dns-write-result-unknown-no-retry")
        return {"success": True, "result": self.live}


class CloudflareTests(unittest.TestCase):
    def test_default_does_not_read_credentials_create_files_or_access_network(self):
        with mock.patch.object(M, "load_private_token", side_effect=AssertionError("credential")), \
             mock.patch.object(M, "Client", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
        self.assertFalse(json.loads(out.getvalue())["dns_changed"])

    def test_only_fixed_source_A_record_with_proxy_off_and_TTL60_is_accepted(self):
        M.validate(record(), M.SOURCE)
        for change in [{"id": "another"}, {"name": "other.b4im.com"}, {"type": "AAAA"},
            {"content": M.TARGET}, {"ttl": 1}, {"ttl": True}, {"proxied": True}, {"proxied": 0}, {"modified_on": None}]:
            with self.assertRaises(M.Refusal):
                M.validate({**record(), **change}, M.SOURCE)

    def test_success_changes_content_once_and_persists_private_before_intent_after(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary).resolve(); folder.chmod(0o700)
            client = FakeClient(folder); result = M.cutover(client, folder)
            self.assertTrue(result["ok"]); self.assertEqual(client.patches, 1)
            self.assertEqual(result["after"]["record"]["content"], M.TARGET)
            self.assertEqual(result["before"]["preserved_attributes_sha256"], result["after"]["preserved_attributes_sha256"])
            self.assertFalse(result["conditional_api_write_proven"])
            for name in ["before.json", "after.json", "report.json"]:
                self.assertEqual((folder / name).stat().st_mode & 0o777, 0o600)

    def test_concurrent_change_before_write_refuses_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary).resolve(); folder.chmod(0o700)
            client = FakeClient(folder); client.change_before_write = True
            result = M.cutover(client, folder)
            self.assertFalse(result["ok"]); self.assertEqual(client.patches, 0)
            self.assertEqual(result["outcome"], "refused-before-write")

    def test_lost_write_response_is_read_back_without_a_second_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary).resolve(); folder.chmod(0o700)
            client = FakeClient(folder); client.unknown_write = True
            result = M.cutover(client, folder)
            self.assertTrue(result["ok"]); self.assertTrue(result["reconciled_lost_write_response"])
            self.assertEqual(client.patches, 1)

    def test_any_unrequested_attribute_change_or_failed_readback_never_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary).resolve(); folder.chmod(0o700)
            client = FakeClient(folder); client.alter_ttl = True
            result = M.cutover(client, folder)
            self.assertFalse(result["ok"]); self.assertEqual(client.patches, 1)
            self.assertEqual(result["outcome"], "write-outcome-needs-read-only-review")

    def test_fixed_http_scope_forbids_other_names_methods_fields_or_addresses(self):
        with mock.patch.object(M.urllib.request, "build_opener") as opener:
            client = M.Client("private-token")
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'{"success":true,"result":{}}'
            client.update_content_once()
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, M.API); self.assertEqual(request.method, "PATCH")
            self.assertEqual(json.loads(request.data), {"content": M.TARGET})
            opener.return_value.open.reset_mock()
            for method, url, body in [("PUT", M.API, {"content": M.TARGET}), ("PATCH", M.LIST_API, {"content": M.TARGET}),
                ("PATCH", M.API, {"content": M.TARGET, "ttl": 60}), ("PATCH", M.API, {"content": M.SOURCE}),
                ("GET", "https://other.example", None)]:
                with self.assertRaises(M.Refusal):
                    client.request(method, url, body)
            opener.return_value.open.assert_not_called()

    def test_unique_record_check_requires_one_total_record_and_exact_record_identity(self):
        client = M.Client("private-token")
        for values in [[], [record(), {**record(), "type": "AAAA"}]]:
            client.request = mock.Mock(return_value={"result": values, "result_info": {"total_count": len(values)}})
            with self.assertRaises(M.Refusal):
                client.unique_record()
        client.request = mock.Mock(return_value={"result": [record()], "result_info": {"total_count": 2}})
        with self.assertRaises(M.Refusal):
            client.unique_record()

    def test_api_error_hides_credentials_and_redirect_does_not_forward_authorization(self):
        token = "private-token-DO-NOT-OUTPUT"
        with mock.patch.object(M.urllib.request, "build_opener") as opener:
            client = M.Client(token)
            opener.return_value.open.side_effect = urllib.error.URLError(token)
            with self.assertRaises(M.Refusal) as error:
                client.record()
            self.assertNotIn(token, str(error.exception))
        with self.assertRaisesRegex(M.Refusal, "redirect-refused"):
            M.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example")


if __name__ == "__main__":
    unittest.main()
