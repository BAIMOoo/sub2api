#!/usr/bin/env python3
"""Offline tests only: never read credentials or contact either production host."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock


HERE = pathlib.Path(__file__).parent
def load(name):
    spec = importlib.util.spec_from_file_location("test_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load("recovery_generation_canary")
CORE, RPC = load("rolling_app_recovery"), load("target_rpc")
TOKEN, CLIENT = "RECOVERY_PRIVATE_TOKEN_DO_NOT_OUTPUT", "01234567-89ab-4cde-8f01-23456789abcd"


def proof():
    return {"v": 1, "run": M.RUN, "owner": M.OWNER, "app_container_id": M.APP_ID, "app_image_id": M.APP_IMAGE,
            "app_config_sha256": "a" * 64, "app_pid": 1234, "instance_id": "new-generation", "control_port": 18093,
            "ready": True, "writer_failures": 0, "writer_dropped": 0, "background_workers_enabled": False,
            "persistent_endpoints": M.ENDPOINTS, "pg_authority": "AWS", "redis_authority": "AWS",
            "pg_container_id": M.PG_ID, "pg_image_id": M.PG_IMAGE, "system_identifier": M.SYSTEM_ID,
            "pg_in_recovery": True, "redis_role": "slave", "redis_run_id": RPC.REDIS_TARGET_RUN_ID,
            "redis_master_link_up": True, "adoption_sha256": "b" * 64, "controller_config_sha256": "c" * 64,
            "target_ledger_sha256": "d" * 64, "ledger_epoch": 14, "public_route_proven": True, "checked_at": time.time()}


def payload():
    return {"token": TOKEN, "request_id": "migration-canary-recovery-generation-" + "a" * 32,
            "client_request_id": CLIENT, "target_preflight": proof()}


def billing():
    return {"usage_count": 1, "dedup_count": 1, "quota_used": "1.00005000", "rows": [{
        "usage_log_id": 1, "model": "gpt-6.1-sol", "requested_model": "gpt-6.1-sol", "stream": True,
        "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 5, "cache_creation_tokens": 0,
        "total_cost": "0.00004", "actual_cost": "0.00005", "rate_multiplier": "1.25", "billing_type": 0,
        "ledger_count": 0, "ledger_delta": "0", "ledger_all_applied": False}]}


def target_fixture():
    ledger = {"v": 1, "owner": M.OWNER, "epoch": 14, "pg": {"authority": "AWS", "arm": {"status": "CANCELLED"}, "promotion_intent": None},
              "redis": {"authority": "AWS"}, "operations": {}}
    common = {"config_sha256": "a" * 64, "rootfs_diffids": ["sha256:" + "b" * 64]}
    cfg = {"v": 1, "run": M.RUN, "owner": M.OWNER, "program_sha256": M.RPC_SHA256, "machine_id_sha256": "f" * 64,
           "app": {**common, "container_id": M.APP_ID, "image_id": M.APP_IMAGE, "control_port": 18093},
           "pg": {**common, "container_id": M.PG_ID, "image_id": M.PG_IMAGE, "system_identifier": M.SYSTEM_ID,
                  "source_timeline": 1, "owner": M.OWNER, "volume_path": "/var/lib/docker/volumes/pinned-pg/_data"},
           "redis": {**common, "container_id": "c" * 64, "image_id": "sha256:" + "d" * 64,
                     "owner": M.OWNER, "volume_path": "/var/lib/docker/volumes/pinned-redis/_data"}}
    runtime = {"instance_id": "new-generation", "ready": True, "serving": True, "draining": False,
               "background_workers_enabled": False,
               "tasks": {name: {"active": 1, "pending": 0, "failed": 0, "dropped": 0} for name in RPC.REQUIRED_WRITERS}}
    controls = {"runtime": runtime, "postgres": {"endpoint": "source", "paused": False},
                "redis": {"address": "127.0.0.1:16379", "paused": False, "pubsub_cache_ready": True},
                "writers": {"held": False}, "ingress": {"paused": False},
                "background": {"activated": False, "activating": False, "started": [], "pending": ["scheduled"], "failures": {}}}
    rolling = {"owner": M.OWNER, "phase": "COMPLETE", "intent": None,
               "new_identity": {"container_id": M.APP_ID, "config_sha256": "a" * 64},
               "adoption_receipt": {"old_exited_cleanly": True, "redirect": CORE.REDIRECT}}
    files = {M.ROOT / "rpc/config.json": M.canonical(cfg), M.ROOT / "rpc/state.json": M.canonical(ledger),
             M.ROOT / "rolling-recovery/state.json": M.canonical(rolling)}
    receipt = {"v": 1, "run": M.RUN, "owner": M.OWNER, "old_container_id": M.OLD_APP_ID, "new_container_id": M.APP_ID,
               "new_instance_id": "new-generation", "new_app_config_sha256": "a" * 64, "new_control_port": 18093,
               "controller_config_raw_sha256": M.sha(files[M.ROOT / "rpc/config.json"]),
               "rolling_complete_sha256": M.sha(files[M.ROOT / "rolling-recovery/state.json"]),
               "new_control_token_sha256": M.sha(b"private-control-token")}
    files[M.ROOT / "rolling-recovery/controller-adoption.json"] = M.canonical(receipt)
    hostcfg = {}
    for name, key in (("rolling_app_host", "host_sha256"), ("rolling_app_recovery", "core_sha256")):
        raw = (HERE / (name + ".py")).read_bytes()
        files[M.LIB / (name + ".py")] = raw
        hostcfg[key] = M.sha(raw)
    files[M.ROOT / "rolling-recovery/host-config.json"] = M.canonical(hostcfg)
    b = mock.Mock()
    old = {"Id": M.OLD_APP_ID, "Image": M.APP_IMAGE,
           "State": {"Running": False, "Status": "exited", "ExitCode": 0, "Pid": 0, "OOMKilled": False},
           "HostConfig": {"RestartPolicy": {"Name": "no"}}}
    b.docker_json.return_value = old
    b.resource.return_value = {"Id": M.APP_ID, "Image": M.APP_IMAGE, "State": {"Pid": 1234}}
    b.endpoint_state.return_value = M.ENDPOINTS
    b._app_token.return_value = b"private-control-token"
    b.app_control.side_effect = lambda name: {"http_status": 200, "body": copy.deepcopy(controls[name.split(".")[0]])}
    b.pg_locks = contextlib.nullcontext
    b.pg_manifest.return_value = {"authority_mode": "STANDBY", "promotion_intent": None}
    b.pg_sample.return_value = {"system_identifier": M.SYSTEM_ID, "version_num": 180003, "listen_addresses": "127.0.0.1",
                                "port": 25432, "in_recovery": True, "source_timeline": 1, "transaction_read_only": "on",
                                "default_transaction_read_only": "off", "free_bytes": 3 * 1024 ** 3}
    b.redis_preroute_pair.return_value = {"source": {"role": "master", "run_id": RPC.REDIS_SOURCE_RUN_ID},
        "target": {"role": "slave", "run_id": RPC.REDIS_TARGET_RUN_ID, "master_host": "127.0.0.1", "master_port": "16379",
                   "master_link_status": "up", "master_sync_in_progress": "0"}}
    b.command.return_value = ("*nat\n:OUTPUT ACCEPT [0:0]\n" + " ".join(CORE.REDIRECT) + "\nCOMMIT\n").encode()
    store = mock.Mock()
    store.read.side_effect = lambda: copy.deepcopy(ledger)
    rpc = types.SimpleNamespace(LocalBackend=lambda cfg: b, FileStore=lambda cfg: store, TargetRPC=RPC.TargetRPC,
                                strict_json=RPC.strict_json, REQUIRED_WRITERS=RPC.REQUIRED_WRITERS,
                                REDIS_SOURCE_RUN_ID=RPC.REDIS_SOURCE_RUN_ID, REDIS_TARGET_RUN_ID=RPC.REDIS_TARGET_RUN_ID)
    return rpc, cfg, b, controls, files, ledger


class RecoveryTests(unittest.TestCase):
    def target_probe(self, change=None):
        rpc, cfg, b, controls, files, ledger = target_fixture()
        if change:
            change(b, controls, files, ledger)
        spec = M.importlib.util.spec_from_file_location
        def local_spec(name, path):
            return spec(name, HERE / pathlib.Path(path).name)
        with mock.patch.object(M, "load_target", return_value=(rpc, cfg)), \
             mock.patch.object(M.os, "geteuid", return_value=0), mock.patch.object(M.sys, "platform", "linux"), \
             mock.patch.object(M, "root_read", side_effect=lambda path, *args, **kwargs: files[pathlib.Path(path)]), \
             mock.patch.object(M.importlib.util, "spec_from_file_location", side_effect=local_spec):
            return M.target_preflight()

    def test_target_probe_really_checks_adoption_roles_controls_and_scoped_route(self):
        value = self.target_probe()
        M.validate_target_proof(value)
        self.assertEqual(value["app_container_id"], M.APP_ID)
        changes = [lambda b, c, f, l: c["runtime"].update(instance_id="old-generation"),
                   lambda b, c, f, l: c["runtime"]["tasks"]["ops_system_log"].update(failed=1),
                   lambda b, c, f, l: c["postgres"].update(endpoint="target"),
                   lambda b, c, f, l: c["redis"].update(paused=True),
                   lambda b, c, f, l: c["background"].update(activated=True),
                   lambda b, c, f, l: b.pg_sample.return_value.update(in_recovery=False),
                   lambda b, c, f, l: b.pg_manifest.return_value.update(authority_mode="PRIMARY"),
                   lambda b, c, f, l: b.redis_preroute_pair.return_value["target"].update(role="master"),
                   lambda b, c, f, l: b.redis_preroute_pair.return_value["target"].update(master_link_status="down"),
                   lambda b, c, f, l: b.docker_json.return_value["State"].update(Running=True),
                   lambda b, c, f, l: b.command.configure_mock(return_value=b"*nat\n:OUTPUT ACCEPT [0:0]\nCOMMIT\n"),
                   lambda b, c, f, l: f.update({M.ROOT / "rpc/config.json": b"changed"})]
        for change in changes:
            with self.subTest(change=change), self.assertRaises((M.Refusal, RPC.Refusal)):
                self.target_probe(change)

    def test_source_cohort_is_bound_to_record_drain_adoption_and_instance(self):
        p = proof()
        drain = {"owner": M.OWNER, "new_instance_id": p["instance_id"], "new_container_id": M.APP_ID,
                 "adoption_sha256": p["adoption_sha256"], "ledger_sha256": p["target_ledger_sha256"], "ledger_epoch": 14}
        drain_raw = M.canonical(drain)
        record = {"v": 1, "run": M.RUN, "owner": M.OWNER, "id": "f" * 32, "proof_sha256": M.sha(drain_raw)}
        record_raw = M.canonical(record)
        state = {"v": 1, "run": M.RUN, "owner": M.OWNER, "window": None, "pg": "AWS", "redis": "AWS",
                 "budget_cohort": {"id": "f" * 32, "record_sha256": M.sha(record_raw)}}
        files = {M.ROOT / "runner/state.json": M.canonical(state),
                 M.ROOT / "runner/budget-cohorts" / ("f" * 32 + ".json"): record_raw,
                 M.ROOT / "runner/recovery-cohort-20261017/drain-proof.json": drain_raw}
        with mock.patch.object(M, "root_read", side_effect=lambda path: files[path]):
            self.assertEqual(M.source_cohort(p)["record_sha256"], M.sha(record_raw))
            p["instance_id"] = "different"
            with self.assertRaisesRegex(M.Refusal, "adoption-or-generation-changed"):
                M.source_cohort(p)

    def test_default_is_offline_without_import_secret_or_command(self):
        with mock.patch.object(M, "load_base", side_effect=AssertionError("offline")), \
             mock.patch.object(M, "load_target", side_effect=AssertionError("offline")), \
             mock.patch.object(M.subprocess, "run", side_effect=AssertionError("offline")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
        report = json.loads(out.getvalue())
        self.assertFalse(report["network_access"])
        self.assertFalse(report["billable_request_sent"])
        self.assertNotIn("/model-canary/", report["durable_no_repeat_journal"])

    def test_unmodified_sse_tls_and_source_sql_implementation_is_hash_bound(self):
        base = M.load_base()
        self.assertEqual(hashlib.sha256((HERE / "model_canary.py").read_bytes()).hexdigest(), M.BASE_SHA256)
        self.assertEqual(base.MODEL, "gpt-6.1-sol")
        self.assertEqual(base.request_body()["max_output_tokens"], 128)
        self.assertEqual(base.TargetTLSConnection.__module__, "recovery_reused_model_canary")
        with self.assertRaisesRegex(M.Refusal, "model-canary-changed"):
            M.reused_base(b"raise AssertionError('must not run')")

    def test_remote_sources_compile_and_inspect_carries_no_token_or_base(self):
        for action in ("target", "apply", "inspect", "finalize"):
            data = payload() if action == "apply" else {"request_id": "x", "target_postflight": proof()}
            source = M.remote_source(action, data)
            compile(source, "offline-recovery-worker", "exec")
            if action == "apply":
                self.assertIn(TOKEN, source)
            else:
                self.assertNotIn(TOKEN, source)
        self.assertIn("result=remote_inspect()", M.remote_source("inspect"))

    def test_proof_rejects_old_cid_wrong_role_unready_and_staleness(self):
        for key, value in [("app_container_id", M.OLD_APP_ID), ("control_port", 18092), ("ready", False),
                           ("writer_failures", 1), ("writer_dropped", 3), ("pg_in_recovery", False),
                           ("redis_role", "master"), ("pg_image_id", "old-image"), ("pg_authority", "TENCENT"),
                           ("public_route_proven", False), ("checked_at", time.time() - 31)]:
            candidate = proof()
            candidate[key] = value
            with self.subTest(key=key), self.assertRaises(M.Refusal):
                M.validate_target_proof(candidate)
        M.validate_target_proof(proof())

    def test_same_generation_allows_only_new_check_timestamp(self):
        before, after = proof(), proof()
        M.same_generation(before, after)
        after["instance_id"] = "restarted-generation"
        with self.assertRaisesRegex(M.Refusal, "generation-or-role-changed"):
            M.same_generation(before, after)

    def test_runtime_requires_all_writer_failure_counters_but_accepts_live_work(self):
        runtime = {"instance_id": "new", "ready": True, "serving": True, "draining": False,
                   "background_workers_enabled": False,
                   "tasks": {name: {"failed": 0, "dropped": 0, "active": 4, "pending": 1} for name in RPC.REQUIRED_WRITERS}}
        M.runtime_proof(runtime, "new", RPC.REQUIRED_WRITERS)
        for key in ("failed", "dropped"):
            bad = copy.deepcopy(runtime)
            bad["tasks"]["ops_system_log"][key] = 1
            with self.assertRaisesRegex(M.Refusal, "failure-or-coverage-gap"):
                M.runtime_proof(bad, "new", RPC.REQUIRED_WRITERS)
        del runtime["tasks"]["usage_records"]
        with self.assertRaises(M.Refusal):
            M.runtime_proof(runtime, "new", RPC.REQUIRED_WRITERS)

    def worker(self, stream_failure=None, sql_change=None, fail_intent=False):
        base, stack = M.load_base(), contextlib.ExitStack()
        tmp = tempfile.TemporaryDirectory()
        stack.callback(tmp.cleanup)
        scope = pathlib.Path(tmp.name).resolve() / "new-recovery-lane"
        stack.enter_context(mock.patch.object(M, "SCOPE", scope))
        stack.enter_context(mock.patch.object(M, "private_root_dir"))
        stack.enter_context(mock.patch.object(M.os, "geteuid", return_value=0))
        stack.enter_context(mock.patch.object(M.sys, "platform", "linux"))
        stack.enter_context(mock.patch.object(M.signal, "signal"))
        stack.enter_context(mock.patch.object(M, "source_cohort", return_value={"id": "d" * 32, "record_sha256": "e" * 64}))
        def sql(query):
            if "SELECT json_build_object('primary'" in query:
                return {"primary": True, "system_identifier": M.SYSTEM_ID}
            if "JOIN users" in query:
                return {"api_key_id": 123, "active": True, "primary": True, "system_identifier": M.SYSTEM_ID,
                        "quota_enabled": True, "quota_used": "1"}
            value = billing()
            if sql_change:
                sql_change(value)
            return value
        source_sql = stack.enter_context(mock.patch.object(base, "sql", side_effect=sql))
        def model(lane, token, request_id, client_id, stats):
            persisted = json.loads((scope / M.JOURNAL).read_bytes())
            self.assertEqual(persisted["phase"], "POST_INTENT")
            self.assertTrue(persisted["post_intent_durable"])
            self.assertEqual(lane, "target")
            stats.update(effective_client_request_id=CLIENT, usage={"input_tokens": 15, "output_tokens": 5},
                         eof_after_terminal=True, terminal_event="response.completed")
            if stream_failure:
                raise base.Refusal(stream_failure)
        model_mock = stack.enter_context(mock.patch.object(base, "model_request", side_effect=model))
        if fail_intent:
            real_write = base.private_write
            def write(path, value, **kwargs):
                if value.get("post_intent_durable"):
                    raise OSError("fsync unavailable")
                return real_write(path, value, **kwargs)
            stack.enter_context(mock.patch.object(base, "private_write", side_effect=write))
        self.addCleanup(stack.close)
        return base, scope, source_sql, model_mock

    def test_source_sql_reuses_original_and_checks_primary_before_each_query(self):
        base = M.load_base()
        calls = []
        def sql(query):
            calls.append(query)
            return {"primary": True, "system_identifier": M.SYSTEM_ID} if len(calls) % 2 else {"ok": True}
        base.sql = sql
        M.source_sql_adapter(base)
        self.assertEqual(base.sql("SELECT 'one'"), {"ok": True})
        self.assertEqual(base.sql("SELECT 'two'"), {"ok": True})
        self.assertIn("NOT pg_is_in_recovery()", calls[0])
        self.assertEqual(calls[1], "SELECT 'one'")
        self.assertIn("NOT pg_is_in_recovery()", calls[2])
        base = M.load_base()
        base.sql = mock.Mock(return_value={"primary": False, "system_identifier": M.SYSTEM_ID})
        authority = M.source_sql_adapter(base)
        with self.assertRaisesRegex(M.Refusal, "not-original-primary"):
            authority()

    def test_complete_stream_and_exact_billing_wait_for_target_postflight(self):
        base, scope, sql, model = self.worker()
        report = M.remote_apply(payload(), base)
        self.assertEqual(report["phase"], "BILLING_VERIFIED")
        self.assertFalse(report["ok"])
        self.assertEqual(report["billing"]["usage_row_count"], 1)
        self.assertEqual(report["billing"]["billing_dedup_row_count"], 1)
        self.assertTrue(report["billing"]["tokens_match_stream"])
        self.assertNotIn(TOKEN, (scope / M.JOURNAL).read_text())
        model.assert_called_once()
        exact = [args.args[0] for args in sql.call_args_list if "FROM usage_logs" in args.args[0]]
        self.assertEqual(len(exact), 1)
        self.assertIn("client:" + CLIENT, exact[0])

    def test_unknown_stream_result_and_existing_journal_never_repost(self):
        base, _, _, model = self.worker(stream_failure="model-stream-result-unavailable-no-retry")
        report = M.remote_apply(payload(), base)
        self.assertFalse(report["ok"])
        self.assertTrue(report["request_attempted"])
        with self.assertRaisesRegex(M.Refusal, "reserved-inspect-evidence-no-repeat"):
            M.remote_apply(payload(), base)
        model.assert_called_once()

    def test_preflight_stale_and_fsync_failure_prevent_post(self):
        base, _, _, model = self.worker()
        data = payload()
        data["target_preflight"]["checked_at"] -= 100
        self.assertEqual(M.remote_apply(data, base)["error"], "target-proof-stale")
        model.assert_not_called()
        base, _, _, model = self.worker(fail_intent=True)
        with self.assertRaises(OSError):
            M.remote_apply(payload(), base)
        model.assert_not_called()

    def test_duplicate_usage_dedup_or_token_mismatch_fail_acceptance(self):
        for key in ("usage_count", "dedup_count"):
            base, _, _, model = self.worker(sql_change=lambda value, key=key: value.update({key: 2}))
            report = M.remote_apply(payload(), base)
            self.assertEqual(report["error"], "duplicate-exact-request-billing")
            model.assert_called_once()
        base, _, _, _ = self.worker(sql_change=lambda value: value["rows"][0].update(output_tokens=6))
        self.assertEqual(M.remote_apply(payload(), base)["error"], "billing-token-count-mismatch")

    def test_finalize_requires_same_generation_and_never_sends_a_request(self):
        base, scope, _, model = self.worker()
        report = M.remote_apply(payload(), base)
        with mock.patch.object(M, "root_read", side_effect=lambda path: pathlib.Path(path).read_bytes()):
            wrong = proof()
            wrong["instance_id"] = "different"
            with self.assertRaisesRegex(M.Refusal, "generation-or-role-changed"):
                M.remote_finalize({"request_id": report["request_id"], "target_postflight": wrong}, base)
            complete = M.remote_finalize({"request_id": report["request_id"], "target_postflight": proof()}, base)
            self.assertTrue(complete["ok"])
            self.assertEqual(complete["phase"], "COMPLETE")
            self.assertFalse(complete["awaiting_target_postflight"])
            model.assert_called_once()
            self.assertEqual(json.loads((scope / M.JOURNAL).read_bytes())["phase"], "COMPLETE")

    def test_inspect_reads_no_token_rpc_sql_or_http(self):
        with mock.patch.object(M, "aws_root"), mock.patch.object(M, "private_root_dir"), \
             mock.patch.object(M, "root_read", return_value=b'{"phase":"POST_INTENT"}'), \
             mock.patch.object(M, "load_base", side_effect=AssertionError("no base")), \
             mock.patch.object(M, "load_target", side_effect=AssertionError("no RPC")):
            self.assertEqual(M.remote_inspect(), {"phase": "POST_INTENT"})

    def test_remote_secret_uses_only_ssh_stdin_and_suppresses_stderr(self):
        with mock.patch.object(M.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b'{"ok":false}', b'private stderr')) as run:
            M.remote(["ssh", "fixed-aws"], "apply", payload())
        self.assertNotIn(TOKEN, repr(run.call_args.args[0]))
        self.assertIn(TOKEN.encode(), run.call_args.kwargs["input"])
        self.assertNotIn("env", run.call_args.kwargs)

    def test_local_apply_performs_one_billable_worker_with_two_target_proofs(self):
        base = M.load_base()
        calls = []
        def remote(ssh, action, data=None):
            calls.append((action, data))
            if action == "target":
                return proof()
            if action == "apply":
                return {"phase": "BILLING_VERIFIED", "awaiting_target_postflight": True}
            self.assertEqual(action, "finalize")
            return {"ok": True, "phase": "COMPLETE"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(M.pathlib.Path, "home", return_value=pathlib.Path(tmp).resolve()), \
             mock.patch.object(M, "load_base", return_value=base), mock.patch.object(base, "load_token", return_value=TOKEN), \
             mock.patch.object(M, "target_ssh", return_value=["ssh", "fixed-target"]), mock.patch.object(M, "remote", side_effect=remote), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main(["--apply"]), 0)
            self.assertEqual([action for action, _ in calls], ["target", "apply", "target", "finalize"])
            self.assertNotIn(TOKEN, out.getvalue())
            for artifact in pathlib.Path(tmp).rglob("*.json"):
                self.assertNotIn(TOKEN, artifact.read_text())


if __name__ == "__main__":
    unittest.main()
