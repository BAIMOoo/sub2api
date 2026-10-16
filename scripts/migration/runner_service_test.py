"""Offline service ordering, drain and acceptance-boundary fault tests."""
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("runner_service", pathlib.Path(__file__).with_name("runner_service.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
OWNER = "ab"*16


def state():
    return {"v": 1, "run": M.E.RUN, "owner": OWNER, "pg": "AWS", "redis": "AWS",
            "pause_seconds": 0, "window": None, "phase": "ENTRY_PREPARATION", "attempt": 0}


class Backend:
    def __init__(self, state):
        self.config = {"owner": OWNER}
        self.store = types.SimpleNamespace(read=lambda kind: copy.deepcopy(state))
        self.events, self.inspections = [], []
        self.clock, self.stop_unknown, self.retire_pending = 0.0, False, 0
        self.source_app = {"running": True, "restart": "no", "exit_code": 0, "oom_killed": False}
        self.source_pg = {"running": True, "restart": "no", "exit_code": 0, "oom_killed": False}
        self.source_redis = copy.deepcopy(self.source_pg)
        self.inbound = 0
        self.runtime = {"instance_id": "fixture-instance", "ready": True, "serving": True, "active_requests": 0,
                        "active_hijacked_connections": 0, "background_workers_enabled": False}
        self.activated = False
        self.stop_signals = 0

    def sleep(self, seconds):
        self.clock += seconds
        if self.clock > 100:
            raise AssertionError("unexpected infinite service loop")

    def source(self, label, method, **fields):
        self.events.append((method, label, self.clock))
        if method == "inspect":
            if self.inspections:
                self.inbound = self.inspections.pop(0)
            return {"resources": {"app": copy.deepcopy(self.source_app), "postgres": copy.deepcopy(self.source_pg),
                                  "redis": copy.deepcopy(self.source_redis)},
                    "app_connections": {"inbound_connections": self.inbound}}
        if method == "app_stop":
            if self.source_app["running"]:
                self.stop_signals += 1
            self.source_app["running"] = False
            if self.stop_unknown:
                self.stop_unknown = False
                raise M.E.Unknown("lost-stop-reply")
            return {"phase": "complete"}
        if method == "retire_pg_monitors":
            if self.retire_pending:
                self.retire_pending -= 1
                raise M.E.Unknown("pending")
            return {"phase": "complete"}
        raise AssertionError(method)

    def attest(self, state, runtime, **sampling):
        self.events.append(("attest", None, self.clock))

    def save(self, value):
        self.events.append(("save", value.get("phase"), self.clock))

    def control(self, label, name, endpoint=None):
        self.events.append((name, label, self.clock))
        if name == "runtime.status":
            return copy.deepcopy(self.runtime)
        if name == "ingress.status":
            return {"paused": False, "queued_requests": 0}
        if name == "postgres.status":
            return {"endpoint": "target", "paused": False}
        if name == "redis.status":
            return {"address": "127.0.0.1:26379", "paused": False, "pubsub_cache_ready": True}
        if name == "background.activate":
            self.activated = True
            self.runtime["background_workers_enabled"] = True
            return {"activated": True, "activating": False, "pending": [], "failures": []}
        raise AssertionError(name)

    def rpc(self, label, action, **fields):
        assert action == "status"
        return {"pg_authority": "TENCENT", "redis_authority": "TENCENT", "pg": {"in_recovery": False},
                "redis": {"role": "master"}}


class RehearsalHung(BaseException):
    """End the intentional controller hang without a real process or timer."""


class RehearsalBackend(Backend):
    def __init__(self, value):
        super().__init__(value)
        self.snapshots = []
        self.target = {"pg_authority": "AWS", "redis_authority": "AWS", "pg_arm": None,
                       "pg_promotion_intent": None, "pg": {"in_recovery": True}, "redis": {"role": "slave"}}
        self.proof = {"postgres": {"writers_quiet": True}, "redis": {"consumers_paused": True}}

    def boot_id(self):
        return "fixture-boot"

    def save(self, value):
        self.snapshots.append(copy.deepcopy(value))
        super().save(value)

    def rpc(self, label, action, **fields):
        self.events.append(("rpc:" + action, label, self.clock))
        if action == "app.quiesce":
            return {"app": {"http_status": 200, "body": copy.deepcopy(self.runtime)}}
        if action != "status":
            raise AssertionError("rehearsal attempted a target authority mutation")
        return copy.deepcopy(self.target)

    def control(self, label, name, endpoint=None):
        if name in ("ingress.pause", "writers.pause", "redis.pause", "postgres.pause"):
            self.events.append((name, label, self.clock))
            return {"paused": True, "quiet": True, "held": True, "active_operations": 0}
        return super().control(label, name, endpoint=endpoint)

    def attest(self, value, runtime, **sampling):
        super().attest(value, runtime, **sampling)
        return copy.deepcopy(self.proof)

    def sleep(self, seconds):
        raise RehearsalHung()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.state = state()
        self.b = Backend(self.state)
        self.entry = mock.Mock(return_value=True)
        self.service = M.Service(self.b, self.state, entry=self.entry, sleep=self.b.sleep, monotonic=lambda: self.b.clock)

    def test_complete_reports_global_history_and_accepts_only_bounded_cohorts(self):
        self.state.update(pg="TENCENT", redis="TENCENT", phase="COMPLETE", pause_seconds=14.5)
        self.b.runtime["background_workers_enabled"] = True
        budget = {"total_pause_seconds": 14.5, "cohort_pause_seconds": 7.4,
                  "max_cohort_pause_seconds": 7.4, "cohort_id": "a"*32}
        with mock.patch.object(M.E, "budget_accounting", return_value=budget):
            result = self.service.verify_complete()
        self.assertEqual(result["pause_seconds"], 14.5)
        self.assertEqual(result["budget"], budget)
        for breach, maximum in ((True, 7.4), (False, 10.01)):
            self.state["deadline_breached"] = breach
            with mock.patch.object(M.E, "budget_accounting", return_value={**budget, "max_cohort_pause_seconds": maximum}), \
                 self.assertRaisesRegex(M.E.Refusal, "FINAL_WAIT_BUDGET_FAILED"):
                self.service.verify_complete()

    def test_default_cli_has_no_production_action(self):
        result = json.loads(subprocess.check_output([sys.executable, M.__file__]))
        self.assertTrue(result["offline"])
        self.assertFalse(result["production_actions"])

    def test_idle_wait_uses_only_fresh_observer_and_embedded_ingress(self):
        runtime = {**self.b.runtime, "ingress": {"paused": False, "queued_requests": 0}}
        self.b.observe_control = mock.Mock(side_effect=lambda name: copy.deepcopy(runtime))
        self.b.control = mock.Mock(side_effect=AssertionError("waiting must not record durable controls"))
        self.service.wait_idle()
        self.assertGreaterEqual(self.b.clock, 1)
        self.assertEqual({c.args[0] for c in self.b.observe_control.call_args_list}, {"runtime.status"})
        self.b.control.assert_not_called()

    def test_idle_wait_restarts_stable_interval_when_embedded_queue_is_nonzero(self):
        runtime = {**self.b.runtime, "ingress": {"paused": False, "queued_requests": 0}}
        samples = [{**runtime, "ingress": {"paused": False, "queued_requests": 1}}, runtime]
        self.b.observe_control = mock.Mock(side_effect=lambda name: samples.pop(0) if samples else runtime)
        self.service.wait_idle(seconds=.5)
        self.assertGreaterEqual(self.b.clock, .75)

    def test_old_app_stop_requires_five_seconds_of_continuously_empty_inbound(self):
        # 3 seconds empty, an existing stream observed, then another quiet run.
        self.b.inspections = [0]*10+[1]+[0]*25
        self.service.wait_old_application()
        stop = next(e for e in self.b.events if e[0] == "app_stop")
        self.assertGreaterEqual(stop[2], 8.3)
        self.assertFalse(self.b.source_app["running"])
        self.assertTrue(self.state["route_fenced"])

    def test_lost_app_stop_response_reconciles_without_another_stop(self):
        self.b.stop_unknown = True
        self.service.wait_old_application()
        stops = [e for e in self.b.events if e[0] == "app_stop"]
        self.assertEqual(len(stops), 2)
        self.assertEqual({e[1] for e in stops}, {"source:app_stop"})
        self.assertEqual(self.b.stop_signals, 1)

    def test_target_unready_never_stops_old_app(self):
        self.b.runtime["ready"] = False
        with self.assertRaises(M.E.Refusal):
            self.service.wait_old_application()
        self.assertFalse(any(e[0] == "app_stop" for e in self.b.events))

    def test_old_app_restart_policy_or_unclean_exit_blocks_migration(self):
        for changes in [{"restart": "always"}, {"running": False, "exit_code": 1},
                        {"running": False, "oom_killed": True}]:
            with self.subTest(changes=changes):
                self.b.source_app.update(changes)
                with self.assertRaises(M.E.Refusal):
                    self.service.wait_old_application()
                self.b.source_app.update(running=True, restart="no", exit_code=0, oom_killed=False)

    def test_entry_proof_loss_keeps_source_app_running(self):
        self.entry.side_effect = M.E.Unknown("entry-proof-lost")
        with self.assertRaises(M.E.Unknown):
            self.service.wait_old_application()
        self.assertTrue(self.b.source_app["running"])
        self.assertFalse(any(e[0] == "app_stop" for e in self.b.events))

    def test_monitor_retirement_uses_same_lifecycle_label_across_pending(self):
        self.b.retire_pending = 3
        self.service.retire_monitors()
        calls = [e for e in self.b.events if e[0] == "retire_pg_monitors"]
        self.assertEqual(len(calls), 4)
        self.assertEqual({e[1] for e in calls}, {"source:retire_pg_monitors"})

    def configure_handover(self, leave_old_pg=False):
        self.b.source_app["running"] = False
        def combined():
            self.b.events.append(("handover_all", None, self.b.clock))
            self.state.update(pg="TENCENT", redis="TENCENT")
            if not leave_old_pg:
                self.b.source_pg["running"] = False
        def retire():
            self.b.events.append(("retire_source_redis", None, self.b.clock))
            self.b.source_redis["running"] = False
        self.service.engine.handover_all = combined
        self.service.engine.retire_source_redis = retire

    def test_background_activation_follows_retired_resources_and_single_data_window(self):
        self.configure_handover()
        result = self.service.run()
        names = [e[0] for e in self.b.events]
        self.assertLess(names.index("retire_pg_monitors"), names.index("handover_all"))
        self.assertLess(names.index("handover_all"), names.index("retire_source_redis"))
        self.assertLess(names.index("retire_source_redis"), names.index("background.activate"))
        self.assertEqual(names.count("handover_all"), 1)
        self.assertNotIn("handover_pg", names)
        self.assertNotIn("handover_redis", names)
        self.assertEqual(result["phase"], "COMPLETE")

    def test_resume_after_guard_closed_data_window_retires_replica_before_background(self):
        self.configure_handover()
        self.state.update(pg="TENCENT", redis="TENCENT", phase="DATA_TENCENT")
        self.b.source_pg["running"] = False
        self.b.source_redis["running"] = True
        self.service.engine.handover_all = mock.Mock(side_effect=AssertionError("already handed over"))
        result = self.service.run()
        self.assertEqual(result["phase"], "COMPLETE")
        names = [event[0] for event in self.b.events]
        self.assertLess(names.index("retire_source_redis"), names.index("background.activate"))
        self.service.engine.handover_all.assert_not_called()

    def test_unknown_retirement_keeps_business_ready_but_blocks_background_activation(self):
        self.configure_handover()
        self.service.engine.retire_source_redis = mock.Mock(side_effect=M.E.Unknown("retirement-unknown"))
        with self.assertRaisesRegex(M.E.Unknown, "retirement-unknown"):
            self.service.run()
        self.assertIsNone(self.state["window"])
        self.assertEqual((self.state["pg"], self.state["redis"]), ("TENCENT", "TENCENT"))
        self.assertTrue(self.b.runtime["ready"])
        self.assertFalse(self.b.activated)

    def test_old_data_resource_left_running_blocks_worker_activation(self):
        self.configure_handover(leave_old_pg=True)
        with self.assertRaises(M.E.Refusal):
            self.service.run()
        self.assertFalse(self.b.activated)

    def test_complete_state_still_requires_fresh_runtime_and_authority_checks(self):
        self.state.update(pg="TENCENT", redis="TENCENT", phase="COMPLETE")
        self.b.runtime.update(background_workers_enabled=True, ready=False)
        with self.assertRaises(M.E.Refusal):
            self.service.run()
        self.assertFalse(self.b.activated)

    def test_recover_cli_does_not_require_acceptance_gates(self):
        instance = mock.Mock()
        instance.recover.return_value = {"window_closed": True}
        with mock.patch.object(M, "execution_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(M.os, "geteuid", return_value=0), \
             mock.patch.object(M.B, "RunnerBackend", return_value=self.b), \
             mock.patch.object(M, "Service", return_value=instance), \
             mock.patch.object(M, "read_gates", side_effect=AssertionError("gates must not block recovery")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M.main(["recover"]), 0)
        instance.recover.assert_called_once()
        instance.run.assert_not_called()

    def test_continue_cli_requires_concrete_acceptance_before_execution(self):
        instance = mock.Mock()
        with mock.patch.object(M, "execution_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(M.os, "geteuid", return_value=0), \
             mock.patch.object(M.B, "RunnerBackend", return_value=self.b), \
             mock.patch.object(M, "Service", return_value=instance), \
             mock.patch.object(M, "selftest_armed", return_value=False), \
             mock.patch.object(M, "read_gates", side_effect=M.E.Refusal("no-gates")):
            with self.assertRaises(M.E.Refusal):
                M.main(["continue"])
        instance.run.assert_not_called()

    def test_state_owner_mismatch_blocks_both_cli_paths(self):
        self.b.store.read = lambda kind: {**state(), "owner": "cd"*16}
        for mode in ["continue", "recover"]:
            with self.subTest(mode=mode), \
                 mock.patch.object(M, "execution_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(M.os, "geteuid", return_value=0), \
                 mock.patch.object(M.B, "RunnerBackend", return_value=self.b):
                with self.assertRaises(M.E.Refusal):
                    M.main([mode])


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.state = {**state(), "route_fenced": False}
        self.b = RehearsalBackend(self.state)
        self.service = M.Service(self.b, self.state, sleep=self.b.sleep, monotonic=lambda: self.b.clock)
        self.rule = mock.patch.object(M.subprocess, "run", return_value=types.SimpleNamespace(returncode=1))
        self.rule_mock = self.rule.start()
        self.addCleanup(self.rule.stop)

    def assert_no_lifecycle_action(self):
        names = [event[0] for event in self.b.events]
        self.assertTrue(set(names) <= {"inspect", "rpc:status", "runtime.status"}, names)
        self.assertEqual(self.b.snapshots, [])
        self.assertTrue(self.b.source_app["running"])
        self.assertTrue(self.b.source_pg["running"])
        self.assertTrue(self.b.source_redis["running"])

    def test_noninitial_core_state_refuses_before_network_or_pause(self):
        cases = [{"phase": "SOURCE_DATA"}, {"pg": "TENCENT"}, {"redis": "COMMIT_INTENT"},
                 {"window": {"kind": "pg"}}, {"attempt": 1}, {"route_fenced": True},
                 {"route_fenced": None}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.state.update({**state(), "route_fenced": False, **changes})
                with self.assertRaisesRegex(M.E.Refusal, "SELFTEST_AFTER_ENTRY_FORBIDDEN"):
                    self.service.fault_rehearsal()
                self.assert_no_lifecycle_action()
        self.rule_mock.assert_not_called()

    def test_present_or_unverifiable_bridge_refuses_before_source_inspection(self):
        for code in (0, 2, -9):
            with self.subTest(returncode=code):
                self.rule_mock.return_value.returncode = code
                with self.assertRaisesRegex(M.E.Refusal, "SELFTEST_REQUIRES_NO_CUSTOMER_BRIDGE"):
                    self.service.fault_rehearsal()
                self.assert_no_lifecycle_action()
                self.assertEqual(self.b.events, [])

    def test_bridge_timeout_or_exec_error_never_pauses_or_signals_source(self):
        for error in (subprocess.TimeoutExpired("iptables", 1.5), OSError("fixture")):
            with self.subTest(error=type(error).__name__):
                self.rule_mock.side_effect = error
                with self.assertRaises((subprocess.TimeoutExpired, OSError)):
                    self.service.fault_rehearsal()
                self.assert_no_lifecycle_action()

    def test_each_source_resource_must_still_be_running(self):
        for key in ("source_app", "source_pg", "source_redis"):
            with self.subTest(resource=key):
                resource = getattr(self.b, key)
                resource["running"] = False
                with self.assertRaisesRegex(M.E.Refusal, "SELFTEST_SOURCE_NOT_LIVE"):
                    self.service.fault_rehearsal()
                resource["running"] = True
                self.assert_no_lifecycle_action()

    def test_target_authority_role_arm_or_promotion_refuses_without_pausing(self):
        cases = [{"pg_authority": "COMMIT_INTENT"}, {"redis_authority": "TENCENT"},
                 {"pg_arm": {"arm_id": "ab"*16}}, {"pg_promotion_intent": {"op_id": "cd"*16}},
                 {"pg": {"in_recovery": False}}, {"pg": {}}, {"redis": {"role": "master"}},
                 {"redis": {}}]
        original = copy.deepcopy(self.b.target)
        for changes in cases:
            with self.subTest(changes=changes):
                self.b.target = {**copy.deepcopy(original), **changes}
                with self.assertRaisesRegex(M.E.Refusal, "SELFTEST_TARGET_NOT_INITIAL_AND_IDLE"):
                    self.service.fault_rehearsal()
                self.assert_no_lifecycle_action()

    def test_target_unready_busy_websocket_or_background_work_refuses(self):
        original = copy.deepcopy(self.b.runtime)
        for key, value in (("ready", False), ("serving", False), ("active_requests", 1),
                           ("active_hijacked_connections", 1), ("background_workers_enabled", True)):
            with self.subTest(field=key):
                self.b.runtime = {**copy.deepcopy(original), key: value}
                with self.assertRaisesRegex(M.E.Refusal, "SELFTEST_TARGET_NOT_INITIAL_AND_IDLE"):
                    self.service.fault_rehearsal()
                self.assert_no_lifecycle_action()

    def test_once_only_combined_window_precedes_quiesce_and_intentional_hang(self):
        self.assertEqual(M.SELFTEST_ATTEMPT, 1000001)
        with self.assertRaises(RehearsalHung):
            self.service.fault_rehearsal()
        names = [event[0] for event in self.b.events]
        self.assertEqual(names.count("rpc:app.quiesce"), 1)
        self.assertLess(names.index("save"), names.index("rpc:app.quiesce"))
        first = self.b.snapshots[0]
        self.assertEqual(first["attempt"], M.SELFTEST_ATTEMPT)
        self.assertEqual(first["phase"], "RECOVERY_SELFTEST")
        self.assertEqual(first["window"]["kind"], "combined")
        self.assertEqual(first["window"]["boot_id"], "fixture-boot")
        self.assertFalse(first["window"]["irreversible"])
        self.assertEqual((first["pg"], first["redis"]), ("AWS", "AWS"))
        self.assertFalse(first["route_fenced"])
        self.assertIn("selftest_paused_at", self.state)
        self.assertEqual(self.state["quiet_instance_id"], "fixture-instance")
        self.assertTrue(set(names) <= {"inspect", "rpc:status", "rpc:app.quiesce", "runtime.status", "save", "attest"}, names)
        quiesce = next(event for event in self.b.events if event[0] == "rpc:app.quiesce")
        self.assertEqual(quiesce[1], str(M.SELFTEST_ATTEMPT) + ":app.quiesce")
        self.assertNotEqual(quiesce[1], "1000000:app.quiesce")
        self.assertEqual(self.service.engine.label("app.restore"), "1000001:app.restore")
        with self.assertRaisesRegex(M.E.Refusal, "SELFTEST_AFTER_ENTRY_FORBIDDEN"):
            self.service.fault_rehearsal()

    def test_failed_quiet_proof_keeps_durable_window_for_guard_recovery(self):
        for component, field in (("postgres", "writers_quiet"), ("redis", "consumers_paused")):
            with self.subTest(component=component):
                self.fresh_rehearsal_fixture()
                self.b.proof[component][field] = False
                with self.assertRaisesRegex(M.E.Refusal, "SELFTEST_QUIET_ATTESTATION_FAILED"):
                    self.service.fault_rehearsal()
                self.assertIsNotNone(self.state["window"])
                self.assertEqual(self.state["attempt"], M.SELFTEST_ATTEMPT)
                self.assertNotIn("selftest_paused_at", self.state)
                self.assertFalse(self.state["window"]["irreversible"])
                self.assertEqual([event[0] for event in self.b.events if event[0] == "rpc:app.quiesce"], ["rpc:app.quiesce"])

    def fresh_rehearsal_fixture(self):
        self.state = {**state(), "route_fenced": False}
        self.b = RehearsalBackend(self.state)
        self.service = M.Service(self.b, self.state, sleep=self.b.sleep, monotonic=lambda: self.b.clock)

    def test_lost_quiesce_reply_preserves_combined_window_without_authority_action(self):
        original = self.b.rpc
        def lost_reply(label, action, **fields):
            result = original(label, action, **fields)
            if action == "app.quiesce":
                raise M.E.Unknown("fixture-lost-quiesce-reply")
            return result
        self.b.rpc = lost_reply
        with self.assertRaises(M.E.Unknown):
            self.service.fault_rehearsal()
        self.assertIsNotNone(self.state["window"])
        self.assertFalse(self.state["window"]["irreversible"])
        self.assertEqual((self.state["pg"], self.state["redis"]), ("AWS", "AWS"))
        self.assertNotIn("selftest_paused_at", self.state)
        self.assertEqual([event[0] for event in self.b.events if event[0].startswith("rpc:")], ["rpc:status", "rpc:app.quiesce"])
        self.assertEqual([event[0] for event in self.b.events if event[0] in
                          ("app_stop", "pg_stop", "redis_failover", "retire_pg_monitors")], [])

    def test_armed_continue_uses_rehearsal_and_never_normal_migration(self):
        instance = mock.Mock()
        instance.fault_rehearsal.return_value = {"fixture": True}
        with mock.patch.object(M, "execution_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(M.os, "geteuid", return_value=0), \
             mock.patch.object(M.B, "RunnerBackend", return_value=self.b), \
             mock.patch.object(M, "Service", return_value=instance), \
             mock.patch.object(M, "selftest_armed", return_value=True), \
             mock.patch.object(M, "read_gates", side_effect=AssertionError("not a normal migration")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M.main(["continue"]), 0)
        instance.fault_rehearsal.assert_called_once()
        instance.run.assert_not_called()
        instance.recover.assert_not_called()


class RehearsalConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.scope = pathlib.Path(directory.name)
        self.path = self.scope / "controller-selftest.json"
        patch = mock.patch.object(M, "SCOPE", self.scope)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(M.S, "private_bytes", side_effect=lambda path: path.read_bytes())
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, value):
        self.path.write_text(json.dumps(value))

    def test_absent_selftest_is_unarmed(self):
        self.assertFalse(M.selftest_armed(OWNER))

    def test_only_exact_owner_scope_and_boolean_armed_are_accepted(self):
        initial = {"v": 1, "run": M.E.RUN, "owner": OWNER, "armed": True}
        self.write(initial)
        self.assertTrue(M.selftest_armed(OWNER))
        self.write({**initial, "armed": False})
        self.assertFalse(M.selftest_armed(OWNER))
        for changes in ({"owner": "cd"*16}, {"run": "other"}, {"v": 2}, {"armed": 1},
                        {"armed": "true"}, {"extra": "forbidden"}):
            with self.subTest(changes=changes):
                self.write({**initial, **changes})
                with self.assertRaisesRegex(M.E.Refusal, "SELFTEST_CONFIGURATION_INVALID"):
                    M.selftest_armed(OWNER)
        missing = dict(initial)
        del missing["armed"]
        self.write(missing)
        with self.assertRaises(M.E.Refusal):
            M.selftest_armed(OWNER)


if __name__ == "__main__":
    unittest.main()
