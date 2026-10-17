"""Local recovery cohort / single-start fault tests; no remote effects."""
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import types
import unittest
from unittest import mock


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(file))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


R = module("tested_idle_resume", "resume_idle_recovery.py")
E = module("idle_resume_engine", "durable_runner.py")
A = module("idle_resume_support", "accept_recovery_generation.py")
REQUIRED = {"detached_writers", "content_moderation", "deferred_last_used"}


def sample():
    ingress = {"paused": False, "queued_requests": 0, "epoch": 2}
    runtime = {"instance_id": R.INSTANCE, "ready": True, "serving": True, "draining": False,
               "background_workers_enabled": False, "active_requests": 0, "active_hijacked_connections": 0,
               "ingress": ingress, "tasks": {name: {"active": 32 if name == "content_moderation" else 0,
                   "pending": 0, "failed": 0, "dropped": 0} for name in REQUIRED}}
    return copy.deepcopy(ingress), runtime, copy.deepcopy(ingress)


class Host:
    def __init__(self, failure=None):
        self.support = A
        self.clock = 0
        self.target = {"checked_at": 100, "instance_id": R.INSTANCE, "ledger_epoch": 17, "restored_ingress_epoch": 2}
        self.required, self.main_unit, self.cohort_id = REQUIRED, "fixed-main.service", "d" * 32
        self.records, self.saved, self.events = {}, {}, []
        self.failure = failure
        self.locked = self.started = self.exists_flag = False
        base = {"v": 1, "run": R.RUN, "owner": R.OWNER, "attempt": 1, "phase": "SOURCE_DATA",
                "pg": "AWS", "redis": "AWS", "window": None, "route_fenced": True,
                "pause_seconds": 7.082038551568985, "deadline_breached": False}
        for identifier, total in [("a" * 32, 11.573), ("b" * 32, 14.656846920028329),
                                  ("9f3d68ecaf85192bd9e9456c387f3b1e", R.LIFETIME)]:
            record = {"v": 1, "run": R.RUN, "owner": R.OWNER, "id": identifier, "previous_state": base,
                      "previous_state_sha256": E.digest(base), "proof_sha256": "c" * 64}
            raw = A.canonical(record)
            self.records[identifier] = raw
            base = {**base, "pause_seconds": total, "budget_cohort": {"id": identifier, "record_sha256": R.sha(raw)}}
        self.initial = {**base, "attempt": 4, "quiet_instance_id": "historical-instance"}
        self.raw = A.canonical(self.initial)
        self.frozen = {name: (self.raw if name == "state.json" else (name + " original").encode()) for name in R.SOURCE_FILES}
        self.engine = types.SimpleNamespace(digest=E.digest,
            budget_accounting=lambda value: E.budget_accounting(value, lambda ref: self.records[ref["id"]]))

    def now(self): return 100 + self.clock
    def monotonic(self): return self.clock
    def sleep(self, duration): self.clock += duration
    def exists(self): return self.exists_flag
    def snapshot(self): return copy.deepcopy(self.frozen)
    def observe(self, name):
        before, runtime, _ = sample()
        if self.failure == "busy" or self.failure == "initial_busy" and self.clock < 3:
            runtime["tasks"]["detached_writers"]["active"] = 1
        return runtime if name == "runtime.status" else before

    @contextlib.contextmanager
    def execution_lock(self):
        self.locked = True
        self.events.append("lock")
        try: yield
        finally:
            self.locked = False
            self.events.append("unlock")

    @contextlib.contextmanager
    def store_lock(self):
        self.events.append("store-lock")
        yield

    def recheck(self, frozen):
        if self.failure == "CAS" and self.records.get(self.cohort_id):
            raise RuntimeError("source-CAS-changed")
        R.need(frozen["state.json"] == self.raw, "source-CAS-changed")
        R.need(all(value == self.frozen[name] for name, value in frozen.items() if name != "state.json"), "ledger-changed")

    def create(self): self.exists_flag = True
    def write(self, name, raw):
        R.need(name not in self.saved, "existing-file")
        self.saved[name] = raw
        self.events.append(name)
        if self.failure == "intent" and name == "main-start.intent.json":
            raise OSError("lost intent result")

    def write_record(self, identifier, raw):
        R.need(identifier not in self.records, "existing-record")
        self.records[identifier] = raw
        self.events.append("record")

    def save_state(self, state):
        record = A.strict(self.records[self.cohort_id])
        R.need(record["proof_sha256"] == R.sha(self.saved["drain-proof.json"]), "proof-not-durable")
        R.need(self.saved["state.json"] == self.raw, "history-not-durable")
        self.raw = A.canonical(state)
        self.events.append("save-state-and-audit")
        if self.failure == "persistence":
            raise OSError("state write acknowledgement lost")

    def read_state_raw(self): return self.raw
    def start_main(self):
        R.need(not self.locked and "main-start.intent.json" in self.saved, "start-before-unlock-or-intent")
        self.events.append("start")
        self.started = True
        if self.failure == "start":
            raise OSError("start result lost")
        if self.failure not in ("monitor_timeout", "main_failed"):
            state = A.strict(self.raw)
            state.update(attempt=5, phase="COMPLETE", pg="TENCENT", redis="TENCENT", pause_seconds=R.LIFETIME + 4)
            self.raw = A.canonical(state)

    def observe_main(self):
        terminal = self.started and self.failure != "monitor_timeout"
        return {"running": self.started and not terminal, "quiescent": not self.started or terminal,
                "InvocationID": "new" if self.started else "old", "Result": "exit-code" if self.failure == "main_failed" else "success",
                "ExecMainStatus": "1" if self.failure == "main_failed" else "0"}


class DrainTests(unittest.TestCase):
    def test_known_idle_moderation_workers_allowed(self):
        self.assertTrue(R.drained(*sample(), REQUIRED))

    def test_accepted_detached_or_other_writer_work_is_not_idle(self):
        for name in ("detached_writers", "deferred_last_used"):
            before, runtime, after = sample()
            runtime["tasks"][name]["active"] = 1
            self.assertFalse(R.drained(before, runtime, after, REQUIRED))

    def test_pending_is_never_exempt_including_moderation(self):
        for name in REQUIRED:
            before, runtime, after = sample()
            runtime["tasks"][name]["pending"] = 1
            self.assertFalse(R.drained(before, runtime, after, REQUIRED))

    def test_http_websocket_queue_or_atomic_snapshot_race_is_not_idle(self):
        for key in ("active_requests", "active_hijacked_connections"):
            before, runtime, after = sample()
            runtime[key] = 1
            self.assertFalse(R.drained(before, runtime, after, REQUIRED))
        before, runtime, after = sample()
        after["queued_requests"] = 1
        self.assertFalse(R.drained(before, runtime, after, REQUIRED))

    def test_ingress_epoch_must_be_latest_restored_strict_integer(self):
        for epoch in (1, 3, True, "2"):
            before, runtime, after = sample()
            before["epoch"] = runtime["ingress"]["epoch"] = after["epoch"] = epoch
            self.assertFalse(R.drained(before, runtime, after, REQUIRED))
        before, runtime, after = sample()
        before["paused"] = runtime["ingress"]["paused"] = after["paused"] = True
        self.assertFalse(R.drained(before, runtime, after, REQUIRED))

    def test_failure_drop_or_missing_writer_is_terminal_refusal(self):
        for key in ("failed", "dropped"):
            before, runtime, after = sample()
            runtime["tasks"]["detached_writers"][key] = 1
            with self.assertRaises(RuntimeError): R.drained(before, runtime, after, REQUIRED)
        before, runtime, after = sample()
        del runtime["tasks"]["detached_writers"]
        with self.assertRaises(RuntimeError): R.drained(before, runtime, after, REQUIRED)

    def test_busy_samples_wait_then_require_full_stable_interval(self):
        host = Host("initial_busy")
        result = R.wait_drained(host, 35)
        self.assertGreaterEqual(host.clock, 5)
        self.assertGreaterEqual(result["stable_seconds"], 2)

    def test_busy_timeout_has_no_cohort_or_start(self):
        host = Host("busy")
        with self.assertRaisesRegex(RuntimeError, "stable-drain-timeout"):
            R.resume(host)
        self.assertFalse(host.saved)
        self.assertNotIn("start", host.events)
        self.assertLess(host.clock, 36)


class ResumeTests(unittest.TestCase):
    def test_append_preserves_every_previous_field_and_history_before_single_start(self):
        host = Host()
        original_records = dict(host.records)
        result = R.resume(host)
        self.assertTrue(result["ok"])
        record = A.strict(host.records[host.cohort_id])
        self.assertEqual(record["previous_state"], host.initial)
        self.assertEqual(record["previous_state_sha256"], E.digest(host.initial))
        self.assertEqual(host.saved["state.json"], A.canonical(host.initial))
        self.assertTrue(all(host.records[key] == raw for key, raw in original_records.items()))
        intent = A.strict(host.saved["main-start.intent.json"])
        self.assertEqual(intent["budget"]["total_pause_seconds"], R.LIFETIME)
        self.assertEqual(intent["budget"]["max_cohort_pause_seconds"], 7.082038551568985)
        self.assertEqual(intent["budget"]["cohort_pause_seconds"], 0)
        self.assertEqual(result["current_state"]["quiet_instance_id"], "historical-instance")
        self.assertEqual(host.events.count("start"), 1)
        self.assertLess(host.events.index("main-start.intent.json"), host.events.index("unlock"))
        self.assertLess(host.events.index("unlock"), host.events.index("start"))
        for name, raw in host.frozen.items():
            if name != "state.json": self.assertEqual(host.saved[name], raw)

    def test_changed_CAS_never_starts_or_rewrites_state(self):
        host = Host("CAS")
        with self.assertRaisesRegex(RuntimeError, "source-CAS-changed"): R.resume(host)
        self.assertEqual(host.raw, A.canonical(host.initial))
        self.assertNotIn("start", host.events)

    def test_lost_state_persistence_or_intent_is_inspect_only(self):
        for failure in ("persistence", "intent"):
            host = Host(failure)
            with self.subTest(failure=failure), self.assertRaises(OSError): R.resume(host)
            self.assertNotIn("start", host.events)
            with self.assertRaisesRegex(RuntimeError, "already-attempted-inspect-only"): R.resume(host)

    def test_ambiguous_start_is_never_repeated(self):
        host = Host("start")
        result = R.resume(host)
        self.assertEqual(result["start_outcome"], "unknown-inspect-only")
        self.assertFalse(result["ok"])
        self.assertEqual(host.events.count("start"), 1)
        with self.assertRaises(RuntimeError): R.resume(host)
        self.assertEqual(host.events.count("start"), 1)

    def test_failure_and_timeout_are_reported_without_restart_or_kill(self):
        for failure, expected in [("main_failed", "stopped-inspect-only"), ("monitor_timeout", "bounded-observation-ended-inspect-only")]:
            host = Host(failure)
            result = R.resume(host)
            self.assertFalse(result["ok"])
            self.assertEqual(result["observation"], expected)
            self.assertEqual(host.events.count("start"), 1)
            self.assertLessEqual(host.clock, 50.3)

    def test_mixed_quiescent_new_invocation_snapshot_cannot_end_observation(self):
        host = Host("monitor_timeout")
        original = host.observe_main
        observed = []
        def mixed():
            value = original()
            if host.started:
                observed.append(host.clock)
                # First post-start read combines old quiescence with new ID.
                if len(observed) == 1:
                    value.update(running=False, quiescent=True)
                elif len(observed) == 3:
                    state = A.strict(host.raw)
                    state.update(attempt=5, phase="COMPLETE", pg="TENCENT", redis="TENCENT")
                    host.raw = A.canonical(state)
                    host.failure = None
            return value
        host.observe_main = mixed
        result = R.resume(host)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(observed), 5)

    def test_stale_target_proof_never_changes_state(self):
        host = Host()
        host.target["checked_at"] = 0
        with self.assertRaisesRegex(RuntimeError, "target-proof-expired"): R.resume(host)
        self.assertFalse(host.saved)

    def test_start_has_independent_observation_budget_after_slow_drain(self):
        host = Host("monitor_timeout")
        original = host.observe
        def slow(name):
            before, runtime, _ = sample()
            if host.clock < 25:
                runtime["active_requests"] = 1
                return runtime if name == "runtime.status" else before
            return original(name)
        host.observe = slow
        result = R.resume(host)
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(host.clock, 74)
        self.assertLessEqual(host.clock, 76)

    def test_prior_receipt_canary_and_gates_bind_exact_bytes(self):
        canary = {"phase": "COMPLETE", "ok": True, "target_preflight": {"instance_id": R.INSTANCE},
                  "target_postflight": {"instance_id": R.INSTANCE}}
        frozen = {"accepted-canary.json": A.canonical(canary), "deployment-gates.json": b"original-gates"}
        receipt = {"run": R.RUN, "owner": R.OWNER, "accepted": True, "target": {"instance_id": R.INSTANCE},
                   "live_evidence": {"canary_sha256": R.sha(frozen["accepted-canary.json"])},
                   "gates_after_sha256": R.sha(frozen["deployment-gates.json"])}
        frozen["prior-acceptance.json"] = A.canonical(receipt)
        frozen["prior-start-observation.json"] = A.canonical({"start_attempted_once": True, "start_outcome": "acknowledged"})
        pins = {name: (path, R.sha(frozen[name])) for name, (path, _) in R.EVIDENCE.items()}
        with mock.patch.object(R, "EVIDENCE", pins):
            R.accepted_evidence(A, frozen)
            for name in pins:
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "prior-evidence-raw-pins-changed"):
                    R.accepted_evidence(A, {**frozen, name: frozen[name] + b" "})
            with self.assertRaisesRegex(RuntimeError, "canary-or-gates-unbound"):
                R.accepted_evidence(A, {**frozen, "deployment-gates.json": b"changed-gates"})

    def test_exact_boundary_checks_attempt_authority_window_history(self):
        host = Host()
        for field, value in [("attempt", 3), ("pg", "TENCENT"), ("window", {}), ("pause_seconds", 0),
                             ("route_fenced", False), ("deadline_breached", True), ("budget_accounting_unknown", True)]:
            changed = {**host.initial, field: value}
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                R.source_state(A, A.canonical(changed), host.engine)

    def test_source_boundary_and_pins_are_rechecked(self):
        host = object.__new__(R.SourceHost)
        host.support = types.SimpleNamespace(root_read=lambda path: b"changed", strict=A.strict)
        host.check_boundary = mock.Mock()
        host.service = types.SimpleNamespace(read_gates=mock.Mock())
        with self.assertRaisesRegex(RuntimeError, "source-exact-pins-changed"): host.snapshot()
        self.assertEqual(host.check_boundary.call_count, 1)
        with self.assertRaisesRegex(RuntimeError, "source-CAS-changed"): host.recheck({"state.json": b"original"})
        self.assertEqual(host.check_boundary.call_count, 2)

    def test_offline_default_reads_nothing(self):
        with mock.patch.object(R.pathlib.Path, "read_bytes", side_effect=AssertionError("file")), \
             mock.patch.object(R.subprocess, "run", side_effect=AssertionError("remote")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(R.main([]), 0)
        self.assertTrue(json.loads(output.getvalue())["offline"])

    def test_pinned_reuse_and_remote_scripts_compile(self):
        folder = pathlib.Path(__file__).parent
        support = R.load_support((folder / "accept_recovery_generation.py").read_bytes())
        self.assertEqual(support.OWNER, R.OWNER)
        for name, expected in R.PROGRAMS.items():
            if name == "durable_runner.py":
                # This executed tool keeps its old pin and must fail closed
                # after the separately reviewed attestation-order upgrade.
                self.assertEqual(expected, "18f0184ce061b4de57146a1f1024ab576622adc521dc16f0a9f0080fb76229d4")
                self.assertEqual(R.sha((folder / name).read_bytes()),
                                 "5419ce229ed41bd98b9b4143d15f89817236c29b94071f7c8fb54dfefa86fb14")
            elif name == "target_rpc.py":
                # Historical executed tools deliberately retain their original
                # program pin; the new continuation owns the reviewed upgrade.
                self.assertEqual(expected, "8180879f75169f130b726536ee16957832a9d2bf9e193f93ad380ebd8264f6ec")
                self.assertEqual(R.sha((folder / name).read_bytes()),
                                 "85d980acff46125de0db0a2734d33b632f1cfb919577be871f8c56faff2db75a")
            else:
                self.assertEqual(R.sha((folder / name).read_bytes()), expected, name)
        for action in ("target", "source", "inspect"):
            code = R.remote_code(action, {"support": "x"}, pathlib.Path(R.__file__).read_bytes())
            compile(code, "remote", "exec")
            self.assertLess(code.index("sys.dont_write_bytecode=True"), code.index("exec(compile"))
        payload = {"support": R.base64.b64encode((folder / "accept_recovery_generation.py").read_bytes()).decode(),
                   "target": {"proof": "x" * 8192}, "cohort_id": "d" * 32}
        source = R.remote_code("source", payload, pathlib.Path(R.__file__).read_bytes())
        self.assertLess(len(source.encode()), 120000)

    def test_inspect_has_no_ledger_or_start_effect(self):
        support = types.SimpleNamespace(root_read=lambda path: b'{"attempt":4}', strict=A.strict)
        with mock.patch.object(R, "SAVED", pathlib.Path("/nonexistent-sub2api-resume-test")), \
             mock.patch.object(R.subprocess, "run", side_effect=AssertionError("start")):
            result = R.inspect_source(support)
        self.assertTrue(result["inspect_only"])
        self.assertFalse(result["start_repeated"])


if __name__ == "__main__":
    unittest.main()
