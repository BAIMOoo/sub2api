#!/usr/bin/env python3
"""Offline adoption faults: private files, fake Docker and fixed GET controls."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import runpy
import stat
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


A = load("adoption_tested", "adopt_recovery_app.py")
R = load("adoption_rpc_fixture", "target_rpc.py")
H = load("adoption_host_fixture", "rolling_app_host.py")
C = load("adoption_core_fixture", "rolling_app_recovery.py")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def controls():
    return {"runtime": {"instance_id": "new-instance", "ready": True, "serving": True,
                        "draining": False, "background_workers_enabled": False,
                        "tasks": {k: {"failed": 0, "dropped": 0} for k in R.REQUIRED_WRITERS}},
            "postgres": {"endpoint": "source", "paused": False},
            "redis": {"address": "127.0.0.1:16379", "paused": False, "pubsub_cache_ready": True},
            "background": {"activated": False, "activating": False, "started": [], "failures": {}},
            "ingress": {"paused": False}, "writers": {"held": False}}


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.clock = types.SimpleNamespace(time=lambda: 100)
        self.env = {"need": A.need, "time": self.clock,
                    "payload": {"owner": A.OWNER, "source_state_sha": A.SOURCE_STATE_SHA},
                    "backend_module": R, "canonical": canonical}
        exec(A.VALIDATION, self.env)

    def test_lifecycle_comparison_allows_health_churn_but_refuses_any_lifecycle_change(self):
        before = {"Running": True, "Pid": 123, "StartedAt": "original", "Status": "running",
                  "ExitCode": 0, "OOMKilled": False, "Paused": False, "Restarting": False, "Dead": False,
                  "Health": {"Log": [{"End": "earlier"}]}}
        after = copy.deepcopy(before)
        after["Health"]["Log"].append({"End": "later"})
        self.assertTrue(self.env["same_lifecycle"](before, after))
        for field, value in [("Running", False), ("Pid", 124), ("StartedAt", "restarted"),
                             ("Status", "exited"), ("ExitCode", 1), ("OOMKilled", True),
                             ("Paused", True), ("Restarting", True), ("Dead", True), ("Pid", True)]:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(after)
                changed[field] = value
                self.assertFalse(self.env["same_lifecycle"](before, changed))
        del after["Dead"]
        self.assertFalse(self.env["same_lifecycle"](before, after))

    def test_source_window_history_and_old_quiet_identity_are_required(self):
        state = {"v": 1, "run": "20261016", "owner": A.OWNER, "attempt": 3,
                 "phase": "SOURCE_DATA", "pg": "AWS", "redis": "AWS", "window": None,
                 "deadline_breached": False, "route_fenced": True,
                 "pause_seconds": 14.656846920028329, "quiet_instance_id": "old-instance"}
        self.env["boundary"](state)
        for field, value in [("attempt", 4), ("pg", "TENCENT"), ("window", {}),
                             ("pause_seconds", 0), ("quiet_instance_id", "")]:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.env["boundary"](dict(state, **{field: value}))

    def test_fresh_source_cannot_claim_unlocked_or_fenced_state(self):
        proof = {"ok": True, "owner": A.OWNER, "checked_at": 99,
                 "state_raw_sha256": A.SOURCE_STATE_SHA, "main_quiescent": True,
                 "guard_healthy": True, "no_fence": True, "lock_held": True}
        self.env["fresh_source"](proof)
        for field, value in [("checked_at", 79), ("checked_at", 101), ("owner", "other"),
                             ("lock_held", False), ("no_fence", False), ("guard_healthy", False)]:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.env["fresh_source"](dict(proof, **{field: value}))

    def test_controls_allow_live_requests_but_refuse_writers_or_changed_generation(self):
        rows = controls()
        rows["runtime"]["active_requests"] = 3
        backend = types.SimpleNamespace(app_control=lambda name: {
            "http_status": 200, "body": copy.deepcopy(rows[name.split(".")[0]])})
        self.assertEqual(self.env["healthy_controls"](backend)["active_requests"], 3)
        for component, field, value in [("runtime", "ready", False), ("redis", "pubsub_cache_ready", False),
                                        ("writers", "held", True), ("background", "activated", True)]:
            original = copy.deepcopy(rows)
            rows[component][field] = value
            with self.subTest(component=component), self.assertRaises(RuntimeError):
                self.env["healthy_controls"](backend)
            rows.clear(); rows.update(original)
        rows["runtime"]["tasks"]["ops_system_log"]["failed"] = 1
        with self.assertRaisesRegex(RuntimeError, "writer-failure"):
            self.env["healthy_controls"](backend)


class CoordinatorTests(unittest.TestCase):
    def fixture(self, failure=None):
        events = []

        class Holder:
            def receive(self, label):
                events.append(label)
                if label == failure:
                    raise RuntimeError("lost reply")
                return {"source-prepared": {"checked_at": 100},
                        "source-verified": {"source_ledgers_preserved": True, "read_only_wire_verified": True},
                        "source-released": {"released": True}}[label]

            def send(self, value):
                events.append(value["command"])

        def remote(label, code, payload):
            events.append(label)
            if label == failure:
                raise RuntimeError("lost reply")
            if label == "target-adopt":
                return {"new_instance_id": "new"}
            return {"source_and_target_verified": True}

        return Holder(), remote, events

    def test_source_lock_is_released_only_after_both_verifications(self):
        holder, remote, events = self.fixture()
        result = A.coordinate(holder, remote, {}, clock=lambda: 101)
        self.assertTrue(result["target_verified"])
        self.assertEqual(events, ["source-prepared", "target-adopt", "install-source", "source-verified",
                                  "target-verify", "release", "source-released"])

    def test_unknown_at_any_stage_does_not_repeat_effect_or_advance(self):
        for failure in ("source-prepared", "target-adopt", "source-verified", "target-verify"):
            with self.subTest(failure=failure):
                holder, remote, events = self.fixture(failure)
                with self.assertRaisesRegex(RuntimeError, "lost reply"):
                    A.coordinate(holder, remote, {}, clock=lambda: 101)
                self.assertEqual(events.count("target-adopt"), int(failure != "source-prepared"))
                self.assertNotIn("release", events)

    def test_stale_proof_never_attempts_target_adoption(self):
        holder, remote, events = self.fixture()
        with self.assertRaisesRegex(RuntimeError, "source-proof-aged"):
            A.coordinate(holder, remote, {}, clock=lambda: 121)
        self.assertEqual(events, ["source-prepared"])

    def test_default_never_starts_ssh_and_remote_programs_compile(self):
        with mock.patch.object(A.subprocess, "Popen") as popen, mock.patch.object(A.subprocess, "run") as run:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(A.main([]), 0)
        popen.assert_not_called(); run.assert_not_called()
        self.assertTrue(json.loads(out.getvalue())["offline"])
        common = runpy.run_path(str(pathlib.Path(__file__).with_name("recover_source_guard.py")))["COMMON"]
        compile(common.replace("payload=json.load(sys.stdin)", "payload=json.loads(sys.stdin.buffer.readline(2*1024*1024))") + A.VALIDATION + A.SOURCE,
                "source-holder", "exec")
        for script in (A.TARGET_ADOPT, A.TARGET_VERIFY):
            compile(common + A.VALIDATION + script, "target-adoption", "exec")

    def test_apply_requires_reviewed_digest_before_any_ssh_or_archive(self):
        with mock.patch.object(A.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "reviewed-rpc-digest-required"):
                A.main(["--apply"])
        popen.assert_not_called()

    def test_private_receipts_are_exclusive_and_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "intent.json"
            A.private_write(path, {"ok": True})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                A.private_write(path, {"ok": False})
            self.assertTrue(json.loads(path.read_bytes())["ok"])


class TargetProgramTests(unittest.TestCase):
    """Run the actual target program against private temp files and fake Docker.

    The real narrow receipt validator is retained. A successful test therefore
    proves that installer-produced raw/config/token hashes fit the RPC schema.
    """
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.lib = self.root / "lib"; self.lib.mkdir()
        self.scope = self.root / "rolling-recovery"; self.scope.mkdir()
        (self.scope / "history").mkdir()
        self.rpcscope = self.root / "rpc"; self.rpcscope.mkdir()
        self.oldrpc = b"# old reviewed rpc\n"
        self.newrpc = b"# new reviewed rpc\n"
        self.oldtoken = b"o" * 64 + b"\n"
        self.newtoken = b"n" * 64 + b"\n"
        self.program = self.lib / "target_rpc.py"
        self.program.write_bytes(self.oldrpc)
        (self.lib / "rolling_app_host.py").write_bytes(b"host")
        (self.lib / "rolling_app_recovery.py").write_bytes(b"core")
        self.oldconfig = {"Env": ["SERVER_PORT=8082"]}
        self.newconfig = {"Env": ["SERVER_PORT=8083"]}
        common = {"image_id": C.IMAGE, "rootfs_diffids": ["sha256:" + "f" * 64]}
        self.cfg = {"v": 1, "run": "20261016", "owner": A.OWNER, "program_sha256": sha(self.oldrpc),
                    "machine_id_sha256": "1" * 64,
                    "app": {**common, "container_id": C.OLD_ID, "config_sha256": R.sha(self.oldconfig), "control_port": 18092},
                    "pg": {**common, "container_id": "2" * 64, "config_sha256": "3" * 64, "owner": A.OWNER,
                           "volume_path": "/fixed/pg", "system_identifier": "123", "source_timeline": 1},
                    "redis": {**common, "container_id": "4" * 64, "config_sha256": "5" * 64, "owner": A.OWNER,
                              "volume_path": "/fixed/redis"}}
        self.old = {"Id": C.OLD_ID, "Name": "/" + R.APP_CONTAINER, "Image": C.IMAGE, "Config": self.oldconfig,
                    "State": {"Running": False, "Status": "exited", "Pid": 0, "ExitCode": 0, "OOMKilled": False,
                              "StartedAt": "old-start", "Paused": False, "Restarting": False, "Dead": False},
                    "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        self.new = {"Id": "a" * 64, "Name": "/" + C.NEW_NAME, "Image": C.IMAGE, "Config": self.newconfig,
                    "State": {"Running": True, "Status": "running", "Pid": os.getpid(), "StartedAt": "same",
                              "ExitCode": 0, "OOMKilled": False, "Paused": False, "Restarting": False, "Dead": False},
                    "Mounts": [{"Source": row["source"], "Destination": row["destination"], "RW": row["rw"]} for row in C.MOUNTS]}
        self.oldident = {**common, "container_id": C.OLD_ID, "config_sha256": R.sha(self.oldconfig)}
        self.newident = {**common, "container_id": self.new["Id"], "config_sha256": R.sha(self.newconfig),
                         "control_token_sha256": sha(self.newtoken.strip())}
        self.rolling = {"v": 1, "owner": A.OWNER, "phase": "COMPLETE", "intent": None,
                        "old_identity": self.oldident, "new_identity": self.newident,
                        "quiet": {"samples": 3}, "completed_intents": [{"kind": k} for k in ("archive", "prepare", "start", "route", "term")],
                        "nat_before": {"output": [], "other": []},
                        "adoption_receipt": {"v": 1, "owner": A.OWNER, "old_container_id": C.OLD_ID,
                         "new_app": self.newident, "control_port": 18093, "endpoint_directory": str(self.root / "app"),
                         "endpoints": C.ENDPOINTS, "postgres_authority": "AWS", "redis_authority": "AWS",
                         "old_exited_cleanly": True, "new_process_failed": 0, "new_process_dropped": 0}}
        request = {"action": "app.quiesce", "expected_epoch": 12}
        self.ledger = {"v": 1, "run": "20261016", "owner": A.OWNER, "epoch": 14,
                       "operations": {"6" * 32: {"request": request, "phase": "COMPLETE", "cancelled": True, "instance_id": "old-instance"}},
                       "pg": {"authority": "AWS", "arm": None, "promotion_intent": None}, "redis": {"authority": "AWS"}}
        self.ledgerraw = canonical(self.ledger)
        self.cfgraw = canonical(self.cfg)
        (self.rpcscope / "state.json").write_bytes(self.ledgerraw)
        (self.rpcscope / "config.json").write_bytes(self.cfgraw)
        (self.rpcscope / "app-control.token").write_bytes(self.oldtoken)
        (self.scope / "state.json").write_bytes(canonical(self.rolling))
        (self.scope / "app-control.token").write_bytes(self.newtoken)
        (self.scope / "history/old-runtime.json").write_bytes(canonical({"instance_id": "old-instance"}))
        (self.scope / "source-controller-closed.json").write_bytes(canonical({"owner": A.OWNER, "run": "20261016",
          "window": None, "controller_stopped": True, "pg": "AWS", "redis": "AWS", "target_ledger_sha256": sha(self.ledgerraw)}))
        ticks = H.process_start_ticks(pathlib.Path("/proc", str(os.getpid()), "stat").read_text()) if pathlib.Path("/proc").exists() else 99
        (self.scope / "new-process.json").write_bytes(canonical({"pid": os.getpid(), "start_ticks": ticks, "started_at": "same"}))
        self.payload = {"owner": A.OWNER, "label": A.LABEL, "retired_name": A.RETIRED_NAME,
                        "old_rpc_sha": sha(self.oldrpc), "new_rpc_sha": sha(self.newrpc),
                        "rpc_program": __import__("base64").b64encode(self.newrpc).decode(),
                        "target_config_sha": sha(self.cfgraw), "target_ledger_sha": sha(self.ledgerraw),
                        "source_state_sha": A.SOURCE_STATE_SHA, "host_sha": sha(b"host"), "core_sha": sha(b"core"),
                        "source": {"ok": True, "owner": A.OWNER, "checked_at": time.time(),
                         "state_raw_sha256": A.SOURCE_STATE_SHA, "main_quiescent": True, "guard_healthy": True,
                         "no_fence": True, "lock_held": True, "quiet_instance_id": "old-instance"}}
        self.rows = controls()
        self.mutations = []
        self.fail_after = None
        self.health_churn = False
        self.lifecycle_fault = None
        fixture = self

        class Store:
            def __init__(self, cfg):
                pass

            def mutation_lock(self):
                return contextlib.nullcontext()

            def read(self):
                return json.loads((fixture.rpcscope / "state.json").read_bytes())

        class Backend(R.LocalBackend):
            def docker_json(self, kind, identity):
                return copy.deepcopy(fixture.old if identity == C.OLD_ID else fixture.new)

            def resource(self, kind):
                if kind == "app":
                    A.need(fixture.new["Name"] == "/" + R.APP_CONTAINER, "wrong-container-name")
                    A.need(R.sha(fixture.new["Config"]) == self.config["app"]["config_sha256"], "wrong-config")
                return copy.deepcopy(fixture.new)

            def app_control(self, name):
                A.need(name.endswith(".status"), "production-POST-forbidden")
                return {"http_status": 200, "body": copy.deepcopy(fixture.rows[name.split(".")[0]])}

            def endpoint_state(self):
                return copy.deepcopy(C.ENDPOINTS)

            def pg_locks(self):
                return contextlib.nullcontext()

            def pg_manifest(self):
                return {"authority_mode": "STANDBY", "promotion_intent": None}

            def pg_sample(self):
                return {"in_recovery": True, "system_identifier": "123", "source_timeline": 1}

            def redis_inspect(self):
                return {"role": "slave", "run_id": R.REDIS_TARGET_RUN_ID, "failover_state": "no-failover"}

        self.backend = Backend
        self.store = Store
        h = types.SimpleNamespace(_docker=lambda kind, identity: copy.deepcopy(self.old if identity == C.OLD_ID else self.new))

        class Recovery:
            def __init__(self, *args):
                pass

            def _observe(self, rolling):
                return {"old": {"state": "exited", "exit_code": 0, "oom_killed": False},
                        "new": {"instance_id": "new-instance"}}

            def _new(self, *args, **kwargs):
                pass

        core = types.SimpleNamespace(OLD_ID=C.OLD_ID, NEW_NAME=C.NEW_NAME, TOKEN_FILE=self.scope / "app-control.token",
                                     MOUNTS=C.MOUNTS, ENDPOINTS=C.ENDPOINTS, REDIRECT=C.REDIRECT,
                                     copy=copy, Recovery=Recovery)
        host = types.SimpleNamespace(load_host=lambda: (core, h, {}),
                                     parse_nat=lambda *args: {"output": [C.REDIRECT], "other": []},
                                     process_start_ticks=lambda raw: ticks)

        def command(argv):
            if argv[:3] == ["/usr/bin/docker", "container", "ls"]:
                return (self.old["Name"][1:] + "\n" + self.new["Name"][1:] + "\n").encode()
            if argv[:2] == ["/usr/bin/docker", "rename"]:
                label = "retire-old" if argv[2] == C.OLD_ID else "adopt-new"
                self.assertTrue((self.scope / A.LABEL / (label + ".intent.json")).is_file())
                row = self.old if argv[2] == C.OLD_ID else self.new
                row["Name"] = "/" + argv[3]
                if self.health_churn:
                    row["State"].setdefault("Health", {}).setdefault("Log", []).append({"End": "async-healthcheck"})
                if self.lifecycle_fault and self.lifecycle_fault[0] == label:
                    row["State"][self.lifecycle_fault[1]] = self.lifecycle_fault[2]
                self.mutations.append(label)
                if self.fail_after == label:
                    raise RuntimeError("rename-reply-lost")
                return b""
            self.assertEqual(argv, ["/usr/sbin/iptables-save", "-t", "nat"])
            return b"nat"

        def read(path, mode=0o600, **kwargs):
            return pathlib.Path(path).read_bytes()

        def write(path, raw, mode=0o600):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(fd, "wb") as out:
                out.write(raw)

        def atomic(path, raw, mode=0o600):
            self.mutations.append(path.name)
            path.write_bytes(raw)

        self.env = {"root": self.root, "lib": self.lib, "payload": self.payload,
                    "need": A.need, "sha": sha, "trusted": read, "write_new": write,
                    "atomic": atomic, "sync": lambda path: None, "canonical": canonical,
                    "load": lambda name, path: host if path.name == "rolling_app_host.py" else core if path.name == "rolling_app_recovery.py" else R,
                    "command": command, "pathlib": pathlib, "time": time, "json": json,
                    "base64": __import__("base64")}
        self.patches = [mock.patch.object(R, "SCOPE", self.rpcscope), mock.patch.object(R, "PREFIX", self.root),
                        mock.patch.object(R, "ADOPTION_SCOPE", self.scope), mock.patch.object(R, "LocalBackend", Backend),
                        mock.patch.object(R, "FileStore", Store), mock.patch.object(R, "private_read", side_effect=read),
                        mock.patch.object(R, "load_config", side_effect=lambda: json.loads((self.rpcscope / "config.json").read_bytes()))]
        for patch in self.patches:
            patch.start(); self.addCleanup(patch.stop)
        # Linux /proc is absent on the developer's macOS. Keep only this fixed
        # process-stat read virtual while retaining all private fixture files.
        self.real_read_text = pathlib.Path.read_text
        self.proc_patch = mock.patch.object(pathlib.Path, "read_text", lambda path, *a, **kw:
            "fixed-proc-stat" if str(path).startswith("/proc/") else self.real_read_text(path, *a, **kw))
        self.proc_patch.start(); self.addCleanup(self.proc_patch.stop)

    def execute(self, code):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            exec(A.VALIDATION + code, self.env)
        return json.loads(out.getvalue())

    def test_full_target_adoption_fits_real_rpc_receipt_without_mutating_history(self):
        result = self.execute(A.TARGET_ADOPT)
        self.assertTrue(result["read_only_receipt_verified"])
        self.assertEqual(self.mutations, ["retire-old", "adopt-new", "app-control.token", "target_rpc.py", "config.json"])
        self.assertEqual((self.rpcscope / "state.json").read_bytes(), self.ledgerraw)
        self.assertEqual((self.scope / "controller-ledger-before.json").read_bytes(), self.ledgerraw)
        self.assertEqual(self.old["Config"], self.oldconfig)
        self.assertEqual(self.new["Config"], self.newconfig)
        self.payload["target"] = result
        final = self.execute(A.TARGET_VERIFY)
        self.assertTrue(final["source_and_target_verified"])
        self.assertEqual((self.rpcscope / "state.json").read_bytes(), self.ledgerraw)
        self.assertEqual(json.loads((self.rpcscope / "config.json").read_bytes())["app"]["control_port"], 18093)

    def check_mounted_target(self):
        exec(A.VALIDATION + A.TARGET_COMMON, self.env)
        self.old["Name"] = "/" + A.RETIRED_NAME
        self.new["Name"] = "/" + R.APP_CONTAINER
        cfg = copy.deepcopy(self.cfg)
        cfg["app"].update(container_id=self.new["Id"], config_sha256=R.sha(self.newconfig), control_port=18093)
        return self.env["target_check"](R, cfg, (self.scope / "state.json").read_bytes(), self.cfg, "new-instance")

    def test_target_check_accepts_both_docker_mount_orders_without_mutation(self):
        for mounts in (copy.deepcopy(self.new["Mounts"]), list(reversed(self.new["Mounts"]))):
            with self.subTest(mounts=mounts):
                self.new["Mounts"] = mounts
                self.check_mounted_target()
                self.assertEqual(self.mutations, [])
                self.assertEqual((self.rpcscope / "state.json").read_bytes(), self.ledgerraw)

    def test_target_check_rejects_mount_value_count_and_duplicate_changes(self):
        original = copy.deepcopy(self.new["Mounts"])
        invalid = []
        for key, value in (("Source", "/wrong-endpoints"), ("RW", True), ("Destination", "/other")):
            mounts = copy.deepcopy(original)
            mounts[1][key] = value
            invalid.append(mounts)
        invalid.extend([original[:1], original + [original[1]], [original[0], original[0]]])
        for mounts in invalid:
            with self.subTest(mounts=mounts):
                self.new["Mounts"] = mounts
                with self.assertRaisesRegex(RuntimeError, "adopted-mounts-changed"):
                    self.check_mounted_target()
                self.assertEqual(self.mutations, [])
                self.assertEqual((self.rpcscope / "state.json").read_bytes(), self.ledgerraw)

    def test_lost_rename_reply_never_replays_or_installs_controller(self):
        self.fail_after = "retire-old"
        with self.assertRaisesRegex(RuntimeError, "rename-reply-lost"):
            self.execute(A.TARGET_ADOPT)
        self.assertEqual(self.mutations, ["retire-old"])
        self.assertEqual(self.program.read_bytes(), self.oldrpc)
        self.assertEqual((self.rpcscope / "config.json").read_bytes(), self.cfgraw)
        with self.assertRaises(RuntimeError):
            self.execute(A.TARGET_ADOPT)
        self.assertEqual(self.mutations, ["retire-old"])
        self.assertEqual((self.rpcscope / "state.json").read_bytes(), self.ledgerraw)

    def test_success_cannot_be_reapplied(self):
        self.execute(A.TARGET_ADOPT)
        effects = list(self.mutations)
        with self.assertRaisesRegex(RuntimeError, "target-old-program-or-config-changed"):
            self.execute(A.TARGET_ADOPT)
        self.assertEqual(self.mutations, effects)

    def test_asynchronous_health_log_update_during_rename_is_accepted(self):
        self.health_churn = True
        result = self.execute(A.TARGET_ADOPT)
        self.assertTrue(result["read_only_receipt_verified"])
        self.assertEqual(self.old["State"]["Health"]["Log"], [{"End": "async-healthcheck"}])
        self.assertEqual(self.new["State"]["Health"]["Log"], [{"End": "async-healthcheck"}])
        self.assertEqual((self.rpcscope / "state.json").read_bytes(), self.ledgerraw)

    def test_lifecycle_change_during_rename_refuses_controller_installation(self):
        self.lifecycle_fault = ("adopt-new", "Pid", os.getpid() + 100)
        with self.assertRaisesRegex(RuntimeError, "rename-changed-container"):
            self.execute(A.TARGET_ADOPT)
        self.assertEqual(self.program.read_bytes(), self.oldrpc)
        self.assertEqual((self.rpcscope / "config.json").read_bytes(), self.cfgraw)
        self.assertEqual((self.rpcscope / "state.json").read_bytes(), self.ledgerraw)

    def test_changed_ledger_blocks_all_effects(self):
        (self.rpcscope / "state.json").write_bytes(canonical(dict(self.ledger, epoch=15)))
        with self.assertRaisesRegex(RuntimeError, "target-ledger-CAS-changed"):
            self.execute(A.TARGET_ADOPT)
        self.assertEqual(self.mutations, [])

    def test_post_install_failed_writer_never_claims_success_and_retains_archive(self):
        self.rows["runtime"]["tasks"]["ops_system_log"]["dropped"] = 1
        with self.assertRaisesRegex(RuntimeError, "writer-failure"):
            self.execute(A.TARGET_ADOPT)
        self.assertTrue((self.scope / A.LABEL / "install-controller.intent.json").is_file())
        self.assertFalse((self.scope / A.LABEL / "target-verified.json").exists())
        self.assertEqual((self.scope / A.LABEL / "old-app-control.token").read_bytes(), self.oldtoken)
        self.assertEqual((self.rpcscope / "state.json").read_bytes(), self.ledgerraw)


class SourceProgramTests(unittest.TestCase):
    def exercise(self, release=True, fail_observer=False):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            scope = root / "runner"; scope.mkdir()
            lib = root / "lib"; lib.mkdir()
            oldrpc, newrpc = b"# old rpc\n", b"# new rpc\n"
            (lib / "target_rpc.py").write_bytes(oldrpc)
            (lib / "runner_guard.py").write_bytes(b"guard")
            state = {"v": 1, "run": "20261016", "owner": A.OWNER, "attempt": 3,
                     "phase": "SOURCE_DATA", "pg": "AWS", "redis": "AWS", "window": None,
                     "deadline_breached": False, "route_fenced": True,
                     "pause_seconds": 14.656846920028329, "quiet_instance_id": "old-instance"}
            files = {name: canonical({}) for name in ("source-operations.json", "runner-config.json", "guard-config.json", "source-manifest.json")}
            files.update({"state.json": canonical(state), "transport-state.json": canonical({"target_epoch": 14})})
            for name, raw in files.items():
                (scope / name).write_bytes(raw)
            target = {"ok": True, "new_rpc_sha256": sha(newrpc), "ledger_raw_sha256": A.TARGET_LEDGER_SHA,
                      "epoch": 14, "new_instance_id": "new-instance"}
            commands = canonical({"command": "install-source", "target": target})
            if release:
                commands += canonical({"command": "release"})
            locked, events = [False], []

            @contextlib.contextmanager
            def lock():
                locked[0] = True
                try:
                    yield
                finally:
                    locked[0] = False

            def resource(kind):
                return {"State": {"Pid": {"app": 0, "postgres": 972839, "redis": 972827}[kind],
                                  "Running": kind != "app", "OOMKilled": False, "ExitCode": 0},
                        "HostConfig": {"RestartPolicy": {"Name": "no"}}}

            sourceops = types.SimpleNamespace(_resource=resource, _assert_monitors_retired=lambda: None,
                _guard=lambda: {"healthy": True, "tripped": False, "recovering": False, "pg_wal_protected": True,
                                "boot_id": "boot", "checked_at": time.time(), "sample": {"mode": "RUNNING_BOUNDED"}})
            rows = controls()

            class Backend:
                def __init__(self):
                    self.source_ops = sourceops
                    self.store = types.SimpleNamespace(read=lambda kind: json.loads((scope / {"state": "state.json", "transport": "transport-state.json"}[kind]).read_bytes()))

                def observe_control(self, name):
                    self_outer.assertTrue(locked[0])
                    self_outer.assertEqual((lib / "target_rpc.py").read_bytes(), newrpc)
                    events.append(name)
                    if fail_observer:
                        raise RuntimeError("wire-observer-unavailable")
                    return copy.deepcopy(rows[name.split(".")[0]])

            self_outer = self
            backend_module = types.SimpleNamespace(RunnerBackend=Backend)
            service = types.SimpleNamespace(B=backend_module, execution_lock=lock,
                                            verify_entry=lambda: self.assertTrue(locked[0]), selftest_armed=lambda owner: False)
            guard = types.SimpleNamespace(LocalBackend=lambda: types.SimpleNamespace(main_status=lambda: {"quiescent": True}, boot_id=lambda: "boot"))
            payload = {"label": A.LABEL, "owner": A.OWNER, "old_rpc_sha": sha(oldrpc), "new_rpc_sha": sha(newrpc),
                       "source_state_sha": sha(files["state.json"]), "target_ledger_sha": A.TARGET_LEDGER_SHA,
                       "source_programs": {"runner_guard.py": sha(b"guard")},
                       "rpc_program": __import__("base64").b64encode(newrpc).decode()}

            def write_new(path, raw, mode=0o600):
                with path.open("xb") as out:
                    out.write(raw)

            env = {"root": root, "lib": lib, "payload": payload, "need": A.need,
                   "sha": sha, "trusted": lambda path, mode: path.read_bytes(), "canonical": canonical,
                   "write_new": write_new, "sync": lambda path: None, "atomic": lambda path, raw, mode: path.write_bytes(raw),
                   "load": lambda name, path: service if path.name == "runner_service.py" else guard if path.name == "runner_guard.py" else R,
                   "time": time, "json": json, "base64": __import__("base64"),
                   "sys": types.SimpleNamespace(stdin=types.SimpleNamespace(buffer=io.BytesIO(commands)))}
            output = io.StringIO()
            error = None
            with contextlib.redirect_stdout(output):
                try:
                    exec(A.VALIDATION + A.SOURCE, env)
                except RuntimeError as caught:
                    error = str(caught)
            self.assertFalse(locked[0])
            self.assertEqual({name: (scope / name).read_bytes() for name in files}, files)
            self.assertEqual(json.loads((scope / "state.json").read_bytes())["quiet_instance_id"], "old-instance")
            self.assertEqual((scope / A.LABEL / "old-target_rpc.py").read_bytes(), oldrpc)
            return {"error": error, "outputs": [json.loads(row) for row in output.getvalue().splitlines()],
                    "events": events, "complete": (scope / A.LABEL / "complete.json").exists(),
                    "mirror_intent": (scope / A.LABEL / "install-mirror.intent.json").exists()}

    def test_source_holder_retains_lock_for_new_wire_observers_and_preserves_all_ledgers(self):
        result = self.exercise()
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["outputs"]), 3)
        self.assertTrue(result["outputs"][-1]["released"])
        self.assertEqual(result["events"], [name + ".status" for name in ("runtime", "ingress", "writers", "postgres", "redis", "background")])
        self.assertTrue(result["complete"])

    def test_source_missing_release_or_unknown_observer_retains_intent_without_completion(self):
        for options, error in [({"release": False}, "coordinator-lost-inspect-only"),
                               ({"fail_observer": True}, "wire-observer-unavailable")]:
            with self.subTest(options=options):
                result = self.exercise(**options)
                self.assertEqual(result["error"], error)
                self.assertFalse(result["complete"])
                self.assertTrue(result["mirror_intent"])


if __name__ == "__main__":
    unittest.main()
