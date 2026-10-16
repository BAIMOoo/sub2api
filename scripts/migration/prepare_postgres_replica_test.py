"""Offline safety checks only. No SSH, Docker, PostgreSQL or cloud mutations."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("replica", pathlib.Path(__file__).with_name("prepare_postgres_replica.py"))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
OWNER = "ab"*16
SECRET = "independent_replication_secret_"+"x"*24


def source_fixture(hba):
    return {"version_num": 180003, "admin": m.ADMIN, "admin_superuser": True,
        "in_recovery": False, "transaction_read_only": "off", "default_transaction_read_only": "off",
        "data_directory": m.DATA, "hba_file": m.DATA+"/pg_hba.conf", "config_file": m.DATA+"/postgresql.conf",
        "wal_level": "replica", "max_wal_senders": 10, "max_replication_slots": 10,
        "ssl": "off", "archive_mode": "off", "custom_tablespaces": [], "file_setting_errors": 0,
        "hba_errors": 0, "max_slot_wal_keep_pending_restart": False, "source_free_bytes": 6*m.GIB,
        "slots": [], "replicas": [], "role": None, "max_slot_wal_keep_size": "-1", "auto_wal_settings": [],
        "system_identifier": "123456789", "timeline": 1, "container_id": "source-container-id", "image_id": "sha256:exact",
        "image_identity": {"config_digest": m.REVIEWED_CONFIG_DIGEST, "rootfs_diff_ids": ["sha256:"+"1"*64], "architecture": "amd64", "os": "linux"},
        "data_host_path": "/unused-in-fake-source",
        "hba_sha256": hashlib.sha256(hba).hexdigest(), "pgdata_bytes": 100*m.MIB}


class FakeSource(m.Remote):
    def __init__(self, directory):
        super().__init__({"side": "source", "owner": OWNER, "action": "prepare", "verifier": m.scram_verifier(SECRET, b"known-test-salt!")})
        self.scope = pathlib.Path(directory)
        self.hba = self.scope/"live-hba"
        self.hba.write_bytes(b"local all all trust\nhost all all all scram-sha-256\n")
        self.facts = source_fixture(self.hba.read_bytes())
        self.persisted = {"run": m.RUN, "owner": OWNER}
        self.events = []

    def source_facts(self, **kwargs):
        return copy.deepcopy(self.facts)

    def state(self, **kwargs):
        return self.persisted

    def checkpoint(self, state, phase):
        self.events.append(phase)
        state["phase"] = phase

    def install_monitor(self, state):
        self.events.append("monitor-active")

    def docker_inspect(self, name, **kwargs):
        return {"Id": "source-container-id"}

    def map_source_file(self, container, filename):
        return self.hba

    def query(self, query, **kwargs):
        if "to_json(setting)" in query:
            return self.facts["max_slot_wal_keep_size"]
        if "pg_reload_conf" in query:
            self.events.append("reload")
            return True
        if "rolpassword=" in query:
            return self.facts["role"] is not None
        if "pg_hba_file_rules" in query:
            return 0
        if "json_agg(slot_name)" in query:
            return [s["name"] for s in self.facts["slots"]]
        raise AssertionError("unexpected fake SQL query")

    def sql(self, query, **kwargs):
        if "ALTER SYSTEM SET" in query:
            self.events.append("set-wal-cap")
            self.facts["max_slot_wal_keep_size"] = "512"
            self.facts["auto_wal_settings"] = [{"setting": "512MB", "applied": True}]
            return ""
        if "CREATE ROLE" in query:
            self.events.append("create-role")
            assert query.startswith("BEGIN;") and query.endswith("COMMIT;")
            assert SECRET not in query
            self.facts["role"] = {"comment": "migration-owner:"+OWNER, "replication": True,
                "superuser": False, "createdb": False, "createrole": False, "bypassrls": False, "login": True}
            return ""
        if "pg_create_physical_replication_slot" in query:
            self.events.append("create-slot")
            assert 0 < int(self.facts["max_slot_wal_keep_size"]) <= 512
            assert "monitor-active" in self.events
            self.facts["slots"] = [{"name": m.SLOT, "type": "physical"}]
            return ""
        raise AssertionError("unexpected fake SQL mutation")


class ReplicaSafetyTests(unittest.TestCase):
    def test_default_and_cleanup_are_offline_dry_runs(self):
        for args in [[], ["--cleanup"]]:
            with self.subTest(args=args), mock.patch.object(m, "Controller", side_effect=AssertionError("must not initialize SSH")):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(m.main(args), 0)
                plan = json.loads(out.getvalue())
                self.assertTrue(plan["dry_run"])
                self.assertFalse(plan["remote_commands_executed"])
                self.assertFalse(plan["promotes"])

    def test_remote_and_monitor_payload_prefix_produces_valid_python(self):
        source = pathlib.Path(m.__file__).read_text()
        for action in ["inspect", "monitor", "fence"]:
            payload = {"side": "source", "owner": OWNER, "action": action}
            compile("REMOTE_PAYLOAD = "+repr(payload)+"\n"+source, "<generated-remote-program>", "exec")

    def test_classic_config_and_containerd_manifest_have_equal_content_identity(self):
        layers = ["sha256:"+"1"*64, "sha256:"+"2"*64]
        config = json.dumps({"rootfs": {"diff_ids": layers}, "architecture": "amd64", "os": "linux"})
        config_digest = "sha256:"+hashlib.sha256(config.encode()).hexdigest()
        manifest = json.dumps({"config": {"digest": config_digest}})
        manifest_digest = "sha256:"+hashlib.sha256(manifest.encode()).hexdigest()
        image = {"Id": config_digest, "RootFS": {"Layers": layers}, "Architecture": "amd64", "Os": "linux"}
        remote = m.Remote({"side": "target", "action": "inspect"})
        def run(argv, **kwargs):
            self.assertEqual(argv[:5], ["ctr", "-n", "moby", "content", "get"])
            return types.SimpleNamespace(stdout={manifest_digest: manifest, config_digest: config}[argv[-1]])
        remote.run = run
        with mock.patch.object(m, "REVIEWED_CONFIG_DIGEST", config_digest):
            classic = remote.image_identity(image)
            image["Id"] = manifest_digest
            self.assertNotEqual(config_digest, manifest_digest)
            self.assertEqual(classic, remote.image_identity(image))
            image["RootFS"]["Layers"] = list(reversed(layers))
            with self.assertRaisesRegex(m.Refusal, "rootfs-mismatch"):
                remote.image_identity(image)

    def test_inspection_cannot_be_combined_with_mutation(self):
        for args in [["--inspect", "--apply"], ["--status", "--cleanup"]]:
            with self.subTest(args=args), mock.patch.object(m, "Controller", side_effect=AssertionError("must not connect")):
                with self.assertRaisesRegex(m.Refusal, "cannot-authorize-mutations"):
                    m.main(args)

    def test_only_finite_cap_and_independent_monitor_precede_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            source = FakeSource(directory)
            result = source.source_prepare()
            self.assertEqual(result["phase"], "source-prepared")
            self.assertLess(source.events.index("monitor-active"), source.events.index("set-wal-cap"))
            self.assertLess(source.events.index("set-wal-cap"), source.events.index("create-slot"))
            self.assertLess(source.events.index("replication-hba-ready"), source.events.index("create-slot"))
            self.assertEqual(source.persisted["old_wal_setting"], "-1")
            self.assertTrue(source.persisted["guc_changed"])
            self.assertEqual(source.hba.read_bytes().count(m.hba_block(OWNER)), 1)
            source.source_prepare()
            self.assertEqual(source.events.count("create-slot"), 1)
            self.assertEqual(source.events.count("create-role"), 1)
            self.assertEqual(source.events.count("set-wal-cap"), 1)

    def test_source_cap_reload_failure_cannot_create_role_or_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            source = FakeSource(directory)
            original_query = source.query
            source.query = lambda query, **kw: "-1" if "to_json(setting)" in query else original_query(query, **kw)
            with mock.patch.object(m.time, "monotonic", side_effect=[0, 0, 9]), mock.patch.object(m.time, "sleep"):
                with self.assertRaisesRegex(m.Refusal, "bounded-wal-setting-not-effective"):
                    source.source_prepare()
            self.assertNotIn("create-role", source.events)
            self.assertNotIn("create-slot", source.events)
            self.assertTrue(source.persisted["guc_changed"], "restore intent survives a failed reload")

    def test_existing_stricter_finite_cap_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = FakeSource(directory)
            source.facts["max_slot_wal_keep_size"] = "128"
            source.source_prepare()
            self.assertNotIn("set-wal-cap", source.events)
            self.assertFalse(source.persisted["guc_changed"])

    def test_unknown_guc_restore_result_can_be_safely_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            source = FakeSource(directory)
            original_hba = source.hba.read_bytes()
            source.source_prepare()
            m.save_json(source.scope/"state.json", source.persisted)
            source.hba.write_bytes(source.hba.read_bytes()+b"# another administrator's later edit\n")
            source.fence_source_objects = lambda **kwargs: source.facts.update(slots=[])
            source.remove_monitor = lambda: source.events.append("remove-monitor")
            original_sql = source.sql
            resets = []
            def sql(query, **kwargs):
                if query.startswith("DROP ROLE"):
                    source.facts["role"] = None
                elif query.startswith("ALTER SYSTEM RESET"):
                    resets.append(query)
                    source.facts["max_slot_wal_keep_size"] = "-1"
                    source.facts["auto_wal_settings"] = []
                    raise m.Refusal("simulated-lost-restore-result")
                else:
                    return original_sql(query, **kwargs)
                return ""
            source.sql = sql
            with self.assertRaisesRegex(m.Refusal, "lost-restore-result"):
                source.cleanup()
            self.assertTrue(source.persisted["guc_restore_intended"])
            self.assertFalse(source.persisted.get("guc_restored", False))
            self.assertTrue(source.cleanup()["cleaned"])
            self.assertEqual(len(resets), 1)
            self.assertEqual(source.hba.read_bytes(), original_hba+b"# another administrator's later edit\n")
            self.assertTrue(source.cleanup()["already_cleaned"])

    def test_refuses_source_version_other_slots_and_nonprimary_before_changes(self):
        mutations = [lambda f: f.update(version_num=180002), lambda f: f.update(in_recovery=True),
            lambda f: f.update(slots=[{"name": "someone_else", "type": "physical"}]),
            lambda f: f.update(custom_tablespaces=["external"]), lambda f: f.update(source_free_bytes=m.GIB)]
        for change in mutations:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                source = FakeSource(directory)
                change(source.facts)
                with self.assertRaises(m.Refusal):
                    source.source_prepare()
                self.assertEqual(source.events, [])

    def test_foreign_role_and_changed_hba_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            source = FakeSource(directory)
            source.facts["role"] = {"comment": "not-our-owner"}
            with self.assertRaisesRegex(m.Refusal, "owner-mismatch"):
                source.source_prepare()
            self.assertNotIn("monitor-active", source.events)

    def test_hba_preserves_other_edits_and_exact_cleanup_only(self):
        original = b"# another team's config\nhost all all all scram-sha-256\n"
        installed = m.add_hba(original, OWNER)
        external_edit = installed+b"# added later\n"
        self.assertEqual(m.remove_hba(external_edit, OWNER), original+b"# added later\n")
        self.assertEqual(m.add_hba(installed, OWNER), installed)
        with self.assertRaisesRegex(m.Refusal, "foreign-hba"):
            m.add_hba(installed, "cd"*16)
        with self.assertRaisesRegex(m.Refusal, "changed-owned-hba"):
            m.remove_hba(installed.replace(b"scram-sha-256", b"trust"), OWNER)

    def test_container_is_isolated_limited_and_never_promoted(self):
        command = m.container_command(OWNER, m.TARGET_SCOPE)
        self.assertEqual(command[command.index("--memory")+1], "768m")
        self.assertEqual(command[command.index("--network")+1], "host")
        self.assertNotIn("--publish", command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("promote", command[-1])
        self.assertIn("listen_addresses=127.0.0.1", command[-1])
        self.assertIn("default_transaction_read_only=on", command[-1])
        self.assertIn("pg_verifybackup --exit-on-error", command[-1])
        self.assertIn(".sub2api-seed-verified", command[-1])
        self.assertNotIn(SECRET, " ".join(command))
        subprocess.run(["sh", "-n"], input=command[-1], text=True, check=True, capture_output=True)

    def test_backup_streams_from_loopback_using_reserved_named_slot(self):
        command = m.backup_command()
        self.assertIn("--wal-method=stream", command)
        self.assertIn("--write-recovery-conf", command)
        self.assertIn("--slot="+m.SLOT, command)
        self.assertIn("--checkpoint=spread", command)
        self.assertNotIn("--create-slot", command)
        self.assertNotIn("--no-sync", command)
        self.assertNotIn("--target=server", " ".join(command))
        self.assertFalse(any("password=" in arg for arg in command))

    def test_scram_verifier_contains_no_plaintext_and_authenticates_known_password(self):
        verifier = m.scram_verifier(SECRET, b"fixed salt for fixture")
        self.assertNotIn(SECRET, verifier)
        scheme, rounds_salt, keys = verifier.split("$")
        self.assertEqual(scheme, "SCRAM-SHA-256")
        rounds, salt = rounds_salt.split(":")
        salted = hashlib.pbkdf2_hmac("sha256", SECRET.encode(), m.base64.b64decode(salt), int(rounds))
        client = m.hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        stored = m.base64.b64encode(hashlib.sha256(client).digest()).decode()
        self.assertEqual(keys.split(":")[0], stored)

    def test_private_state_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            m.atomic_private(root/"credential", SECRET)
            self.assertEqual(stat.S_IMODE((root/"credential").stat().st_mode), 0o600)
            link = root/"credential-link"
            link.symlink_to(root/"credential")
            with self.assertRaisesRegex(m.Refusal, "symlink"):
                m.atomic_private(link, "overwrite")
            self.assertEqual((root/"credential").read_text(), SECRET)

    def test_ssh_stderr_is_private_not_embedded_in_exception_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(m.Controller)
            controller.directory = pathlib.Path(directory).resolve()
            controller.program = "# isolated test"
            controller.commands = {"source": ["ssh", "fake"]}
            response = types.SimpleNamespace(returncode=1, stdout=json.dumps({"ok": False, "error": "remote-command-failed"}),
                stderr="connection password="+SECRET)
            out = io.StringIO()
            with mock.patch.object(m.subprocess, "run", return_value=response), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                with self.assertRaises(m.Refusal) as raised:
                    controller.call("source", "status", {"owner": OWNER})
            self.assertNotIn(SECRET, str(raised.exception)+out.getvalue())
            private = pathlib.Path(directory)/"source-private-errors.log"
            self.assertIn(SECRET, private.read_text())
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)

    def test_wal_guard_fences_only_owned_access_and_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = m.Remote({"side": "source", "owner": OWNER, "action": "monitor"})
            remote.scope = pathlib.Path(directory).resolve()
            remote.state = lambda **kwargs: {"system_identifier": "123", "data_host_path": directory}
            remote.source_status = lambda: {"sampled_at": m.time.time(), "source_free_bytes": 5*m.GIB, "pgwal_bytes": 400*m.MIB,
                "slots": [{"name": m.SLOT, "retained_bytes": m.WAL_TRIP_BYTES, "wal_status": "reserved"}],
                "wal_cap_mb": 512, "in_recovery": False, "safety_tripped": False}
            fences = []
            remote.fence_source_objects = lambda **kwargs: fences.append(kwargs)
            result = remote.monitor()
            self.assertFalse(result["monitor_ok"])
            self.assertEqual(fences, [{"drop_slot": True}])
            self.assertTrue((remote.scope/"safety-tripped.json").is_file())
            with self.assertRaisesRegex(m.Refusal, "explicit-cleanup-required"):
                remote.no_trip()

    def test_receiver_reconnect_is_not_mistaken_for_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = m.Remote({"side": "target", "owner": OWNER, "action": "monitor"})
            remote.scope = pathlib.Path(directory).resolve()
            remote.state = lambda **kwargs: {"system_identifier": "123"}
            remote.target_status = lambda: {"sampled_at": m.time.time(), "target_free_bytes": 5*m.GIB,
                "in_recovery": True, "transaction_read_only": "on", "default_transaction_read_only": "on",
                "listen_addresses": "127.0.0.1", "port": m.PORT, "primary_slot_name": m.SLOT,
                "system_identifier": "123", "version_num": 180003, "receiver_status": None, "standby_ready": False,
                "safety_tripped": False}
            remote.run = lambda *args, **kwargs: self.fail("reconnect must not stop the replica")
            self.assertTrue(remote.monitor()["monitor_ok"])

    def test_target_cleanup_before_it_was_prepared_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = m.Remote({"side": "target", "owner": OWNER, "action": "cleanup"})
            remote.scope = pathlib.Path(directory).resolve()
            remote.docker_inspect = lambda *args, **kwargs: None
            self.assertTrue(remote.cleanup()["not_prepared"])
            m.save_json(remote.scope/"state.json", {"owner": OWNER, "run": m.RUN, "cleaned": True})
            self.assertTrue(remote.cleanup()["already_cleaned"])

    def test_missing_container_cannot_delete_promoted_or_unverified_volume(self):
        for completed in [True, False]:
            with self.subTest(completed=completed), tempfile.TemporaryDirectory() as directory:
                scope = pathlib.Path(directory).resolve()
                data = scope/"owned-volume-data"
                data.mkdir(mode=0o700)
                (data/"PG_VERSION").write_text("18\n")
                if completed:
                    (data/".sub2api-seed-verified").touch()
                # There is deliberately no standby.signal (e.g. after promotion).
                m.save_json(scope/"state.json", {"run": m.RUN, "owner": OWNER, "system_identifier": "123"})
                remote = m.Remote({"side": "target", "owner": OWNER, "action": "cleanup"})
                remote.scope = scope
                remote.owned_container = lambda: None
                remote.owned_volume = lambda: {"Mountpoint": str(data)}
                remote.run = lambda *args, **kwargs: self.fail("must refuse before any Docker deletion")
                with self.assertRaisesRegex(m.Refusal, "refuse-cleanup"):
                    remote.cleanup()
                self.assertTrue((data/"PG_VERSION").is_file())

    def test_source_sampling_failure_uses_emergency_fence_and_persistent_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = m.Remote({"side": "source", "owner": OWNER, "action": "monitor"})
            remote.scope = pathlib.Path(directory).resolve()
            remote.state = lambda **kwargs: {"system_identifier": "123", "data_host_path": directory}
            remote.source_status = lambda: (_ for _ in ()).throw(m.Refusal("simulated-stats-query-timeout"))
            fences = []
            remote.fence_source_objects = lambda **kwargs: fences.append(kwargs)
            with mock.patch.object(m.shutil, "disk_usage", return_value=types.SimpleNamespace(free=5*m.GIB)):
                with self.assertRaisesRegex(m.Refusal, "monitor-sample-failed"):
                    remote.monitor()
            self.assertEqual(fences, [{"drop_slot": True}])
            self.assertEqual(m.read_json(remote.scope/"safety-tripped.json")["reason"], "source-monitor-sampling-failed")
            _, service, _ = m.monitor_units("source", str(remote.scope))
            self.assertIn("OnFailure="+m.UNIT_SOURCE+"-fence.service", service)
            self.assertIn("Restart=on-failure", m.failure_fence_unit(str(remote.scope)))

    def test_emergency_fence_uses_minimal_proof_and_scopes_every_effect(self):
        for foreign in [False, True]:
            with self.subTest(foreign=foreign):
                remote = m.Remote({"side": "source", "owner": OWNER, "action": "fence"})
                remote.state = lambda **kwargs: {"system_identifier": "123", "timeline": 1,
                    "container_id": "cid", "image_id": "local-image-id", "slot_create_intended": True}
                remote.docker_inspect = lambda *args, **kwargs: {"Id": "cid", "Image": "local-image-id"}
                remote.source_facts = lambda **kwargs: self.fail("emergency fence must not rerun the large facts query")
                effects = []
                proof = {"system_identifier": "123", "timeline": 1, "in_recovery": False,
                    "role": {"comment": "migration-owner:"+("different" if foreign else OWNER), "replication": True,
                        "superuser": False, "createdb": False, "createrole": False, "bypassrls": False},
                    "slots": [{"name": m.SLOT, "type": "physical"}]}
                def guard(query, *, as_json=False):
                    if query.startswith("SELECT json_build_object"):
                        return proof
                    if as_json:
                        return False
                    effects.append(query)
                    return ""
                remote.guard_sql = guard
                if foreign:
                    with self.assertRaisesRegex(m.Refusal, "owner-mismatch"):
                        remote.fence_source_objects(drop_slot=True)
                    self.assertEqual(effects, [])
                else:
                    remote.fence_source_objects(drop_slot=True)
                    self.assertEqual(effects[0], f"ALTER ROLE {m.ROLE} NOLOGIN;")
                    self.assertIn("usename='"+m.ROLE+"' AND backend_type='walsender'", effects[1])
                    self.assertEqual(effects[2], "SELECT pg_drop_replication_slot('"+m.SLOT+"');")

    def test_docker_missing_object_case_does_not_hide_real_errors(self):
        remote = m.Remote({"side": "target", "action": "inspect"})
        for message in ["Error: No such container", "Error response from daemon: no such volume"]:
            remote.run = lambda *args, **kwargs: types.SimpleNamespace(returncode=1, stderr=message)
            self.assertIsNone(remote.docker_inspect("not-created"))
        remote.run = lambda *args, **kwargs: types.SimpleNamespace(returncode=1, stderr="permission denied")
        with self.assertRaisesRegex(m.Refusal, "docker-inspect-failed"):
            remote.docker_inspect("unverified")

    def test_monitor_health_requires_active_timer_and_fresh_successful_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = m.Remote({"side": "target", "owner": OWNER, "action": "status"})
            remote.scope = pathlib.Path(directory).resolve()
            remote.run = lambda *args, **kwargs: types.SimpleNamespace(returncode=0)
            m.save_json(remote.scope/"monitor-status.json", {"sampled_at": m.time.time(), "monitor_ok": True})
            self.assertTrue(remote.monitor_health()["healthy"])
            remote.run = lambda *args, **kwargs: types.SimpleNamespace(returncode=3)
            self.assertFalse(remote.monitor_health()["healthy"])
            remote.run = lambda *args, **kwargs: types.SimpleNamespace(returncode=0)
            m.save_json(remote.scope/"monitor-status.json", {"sampled_at": m.time.time()-60, "monitor_ok": True})
            self.assertFalse(remote.monitor_health()["healthy"])


if __name__ == "__main__":
    unittest.main()
