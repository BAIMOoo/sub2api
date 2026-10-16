"""Offline checks for the one-attempt, source-only guard recovery."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("recover_source_guard", HERE / "recover_source_guard.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def need(value, code):
    if not value:
        raise RuntimeError(code)


class TargetReadTests(unittest.TestCase):
    def run_target(self, change=None):
        cfg = {"pg": {"container_id": "pinned-pg", "source_timeline": 1}}
        ledger = {"epoch": 2, "pg": {"authority": "AWS"}, "redis": {"authority": "AWS"},
                  "operations": {"old-rehearsal": {"phase": "COMPLETE"}}}
        pg = {"system_identifier": "7626048206782038050", "in_recovery": True,
              "source_timeline": 1, "wal_timeline": None}
        receiver = {"slot": "sub2api_mig_20261016", "receivers": [
            {"status": "streaming", "timeline": 1, "slot": "sub2api_mig_20261016"}]}
        controls = {
            "runtime": {"ready": True, "serving": True, "draining": False, "background_workers_enabled": False},
            "postgres": {"endpoint": "source", "paused": False},
            "redis": {"address": "127.0.0.1:16379", "paused": False, "pubsub_cache_ready": True},
            "background": {"activated": False, "activating": False, "started": [], "failures": []},
        }
        pids = dict(R.TARGET_PIDS)
        if change:
            change(ledger, pg, receiver, controls, pids)
        calls = []

        class Backend:
            def __init__(self, _cfg): pass
            def resource(self, key): return {"State": {"Pid": pids[key]}}
            def endpoint_state(self): return {"v": 1, "postgres": "source", "redis": "source"}
            def pg_sample(self): return pg
            def redis_inspect(self): return {"role": "slave"}
            def command(self, argv, query):
                calls.append((argv, query))
                self_test = query.decode()
                need("SELECT json_build_object" in self_test and "ALTER " not in self_test, "target-query-mutates")
                return json.dumps(receiver).encode()
            def app_control(self, control):
                need(control.endswith(".status"), "target-control-mutates")
                calls.append(control)
                return {"http_status": 200, "body": controls[control.split(".")[0]]}

        M = types.SimpleNamespace(load_config=lambda: cfg, LocalBackend=Backend,
            FileStore=lambda _cfg: types.SimpleNamespace(read=lambda: ledger),
            canonical=lambda x: json.dumps(x, sort_keys=True).encode(), strict_json=lambda raw, limit: json.loads(raw))
        payload = {"new": {"target_rpc.py": "new-rpc"}, "target_config_sha": "config", "target_pids": R.TARGET_PIDS}
        env = {"payload": payload, "lib": pathlib.Path("/libexec"), "root": pathlib.Path("/state"),
               "trusted": lambda p, mode: b"new-rpc" if p.name == "target_rpc.py" else b"config",
               "sha": lambda raw: raw.decode() if raw in (b"new-rpc", b"config") else hashlib.sha256(raw).hexdigest(),
               "load": lambda name, path: M, "need": need, "json": json,
               "time": types.SimpleNamespace(time=lambda: 100),
               "command": lambda argv: self.entry_status(argv)}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            exec(compile(R.TARGET_READ, "target-read-test", "exec"), env)
        return json.loads(out.getvalue()), calls

    def entry_status(self, argv):
        self.assertEqual(argv[1:3], ["show", "sub2api-tencent-entry.service"])
        return b"MainPID=259990\nActiveState=active\nJob=\n"

    def test_standby_with_null_wal_timeline_uses_receiver_source_timeline(self):
        result, calls = self.run_target()
        self.assertTrue(result["ok"])
        self.assertEqual(result["timeline"], 1)
        self.assertEqual(len(calls), 5)
        self.assertIn("25432", calls[0][0])

    def test_target_rejects_promotion_pending_operation_pid_receiver_and_background_changes(self):
        mutations = [
            lambda l, p, r, c, i: p.update(in_recovery=False),
            lambda l, p, r, c, i: l["operations"]["old-rehearsal"].update(phase="INTENT"),
            lambda l, p, r, c, i: i.update(pg=999),
            lambda l, p, r, c, i: r["receivers"][0].update(status="stopped"),
            lambda l, p, r, c, i: c["background"].update(activated=True),
            lambda l, p, r, c, i: l["pg"].update(authority="TENCENT"),
        ]
        for index, change in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                self.run_target(change)


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.facts = {
            "system_identifier": "7626048206782038050", "timeline": 1,
            "role": {"login": False, "name": "sub2api_mig_20261016_replica"},
            "slots": [{"name": "sub2api_mig_20261016", "type": "physical", "temporary": False,
                "active": True, "pid": 41, "wal_status": "reserved", "restart_lsn": "0/123",
                "retained_bytes": 0}],
            "senders": [{"pid": 41, "user": "sub2api_mig_20261016_replica",
                "application": "sub2api-pg-replica-20261016", "address": "172.18.0.1", "state": "streaming"}],
            "orphan_guard_sql": 0, "wal_cap_bytes": 512 * 1024**2,
        }
        tree = ast.parse(R.SOURCE_RECOVERY)
        funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("snapshot", "stable_key")]
        S = types.SimpleNamespace(strict_json=json.loads, match=lambda v, rx: isinstance(v, str) and re.fullmatch(rx, v), LSN=r"[0-9A-F]+/[0-9A-F]+")
        G = types.SimpleNamespace(RETAIN_LIMIT=384 * 1024**2, WAL_CAP=512 * 1024**2, FREE_MIN=2 * 1024**3,
                                 ROLE="sub2api_mig_20261016_replica", APPLICATION="sub2api-pg-replica-20261016", ADDRESS="172.18.0.1")
        self.top = b"PID COMMAND\n123 postgres\n"
        self.env = {"need": need, "stopped_guard": lambda: None, "resources": lambda: None,
            "fixed_files": lambda: None, "command": lambda argv: self.top,
            "source": types.SimpleNamespace(m={"containers": {"postgres": {"id": "owned-pg"}}}),
            "S": S, "G": G, "QUERY": b"fixed-query", "sql": lambda q: json.dumps(self.facts).encode(),
            "backend": types.SimpleNamespace(_cluster=lambda f: None, _role=lambda f: f["role"],
                _slots=lambda f: f["slots"], config={"pg_data_host_path": "/owned"}),
            "os": types.SimpleNamespace(statvfs=lambda p: types.SimpleNamespace(f_bavail=4 * 1024**3, f_frsize=1))}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "source-snapshot-test", "exec"), self.env)

    def test_exact_active_owned_slot_sender_are_accepted(self):
        self.assertEqual(self.env["snapshot"](False), self.facts)

    def test_missing_inactive_lost_foreign_sender_and_orphan_sql_are_refused(self):
        original = copy.deepcopy(self.facts)
        changes = [
            lambda f: f.update(slots=[]),
            lambda f: f["slots"][0].update(active=False),
            lambda f: f["slots"][0].update(wal_status="lost"),
            lambda f: f["senders"][0].update(address="unowned"),
            lambda f: f.update(orphan_guard_sql=1),
            lambda f: f["role"].update(login=True),
            lambda f: f["slots"][0].update(retained_bytes=384 * 1024**2),
            lambda f: f.update(wal_cap_bytes=-1),
        ]
        for index, change in enumerate(changes):
            self.facts = copy.deepcopy(original); change(self.facts)
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                self.env["snapshot"](False)

    def test_container_side_psql_must_end_even_when_host_guard_is_empty(self):
        self.top = b"PID COMMAND\n123 postgres\n124 psql\n"
        with self.assertRaisesRegex(RuntimeError, "lingering-container-psql"):
            self.env["snapshot"](False)

    def test_stability_allows_wal_progress_but_detects_sender_replacement(self):
        later = copy.deepcopy(self.facts)
        later["slots"][0].update(restart_lsn="0/456", retained_bytes=40)
        self.assertEqual(self.env["stable_key"](later), self.env["stable_key"](self.facts))
        later["slots"][0]["pid"] = 42
        self.assertNotEqual(self.env["stable_key"](later), self.env["stable_key"](self.facts))

    def test_restore_transaction_only_mutates_exact_role_login(self):
        tree = ast.parse(R.SOURCE_RECOVERY)
        expr = next(n.value for n in tree.body if isinstance(n, ast.Try) for n in n.body
                    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "restore" for t in n.targets))
        sql = eval(compile(ast.Expression(expr), "restore-sql-test", "eval"),
                   {"pg": {"system_identifier": "7626048206782038050", "timeline": 1},
                    "pid": 41, "str": str, "payload": {"role_comment_sha": R.ROLE_COMMENT_SHA}})
        self.assertEqual(sql.count("ALTER ROLE sub2api_mig_20261016_replica LOGIN"), 1)
        self.assertIn("BEGIN; DO $recover$", sql)
        self.assertIn("COMMIT;", sql)
        self.assertIn("host(client_addr)='172.18.0.1'", sql)
        self.assertIn("AND active AND active_pid=41 AND wal_status='reserved'", sql)
        self.assertNotRegex(sql, r"pg_drop_replication_slot|pg_create_physical_replication_slot|pg_terminate_backend|PASSWORD|NOLOGIN")


class LocalOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name).resolve()
        self.parent = self.home / ".codex/backups/sub2api-source-controller-20261016"
        self.parent.mkdir(parents=True)
        # This installer is a frozen historical recovery. Orchestration tests
        # use its reviewed-input boundary, independent of later controller edits.
        patch = mock.patch.object(R, "reviewed_payload", return_value={"new": {"fixture.py": "a"*64}})
        patch.start()
        self.addCleanup(patch.stop)

    def test_default_is_offline_and_generated_workers_compile(self):
        with mock.patch.object(sys, "argv", ["recover_source_guard.py"]), mock.patch.object(R.subprocess, "run") as run:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                R.main()
            self.assertTrue(json.loads(output.getvalue())["offline"])
            run.assert_not_called()
        compile(R.COMMON + R.SOURCE_RECOVERY, "generated-source", "exec")
        compile(R.COMMON + R.TARGET_READ, "generated-target", "exec")
        self.assertIn("sys.dont_write_bytecode=True", R.COMMON)

    def run_apply(self, failure=None):
        target = {"ok": True, "checked_at": 1, "pids": R.TARGET_PIDS, "ledger_sha256": "same",
                  "system_identifier": "same", "timeline": 1, "receiver": {}, "config_sha256": "same"}
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            code = shlex.split(argv[-1])[-1]
            compile(code, "captured-remote", "exec")
            if failure and len(calls) == 2:
                raise subprocess.TimeoutExpired(argv, 55)
            value = {"ok": True} if len(calls) == 2 else target
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(value).encode(), stderr=b"")
        patches = (mock.patch.object(sys, "argv", ["recover_source_guard.py", "--apply"]),
                   mock.patch.object(R.pathlib.Path, "home", return_value=self.home),
                   mock.patch.object(R.subprocess, "run", side_effect=run))
        with patches[0], patches[1], patches[2], contextlib.redirect_stdout(io.StringIO()):
            if failure:
                with self.assertRaises(subprocess.TimeoutExpired): R.main()
            else:
                R.main()
            with self.assertRaises(FileExistsError): R.main()
        return calls, self.parent / "guard-trip-recovery-20261017"

    def test_target_read_source_recovery_target_read_order_and_exclusive_journal(self):
        calls, folder = self.run_apply()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][-2], "ubuntu@43.165.177.136")
        self.assertEqual(calls[1][-2], "sub2api")
        self.assertEqual(calls[2][-2], "ubuntu@43.165.177.136")
        self.assertTrue((folder / "complete.json").is_file())
        self.assertEqual((folder / "intent.json").stat().st_mode & 0o777, 0o600)
        for index in (0, 2):
            self.assertNotIn("systemctl','stop", calls[index][-1])
        target_calls = [node for node in ast.walk(ast.parse(R.TARGET_READ)) if isinstance(node, ast.Call)]
        self.assertFalse(any(isinstance(node.func, ast.Name) and node.func.id in ("atomic", "write_new") for node in target_calls))

    def test_uncertain_source_preserves_evidence_and_never_calls_postcheck_or_retries(self):
        calls, folder = self.run_apply(failure=True)
        self.assertEqual(len(calls), 2)
        self.assertTrue((folder / "source-recovery-uncertain.json").is_file())
        self.assertFalse((folder / "complete.json").exists())

    def test_existing_local_intent_cannot_be_overwritten(self):
        folder = self.parent / "manual-evidence"; folder.mkdir()
        R.journal_write(folder, "intent.json", {"first": True})
        with self.assertRaises(FileExistsError): R.journal_write(folder, "intent.json", {"first": False})
        self.assertEqual(json.loads((folder / "intent.json").read_bytes()), {"first": True})

    def test_source_recovery_order_archives_before_login_and_retires_trip_before_install(self):
        text = R.SOURCE_RECOVERY
        ordered = ["mark('stop-old-guard-intent')", "first=snapshot(False)", "second=snapshot(False)",
                   "write_new(saved/'guard-status.json',status)", "mark('restore-owned-login-intent')",
                   "sql(restore.encode())", "restored=snapshot(True)", "os.rename(scope/'guard-trip.json'",
                   "atomic(lib/name,data,0o644)", "command(['/usr/bin/systemctl','start',G.GUARD_UNIT]",
                   "started_at=time.time()"]
        positions = [text.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("systemctl','restart", text)
        self.assertNotIn("pg_drop_replication_slot(", text)


if __name__ == "__main__":
    unittest.main()
