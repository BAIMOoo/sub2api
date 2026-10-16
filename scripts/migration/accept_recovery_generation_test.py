#!/usr/bin/env python3
"""Offline acceptance fault tests; no SSH, Docker, credentials or host starts."""
import base64
import contextlib
import copy
import importlib.util
import io
import pathlib
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


M, C, E = load("accept_recovery_generation"), load("recovery_generation_canary"), load("durable_runner")


def target(now):
    return {"v": 1, "run": M.RUN, "owner": M.OWNER, "app_container_id": C.APP_ID, "app_image_id": C.APP_IMAGE,
        "app_config_sha256": "a" * 64, "app_pid": 534792, "instance_id": "new-generation", "control_port": 18093,
        "ready": True, "writer_failures": 0, "writer_dropped": 0, "background_workers_enabled": False,
        "persistent_endpoints": C.ENDPOINTS, "pg_authority": "AWS", "redis_authority": "AWS",
        "pg_container_id": C.PG_ID, "pg_image_id": C.PG_IMAGE, "system_identifier": C.SYSTEM_ID,
        "pg_in_recovery": True, "redis_role": "slave", "redis_run_id": "f" * 40,
        "redis_master_link_up": True, "adoption_sha256": "b" * 64, "controller_config_sha256": "c" * 64,
        "target_ledger_sha256": M.LEDGER_SHA, "ledger_epoch": 14, "public_route_proven": True, "checked_at": now}


def delta():
    output = "Ran one useful test\nOK\n"
    return {"v": 1, "run": M.RUN, "owner": M.OWNER, "ok": True, "tested_at": 1000,
        "programs": {name: M.RPC_SHA if name == "target_rpc.py" else M.GUARD_SHA if name == "runner_guard.py" else "d" * 64
                     for name in M.SOURCE_PROGRAMS},
        "tests": [{"file": name, "test_sha256": "f" * 64, "exit_code": 0,
                   "output": output, "output_sha256": M.sha(output.encode())} for name in sorted(M.TEST_FILES)]}


def fixture(now):
    records = {}
    initial = {"v": 1, "run": M.RUN, "owner": M.OWNER, "phase": "SOURCE_DATA", "attempt": 1,
        "pause_seconds": 7.082038551568985, "window": None, "pg": "AWS", "redis": "AWS",
        "route_fenced": True, "quiet_instance_id": "old-generation", "deadline_breached": False}

    def record(identifier, previous, proof_sha):
        row = {"v": 1, "run": M.RUN, "owner": M.OWNER, "id": identifier, "previous_state": copy.deepcopy(previous),
               "previous_state_sha256": E.digest(previous), "proof_sha256": proof_sha}
        raw = M.canonical(row)
        records[identifier] = raw
        return {"id": identifier, "record_sha256": M.sha(raw)}

    previous = {**initial, "attempt": 3, "pause_seconds": M.LIFETIME_PAUSE,
                "budget_cohort": record("1" * 32, initial, "a" * 64)}
    proof = target(now)
    drain = {"v": 1, "run": M.RUN, "owner": M.OWNER, "new_instance_id": proof["instance_id"],
        "old_instance_id": "old-generation", "new_container_id": C.APP_ID, "adoption_sha256": proof["adoption_sha256"],
        "ledger_sha256": M.LEDGER_SHA, "ledger_epoch": 14}
    drain_raw = M.canonical(drain)
    reference = record("2" * 32, previous, M.sha(drain_raw))
    state = {**previous, "budget_cohort": reference}
    engine = types.SimpleNamespace(digest=E.digest,
        budget_accounting=lambda value: E.budget_accounting(value, lambda ref: records[ref["id"]]))
    budget = engine.budget_accounting(state)
    cohort = {"ok": True, "cohort_id": reference["id"], "budget": budget, "request_pause": False,
        "main_started": False, "quiet_instance_id_preserved": True, "lifetime_pause_seconds_preserved": True,
        "operation_ledgers_preserved": True}
    source_cohort = {**reference, "proof_sha256": M.sha(drain_raw), "adoption_sha256": proof["adoption_sha256"],
                     "instance_id": proof["instance_id"]}
    client = "01234567-89ab-4cde-8f01-23456789abcd"
    report = {"v": 1, "run": M.RUN, "lane": "recovery-generation", "phase": "COMPLETE", "ok": True,
        "awaiting_target_postflight": False, "request_attempted": True, "post_intent_durable": True,
        "no_automatic_retry": True, "production_routes_changed": False, "finished_unix_seconds": now - 2,
        "target_preflight": {**proof, "checked_at": now - 10}, "target_postflight": {**proof, "checked_at": now - 3},
        "source_cohort": source_cohort,
        "stream": {"terminal_event": "response.completed", "eof_after_terminal": True, "effective_client_request_id": client},
        "billing": {"usage_row_count": 1, "billing_dedup_row_count": 1, "tokens_match_stream": True, "request_id": "client:" + client},
        **{name: {"primary": True, "system_identifier": C.SYSTEM_ID}
           for name in ("source_preflight", "source_postflight", "source_finalflight")}}
    gates = {"v": 1, "run": M.RUN, "owner": M.OWNER, "accepted": True, "app_image_id": C.APP_IMAGE,
        "app_binary_sha256": "0bdcaa371aa45abfa1eb61c763d2da10f133d718c4ad93251587ca0cc6191724",
        "rehearsal_sha256": "4" * 64, "live_canary_sha256": "5" * 64, "entry_tls_sha256": "6" * 64,
        "source_deployment_paths_fenced": True}
    snapshot = {name: M.canonical({"fixed": name}) for name in M.FROZEN}
    snapshot.update({"state.json": M.canonical(state), "transport-state.json": M.canonical({"target_epoch": 14}),
        "deployment-gates.json": b"  " + M.canonical(gates), "cohort-record.json": records[reference["id"]],
        "cohort-verification.json": M.canonical(cohort), "cohort-drain-proof.json": drain_raw,
        "historical-r2-result.json": M.canonical({"ok": True, "historical": "r2"}),
        "historical-r2-closure.json": M.canonical({"ok": True, "selftest_disarmed": True}),
        "delta-tests.json": M.canonical(delta()), "canary.json": M.canonical(report),
        "guard": {"v": 1, "run": M.RUN, "owner": M.OWNER, "checked_at": now, "healthy": True,
                  "tripped": False, "recovering": False, "pg_wal_protected": True, "sample": {"mode": "RUNNING_BOUNDED"}}})
    return snapshot, proof, engine


class Host:
    def __init__(self, snapshot, proof, engine, now):
        self.current, self.proof, self.engine, self.clock = copy.deepcopy(snapshot), proof, engine, now
        self.canary, self.main_unit = C, "sub2api-migration-runner-20261016.service"
        self.files, self.events, self.locked, self.created = {}, [], False, False
        self.starts, self.fail_write, self.lost_start, self.recheck_failure, self.cas_failure = 0, None, False, False, False

    def now(self):
        return self.clock

    @contextlib.contextmanager
    def execution_lock(self):
        assert not self.locked
        self.locked = True
        self.events.append("lock")
        try:
            yield
        finally:
            self.locked = False
            self.events.append("unlock")

    def exists(self):
        return self.created

    def snapshot(self):
        assert self.locked
        return copy.deepcopy(self.current)

    def create(self):
        assert self.locked
        self.created = True

    def write(self, name, raw):
        if self.fail_write == name:
            raise OSError("fsync failed")
        assert name not in self.files
        if name == "main-start.intent.json":
            assert self.locked
        self.files[name] = bytes(raw)
        self.events.append("write:" + name)

    def recheck(self, snapshot, gates):
        assert self.locked
        M.need(not self.recheck_failure, "source-frozen-file-CAS-changed")
        M.need(all(self.current[name] == snapshot[name] for name in M.FROZEN), "source-frozen-file-CAS-changed")
        M.need(self.current["deployment-gates.json"] == gates, "deployment-gates-CAS-changed")

    def cas_gates(self, before, after):
        assert self.locked
        M.need(not self.cas_failure and self.current["deployment-gates.json"] == before, "deployment-gates-CAS-changed")
        self.current["deployment-gates.json"] = bytes(after)
        self.events.append("gates-CAS")

    def start_main(self):
        assert not self.locked
        assert "main-start.intent.json" in self.files
        assert "gates-update.complete.json" in self.files
        self.starts += 1
        self.events.append("start")
        if self.lost_start:
            raise TimeoutError("lost reply after systemd accepted start")

    def observe_main(self):
        return {"running": True, "quiescent": False}


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.snapshot, self.proof, self.engine = fixture(self.now)
        self.host = Host(self.snapshot, self.proof, self.engine, self.now)
        self.patches = [mock.patch.object(M, "R2_SHA", M.sha(self.snapshot["historical-r2-result.json"])),
                        mock.patch.object(M, "R2_CLOSURE_SHA", M.sha(self.snapshot["historical-r2-closure.json"])),
                        mock.patch.object(M, "DELTA_SHA", M.sha(self.snapshot["delta-tests.json"]))]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def change(self, file, callback):
        value = M.strict(self.host.current[file])
        callback(value)
        self.host.current[file] = M.canonical(value)

    def test_offline_default_never_reads_or_contacts_hosts(self):
        with mock.patch.object(M.pathlib.Path, "read_bytes", side_effect=AssertionError("offline")), \
             mock.patch.object(M.subprocess, "run", side_effect=AssertionError("offline")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
        self.assertTrue(M.strict(out.getvalue())["offline"])

    def test_acceptance_archives_exact_bytes_changes_only_two_gates_and_starts_after_unlock(self):
        result = M.accept_generation(self.host, self.proof)
        self.assertTrue(result["ok"])
        self.assertEqual(self.host.files["deployment-gates.json"], self.snapshot["deployment-gates.json"])
        for name in M.FROZEN:
            self.assertEqual(self.host.current[name], self.snapshot[name])
            self.assertEqual(self.host.files[name], self.snapshot[name])
        before, after = map(M.strict, (self.snapshot["deployment-gates.json"], self.host.current["deployment-gates.json"]))
        self.assertEqual({k: v for k, v in before.items() if k not in ("rehearsal_sha256", "live_canary_sha256")},
                         {k: v for k, v in after.items() if k not in ("rehearsal_sha256", "live_canary_sha256")})
        receipt = M.strict(self.host.files["generation-acceptance.json"])
        self.assertEqual(after["live_canary_sha256"], M.sha(M.canonical(receipt["live_evidence"])))
        self.assertEqual(after["rehearsal_sha256"], M.sha(M.canonical(receipt["rehearsal_evidence"])))
        self.assertEqual(receipt["budget"]["total_pause_seconds"], M.LIFETIME_PAUSE)
        self.assertEqual(receipt["budget"]["cohort_pause_seconds"], 0)
        self.assertGreater(receipt["budget"]["max_cohort_pause_seconds"], 7)
        self.assertEqual(self.host.events[-3:], ["unlock", "start", "write:main-start.observation.json"])

    def test_completed_acceptance_cannot_start_main_twice(self):
        M.accept_generation(self.host, self.proof)
        with self.assertRaisesRegex(M.Refusal, "already-attempted-inspect-only"):
            M.accept_generation(self.host, self.proof)
        self.assertEqual(self.host.starts, 1)

    def test_lost_start_reply_preserves_intent_and_never_repeats(self):
        self.host.lost_start = True
        result = M.accept_generation(self.host, self.proof)
        self.assertFalse(result["ok"])
        self.assertEqual(result["start_outcome"], "unknown-inspect-only")
        with self.assertRaises(M.Refusal):
            M.accept_generation(self.host, self.proof)
        self.assertEqual(self.host.starts, 1)
        self.assertTrue(M.strict(self.host.files["main-start.intent.json"])["no_repeat"])

    def test_write_fsync_failures_at_each_boundary_prevent_start_and_replay(self):
        for name in ("deployment-gates.json", "generation-acceptance.json", "gates-update.intent.json",
                     "gates-update.complete.json", "main-start.intent.json"):
            host = Host(self.snapshot, self.proof, self.engine, self.now)
            host.fail_write = name
            with self.subTest(name=name), self.assertRaises(OSError):
                M.accept_generation(host, self.proof)
            self.assertEqual(host.starts, 0)
            with self.assertRaisesRegex(M.Refusal, "already-attempted"):
                M.accept_generation(host, self.proof)

    def test_source_or_gates_CAS_failure_never_starts(self):
        for field in ("recheck_failure", "cas_failure"):
            host = Host(self.snapshot, self.proof, self.engine, self.now)
            setattr(host, field, True)
            with self.subTest(field=field), self.assertRaisesRegex(M.Refusal, "CAS-changed"):
                M.accept_generation(host, self.proof)
            self.assertEqual(host.starts, 0)
            self.assertNotIn("main-start.intent.json", host.files)

    def test_source_boundary_regressions_refuse_before_any_write(self):
        for key, value in (("attempt", 4), ("pg", "TENCENT"), ("redis", "COMMIT_INTENT"), ("window", {}),
                           ("pause_seconds", 0), ("deadline_breached", True), ("budget_accounting_unknown", True),
                           ("quiet_instance_id", "changed-old-generation")):
            host = copy.copy(self.host)
            host.current = copy.deepcopy(self.snapshot)
            state = M.strict(host.current["state.json"])
            state[key] = value
            host.current["state.json"] = M.canonical(state)
            with self.subTest(key=key), self.assertRaises((M.Refusal, E.Refusal)):
                M.accept_generation(host, self.proof)
            self.assertFalse(host.created)

    def test_guard_stale_trip_recovery_wrong_mode_or_wal_loss_refuse_before_write(self):
        for key, value in (("checked_at", self.now - 1.1), ("tripped", True), ("recovering", True),
                           ("healthy", False), ("pg_wal_protected", False), ("sample", {"mode": "UNPROTECTED"})):
            host = Host(self.snapshot, self.proof, self.engine, self.now)
            host.current["guard"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(M.Refusal, "fresh-healthy"):
                M.accept_generation(host, self.proof)
            self.assertFalse(host.created)

    def test_changed_cohort_record_or_history_is_refused(self):
        self.change("cohort-record.json", lambda v: v.update(proof_sha256="9" * 64))
        with self.assertRaisesRegex(M.Refusal, "cohort"):
            M.accept_generation(self.host, self.proof)
        self.assertFalse(self.host.created)

    def test_canary_wrong_generation_cohort_billing_incomplete_or_old_is_refused(self):
        mutations = [lambda r: r.update(phase="BILLING_VERIFIED"), lambda r: r.update(ok=False),
            lambda r: r.update(finished_unix_seconds=self.now - 301),
            lambda r: r["target_postflight"].update(instance_id="other-generation"),
            lambda r: r["source_cohort"].update(id="3" * 32),
            lambda r: r["billing"].update(billing_dedup_row_count=2),
            lambda r: r["stream"].update(eof_after_terminal=False),
            lambda r: r["source_finalflight"].update(primary=False)]
        for mutate in mutations:
            host = Host(self.snapshot, self.proof, self.engine, self.now)
            report = M.strict(host.current["canary.json"])
            mutate(report)
            host.current["canary.json"] = M.canonical(report)
            with self.subTest(mutate=mutate), self.assertRaises(M.Refusal):
                M.accept_generation(host, self.proof)
            self.assertFalse(host.created)

    def test_changed_historical_r2_or_delta_hash_is_refused(self):
        for file in ("historical-r2-result.json", "historical-r2-closure.json", "delta-tests.json"):
            host = Host(self.snapshot, self.proof, self.engine, self.now)
            host.current[file] += b" "
            with self.subTest(file=file), self.assertRaises(M.Refusal):
                M.accept_generation(host, self.proof)
            self.assertFalse(host.created)

    def test_target_old_instance_wrong_endpoints_ledger_or_stale_proof_refused(self):
        for key, value in (("checked_at", self.now - 21), ("target_ledger_sha256", "0" * 64),
                           ("ledger_epoch", 15), ("app_container_id", C.OLD_APP_ID), ("background_workers_enabled", True),
                           ("persistent_endpoints", {"v": 1, "postgres": "target", "redis": "source"})):
            proof = {**self.proof, key: value}
            with self.subTest(key=key), self.assertRaises((M.Refusal, C.Refusal)):
                M.accept_generation(self.host, proof)
            self.assertFalse(self.host.created)

    def test_target_probe_requires_all_complete_original_raw_ledger_and_no_read_mutation(self):
        raw = M.canonical({"epoch": 14, "operations": {"one": {"phase": "COMPLETE"}}})
        proof = {**self.proof, "target_ledger_sha256": M.sha(raw)}
        c = types.SimpleNamespace(target_preflight=mock.Mock(return_value=proof), validate_target_proof=lambda p: None)
        with mock.patch.object(M, "LEDGER_SHA", M.sha(raw)), mock.patch.object(M, "root_read", side_effect=[raw, raw]), \
             mock.patch.object(M.os, "geteuid", return_value=0), mock.patch.object(M.sys, "platform", "linux"):
            self.assertEqual(M.target_check(c), proof)
        for rows in ([raw, raw + b" "], [M.canonical({"epoch": 14, "operations": {"one": {"phase": "PENDING"}}})]):
            with self.subTest(rows=rows), mock.patch.object(M, "LEDGER_SHA", M.sha(rows[0])), \
                 mock.patch.object(M, "root_read", side_effect=rows), mock.patch.object(M.os, "geteuid", return_value=0), \
                 mock.patch.object(M.sys, "platform", "linux"), self.assertRaises(M.Refusal):
                M.target_check(c)

    def test_delta_failures_or_modified_output_are_not_acceptance(self):
        value = delta()
        for mutate in (lambda v: v["tests"][0].update(exit_code=1),
                       lambda v: v["tests"][0].update(output="different"), lambda v: v["tests"].pop()):
            row = copy.deepcopy(value)
            mutate(row)
            raw = M.canonical(row)
            with mock.patch.object(M, "DELTA_SHA", M.sha(raw)), self.assertRaises(M.Refusal):
                M.validate_delta(raw)

    def test_frozen_canary_bytes_and_both_generated_workers_compile(self):
        self.assertEqual(M.sha((HERE / "recovery_generation_canary.py").read_bytes()), M.CANARY_SHA)
        payload = {"canary_program": base64.b64encode(b"reviewed").decode(), "delta": "e30=", "target": self.proof}
        for action in ("source", "target", "inspect"):
            compile(M.remote_source(action, payload), "generated-" + action, "exec")

    def test_real_source_gate_CAS_preserves_original_and_uses_private_atomic_file(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            scope = pathlib.Path(directory)
            saved = scope / "acceptance"
            saved.mkdir(mode=0o700)
            path = scope / "deployment-gates.json"
            before = self.snapshot["deployment-gates.json"]
            path.write_bytes(before)
            after = M.canonical({**M.strict(before), "live_canary_sha256": "9" * 64})
            host = M.SourceHost.__new__(M.SourceHost)
            host.backend = types.SimpleNamespace(store=types.SimpleNamespace(lock=contextlib.nullcontext))
            host.service = types.SimpleNamespace(read_gates=mock.Mock())
            with mock.patch.object(M, "SCOPE", scope), mock.patch.object(M, "SAVED", saved), \
                 mock.patch.object(M, "root_read", side_effect=lambda p: pathlib.Path(p).read_bytes()):
                with self.assertRaisesRegex(M.Refusal, "CAS-changed"):
                    host.cas_gates(before + b" ", after)
                self.assertEqual(path.read_bytes(), before)
                host.cas_gates(before, after)
            self.assertEqual(path.read_bytes(), after)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse((saved / "deployment-gates.pending.json").exists())
            host.service.read_gates.assert_called_once_with(M.OWNER)

    def test_real_source_preflight_refuses_main_selftest_or_irreversible_source_intent(self):
        host = M.SourceHost.__new__(M.SourceHost)
        host.clock = self.now
        host.now = lambda: self.now
        sample = {**self.snapshot["guard"], "boot_id": "boot-one"}
        source_ops = mock.Mock()
        source_ops._guard.return_value = sample
        source_ops._ledger.return_value = {"operations": {}}
        resources = {kind: {"State": {"Pid": pid, "Running": kind != "app", "OOMKilled": False,
            "Status": "running" if kind != "app" else "exited", "ExitCode": 0},
            "HostConfig": {"RestartPolicy": {"Name": "no" if kind == "app" else "unless-stopped"}}}
            for kind, pid in (("app", 0), ("postgres", 972839), ("redis", 972827))}
        source_ops._resource.side_effect = lambda kind: resources[kind]
        host.backend = types.SimpleNamespace(source_ops=source_ops)
        host.guard = types.SimpleNamespace(main_status=mock.Mock(return_value={"quiescent": True}), boot_id=lambda: "boot-one")
        host.service = types.SimpleNamespace(selftest_armed=mock.Mock(return_value=False), verify_entry=lambda: None)
        with mock.patch.object(pathlib.Path, "exists", return_value=False), \
             mock.patch.object(pathlib.Path, "is_symlink", return_value=False):
            self.assertEqual(host.check_boundary(), sample)
            host.service.selftest_armed.return_value = True
            with self.assertRaisesRegex(M.Refusal, "main-or-selftest"):
                host.check_boundary()
            host.service.selftest_armed.return_value = False
            host.guard.main_status.return_value = {"quiescent": False}
            with self.assertRaisesRegex(M.Refusal, "main-or-selftest"):
                host.check_boundary()
            host.guard.main_status.return_value = {"quiescent": True}
            for action in ("pg_stop", "redis_failover", "redis_role_persist"):
                source_ops._ledger.return_value = {"operations": {"pending": {"request": {"action": action}}}}
                with self.subTest(action=action), self.assertRaisesRegex(M.Refusal, "irreversible"):
                    host.check_boundary()

    def test_local_lost_source_SSH_reply_leaves_durable_marker_and_cannot_resubmit_source(self):
        calls = []
        def remote(ssh, action, payload):
            calls.append(action)
            if action == "source":
                raise M.Refusal("SSH-outcome-unknown")
            return self.proof

        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            folder = pathlib.Path(directory)
            (folder / M.DELTA_FILE).write_bytes(self.snapshot["delta-tests.json"])
            with mock.patch.object(M, "FOLDER", folder), mock.patch.object(M, "load_canary"), \
                 mock.patch.object(M, "validate_delta"), mock.patch.object(M, "remote", side_effect=remote):
                with self.assertRaisesRegex(M.Refusal, "SSH-outcome-unknown"):
                    M.main(["--apply"])
                self.assertTrue((folder / "generation-acceptance-20261017.intent.json").is_file())
                self.assertTrue((folder / "generation-acceptance-20261017.unknown.json").is_file())
                with self.assertRaisesRegex(M.Refusal, "already-attempted"):
                    M.main(["--apply"])
            self.assertEqual(calls.count("source"), 1)

    def test_main_start_adapter_submits_only_one_nonblocking_start(self):
        host = M.SourceHost.__new__(M.SourceHost)
        host.main_unit = "sub2api-migration-runner-20261016.service"
        with mock.patch.object(M.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as command:
            host.start_main()
        command.assert_called_once_with(["/usr/bin/systemctl", "start", "--no-block", host.main_unit],
                                       input=b"", capture_output=True, timeout=3)


if __name__ == "__main__":
    unittest.main()
