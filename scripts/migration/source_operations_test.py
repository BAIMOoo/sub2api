"""Offline fixed-action source backend tests; no network or production access."""
import contextlib
import copy
import importlib.util
import io
import ipaddress
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("source_operations", pathlib.Path(__file__).with_name("source_operations.py"))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
OP = "11" * 16
OP2 = "22" * 16
OP3 = "33" * 16
OWNER = "ab" * 16


def fixture():
    configs = {kind: {"Cmd": [kind], "Env": ["SECRET=never-expose"]} for kind in m.NAMES}
    manifest = {"v": 1, "run": m.RUN, "owner": OWNER, "machine_id_sha256": "a" * 64,
                "containers": {}, "postgres": {"system_identifier": "7626048206782038050", "timeline": 1,
                "replication_user": "sub2api_mig_20261016_replica", "replication_application": "sub2api-pg-replica-20261016",
                "replication_address": "172.18.0.1", "slot": "sub2api_mig_20261016"},
                "redis": {"source_run_id": "a" * 40, "source_replid": "b" * 40, "target_run_id": "c" * 40},
                "units": {unit: "d" * 64 for unit in (*m.PG_UNITS, m.GUARD_UNIT)}}
    containers, images = {}, {}
    for index, kind in enumerate(m.NAMES, 1):
        cid = str(index) * 64
        image_id = "sha256:" + str(index + 3) * 64
        image = {"config_digest": image_id, "diff_ids": ["sha256:" + "f" * 64], "os": "linux", "architecture": "amd64"}
        manifest["containers"][kind] = {"id": cid, "config_sha256": m.digest(configs[kind]), "image": image}
        containers[m.NAMES[kind]] = {"Id": cid, "Image": image_id, "Config": configs[kind],
                                   "State": {"Running": True, "Status": "running", "Pid": 1234,
                                             "StartedAt": "2026-10-16T00:00:00.000000000Z", "ExitCode": 0, "OOMKilled": False},
                                   "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}
        images[image_id] = {"Id": image_id, "RootFS": {"Layers": image["diff_ids"]}, "Os": "linux", "Architecture": "amd64"}
    return manifest, containers, images


class FakeBackend:
    def pg_sampling_lock(self):
        return contextlib.nullcontext()

    def __init__(self):
        manifest, self.containers, self.images = fixture()
        self.clock = 1000.0
        self.files = {"manifest": manifest, "ledger": None,
                      "guard": {"v": 1, "run": m.RUN, "owner": OWNER, "boot_id": "boot1", "checked_at": self.clock,
                                "healthy": True, "pg_wal_protected": True},
                      "attestations": {"v": 1, "run": m.RUN, "owner": OWNER, "boot_id": "boot1", "checked_at": self.clock,
                                       "app_route_fenced": True, "postgres": {"writers_quiet": True, "final_lsn": "1/100", "target_replay_lsn": "1/100", "arm_id": "e" * 32},
                                       "redis": {"writers_quiet": True, "consumers_paused": True}}}
        self.pg = {"system_identifier": manifest["postgres"]["system_identifier"], "timeline": 1, "in_recovery": False,
                   "flushed_lsn": "1/110", "prepared_transactions": 0, "sessions": [], "replication": [], "slots": [], "wal_cap_mb": "512MB"}
        self.source_redis = {"run_id": "a" * 40, "role": "master", "master_replid": "b" * 40,
                             "master_repl_offset": "10", "master_failover_state": "no-failover",
                             "slave0": "ip=172.18.0.1,port=26379,state=online,offset=10,lag=0"}
        self.target_redis = {"run_id": "c" * 40, "role": "slave", "master_replid": "b" * 40,
                             "master_link_status": "up", "master_sync_in_progress": "0"}
        self.clients = [{"id": "20", "addr": "172.18.0.1:45678", "flags": "S", "name": ""}]
        self.target_clients = [{"id": "30", "addr": "127.0.0.1:16379", "flags": "M", "name": ""}]
        self.sockets = b""
        self.events, self.snapshots = [], []
        self.fail_after = None
        self.signal_exits = True
        self.units = {unit: {"ActiveState": "inactive", "SubState": "dead", "MainPID": "0", "Job": "", "UnitFileState": "static", "LoadState": "loaded"} for unit in m.PG_UNITS}
        self.units[m.PG_UNITS[0]].update(ActiveState="active", SubState="waiting", UnitFileState="enabled")
        self.units[m.GUARD_UNIT] = {"ActiveState": "active", "SubState": "running", "MainPID": "100", "Job": "", "UnitFileState": "enabled", "LoadState": "loaded"}
        self.masked = set()
        self.machine = "a" * 64
        self.boot = "boot1"
        self.contents = {}

    def read(self, name):
        return copy.deepcopy(self.files[name])

    def write(self, value):
        self.files["ledger"] = copy.deepcopy(value)
        self.snapshots.append(copy.deepcopy(value))

    @contextlib.contextmanager
    def lock(self):
        yield

    def now(self):
        return self.clock

    def machine_digest(self):
        return self.machine

    def boot_id(self):
        return self.boot

    def unit_status(self, unit):
        return copy.deepcopy(self.units[unit])

    def unit_identity(self, unit, expected_digest, allow_masked=False):
        if unit in self.masked:
            m.require(allow_masked, "UNIT_IDENTITY_CHANGED")
            return "masked"
        return "original"

    def mask_unit(self, unit, expected_digest):
        self.events.append(("mask", unit))
        self.masked.add(unit)
        self.units[unit].update(UnitFileState="masked", LoadState="masked")

    def stopped(self, kind):
        self.containers[m.NAMES[kind]]["State"].update(Running=False, Status="exited", ExitCode=0)
        self.containers[m.NAMES[kind]]["HostConfig"]["RestartPolicy"]["Name"] = "no"

    def promote_redis(self):
        self.source_redis.update(role="slave", master_host="172.18.0.1", master_port="26379", master_link_status="up", master_failover_state="no-failover")
        self.target_redis["role"] = "master"
        self.clients = [{"id": "20", "addr": "172.18.0.1:26379", "flags": "M", "name": ""}]
        self.target_clients = [{"id": "30", "addr": "127.0.0.1:45678", "flags": "S", "name": ""}]

    def command(self, argv, data=None, timeout=2):
        self.events.append(tuple(argv))
        if argv[:3] == ["/usr/bin/docker", "container", "inspect"]:
            return m.canonical([self.containers[argv[3]]])
        if argv[:3] == ["/usr/bin/docker", "image", "inspect"]:
            return m.canonical([self.images[argv[3]]])
        if argv[:5] == ["/usr/bin/ctr", "-n", "moby", "content", "get"]:
            return self.contents[argv[5]]
        if argv[0] == "/usr/bin/nsenter":
            return self.sockets
        if argv[:3] == ["/usr/bin/systemctl", "disable", m.PG_UNITS[0]]:
            self.units[m.PG_UNITS[0]]["UnitFileState"] = "disabled"
            return b""
        if argv[:3] == ["/usr/bin/systemctl", "stop", m.PG_UNITS[0]]:
            self.units[m.PG_UNITS[0]].update(ActiveState="inactive", SubState="dead")
            return b""
        if argv == ["/usr/bin/systemctl", "daemon-reload"]:
            return b""
        if argv[:2] == ["/usr/bin/docker", "update"]:
            kind = next(kind for kind, name in m.NAMES.items() if self.containers[name]["Id"] == argv[-1])
            self.containers[m.NAMES[kind]]["HostConfig"]["RestartPolicy"]["Name"] = "no"
            self._assert_intent("restart_no")
            return self._maybe_lost("update", b"")
        if argv[:2] == ["/usr/bin/docker", "kill"]:
            kind = next(kind for kind, name in m.NAMES.items() if self.containers[name]["Id"] == argv[-1])
            self._assert_intent("sigint" if kind == "postgres" else "sigterm")
            if self.signal_exits:
                self.stopped(kind)
            return self._maybe_lost("signal", b"")
        if "psql" in argv:
            assert data == m.PG_QUERY
            return m.canonical(self.pg) + b"\n"
        if "redis-cli" in argv:
            assert self.containers[m.NAMES["redis"]]["State"]["Running"], "exec must not use stopped source Redis"
            target = "-h" in argv
            info = self.target_redis if target else self.source_redis
            if data:
                assert data == ("CLIENT SETNAME " + m.SELF_CLIENT + "\nCLIENT LIST\n").encode()
                clients = copy.deepcopy(self.target_clients if target else self.clients)
                clients.append({"id": "999", "addr": "127.0.0.1:60000", "flags": "N", "name": m.SELF_CLIENT})
                return ("OK\n" + "\n".join(" ".join(key + "=" + value for key, value in item.items()) for item in clients) + "\n").encode()
            if "INFO" in argv:
                if argv[-1] == "INFO":
                    return ("# Server\r\nrun_id:" + info["run_id"] + "\r\n\r\n"
                            "# Clients\r\nconnected_clients:4\r\n\r\n# Replication\r\n" +
                            "\r\n".join(key + ":" + value for key, value in info.items() if key != "run_id") +
                            "\r\n\r\n# Keyspace\r\ndb0:keys=5,expires=2,avg_ttl=1\r\n").encode()
                return ("\n".join(key + ":" + value for key, value in info.items()) + "\n").encode()
            if argv[-1] == "ROLE":
                return (info["role"] + "\n").encode()
            if "FAILOVER" in argv:
                self._assert_intent("failover")
                assert "FORCE" not in argv
                self.promote_redis()
                return self._maybe_lost("failover", b"OK\n")
        raise AssertionError("Unexpected fixed command: " + repr(argv))

    def _assert_intent(self, name):
        assert any(op["steps"].get(name) == "intent" for op in self.files["ledger"]["operations"].values())

    def _maybe_lost(self, kind, result):
        if self.fail_after == kind:
            self.fail_after = None
            raise m.Unknown("COMMAND_OUTCOME_UNKNOWN")
        return result


class PG18CatalogBackend(FakeBackend):
    """Model inet projection and backend filtering over real PG18 catalog rows."""
    def command(self, argv, data=None, timeout=2):
        if "psql" not in argv:
            return super().command(argv, data, timeout)
        self.events.append(tuple(argv))
        assert data == m.PG_QUERY
        query = data.decode()
        excluded = re.search(r"backend_type NOT IN\s*\(([^)]+)\)", query)
        assert excluded is not None
        excluded = set(re.findall(r"'([^']+)'", excluded.group(1)))
        sample = copy.deepcopy(self.pg)
        sample["sessions"] = [row for row in sample["sessions"] if row["backend_type"] not in excluded]
        # PostgreSQL inet::text preserves /32; host(inet) returns its host.
        addresses = re.findall(r"'address',(host\(client_addr\)|client_addr::text)", query)
        assert len(addresses) == 2
        for collection, expression in zip(("sessions", "replication"), addresses):
            if expression == "host(client_addr)":
                for row in sample[collection]:
                    if row.get("address") is not None:
                        row["address"] = str(ipaddress.ip_interface(row["address"]).ip)
        return m.canonical(sample) + b"\n"


class SourceOperationsTests(unittest.TestCase):
    def test_real_pg_sampling_lock_serializes_helpers_and_rejects_unsafe_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            real_fstat = os.fstat
            def owned_stat(fd):
                value = real_fstat(fd)
                return types.SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_nlink=value.st_nlink)
            entered, done, failures = threading.Event(), threading.Event(), []
            def contender():
                try:
                    with m.pg_sampling_lock():
                        entered.set()
                except Exception as error:
                    failures.append(error)
                finally:
                    done.set()
            with mock.patch.object(m, "SCOPE", pathlib.Path(directory)), mock.patch.object(m, "trusted_parent"), \
                 mock.patch.object(m.os, "fstat", side_effect=owned_stat):
                with m.pg_sampling_lock():
                    thread = threading.Thread(target=contender)
                    thread.start()
                    self.assertFalse(entered.wait(0.03), "overlapping guard SQL was admitted")
                self.assertTrue(done.wait(1))
                thread.join()
                self.assertTrue(entered.is_set())
                self.assertEqual(failures, [])
                lock = pathlib.Path(directory) / "pg-sampling.lock"
                self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
                lock.chmod(0o644)
                with self.assertRaisesRegex(m.Refusal, "PG_SAMPLING_LOCK_UNTRUSTED"):
                    with m.pg_sampling_lock():
                        self.fail("unsafe lock admitted")

    def setUp(self):
        self.b = FakeBackend()
        self.ops = m.SourceOperations(self.b)

    def assert_refuses(self, code, callback):
        with self.assertRaises(m.Refusal) as raised:
            callback()
        self.assertEqual(str(raised.exception), code)

    def prepare_pg(self):
        self.b.stopped("app")
        self.assertEqual(self.ops.retire_pg_monitors(OP)["phase"], "complete")

    def prepare_redis(self):
        self.b.stopped("app")
        self.b.stopped("postgres")

    def signals(self):
        return [event for event in self.b.events if len(event) >= 2 and event[:2] == ("/usr/bin/docker", "kill")]

    def failovers(self):
        return [event for event in self.b.events if "FAILOVER" in event]

    def test_default_program_is_offline(self):
        output = subprocess.check_output([sys.executable, str(pathlib.Path(m.__file__))])
        self.assertFalse(json.loads(output)["commands_executed"])

    def test_resource_fences_is_only_full_identity_resource_summary(self):
        with mock.patch.object(self.b, "read", side_effect=AssertionError("no state sampling")), \
             mock.patch.object(self.b, "unit_status", side_effect=AssertionError("no unit sampling")), \
             mock.patch.object(self.b, "pg_sampling_lock", side_effect=AssertionError("no SQL sampling")):
            result = self.ops.resource_fences()
        self.assertEqual(set(result), {"v", "run", "owner", "checked_at", "resources"})
        self.assertEqual((result["v"], result["run"], result["owner"], result["checked_at"]),
                         (1, m.RUN, OWNER, self.b.clock))
        self.assertEqual(set(result["resources"]), set(m.NAMES))
        expected_commands = []
        for kind, name in m.NAMES.items():
            info = self.b.containers[name]
            self.assertEqual(result["resources"][kind], {
                "id": info["Id"], "running": True, "status": "running", "restart": "unless-stopped",
                "exit_code": 0, "oom_killed": False,
            })
            expected_commands.extend([("/usr/bin/docker", "container", "inspect", name),
                                      ("/usr/bin/docker", "image", "inspect", info["Image"])])
        self.assertEqual(self.b.events, expected_commands)
        self.assertEqual(self.b.snapshots, [])

    def test_resource_fences_preserves_stopped_and_faulted_state_fields(self):
        self.b.stopped("app")
        self.b.stopped("postgres")
        self.b.containers[m.NAMES["postgres"]]["State"].update(ExitCode=137, OOMKilled=True)
        result = self.ops.resource_fences()["resources"]
        self.assertFalse(result["app"]["running"])
        self.assertEqual(result["app"]["restart"], "no")
        self.assertEqual(result["app"]["exit_code"], 0)
        self.assertEqual(result["postgres"]["status"], "exited")
        self.assertEqual(result["postgres"]["exit_code"], 137)
        self.assertTrue(result["postgres"]["oom_killed"])
        self.assertTrue(result["redis"]["running"])

    def test_resource_fences_rejects_changed_identity_for_every_resource(self):
        for kind in m.NAMES:
            for field in ("container", "config", "rootfs", "architecture"):
                with self.subTest(kind=kind, field=field):
                    backend = FakeBackend()
                    operations = m.SourceOperations(backend)
                    info = backend.containers[m.NAMES[kind]]
                    image = backend.images[info["Image"]]
                    if field == "container":
                        info["Id"] = "0" * 64
                    elif field == "config":
                        info["Config"]["Cmd"] = ["unexpected"]
                    elif field == "rootfs":
                        image["RootFS"]["Layers"] = ["sha256:" + "0" * 64]
                    else:
                        image["Architecture"] = "arm64"
                    self.assert_refuses("CONTAINER_CHANGED" if field in ("container", "config") else "IMAGE_CHANGED",
                                        operations.resource_fences)
                    self.assertEqual(backend.snapshots, [])

    def test_resource_fences_revalidates_machine(self):
        self.b.machine = "b" * 64
        self.assert_refuses("MACHINE_CHANGED", self.ops.resource_fences)
        self.assertEqual(self.b.events, [])

    def test_manifest_rejects_unknown_fields(self):
        self.b.files["manifest"]["command"] = "arbitrary"
        self.assert_refuses("MANIFEST_FIELDS", lambda: m.SourceOperations(self.b))

    def test_manifest_cannot_inject_unit_or_container(self):
        self.b.files["manifest"]["units"]["evil.service"] = "a" * 64
        self.assert_refuses("MANIFEST_UNITS", lambda: m.SourceOperations(self.b))

    def test_machine_revalidated_each_action(self):
        self.b.machine = "b" * 64
        self.assert_refuses("MACHINE_CHANGED", lambda: self.ops.disable_app_restart(OP))
        self.assertFalse(self.signals())

    def test_config_digest_changed_refuses(self):
        self.b.containers[m.NAMES["app"]]["Config"]["Cmd"] = ["unexpected"]
        self.assert_refuses("CONTAINER_CHANGED", lambda: self.ops.disable_app_restart(OP))

    def test_container_id_changed_refuses(self):
        self.b.containers[m.NAMES["app"]]["Id"] = "0" * 64
        self.assert_refuses("CONTAINER_CHANGED", lambda: self.ops.disable_app_restart(OP))

    def test_rootfs_order_changed_refuses(self):
        image_id = self.b.containers[m.NAMES["app"]]["Image"]
        self.b.images[image_id]["RootFS"]["Layers"] = ["sha256:" + "a" * 64]
        self.assert_refuses("IMAGE_CHANGED", lambda: self.ops.disable_app_restart(OP))

    def test_containerd_manifest_config_identity(self):
        wanted = self.b.files["manifest"]["containers"]["app"]["image"]
        config = m.canonical({"rootfs": {"diff_ids": wanted["diff_ids"]}})
        config_digest = "sha256:" + m.digest(config)
        wanted["config_digest"] = config_digest
        raw_manifest = m.canonical({"config": {"digest": config_digest}})
        manifest_digest = "sha256:" + m.digest(raw_manifest)
        app = self.b.containers[m.NAMES["app"]]
        old = app["Image"]
        app["Image"] = manifest_digest
        self.b.images[manifest_digest] = self.b.images.pop(old)
        self.b.images[manifest_digest]["Id"] = manifest_digest
        self.b.contents = {manifest_digest: raw_manifest, config_digest: config}
        operations = m.SourceOperations(self.b)
        self.assertEqual(operations.disable_app_restart(OP)["phase"], "complete")

    def test_disable_restart_does_not_stop_app(self):
        self.assertEqual(self.ops.disable_app_restart(OP)["phase"], "complete")
        self.assertTrue(self.b.containers[m.NAMES["app"]]["State"]["Running"])
        self.assertFalse(self.signals())

    def test_disable_restart_lost_response_reconciles(self):
        self.b.fail_after = "update"
        self.assert_refuses("COMMAND_OUTCOME_UNKNOWN", lambda: self.ops.disable_app_restart(OP))
        self.assertEqual(self.ops.disable_app_restart(OP)["phase"], "complete")
        self.assertEqual(sum(event[:2] == ("/usr/bin/docker", "update") for event in self.b.events), 1)

    def test_app_route_fence_required(self):
        self.ops.disable_app_restart(OP)
        self.b.files["attestations"]["app_route_fenced"] = False
        self.assert_refuses("SOURCE_APP_ROUTE_NOT_FENCED", lambda: self.ops.app_stop(OP2))
        self.assertFalse(self.signals())

    def test_app_stale_attestation_refuses(self):
        self.ops.disable_app_restart(OP)
        self.b.clock += 4
        self.assert_refuses("ATTESTATION_STALE", lambda: self.ops.app_stop(OP2))

    def test_app_boot_changed_refuses(self):
        self.ops.disable_app_restart(OP)
        self.b.boot = "boot2"
        self.assert_refuses("ATTESTATION_IDENTITY", lambda: self.ops.app_stop(OP2))

    def test_app_streams_wait_naturally(self):
        self.ops.disable_app_restart(OP)
        self.b.sockets = b"ESTAB 0 0 172.18.0.2:8080 172.18.0.1:51112\nCLOSE-WAIT 0 0 172.18.0.2:8080 172.18.0.1:51113\n"
        result = self.ops.app_stop(OP2)
        self.assertEqual(result["phase"], "pending")
        self.assertEqual(result["connections"]["inbound_connections"], 2)
        self.assertFalse(self.signals())
        self.b.sockets = b""
        self.assertEqual(self.ops.app_stop(OP2)["phase"], "complete")
        self.assertEqual(len(self.signals()), 1)

    def test_outbound_database_pools_do_not_prevent_app_exit(self):
        self.ops.disable_app_restart(OP)
        self.b.sockets = b"ESTAB 0 0 172.18.0.2:51112 172.18.0.3:5432\nESTAB 0 0 172.18.0.2:51113 172.18.0.4:6379\n"
        self.assertEqual(self.ops.app_stop(OP2)["phase"], "complete")

    def test_app_signal_unknown_never_resent_even_new_id(self):
        self.ops.disable_app_restart(OP)
        self.b.fail_after = "signal"
        self.assert_refuses("COMMAND_OUTCOME_UNKNOWN", lambda: self.ops.app_stop(OP2))
        self.assertEqual(self.ops.app_stop(OP2)["phase"], "complete")
        self.assert_refuses("ACTION_ALREADY_HAS_OPERATION", lambda: self.ops.app_stop(OP3))
        self.assertEqual(len(self.signals()), 1)

    def test_pending_signal_never_escalates(self):
        self.ops.disable_app_restart(OP)
        self.b.signal_exits = False
        for _ in range(3):
            self.assertEqual(self.ops.app_stop(OP2)["phase"], "pending")
        self.assertEqual(len(self.signals()), 1)
        self.assertIn("--signal=SIGTERM", self.signals()[0])

    def test_unowned_exit_not_accepted(self):
        self.b.stopped("app")
        self.assert_refuses("APP_EXITED_WITHOUT_OWNED_STOP", lambda: self.ops.app_stop(OP))

    def test_pg_unknown_idle_client_and_prepared_xact_fail_quiet(self):
        self.b.pg["sessions"] = [{"pid": 300, "backend_type": "client backend", "state": "idle", "user": "sub2api", "address": "172.18.0.1", "application": ""}]
        sample = self.ops.pg_final_evidence()
        self.assertFalse(sample["quiet"])
        self.assertEqual(len(sample["unknown_sessions"]), 1)
        self.b.pg["sessions"] = []
        self.b.pg["prepared_transactions"] = 1
        self.assertFalse(self.ops.pg_final_evidence()["quiet"])

    def test_only_exact_physical_sender_is_known(self):
        p = self.b.files["manifest"]["postgres"]
        sender = {"pid": 1, "backend_type": "walsender", "user": p["replication_user"], "address": p["replication_address"], "application": p["replication_application"]}
        self.b.pg["sessions"] = [sender]
        self.assertTrue(self.ops.pg_final_evidence()["quiet"])
        sender["application"] = "foreign"
        self.assertFalse(self.ops.pg_final_evidence()["quiet"])

    def pg18_catalog(self):
        self.b = PG18CatalogBackend()
        self.ops = m.SourceOperations(self.b)
        expected = self.b.files["manifest"]["postgres"]
        sender = {"pid": 970644, "backend_type": "walsender", "user": expected["replication_user"],
                  "address": "172.18.0.1/32", "application": expected["replication_application"]}
        self.b.pg["sessions"] = [sender, *({"pid": pid, "backend_type": "io worker", "user": None,
                                          "address": None, "application": ""} for pid in (26, 27, 28))]
        self.b.pg["replication"] = [{**sender, "state": "streaming", "replay_lsn": "1/110"}]
        return sender

    def test_pg18_inet_host_and_io_workers_preserve_exact_sender_identity(self):
        self.pg18_catalog()
        sample = self.ops.pg_final_evidence()
        self.assertTrue(sample["quiet"])
        self.assertEqual(sample["unknown_sessions"], [])
        self.assertEqual(len(sample["owned_replication"]), 1)
        self.assertEqual(sample["owned_replication"][0]["address"], "172.18.0.1")
        self.assertEqual(sample["sessions"][0]["address"], "172.18.0.1")
        self.assertEqual(self.b.files["manifest"]["postgres"]["replication_address"], "172.18.0.1")
        self.assertEqual(self.b.pg["sessions"][0]["address"], "172.18.0.1/32")

    def test_pg18_io_worker_allowlist_does_not_hide_unknown_or_business_sessions(self):
        for backend_type, state in (("client backend", "active"), ("client backend", "idle"),
                                    ("io worker extension", None), ("unknown worker", None)):
            with self.subTest(backend_type=backend_type, state=state):
                self.pg18_catalog()
                unknown = {"pid": 9000, "backend_type": backend_type, "state": state,
                           "user": "sub2api", "address": "172.18.0.4/32", "application": ""}
                self.b.pg["sessions"].append(unknown)
                sample = self.ops.pg_final_evidence()
                self.assertFalse(sample["quiet"])
                self.assertEqual([(row["pid"], row["backend_type"]) for row in sample["unknown_sessions"]],
                                 [(9000, backend_type)])
                self.prepare_pg()
                self.assert_refuses("PG_NOT_QUIET", lambda: self.ops.pg_stop(OP2))
                self.assertFalse(self.signals())

    def test_pg18_host_projection_still_refuses_every_foreign_sender_identity(self):
        for field, value in (("address", "172.18.0.2/32"), ("user", "foreign_user"),
                             ("application", "foreign_application")):
            with self.subTest(field=field):
                sender = self.pg18_catalog()
                sender[field] = value
                self.b.pg["replication"][0][field] = value
                sample = self.ops.pg_final_evidence()
                self.assertFalse(sample["quiet"])
                self.assertEqual(sample["owned_replication"], [])
                self.assertEqual([row["pid"] for row in sample["unknown_sessions"]], [970644])
                self.prepare_pg()
                self.assert_refuses("PG_NOT_QUIET", lambda: self.ops.pg_stop(OP2))
                self.assertFalse(self.signals())

    def test_pg_role_or_systemid_changed_refuses(self):
        self.b.pg["in_recovery"] = True
        self.assert_refuses("PG_IDENTITY_OR_ROLE_CHANGED", self.ops.pg_final_evidence)

    def test_monitors_not_retired_without_fresh_guard(self):
        self.b.files["guard"]["pg_wal_protected"] = False
        self.assert_refuses("RECOVERY_GUARD_NOT_PROTECTING", lambda: self.ops.retire_pg_monitors(OP))
        self.assertFalse(self.b.masked)

    def test_guard_process_alone_insufficient(self):
        self.b.files["guard"]["checked_at"] -= 4
        self.assert_refuses("ATTESTATION_STALE", lambda: self.ops.retire_pg_monitors(OP))

    def test_monitor_inflight_job_waits_without_signal(self):
        self.b.units[m.PG_UNITS[1]].update(ActiveState="activating", MainPID="500", Job="12 /job/12")
        self.assertEqual(self.ops.retire_pg_monitors(OP)["phase"], "pending")
        self.assertFalse(self.b.masked)
        self.assertFalse(self.signals())
        self.b.units[m.PG_UNITS[1]].update(ActiveState="inactive", MainPID="0", Job="")
        self.assertEqual(self.ops.retire_pg_monitors(OP)["phase"], "complete")
        self.assertEqual(self.b.masked, set(m.PG_UNITS))
        self.assertEqual(self.ops.retire_pg_monitors(OP)["phase"], "complete")

    def test_failed_monitor_does_not_get_silently_retired(self):
        self.b.units[m.PG_UNITS[2]]["ActiveState"] = "failed"
        self.assert_refuses("SOURCE_MONITOR_OR_FENCE_FAILED", lambda: self.ops.retire_pg_monitors(OP))
        self.assertFalse(self.b.masked)

    def test_completed_monitor_retirement_revalidates_without_reload_or_writes(self):
        self.ops.retire_pg_monitors(OP)
        before = copy.deepcopy(self.b.files["ledger"])
        self.b.events.clear()
        with mock.patch.object(self.b, "write", side_effect=AssertionError("completed retirement must not write")), \
             mock.patch.object(self.b, "mask_unit", side_effect=AssertionError("completed retirement must not remask")):
            self.assertEqual(self.ops.retire_pg_monitors(OP)["phase"], "complete")
        self.assertFalse(any(event[:1] == ("/usr/bin/systemctl",) for event in self.b.events))
        self.assertEqual(self.b.files["ledger"], before)

    def test_completed_monitor_retirement_still_refuses_unmasked_or_live_units(self):
        self.ops.retire_pg_monitors(OP)
        self.b.masked.remove(m.PG_UNITS[1])
        self.assert_refuses("SOURCE_MONITOR_NOT_MASKED", lambda: self.ops.retire_pg_monitors(OP))
        self.b.masked.add(m.PG_UNITS[1])
        self.b.units[m.PG_UNITS[1]].update(ActiveState="active", MainPID="10")
        self.assert_refuses("SOURCE_MONITOR_NOT_IDLE_MASKED", lambda: self.ops.retire_pg_monitors(OP))

    def test_completed_monitor_retirement_still_requires_healthy_guard_and_steps(self):
        self.ops.retire_pg_monitors(OP)
        self.b.files["guard"]["healthy"] = False
        self.assert_refuses("RECOVERY_GUARD_NOT_PROTECTING", lambda: self.ops.retire_pg_monitors(OP))
        self.b.files["guard"]["healthy"] = True
        self.b.files["ledger"]["operations"][OP]["steps"].pop("stop_timer")
        self.assert_refuses("MONITOR_RETIREMENT_EVIDENCE_INVALID", lambda: self.ops.retire_pg_monitors(OP))

    def test_pg_stop_requires_retired_monitors(self):
        self.b.stopped("app")
        self.assert_refuses("SOURCE_MONITOR_NOT_MASKED", lambda: self.ops.pg_stop(OP))
        self.assertFalse(self.signals())

    def test_pg_stop_refuses_target_behind(self):
        self.prepare_pg()
        self.b.files["attestations"]["postgres"]["target_replay_lsn"] = "1/F0"
        self.assert_refuses("TARGET_BEHIND_FINAL_LSN", lambda: self.ops.pg_stop(OP2))
        self.assertFalse(self.signals())

    def test_pg_stop_refuses_unknown_sessions(self):
        self.prepare_pg()
        self.b.pg["sessions"] = [{"pid": 1, "backend_type": "client backend", "state": "idle"}]
        self.assert_refuses("PG_NOT_QUIET", lambda: self.ops.pg_stop(OP2))
        self.assertFalse(self.signals())

    def test_pg_stop_fast_signal_once_and_reconcile(self):
        self.prepare_pg()
        self.b.fail_after = "signal"
        self.assert_refuses("COMMAND_OUTCOME_UNKNOWN", lambda: self.ops.pg_stop(OP2))
        result = self.ops.pg_stop(OP2)
        self.assertEqual(result["authority"], "SOURCE_FENCED")
        self.assertEqual(result["final_lsn"], "1/100")
        self.assertEqual(len(self.signals()), 1)
        self.assertIn("--signal=SIGINT", self.signals()[0])

    def test_pg_pending_has_no_kill_or_restart(self):
        self.prepare_pg()
        self.b.signal_exits = False
        for _ in range(4):
            result = self.ops.pg_stop(OP2)
            self.assertEqual(result["phase"], "pending")
        self.assertEqual(len(self.signals()), 1)
        self.assertFalse(any("start" in event for event in self.b.events))

    def test_failover_budget_rejects_injection_or_unbounded(self):
        for value in (True, 0, 99, 6001, "2000 FORCE"):
            self.assert_refuses("INVALID_FAILOVER_BUDGET", lambda: self.ops.redis_failover(OP, value))
        self.assertFalse(self.failovers())

    def test_redis_pair_uses_eight_fixed_read_commands_and_existing_evidence_fields(self):
        result = self.ops.redis_roles()
        self.assertEqual(result, {"source": self.b.source_redis, "target": self.b.target_redis})
        cid = self.b.files["manifest"]["containers"]["redis"]["id"]
        image = self.b.containers[m.NAMES["redis"]]["Image"]
        resource = [("/usr/bin/docker", "container", "inspect", m.NAMES["redis"]),
                    ("/usr/bin/docker", "image", "inspect", image)]
        prefix = ("/usr/bin/docker", "exec", "-i", "-e", "REDISCLI_AUTH=", cid, "redis-cli", "--raw")
        self.assertEqual(self.b.events, resource + [
            prefix + ("INFO",), prefix + ("ROLE",),
            prefix + ("-h", "172.18.0.1", "-p", "26379", "INFO"),
            prefix + ("-h", "172.18.0.1", "-p", "26379", "ROLE"),
        ] + resource)
        self.assertEqual(self.b.snapshots, [])

    def test_redis_pair_rejects_restart_or_invalid_pid_during_batch(self):
        for field, value in (("Pid", 4321), ("StartedAt", "2026-10-17T00:00:00Z"), ("Running", False)):
            with self.subTest(field=field):
                backend = FakeBackend()
                operations = m.SourceOperations(backend)
                command = backend.command
                def changed(argv, data=None, timeout=2):
                    raw = command(argv, data, timeout)
                    if argv[-1] == "ROLE" and "-h" in argv:
                        backend.containers[m.NAMES["redis"]]["State"][field] = value
                    return raw
                backend.command = changed
                self.assert_refuses("RESOURCE_NOT_RUNNING" if field == "Running" else "REDIS_PROCESS_CHANGED_DURING_SAMPLE",
                                    operations.redis_roles)
        for field, value in (("Pid", 0), ("Pid", True), ("StartedAt", "")):
            with self.subTest(initial_field=field, value=value):
                backend = FakeBackend()
                operations = m.SourceOperations(backend)
                backend.containers[m.NAMES["redis"]]["State"][field] = value
                self.assert_refuses("INVALID_REDIS_PROCESS", operations.redis_roles)

    def test_redis_pair_rejects_changed_container_config_or_image_after_sampling(self):
        for field in ("container", "config", "image"):
            with self.subTest(field=field):
                backend = FakeBackend()
                operations = m.SourceOperations(backend)
                command = backend.command
                def changed(argv, data=None, timeout=2):
                    raw = command(argv, data, timeout)
                    if argv[-1] == "ROLE" and "-h" in argv:
                        info = backend.containers[m.NAMES["redis"]]
                        if field == "container":
                            info["Id"] = "0" * 64
                        elif field == "config":
                            info["Config"]["Cmd"] = ["unexpected"]
                        else:
                            backend.images[info["Image"]]["RootFS"]["Layers"] = []
                    return raw
                backend.command = changed
                self.assert_refuses("IMAGE_CHANGED" if field == "image" else "CONTAINER_CHANGED", operations.redis_roles)

    def test_redis_pair_preserves_role_and_run_id_checks_both_peers(self):
        for target in (False, True):
            for field in ("role", "run_id"):
                with self.subTest(target=target, field=field):
                    backend = FakeBackend()
                    operations = m.SourceOperations(backend)
                    command = backend.command
                    if field == "run_id":
                        (backend.target_redis if target else backend.source_redis)["run_id"] = "e" * 40
                    def changed(argv, data=None, timeout=2):
                        if field == "role" and argv[-1] == "ROLE" and ("-h" in argv) is target:
                            return b"slave\n" if not target else b"master\n"
                        return command(argv, data, timeout)
                    backend.command = changed
                    self.assert_refuses("REDIS_ROLE_UNKNOWN" if field == "role" else "REDIS_PROCESS_IDENTITY_CHANGED",
                                        operations.redis_roles)

    def test_redis_pair_requires_both_info_sections_without_duplicate_fields(self):
        for raw, code in ((b"# Server\r\nrun_id:abc\r\n", "REDIS_INFO_SECTIONS_MISSING"),
                          (b"# Server\r\nrun_id:abc\r\n# Replication\r\nrole:master\r\nrole:slave\r\n", "DUPLICATE_REDIS_INFO")):
            with self.subTest(code=code):
                command = self.b.command
                def changed(argv, data=None, timeout=2):
                    if argv[-1] == "INFO":
                        return raw
                    return command(argv, data, timeout)
                with mock.patch.object(self.b, "command", side_effect=changed):
                    self.assert_refuses(code, self.ops.redis_roles)

    def test_failover_requires_physically_closed_business_clients_both_ends(self):
        self.prepare_redis()
        self.b.target_clients.append({"id": "44", "addr": "127.0.0.1:44500", "flags": "N", "name": ""})
        self.assert_refuses("REDIS_UNKNOWN_CLIENTS", lambda: self.ops.redis_failover(OP, 1500))
        self.assertFalse(self.failovers())
        self.b.target_clients.pop()
        self.b.clients.append({"id": "45", "addr": "172.18.0.1:55555", "flags": "N", "name": ""})
        self.assert_refuses("REDIS_UNKNOWN_CLIENTS", lambda: self.ops.redis_failover(OP, 1500))
        self.assertFalse(self.failovers())

    def test_failover_identity_mismatch_refuses(self):
        self.prepare_redis()
        self.b.target_redis["run_id"] = "d" * 40
        self.assert_refuses("REDIS_PROCESS_IDENTITY_CHANGED", lambda: self.ops.redis_failover(OP, 1500))

    def test_failover_requires_advertised_reverse_route(self):
        self.prepare_redis()
        self.b.source_redis["slave0"] = "ip=172.18.0.1,port=12345,state=online,offset=10,lag=0"
        self.assert_refuses("REDIS_TARGET_NOT_UNIQUE_ONLINE_REPLICA", lambda: self.ops.redis_failover(OP, 1500))

    def test_failover_no_force_and_lost_reply_reconciles_without_repeat(self):
        self.prepare_redis()
        self.b.fail_after = "failover"
        self.assert_refuses("COMMAND_OUTCOME_UNKNOWN", lambda: self.ops.redis_failover(OP, 1500))
        result = self.ops.redis_failover(OP, 1500)
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["authority"], "TENCENT")
        self.assertEqual(len(self.failovers()), 1)
        self.assertNotIn("FORCE", self.failovers()[0])
        self.assertEqual(self.b.containers[m.NAMES["redis"]]["HostConfig"]["RestartPolicy"]["Name"], "no")

    def test_failover_same_id_changed_budget_refused(self):
        self.prepare_redis()
        self.ops.redis_failover(OP, 1500)
        self.assert_refuses("OPERATION_ID_REUSED", lambda: self.ops.redis_failover(OP, 2000))

    def test_failover_unclear_roles_stay_unknown(self):
        self.prepare_redis()
        self.ops.redis_failover(OP, 1500)
        self.b.source_redis["role"] = "master"
        self.b.target_redis["role"] = "slave"
        self.assertEqual(self.ops.redis_failover(OP, 1500)["phase"], "unknown")
        self.assertEqual(len(self.failovers()), 1)

    def test_retired_source_redis_stays_stopped_without_auto_restart(self):
        self.prepare_redis()
        self.ops.redis_failover(OP, 1500)
        self.ops.redis_failover(OP, 1500)
        result = self.ops.redis_role_persist(OP2)
        self.assertEqual(result["source_role"], "RETIRED_STOPPED")
        self.assertEqual(self.ops.redis_role_persist(OP2)["phase"], "complete")
        self.assertEqual(len(self.signals()), 1)
        self.assertFalse(any("start" in event for event in self.b.events))

    def test_redis_retirement_unknown_signal_reconciles(self):
        self.prepare_redis()
        self.ops.redis_failover(OP, 1500)
        self.ops.redis_failover(OP, 1500)
        self.b.fail_after = "signal"
        self.assert_refuses("COMMAND_OUTCOME_UNKNOWN", lambda: self.ops.redis_role_persist(OP2))
        self.assertEqual(self.ops.redis_role_persist(OP2)["phase"], "complete")
        self.assertEqual(len(self.signals()), 1)

    def test_failover_rejects_reused_probe_client_name(self):
        self.prepare_redis()
        self.b.clients.append({"id": "55", "addr": "172.18.0.1:55555", "flags": "N", "name": m.SELF_CLIENT})
        self.assert_refuses("REDIS_SELF_PROBE_AMBIGUOUS", lambda: self.ops.redis_failover(OP, 1500))
        self.assertFalse(self.failovers())

    def test_failover_source_process_restart_cannot_be_adopted(self):
        self.prepare_redis()
        self.b.source_redis["run_id"] = "e" * 40
        self.assert_refuses("REDIS_PROCESS_IDENTITY_CHANGED", lambda: self.ops.redis_failover(OP, 1500))

    def test_faulted_exit_cannot_fence_postgres(self):
        self.prepare_pg()
        self.b.fail_after = "signal"
        self.assert_refuses("COMMAND_OUTCOME_UNKNOWN", lambda: self.ops.pg_stop(OP2))
        self.b.containers[m.NAMES["postgres"]]["State"].update(ExitCode=137, OOMKilled=True)
        self.assert_refuses("RESOURCE_NOT_CLEANLY_STOPPED", lambda: self.ops.pg_stop(OP2))

    def test_actual_mutations_use_full_container_ids(self):
        self.ops.disable_app_restart(OP)
        self.ops.app_stop(OP2)
        mutations = [event for event in self.b.events if event[:2] in (("/usr/bin/docker", "kill"), ("/usr/bin/docker", "update"))]
        self.assertTrue(mutations)
        self.assertTrue(all(event[-1] == self.b.files["manifest"]["containers"]["app"]["id"] for event in mutations))

    def test_unit_override_rejected_by_os_adapter(self):
        fields = {"ActiveState": "active", "SubState": "running", "MainPID": "1", "Job": "", "UnitFileState": "enabled",
                  "LoadState": "loaded", "DropInPaths": "/etc/systemd/system/evil.conf", "FragmentPath": "/etc/systemd/system/" + m.GUARD_UNIT}
        raw = "\n".join(key + "=" + value for key, value in fields.items()).encode()
        with mock.patch.object(m.LocalBackend, "command", return_value=raw):
            self.assert_refuses("UNIT_OVERRIDE_OR_FRAGMENT_CHANGED", lambda: m.LocalBackend().unit_status(m.GUARD_UNIT))

    def test_fixed_monitor_timer_without_mainpid_uses_zero(self):
        raw = (
            b"LoadState=loaded\n"
            b"ActiveState=active\n"
            b"SubState=waiting\n"
            b"FragmentPath=/etc/systemd/system/sub2api-pg-replica-20261016-source-monitor.timer\n"
            b"DropInPaths=\n"
            b"UnitFileState=enabled\n"
            b"Job=\n"
        )
        with mock.patch.object(m.LocalBackend, "command", return_value=raw) as command:
            status = m.LocalBackend().unit_status(m.PG_UNITS[0])
        self.assertEqual(status["MainPID"], "0")
        self.assertEqual(status["ActiveState"], "active")
        self.assertEqual(status["SubState"], "waiting")
        command.assert_called_once_with([
            "/usr/bin/systemctl", "show", m.PG_UNITS[0],
            "--property=ActiveState,SubState,MainPID,Job,UnitFileState,LoadState,DropInPaths,FragmentPath",
        ])

    def test_services_still_require_reported_mainpid(self):
        for unit in (*m.PG_UNITS[1:], m.GUARD_UNIT):
            with self.subTest(unit=unit):
                raw = (
                    "LoadState=loaded\nActiveState=active\nSubState=running\n"
                    "FragmentPath=/etc/systemd/system/" + unit + "\n"
                    "DropInPaths=\nUnitFileState=enabled\nJob=\n"
                ).encode()
                with mock.patch.object(m.LocalBackend, "command", return_value=raw):
                    self.assert_refuses("INVALID_UNIT_STATE", lambda: m.LocalBackend().unit_status(unit))

    def test_timer_duplicate_fields_still_refused(self):
        raw = (
            "LoadState=loaded\nActiveState=active\nSubState=waiting\n"
            "FragmentPath=/etc/systemd/system/" + m.PG_UNITS[0] + "\n"
            "DropInPaths=\nUnitFileState=enabled\nJob=\nJob=\n"
        ).encode()
        with mock.patch.object(m.LocalBackend, "command", return_value=raw):
            self.assert_refuses("INVALID_UNIT_STATE", lambda: m.LocalBackend().unit_status(m.PG_UNITS[0]))

    def test_timer_still_requires_other_status_fields(self):
        raw = (
            "LoadState=loaded\nActiveState=active\nSubState=waiting\n"
            "FragmentPath=/etc/systemd/system/" + m.PG_UNITS[0] + "\n"
            "DropInPaths=\nUnitFileState=enabled\n"
        ).encode()
        with mock.patch.object(m.LocalBackend, "command", return_value=raw):
            self.assert_refuses("INVALID_UNIT_STATE", lambda: m.LocalBackend().unit_status(m.PG_UNITS[0]))

    def test_unknown_units_never_reach_systemctl(self):
        with mock.patch.object(m.LocalBackend, "command") as command:
            self.assert_refuses("UNKNOWN_UNIT", lambda: m.LocalBackend().unit_status("docker.service"))
        command.assert_not_called()

    def test_failover_replay_after_owned_source_retirement_uses_no_stopped_exec(self):
        self.prepare_redis()
        self.ops.redis_failover(OP, 1500)
        self.ops.redis_failover(OP, 1500)
        self.ops.redis_role_persist(OP2)
        before = len(self.b.events)
        result = self.ops.redis_failover(OP, 1500)
        self.assertEqual(result["phase"], "complete")
        self.assertTrue(result["source_retired"])
        self.assertTrue(result["target_verification_required"])
        self.assertFalse(any("redis-cli" in event for event in self.b.events[before:]))
        self.assertEqual(len(self.failovers()), 1)

    def test_failover_replay_reconciles_lost_retirement_signal_reply(self):
        self.prepare_redis()
        self.ops.redis_failover(OP, 1500)
        self.ops.redis_failover(OP, 1500)
        self.b.fail_after = "signal"
        self.assert_refuses("COMMAND_OUTCOME_UNKNOWN", lambda: self.ops.redis_role_persist(OP2))
        self.assertEqual(self.ops.redis_failover(OP, 1500)["phase"], "complete")
        self.assertEqual(self.b.files["ledger"]["operations"][OP2]["source_role"], "RETIRED_STOPPED")
        self.assertEqual(len(self.signals()), 1)

    def test_failover_replay_rejects_foreign_stopped_source(self):
        self.prepare_redis()
        self.ops.redis_failover(OP, 1500)
        self.ops.redis_failover(OP, 1500)
        self.b.stopped("redis")
        self.assert_refuses("REDIS_RETIREMENT_NOT_OWNED", lambda: self.ops.redis_failover(OP, 1500))

    def test_operation_id_cannot_change_action(self):
        self.ops.disable_app_restart(OP)
        self.assert_refuses("OPERATION_ID_REUSED", lambda: self.ops.app_stop(OP))

    def test_inspection_reports_without_runtime_configuration_secrets(self):
        result = self.ops.inspect()
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertTrue(result["postgres"]["quiet"])
        self.assertEqual(result["app_connections"]["inbound_connections"], 0)
        self.assertFalse(self.signals())
        self.assertFalse(self.failovers())
        self.assertIsNone(self.b.files["ledger"])

    def test_strict_json_duplicate_nonfinite_and_injection(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'{} trailing', b'\xff'):
            with self.assertRaises(m.Refusal):
                m.strict_json(raw)

    def test_socket_sample_counts_all_live_inbound_states(self):
        raw = b"LISTEN 0 100 *:8080 *:*\nESTAB 0 0 [::1]:8080 [::1]:33000\nCLOSE-WAIT 0 0 1.1.1.1:8080 2.2.2.2:40000\nTIME-WAIT 0 0 1.1.1.1:8080 2.2.2.2:50000\n"
        self.assertEqual(m.parse_sockets(raw)["inbound_connections"], 2)

    def test_adapter_subprocess_timeout_does_not_expose_output(self):
        backend = m.LocalBackend()
        with mock.patch.object(m.subprocess, "run", side_effect=subprocess.TimeoutExpired(["secret"], 2, output=b"token=secret")):
            self.assert_refuses("COMMAND_OUTCOME_UNKNOWN", lambda: backend.command(["/usr/bin/docker", "inspect"]))


if __name__ == "__main__":
    unittest.main()
