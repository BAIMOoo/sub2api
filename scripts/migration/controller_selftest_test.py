"""Offline r2 driver and closure faults; no SSH or real service operations."""
import concurrent.futures
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


def load(name):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DRIVER, CLOSURE = load("controller_selftest"), load("close_controller_selftest")
OWNER = "ab" * 16
PIDS = {"app": 2260633, "postgres": 972839, "redis": 972827}


class Refusal(RuntimeError):
    pass


def need(condition, reason):
    if not condition:
        raise Refusal(reason)


class InlinePool:
    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def submit(self, function):
        future = concurrent.futures.Future()
        try:
            future.set_result(function())
        except Exception as error:
            future.set_exception(error)
        return future


class Fixture:
    def __init__(self, root):
        self.scope = pathlib.Path(root)
        self.clock, self.health_wait = 100.0, 8.0
        self.started, self.closed_live, self.polls = False, False, 0
        self.events, self.writes = [], []
        self.bridge, self.changed_pid = False, False
        self.initial = {"v": 1, "run": "20261016", "owner": OWNER, "phase": "ENTRY_PREPARATION",
                        "attempt": 0, "pause_seconds": 0.0, "window": None,
                        "pg": "AWS", "redis": "AWS", "route_fenced": False}
        self.state = copy.deepcopy(self.initial)
        self.closed = {**self.initial, "phase": "SOURCE_DATA", "attempt": 1000001,
                       "pause_seconds": 9.0, "deadline_breached": False,
                       "selftest_paused_at": 1792170090.0}
        self.resources = {kind: {"id": kind * 4, "running": True, "restart": "no"}
                          for kind in PIDS}
        self.guard = {"healthy": True, "pg_wal_protected": True, "recovering": False}
        self.target = {"pg_authority": "AWS", "redis_authority": "AWS", "pg_arm": None,
                       "pg_promotion_intent": None, "pg": {"in_recovery": True},
                       "redis": {"role": "slave"},
                       "persistent_endpoints": {"v": 1, "postgres": "source", "redis": "source"}}
        self.store = types.SimpleNamespace(read=self.read, save_state=self.save_state)
        source_ops = types.SimpleNamespace(_guard=lambda: copy.deepcopy(self.guard), _running=self.running)
        self.backend = types.SimpleNamespace(config={"owner": OWNER}, store=self.store,
                                             source_ops=source_ops, rpc=self.rpc,
                                             source=self.source, control=self.control)
        source = types.SimpleNamespace(strict_json=json.loads, private_bytes=lambda p: p.read_bytes(),
                                       canonical=lambda v: json.dumps(v, sort_keys=True).encode(),
                                       atomic_private=self.atomic, fsync_directory=lambda p: None)
        self.service = types.SimpleNamespace(
            B=types.SimpleNamespace(RunnerBackend=lambda: self.backend, SCOPE=self.scope),
            E=types.SimpleNamespace(need=need), S=source, SELFTEST_ATTEMPT=1000001,
            RULE=["fixed-rule"], execution_lock=contextlib.nullcontext)
        self.arm(False)
        self.write("state.json", self.initial)
        for name in ("transport-state.json", "source-attestations.json", "audit.jsonl", "target-rpc-ledger.json",
                     "selftest-initial-production-state.json", "controller-selftest-result.json"):
            (self.scope / name).write_bytes(("old:" + name).encode())
        old_archive = self.scope / "passed-selftest-20261017"
        old_archive.mkdir()
        (old_archive / "retained").write_bytes(b"old archive")

    def write(self, name, value):
        (self.scope / name).write_text(json.dumps(value, sort_keys=True))

    def arm(self, armed, owner=OWNER):
        self.write("controller-selftest.json", {"v": 1, "run": "20261016", "owner": owner, "armed": armed})

    def atomic(self, path, raw):
        self.writes.append(path.name)
        path.write_bytes(raw)

    def read(self, kind):
        assert kind == "state"
        if self.started:
            self.polls += 1
            if self.polls == 1:
                self.state = {**self.initial, "attempt": 1000001,
                              "window": {"kind": "combined", "irreversible": False}}
            else:
                self.state = copy.deepcopy(self.closed)
                self.closed_live = True
            self.write("state.json", self.state)
        return copy.deepcopy(self.state)

    def save_state(self, value):
        self.events.append("restore-initial")
        self.state = copy.deepcopy(value)
        self.write("state.json", value)
        with (self.scope / "audit.jsonl").open("ab") as out:
            out.write(b"\ncheckpoint restore audit\n")

    def running(self, kind):
        pid = PIDS[kind] + (1 if self.changed_pid and self.closed_live and kind == "app" else 0)
        return {"State": {"Pid": pid}}

    def rpc(self, op_id, action):
        assert op_id is None and action == "status"
        return copy.deepcopy(self.target)

    def source(self, op_id, action):
        assert op_id is None and action == "inspect"
        return {"resources": copy.deepcopy(self.resources)}

    def control(self, op_id, control):
        assert op_id is None
        if control == "runtime.status":
            return {"ready": True, "background_workers_enabled": False}
        if control == "ingress.status":
            return {"paused": self.started and not self.closed_live}
        return {"writers.status": {"held": False},
                "postgres.status": {"paused": False, "endpoint": "source"},
                "redis.status": {"paused": False, "address": "127.0.0.1:16379", "pubsub_cache_ready": True}}[control]

    def command(self, argv, **kwargs):
        self.events.append(tuple(argv))
        if argv[0] == "/usr/sbin/iptables":
            return types.SimpleNamespace(returncode=0 if self.bridge else 1)
        if "start" in argv:
            self.started = True
            return types.SimpleNamespace(returncode=0)
        raise AssertionError("unexpected mutation " + repr(argv))

    def output(self, argv, **kwargs):
        assert "show" in argv
        if self.started and not self.closed_live:
            values = {"MainPID": "4321", "ActiveState": "active", "SubState": "running", "Result": "success", "Job": ""}
        else:
            values = {"MainPID": "0", "ActiveState": "inactive", "SubState": "dead", "Result": "signal", "Job": ""}
        requested = next(arg for arg in argv if arg.startswith("--property=")).split("=", 1)[1].split(",")
        return ("\n".join(key + "=" + values[key] for key in requested) + "\n").encode()

    def connection(self, host, port, **kwargs):
        assert (host, port) == ("127.0.0.1", 18081)
        fixture = self
        class Connection:
            def request(self, method, path, **kwargs):
                assert (method, path) == ("GET", "/health")
                fixture.events.append("health-queued")

            def getresponse(self):
                fixture.clock += fixture.health_wait
                return types.SimpleNamespace(status=200, read=lambda limit: b'{"status":"ok"}')

            def close(self):
                pass
        return Connection()

    def sleep(self, seconds):
        self.clock += seconds

    def execute(self, script):
        loader = types.SimpleNamespace(exec_module=lambda module: None)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict("sys.modules", {"runner_service": self.service}))
            stack.enter_context(mock.patch("importlib.util.spec_from_file_location", return_value=types.SimpleNamespace(loader=loader)))
            stack.enter_context(mock.patch("importlib.util.module_from_spec", return_value=self.service))
            stack.enter_context(mock.patch("subprocess.run", side_effect=self.command))
            stack.enter_context(mock.patch("subprocess.check_output", side_effect=self.output))
            stack.enter_context(mock.patch("http.client.HTTPConnection", side_effect=self.connection))
            stack.enter_context(mock.patch("concurrent.futures.ThreadPoolExecutor", InlinePool))
            stack.enter_context(mock.patch("time.monotonic", side_effect=lambda: self.clock))
            stack.enter_context(mock.patch("time.time", side_effect=lambda: 1792170000.0 + self.clock))
            stack.enter_context(mock.patch("time.sleep", side_effect=self.sleep))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            exec(compile(script, "selftest-remote", "exec"), {})
        return json.loads(output.getvalue())


class ControllerSelftestR2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)

    def test_both_default_commands_are_offline(self):
        for module in (DRIVER, CLOSURE):
            with self.subTest(module=module.__name__), mock.patch.object(module.subprocess, "run") as run, \
                 mock.patch.object(module.runpy, "run_path") as channel, mock.patch.object(module.os, "open") as opened, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                module.main([])
                self.assertTrue(json.loads(output.getvalue())["offline"])
                run.assert_not_called(); channel.assert_not_called(); opened.assert_not_called()

    def test_existing_local_r2_evidence_refuses_before_ssh(self):
        for module, name in ((DRIVER, "controller-selftest-result-r2.json"),
                             (CLOSURE, "selftest-closure-20261017-r2.json")):
            folder = self.f.scope / ".codex/backups/sub2api-source-controller-20261016"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / name
            path.write_bytes(b"retained attempt")
            with self.subTest(module=module.__name__), mock.patch.object(pathlib.Path, "home", return_value=self.f.scope), \
                 mock.patch.object(module.subprocess, "run") as run, mock.patch.object(module.runpy, "run_path") as channel:
                with self.assertRaises(FileExistsError):
                    module.main(["--apply"])
                run.assert_not_called(); channel.assert_not_called()
                self.assertEqual(path.read_bytes(), b"retained attempt")

    def test_r2_driver_proves_pause_recovery_and_retains_old_evidence(self):
        old_names = ("selftest-initial-production-state.json", "controller-selftest-result.json",
                     "transport-state.json", "source-attestations.json", "target-rpc-ledger.json", "audit.jsonl")
        old = {name: (self.f.scope / name).read_bytes() for name in old_names}
        report = self.f.execute(DRIVER.REMOTE)
        self.assertTrue(report["ok"])
        self.assertEqual(report["selftest_attempt"], 1000001)
        self.assertEqual(report["main_started_pid"], 4321)
        self.assertEqual(report["main_unit"]["MainPID"], "0")
        self.assertEqual(report["source_pids"], PIDS)
        self.assertFalse(report["checkpoint_reset"])
        for name, raw in old.items():
            self.assertEqual((self.f.scope / name).read_bytes(), raw)
        self.assertEqual(json.loads((self.f.scope / "selftest-initial-production-state-r2.json").read_bytes()), self.f.initial)
        self.assertTrue(json.loads((self.f.scope / "controller-selftest.json").read_bytes())["armed"])
        self.assertEqual((self.f.scope / "passed-selftest-20261017/retained").read_bytes(), b"old archive")

    def test_r2_requires_matching_installed_attempt_and_disarmed_owner(self):
        for scenario in ("old_attempt", "wrong_owner", "already_armed", "changed_initial", "bridge_present"):
            with self.subTest(scenario=scenario):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = Fixture(directory)
                    if scenario == "old_attempt":fixture.service.SELFTEST_ATTEMPT = 1000000
                    elif scenario == "wrong_owner":fixture.arm(False, "cd" * 16)
                    elif scenario == "already_armed":fixture.arm(True)
                    elif scenario == "changed_initial":fixture.state["stale_selftest_field"] = True
                    else:fixture.bridge = True
                    with self.assertRaises(Refusal):fixture.execute(DRIVER.REMOTE)
                    self.assertEqual(fixture.writes, [])
                    self.assertFalse(fixture.started)

    def test_r2_existing_remote_evidence_is_never_replaced(self):
        for name in ("selftest-initial-production-state-r2.json", "controller-selftest-result-r2.json"):
            with self.subTest(name=name):
                path = self.f.scope / name
                path.write_bytes(b"retained r2")
                with self.assertRaisesRegex(Refusal, "SELFTEST_R2_ALREADY_ATTEMPTED"):
                    self.f.execute(DRIVER.REMOTE)
                self.assertEqual(path.read_bytes(), b"retained r2")
                self.assertFalse(self.f.started)
                path.unlink()

    def test_r2_rejects_missing_quiet_proof_overruns_and_source_pid_change(self):
        for scenario in ("missing_quiet", "boolean_quiet", "pause_over_9_5", "deadline_breached", "health_over_10", "source_pid"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                if scenario == "missing_quiet":fixture.closed.pop("selftest_paused_at")
                elif scenario == "boolean_quiet":fixture.closed["selftest_paused_at"] = True
                elif scenario == "pause_over_9_5":fixture.closed["pause_seconds"] = 9.5001
                elif scenario == "deadline_breached":fixture.closed["deadline_breached"] = True
                elif scenario == "health_over_10":fixture.health_wait = 10.0001
                else:fixture.changed_pid = True
                with self.assertRaises(Refusal):fixture.execute(DRIVER.REMOTE)
                self.assertFalse(json.loads((fixture.scope / "controller-selftest-result-r2.json").read_bytes())["ok"])
                self.assertEqual(fixture.state["attempt"], 1000001)
                self.assertNotIn("restore-initial", fixture.events)

    def test_r2_accepts_9_5_pause_and_10_health_boundaries(self):
        self.f.closed["pause_seconds"], self.f.health_wait = 9.5, 10.0
        self.assertTrue(self.f.execute(DRIVER.REMOTE)["ok"])

    def test_closure_archives_six_r2_files_before_disarming_and_preserves_ledgers(self):
        report = self.f.execute(DRIVER.REMOTE)
        old_transport = (self.f.scope / "transport-state.json").read_bytes()
        old_rpc = (self.f.scope / "target-rpc-ledger.json").read_bytes()
        old_audit = (self.f.scope / "audit.jsonl").read_bytes()
        result = self.f.execute(CLOSURE.REMOTE)
        self.assertTrue(result["ok"])
        self.assertEqual(self.f.state, self.f.initial)
        self.assertFalse(json.loads((self.f.scope / "controller-selftest.json").read_bytes())["armed"])
        archive = self.f.scope / "passed-selftest-20261017-r2"
        expected = {"state.json", "controller-selftest.json", "controller-selftest-result-r2.json",
                    "selftest-initial-production-state-r2.json", "transport-state.json", "source-attestations.json"}
        self.assertEqual({path.name for path in archive.iterdir()}, expected | {"closure-verification.json"})
        self.assertEqual(json.loads((archive / "state.json").read_bytes()), report["state"])
        self.assertTrue(json.loads((archive / "controller-selftest.json").read_bytes())["armed"])
        self.assertEqual((self.f.scope / "transport-state.json").read_bytes(), old_transport)
        self.assertEqual((self.f.scope / "target-rpc-ledger.json").read_bytes(), old_rpc)
        self.assertTrue((self.f.scope / "audit.jsonl").read_bytes().startswith(old_audit))
        self.assertEqual((self.f.scope / "passed-selftest-20261017/retained").read_bytes(), b"old archive")
        disarm = [i for i, name in enumerate(self.f.writes) if name == "controller-selftest.json"][-1]
        self.assertEqual(self.f.writes[disarm - 6:disarm],
                         ["state.json", "controller-selftest.json", "controller-selftest-result-r2.json",
                          "selftest-initial-production-state-r2.json", "transport-state.json", "source-attestations.json"])

    def test_closure_refuses_stale_round_state_foreign_arm_and_existing_archive(self):
        for scenario in ("old_round", "different_state", "old_attempt", "arm_false", "pause_over_9_5", "existing_archive", "source_pid"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                fixture.execute(DRIVER.REMOTE)
                path = fixture.scope / "controller-selftest-result-r2.json"
                report = json.loads(path.read_bytes())
                if scenario == "old_round":report["selftest_attempt"] = 1000000
                elif scenario == "different_state":report["state"]["pause_seconds"] = 8.5
                elif scenario == "old_attempt":fixture.service.SELFTEST_ATTEMPT = 1000000
                elif scenario == "arm_false":fixture.arm(False)
                elif scenario == "pause_over_9_5":
                    fixture.closed["pause_seconds"] = report["state"]["pause_seconds"] = 9.5001
                elif scenario == "existing_archive":(fixture.scope / "passed-selftest-20261017-r2").mkdir()
                else:fixture.changed_pid = True
                path.write_text(json.dumps(report))
                fixture.writes.clear()
                with self.assertRaises(Refusal):fixture.execute(CLOSURE.REMOTE)
                self.assertEqual(fixture.writes, [])
                self.assertNotIn("restore-initial", fixture.events)


if __name__ == "__main__":
    unittest.main()
