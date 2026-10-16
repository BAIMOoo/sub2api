"""Fail-closed authorization checks for a new, independently drained cohort."""
import copy
import contextlib
import ast
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import tempfile
import types
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("prepare_retry", pathlib.Path(__file__).with_name("prepare_retry.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
engine_spec = importlib.util.spec_from_file_location("retry_test_engine", pathlib.Path(__file__).with_name("durable_runner.py"))
E = importlib.util.module_from_spec(engine_spec)
engine_spec.loader.exec_module(E)


class RetryAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.ns = {"payload": {"owner": M.OWNER, "prior_pause": M.PRIOR_PAUSE}}
        exec(M.VALIDATION, self.ns)
        ingress = {"paused": False, "epoch": 8, "queued_requests": 0}
        self.proof = {"v": 1, "run": "20261016", "owner": M.OWNER, "checked_at": 100,
            "runtime": {"instance_id": "r23hcagq4x9cyq", "ready": True, "serving": True,
                "draining": False, "background_workers_enabled": False, "active_requests": 0,
                "active_hijacked_connections": 0, "ingress": copy.deepcopy(ingress)},
            "ingress_before": copy.deepcopy(ingress), "ingress_after": copy.deepcopy(ingress),
            "restore_ingress_epoch": 8, "pg_authority": "AWS", "redis_authority": "AWS",
            "pg_in_recovery": True, "redis_role": "slave", "arm_cancelled": True,
            "promotion_intent": None, "all_operations_complete": True, "all_quiesces_cancelled": True,
            "endpoints": {"v": 1, "postgres": "source", "redis": "source"},
            "app_pid": 257639, "pg_pid": 180813, "redis_pid": 133623}

    def test_offline_default_never_accesses_remote_or_files(self):
        out = io.StringIO()
        with mock.patch.object(M.subprocess, "run", side_effect=AssertionError("remote")), \
             mock.patch.object(M.pathlib.Path, "read_bytes", side_effect=AssertionError("file")), \
             contextlib.redirect_stdout(out):
            M.main([])
        answer = json.loads(out.getvalue())
        self.assertTrue(answer["offline"])
        self.assertFalse(answer["request_pauses"])
        self.assertFalse(answer["global_pause_history_reset"])

    def test_remote_programs_compile_without_bytecode_writes(self):
        for code in (M.SOURCE_RECONCILE_APP, M.TARGET_UPGRADE, M.SOURCE_UPGRADE, M.TARGET_DRAIN, M.SOURCE_AUTHORIZE):
            script = M.COMMON + M.VALIDATION + code
            compile(script, "retry-remote", "exec")
            self.assertLess(script.index("sys.dont_write_bytecode=True"), script.index("=load("))

    def test_fresh_atomic_zero_snapshot_is_accepted(self):
        self.ns["drain_proof"](self.proof, 101)

    def test_waiters_active_http_and_live_websockets_each_block_new_budget(self):
        for kind in ("queued_requests", "active_requests", "active_hijacked_connections"):
            with self.subTest(kind=kind):
                value = copy.deepcopy(self.proof)
                if kind == "queued_requests":
                    for row in (value["runtime"]["ingress"], value["ingress_before"], value["ingress_after"]):
                        row[kind] = 1
                else:
                    value["runtime"][kind] = 1
                with self.assertRaisesRegex(RuntimeError, "not-atomically-drained"):
                    self.ns["drain_proof"](value, 101)

    def test_nonatomic_zero_samples_and_epoch_changes_are_rejected(self):
        for component in ("ingress_before", "ingress_after", "runtime"):
            with self.subTest(component=component):
                value = copy.deepcopy(self.proof)
                row = value[component]["ingress"] if component == "runtime" else value[component]
                row["epoch"] = 9
                with self.assertRaises(RuntimeError):
                    self.ns["drain_proof"](value, 101)

    def test_promoted_unknown_armed_and_restarted_target_cannot_start_cohort(self):
        invalid = {"pg_authority": "TENCENT", "redis_role": "master", "arm_cancelled": False,
                   "promotion_intent": {"op_id": "intent"}, "all_operations_complete": False,
                   "all_quiesces_cancelled": False, "app_pid": 1}
        for key, bad in invalid.items():
            with self.subTest(key=key):
                value = copy.deepcopy(self.proof); value[key] = bad
                with self.assertRaisesRegex(RuntimeError, "reversible-abort-boundary"):
                    self.ns["drain_proof"](value, 101)
        for now in (99, 121, float("nan")):
            with self.subTest(now=now), self.assertRaises(RuntimeError):
                self.ns["drain_proof"](self.proof, now)

    def test_only_exact_aborted_checkpoint_keeps_original_pause_history(self):
        state = {"v": 1, "run": "20261016", "owner": M.OWNER, "phase": "SOURCE_DATA",
                 "attempt": 1, "route_fenced": True, "pg": "AWS", "redis": "AWS", "window": None,
                 "deadline_breached": False, "pause_seconds": M.PRIOR_PAUSE}
        self.ns["previous_state"](state)
        for fields in ({"pause_seconds": 0}, {"attempt": 0}, {"window": {"kind": "combined"}},
                       {"deadline_breached": True}, {"budget_cohort": {}}, {"pg": "COMMIT_INTENT"}):
            with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                self.ns["previous_state"]({**state, **fields})

    @contextlib.contextmanager
    def authorization_environment(self, *, irreversible=None, change_at_lock=False,
                                  monitors_retired=True, times=(101, 101), restart_override=None):
        """Execute the actual authorizer with only trusted reads/backend mocked.

        Files, exclusive creates, fsyncs, record hashes, accounting and both raw
        checkpoint CAS checks run normally inside an isolated temporary root.
        The fake backend exposes inspections and the one state-reference write;
        any lifecycle action fails the test immediately.
        """
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            scope, lib = root / "runner", root / "lib"
            scope.mkdir(mode=0o700); lib.mkdir(mode=0o700)
            canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            sha = lambda raw: hashlib.sha256(raw).hexdigest()
            state = {"v": 1, "run": "20261016", "owner": M.OWNER, "phase": "SOURCE_DATA",
                "attempt": 1, "route_fenced": True, "pg": "AWS", "redis": "AWS", "window": None,
                "deadline_breached": False, "pause_seconds": M.PRIOR_PAUSE,
                "quiet_instance_id": self.proof["runtime"]["instance_id"]}
            state_path = scope / "state.json"
            oldraw = canonical(state) + b"\n"
            state_path.write_bytes(oldraw)
            dependency, program = b"dependency fixture", b"program fixture"
            (lib / "runner_backend.py").write_bytes(dependency)
            (lib / "runner_service.py").write_bytes(program)
            payload = {"owner": M.OWNER, "prior_pause": M.PRIOR_PAUSE,
                "dependencies": {"runner_backend.py": sha(dependency)},
                "new": {"runner_service.py": sha(program)}, "proof": copy.deepcopy(self.proof),
                "state_raw_sha256": sha(oldraw), "cohort_id": "a" * 32}
            events, writes, effects = [], [], []
            operations = {"stopped": {"request": {"action": "app_stop"}, "phase": "complete", "steps": {"sigterm": "accepted"}}}
            if irreversible:
                operations["irreversible"] = {"request": {"action": irreversible}, "phase": "intent", "steps": {}}

            class Source:
                def _assert_monitors_retired(self):
                    events.append("monitor-check")
                    if not monitors_retired:
                        raise RuntimeError("SOURCE_MONITOR_NOT_IDLE_MASKED")

                def _guard(self):
                    return {"recovering": False, "tripped": False}

                def _resource(self, kind):
                    restart = "no" if kind == "app" else "unless-stopped"
                    if restart_override and restart_override[0] == kind:
                        restart = restart_override[1]
                    return {"HostConfig": {"RestartPolicy": {"Name": restart}}, "State": {
                        "Pid": {"app": 0, "postgres": 972839, "redis": 972827}[kind],
                        "Running": kind != "app", "OOMKilled": False, "Status": "exited" if kind == "app" else "running", "ExitCode": 0}}

                def _ledger(self):
                    return {"operations": copy.deepcopy(operations)}

                def __getattr__(self, name):
                    effects.append(name)
                    raise AssertionError("unexpected lifecycle action: " + name)

            class Store:
                locked = False

                def read(self, name):
                    if name != "state":
                        raise AssertionError("unexpected store read")
                    return json.loads(state_path.read_bytes())

                @contextlib.contextmanager
                def lock(self):
                    events.append("state-lock")
                    if change_at_lock:
                        state_path.write_bytes(state_path.read_bytes() + b" ")
                    self.locked = True
                    try:
                        yield
                    finally:
                        self.locked = False

                def _write(self, name, value):
                    if name != "state" or not self.locked:
                        raise AssertionError("unexpected or unlocked state write")
                    cid = value["budget_cohort"]["id"]
                    recordraw = (scope / "budget-cohorts" / (cid + ".json")).read_bytes()
                    proofraw = (scope / ("request-cohort-" + cid) / "drain-proof.json").read_bytes()
                    record = json.loads(recordraw)
                    if sha(recordraw) != value["budget_cohort"]["record_sha256"] or sha(proofraw) != record["proof_sha256"]:
                        raise AssertionError("state activated before durable bound evidence")
                    if (scope / ("request-cohort-" + cid) / "previous-state.json").read_bytes() != oldraw:
                        raise AssertionError("raw previous checkpoint was not preserved")
                    events.append("activate-state-reference")
                    writes.append(copy.deepcopy(value))
                    state_path.write_bytes(canonical(value) + b"\n")

            store = Store()
            backend = types.SimpleNamespace(store=store, source_ops=Source())

            @contextlib.contextmanager
            def execution_lock():
                events.append("execution-lock")
                yield

            def accounting(value):
                events.append("validate-accounting")
                return E.budget_accounting(value, lambda ref: (scope / "budget-cohorts" / (ref["id"] + ".json")).read_bytes())

            service = types.SimpleNamespace(B=types.SimpleNamespace(RunnerBackend=lambda: backend),
                E=types.SimpleNamespace(budget_accounting=accounting), execution_lock=execution_lock,
                selftest_armed=lambda owner: False, verify_entry=lambda: events.append("entry-check"))
            guard = types.SimpleNamespace(LocalBackend=lambda: types.SimpleNamespace(main_status=lambda: {"quiescent": True}))

            def load(name, path):
                if name == "retry_service_new" and path == lib / "runner_service.py":
                    return service
                if name == "retry_guard" and path == lib / "runner_guard.py":
                    return guard
                raise AssertionError("unexpected dynamic import")

            ns = {"payload": payload, "lib": lib, "scope": scope, "sha": sha,
                  "os": os, "json": json, "stat": stat, "time": types.SimpleNamespace(time=mock.Mock(side_effect=times)),
                  "load": load, "trusted": lambda path, mode: path.read_bytes()}
            # Exercise real helper implementations while replacing only the
            # top-level production paths/imports and trusted-reader boundary.
            definitions = [node for node in ast.parse(M.COMMON).body if isinstance(node, ast.FunctionDef) and
                           node.name in {"canonical", "sync", "write_new", "source_idle"}]
            exec(compile(ast.Module(body=definitions, type_ignores=[]), "retry-test-helpers", "exec"), ns)
            exec(M.VALIDATION, ns)
            write_new = ns["write_new"]

            def record_write(path, raw, mode=0o600):
                if root not in path.parents:
                    raise AssertionError("authorizer escaped isolated filesystem")
                events.append("write:" + path.name)
                return write_new(path, raw, mode)

            ns["write_new"] = record_write
            real_lstat = pathlib.Path.lstat

            def root_owned_cohort_directory(path):
                value = real_lstat(path)
                if path == scope / "budget-cohorts":
                    return types.SimpleNamespace(st_mode=value.st_mode, st_uid=0)
                return value

            fixture = types.SimpleNamespace(ns=ns, payload=payload, state=state, state_path=state_path,
                scope=scope, oldraw=oldraw, events=events, writes=writes, effects=effects,
                canonical=canonical, sha=sha, output=io.StringIO())
            with mock.patch.object(pathlib.Path, "lstat", root_owned_cohort_directory), contextlib.redirect_stdout(fixture.output):
                yield fixture

    def execute_authorizer(self, fixture):
        exec(M.SOURCE_AUTHORIZE, fixture.ns)

    def test_authorizer_activates_only_after_durable_evidence_without_resetting_history(self):
        with self.authorization_environment() as f:
            self.execute_authorizer(f)
            self.assertEqual(len(f.writes), 1)
            updated = f.writes[0]
            self.assertEqual({key: value for key, value in updated.items() if key != "budget_cohort"}, f.state)
            accounting = json.loads(f.output.getvalue())["budget"]
            self.assertEqual(accounting["total_pause_seconds"], M.PRIOR_PAUSE)
            self.assertEqual(accounting["cohort_pause_seconds"], 0)
            self.assertEqual(accounting["max_cohort_pause_seconds"], M.PRIOR_PAUSE)
            self.assertLess(f.events.index("write:" + "a" * 32 + ".json"), f.events.index("activate-state-reference"))
            self.assertEqual(f.effects, [])

    def test_authorizer_first_raw_cas_failure_has_no_evidence_or_state_writes(self):
        with self.authorization_environment() as f:
            f.payload["state_raw_sha256"] = "0" * 64
            with self.assertRaisesRegex(RuntimeError, "source-checkpoint-CAS-mismatch"):
                self.execute_authorizer(f)
            self.assertFalse(any(event.startswith("write:") for event in f.events))
            self.assertFalse((f.scope / "budget-cohorts").exists())
            self.assertEqual(f.state_path.read_bytes(), f.oldraw)
            self.assertEqual(f.writes, [])
            self.assertEqual(f.effects, [])

    def test_authorizer_second_raw_cas_detects_even_whitespace_change_and_leaves_record_inert(self):
        with self.authorization_environment(change_at_lock=True) as f:
            with self.assertRaisesRegex(RuntimeError, "source-checkpoint-CAS-mismatch"):
                self.execute_authorizer(f)
            self.assertTrue((f.scope / "budget-cohorts" / ("a" * 32 + ".json")).exists())
            self.assertNotIn("budget_cohort", json.loads(f.state_path.read_bytes()))
            self.assertEqual(f.state_path.read_bytes(), f.oldraw + b" ")
            self.assertNotIn("activate-state-reference", f.events)
            self.assertEqual(f.writes, [])
            self.assertEqual(f.effects, [])

    def test_authorizer_irreversible_intents_refuse_before_any_cohort_side_effect(self):
        for action in ("pg_stop", "redis_failover", "redis_role_persist"):
            with self.subTest(action=action), self.authorization_environment(irreversible=action) as f:
                with self.assertRaisesRegex(RuntimeError, "irreversible-operation-already-intended"):
                    self.execute_authorizer(f)
                self.assertFalse(any(event.startswith("write:") for event in f.events))
                self.assertFalse((f.scope / "budget-cohorts").exists())
                self.assertEqual(f.state_path.read_bytes(), f.oldraw)
                self.assertEqual(f.writes, [])
                self.assertEqual(f.effects, [])

    def test_authorizer_requires_retired_source_monitors_without_side_effects(self):
        with self.authorization_environment(monitors_retired=False) as f:
            with self.assertRaisesRegex(RuntimeError, "SOURCE_MONITOR_NOT_IDLE_MASKED"):
                self.execute_authorizer(f)
            self.assertFalse(any(event.startswith("write:") for event in f.events))
            self.assertEqual(f.writes, [])
            self.assertEqual(f.effects, [])

    def test_authorizer_source_restart_policy_drift_is_not_an_authorized_snapshot(self):
        for kind, policy in (("app", "unless-stopped"), ("postgres", "no"), ("redis", "no"),
                             ("postgres", "always"), ("redis", "always")):
            with self.subTest(kind=kind, policy=policy), self.authorization_environment(restart_override=(kind, policy)) as f:
                with self.assertRaisesRegex(RuntimeError, "source-resource-changed"):
                    self.execute_authorizer(f)
                self.assertFalse(any(event.startswith("write:") for event in f.events))
                self.assertEqual(f.state_path.read_bytes(), f.oldraw)
                self.assertEqual(f.writes, [])
                self.assertEqual(f.effects, [])

    def test_authorizer_rechecks_proof_age_at_state_activation(self):
        with self.authorization_environment(times=(101, 121)) as f:
            with self.assertRaisesRegex(RuntimeError, "fresh-target-drain-proof-required"):
                self.execute_authorizer(f)
            self.assertTrue((f.scope / "budget-cohorts" / ("a" * 32 + ".json")).exists())
            self.assertEqual(f.state_path.read_bytes(), f.oldraw)
            self.assertEqual(f.writes, [])
            self.assertEqual(f.effects, [])

    def test_remote_timeout_leaves_exclusive_intent_and_uncertain_evidence_before_retry(self):
        raw = b"reviewed fixture"
        frozen = {name: hashlib.sha256(raw).hexdigest() for name in M.OLD}
        with tempfile.TemporaryDirectory() as folder:
            evidence = pathlib.Path(folder)
            intent = evidence / "retry-app-reconcile-20261017.intent.json"
            uncertain = evidence / "retry-app-reconcile-20261017.uncertain.json"

            def timeout_after_intent(*args, **kwargs):
                self.assertTrue(intent.exists())
                raise M.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

            with mock.patch.object(M, "NEW", frozen), mock.patch.object(M, "FOLDER", evidence), \
                 mock.patch.object(M.pathlib.Path, "read_bytes", return_value=raw), \
                 mock.patch.object(M.runpy, "run_path", return_value={"TARGET": ["target"], "SSH_SOURCE": ["source"]}), \
                 mock.patch.object(M.subprocess, "run", side_effect=timeout_after_intent) as remote:
                with self.assertRaises(M.subprocess.TimeoutExpired):
                    M.main(["--apply"])
                self.assertTrue(uncertain.exists())
                with self.assertRaises(FileExistsError):
                    M.main(["--apply"])
                self.assertEqual(remote.call_count, 1)
            self.assertTrue(json.loads(intent.read_bytes())["inspect_before_any_retry"])
            self.assertEqual(json.loads(uncertain.read_bytes())["exception"], "TimeoutExpired")


if __name__ == "__main__":
    unittest.main()
