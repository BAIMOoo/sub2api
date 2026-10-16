#!/usr/bin/env python3
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import types
import unittest
from unittest import mock


def load(name):
    spec = importlib.util.spec_from_file_location("test_" + name, pathlib.Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R, M = load("rolling_app_route"), load("rolling_app_recovery")
OWNER = "30bda011defad8149621c2630a2bd89c"
BINDING = {"test": "fixed-reviewed-generation"}
BASE = {"output": [["-A", "OUTPUT", "!", "-d", "127.0.0.0/8", "-m", "addrtype", "--dst-type", "LOCAL", "-j", "DOCKER"]],
        "other": [["policy", "OUTPUT", "ACCEPT"], ["policy", "DOCKER", "-"]]}


class Store:
    def __init__(self, boot="first-boot", records=None):
        self.boot_id, self.records = boot, {} if records is None else records
        self.writes = 0
        self.locked = False

    @contextlib.contextmanager
    def lock(self):
        if self.locked:
            raise RuntimeError("concurrent")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def read(self):
        return copy.deepcopy(self.records.get(self.boot_id))

    def write(self, value):
        assert self.locked
        self.writes += 1
        self.records[self.boot_id] = copy.deepcopy(value)


class Backend:
    def __init__(self, nat=None):
        self.value = copy.deepcopy(BASE if nat is None else nat)
        self.inserts, self.verified = 0, 0
        self.failure = None
        self.invalid = False

    def verify(self):
        self.verified += 1
        if self.invalid:
            raise R.Refusal("ADOPTED_APPLICATION_CHANGED")

    def nat(self):
        return copy.deepcopy(self.value)

    def insert(self):
        self.inserts += 1
        if self.failure == "before":
            raise OSError("lost command")
        self.value["output"].insert(0, list(M.REDIRECT))
        if self.failure == "after":
            raise OSError("lost reply")


def route(backend=None, store=None):
    return R.Route(Backend() if backend is None else backend, Store() if store is None else store, M.REDIRECT, OWNER, BINDING)


def adoption():
    old_config, new_config = {"Env": ["SERVER_PORT=8082"]}, {"Env": ["SERVER_PORT=8083"]}
    layers = ["sha256:" + "e" * 64]
    old = {"Id": M.OLD_ID, "Image": M.IMAGE, "Config": old_config,
           "State": {"Running": False, "Status": "exited", "Pid": 0, "ExitCode": 0, "OOMKilled": False},
           "HostConfig": {"RestartPolicy": {"Name": "no"}}}
    new = {"Id": "b" * 64, "Image": M.IMAGE, "Config": new_config, "Name": "/sub2api-migration-app-20261016",
           "State": {"Running": True, "Status": "running", "OOMKilled": False},
           "HostConfig": {"RestartPolicy": {"Name": "no"}, "NetworkMode": "host", "PortBindings": {}}}
    app = {"container_id": new["Id"], "image_id": M.IMAGE, "config_sha256": M.sha(new_config), "rootfs_diffids": layers, "control_port": 18093}
    config = {"owner": OWNER, "app": app}
    new_identity = {"container_id": new["Id"], "image_id": M.IMAGE, "config_sha256": M.sha(new_config),
                    "rootfs_diffids": layers, "control_token_sha256": "c" * 64}
    old_identity = {"container_id": M.OLD_ID, "image_id": M.IMAGE, "config_sha256": M.sha(old_config), "rootfs_diffids": layers}
    completed = {"v": 1, "owner": OWNER, "new_app": new_identity, "old_container_id": M.OLD_ID, "redirect": M.REDIRECT,
                 "control_port": 18093, "control_token_file": str(M.TOKEN_FILE), "data_directory": str(M.DATA),
                 "endpoint_directory": str(R.ROOT / "app"), "old_exited_cleanly": True, "new_process_failed": 0, "new_process_dropped": 0}
    rolling = {"v": 1, "owner": OWNER, "phase": "COMPLETE", "intent": None, "old_identity": old_identity,
               "new_identity": new_identity, "adoption_receipt": completed}
    rolling_raw, controller_raw = M.canonical(rolling), M.canonical(config)
    receipt = {"v": 1, "run": "20261016", "owner": OWNER, "old_instance_id": "old", "new_instance_id": "new",
               "old_container_id": M.OLD_ID, "new_container_id": new["Id"], "new_app_config_sha256": app["config_sha256"],
               "new_control_port": 18093, "new_control_token_sha256": "c" * 64,
               "controller_config_raw_sha256": R.sha(controller_raw), "ledger_raw_sha256": R.sha(b"ledger"),
               "ledger_epoch": 14, "operation_floor": 14, "source_closed_sha256": R.sha(b"source"),
               "rolling_complete_sha256": R.sha(rolling_raw), "old_clean_exit": {"container_id": M.OLD_ID,
                   "exit_code": 0, "oom_killed": False, "pid": 0, "restart_policy": "no"}}
    return dict(receipt=receipt, rolling=rolling, config=config, old=old, new=new,
                image={"Id": M.IMAGE, "RootFS": {"Layers": layers}}, controller_raw=controller_raw, rolling_raw=rolling_raw,
                source_raw=b"source", ledger_raw=b"ledger", token_sha="c" * 64, mounts=M.MOUNTS, core=M)


class RouteTests(unittest.TestCase):
    def test_backend_reads_original_rolling_token_as_root(self):
        values = adoption()
        token = b"private-control-token"
        reads = []
        def private_read(path, **kwargs):
            if path == M.TOKEN_FILE:
                reads.append(kwargs.get("uid"))
                R.need(kwargs.get("uid") == 0, "TOKEN_OWNER_CHANGED")
                return token + b"\n"
            return b"{}"
        backend = R.Backend.__new__(R.Backend)
        backend.m = M
        backend.h = types.SimpleNamespace(private_read=private_read)
        backend.config = values["config"]
        backend.controller_raw = b"{}"
        backend.binding = {key: R.sha(b"{}") for key in
            ("route_program_sha256", "core_sha256", "host_sha256", "adoption_sha256")}
        backend.r = types.SimpleNamespace(load_config=lambda: backend.config,
            private_read=private_read, SCOPE=R.ROOT / "rpc", APP_CONTAINER="new",
            strict_json=lambda raw, limit: json.loads(raw))
        mounts = [{"Source": row["source"], "Destination": row["destination"], "RW": row["rw"]}
                  for row in M.MOUNTS]
        for order in (mounts, list(reversed(mounts))):
            backend.local = types.SimpleNamespace(_app_token=lambda: token,
                docker_json=lambda kind, identity: {"Mounts": order})
            with mock.patch.object(R, "validate_adoption") as validated:
                backend.verify()
                self.assertEqual(validated.call_args.kwargs["mounts"], M.MOUNTS)
        self.assertEqual(reads, [0, 0])

    def test_default_offline_never_loads_host(self):
        with mock.patch.object(R, "load_route", side_effect=AssertionError("offline")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(R.main([]), 0)
        self.assertTrue(json.loads(out.getvalue())["offline"])

    def test_cli_never_echoes_private_exception(self):
        with mock.patch.object(R, "load_route", side_effect=RuntimeError("private-token")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(R.main(["ensure"]), 1)
        self.assertNotIn("private-token", out.getvalue())

    def test_exact_first_rule_is_read_only_even_without_intent(self):
        b, s = Backend(), Store()
        b.value["output"].insert(0, M.REDIRECT)
        self.assertFalse(route(b, s).ensure()["inserted"])
        self.assertEqual((b.inserts, s.writes), (0, 0))

    def test_inspect_is_read_only(self):
        b, s = Backend(), Store()
        self.assertEqual(route(b, s).inspect()["status"], "absent")
        self.assertEqual((b.inserts, s.writes), (0, 0))

    def test_insert_records_durable_intent_and_preserves_other_nat(self):
        b, s = Backend(), Store()
        original_insert = b.insert
        def checked_insert():
            self.assertEqual(s.read()["status"], "INTENT")
            self.assertTrue(s.locked)
            original_insert()
        b.insert = checked_insert
        self.assertTrue(route(b, s).ensure()["inserted"])
        self.assertEqual(s.read()["status"], "COMPLETE")
        expected = copy.deepcopy(BASE)
        expected["output"].insert(0, M.REDIRECT)
        self.assertEqual(b.value, expected)
        self.assertFalse(route(b, s).ensure()["inserted"])
        self.assertEqual(b.inserts, 1)

    def test_missing_route_after_unknown_is_never_replayed_same_boot(self):
        b, s = Backend(), Store()
        b.failure = "before"
        with self.assertRaisesRegex(R.Refusal, "ROUTE_OUTCOME_UNKNOWN_INSPECT_ONLY"):
            route(b, s).ensure()
        b.failure = None
        with self.assertRaisesRegex(R.Refusal, "SAME_BOOT_ROUTE_MISSING_INSPECT_ONLY"):
            route(b, s).ensure()
        self.assertEqual(b.inserts, 1)
        self.assertEqual(s.read()["status"], "UNKNOWN")

    def test_lost_ack_reconciles_present_rule_without_repeat(self):
        b, s = Backend(), Store()
        b.failure = "after"
        with self.assertRaises(R.Refusal):
            route(b, s).ensure()
        self.assertFalse(route(b, s).ensure()["inserted"])
        self.assertEqual((b.inserts, s.read()["status"]), (1, "COMPLETE"))

    def test_unknown_rule_with_other_nat_change_is_not_adopted(self):
        b, s = Backend(), Store()
        b.failure = "after"
        with self.assertRaises(R.Refusal):
            route(b, s).ensure()
        b.value["other"].append(["policy", "NEW_CHAIN", "-"])
        with self.assertRaisesRegex(R.Refusal, "UNKNOWN_ROUTE_BASELINE_CHANGED"):
            route(b, s).ensure()
        self.assertEqual(b.inserts, 1)

    def test_same_boot_deleted_complete_rule_cannot_reinsert_but_new_boot_can(self):
        b, s = Backend(), Store()
        route(b, s).ensure()
        b.value = copy.deepcopy(BASE)
        with self.assertRaisesRegex(R.Refusal, "SAME_BOOT_ROUTE_MISSING_INSPECT_ONLY"):
            route(b, s).ensure()
        next_boot = Store("second-boot", s.records)
        self.assertTrue(route(b, next_boot).ensure()["inserted"])
        self.assertEqual(s.records["first-boot"]["status"], "COMPLETE")
        self.assertEqual(b.inserts, 2)

    def test_owned_rule_malformed_duplicate_other_chain_or_not_first_refused(self):
        changed = list(M.REDIRECT)
        changed[changed.index("--uid-owner") + 1] = "0"
        for output, other in [([changed], []), ([M.REDIRECT, M.REDIRECT], []), ([BASE["output"][0], M.REDIRECT], []),
                              ([], [["-A", "PREROUTING"] + M.REDIRECT[2:]])]:
            b, s = Backend({"output": output, "other": other}), Store()
            with self.subTest(output=output, other=other), self.assertRaises(R.Refusal):
                route(b, s).ensure()
            self.assertEqual((b.inserts, s.writes), (0, 0))

    def test_chain_conflicts_refused_and_disjoint_rules_preserved(self):
        disjoint = [["-A", "OUTPUT", "-p", "udp", "-j", "CUSTOM"], ["-A", "OUTPUT", "--dport", "18000", "-j", "RETURN"],
                    ["-A", "OUTPUT", "-d", "10.0.0.0/8", "-j", "CUSTOM"], ["-A", "OUTPUT", "--uid-owner", "0", "-j", "CUSTOM"]]
        for row in disjoint:
            self.assertFalse(R.touches_app_destination(row))
        for row in [["-A", "OUTPUT", "-j", "CUSTOM"], ["-A", "OUTPUT", "--dport", "8000:9000", "-j", "REDIRECT"],
                    ["-A", "OUTPUT", "!", "--dport", "18000", "-j", "CUSTOM"]]:
            with self.subTest(row=row), self.assertRaisesRegex(R.Refusal, "OUTPUT_CHAIN_CONFLICT"):
                route(Backend({"output": [row], "other": []})).ensure()

    def test_invalid_adoption_blocks_before_any_intent_or_command(self):
        b, s = Backend(), Store()
        b.invalid = True
        with self.assertRaises(R.Refusal):
            route(b, s).ensure()
        self.assertEqual((b.inserts, s.writes), (0, 0))

    def test_boot_binding_change_refuses_unknown_adoption(self):
        b, s = Backend(), Store()
        b.failure = "after"
        with self.assertRaises(R.Refusal):
            route(b, s).ensure()
        changed = R.Route(b, s, M.REDIRECT, OWNER, {"test": "different"})
        with self.assertRaisesRegex(R.Refusal, "BOOT_ROUTE_INTENT_CHANGED"):
            changed.ensure()

    def test_complete_adoption_and_stopped_new_application_valid(self):
        values = adoption()
        R.validate_adoption(**values)
        values["new"]["State"]["Running"] = False
        values["new"]["State"]["Status"] = "exited"
        R.validate_adoption(**values)

    def test_changed_generation_config_old_exit_token_mount_and_hash_refused(self):
        for field, subfield, value in [("old", "Id", "f" * 64), ("new", "Id", "f" * 64),
                                       ("new", "Name", "/other"), ("new", "Config", {"Env": ["changed"]}),
                                       ("receipt", "new_control_port", 18092), ("rolling", "phase", "DRAINING")]:
            values = adoption()
            values[field][subfield] = value
            with self.subTest(field=field, subfield=subfield), self.assertRaises(R.Refusal):
                R.validate_adoption(**values)
        for field, value in [("controller_raw", b"changed"), ("rolling_raw", b"changed"), ("source_raw", b"changed"),
                             ("ledger_raw", b"changed"), ("token_sha", "f" * 64), ("mounts", [])]:
            values = adoption()
            values[field] = value
            with self.subTest(field=field), self.assertRaises(R.Refusal):
                R.validate_adoption(**values)
        for key, value in [("Running", True), ("Status", "running"), ("Pid", 10), ("ExitCode", 137), ("OOMKilled", True)]:
            values = adoption()
            values["old"]["State"][key] = value
            with self.subTest(old_state=key), self.assertRaises(R.Refusal):
                R.validate_adoption(**values)


if __name__ == "__main__":
    unittest.main()
