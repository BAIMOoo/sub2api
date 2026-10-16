"""Offline WAL, takeover and heartbeat fault tests. No SSH or production commands."""
import copy
import contextlib
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("runner_guard", pathlib.Path(__file__).with_name("runner_guard.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
OWNER = "ab"*16
COMMENT = "migration-owner:owned-original-seed"


def config():
    return {"v": 1, "run": M.RUN, "owner": OWNER, "source_manifest_sha256": "a"*64,
            "role_owner_comment_sha256": hashlib.sha256(COMMENT.encode()).hexdigest(),
            "pg_data_host_path": "/var/lib/docker/volumes/owned/_data", "slot_create_intended": True}


def facts():
    return {"system_identifier": "12345", "timeline": 1, "in_recovery": False,
            "data_directory": M.DATA, "wal_cap_bytes": M.WAL_CAP,
            "role": {"name": M.ROLE, "comment": COMMENT, "replication": True, "login": True,
                     "superuser": False, "createdb": False, "createrole": False, "bypassrls": False},
            "slots": [{"name": M.SLOT, "type": "physical", "temporary": False, "active": True,
                       "restart_lsn": "1/100", "retained_bytes": 1000}]}


class Clock:
    def __init__(self):
        self.wall, self.mono = 1000.0, 100.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, value):
        self.wall += value
        self.mono += value


class Job:
    def __init__(self, now, monotonic, result=None, done=False, error=None):
        self.started, self.monotonic = now, monotonic
        self.completed_at, self.result, self.error = None, result, error
        self.done = done

    @property
    def done(self):
        return self._done

    @done.setter
    def done(self, value):
        self._done = value
        if value:
            self.completed_at = self.monotonic()

    def poll(self):
        return self.done


class Child:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class Backend:
    def __init__(self, clock):
        self.clock, self.config = clock, config()
        self.files, self.events, self.jobs = {}, [], []
        self.main = {"running": True, "quiescent": False}
        self.children = []
        self.persist_trip_fails = False
        self.persist_status_fails = False

    def boot_id(self):
        return "boot1"

    def read(self, name, missing=False):
        return copy.deepcopy(self.files.get(name))

    def write(self, name, value):
        self.events.append(("write", name, self.clock.now()))
        if name == "guard-trip.json" and self.persist_trip_fails:
            raise OSError("disk-full")
        if name == "guard-status.json" and self.persist_status_fails:
            raise OSError("disk-full")
        self.files[name] = copy.deepcopy(value)

    def start_job(self, kind, now):
        self.events.append(("start", kind, self.clock.now()))
        job = Job(now, self.clock.monotonic)
        self.jobs.append((kind, job))
        return job

    def main_status(self):
        self.events.append(("main_status",))
        return copy.deepcopy(self.main)

    def stop_main(self):
        self.events.append(("stop_main",))

    def kill_main(self):
        self.events.append(("kill_main",))

    def spawn_recover(self):
        assert self.main["quiescent"]
        self.events.append(("recover",))
        child = Child()
        self.children.append(child)
        return child

    def window(self, used=0, elapsed=0):
        self.files["state.json"] = {"v": 1, "run": M.RUN, "pause_seconds": used,
            "window": {"boot_id": "boot1", "started_monotonic": self.clock.mono-elapsed,
                       "started_at": self.clock.wall-elapsed, "elapsed_floor": elapsed}}


class ThreadJobTests(unittest.TestCase):
    def test_worker_records_completion_before_publishing_success_or_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                clock = Clock()
                def work():
                    clock.advance(0.4)
                    if fail:
                        raise OSError("fixture")
                    return {"pg_wal_protected": True}
                job = M.ThreadJob(work, clock.monotonic(), monotonic=clock.monotonic)
                self.assertTrue(job.done.wait(1))
                self.assertTrue(job.poll())
                self.assertAlmostEqual(job.completed_at-job.started, 0.4)
                if fail:
                    self.assertEqual(job.error, "GUARD_LOCAL_PROBE_FAILED")
                else:
                    self.assertEqual(job.result, {"pg_wal_protected": True})
                completed = job.completed_at
                clock.advance(2)
                self.assertEqual(job.completed_at, completed)

    def test_fixed_refusal_codes_are_preserved_without_raw_error_text(self):
        for error, expected in ((M.ProbeUnavailable("GUARD_COMMAND_TIMEOUT"), "GUARD_COMMAND_TIMEOUT"),
                                (M.S.Refusal("GUARD_ROLE_NOT_OWNED"), "GUARD_ROLE_NOT_OWNED"),
                                (M.S.Refusal("private SQL or stderr"), "GUARD_LOCAL_PROBE_FAILED")):
            with self.subTest(expected=expected):
                def work():
                    raise error
                job = M.ThreadJob(work, 0)
                self.assertTrue(job.done.wait(1))
                self.assertEqual(job.error, expected)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.b = Backend(self.clock)
        self.g = M.Guard(self.b, self.clock.now, self.clock.monotonic)

    def sample_ok(self):
        self.g.tick()
        job = self.g.sample_job
        job.done, job.result = True, {"pg_wal_protected": True, "mode": "RUNNING_BOUNDED"}
        return self.g.tick()

    def test_default_cli_is_offline(self):
        result = json.loads(subprocess.check_output([sys.executable, M.__file__]))
        self.assertTrue(result["offline"])
        self.assertFalse(result["local_actions"])

    def test_initial_heartbeat_fails_closed_until_sample_completes(self):
        self.assertFalse(self.g.tick()["healthy"])
        self.assertFalse(self.b.files["guard-status.json"]["pg_wal_protected"])
        self.assertTrue(self.sample_ok()["healthy"])

    def test_status_exactly_matches_source_fresh_contract(self):
        result = self.sample_ok()
        self.assertEqual({k: result[k] for k in ("v", "run", "owner", "boot_id")},
                         {"v": 1, "run": M.RUN, "owner": OWNER, "boot_id": "boot1"})
        self.assertEqual(result["checked_at"], self.clock.now())
        self.assertTrue(result["healthy"] and result["pg_wal_protected"])

    def test_main_policy_is_checked_without_checkpoint_or_open_window(self):
        self.sample_ok()
        first = self.b.events.count(("main_status",))
        self.assertGreater(first, 0)
        self.b.files["state.json"] = {"v": 1, "run": M.RUN, "pause_seconds": 0, "window": None}
        self.b.main = {"running": False, "quiescent": True}
        status = self.g.tick()
        self.assertGreater(self.b.events.count(("main_status",)), first)
        self.assertTrue(status["healthy"])
        self.assertIsNone(status["reason"])
        self.assertFalse(any(event[0] in ("stop_main", "kill_main", "recover") for event in self.b.events))

    def test_main_policy_change_or_timeout_revokes_health_without_wal_trip_then_recovers(self):
        self.sample_ok()
        self.b.files["state.json"] = {"v": 1, "run": M.RUN, "pause_seconds": 0, "window": None}
        original = self.b.main_status
        for error in (M.S.Refusal("GUARD_MAIN_OVERRIDE_OR_POLICY_CHANGED"),
                      subprocess.TimeoutExpired("systemctl", 0.2)):
            with self.subTest(error=type(error).__name__):
                self.b.main_status = mock.Mock(side_effect=error)
                for _ in range(2):
                    status = self.g.tick()
                    self.assertTrue(status["pg_wal_protected"])
                    self.assertFalse(status["healthy"])
                    self.assertFalse(status["tripped"])
                    self.assertEqual(status["reason"], "GUARD_SUPERVISOR_UNCERTAIN")
                    self.assertNotIn("guard-trip.json", self.b.files)
                    self.assertFalse(any(kind == "fence" for kind, job in self.b.jobs))
                self.b.main_status = original
                for _ in range(2):
                    status = self.g.tick()
                    self.assertTrue(status["healthy"])
                    self.assertIsNone(status["reason"])

    def test_source_guard_fresh_rejects_supervisor_failure_and_accepts_fresh_recovery(self):
        self.sample_ok()
        original = self.b.main_status
        self.b.main_status = mock.Mock(side_effect=M.S.Refusal("GUARD_UNIT_CHANGED"))
        source_backend = mock.Mock()
        source_backend.read.side_effect = lambda name: copy.deepcopy(self.b.files["guard-status.json"] if name == "guard" else self.b.files[name])
        source_backend.now.side_effect = self.clock.now
        source_backend.boot_id.return_value = "boot1"
        source_backend.unit_status.return_value = {"ActiveState": "active", "SubState": "running", "MainPID": "123", "Job": ""}
        source = M.S.SourceOperations.__new__(M.S.SourceOperations)
        source.b, source.m = source_backend, {"owner": OWNER, "units": {M.GUARD_UNIT: "a"*64}}
        self.g.tick()
        with self.assertRaisesRegex(M.S.Refusal, "RECOVERY_GUARD_NOT_PROTECTING"):
            source._guard()
        self.b.main_status = original
        self.g.tick()
        self.assertTrue(source._guard()["healthy"])

    def test_supervisor_recovery_does_not_clear_sticky_wal_trip(self):
        self.g.fail("GUARD_WAL_RETENTION_HIGH")
        original = self.b.main_status
        self.b.main_status = mock.Mock(side_effect=subprocess.TimeoutExpired("systemctl", 0.2))
        self.g.tick()
        self.b.main_status = original
        status = self.sample_ok()
        self.assertFalse(status["healthy"])
        self.assertTrue(status["tripped"])
        self.assertEqual(status["reason"], "GUARD_WAL_RETENTION_HIGH")

    def test_uncertain_state_revokes_health_but_still_starts_recovery(self):
        self.b.window(elapsed=3)
        self.b.files["state.json"]["window"].update(boot_id="old", elapsed_floor=4)
        self.b.main = {"running": False, "quiescent": True}
        status = self.sample_ok()
        self.assertTrue(status["pg_wal_protected"])
        self.assertFalse(status["healthy"])
        self.assertEqual(status["reason"], "GUARD_STATE_OR_CLOCK_UNCERTAIN")
        self.assertIn(("recover",), self.b.events)
        self.assertFalse(status["tripped"])
        self.b.files["state.json"]["window"] = None
        status = self.g.tick()
        self.assertTrue(status["healthy"])
        self.assertIsNone(status["reason"])

    def test_supervisor_failure_keeps_heartbeat_sampling_and_existing_recovery_alive(self):
        self.b.window(elapsed=7)
        self.b.main = {"running": False, "quiescent": True}
        self.sample_ok()
        child = self.g.recovery
        original = self.b.main_status
        self.b.main_status = mock.Mock(side_effect=subprocess.TimeoutExpired("systemctl", 0.2))
        for _ in range(4):
            self.clock.advance(0.1)
            status = self.g.tick()
            self.assertFalse(status["healthy"])
            self.assertEqual(status["checked_at"], self.clock.now())
            self.assertIs(self.g.recovery, child)
            self.assertIsNone(child.poll())
        self.assertGreaterEqual(len([job for kind, job in self.b.jobs if kind == "sample"]), 2)
        self.assertFalse(any(kind == "fence" for kind, job in self.b.jobs))
        self.b.main_status = original
        status = self.g.tick()
        self.assertTrue(status["healthy"])
        self.assertIsNone(status["reason"])

    def test_sample_failure_persists_trip_before_independent_fence(self):
        self.g.tick()
        self.g.sample_job.done, self.g.sample_job.error = True, "FAIL"
        status = self.g.tick()
        self.assertFalse(status["healthy"])
        self.assertTrue(status["tripped"])
        events = [(e[0], e[1]) for e in self.b.events if len(e) > 1]
        self.assertLess(events.index(("write", "guard-trip.json")), events.index(("start", "fence")))

    def test_hung_sampler_cannot_block_heartbeat_or_trip(self):
        self.g.tick()
        started = self.clock.monotonic()
        self.clock.advance(M.SAMPLE_TIMEOUT+0.01)
        self.assertFalse(self.g.tick()["tripped"])
        while not self.g.trip:
            self.clock.advance(0.1)
            self.assertEqual(self.g.tick()["checked_at"], self.clock.now())
            self.assertLessEqual(self.clock.monotonic()-started, 5)
        self.assertTrue(self.g.tick()["tripped"])
        self.assertEqual(self.g.trip["reason"], "GUARD_PROBE_UNAVAILABLE_DEADLINE")
        self.assertEqual(len([j for kind, j in self.b.jobs if kind == "sample"]), 1)

    def test_late_success_remains_unhealthy_until_a_new_timely_sample(self):
        self.g.tick()
        self.clock.advance(M.SAMPLE_TIMEOUT+0.01)
        self.g.sample_job.done, self.g.sample_job.result = True, {"pg_wal_protected": True}
        status = self.g.tick()
        self.assertFalse(status["tripped"])
        self.assertFalse(status["healthy"])
        self.assertIsNone(self.g.last_sample)
        self.clock.advance(M.SAMPLE_INTERVAL)
        self.assertTrue(self.sample_ok()["healthy"])

    def transient_failure(self, delay=0.65):
        if self.g.sample_job is None:
            self.clock.advance(M.SAMPLE_INTERVAL)
            self.g.tick()
        self.clock.advance(delay)
        self.g.sample_job.error = "GUARD_COMMAND_TIMEOUT"
        self.g.sample_job.done = True
        return self.g.tick()

    def test_single_650ms_timeout_revokes_health_without_destroying_replica(self):
        self.sample_ok()
        status = self.transient_failure()
        self.assertFalse(status["healthy"])
        self.assertFalse(status["pg_wal_protected"])
        self.assertFalse(status["tripped"])
        self.assertEqual(status["reason"], "GUARD_COMMAND_TIMEOUT")
        self.assertIsNone(self.g.last_sample)
        self.assertFalse(any(kind == "fence" for kind, job in self.b.jobs))
        self.clock.advance(M.SAMPLE_INTERVAL)
        status = self.sample_ok()
        self.assertTrue(status["healthy"])
        self.assertEqual(status["probe_failures"], 0)
        self.assertIsNone(status["reason"])

    def test_repeated_transient_failures_need_count_and_duration_before_fence(self):
        self.sample_ok()
        for _ in range(3):
            self.assertFalse(self.transient_failure(0.1)["tripped"])
        self.clock.advance(2.51)
        status = self.transient_failure(0.0)
        self.assertTrue(status["tripped"])
        self.assertEqual(self.g.trip["reason"], "GUARD_COMMAND_TIMEOUT")
        self.assertGreaterEqual(self.g.trip["probe_failures"], 3)

    def test_two_failures_cannot_fence_until_absolute_unavailability_deadline(self):
        self.sample_ok()
        self.transient_failure(0.1)
        self.transient_failure(0.1)
        self.clock.advance(3.0)
        self.assertFalse(self.g.tick()["tripped"])
        self.clock.advance(1.31)
        self.assertTrue(self.g.tick()["tripped"])
        self.assertEqual(self.g.trip["reason"], "GUARD_PROBE_UNAVAILABLE_DEADLINE")

    def test_transient_failure_does_not_delay_window_recovery(self):
        self.sample_ok()
        self.b.window(elapsed=7.5)
        self.b.main = {"running": False, "quiescent": True}
        status = self.transient_failure()
        self.assertFalse(status["healthy"])
        self.assertFalse(status["tripped"])
        self.assertIn(("recover",), self.b.events)

    def test_identity_threshold_and_malformed_results_still_fence_immediately(self):
        for reason in ("GUARD_ROLE_NOT_OWNED", "GUARD_CLUSTER_CHANGED", "GUARD_WAL_RETENTION_HIGH",
                       "GUARD_WAL_CAP_UNBOUNDED", "GUARD_DISK_LOW", "INVALID_JSON"):
            with self.subTest(reason=reason):
                b = Backend(self.clock)
                g = M.Guard(b, self.clock.now, self.clock.monotonic)
                g.tick()
                g.sample_job.error, g.sample_job.done = reason, True
                status = g.tick()
                self.assertTrue(status["tripped"])
                self.assertEqual(g.trip["reason"], reason)

    def test_completed_fast_probe_is_not_timed_out_by_delayed_poll(self):
        self.g.tick()
        self.clock.advance(0.2)
        job = self.g.sample_job
        job.done, job.result = True, {"pg_wal_protected": True}
        completed = self.clock.monotonic()
        self.clock.advance(1.1)
        status = self.g.tick()
        self.assertTrue(status["healthy"])
        self.assertFalse(status["tripped"])
        self.assertEqual(self.g.sample_at, completed)
        self.assertFalse(any(kind == "fence" for kind, job in self.b.jobs))

    def test_exact_sample_timeout_is_accepted_by_worker_duration(self):
        self.g.tick()
        self.clock.advance(M.SAMPLE_TIMEOUT)
        self.g.sample_job.done, self.g.sample_job.result = True, {"pg_wal_protected": True}
        self.clock.advance(0.1)
        status = self.g.tick()
        self.assertTrue(status["healthy"])
        self.assertFalse(status["tripped"])

    def test_delayed_poll_does_not_refresh_stale_completed_probe(self):
        self.g.tick()
        self.clock.advance(0.2)
        self.g.sample_job.done, self.g.sample_job.result = True, {"pg_wal_protected": True}
        self.clock.advance(M.SAMPLE_FRESH+0.01)
        status = self.g.tick()
        self.assertFalse(status["healthy"])
        self.assertFalse(status["pg_wal_protected"])
        self.assertFalse(status["tripped"])

    def test_supervisor_time_is_included_in_completed_probe_freshness(self):
        self.sample_ok()
        self.b.main_status = lambda: self.clock.advance(M.SAMPLE_FRESH+0.01) or self.b.main
        status = self.g.tick()
        self.assertFalse(status["healthy"])
        self.assertFalse(status["pg_wal_protected"])
        self.assertFalse(status["tripped"])

    def test_invalid_worker_completion_timestamp_fails_closed(self):
        for completed in (None, float("nan"), float("inf"), 99.0, 101.0):
            with self.subTest(completed=completed):
                clock = Clock()
                backend = Backend(clock)
                guard = M.Guard(backend, clock.now, clock.monotonic)
                guard.tick()
                guard.sample_job.done, guard.sample_job.result = True, {"pg_wal_protected": True}
                guard.sample_job.completed_at = completed
                status = guard.tick()
                self.assertFalse(status["healthy"])
                self.assertTrue(status["tripped"])

    def test_successful_fence_never_clears_trip_or_rearms_main(self):
        self.g.fail("test")
        self.g.tick()
        self.g.fence_job.done, self.g.fence_job.result = True, {"pg_wal_protected": True}
        self.g.tick()
        self.assertTrue(self.sample_ok()["tripped"])
        self.assertFalse(self.g.tick()["healthy"])
        self.assertFalse(any(e[0] in ("stop_main", "kill_main", "recover") for e in self.b.events))

    def test_trip_survives_guard_restart(self):
        self.g.fail("sample-failed")
        restarted = M.Guard(self.b, self.clock.now, self.clock.monotonic)
        self.assertTrue(restarted.tick()["tripped"])
        self.assertFalse(restarted.tick()["healthy"])

    def test_fresh_success_after_restart_keeps_historical_trip(self):
        self.g.fail("GUARD_WAL_RETENTION_HIGH")
        self.g = M.Guard(self.b, self.clock.now, self.clock.monotonic)
        status = self.sample_ok()
        self.assertTrue(status["pg_wal_protected"])
        self.assertFalse(status["healthy"])
        self.assertTrue(status["tripped"])
        self.assertEqual(status["reason"], "GUARD_WAL_RETENTION_HIGH")

    def test_trip_persist_failure_still_starts_fence(self):
        self.b.persist_trip_fails = True
        self.g.fail("disk-full")
        status = self.g.tick()
        self.assertFalse(status["healthy"])
        self.assertTrue(any(kind == "fence" for kind, job in self.b.jobs))
        self.b.persist_trip_fails = False
        self.g.tick()
        self.assertTrue(self.g.trip_persisted)
        self.assertIn("guard-trip.json", self.b.files)

    def test_heartbeat_write_failure_does_not_kill_independent_fence(self):
        self.g.fail("disk-full")
        self.b.persist_status_fails = True
        self.g.tick()
        self.assertIsNotNone(self.g.fence_job)
        self.assertEqual(self.g.reason, "GUARD_HEARTBEAT_PERSIST_FAILED")
        self.g.fence_job.done, self.g.fence_job.result = True, {"pg_wal_protected": True}
        self.g.tick()
        self.assertIsNone(self.g.fence_job)

    def test_malformed_success_is_a_persistent_trip(self):
        self.g.tick()
        self.g.sample_job.done, self.g.sample_job.result = True, {}
        self.assertTrue(self.g.tick()["tripped"])

    def test_fence_retries_are_finite(self):
        self.g.fail("test")
        for _ in range(10):
            self.g.tick()
            if self.g.fence_job:
                self.g.fence_job.done, self.g.fence_job.error = True, "FAIL"
        self.assertEqual(len([j for kind, j in self.b.jobs if kind == "fence"]), 3)

    def test_budget_consumption_across_windows_triggers_reserve_takeover(self):
        self.b.window(used=3, elapsed=4.5)
        self.g.tick()
        self.assertIn(("stop_main",), self.b.events)
        self.assertNotIn(("recover",), self.b.events)

    def test_live_main_keeps_cumulative_budget_until_7_5_seconds(self):
        self.b.window(used=3, elapsed=2.5)
        self.g.tick()
        self.assertNotIn(("stop_main",), self.b.events)
        self.clock.advance(1.99)
        self.g.tick()
        self.assertNotIn(("stop_main",), self.b.events)
        self.clock.advance(0.02)
        self.g.tick()
        self.assertIn(("stop_main",), self.b.events)

    def test_hung_live_main_is_stopped_then_killed_before_recovery(self):
        self.b.window(elapsed=7.5)
        self.g.tick()
        self.assertIn(("stop_main",), self.b.events)
        self.assertNotIn(("recover",), self.b.events)
        self.clock.advance(0.61)
        self.g.tick()
        self.assertIn(("kill_main",), self.b.events)
        self.assertNotIn(("recover",), self.b.events)
        self.b.main = {"running": False, "quiescent": True}
        self.g.tick()
        self.assertIn(("recover",), self.b.events)

    def test_live_main_with_budget_is_left_running(self):
        self.b.window(elapsed=1)
        self.g.tick()
        self.assertNotIn(("stop_main",), self.b.events)

    def test_dead_main_triggers_takeover_before_reserve(self):
        self.b.window(elapsed=0.1)
        self.b.main = {"running": False, "quiescent": True}
        self.g.tick()
        self.assertIn(("stop_main",), self.b.events)
        self.assertIn(("recover",), self.b.events)

    def test_helper_cgroup_must_be_empty_before_recovery(self):
        self.b.window(elapsed=7)
        self.b.main = {"running": False, "quiescent": False}
        self.g.tick()
        self.clock.advance(0.61)
        self.g.tick()
        self.assertIn(("kill_main",), self.b.events)
        self.assertNotIn(("recover",), self.b.events)
        self.b.main["quiescent"] = True
        self.g.tick()
        self.assertIn(("recover",), self.b.events)
        self.g.tick()
        self.assertEqual(self.b.events.count(("kill_main",)), 1)

    def test_hung_network_recovery_never_blocks_wal_sampling_or_heartbeat(self):
        self.b.window(elapsed=7)
        self.b.main = {"running": False, "quiescent": True}
        self.sample_ok()
        child = self.g.recovery
        for _ in range(10):
            self.clock.advance(0.1)
            self.g.tick()
        self.assertIs(self.g.recovery, child)
        self.assertIsNone(child.poll())
        self.assertEqual(len(self.b.children), 1)
        self.assertGreater(len([e for e in self.b.events if e[:2] == ("write", "guard-status.json")]), 10)
        self.assertGreaterEqual(len([j for k, j in self.b.jobs if k == "sample"]), 2)

    def test_failed_recover_retries_same_state_until_window_closes(self):
        self.b.window(elapsed=7)
        self.b.main = {"running": False, "quiescent": True}
        self.g.tick()
        self.g.recovery.code = 1
        self.g.tick()
        self.clock.advance(0.11)
        self.g.tick()
        self.assertEqual(len(self.b.children), 2)
        self.b.files["state.json"]["window"] = None
        self.g.recovery.code = 0
        self.clock.advance(1)
        self.g.tick()
        self.assertEqual(len(self.b.children), 2)

    def test_cross_boot_clock_rollback_triggers_immediate_takeover(self):
        self.b.window(elapsed=3)
        self.b.files["state.json"]["window"].update(boot_id="old", elapsed_floor=4)
        self.g.tick()
        self.assertIn(("stop_main",), self.b.events)

    def test_unit_text_has_required_restart_and_kill_contract(self):
        self.assertIn("Restart=no\n", M.main_unit_text())
        self.assertIn("TimeoutStopSec=0.4\n", M.main_unit_text())
        self.assertIn("KillMode=control-group\n", M.main_unit_text())
        self.assertIn("Restart=always\n", M.unit_text())


class LocalBackendTests(unittest.TestCase):
    def setUp(self):
        self.b = M.LocalBackend.__new__(M.LocalBackend)
        self.b.config, self.b.manifest = config(), {"postgres": {"system_identifier": "12345", "timeline": 1},
                                                   "containers": {"postgres": {"id": "1"*64}}}
        self.facts = facts()
        self.resource = {"State": {"Running": True, "ExitCode": 0, "OOMKilled": False},
                         "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        self.b._resource = lambda: copy.deepcopy(self.resource)
        self.queries, self.files = [], {}
        self.b.read = lambda name, missing=False: copy.deepcopy(self.files.get(name))
        self.b.write = lambda name, value: self.files.update({name: copy.deepcopy(value)})
        self.b.boot_id = lambda: "boot1"
        self.ledger = {"operations": {"a"*32: {"request": {"action": "pg_stop"}, "steps": {"sigint": "accepted"},
                                             "arm_id": "b"*32, "final_lsn": "1/100"}}}
        self.b.source = mock.Mock()
        self.b.source._ledger.side_effect = lambda: self.ledger
        self.b.source._stopped.side_effect = lambda info: M.S.require(
            info["State"]["ExitCode"] == 0 and not info["State"]["OOMKilled"] and
            info["HostConfig"]["RestartPolicy"]["Name"] == "no", "BAD_STOP")
        def sql(query):
            self.queries.append(query)
            if query != M.PG_SAMPLE:
                self.facts["role"]["login"], self.facts["slots"] = False, []
                return b""
            return M.S.canonical(self.facts)
        self.b._sql = sql
        stats = mock.Mock(f_bavail=M.FREE_MIN, f_frsize=1)
        patch = mock.patch.object(M.os, "statvfs", return_value=stats)
        patch.start()
        self.addCleanup(patch.stop)
        sleeper = mock.patch.object(M.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.stats = stats

    def test_exact_boundary_rules(self):
        self.assertTrue(self.b.sample()["pg_wal_protected"])
        self.facts["slots"][0]["retained_bytes"] = M.RETAIN_LIMIT-1
        self.b.sample()
        self.facts["slots"][0]["retained_bytes"] = M.RETAIN_LIMIT
        with self.assertRaises(M.S.Refusal):
            self.b.sample()

    def test_bad_cap_or_low_disk_refuses_sample(self):
        for cap, free in [(-1, M.FREE_MIN), (M.WAL_CAP+1, M.FREE_MIN), (M.WAL_CAP, M.FREE_MIN-1)]:
            with self.subTest(cap=cap, free=free):
                self.facts["wal_cap_bytes"], self.stats.f_bavail = cap, free
                with self.assertRaises(M.S.Refusal):
                    self.b.sample()

    def test_changed_cluster_directory_or_timeline_refuses_fence_before_sql(self):
        for field, value in [("system_identifier", "999"), ("timeline", 2),
                             ("in_recovery", True), ("data_directory", "/foreign")]:
            with self.subTest(field=field):
                self.facts = facts()
                self.facts[field] = value
                self.queries.clear()
                with self.assertRaises(M.S.Refusal):
                    self.b.fence()
                self.assertTrue(all(q == M.PG_SAMPLE for q in self.queries))

    def test_foreign_or_privileged_role_and_logical_slot_never_mutated(self):
        for field, value in [("comment", "foreign"), ("superuser", True), ("replication", False)]:
            with self.subTest(field=field):
                self.facts = facts()
                self.facts["role"][field] = value
                self.queries.clear()
                with self.assertRaises(M.S.Refusal):
                    self.b.fence()
                self.assertTrue(all(q == M.PG_SAMPLE for q in self.queries))
        self.facts = facts()
        self.facts["slots"][0]["type"] = "logical"
        with self.assertRaises(M.S.Refusal):
            self.b.fence()

    def test_fence_only_disables_owned_role_and_drops_owned_physical_slot(self):
        result = self.b.fence()
        self.assertTrue(result["pg_wal_protected"])
        mutation = next(q.decode() for q in self.queries if q != M.PG_SAMPLE)
        for fragment in ["ALTER ROLE sub2api_mig_20261016_replica NOLOGIN", "backend_type='walsender'",
                         "application_name='sub2api-pg-replica-20261016'", "host(client_addr)='172.18.0.1'",
                         "slot_name='sub2api_mig_20261016'", "slot_type='physical'", "NOT active"]:
            self.assertIn(fragment, mutation)
        self.assertNotIn("docker", mutation)
        self.assertNotIn("pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid", mutation)
        self.assertTrue(self.files["guard-fence.json"]["fenced"])
        self.assertEqual(self.b.sample()["mode"], "OWNED_REPLICA_FENCED")

    def test_missing_slot_needs_owned_completed_fence(self):
        self.facts["slots"], self.facts["role"]["login"] = [], False
        with self.assertRaises(M.S.Refusal):
            self.b.sample()

    def test_stopped_pg_only_accepts_owned_clean_fence(self):
        self.resource["State"]["Running"] = False
        self.assertEqual(self.b.sample()["mode"], "OWNED_CLEAN_STOP")
        self.assertFalse(self.queries)
        self.ledger["operations"] = {}
        with self.assertRaises(M.S.Refusal):
            self.b.sample()

    def test_crashed_or_restarting_pg_never_proves_protection(self):
        self.resource["State"].update(Running=False, ExitCode=1)
        with self.assertRaises(M.S.Refusal):
            self.b.sample()
        self.resource["State"]["ExitCode"] = 0
        self.resource["HostConfig"]["RestartPolicy"]["Name"] = "always"
        with self.assertRaises(M.S.Refusal):
            self.b.sample()

    def test_owned_clean_stop_between_inspect_and_query_is_safe(self):
        def sql(query):
            self.resource["State"]["Running"] = False
            raise M.S.Refusal("connection-lost")
        self.b._sql = sql
        self.assertEqual(self.b.sample()["mode"], "OWNED_CLEAN_STOP")

    def test_query_failure_while_still_running_is_not_hidden(self):
        self.b._sql = mock.Mock(side_effect=M.S.Refusal("timeout"))
        with self.assertRaises(M.S.Refusal):
            self.b.sample()

    def test_650ms_command_timeout_is_transient_and_keeps_exact_safe_reason(self):
        with mock.patch.object(M.subprocess, "run", side_effect=subprocess.TimeoutExpired("private command", 0.65)):
            with self.assertRaisesRegex(M.ProbeUnavailable, "^GUARD_COMMAND_TIMEOUT$"):
                self.b.command(["/usr/bin/docker"])
        with mock.patch.object(M.subprocess, "run", return_value=mock.Mock(returncode=1, stdout=b"")):
            with self.assertRaisesRegex(M.ProbeUnavailable, "^GUARD_LOCAL_COMMAND_FAILED$"):
                self.b.command(["/usr/bin/docker"])

    def test_sql_timeout_recheck_does_not_replace_original_cause_with_pg_running(self):
        self.b._sql = mock.Mock(side_effect=M.ProbeUnavailable("GUARD_COMMAND_TIMEOUT"))
        with self.assertRaisesRegex(M.ProbeUnavailable, "^GUARD_COMMAND_TIMEOUT$"):
            self.b.sample()

    def test_disk_violation_precedes_a_transient_sql_timeout(self):
        self.stats.f_bavail = M.FREE_MIN-1
        self.b._sql = mock.Mock(side_effect=M.ProbeUnavailable("GUARD_COMMAND_TIMEOUT"))
        with self.assertRaisesRegex(M.S.Refusal, "^GUARD_DISK_LOW$"):
            self.b.sample()
        self.b._sql.assert_not_called()

    def test_sql_sampling_contention_is_transient_but_bad_lock_is_not(self):
        del self.b._sql
        for reason, expected in (("PG_SAMPLING_BUSY", "GUARD_PG_SAMPLING_BUSY"),
                                 ("PG_SAMPLING_LOCK_UNTRUSTED", "PG_SAMPLING_LOCK_UNTRUSTED")):
            with self.subTest(reason=reason), \
                 mock.patch.object(M.S, "pg_sampling_lock", side_effect=M.S.Refusal(reason)):
                with self.assertRaisesRegex(M.S.Refusal, "^"+expected+"$") as caught:
                    self.b._sql(M.PG_SAMPLE)
                self.assertEqual(isinstance(caught.exception, M.ProbeUnavailable), reason == "PG_SAMPLING_BUSY")

    def test_config_is_closed_and_paths_are_canonical(self):
        self.assertEqual(M.validate_config(config()), config())
        for field, value in [("command", "anything"), ("slot_create_intended", False),
                             ("pg_data_host_path", "/tmp/../foreign"), ("pg_data_host_path", "/")]:
            candidate = config()
            candidate[field] = value
            with self.assertRaises(M.S.Refusal):
                M.validate_config(candidate)

    def test_mount_source_and_destination_must_match_exact_root_config(self):
        del self.b._resource
        raw = M.S.canonical(self.b.manifest)+b"\n"
        self.b.config["source_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory).resolve())
            self.b.config["pg_data_host_path"] = path
            self.resource["Mounts"] = [{"Destination": M.DATA, "Source": path, "RW": True}]
            self.b.source._resource.side_effect = lambda kind: copy.deepcopy(self.resource)
            with mock.patch.object(M.S, "private_bytes", return_value=raw):
                self.b._resource()
                self.resource["Mounts"][0]["Source"] = "/foreign"
                with self.assertRaises(M.S.Refusal):
                    self.b._resource()

    def test_commands_pin_full_container_id_and_timeout(self):
        del self.b._sql
        seen = []
        self.b.command = lambda argv, data=None, timeout=0.65: seen.append((argv, data, timeout)) or b"{}"
        with mock.patch.object(M.S, "pg_sampling_lock", return_value=contextlib.nullcontext()) as lock:
            self.b._sql(M.PG_SAMPLE)
        lock.assert_called_once_with()
        self.assertEqual(seen[0][0][:4], ["/usr/bin/docker", "exec", "-i", "1"*64])
        self.assertEqual(seen[0][1], M.PG_SAMPLE)

    def main_properties(self):
        return {"MainPID": "0", "Job": "", "ControlGroup": M.MAIN_CGROUP, "ActiveState": "inactive",
                "FragmentPath": "/etc/systemd/system/"+M.MAIN_UNIT, "DropInPaths": "", "LoadState": "loaded",
                "NeedDaemonReload": "no", "Restart": "no", "KillMode": "control-group", "TimeoutStopUSec": "400ms"}

    def test_unit_runtime_overrides_or_wrong_cgroup_refuse_takeover(self):
        self.b.validate_units = lambda: None
        for key, value in [("DropInPaths", "/foreign.conf"), ("Restart", "always"), ("KillMode", "process"),
                           ("TimeoutStopUSec", "90s"), ("ControlGroup", "/system.slice/foreign.service"),
                           ("NeedDaemonReload", "yes")]:
            with self.subTest(key=key):
                props = self.main_properties()
                props[key] = value
                raw = "\n".join(k+"="+v for k, v in props.items()).encode()
                self.b.command = lambda *args, **kwargs: raw
                with self.assertRaises(M.S.Refusal):
                    self.b.main_status()

    def test_empty_pid_with_populated_cgroup_is_not_quiescent(self):
        self.b.validate_units = lambda: None
        props = self.main_properties()
        raw = "\n".join(k+"="+v for k, v in props.items()).encode()
        self.b.command = lambda *args, **kwargs: raw
        with mock.patch.object(M.pathlib.Path, "exists", return_value=True), \
             mock.patch.object(M.pathlib.Path, "read_text", return_value="populated 1\nfrozen 0\n"):
            self.assertFalse(self.b.main_status()["quiescent"])
        with mock.patch.object(M.pathlib.Path, "exists", return_value=True), \
             mock.patch.object(M.pathlib.Path, "read_text", return_value="populated 0\nfrozen 0\n"):
            self.assertTrue(self.b.main_status()["quiescent"])

    def test_stop_and_kill_commands_only_name_fixed_runner_service(self):
        self.b.validate_units = lambda: None
        commands = []
        self.b.command = lambda argv, **kwargs: commands.append(argv) or b""
        self.b.stop_main()
        self.b.kill_main()
        self.assertEqual(commands, [["/usr/bin/systemctl", "--no-block", "stop", M.MAIN_UNIT],
                                    ["/usr/bin/systemctl", "kill", "--kill-who=all", "--signal=SIGKILL", M.MAIN_UNIT]])

    def test_recovery_process_has_fixed_path_and_no_arbitrary_arguments(self):
        self.b.main_status = lambda: {"running": False, "quiescent": True}
        with mock.patch.object(M.S, "private_bytes", return_value=b"trusted"), \
             mock.patch.object(M.subprocess, "Popen") as popen:
            self.b.spawn_recover()
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/python3", "-I", str(M.LIBEXEC / "runner_service.py"), "recover"])
        self.assertEqual(popen.call_args.kwargs["cwd"], M.SCOPE)
        self.b.main_status = lambda: {"running": False, "quiescent": False}
        with self.assertRaises(M.S.Refusal):
            self.b.spawn_recover()


class CohortGuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.b = Backend(self.clock)
        previous = {"v": 1, "run": M.RUN, "owner": OWNER, "phase": "SOURCE_DATA", "pg": "AWS", "redis": "AWS",
                    "attempt": 1, "pause_seconds": 7.0820385516, "window": None, "route_fenced": True}
        record = {"v": 1, "run": M.RUN, "owner": OWNER, "id": "01"*16,
                  "previous_state": previous, "previous_state_sha256": M.E.digest(previous), "proof_sha256": "cd"*32}
        self.raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()+b"\n"
        self.state = {**previous, "attempt": 2, "phase": "COMBINED_WINDOW",
                      "budget_cohort": {"id": record["id"], "record_sha256": hashlib.sha256(self.raw).hexdigest()},
                      "window": {"boot_id": "boot1", "started_at": self.clock.wall,
                                 "started_monotonic": self.clock.mono, "elapsed_floor": 0}}
        self.g = M.Guard(self.b, self.clock.now, self.clock.monotonic, record_reader=lambda ref: self.raw)

    def test_guard_uses_same_cohort_budget_as_engine_and_keeps_takeover_reserve(self):
        self.b.files["state.json"] = self.state
        self.assertAlmostEqual(self.g.remaining(self.state), 8)
        self.assertIsNone(self.g.supervise())
        self.assertFalse(any(row[0] == "stop_main" for row in self.b.events))
        self.clock.advance(7.5)
        self.assertAlmostEqual(self.g.remaining(self.state), M.RECOVERY_RESERVE)
        self.g.supervise()
        self.assertTrue(any(row[0] == "stop_main" for row in self.b.events))

    def test_restart_keeps_closed_cohort_usage_and_same_window_start(self):
        self.state["pause_seconds"] += 2
        self.clock.advance(1)
        restarted = M.Guard(self.b, self.clock.now, self.clock.monotonic, record_reader=lambda ref: self.raw)
        self.assertAlmostEqual(restarted.remaining(self.state), 5)
        self.state["window"]["boot_id"] = "old-boot"
        self.assertAlmostEqual(restarted.remaining(self.state), 5)

    def test_unknown_record_fails_closed_and_still_starts_existing_recovery_path(self):
        self.b.files["state.json"] = self.state
        self.g.record_reader = lambda ref: (_ for _ in ()).throw(FileNotFoundError())
        with self.assertRaisesRegex(M.S.Refusal, "COHORT_ACCOUNTING_UNCERTAIN"):
            self.g.remaining(self.state)
        reason = self.g.supervise()
        self.assertEqual(reason, "GUARD_STATE_OR_CLOCK_UNCERTAIN")
        self.assertTrue(any(row[0] == "stop_main" for row in self.b.events))
        self.b.main = {"running": False, "quiescent": True}
        self.g.supervise()
        self.assertTrue(any(row[0] == "recover" for row in self.b.events))

    def test_unreferenced_record_never_changes_legacy_budget(self):
        legacy = {"v": 1, "run": M.RUN, "pause_seconds": 7.08, "window": self.state["window"]}
        self.g.record_reader = mock.Mock(side_effect=AssertionError("unreferenced"))
        self.assertAlmostEqual(self.g.remaining(legacy), .92)
        self.g.record_reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
