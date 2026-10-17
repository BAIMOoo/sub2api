#!/usr/bin/env python3
"""Offline postcutover tests: no private config, SSH, model, SQL or Docker."""
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock

PATH = pathlib.Path(__file__).with_name("postcutover_canary.py")
spec = importlib.util.spec_from_file_location("postcutover_canary", PATH)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
CLIENT = "01234567-89ab-4cde-8f01-23456789abcd"
TOKEN = "POSTCUTOVER_PRIVATE_TOKEN_DO_NOT_OUTPUT"


def payload():
    return {"token": TOKEN, "request_id": "migration-canary-postcutover-" + "a" * 32, "client_request_id": CLIENT}


def billing():
    return {"usage_count": 1, "dedup_count": 1, "quota_used": "1.00005000", "rows": [{
        "usage_log_id": 1, "model": "gpt-6.1-sol", "requested_model": "gpt-6.1-sol", "stream": True,
        "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 5, "cache_creation_tokens": 0,
        "total_cost": "0.00004", "actual_cost": "0.00005", "rate_multiplier": "1.25", "billing_type": 0,
        "ledger_count": 0, "ledger_delta": "0", "ledger_all_applied": False}]}


class WorkerBackend:
    def __init__(self):
        self.calls = []
        self.proof = {"pg_container_id": M.PG_ID, "instance_id": "same-process", "background_ready": True}

    def preflight(self):
        self.calls.append("preflight")
        return copy.deepcopy(self.proof)

    def sql(self, query):
        self.calls.append(query)
        if "JOIN users" in query:
            return {"api_key_id": 123, "active": True, "primary": True, "system_identifier": M.SYSTEM_ID,
                    "quota_enabled": True, "quota_used": "1"}
        return billing()


def controls():
    return {"runtime": {"instance_id": "same-process", "ready": True, "serving": True, "draining": False,
                        "background_workers_enabled": True},
            "postgres": {"endpoint": "target", "paused": False},
            "redis": {"address": "127.0.0.1:26379", "paused": False, "pubsub_cache_ready": True},
            "writers": {"held": False}, "ingress": {"paused": False},
            "background": {"activated": True, "activating": False, "pending": [], "started": ["scheduled"], "failures": {}}}


class CanaryTests(unittest.TestCase):
    def test_current_recovery_generation_and_wrapper_image_are_pinned(self):
        self.assertEqual(M.RPC_SHA256, "85d980acff46125de0db0a2734d33b632f1cfb919577be871f8c56faff2db75a")
        self.assertEqual(M.APP_ID, "17e3bfacbd442beff52427e003e58f4970e613203356b8bb8bde0e98b9229bce")
        self.assertEqual(M.PG_IMAGE, "sha256:9a5296338b4957a7082ad925c9f31346f6f1cb6b7cbe7bd2971430432488a395")

    def test_default_is_offline_without_imports_secret_reads_or_commands(self):
        with mock.patch.object(M, "load_base", side_effect=AssertionError("no base")), \
             mock.patch.object(M, "load_target", side_effect=AssertionError("no target")), \
             mock.patch.object(M.subprocess, "run", side_effect=AssertionError("no process")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
        self.assertFalse(json.loads(out.getvalue())["billable_request_sent"])

    def test_original_canary_is_hash_bound_and_reused_without_copying_logic(self):
        base = M.load_base()
        self.assertEqual(M.BASE_SHA256, M.hashlib.sha256(PATH.with_name("model_canary.py").read_bytes()).hexdigest())
        self.assertEqual(base.MODEL, "gpt-6.1-sol")
        self.assertEqual(base.request_body()["max_output_tokens"], 128)
        self.assertEqual(base.TargetTLSConnection.__module__, "postcutover_reused_model_canary")
        with self.assertRaisesRegex(M.Refusal, "model-canary-changed"):
            M.reused_base(b"raise AssertionError('must not execute')")

    def test_remote_sources_compile_and_inspect_does_not_load_token_or_rpc(self):
        apply = M.remote_source(payload())
        compile(apply, "offline-postcutover-worker", "exec")
        inspect = M.remote_source(inspect=True)
        compile(inspect, "offline-inspect-worker", "exec")
        self.assertIn("result=remote_inspect()", inspect)
        self.assertNotIn(TOKEN, inspect)
        self.assertIn(TOKEN, apply)

    def worker(self, hook=None, failing_write=False):
        base, backend = M.load_base(), WorkerBackend()
        temporary = tempfile.TemporaryDirectory()
        scope = pathlib.Path(temporary.name).resolve() / "postcutover"
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(M, "SCOPE", scope))
        stack.enter_context(mock.patch.object(M, "private_root_dir"))
        stack.enter_context(mock.patch.object(M.os, "geteuid", return_value=0))
        stack.enter_context(mock.patch.object(M.sys, "platform", "linux"))
        stack.enter_context(mock.patch.object(M.signal, "signal"))
        stack.enter_context(mock.patch.object(M, "tls_health", return_value={"http_status": 200}))
        def model(lane, token, request_id, client_id, stats):
            persisted = json.loads((scope / M.JOURNAL).read_bytes())
            self.assertTrue(persisted["request_attempted"])
            self.assertTrue(persisted["post_intent_durable"])
            self.assertEqual(lane, "target")
            stats.update(effective_client_request_id=CLIENT, usage={"input_tokens": 15, "output_tokens": 5},
                         eof_after_terminal=True)
            if hook:
                hook(base, backend, scope)
        mocked = stack.enter_context(mock.patch.object(base, "model_request", side_effect=model))
        if failing_write:
            real_write = base.private_write
            def write(path, report, **kwargs):
                if report.get("post_intent_durable"):
                    raise OSError("fsync unavailable")
                return real_write(path, report, **kwargs)
            stack.enter_context(mock.patch.object(base, "private_write", side_effect=write))
        self.addCleanup(temporary.cleanup)
        self.addCleanup(stack.close)
        return base, backend, scope, mocked

    def test_success_has_one_post_and_exact_reused_billing_without_secret_evidence(self):
        base, backend, scope, model = self.worker()
        report = M.remote_apply(payload(), base, backend)
        self.assertTrue(report["ok"])
        self.assertTrue(report["billing"]["tokens_match_stream"])
        self.assertEqual(report["billing"]["request_id"], "client:" + CLIENT)
        self.assertFalse(report["billing"]["balance_delta_attribution_proven"])
        self.assertNotIn(TOKEN, json.dumps(report))
        self.assertNotIn(TOKEN, (scope / M.JOURNAL).read_text())
        self.assertEqual((scope / M.JOURNAL).stat().st_mode & 0o777, 0o600)
        model.assert_called_once()
        self.assertEqual(backend.calls.count("preflight"), 3)

    def test_existing_journal_prevents_every_repeated_post_even_after_success(self):
        base, backend, _, model = self.worker()
        M.remote_apply(payload(), base, backend)
        with self.assertRaisesRegex(M.Refusal, "reserved"):
            M.remote_apply(payload(), base, backend)
        model.assert_called_once()

    def test_lost_model_receipt_is_not_retried_and_does_not_expose_exception(self):
        def fail(*args):
            raise OSError(TOKEN)
        base, backend, _, model = self.worker(fail)
        report = M.remote_apply(payload(), base, backend)
        self.assertFalse(report["ok"])
        self.assertNotIn(TOKEN, json.dumps(report))
        with self.assertRaisesRegex(M.Refusal, "reserved"):
            M.remote_apply(payload(), base, backend)
        model.assert_called_once()

    def test_intent_fsync_failure_prevents_post(self):
        base, backend, _, model = self.worker(failing_write=True)
        with self.assertRaises(OSError):
            M.remote_apply(payload(), base, backend)
        model.assert_not_called()

    def test_preflight_failure_reserves_lane_without_post(self):
        base, backend, _, model = self.worker()
        backend.preflight = mock.Mock(side_effect=M.Refusal("target-not-ready"))
        report = M.remote_apply(payload(), base, backend)
        self.assertFalse(report["ok"])
        self.assertFalse(report["request_attempted"])
        model.assert_not_called()

    def test_prepost_instance_change_blocks_post(self):
        base, backend, _, model = self.worker()
        backend.preflight = mock.Mock(side_effect=[{"instance_id": "old"}, {"instance_id": "new"}])
        self.assertFalse(M.remote_apply(payload(), base, backend)["ok"])
        model.assert_not_called()

    def test_postflight_change_fails_acceptance_without_another_request(self):
        def changed(base, backend, scope):
            backend.proof["instance_id"] = "replacement"
        base, backend, _, model = self.worker(changed)
        report = M.remote_apply(payload(), base, backend)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "target-changed-during-canary")
        model.assert_called_once()

    def test_duplicate_exact_billing_fails(self):
        base, backend, _, model = self.worker()
        real_sql = backend.sql
        def sql(query):
            value = real_sql(query)
            if "JOIN users" not in query:
                value["dedup_count"] = 2
            return value
        backend.sql = sql
        report = M.remote_apply(payload(), base, backend)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "duplicate-exact-request-billing")
        model.assert_called_once()

    def target(self):
        backend = object.__new__(M.TargetBackend)
        backend.r = types.SimpleNamespace(TargetRPC=types.SimpleNamespace(is_mutation=lambda req: req["action"] != "status"))
        ledger = {"owner": M.OWNER, "pg": {"authority": "TENCENT", "promotion_intent": {"final_lsn": "10/0"}},
                  "redis": {"authority": "TENCENT"}, "operations": {}}
        backend.ledger = mock.Mock()
        backend.ledger.read.side_effect = lambda: copy.deepcopy(ledger)
        backend.b = mock.Mock()
        backend.b.endpoint_state.return_value = M.ENDPOINTS
        return backend, ledger

    def test_mixed_authority_endpoints_and_pending_mutations_block_before_pg_probe(self):
        cases = (lambda ledger, b: ledger["pg"].update(authority="AWS"),
                 lambda ledger, b: ledger["redis"].update(authority="COMMIT_INTENT"),
                 lambda ledger, b: b.endpoint_state.configure_mock(return_value={**M.ENDPOINTS, "redis": "source"}),
                 lambda ledger, b: ledger["operations"].update(x={"phase": "INTENT", "request": {"action": "app.restore"}}))
        for change in cases:
            backend, ledger = self.target()
            change(ledger, backend.b)
            with self.subTest(change=change), self.assertRaises(M.Refusal):
                backend.authority()
            backend.b.resource.assert_not_called()

    def test_target_sql_uses_exact_target_container_and_explicit_nondefault_port_stdin(self):
        backend = object.__new__(M.TargetBackend)
        backend.authority = mock.Mock()
        backend.b = mock.Mock()
        backend.b.command.return_value = b'{"ok":true}'
        backend.r = types.SimpleNamespace(strict_json=lambda raw, limit: json.loads(raw))
        self.assertEqual(backend.sql("SELECT '{}'::json"), {"ok": True})
        backend.authority.assert_called_once()
        argv, query = backend.b.command.call_args.args
        self.assertEqual(argv[:6], ["/usr/bin/docker", "exec", "--user", "postgres", "-i", M.PG_ID])
        self.assertIn("25432", argv)
        self.assertEqual(argv[argv.index("-h") + 1], "/tmp")
        self.assertTrue(query.startswith(b"BEGIN READ ONLY; SET LOCAL statement_timeout='2s';"))
        self.assertNotIn("SELECT", repr(argv))

    def test_installed_rpc_hash_and_all_accepted_resource_pins_are_required(self):
        raw = b"reviewed rpc test bytes"
        cfg = {"owner": M.OWNER, "pg": {"container_id": M.PG_ID, "image_id": M.PG_IMAGE, "system_identifier": M.SYSTEM_ID},
               "app": {"container_id": M.APP_ID, "image_id": M.APP_IMAGE, "control_port": 18093}}
        loaded = types.SimpleNamespace(load_config=lambda: copy.deepcopy(cfg))
        with mock.patch.object(M, "root_read", return_value=raw), \
             mock.patch.object(M.importlib.util, "spec_from_file_location") as spec, \
             mock.patch.object(M.importlib.util, "module_from_spec", return_value=loaded):
            with self.assertRaisesRegex(M.Refusal, "target-rpc-changed"):
                M.load_target()
            spec.assert_not_called()
            with mock.patch.object(M, "RPC_SHA256", M.hashlib.sha256(raw).hexdigest()):
                self.assertEqual(M.load_target()[1], cfg)
                for kind, field in (("pg", "container_id"), ("pg", "image_id"), ("pg", "system_identifier"),
                                    ("app", "container_id"), ("app", "image_id"), ("app", "control_port")):
                    before = cfg[kind][field]
                    cfg[kind][field] = "replacement"
                    with self.subTest(kind=kind, field=field), self.assertRaisesRegex(M.Refusal, "pins-changed"):
                        M.load_target()
                    cfg[kind][field] = before

    def test_authority_requires_original_running_primary_and_matching_durable_intent(self):
        def prepared():
            backend, ledger = self.target()
            backend.cfg = {}
            check = types.SimpleNamespace(_pg_validate=lambda sample, primary: M.need(
                primary is True and sample["in_recovery"] is False, "target-primary-proof-failed"))
            backend.r.TargetRPC = mock.Mock(return_value=check)
            backend.r.TargetRPC.is_mutation = lambda req: req["action"] != "status"
            backend.b.resource.return_value = {"Id": M.PG_ID, "Image": M.PG_IMAGE, "State": {"Running": True}}
            backend.b.pg_locks = contextlib.nullcontext
            backend.b.pg_manifest.return_value = {"authority_mode": "PRIMARY", "promotion_intent": ledger["pg"]["promotion_intent"]}
            backend.b.pg_sample.return_value = {"system_identifier": M.SYSTEM_ID, "in_recovery": False, "wal_timeline": 2}
            backend.b.redis_inspect.return_value = {"role": "master", "failover_state": "no-failover"}
            return backend
        self.assertTrue(prepared().authority()["primary"])
        changes = (lambda b: b.resource.return_value.update(Id="replacement"),
                   lambda b: b.resource.return_value.update(Image="replacement"),
                   lambda b: b.resource.return_value["State"].update(Running=False),
                   lambda b: b.pg_manifest.return_value.update(authority_mode="STANDBY"),
                   lambda b: b.pg_manifest.return_value.update(promotion_intent={"final_lsn": "11/0"}),
                   lambda b: b.pg_sample.return_value.update(in_recovery=True),
                   lambda b: b.pg_sample.return_value.update(system_identifier="another-cluster"),
                   lambda b: b.redis_inspect.return_value.update(role="slave"))
        for change in changes:
            backend = prepared()
            change(backend.b)
            with self.subTest(change=change), self.assertRaises(M.Refusal):
                backend.authority()

    def test_background_and_live_routes_must_be_ready(self):
        for field, key, value in (("runtime", "background_workers_enabled", False), ("runtime", "ready", False),
                ("postgres", "endpoint", "source"), ("redis", "pubsub_cache_ready", False),
                ("redis", "address", "127.0.0.1:16379"), ("writers", "held", True), ("ingress", "paused", True),
                ("background", "pending", ["scheduled"]), ("background", "activated", False)):
            backend = object.__new__(M.TargetBackend)
            backend.authority = lambda: {"primary": True}
            backend.b = mock.Mock()
            backend.b.resource.return_value = {"Id": M.APP_ID, "Image": M.APP_IMAGE, "State": {"Pid": 123}}
            values = controls()
            values[field][key] = value
            backend.control = lambda name: copy.deepcopy(values[name.split(".")[0]])
            with self.subTest(field=field, key=key), self.assertRaises(M.Refusal):
                backend.preflight()

    def test_tls_health_is_only_fixed_verified_https_and_always_closes(self):
        base = M.load_base()
        with mock.patch.object(base, "TargetTLSConnection") as ctor:
            conn = ctor.return_value
            conn.evidence = {"peer_ip": M.TARGET_IP, "certificate_chain_and_hostname_verified": True}
            conn.getresponse.return_value.status = 200
            conn.getresponse.return_value.read.return_value = b"ok"
            result = M.tls_health(base)
            self.assertTrue(result["transport"]["certificate_chain_and_hostname_verified"])
            ctor.assert_called_once_with(timeout=5)
            conn.request.assert_called_once_with("GET", "/health", headers={"Connection": "close"})
            conn.close.assert_called_once()

    def test_local_apply_sends_token_only_in_target_ssh_stdin(self):
        base = M.load_base()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(M, "load_base", return_value=base), \
             mock.patch.object(base, "load_token", return_value=TOKEN), \
             mock.patch.object(M.pathlib.Path, "home", return_value=pathlib.Path(temporary).resolve()), \
             mock.patch.object(M, "target_ssh", return_value=["ssh", "fixed-tencent"]), \
             mock.patch.object(M.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b'{"ok":true}', b"")) as process, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main(["--apply"]), 0)
            self.assertEqual(process.call_args.args[0][-1], "sudo -n python3 -I -")
            self.assertNotIn(TOKEN, repr(process.call_args.args[0]))
            self.assertIn(TOKEN.encode(), process.call_args.kwargs["input"])
            self.assertNotIn("env", process.call_args.kwargs)
            self.assertNotIn(TOKEN, out.getvalue())
            for artifact in pathlib.Path(temporary).rglob("*.json"):
                self.assertNotIn(TOKEN, artifact.read_text())

    def test_inspect_uses_no_token_and_remote_inspect_no_sql_http_or_rpc(self):
        base = M.load_base()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(M, "load_base", return_value=base), \
             mock.patch.object(base, "load_token", side_effect=AssertionError("no private token")), \
             mock.patch.object(M.pathlib.Path, "home", return_value=pathlib.Path(temporary).resolve()), \
             mock.patch.object(M, "target_ssh", return_value=["ssh", "fixed-tencent"]), \
             mock.patch.object(M.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b'{"ok":true}', b"")) as process, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M.main(["--inspect"]), 0)
            self.assertIn(b"result=remote_inspect()", process.call_args.kwargs["input"])
        with mock.patch.object(M.os, "geteuid", return_value=0), mock.patch.object(M.sys, "platform", "linux"), \
             mock.patch.object(M, "private_root_dir"), mock.patch.object(M, "root_read", return_value=b'{"ok":true}'), \
             mock.patch.object(M, "load_target", side_effect=AssertionError("no RPC")):
            self.assertEqual(M.remote_inspect(), {"ok": True})

    def test_root_file_reader_refuses_symlink_and_untrusted_owner(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(M, "private_root_dir"):
            root = pathlib.Path(temporary)
            target = root / "target"
            target.write_bytes(b"secret")
            target.chmod(0o600)
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                M.root_read(link)
            info = target.stat()
            changed = list(info)
            changed[4] = 12345
            with mock.patch.object(M.os, "fstat", return_value=M.os.stat_result(changed)), self.assertRaises(M.Refusal):
                M.root_read(target)


if __name__ == "__main__":
    unittest.main()
