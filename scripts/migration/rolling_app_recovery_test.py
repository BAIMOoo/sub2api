#!/usr/bin/env python3
import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("rolling_app_recovery", pathlib.Path(__file__).with_name("rolling_app_recovery.py"))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

OWNER = "30bda011defad8149621c2630a2bd89c"


def old_app():
    return {"container_id": M.OLD_ID, "image_id": M.IMAGE, "config_sha256": "1" * 64,
            "rootfs_diffids": ["sha256:" + "2" * 64], "environment_sha256": "3" * 64,
            "business_env_sha256": "4" * 64, "control_token_sha256": "5" * 64,
            "files": {f: str(i + 1) * 64 for i, f in enumerate(M.FILES)},
            "endpoints": copy.deepcopy(M.ENDPOINTS), "state": "running",
            "ops_failed": 1, "ops_dropped": 3, "tcp_scope": "all-accepted-including-idle",
            "work": {k: 0 for k in M.QUIET_FIELDS}, "exit_code": None, "oom_killed": False}


def new_app():
    app = old_app()
    app.update(container_id="a" * 64, config_sha256="b" * 64, environment_sha256="c" * 64,
               control_token_sha256="d" * 64, name=M.NEW_NAME, settings=copy.deepcopy(M.SETTINGS),
               mounts=copy.deepcopy(M.MOUNTS), restart="no", network="host", state="created",
               entrypoint=["/app/docker-entrypoint.sh"], command=["/app/sub2api"],
               control_token_file=str(M.TOKEN_FILE), ops_failed=0, ops_dropped=0,
               ready=False, serving=False, draining=False, all_writer_failures=0, all_writer_drops=0)
    return app


class MemoryStore:
    def __init__(self):
        self.value = None
        self.writes = []

    @contextlib.contextmanager
    def locked(self):
        yield

    def read(self):
        return copy.deepcopy(self.value)

    def write(self, value):
        self.value = copy.deepcopy(value)
        self.writes.append(copy.deepcopy(value))


class Backend:
    def __init__(self, store):
        self.store = store
        self.sample = 0
        self.calls = []
        self.fail_before = None
        self.fail_after = None
        self.exit_on_term = True
        self.history = None
        self.observed = {"window": None, "quiesce_cancelled": True, "pg_authority": "AWS",
                         "redis_authority": "AWS", "endpoints": copy.deepcopy(M.ENDPOINTS),
                         "boot_id": "boot-A", "old": old_app(), "new": None,
                         "nat": {"output": [["-A", "OUTPUT", "-j", "RETURN"]],
                                 "other": {"PREROUTING": [], "policy": "ACCEPT"}}}

    def observe(self):
        self.sample += 1
        return {**copy.deepcopy(self.observed), "sample_id": str(self.sample)}

    def inspect_history(self):
        return copy.deepcopy(self.history)

    def _mutate(self, kind, change):
        state = self.store.read()
        assert state["intent"]["kind"] == kind
        assert state["intent"]["status"] == "INTENT"
        self.calls.append(kind)
        if self.fail_before == kind:
            raise TimeoutError("secret must never appear in state or output")
        change()
        if self.fail_after == kind:
            raise TimeoutError("secret must never appear in state or output")

    def archive_history(self, spec):
        assert spec["historical_failed"] == 1 and spec["historical_dropped"] == 3
        def change():
            self.history = {"failed": 1, "dropped": 3, "logs_sha256": "e" * 64,
                            "recovery_receipt_sha256": "f" * 64,
                            "archive_directory": str(M.HISTORY_DIRECTORY), "durable": True}
        self._mutate("archive", change)

    def prepare_app(self, spec):
        assert spec["mounts"][1]["source"] == str(M.ENDPOINT_DIRECTORY)
        assert spec["files"] == self.observed["old"]["files"]
        assert spec["inherit_all_environment_except"] == list(M.ENV_DIFFERENCES)
        self._mutate("prepare", lambda: self.observed.update(new=new_app()))

    def start_app(self, container_id):
        assert container_id == "a" * 64
        self._mutate("start", lambda: self.observed["new"].update(state="running", ready=True, serving=True))

    def install_redirect(self, rule, nat_before):
        assert rule == M.REDIRECT and nat_before == self.observed["nat"]
        def change():
            self.observed["nat"]["output"].insert(0, copy.deepcopy(rule))
        self._mutate("route", change)

    def signal_old_once(self, container_id):
        assert container_id == M.OLD_ID
        def change():
            if self.exit_on_term:
                self.observed["old"].update(state="exited", exit_code=0)
        self._mutate("term", change)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.b = Backend(self.store)
        self.now = 100.0
        self.r = M.Recovery(self.b, self.store, OWNER, clock=lambda: self.now)

    def advance_to(self, phase):
        for _ in range(12):
            result = self.r.advance()
            if result["phase"] == phase:
                return result
        self.fail("did not reach " + phase)

    def complete(self):
        self.advance_to("DRAINING")
        self.r.advance()
        self.now += 1.0
        self.r.advance()
        self.now += 1.0
        return self.r.advance()

    def test_complete_preserves_history_and_produces_adoption(self):
        result = self.complete()
        self.assertEqual(result["phase"], "COMPLETE")
        receipt = result["adoption_receipt"]
        self.assertEqual((receipt["history"]["failed"], receipt["history"]["dropped"]), (1, 3))
        self.assertEqual((receipt["new_process_failed"], receipt["new_process_dropped"]), (0, 0))
        self.assertEqual(receipt["new_app"]["container_id"], "a" * 64)
        self.assertEqual(receipt["control_port"], 18093)
        self.assertEqual(receipt["endpoint_directory"], str(M.ENDPOINT_DIRECTORY))
        self.assertFalse(receipt["controller_config_changed"])
        self.assertEqual(self.b.calls, ["archive", "prepare", "start", "route", "term"])
        self.r.advance()
        self.assertEqual(self.b.calls.count("term"), 1)

    def test_prepare_and_start_do_not_change_old_or_route(self):
        old = copy.deepcopy(self.b.observed["old"])
        nat = copy.deepcopy(self.b.observed["nat"])
        self.advance_to("HEALTHY")
        self.assertEqual(self.b.observed["old"], old)
        self.assertEqual(self.b.observed["nat"], nat)
        self.assertEqual(self.b.calls, ["archive", "prepare", "start"])

    def test_every_work_category_including_idle_tcp_blocks_term(self):
        self.advance_to("DRAINING")
        for key in M.QUIET_FIELDS:
            with self.subTest(key=key):
                self.b.observed["old"]["work"][key] = 1
                for _ in range(4):
                    self.now += 10
                    self.assertEqual(self.r.advance()["phase"], "DRAINING")
                self.b.observed["old"]["work"][key] = 0
        self.assertNotIn("term", self.b.calls)

    def test_new_work_resets_stability_timer(self):
        self.advance_to("DRAINING")
        self.r.advance()
        self.now += 3
        self.b.observed["old"]["work"]["http"] = 1
        self.r.advance()
        self.b.observed["old"]["work"]["http"] = 0
        self.r.advance()
        self.now += 1
        self.r.advance()
        self.assertNotIn("term", self.b.calls)

    def test_repeated_sample_cannot_satisfy_stability(self):
        self.advance_to("DRAINING")
        real = self.b.observe
        sample = real()
        self.b.observe = lambda: copy.deepcopy(sample)
        for _ in range(5):
            self.now += 10
            self.r.advance()
        self.assertNotIn("term", self.b.calls)

    def test_boot_change_resets_stability(self):
        self.advance_to("DRAINING")
        self.r.advance()
        self.now += 5
        self.b.observed["boot_id"] = "boot-B"
        self.r.advance()
        self.assertEqual(self.store.value["quiet"]["samples"], 1)
        self.assertNotIn("term", self.b.calls)

    def test_unknown_mutations_reconcile_without_replay(self):
        for kind, before, after in [("archive", None, "ARCHIVED"), ("prepare", "ARCHIVED", "PREPARED"),
                                    ("start", "PREPARED", "HEALTHY"), ("route", "HEALTHY", "DRAINING")]:
            with self.subTest(kind=kind):
                self.setUp()
                if before:
                    self.advance_to(before)
                self.b.fail_after = kind
                result = self.r.advance()
                self.assertEqual(result["status"], "unknown_inspect_only")
                calls = list(self.b.calls)
                self.assertEqual(self.r.advance()["phase"], after)
                self.assertEqual(self.b.calls, calls)

    def test_unknown_before_effect_never_retries(self):
        self.b.fail_before = "archive"
        self.r.advance()
        for _ in range(5):
            result = self.r.advance()
            self.assertEqual(result["status"], "unknown_inspect_only")
        self.assertEqual(self.b.calls, ["archive"])
        self.assertNotIn("secret", json.dumps(self.store.value))

    def test_sigterm_once_waits_indefinitely_without_kill(self):
        self.b.exit_on_term = False
        result = self.complete()
        self.assertEqual(result["phase"], "TERM_INTENT")
        for _ in range(8):
            self.now += 100000
            self.r.advance()
        self.assertEqual(self.b.calls.count("term"), 1)
        self.b.observed["old"].update(state="exited", exit_code=0)
        self.assertEqual(self.r.advance()["phase"], "COMPLETE")
        self.assertEqual(self.b.calls.count("term"), 1)

    def test_unrelated_nat_change_blocks_drain_and_signal(self):
        self.advance_to("DRAINING")
        self.b.observed["nat"]["other"]["PREROUTING"].append(["-A", "PREROUTING", "-j", "ACCEPT"])
        with self.assertRaisesRegex(M.Refusal, "NAT_CHANGED"):
            self.r.advance()
        self.assertNotIn("term", self.b.calls)

    def test_redirect_must_be_first_and_unique(self):
        self.advance_to("DRAINING")
        original = copy.deepcopy(self.b.observed["nat"])
        for changed in [original["output"][1:] + original["output"][:1],
                        [M.REDIRECT] + original["output"]]:
            self.b.observed["nat"]["output"] = changed
            with self.assertRaisesRegex(M.Refusal, "NAT_CHANGED"):
                self.r.advance()

    def test_source_authority_window_and_cancellation_are_required(self):
        for field, value in [("pg_authority", "TENCENT"), ("redis_authority", "TENCENT"),
                             ("window", {"open": True}), ("quiesce_cancelled", False),
                             ("endpoints", {"v": 1, "postgres": "target", "redis": "source"})]:
            with self.subTest(field=field):
                self.setUp()
                self.b.observed[field] = value
                with self.assertRaises(M.Refusal):
                    self.r.advance()
                self.assertEqual(self.b.calls, [])

    def test_files_environment_data_and_endpoint_mounts_are_pinned(self):
        for tamper in [lambda app: app["files"].update({"config.yaml": "0" * 64}),
                       lambda app: app.update(business_env_sha256="0" * 64),
                       lambda app: app["mounts"][0].update(source=str(M.ENDPOINT_DIRECTORY / "data")),
                       lambda app: app["mounts"][1].update(source=str(M.SCOPE / "app")),
                       lambda app: app["settings"].update(MIGRATION_BACKGROUND_WORKERS="true"),
                       lambda app: app.update(control_token_sha256="5" * 64)]:
            with self.subTest(tamper=tamper):
                self.setUp()
                self.advance_to("PREPARED")
                tamper(self.b.observed["new"])
                with self.assertRaises(M.Refusal):
                    self.r.advance()
                self.assertNotIn("start", self.b.calls)

    def test_new_zero_counters_cannot_clear_history(self):
        self.advance_to("HEALTHY")
        self.b.observed["old"]["ops_failed"] = 0
        with self.assertRaisesRegex(M.Refusal, "HISTORICAL_COUNTERS_CHANGED"):
            self.r.advance()
        self.assertNotIn("route", self.b.calls)

    def test_history_changes_block_prepare(self):
        self.advance_to("ARCHIVED")
        self.b.history["logs_sha256"] = "0" * 64
        with self.assertRaisesRegex(M.Refusal, "HISTORY_ARCHIVE_CHANGED"):
            self.r.advance()
        self.assertNotIn("prepare", self.b.calls)

    def test_archive_loss_after_route_blocks_signal(self):
        self.advance_to("DRAINING")
        self.r.advance()
        self.b.history = None
        self.now += 10
        with self.assertRaisesRegex(M.Refusal, "HISTORY_RECEIPT_REQUIRED"):
            self.r.advance()
        self.assertNotIn("term", self.b.calls)

    def test_checkpoint_reply_loss_does_not_repeat_effect(self):
        real_write = self.store.write
        def write_then_lose_reply(value):
            real_write(value)
            if value["phase"] == "ARCHIVED":
                raise OSError("checkpoint acknowledgement lost")
        self.store.write = write_then_lose_reply
        self.r.advance()
        self.store.write = real_write
        self.assertEqual(self.r.advance()["phase"], "PREPARED")
        self.assertEqual(self.b.calls.count("archive"), 1)

    def test_old_oom_or_nonzero_exit_cannot_complete(self):
        self.b.exit_on_term = False
        self.complete()
        for exit_code, oom in [(1, False), (0, True)]:
            self.b.observed["old"].update(state="exited", exit_code=exit_code, oom_killed=oom)
            self.assertNotEqual(self.r.advance()["phase"], "COMPLETE")

    def test_default_cli_is_offline(self):
        out = io.StringIO()
        with mock.patch.object(M.Recovery, "advance", side_effect=AssertionError("must not execute")), contextlib.redirect_stdout(out):
            self.assertEqual(M.main([]), 0)
        self.assertTrue(json.loads(out.getvalue())["offline"])


class FileStoreTests(unittest.TestCase):
    def test_real_store_fsyncs_before_backend_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            store = M.FileStore(pathlib.Path(directory).resolve() / "state.json")
            backend = Backend(store)
            recovery = M.Recovery(backend, store, OWNER)
            real_sync = os.fsync
            kinds = []
            def sync(fd):
                kinds.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
                real_sync(fd)
            with mock.patch.object(M.os, "fsync", side_effect=sync):
                self.assertEqual(recovery.advance()["phase"], "ARCHIVED")
            self.assertGreaterEqual(kinds.count("file"), 3)
            self.assertGreaterEqual(kinds.count("directory"), 3)
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
            self.assertEqual(store.read()["history"]["failed"], 1)

    def test_store_refuses_insecure_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.json"
            store = M.FileStore(path)
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(M.Refusal):
                store.read()
            path.unlink()
            path.symlink_to(pathlib.Path(directory) / "elsewhere")
            with self.assertRaises(OSError):
                store.read()

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaisesRegex(M.Refusal, "DUPLICATE_KEY"):
            M._strict_json(b'{"owner":1,"owner":2}')


if __name__ == "__main__":
    unittest.main()
