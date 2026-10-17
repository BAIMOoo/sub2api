#!/usr/bin/env python3
"""Offline continuation faults: no SSH, production config or model request."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock

PATH = pathlib.Path(__file__).with_name("postcutover_confirmed_canary.py")
spec = importlib.util.spec_from_file_location("confirmed_canary", PATH)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
CLIENT = "01234567-89ab-4cde-8f01-23456789abcd"
TOKEN = "CONFIRMED_PRIVATE_TOKEN_DO_NOT_OUTPUT"


class RPCRefusal(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


RPC = types.SimpleNamespace(Refusal=RPCRefusal)


def unsent_report():
    return {"ok": False, "request_attempted": False, "lane": "postcutover", "stream": {},
            "journal_path": str(M.FAILED_REMOTE_REPORT)}


def payload():
    return {"token": TOKEN, "request_id": "migration-canary-postcutover-" + "b" * 32,
            "client_request_id": CLIENT}


class Backend:
    def __init__(self, original):
        self.original = original
        self.preflight = mock.Mock(return_value={"instance_id": "same", "background_ready": True})
        self.sql = mock.Mock(side_effect=self.sql_result)

    def sql_result(self, query):
        if "JOIN users" in query:
            return {"api_key_id": 123, "active": True, "primary": True,
                    "system_identifier": self.original.SYSTEM_ID,
                    "quota_enabled": True, "quota_used": "1"}
        return {"usage_count": 1, "dedup_count": 1, "quota_used": "1.00005000", "rows": [{
            "usage_log_id": 1, "model": "gpt-6.1-sol", "requested_model": "gpt-6.1-sol", "stream": True,
            "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 5, "cache_creation_tokens": 0,
            "total_cost": "0.00004", "actual_cost": "0.00005", "rate_multiplier": "1.25", "billing_type": 0,
            "ledger_count": 0, "ledger_delta": "0", "ledger_all_applied": False}]}


class ConfirmedCanaryTests(unittest.TestCase):
    def test_original_program_and_real_failed_receipt_digest_are_fixed(self):
        self.assertEqual(M.FAILED_REPORT_SHA256, "0dc9baa14e3968ee667711ae1f6565dcbf019d27034a23392a9fa7ad8d9a8b5a")
        raw = PATH.with_name("postcutover_canary.py").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), M.ORIGINAL_SHA256)
        self.assertEqual(M.load_original().SCOPE / "target.json", M.FAILED_REMOTE_REPORT)
        with self.assertRaisesRegex(M.Refusal, "canary-changed"):
            M.reused_postcutover(raw + b"\n")

    def test_default_offline_has_no_credentials_import_or_command(self):
        with mock.patch.object(M, "load_original", side_effect=AssertionError("no original")), \
             mock.patch.object(M, "local_unsent_report", side_effect=AssertionError("no evidence read")), \
             mock.patch.object(M.subprocess, "run", side_effect=AssertionError("no commands")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
        self.assertFalse(json.loads(out.getvalue())["billable_request_sent"])

    def test_exact_report_hash_and_unsent_semantics_both_required(self):
        report = unsent_report()
        raw = M.canonical(report)
        digest = hashlib.sha256(raw).hexdigest()
        with mock.patch.object(M, "FAILED_REPORT_SHA256", digest):
            self.assertEqual(M.validate_unsent_report(raw), report)
            with self.assertRaisesRegex(M.Refusal, "sha-changed"):
                M.validate_unsent_report(M.canonical({**report, "request_attempted": True}))
        for changed in ({"ok": True}, {"request_attempted": True}, {"post_intent_durable": True},
                        {"stream": {"event_count": 1}}, {"lane": "other"}, {"journal_path": "/different"}):
            value = M.canonical({**report, **changed})
            with self.subTest(changed=changed), \
                 mock.patch.object(M, "FAILED_REPORT_SHA256", hashlib.sha256(value).hexdigest()), \
                 self.assertRaisesRegex(M.Refusal, "not-proven-unsent"):
                M.validate_unsent_report(value)

    def test_busy_preflight_and_sql_retry_only_read_methods(self):
        backend = mock.Mock()
        backend.preflight.side_effect = [RPCRefusal("OPERATION_BUSY"), {"ready": True}]
        backend.sql.side_effect = [RPCRefusal("OPERATION_BUSY"), {"count": 1}]
        adapter = M.ReadOnlyBusyAdapter(backend, RPC)
        with mock.patch.object(M.time, "sleep") as sleep:
            self.assertEqual(adapter.preflight(), {"ready": True})
            self.assertEqual(adapter.sql("SELECT 1"), {"count": 1})
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(adapter.events, [{"operation": "preflight", "busy_attempt": 1},
                                          {"operation": "sql", "busy_attempt": 1}])
        with self.assertRaisesRegex(M.Refusal, "only-read-only"):
            adapter._read("model_request")
        backend.model_request.assert_not_called()

    def test_all_other_errors_and_busy_lookalikes_are_not_retried(self):
        class Lookalike(Exception):
            code = "OPERATION_BUSY"
        for error in (RPCRefusal("AUTHORITY_CHANGED"), OSError(TOKEN), Lookalike("OPERATION_BUSY")):
            backend = mock.Mock()
            backend.preflight.side_effect = error
            adapter = M.ReadOnlyBusyAdapter(backend, RPC)
            with self.subTest(error=type(error).__name__), mock.patch.object(M.time, "sleep") as sleep, \
                 self.assertRaises(type(error)):
                adapter.preflight()
            backend.preflight.assert_called_once()
            sleep.assert_not_called()

    def test_busy_attempt_count_and_elapsed_window_are_bounded(self):
        for clock, expected in ((None, M.MAX_READ_ATTEMPTS), ([0, 3], 1)):
            backend = mock.Mock()
            backend.sql.side_effect = RPCRefusal("OPERATION_BUSY")
            adapter = M.ReadOnlyBusyAdapter(backend, RPC)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(M.time, "sleep"))
                if clock:
                    stack.enter_context(mock.patch.object(M.time, "monotonic", side_effect=clock))
                with self.assertRaisesRegex(M.Refusal, "busy-exhausted"):
                    adapter.sql("SELECT 1")
            self.assertEqual(backend.sql.call_count, expected)

    def worker(self, model_error=None):
        original = M.load_original()
        base = original.load_base()
        backend = Backend(original)
        temporary = tempfile.TemporaryDirectory()
        scope = pathlib.Path(temporary.name).resolve() / "new-canary"
        raw = M.canonical(unsent_report())
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(M, "SCOPE", scope))
        stack.enter_context(mock.patch.object(M, "FAILED_REPORT_SHA256", hashlib.sha256(raw).hexdigest()))
        stack.enter_context(mock.patch.object(original, "root_read", return_value=raw))
        stack.enter_context(mock.patch.object(original, "private_root_dir"))
        stack.enter_context(mock.patch.object(M.os, "geteuid", return_value=0))
        stack.enter_context(mock.patch.object(M.sys, "platform", "linux"))
        stack.enter_context(mock.patch.object(original.signal, "signal"))
        stack.enter_context(mock.patch.object(original, "tls_health", return_value={"http_status": 200}))
        stack.enter_context(mock.patch.object(M.time, "sleep"))

        def model(lane, token, request_id, client_id, stats):
            durable = json.loads((scope / "target.json").read_bytes())
            self.assertTrue(durable["request_attempted"])
            self.assertTrue(durable["post_intent_durable"])
            self.assertEqual(durable["lane"], M.LANE)
            self.assertEqual(durable["original_unsent_report_sha256"], hashlib.sha256(raw).hexdigest())
            if model_error:
                raise model_error
            stats.update(effective_client_request_id=CLIENT, usage={"input_tokens": 15, "output_tokens": 5},
                         eof_after_terminal=True)

        model = stack.enter_context(mock.patch.object(base, "model_request", side_effect=model))
        self.addCleanup(temporary.cleanup)
        self.addCleanup(stack.close)
        return original, base, backend, scope, model

    def test_busy_before_post_recovers_and_exact_billing_remains_required(self):
        original, base, backend, scope, model = self.worker()
        proof = copy.deepcopy(backend.preflight.return_value)
        backend.preflight.side_effect = [proof, RPCRefusal("OPERATION_BUSY"), proof, proof]
        original_sql = backend.sql_result
        first = True
        def sql(query):
            nonlocal first
            if first:
                first = False
                raise RPCRefusal("OPERATION_BUSY")
            return original_sql(query)
        backend.sql.side_effect = sql
        report = M.remote_apply(payload(), original, base, backend, RPC)
        self.assertTrue(report["ok"])
        self.assertTrue(report["billing"]["tokens_match_stream"])
        self.assertEqual(report["lane"], M.LANE)
        self.assertEqual({x["operation"] for x in report["read_only_busy_retries"]}, {"preflight", "sql"})
        self.assertNotIn(TOKEN, (scope / "target.json").read_text())
        model.assert_called_once()
        with self.assertRaisesRegex(original.Refusal, "lane-reserved"):
            M.remote_apply(payload(), original, base, backend, RPC)
        model.assert_called_once()

    def test_nonbusy_preflight_failure_never_posts_and_never_retries(self):
        original, base, backend, _, model = self.worker()
        backend.preflight.side_effect = RPCRefusal("AUTHORITY_CHANGED")
        report = M.remote_apply(payload(), original, base, backend, RPC)
        self.assertFalse(report["ok"])
        self.assertFalse(report["request_attempted"])
        backend.preflight.assert_called_once()
        model.assert_not_called()

    def test_model_busy_error_is_never_retried_and_journal_blocks_reentry(self):
        original, base, backend, _, model = self.worker(RPCRefusal("OPERATION_BUSY"))
        report = M.remote_apply(payload(), original, base, backend, RPC)
        self.assertFalse(report["ok"])
        self.assertTrue(report["request_attempted"])
        self.assertEqual(report["read_only_busy_retries"], [])
        model.assert_called_once()
        with self.assertRaisesRegex(original.Refusal, "lane-reserved"):
            M.remote_apply(payload(), original, base, backend, RPC)
        model.assert_called_once()

    def test_remote_report_change_blocks_before_scope_or_model(self):
        original, base, backend, scope, model = self.worker()
        with mock.patch.object(original, "root_read", return_value=b'{"request_attempted":true}'), \
             self.assertRaisesRegex(M.Refusal, "sha-changed"):
            M.remote_apply(payload(), original, base, backend, RPC)
        self.assertFalse(scope.exists())
        backend.preflight.assert_not_called()
        model.assert_not_called()

    def test_remote_sources_embed_original_exact_bytes_without_wrong_file_read(self):
        for inspect in (False, True):
            source = M.remote_source(payload(), inspect=inspect)
            compile(source, "offline-confirmed-worker", "exec")
            encoded = M.base64.b64encode(PATH.with_name("postcutover_canary.py").read_bytes()).decode()
            self.assertIn(encoded, source)
            self.assertIn("original=reused_postcutover(base64.b64decode(", source)
            self.assertEqual(TOKEN in source, not inspect)
            namespace = {}
            # Execute only the generated definitions and embedded original;
            # the final apply/inspect call remains deliberately unexecuted.
            exec(compile(source.rsplit("\ntry:\n result=", 1)[0], "generated-definitions", "exec"), namespace)
            embedded_original = namespace["original"]
            self.assertEqual(embedded_original.__file__, "postcutover_canary.py")
            self.assertEqual(embedded_original.SCOPE / embedded_original.JOURNAL, M.FAILED_REMOTE_REPORT)
            self.assertEqual(embedded_original.reused_base(PATH.with_name("model_canary.py").read_bytes()).MODEL,
                             "gpt-6.1-sol")
        with mock.patch.object(M.pathlib.Path, "read_bytes", return_value=b"changed"), \
             self.assertRaisesRegex(M.Refusal, "canary-changed"):
            M.remote_source(payload())

    def test_local_unsent_hash_failure_precedes_token_and_ssh(self):
        original = M.load_original()
        base = original.load_base()
        with mock.patch.object(M, "load_original", return_value=original), \
             mock.patch.object(original, "load_base", return_value=base), \
             mock.patch.object(M, "local_unsent_report", side_effect=M.Refusal("receipt-changed")), \
             mock.patch.object(base, "load_token") as token, \
             mock.patch.object(M.subprocess, "run") as process, \
             self.assertRaisesRegex(M.Refusal, "receipt-changed"):
            M.main(["--apply"])
        token.assert_not_called()
        process.assert_not_called()

    def test_local_apply_keeps_token_only_in_ssh_stdin(self):
        original = M.load_original()
        base = original.load_base()
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(M, "load_original", return_value=original), \
             mock.patch.object(original, "load_base", return_value=base), \
             mock.patch.object(M, "local_unsent_report", return_value=unsent_report()), \
             mock.patch.object(base, "load_token", return_value=TOKEN), \
             mock.patch.object(M.pathlib.Path, "home", return_value=pathlib.Path(temporary).resolve()), \
             mock.patch.object(original, "target_ssh", return_value=["ssh", "fixed-tencent"]), \
             mock.patch.object(M.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b'{"ok":true}', b"")) as process, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main(["--apply"]), 0)
            self.assertNotIn(TOKEN, repr(process.call_args.args[0]))
            self.assertIn(TOKEN.encode(), process.call_args.kwargs["input"])
            self.assertNotIn(TOKEN, out.getvalue())
            for artifact in pathlib.Path(temporary).rglob("*.json"):
                self.assertNotIn(TOKEN, artifact.read_text())

    def test_inspect_reads_only_new_bound_journal_and_loads_no_token(self):
        original = M.load_original()
        report = {"lane": M.LANE, "original_unsent_report_sha256": M.FAILED_REPORT_SHA256,
                  "original_unsent_journal": str(M.FAILED_REMOTE_REPORT), "ok": True}
        with mock.patch.object(original, "remote_inspect", return_value=report) as inspect, \
             mock.patch.object(original, "load_target", side_effect=AssertionError("no rpc")):
            self.assertEqual(M.remote_inspect(original), report)
            self.assertEqual(original.SCOPE, M.SCOPE)
            inspect.assert_called_once()
            with mock.patch.object(original, "remote_inspect", return_value={"ok": True}), \
                 self.assertRaisesRegex(M.Refusal, "binding-invalid"):
                M.remote_inspect(original)
        base = original.load_base()
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(M, "load_original", return_value=original), \
             mock.patch.object(original, "load_base", return_value=base), \
             mock.patch.object(base, "load_token", side_effect=AssertionError("no token")), \
             mock.patch.object(M, "local_unsent_report", side_effect=AssertionError("no old local read")), \
             mock.patch.object(M.pathlib.Path, "home", return_value=pathlib.Path(temporary).resolve()), \
             mock.patch.object(original, "target_ssh", return_value=["ssh", "fixed-tencent"]), \
             mock.patch.object(M.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, M.canonical(report), b"")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M.main(["--inspect"]), 0)


if __name__ == "__main__":
    unittest.main()
