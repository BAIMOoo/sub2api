#!/usr/bin/env python3
"""Offline fixed bridge fault tests; no SSH, iptables, services, or sockets."""
import copy
import importlib.util
import io
import pathlib
import shlex
import types
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("activate_entry_bridge", pathlib.Path(__file__).with_name("activate_entry_bridge.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
BEFORE = b"-P PREROUTING ACCEPT\n-P INPUT ACCEPT\n-P OUTPUT ACCEPT\n-P POSTROUTING ACCEPT\n-N DOCKER\n-A OUTPUT -d 127.0.0.0/8 -j RETURN\n-A POSTROUTING -s 172.18.0.0/16 -j MASQUERADE\n"
OWN = "-A OUTPUT " + shlex.join(M.RULE)


def inserted(before=BEFORE):
    lines = before.decode().splitlines()
    position = next((index for index, line in enumerate(lines) if line.startswith("-A OUTPUT ")), len(lines))
    lines.insert(position, OWN)
    return ("\n".join(lines) + "\n").encode()


def proof():
    return {"owner": "a" * 32, "machine_id_sha256": "b" * 64, "boot_id": "boot1", "gates_sha256": "c" * 64,
            "gates_accepted": True, "guard_healthy": True, "bridge_health": 200, "selftest_armed": False,
            "state": {"owner": "a" * 32, "pg": "AWS", "redis": "AWS", "window": None, "route_fenced": False},
            "main": {"ActiveState": "inactive", "SubState": "dead", "MainPID": "0", "Job": ""},
            "resources": {key: {"id": str(index) * 64, "running": True, "pid": index + 20}
                          for index, key in enumerate(("app", "postgres", "redis"), 1)},
            "target": {"pg_authority": "AWS", "redis_authority": "AWS", "pg_arm": None, "pg_promotion_intent": None,
                       "pg": {"in_recovery": True}, "redis": {"role": "slave"},
                       "persistent_endpoints": {"v": 1, "postgres": "source", "redis": "source"}},
            "runtime": {"ready": True, "serving": True, "draining": False, "background_workers_enabled": False},
            "postgres": {"endpoint": "source", "paused": False},
            "redis": {"address": "127.0.0.1:16379", "paused": False, "pubsub_cache_ready": True},
            "background": {"activated": False, "activating": False, "started": [], "failures": {}},
            "processes": {key: {"ActiveState": "active", "SubState": "running", "MainPID": str(index + 100), "Job": "",
                                "ExecMainStartTimestampMonotonic": str(index + 1000)}
                          for index, key in enumerate(M.PROCESS_UNITS)}}


class FakeStore:
    def __init__(self):
        self.state, self.nat, self.before = None, None, None
        self.fail_save, self.fail_snapshot = False, False

    def read(self):
        return copy.deepcopy(self.state)

    def snapshot(self, before, nat):
        if self.fail_snapshot:
            raise OSError("fsync snapshot")
        self.nat, self.before = nat, copy.deepcopy(before)

    def before_nat(self):
        return self.nat

    def save(self, value):
        if self.fail_save:
            raise OSError("fsync intent")
        self.state = copy.deepcopy(value)


class FakeBackend:
    def __init__(self):
        self.value, self.rules = proof(), BEFORE
        self.inserts, self.preflights = 0, 0
        self.lost_reply, self.insert_effect = False, True
        self.after_insert = lambda: None
        self.before_final = lambda: None
        self.inspection_fails = False

    def preflight(self):
        self.preflights += 1
        if self.preflights == 2:
            self.before_final()
        return M.validate_preflight(copy.deepcopy(self.value))

    def nat(self):
        if self.inspection_fails:
            raise TimeoutError()
        return self.rules

    def check(self):
        return 0 if OWN.encode() in self.rules else 1

    def processes(self):
        return copy.deepcopy(self.value["processes"])

    def resources(self):
        return copy.deepcopy(self.value["resources"])

    def insert(self):
        self.inserts += 1
        if self.insert_effect:
            self.rules = inserted(self.rules)
        self.after_insert()
        if self.lost_reply:
            raise TimeoutError()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.b, self.s = FakeBackend(), FakeStore()
        self.driver = M.Driver(self.b, self.s)

    def test_default_is_offline_and_does_not_import_or_inspect(self):
        with mock.patch.object(M, "load_service", side_effect=AssertionError("no import")), \
                mock.patch.object(M.subprocess, "run", side_effect=AssertionError("no command")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(M.main([]), 0)
            self.assertIn('"insert_attempted": false', out.getvalue())

    def test_noninstalled_apply_is_refused_before_loading_programs(self):
        with mock.patch.object(M, "load_service", side_effect=AssertionError("no import")), self.assertRaises(M.Refusal):
            M.main(["apply"])

    def test_current_controller_rule_is_statically_identical(self):
        source = pathlib.Path(__file__).with_name("runner_service.py").read_bytes()
        self.assertEqual(M.verify_controller_rule(source), M.sha(M.RULE))
        with self.assertRaises(M.Refusal):
            M.verify_controller_rule(source.replace(b'"8081"', b'"8089"', 1))

    def test_rule_parser_never_evaluates_python_calls(self):
        with self.assertRaises((M.Refusal, ValueError)):
            M.verify_controller_rule(b"COMMENT=__import__('os').system('false')\nRULE=[]\n")

    def test_only_mutation_command_is_fixed_insert_first(self):
        backend = object.__new__(M.LocalBackend)
        with mock.patch.object(backend, "command", return_value=types.SimpleNamespace(returncode=0)) as command:
            backend.insert()
        self.assertEqual(command.call_args.args[0], M.BASE + ["-I", "OUTPUT", "1"] + M.RULE)

    def test_success_snapshot_and_single_intent_before_insert(self):
        original = self.b.insert

        def insert():
            self.assertEqual(self.s.nat, BEFORE)
            self.assertEqual(self.s.state["phase"], "INTENT")
            self.assertTrue(self.s.state["attempted"])
            original()
        self.b.insert = insert
        result = self.driver.apply()
        self.assertEqual(result["phase"], "COMPLETE")
        self.assertFalse(result["automatic_rollback"])
        self.assertEqual(self.b.inserts, 1)

    def test_lost_insert_receipt_is_reconciled_without_repeat(self):
        self.b.lost_reply = True
        self.assertEqual(self.driver.apply()["phase"], "COMPLETE")
        self.assertEqual(self.driver.apply()["phase"], "COMPLETE")
        self.assertEqual(self.b.inserts, 1)
        self.assertEqual(self.b.preflights, 2)

    def test_unknown_without_insert_effect_is_never_retried(self):
        self.b.insert_effect, self.b.lost_reply = False, True
        self.assertEqual(self.driver.apply()["phase"], "UNKNOWN")
        self.assertEqual(self.driver.apply()["phase"], "UNKNOWN")
        self.assertEqual(self.b.inserts, 1)

    def test_failed_inspect_can_later_reconcile_without_mutation(self):
        self.b.after_insert = lambda: setattr(self.b, "inspection_fails", True)
        self.assertEqual(self.driver.apply()["phase"], "UNKNOWN")
        self.b.inspection_fails = False
        self.assertEqual(self.driver.apply()["phase"], "COMPLETE")
        self.assertEqual(self.b.inserts, 1)

    def test_snapshot_or_intent_fsync_failure_prevents_insert(self):
        for attribute in ("fail_snapshot", "fail_save"):
            self.setUp()
            setattr(self.s, attribute, True)
            with self.subTest(attribute=attribute), self.assertRaises(OSError):
                self.driver.apply()
            self.assertEqual(self.b.inserts, 0)

    def test_existing_unowned_bridge_is_not_adopted_or_inserted_again(self):
        self.b.rules = inserted()
        with self.assertRaisesRegex(M.Refusal, "ALREADY_PRESENT"):
            self.driver.apply()
        self.assertEqual(self.b.inserts, 0)

    def test_concurrent_nat_change_before_insert_is_refused(self):
        self.b.before_final = lambda: setattr(self.b, "rules", BEFORE + b"-A INPUT -j RETURN\n")
        with self.assertRaisesRegex(M.Refusal, "NAT_CHANGED"):
            self.driver.apply()
        self.assertEqual(self.b.inserts, 0)

    def test_other_rule_change_after_insert_is_unknown_without_rollback(self):
        self.b.after_insert = lambda: setattr(self.b, "rules", self.b.rules + b"-A INPUT -j RETURN\n")
        self.assertEqual(self.driver.apply()["phase"], "UNKNOWN")
        self.assertEqual(self.b.inserts, 1)
        self.assertIn(OWN.encode(), self.b.rules)

    def test_caddy_relay_and_channel_pid_must_all_remain_unchanged(self):
        for unit in M.PROCESS_UNITS:
            self.setUp()
            self.b.after_insert = lambda: self.b.value["processes"][unit].update(MainPID="9000")
            with self.subTest(unit=unit):
                self.assertEqual(self.driver.apply()["phase"], "UNKNOWN")
                self.assertEqual(self.b.inserts, 1)

    def test_source_resource_pid_change_is_unknown(self):
        self.b.after_insert = lambda: self.b.value["resources"]["app"].update(pid=12345)
        self.assertEqual(self.driver.apply()["phase"], "UNKNOWN")

    def test_source_and_target_gate_failures_prevent_insert(self):
        cases = [("state", "window", {}), ("state", "pg", "TENCENT"), ("main", "ActiveState", "active"),
                 ("main", "MainPID", "200"), ("target", "pg_arm", {"arm_id": "x"}),
                 ("target", "redis_authority", "TENCENT"), ("runtime", "background_workers_enabled", True),
                 ("runtime", "ready", False), ("background", "activated", True), ("postgres", "endpoint", "target")]
        for group, field, value in cases:
            self.setUp()
            self.b.value[group][field] = value
            with self.subTest(group=group, field=field), self.assertRaises(M.Refusal):
                self.driver.apply()
            self.assertEqual(self.b.inserts, 0)
        for field, value in (("selftest_armed", True), ("guard_healthy", False), ("gates_accepted", False), ("bridge_health", 503)):
            self.setUp()
            self.b.value[field] = value
            with self.subTest(field=field), self.assertRaises(M.Refusal):
                self.driver.apply()
            self.assertEqual(self.b.inserts, 0)

    def test_changed_gates_between_checks_prevent_insert(self):
        self.b.before_final = lambda: self.b.value.update(gates_sha256="d" * 64)
        with self.assertRaisesRegex(M.Refusal, "PREFLIGHT_CHANGED"):
            self.driver.apply()
        self.assertEqual(self.b.inserts, 0)

    def test_snapshot_hash_change_is_not_accepted_as_complete(self):
        self.driver.apply()
        self.s.nat += b"-A INPUT -j RETURN\n"
        result = self.driver.apply()
        self.assertEqual(result["phase"], "UNKNOWN")
        self.assertEqual(result["reason"], "BEFORE_SNAPSHOT_CHANGED")
        self.assertNotIn("after_nat_sha256", self.s.state)
        self.assertFalse(self.s.state["source_resources_unchanged"])
        self.assertEqual(self.b.inserts, 1)

    def test_unexpected_inspection_error_never_leaks_external_text(self):
        self.driver.apply()
        self.b.nat = mock.Mock(side_effect=ValueError("external secret text"))
        result = self.driver.apply()
        self.assertEqual(result["phase"], "UNKNOWN")
        self.assertEqual(result["reason"], "INSPECTION_FAILED")
        self.assertNotIn("external secret text", str(result) + str(self.s.state))
        self.assertEqual(self.b.inserts, 1)

    def test_nat_first_unique_and_other_rules_preserved(self):
        M.verify_nat(BEFORE, inserted(), 0)
        for raw in (inserted() + (OWN + "\n").encode(),
                    BEFORE + (OWN + "\n").encode(), inserted().replace(b"-j MASQUERADE", b"-j RETURN")):
            with self.subTest(raw=raw), self.assertRaises(M.Refusal):
                M.verify_nat(BEFORE, raw, 0)
        with self.assertRaises(M.Refusal):
            M.verify_nat(BEFORE, inserted(), 1)


if __name__ == "__main__":
    unittest.main()
