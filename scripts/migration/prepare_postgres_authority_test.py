"""Offline authority handover tests; opt-in Docker test uses network=none only."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import secrets
import shlex
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("authority", pathlib.Path(__file__).with_name("prepare_postgres_authority.py"))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
OWNER = "ab"*16


def authority_state(directory):
    return {"run": m.RUN, "owner": OWNER, "system_identifier": "123456789",
        "source_timeline": 1, "image_id": "sha256:reviewed", "volume_path": str(pathlib.Path(directory).resolve()/"data"),
        "authority_mode": "ADOPTING", "phase": "new-monitor-active", "promotion_intent": None,
        "library_sha256": m.digest(m.LIBRARY_SOURCE), "original_container_id": "original", "created_at": time.time()}


def standby_sample():
    return {"running": True, "target_free_bytes": 5*1024**3, "container_id": "original",
        "system_identifier": "123456789", "version_num": 180003, "port": m.PORT,
        "listen_addresses": "127.0.0.1", "in_recovery": True, "transaction_read_only": "on",
        "default_transaction_read_only": "off", "receiver_status": "streaming", "primary_slot_name": m.SLOT,
        "sender_host": "127.0.0.1", "sender_port": 15432, "received_tli": 1,
        "replay_lsn": "1/20000", "receive_lsn": "1/20000", "wal_timeline": None}


def valid_promotion(state):
    state["authority_mode"] = "PROMOTION_INTENT"
    state["promotion_intent"] = {"op_id": "cd"*16, "source_fence_sha256": "e"*64,
        "final_lsn": "1/20000", "system_identifier": state["system_identifier"],
        "source_timeline": 1, "accepted_at": time.time()}


def container_fixture(state, original=True):
    return {"Id": "original" if original else "replacement", "Image": state["image_id"],
        "Config": {"Labels": {m.LABEL: OWNER, "io.sub2api.migration.run": m.LIB["RUN"],
                                  **({} if original else {m.AUTHORITY_LABEL: m.RUN})},
                   "Entrypoint": ["/bin/sh"], "Cmd": ["-ceu", m.LIB["bootstrap_shell"]() if original else m.role_bootstrap()]},
        "HostConfig": {"NetworkMode": "host", "PortBindings": {}, "Memory": 768*m.MIB, "MemorySwap": 768*m.MIB,
                       "PidsLimit": 128, "NanoCpus": 1000000000, "RestartPolicy": {"Name": "no"},
                       "Tmpfs": {"/var/lib/postgresql": "rw,noexec,nosuid,size=1m,mode=0755",
                                 "/run/sub2api-migration": "rw,noexec,nosuid,size=1m,mode=0700"}},
        "Mounts": [{"Destination": m.DATA, "Type": "volume", "Name": m.VOLUME, "Source": state["volume_path"], "RW": True},
                   {"Destination": "/run/replication-input.pgpass", "Type": "bind", "RW": False,
                    "Source": str(pathlib.Path(m.OLD_SCOPE if original else m.SCOPE)/"replication.pgpass")},
                   {"Destination": "/run/sub2api-migration", "Type": "tmpfs", "RW": True}],
        "State": {"Running": True, "Status": "running", "ExitCode": 0, "OOMKilled": False}}


class FakeAuthority(m.Authority):
    """Real handover state machine with filesystem, Docker and systemd adapters."""
    def __init__(self, directory):
        super().__init__({"action": "apply", "program": pathlib.Path(m.__file__).read_text()})
        self.scope = pathlib.Path(directory)
        self.log = self.scope/"errors.log"
        self.events, self.fail_after = [], None
        self.persisted = authority_state(directory)
        path = pathlib.Path(self.persisted["volume_path"])
        path.mkdir()
        for name, content in [("PG_VERSION", "18\n"), (".sub2api-seed-verified", ""), ("standby.signal", "")]:
            (path/name).write_text(content)
        m.save_json(self.scope/"state.json", self.persisted)
        self.containers = {m.CONTAINER: container_fixture(self.persisted)}
        self.p["source"] = {"sampled_at": time.time(), "in_recovery": False, "role_login": True,
            "monitor": {"healthy": True}, "safety_tripped": False, "source_free_bytes": 5*1024**3,
            "wal_cap_mb": 512, "slots": [{"name": m.SLOT, "active": True, "wal_status": "reserved"}],
            "owner": OWNER, "system_identifier": self.persisted["system_identifier"], "timeline": 1, "wal_lsn": "1/10000"}
        self.legacy_masked = False

    def docker_inspect(self, name, *, kind="container", **kwargs):
        if kind == "image":
            return {"Id": "sha256:reviewed"}
        if kind == "volume":
            return {"Labels": {m.LABEL: OWNER, "io.sub2api.migration.run": m.LIB["RUN"]},
                    "Mountpoint": self.persisted["volume_path"], "Driver": "local"}
        return copy.deepcopy(self.containers.get(name))

    def query(self, query, **kwargs):
        self.assert_no_promotion_query(query)
        sample = standby_sample()
        sample["default_transaction_read_only"] = "on" if self.containers[m.CONTAINER]["Id"] == "original" else "off"
        return {k: v for k, v in sample.items() if k not in ["running", "target_free_bytes", "container_id"]}

    def assert_no_promotion_query(self, query):
        assert "pg_promote" not in query and "ALTER " not in query and "INSERT " not in query

    def legacy_unit_status(self):
        return {name: {"masked": self.legacy_masked, "ActiveState": "inactive" if self.legacy_masked else "active",
                       "MainPID": "0", "Job": ""} for name in self.old_units()}

    def ensure_monitor_installed(self, state):
        self.events.append("new-monitor-active")

    def install_monitor(self, state):
        self.events.append("new-monitor-active")
        self.checkpoint(state, "new-monitor-active")

    def retire_old_monitor(self, state):
        self.events.append("legacy-masked-idle")
        self.legacy_masked = True
        self.checkpoint(state, "legacy-monitor-masked-and-idle")

    def run(self, argv, data=None, **kwargs):
        self.events.append(tuple(argv))
        if argv[:2] == ["docker", "kill"]:
            self.containers[m.CONTAINER]["State"].update(Running=False, Status="exited")
        elif argv[:2] == ["docker", "rename"]:
            self.containers[m.ARCHIVE] = self.containers.pop(m.CONTAINER)
        elif argv[:2] == ["docker", "create"]:
            assert m.CONTAINER not in self.containers
            self.containers[m.CONTAINER] = container_fixture(self.persisted, original=False)
            self.containers[m.CONTAINER]["State"].update(Running=False, Status="created")
        elif argv[:2] == ["docker", "start"]:
            self.containers[m.CONTAINER]["State"].update(Running=True, Status="running")
        elif argv[:2] != ["systemctl", "is-active"]:
            raise AssertionError("unexpected external command: "+str(argv))
        if self.fail_after == tuple(argv[:2]):
            self.fail_after = None
            raise m.Refusal("injected-lost-response-after-success")
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)


class AuthorityTests(unittest.TestCase):
    def test_default_is_entirely_offline(self):
        with mock.patch.object(m, "Controller", side_effect=AssertionError("must not connect")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(m.main([]), 0)
            self.assertFalse(json.loads(out.getvalue())["remote_commands_executed"])

    def test_inspection_never_selects_mutating_remote_action(self):
        fake = mock.Mock()
        fake.call.return_value = {}
        with mock.patch.object(m, "Controller", return_value=fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(["--inspect"]), 0)
        self.assertEqual([c.args[:2] for c in fake.call.call_args_list], [("source", "source-inspect"), ("target", "inspect")])

    def test_source_readonly_inspect_derives_identity_without_full_size_scan(self):
        remote = mock.Mock()
        remote.source_status.return_value = {"system_identifier": "123"}
        remote.source_facts.return_value = {"image_id": "sha256:reviewed"}
        remote.docker_inspect.return_value = {"Id": "sha256:reviewed"}
        remote.image_identity.return_value = {"config_digest": "sha256:reviewed"}
        with mock.patch.dict(m.LIB, {"private_dir": mock.Mock()}), mock.patch.object(m, "read_json", return_value={"owner": OWNER}), \
             mock.patch.object(m, "Base", return_value=remote):
            result = m.source_inspect()
        self.assertEqual(result["image_identity"], {"config_digest": "sha256:reviewed"})
        remote.source_facts.assert_called_once_with(full_size=False)
        remote.docker_inspect.assert_called_once_with("sha256:reviewed", kind="image")

    def test_mutually_exclusive_modes_cannot_inspect_and_apply(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.main(["--inspect", "--apply"])

    def test_generated_remote_and_monitor_programs_compile(self):
        source = pathlib.Path(m.__file__).read_text()
        for action in ["source-inspect", "inspect", "apply", "monitor"]:
            compile(m.bundled_program({"action": action}, source, m.LIBRARY_SOURCE), "<generated-authority>", "exec")
        subprocess.run(["sh", "-n"], input=m.role_bootstrap(), text=True, check=True, capture_output=True)

    def test_role_aware_bootstrap_retains_verified_volume_without_reseeding(self):
        source = m.role_bootstrap()
        self.assertIn(".sub2api-seed-verified", source)
        self.assertIn("default_transaction_read_only=off", source)
        self.assertNotIn("standby.signal", source)
        self.assertNotIn("pg_basebackup", source)
        self.assertNotIn("pg_promote", source)
        command = m.container_command(OWNER, "sha256:reviewed")
        self.assertIn("type=volume,src="+m.VOLUME+",dst="+m.DATA+",volume-nocopy", command)
        self.assertEqual(command[command.index("--restart")+1], "no")

    def test_unexpected_primary_and_unknown_identity_are_never_stopped(self):
        state = authority_state("/unused")
        for values in [{"in_recovery": False}, {"system_identifier": "another-cluster"}, {"in_recovery": None}]:
            sample = {**standby_sample(), **values, "target_free_bytes": 0}
            decision = m.classify(state, sample)
            self.assertFalse(decision["healthy"])
            self.assertFalse(decision["stop_standby"])

    def test_only_identified_uncommitted_standby_can_be_stopped_on_low_disk(self):
        state = authority_state("/unused")
        sample = {**standby_sample(), "target_free_bytes": 0}
        self.assertTrue(m.classify(state, sample)["stop_standby"])
        state["promotion_intent"] = {"malformed": "must-still-block-stop"}
        self.assertFalse(m.classify(state, sample)["stop_standby"])
        self.assertFalse(m.classify(state, sample)["healthy"])
        valid_promotion(state)
        self.assertFalse(m.classify(state, sample)["stop_standby"])

    def test_valid_promotion_intent_accepts_new_timeline_without_stopping(self):
        state = authority_state("/unused")
        valid_promotion(state)
        sample = {**standby_sample(), "in_recovery": False, "transaction_read_only": "off", "wal_timeline": 2}
        self.assertTrue(m.classify(state, sample)["healthy"])
        for mutation in [{"wal_timeline": 1}, {"default_transaction_read_only": "on"}, {"target_free_bytes": 0}]:
            decision = m.classify(state, {**sample, **mutation})
            self.assertFalse(decision["healthy"])
            self.assertFalse(decision["stop_standby"])

    def test_incomplete_or_nonfinite_intent_is_not_authority(self):
        state = authority_state("/unused")
        valid_promotion(state)
        for key, value in [("source_fence_sha256", "wrong"), ("system_identifier", "wrong"), ("accepted_at", float("inf")),
                           ("accepted_at", True), ("final_lsn", "100000000/0")]:
            changed = copy.deepcopy(state)
            changed["promotion_intent"][key] = value
            self.assertFalse(m.valid_intent(changed))

    def test_standby_streaming_identity_and_default_are_verified(self):
        state = authority_state("/unused")
        state["authority_mode"] = "STANDBY"
        for mutation in [{"received_tli": 2}, {"sender_port": 5432}, {"sender_host": "another-host"},
                         {"primary_slot_name": "other"}, {"default_transaction_read_only": "on"}]:
            self.assertFalse(m.classify(state, {**standby_sample(), **mutation})["healthy"])

    def test_normal_handover_masks_before_stop_and_preserves_same_volume(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as old:
            path = pathlib.Path(old)/"replication.pgpass"
            path.write_text("private-replication-password\n")
            path.chmod(0o600)
            with mock.patch.object(m, "SCOPE", directory), mock.patch.object(m, "OLD_SCOPE", old), \
                 mock.patch.object(m, "root_directory", side_effect=lambda p, **kw: pathlib.Path(p)), mock.patch.object(m, "require_root"):
                remote = FakeAuthority(directory)
                result = remote.apply()
                self.assertEqual(result["authority_mode"], "STANDBY")
                self.assertTrue(result["legacy_monitors_masked_idle"])
                stop = next(i for i, e in enumerate(remote.events) if isinstance(e, tuple) and e[:2] == ("docker", "kill"))
                self.assertLess(remote.events.index("new-monitor-active"), remote.events.index("legacy-masked-idle"))
                self.assertLess(remote.events.index("legacy-masked-idle"), stop)
                self.assertEqual(remote.containers[m.ARCHIVE]["Id"], "original")
                self.assertFalse(remote.containers[m.ARCHIVE]["State"]["Running"])
                self.assertEqual(remote.containers[m.CONTAINER]["Mounts"][0]["Name"], m.VOLUME)
                self.assertTrue((pathlib.Path(remote.persisted["volume_path"])/"standby.signal").exists())
                self.assertFalse(any(isinstance(e, tuple) and e[:2] in [("docker", "rm"), ("docker", "stop")] for e in remote.events))

    def test_first_adoption_verifies_live_legacy_monitor_before_replacement(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as old:
            with mock.patch.object(m, "SCOPE", directory), mock.patch.object(m, "OLD_SCOPE", old), \
                 mock.patch.object(m, "root_directory", side_effect=lambda p, **kw: pathlib.Path(p)):
                remote = FakeAuthority(directory)
                (remote.scope/"state.json").unlink()
                prepared = {"owner": OWNER, "system_identifier": "123456789", "image_id": "sha256:reviewed"}
                remote.prepared_state = lambda: prepared
                remote.p["source"]["image_identity"] = {"config_digest": "reviewed"}
                remote.image_identity = lambda image: {"config_digest": "reviewed"}
                sample = pathlib.Path(old)/"monitor-status.json"
                m.save_json(sample, {"sampled_at": time.time()-21, "monitor_ok": True})
                with self.assertRaisesRegex(m.Refusal, "sample-stale"):
                    remote.adopt_state(remote.p["source"])
                self.assertFalse((remote.scope/"state.json").exists())
                m.save_json(sample, {"sampled_at": time.time(), "monitor_ok": True})
                state = remote.adopt_state(remote.p["source"])
                self.assertEqual(state["original_container_id"], "original")
                self.assertEqual(state["authority_mode"], "ADOPTING")
                self.assertEqual(remote.events, [])

    def test_legacy_anonymous_parent_volume_is_pinned_and_never_repurposed(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = FakeAuthority(directory)
            parent = {"Destination": "/var/lib/postgresql", "Type": "volume", "Name": "legacy-anonymous",
                      "Source": "/var/lib/docker/volumes/legacy-anonymous/_data", "RW": True}
            remote.containers[m.CONTAINER]["Mounts"].append(copy.deepcopy(parent))
            remote.persisted["legacy_parent_mount"] = [parent]
            self.assertIsNotNone(remote.object(m.CONTAINER, remote.persisted, original=True))
            remote.containers[m.CONTAINER]["Mounts"][-1]["Name"] = "unrelated"
            with self.assertRaisesRegex(m.Refusal, "legacy-parent-mount"):
                remote.object(m.CONTAINER, remote.persisted, original=True)

    def test_lost_rename_create_start_responses_are_retryable_with_inactive_slot(self):
        for operation in [("docker", "rename"), ("docker", "create"), ("docker", "start")]:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as old:
                secret = pathlib.Path(old)/"replication.pgpass"
                secret.write_text("private-replication-password\n")
                secret.chmod(0o600)
                with mock.patch.object(m, "SCOPE", directory), mock.patch.object(m, "OLD_SCOPE", old), \
                     mock.patch.object(m, "root_directory", side_effect=lambda p, **kw: pathlib.Path(p)), mock.patch.object(m, "require_root"):
                    remote = FakeAuthority(directory)
                    remote.fail_after = operation
                    with self.assertRaisesRegex(m.Refusal, "lost-response"):
                        remote.apply()
                    remote.p["source"]["slots"][0]["active"] = False
                    result = remote.apply()
                    self.assertEqual(result["phase"], "standby-role-aware-ready")
                    self.assertEqual(sum(e == operation+(m.CONTAINER, m.ARCHIVE) for e in remote.events), 1 if operation == ("docker", "rename") else 0)
                    self.assertEqual(remote.containers[m.ARCHIVE]["Id"], "original")
                    self.assertEqual(remote.containers[m.CONTAINER]["Id"], "replacement")

    def test_missing_standby_signal_or_promotion_intent_refuses_apply_before_mutation(self):
        for promoted in [False, True]:
            with self.subTest(promoted=promoted), tempfile.TemporaryDirectory() as directory:
                remote = FakeAuthority(directory)
                state = remote.persisted
                if promoted:
                    valid_promotion(state)
                    m.save_json(remote.scope/"state.json", state)
                else:
                    (pathlib.Path(state["volume_path"])/"standby.signal").unlink()
                with mock.patch.object(m, "root_directory", side_effect=lambda p, **kw: pathlib.Path(p)), mock.patch.object(m, "require_root"):
                    with self.assertRaises(m.Refusal):
                        remote.apply()
                self.assertEqual(remote.events, [])

    def test_changed_volume_or_container_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = FakeAuthority(directory)
            remote.containers[m.CONTAINER]["Mounts"][0]["Name"] = "unrelated-volume"
            with self.assertRaisesRegex(m.Refusal, "pgdata-mount"):
                remote.sample(remote.persisted)
            remote.containers[m.CONTAINER]["Mounts"][0]["Name"] = m.VOLUME
            remote.containers[m.CONTAINER]["Config"]["Labels"][m.LABEL] = "wrong-owner"
            with self.assertRaisesRegex(m.Refusal, "ownership"):
                remote.sample(remote.persisted)

    def test_source_slot_and_protection_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = FakeAuthority(directory)
            for mutation in [{"in_recovery": True}, {"wal_cap_mb": -1}, {"monitor": {"healthy": False}}, {"sampled_at": time.time()-121}]:
                original = copy.deepcopy(remote.p["source"])
                remote.p["source"].update(mutation)
                with self.assertRaises(m.Refusal):
                    remote.verify_source()
                remote.p["source"] = original

    def test_legacy_timer_mainpid_is_correctly_absent_on_real_systemd(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = m.Authority({"action": "inspect"})
            for name, body in remote.old_units().items():
                (pathlib.Path(directory)/name).write_text(body)
            def run(argv, **kwargs):
                timer = argv[2].endswith(".timer")
                return types.SimpleNamespace(stdout="ActiveState=inactive\nJob=\n"+("" if timer else "MainPID=0\nRestart=no\n"))
            remote.run = run
            with mock.patch.object(m, "SYSTEM_UNITS", directory):
                self.assertTrue(all(s["MainPID"] == "0" for s in remote.legacy_unit_status().values()))

    def test_legacy_unit_retirement_preserves_backup_and_masks_persistently(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as units:
            remote = m.Authority({"action": "apply"})
            remote.scope = pathlib.Path(directory)
            state = authority_state(directory)
            events = []
            for name, body in remote.old_units().items():
                (pathlib.Path(units)/name).write_text(body)
            def run(argv, **kwargs):
                events.append(argv)
                return types.SimpleNamespace(stdout="ActiveState=inactive\nMainPID=0\nJob=\nRestart=no\n")
            remote.run = run
            with mock.patch.object(m, "SYSTEM_UNITS", units):
                remote.retire_old_monitor(state)
                remote.retire_old_monitor(state)
            for name, body in remote.old_units().items():
                self.assertEqual(os.readlink(pathlib.Path(units)/name), "/dev/null")
                self.assertEqual((remote.scope/(name+".before")).read_text(), body)
            self.assertFalse(any(argv[:2] == ["systemctl", "stop"] for argv in events))

    def test_busy_legacy_oneshot_is_never_killed_or_bypassed(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = m.Authority({"action": "apply"})
            remote.scope = pathlib.Path(directory)
            remote.run = mock.Mock()
            busy = {name: {"masked": False, "ActiveState": "activating", "MainPID": "567", "Job": ""} for name in remote.old_units()}
            remote.legacy_unit_status = lambda: busy
            with mock.patch.object(m.time, "monotonic", side_effect=[0, 0, 36]), mock.patch.object(m.time, "sleep"):
                with self.assertRaisesRegex(m.Refusal, "still-running"):
                    remote.retire_old_monitor(authority_state(directory))
            self.assertEqual(remote.run.call_args_list, [mock.call(["systemctl", "disable", "--now", m.LIB["UNIT_TARGET"]+".timer"])])

    def test_monitor_does_not_issue_stop_for_a_primary_after_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = m.Authority({"action": "monitor"})
            remote.scope = pathlib.Path(directory)
            state = authority_state(directory)
            valid_promotion(state)
            state["authority_mode"] = "PRIMARY"
            remote.state = lambda: state
            remote.owned_volume = lambda state: pathlib.Path(directory)
            remote.sample = lambda state: {**standby_sample(), "in_recovery": False, "transaction_read_only": "off", "wal_timeline": 2, "target_free_bytes": 0}
            remote.stop_standby = mock.Mock(side_effect=AssertionError("must not stop primary"))
            self.assertFalse(remote.monitor()["monitor_ok"])
            remote.stop_standby.assert_not_called()

    def test_monitor_keeps_disk_evidence_when_role_query_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = m.Authority({"action": "monitor"})
            remote.scope = pathlib.Path(directory)
            remote.state = lambda: authority_state(directory)
            remote.owned_volume = lambda state: pathlib.Path(directory)
            remote.sample = mock.Mock(side_effect=m.Refusal("query-timeout"))
            remote.stop_standby = mock.Mock(side_effect=AssertionError("unknown role must not stop"))
            status = remote.monitor()
            self.assertFalse(status["monitor_ok"])
            self.assertIsInstance(status["target_free_bytes"], int)
            remote.stop_standby.assert_not_called()

    def test_wal_lsn_comparison_is_numeric(self):
        self.assertGreater(m.lsn_number("A/0"), m.lsn_number("9/FFFFFFFF"))
        for value in ["a/0", "100000000/0", "bad", None]:
            with self.assertRaises(m.Refusal):
                m.lsn_number(value)


@unittest.skipUnless(os.environ.get("SUB2API_PG_AUTHORITY_DOCKER_IMAGE"), "opt-in isolated PostgreSQL image required")
class IsolatedDockerRestartTests(unittest.TestCase):
    def test_recovery_and_primary_survive_generated_bootstrap_restart(self):
        # No network, published ports, production resources, remote hosts, or
        # pulls. Replication uses a disposable shared Unix socket volume.
        image = os.environ["SUB2API_PG_AUTHORITY_DOCKER_IMAGE"]
        tag = "sub2api-authority-test-"+secrets.token_hex(6)
        source, target = tag+"-source", tag+"-target"
        volumes = [tag+"-source-data", tag+"-target-data", tag+"-socket"]
        containers = []
        def run(*argv, timeout=60, check=True, data=None):
            return subprocess.run(["docker", *argv], input=data, text=True, capture_output=True, timeout=timeout, check=check).stdout.strip()
        def sql(name, query):
            port = "5432" if name == source else str(m.PORT)
            host = "/var/run/postgresql" if name == source else "/tmp"
            return run("exec", "-i", name, "psql", "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-h", host,
                       "-p", port, "-U", "sub2api", "-d", "sub2api", data=query)
        def wait_sql(name, predicate):
            end = time.monotonic()+40
            while time.monotonic() < end:
                try:
                    if predicate():
                        return
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    pass
                time.sleep(0.2)
            raise AssertionError("isolated PostgreSQL did not reach expected role")
        with tempfile.TemporaryDirectory() as directory:
            pgpass = pathlib.Path(directory)/"pgpass"
            pgpass.write_text("# Unix socket test uses trust\n")
            pgpass.chmod(0o600)
            try:
                run("image", "inspect", image)
                for volume in volumes:
                    run("volume", "create", "--label", "io.sub2api.test="+tag, volume)
                containers.append(source)
                run("run", "-d", "--name", source, "--label", "io.sub2api.test="+tag, "--network", "none", "--restart", "no",
                    "-e", "POSTGRES_USER=sub2api", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "PGDATA="+m.DATA,
                    "--mount", f"type=volume,src={volumes[0]},dst={m.DATA}", "--mount", f"type=volume,src={volumes[2]},dst=/var/run/postgresql",
                    image, "postgres", "-c", "shared_buffers=32MB")
                wait_sql(source, lambda: sql(source, "SELECT NOT pg_is_in_recovery();") == "t")
                seed = tag+"-seed"
                containers.append(seed)
                script = f'''set -eu
chown postgres:postgres {m.DATA}
chmod 700 {m.DATA}
if command -v gosu >/dev/null; then drop=gosu; else drop=su-exec; fi
"$drop" postgres pg_basebackup -h /source-sock -U sub2api -D {m.DATA} -R -X stream --checkpoint=fast
"$drop" postgres pg_verifybackup {m.DATA}
touch {m.DATA}/.sub2api-seed-verified
chown postgres:postgres {m.DATA}/.sub2api-seed-verified
'''
                run("run", "--name", seed, "--label", "io.sub2api.test="+tag, "--network", "none", "--entrypoint", "/bin/sh",
                    "--mount", f"type=volume,src={volumes[1]},dst={m.DATA}", "--mount", f"type=volume,src={volumes[2]},dst=/source-sock",
                    image, "-ceu", script, timeout=90)
                containers.append(target)
                run("run", "-d", "--name", target, "--label", "io.sub2api.test="+tag, "--network", "none", "--restart", "no",
                    "--tmpfs", "/var/lib/postgresql:rw,noexec,nosuid,size=1m,mode=0755",
                    "--entrypoint", "/bin/sh", "--mount", f"type=volume,src={volumes[1]},dst={m.DATA}",
                    "--mount", f"type=volume,src={volumes[2]},dst=/source-sock", "--mount", f"type=bind,src={pgpass},dst=/run/replication-input.pgpass,readonly",
                    image, "-ceu", m.role_bootstrap())
                expected = "t|on|off"
                role_query = "SELECT pg_is_in_recovery(),current_setting('transaction_read_only'),current_setting('default_transaction_read_only');"
                wait_sql(target, lambda: sql(target, role_query) == expected)
                run("restart", "--time", "30", target, timeout=40)
                wait_sql(target, lambda: sql(target, role_query) == expected)
                # Promotion exists only in this disposable offline test, never
                # in the preparation tool or its remote action allowlist.
                self.assertEqual(sql(target, "SELECT pg_promote(true,5);"), "t")
                self.assertEqual(sql(target, role_query), "f|off|off")
                sql(target, "CREATE TABLE authority_restart_test(id int); INSERT INTO authority_restart_test VALUES (42);")
                run("restart", "--time", "30", target, timeout=40)
                wait_sql(target, lambda: sql(target, role_query) == "f|off|off")
                self.assertEqual(sql(target, "SELECT id FROM authority_restart_test;"), "42")
            except Exception as exc:
                diagnostics = {}
                for name in containers:
                    result = subprocess.run(["docker", "logs", "--tail", "25", name], text=True, capture_output=True, timeout=10)
                    diagnostics[name] = (result.stdout+result.stderr)[-5000:]
                raise AssertionError("isolated test diagnostics: "+json.dumps(diagnostics)) from exc
            finally:
                for name in reversed(containers):
                    # Names are random and creation intent was recorded before
                    # each create. Identity is checked before local cleanup.
                    details = run("inspect", name, check=False)
                    if details:
                        info = json.loads(details)[0]
                        self.assertEqual(info["Config"]["Labels"].get("io.sub2api.test"), tag)
                        run("rm", "-f", "-v", name, check=False)
                for volume in reversed(volumes):
                    details = run("volume", "inspect", volume, check=False)
                    if details:
                        self.assertEqual(json.loads(details)[0]["Labels"].get("io.sub2api.test"), tag)
                        run("volume", "rm", volume, check=False)


if __name__ == "__main__":
    unittest.main()
