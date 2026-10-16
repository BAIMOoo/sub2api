"""Offline installer ownership, immutable retry and pre-import boundary tests."""
import copy
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("install_source_runner", pathlib.Path(__file__).with_name("install_source_runner.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
SEED_OWNER = "67d9c2787b856797ffc819747b056420"
DATA_PATH = "/var/lib/docker/volumes/owned-postgres/_data"


def seed():
    return {"run": "sub2api-pg-replica-20261016", "side": "source", "owner": SEED_OWNER,
            "container_id": M.EXPECTED["postgres"], "system_identifier": "7626048206782038050", "timeline": 1,
            "slot_create_intended": True, "slot_created": True, "data_host_path": DATA_PATH,
            "phase": "source-prepared"}


class SeedTests(unittest.TestCase):
    def test_default_program_is_offline(self):
        result = json.loads(subprocess.check_output([sys.executable, M.__file__]))
        self.assertTrue(result["offline"])
        self.assertFalse(result["production_actions"])

    def test_original_owner_is_preserved_independently_of_new_runner_owner(self):
        value = seed()
        raw = M.canonical(value)
        result = M.verify_replica_seed(raw, M.sha(raw), DATA_PATH)
        self.assertEqual(result["owner"], SEED_OWNER)
        self.assertEqual("migration-owner:"+result["owner"], "migration-owner:"+SEED_OWNER)

    def test_raw_hash_includes_final_newline(self):
        raw = M.canonical(seed())
        self.assertTrue(raw.endswith(b"\n"))
        with self.assertRaisesRegex(RuntimeError, "REPLICA_STATE_CHANGED"):
            M.verify_replica_seed(raw.rstrip(), M.sha(raw), DATA_PATH)

    def test_modified_bytes_refuse_before_using_seed_identity(self):
        raw = M.canonical(seed())
        with self.assertRaisesRegex(RuntimeError, "REPLICA_STATE_CHANGED"):
            M.verify_replica_seed(raw.replace(b"source-prepared", b"forged-prepared"), M.sha(raw), DATA_PATH)

    def test_wrong_scope_cluster_container_or_slot_intent_refuses(self):
        cases = [("run", "other"), ("side", "target"), ("owner", "sub2api-but-foreign"),
                 ("container_id", "1"*64), ("system_identifier", "999"), ("timeline", 2),
                 ("slot_create_intended", False), ("data_host_path", "/foreign")]
        for key, value in cases:
            with self.subTest(key=key):
                candidate = seed()
                candidate[key] = value
                raw = M.canonical(candidate)
                with self.assertRaisesRegex(RuntimeError, "REPLICA_STATE_NOT_OWNED"):
                    M.verify_replica_seed(raw, M.sha(raw), DATA_PATH)

    def test_missing_ownership_proof_never_defaults_to_owned(self):
        for key in ("owner", "slot_create_intended", "container_id", "data_host_path"):
            with self.subTest(key=key):
                candidate = seed()
                candidate.pop(key)
                raw = M.canonical(candidate)
                with self.assertRaises(RuntimeError):
                    M.verify_replica_seed(raw, M.sha(raw), DATA_PATH)


class ImmutableInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = pathlib.Path(directory.name) / "owned.json"

    @staticmethod
    def uid(info, uid=0):
        fields = list(info)
        fields[4] = uid
        return os.stat_result(fields)

    def root_stat(self):
        original = pathlib.Path.stat
        path = self.path
        def sample(item, *args, **kwargs):
            result = original(item, *args, **kwargs)
            return self.uid(result) if item == path else result
        return mock.patch.object(pathlib.Path, "stat", autospec=True, side_effect=sample)

    def test_identical_inert_install_is_repeatable_without_rewriting(self):
        M.immutable_write(self.path, b"same\n")
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        with self.root_stat():
            M.immutable_write(self.path, b"same\n")
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_changed_or_partial_existing_install_is_preserved_for_review(self):
        self.path.write_bytes(b"partial")
        self.path.chmod(0o600)
        with self.root_stat(), self.assertRaisesRegex(RuntimeError, "EXISTING_INSTALL_DIFFERS"):
            M.immutable_write(self.path, b"complete\n")
        self.assertEqual(self.path.read_bytes(), b"partial")

    def test_wrong_mode_or_hardlink_is_never_adopted(self):
        self.path.write_bytes(b"same")
        self.path.chmod(0o644)
        with self.root_stat(), self.assertRaises(RuntimeError):
            M.immutable_write(self.path, b"same")
        self.path.chmod(0o600)
        os.link(self.path, self.path.parent / "second-link")
        with self.root_stat(), self.assertRaises(RuntimeError):
            M.immutable_write(self.path, b"same")

    def test_symlink_is_never_followed_or_overwritten(self):
        target = self.path.parent / "other"
        target.write_bytes(b"other")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "SYMLINK_REFUSED"):
            M.immutable_write(self.path, b"new")
        self.assertEqual(target.read_bytes(), b"other")


class PreflightTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.libexec = pathlib.Path(directory.name)
        programs = {}
        for name in M.PROGRAMS:
            path = self.libexec / name
            raw = ("# offline fixture "+name+"\n").encode()
            path.write_bytes(raw)
            path.chmod(0o644)
            programs[name] = M.sha(raw)
        self.request = {"v": 1, "run": "20261016", "owner": "ab"*16, "initial_target_epoch": 0,
                        "known_hosts_sha256": "b"*64, "source_replica_state_sha256": M.sha(M.canonical(seed())),
                        "programs": programs}
        original = pathlib.Path.lstat
        libexec = self.libexec
        def root_info(item):
            result = original(item)
            return ImmutableInstallTests.uid(result) if item.parent == libexec else result
        patches = [mock.patch.object(M, "LIBEXEC", self.libexec), mock.patch.object(M.os, "geteuid", return_value=0),
                   mock.patch.object(M.sys, "platform", "linux"), mock.patch.object(pathlib.Path, "lstat", autospec=True, side_effect=root_info)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.run = mock.patch.object(M, "run", side_effect=AssertionError("no production command during rejected preflight"))
        self.run_mock = self.run.start()
        self.addCleanup(self.run.stop)
        self.load = mock.patch.object(M, "load", side_effect=AssertionError("must verify program before import"))
        self.load_mock = self.load.start()
        self.addCleanup(self.load.stop)

    def rejected(self, request=None):
        with self.assertRaises(RuntimeError):
            M.install(self.request if request is None else request)
        self.run_mock.assert_not_called()
        self.load_mock.assert_not_called()

    def test_unknown_request_fields_are_rejected_before_import(self):
        self.rejected({**self.request, "command": "arbitrary"})

    def test_missing_reviewed_program_is_rejected(self):
        request = copy.deepcopy(self.request)
        request["programs"].pop("runner_guard.py")
        self.rejected(request)

    def test_changed_root_program_bytes_are_rejected_before_import(self):
        (self.libexec / "runner_guard.py").write_bytes(b"changed")
        self.rejected()

    def test_group_or_user_editable_program_is_rejected_before_import(self):
        (self.libexec / "runner_guard.py").chmod(0o664)
        self.rejected()

    def test_program_symlink_is_rejected_before_import(self):
        path = self.libexec / "runner_guard.py"
        path.unlink()
        path.symlink_to(self.libexec / "durable_runner.py")
        self.rejected()

    def test_nonroot_or_negative_epoch_is_rejected_before_import(self):
        with mock.patch.object(M.os, "geteuid", return_value=1000):
            self.rejected()
        self.rejected({**self.request, "initial_target_epoch": -1})

    def test_missing_original_state_attestation_is_rejected_before_import(self):
        request = copy.deepcopy(self.request)
        request.pop("source_replica_state_sha256")
        self.rejected(request)


if __name__ == "__main__":
    unittest.main()
