#!/usr/bin/env python3
"""Offline keeper fault tests. No SSH, Docker, sockets, or host unit actions."""
import copy
import contextlib
import importlib.util
import io
import os
import pathlib
import stat
import tempfile
import types
import unittest
from unittest import mock


def load(name):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


K, R = load("target_keeper"), load("target_rpc")


def config():
    common = {"container_id": "c" * 64, "image_id": "sha256:" + "d" * 64,
              "config_sha256": "e" * 64, "rootfs_diffids": ["sha256:" + "f" * 64]}
    return {"v": 1, "run": K.RUN, "owner": "a" * 32, "program_sha256": "1" * 64,
            "machine_id_sha256": "2" * 64, "app": {**common, "control_port": 29001},
            "pg": {**common, "container_id": "b" * 64, "owner": "b" * 32,
                   "volume_path": "/var/lib/docker/volumes/fixed-pg/_data", "system_identifier": "12345", "source_timeline": 1},
            "redis": {**common, "container_id": "a" * 64, "owner": "b" * 32,
                      "volume_path": "/var/lib/docker/volumes/fixed-redis/_data"}}


def arm(cfg):
    source = {"id": "f" * 64, "running": False, "status": "exited", "restart": "no", "exit_code": 0, "oom_killed": False}
    return {"v": 1, "run": K.RUN, "owner": cfg["owner"], "machine_id_sha256": cfg["machine_id_sha256"],
            "rpc_config_sha256": R.sha(cfg), "program_sha256": "3" * 64, "armed_at": 1000,
            "pg_intent_sha256": "4" * 64, "redis_persist_op_id": "5" * 32, "redis_normalized_config_sha256": "6" * 64,
            "complete": {"phase": "COMPLETE", "pg": "TENCENT", "redis": "TENCENT", "window": None,
                         "verified_at": 999, "state_sha256": "7" * 64, "verification_sha256": "8" * 64,
                         "source_machine_id_sha256": "9" * 64, "source_inspect_sha256": "a" * 64,
                         "source_tombstones_sha256": "b" * 64,
                         "source_resources": {key: {**copy.deepcopy(source), "id": str(index) * 64}
                                              for index, key in enumerate(("app", "postgres", "redis"), 1)}}}


class MemoryStore:
    def __init__(self, cfg):
        self.cfg, self.proof, self.value = cfg, arm(cfg), None
        self.fail_save = False

    def arm(self, program_sha):
        return K.validate_arm(copy.deepcopy(self.proof), self.cfg, R, program_sha)

    def journal(self, proof):
        if self.value is None:
            return {"v": 1, "run": K.RUN, "owner": self.cfg["owner"], "arm_sha256": R.sha(proof), "starts": {}, "activations": {}}
        return copy.deepcopy(self.value)

    def save(self, value):
        if self.fail_save:
            raise OSError("disk failure")
        self.value = copy.deepcopy(value)


class FakeBackend:
    def __init__(self, cfg):
        self.cfg, self.events = cfg, []
        self.resources = {kind: {"Id": cfg[kind]["container_id"], "State": {"Running": True,
            "StartedAt": "start1", "FinishedAt": "", "Pid": 50}} for kind in ("pg", "redis", "app")}
        self.instance, self.boot = "instance1", "boot1"
        self.ready, self.pg_primary, self.redis_master = True, True, True
        self.activated, self.activating, self.failures = True, False, {}
        self.activation_posts = 0
        self.start_lost, self.start_effect = False, True
        self.post_lost, self.post_effect = False, True
        self.entry_active, self.entry_starts = True, 0
        self.persistent_failure = False
        self.route_failure = None

    def boot_id(self):
        return self.boot

    def inspect(self, kind):
        self.events.append("inspect:" + kind)
        return copy.deepcopy(self.resources[kind])

    def persistent(self, proof):
        self.events.append("persistent")
        K.need(not self.persistent_failure, "AUTHORITY_NOT_COMPLETE")

    def prestart(self, kind, proof):
        self.events.append("prestart:" + kind)

    def primary(self, proof):
        self.events.append("primary")
        K.need(self.pg_primary, "PG_NOT_PRIMARY")

    def master(self, proof):
        self.events.append("master")
        K.need(self.redis_master, "REDIS_NOT_MASTER")

    def start(self, kind):
        self.events.append("start:" + kind)
        if self.start_effect:
            item = self.resources[kind]["State"]
            item.update(Running=True, StartedAt=item["StartedAt"] + "new", Pid=51)
        if self.start_lost:
            raise TimeoutError()

    def ensure_route(self):
        self.events.append("ensure:route")
        if self.route_failure:
            raise self.route_failure
        return {"ok": True, "status": "present", "inserted": False}

    def control(self, name, epoch=0):
        self.events.append(name)
        if name == "runtime.status":
            return {"instance_id": self.instance, "ready": self.ready, "serving": True,
                    "draining": False, "background_workers_enabled": self.activated}
        if name == "postgres.status":
            return {"endpoint": "target", "paused": False}
        if name == "redis.status":
            return {"address": "127.0.0.1:26379", "paused": False, "pubsub_cache_ready": True}
        if name == "writers.status":
            return {"held": False}
        if name == "ingress.status":
            return {"paused": False}
        if name == "background.activate":
            self.activation_posts += 1
            if self.post_effect:
                self.activated = True
            if self.post_lost:
                raise TimeoutError()
        return {"epoch": 0, "activated": self.activated, "activating": self.activating,
                "pending": [] if self.activated else ["scheduled"], "started": ["scheduled"] if self.activated else [],
                "failures": self.failures}

    def entry(self):
        self.events.append("entry")
        return {"ActiveState": "active" if self.entry_active else "inactive",
                "SubState": "running" if self.entry_active else "dead", "MainPID": "77" if self.entry_active else "0"}

    def start_entry(self):
        self.events.append("start:entry")
        self.entry_starts += 1
        self.entry_active = True


class KeeperTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.b, self.s = FakeBackend(self.cfg), MemoryStore(self.cfg)
        self.k = K.Keeper(R, self.cfg, self.b, self.s, "3" * 64)

    def stopped(self, *kinds):
        for kind in kinds:
            self.b.resources[kind]["State"].update(Running=False, Pid=0, FinishedAt="finish1")

    def test_default_entirely_offline(self):
        with mock.patch.object(K.importlib.util, "spec_from_file_location", side_effect=AssertionError("no import")), \
                mock.patch.object(K.pathlib.Path, "read_text", side_effect=AssertionError("no read")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(K.main([]), 0)
            self.assertIn('"production_actions": false', out.getvalue())

    def test_adopts_every_running_service_without_post_or_start(self):
        self.assertTrue(self.k.step()["ready"])
        self.k.step()
        self.assertFalse(any(item.startswith("start:") for item in self.b.events))
        self.assertEqual(self.b.activation_posts, 0)
        self.assertEqual(self.b.events.count("ensure:route"), 2)

    def test_cold_start_checks_route_before_components_and_rechecks_before_entry(self):
        self.stopped("pg", "redis", "app")
        self.b.activated = self.b.entry_active = False
        self.assertTrue(self.k.step()["ready"])
        order = [item for item in self.b.events if item.startswith("start:") or item in ("background.activate", "ensure:route")]
        self.assertEqual(order, ["ensure:route", "start:pg", "start:redis", "start:app", "background.activate",
                                 "ensure:route", "start:entry"])

    def test_route_refusal_prevents_all_starts_and_activation(self):
        self.stopped("pg", "redis", "app")
        self.b.entry_active = self.b.activated = False
        self.b.route_failure = K.Refusal("ROUTE_OUTCOME_UNKNOWN_INSPECT_ONLY")
        for _ in range(2):
            with self.assertRaisesRegex(K.Refusal, "ROUTE_OUTCOME_UNKNOWN_INSPECT_ONLY"):
                self.k.step()
        self.assertEqual(self.b.events, ["persistent", "ensure:route"] * 2)
        self.assertEqual(self.b.activation_posts, 0)
        self.assertEqual(self.k.state["starts"], {})

    def test_route_refusal_on_fresh_entry_recheck_does_not_consume_start_intent(self):
        self.b.entry_active = False
        with mock.patch.object(self.b, "ensure_route", side_effect=[None, K.Refusal("OWNED_ROUTE_CONFLICT")]):
            with self.assertRaisesRegex(K.Refusal, "OWNED_ROUTE_CONFLICT"):
                self.k.step()
        self.assertEqual(self.b.entry_starts, 0)
        self.assertEqual(self.k.state["starts"], {})

    def test_invalid_authority_never_reaches_route_mutation(self):
        self.b.persistent_failure = True
        with self.assertRaisesRegex(K.Refusal, "AUTHORITY_NOT_COMPLETE"):
            self.k.step()
        self.assertEqual(self.b.events, ["persistent"])

    def test_running_unhealthy_is_never_restarted_or_signalled(self):
        self.b.ready = False
        with self.assertRaisesRegex(K.Refusal, "APP_NOT_READY"):
            self.k.step()
        self.assertFalse(any(item.startswith("start:") for item in self.b.events))
        self.assertEqual(self.b.activation_posts, 0)

    def test_unarmed_or_incomplete_source_never_mutates(self):
        for field, value in (("running", True), ("restart", "always"), ("exit_code", 1), ("oom_killed", True)):
            with self.subTest(field=field):
                self.s.proof = arm(self.cfg)
                self.s.proof["complete"]["source_resources"]["app"][field] = value
                with self.assertRaises(K.Refusal):
                    self.k.step()
        self.assertEqual(self.b.events, [])

    def test_requires_complete_phase_target_authority_and_closed_window(self):
        for field, value in (("phase", "OLD_APP_STOPPED"), ("pg", "AWS"), ("redis", "AWS"), ("window", {})):
            proof = arm(self.cfg)
            proof["complete"][field] = value
            with self.subTest(field=field), self.assertRaises(K.Refusal):
                K.validate_arm(proof, self.cfg, R, "3" * 64)

    def test_repeated_source_resource_id_is_not_three_retirement_proofs(self):
        self.s.proof["complete"]["source_resources"]["redis"]["id"] = self.s.proof["complete"]["source_resources"]["app"]["id"]
        with self.assertRaisesRegex(K.Refusal, "NOT_DISTINCT"):
            self.k.step()
        self.assertEqual(self.b.events, [])

    def test_proof_identity_and_transfer_freshness(self):
        for field, value in (("owner", "b" * 32), ("machine_id_sha256", "f" * 64),
                             ("rpc_config_sha256", "f" * 64), ("program_sha256", "f" * 64), ("armed_at", 2000)):
            proof = arm(self.cfg)
            proof[field] = value
            with self.subTest(field=field), self.assertRaises(K.Refusal):
                K.validate_arm(proof, self.cfg, R, "3" * 64)

    def test_authority_regression_never_starts_next_component(self):
        self.stopped("redis", "app")
        self.b.pg_primary = False
        with self.assertRaisesRegex(K.Refusal, "PG_NOT_PRIMARY"):
            self.k.step()
        self.assertFalse(any(item.startswith("start:") for item in self.b.events))

    def test_redis_not_master_prevents_application_start(self):
        self.stopped("app")
        self.b.redis_master = False
        with self.assertRaisesRegex(K.Refusal, "REDIS_NOT_MASTER"):
            self.k.step()
        self.assertNotIn("start:app", self.b.events)

    def test_start_intent_fsync_failure_prevents_start(self):
        self.stopped("pg")
        self.s.fail_save = True
        with self.assertRaises(OSError):
            self.k.step()
        self.assertNotIn("start:pg", self.b.events)

    def test_start_lost_receipt_inspected_without_duplicate(self):
        self.stopped("pg")
        self.b.start_lost = True
        self.k.step()
        self.k.step()
        self.assertEqual(self.b.events.count("start:pg"), 1)

    def test_start_unknown_without_effect_is_never_repeated(self):
        self.stopped("pg")
        self.b.start_effect, self.b.start_lost = False, True
        for _ in range(2):
            with self.assertRaisesRegex(K.Refusal, "START_EFFECT"):
                self.k.step()
        self.assertEqual(self.b.events.count("start:pg"), 1)

    def test_activation_lost_receipt_reconciles_from_fresh_status(self):
        self.b.activated, self.b.post_lost = False, True
        self.k.step()
        self.k.step()
        self.assertEqual(self.b.activation_posts, 1)
        self.assertTrue(all(row["phase"] == "COMPLETE" for row in self.s.value["activations"].values()))

    def test_activation_ambiguous_never_repeated_same_instance(self):
        self.b.activated = self.b.post_effect = False
        self.b.post_lost = True
        for _ in range(2):
            with self.assertRaisesRegex(K.Refusal, "BACKGROUND_EFFECT"):
                self.k.step()
        self.assertEqual(self.b.activation_posts, 1)
        self.b.activated = True
        self.k.step()
        self.assertEqual(self.b.activation_posts, 1)

    def test_fresh_process_activates_once_despite_old_completed_generation(self):
        self.k.step()
        self.b.instance, self.b.activated = "instance2", False
        self.b.resources["app"]["State"]["StartedAt"] = "start2"
        self.k.step()
        self.k.step()
        self.assertEqual(self.b.activation_posts, 1)
        self.assertEqual(len(self.s.value["activations"]), 2)

    def test_activation_fsync_failure_prevents_post(self):
        self.b.activated, self.s.fail_save = False, True
        with self.assertRaises(OSError):
            self.k.step()
        self.assertEqual(self.b.activation_posts, 0)

    def test_partial_background_failure_is_not_retried(self):
        self.b.activated, self.b.failures = True, {"one": "failed"}
        with self.assertRaisesRegex(K.Refusal, "BACKGROUND_FAILED"):
            self.k.step()
        self.assertEqual(self.b.activation_posts, 0)

    def test_keeper_stop_has_no_service_stop_command(self):
        unit = K.unit()
        self.assertIn("KillMode=process", unit)
        self.assertNotIn("ExecStop", unit)

    def test_noninstalled_run_refused_before_import_or_host_action(self):
        with mock.patch.object(K.importlib.util, "spec_from_file_location", side_effect=AssertionError("no import")):
            with self.assertRaisesRegex(K.Refusal, "INSTALLED_ROOT_RUN_REQUIRED"):
                K.main(["run"])


class RouteProgramTests(unittest.TestCase):
    RAW = b"Refusal = injected_refusal\ndef load_route():\n    assert __file__ == injected_path\n    return injected_route\n"

    @contextlib.contextmanager
    def installed_route(self, route, refusal=K.Refusal):
        original = importlib.util.module_from_spec

        def module(spec):
            value = original(spec)
            value.injected_refusal = refusal
            value.injected_path = str(K.ROUTE_PROGRAM)
            value.injected_route = route
            return value

        with mock.patch.object(R, "private_read", return_value=self.RAW) as reader, \
                mock.patch.object(K, "ROUTE_SHA", R.sha(self.RAW)), \
                mock.patch.object(K.importlib.util, "module_from_spec", side_effect=module):
            yield reader

    def test_frozen_route_pin_matches_reviewed_program(self):
        raw = pathlib.Path(__file__).with_name("rolling_app_route.py").read_bytes()
        self.assertEqual(R.sha(raw), K.ROUTE_SHA)

    def test_trusted_pinned_bytes_execute_once_and_preserve_installed_file_path(self):
        ensure = mock.Mock(return_value={"ok": True, "status": "present", "inserted": False})
        backend = K.backend_class(R)(config())
        with self.installed_route(types.SimpleNamespace(ensure=ensure)) as reader:
            self.assertEqual(backend.ensure_route()["status"], "present")
        reader.assert_called_once_with(K.ROUTE_PROGRAM, mode=0o700, limit=2 * 1024 * 1024)
        ensure.assert_called_once_with()

    def test_changed_or_untrusted_route_is_refused_before_module_execution(self):
        backend = K.backend_class(R)(config())
        for kwargs, exception in (({"return_value": b"changed"}, K.Refusal),
                                  ({"side_effect": R.Refusal("UNTRUSTED_FILE")}, R.Refusal)):
            with self.subTest(kwargs=kwargs), mock.patch.object(R, "private_read", **kwargs), \
                    mock.patch.object(K.importlib.util, "spec_from_file_location") as spec, \
                    self.assertRaises(exception):
                backend.ensure_route()
            spec.assert_not_called()

    def test_route_missing_or_false_success_never_accepts_entry_prerequisite(self):
        backend = K.backend_class(R)(config())
        for result in ({"ok": False, "status": "present"}, {"ok": True, "status": "absent"}, None):
            route = types.SimpleNamespace(ensure=lambda: result)
            with self.subTest(result=result), self.installed_route(route), \
                    self.assertRaisesRegex(K.Refusal, "ROUTE_NOT_PROVEN_PRESENT"):
                backend.ensure_route()

    def test_real_route_unknown_is_inspected_without_duplicate_insert_or_entry_start(self):
        route_module, core = load("rolling_app_route"), load("rolling_app_recovery")
        state, nat, inserts = {}, {"output": [], "other": []}, []

        def insert():
            inserts.append("attempt")
            raise TimeoutError("lost insertion reply")

        store = types.SimpleNamespace(boot_id="boot-one", lock=contextlib.nullcontext,
            read=lambda: copy.deepcopy(state.get("intent")),
            write=lambda value: state.update(intent=copy.deepcopy(value)))
        backend = types.SimpleNamespace(verify=lambda: None, nat=lambda: copy.deepcopy(nat), insert=insert)
        route = route_module.Route(backend, store, core.REDIRECT, "a" * 32, {"fixed": "binding"})
        cfg = config()
        b = FakeBackend(cfg)
        b.entry_active = False
        b.ensure_route = K.backend_class(R)(cfg).ensure_route
        keeper = K.Keeper(R, cfg, b, MemoryStore(cfg), "3" * 64)
        with self.installed_route(route, route_module.Refusal):
            with self.assertRaisesRegex(K.Refusal, "ROUTE_OUTCOME_UNKNOWN_INSPECT_ONLY"):
                keeper.step()
            with self.assertRaisesRegex(K.Refusal, "SAME_BOOT_ROUTE_MISSING_INSPECT_ONLY"):
                keeper.step()
        self.assertEqual(inserts, ["attempt"])
        self.assertEqual(state["intent"]["status"], "UNKNOWN")
        self.assertEqual(keeper.state["starts"], {})
        self.assertEqual(b.entry_starts, 0)


class DataAndIdentityTests(unittest.TestCase):
    def test_redis_module_normalization_only(self):
        raw = b'bind 127.0.0.1\nport 26379\nappendonly no\nlogfile ""\n'
        normalized = K.normalized_redis(raw, R.sha)
        self.assertEqual(K.normalized_redis(raw + b"loadmodule /usr/local/lib/redis/modules/redisjson.so\n", R.sha), normalized)
        self.assertNotEqual(K.normalized_redis(raw.replace(b"26379", b"6379"), R.sha), normalized)
        for addition in (b"replicaof 127.0.0.1 16379", b"SLAVEOF old 6379", b"include /tmp/other",
                         b"loadmodule /tmp/evil.so", b"loadmodule /usr/local/lib/redis/modules/one.so argument"):
            with self.subTest(addition=addition), self.assertRaises(K.Refusal):
                K.normalized_redis(raw + addition, R.sha)

    def test_real_store_atomic_permissions_and_arm_change_refusal(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent) as directory:
            scope = pathlib.Path(directory)
            scope.chmod(0o700)
            cfg = config()
            store = K.Store(R, cfg, scope, os.getuid())
            R.atomic_json(scope / "arm.json", arm(cfg), os.getuid())
            proof = store.arm("3" * 64)
            state = store.journal(proof)
            store.save(state)
            self.assertEqual((scope / "state.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.journal(proof), state)
            changed = copy.deepcopy(proof)
            changed["armed_at"] += 1
            with self.assertRaisesRegex(K.Refusal, "KEEPER_STATE_CHANGED"):
                store.journal(changed)
            (scope / "arm.json").chmod(0o644)
            with self.assertRaises(R.Refusal):
                store.arm("3" * 64)

    def inspector(self):
        cfg = config()
        actual = {"Id": cfg["pg"]["container_id"], "Image": cfg["pg"]["image_id"],
                  "Config": {"Labels": {R.OWNER_LABEL: cfg["pg"]["owner"],
                                        "io.sub2api.migration.run": "sub2api-pg-replica-20261016"}},
                  "HostConfig": {"NetworkMode": "host", "PortBindings": {}, "RestartPolicy": {"Name": "no"}},
                  "State": {"Running": False, "Status": "exited", "Pid": 0},
                  "Mounts": [{"Destination": R.PG_DATA, "Type": "volume", "Name": R.PG_VOLUME,
                              "Source": cfg["pg"]["volume_path"], "RW": True}]}
        cfg["pg"]["config_sha256"] = R.sha(actual["Config"])
        image = {"Id": cfg["pg"]["image_id"], "RootFS": {"Layers": cfg["pg"]["rootfs_diffids"]}}
        volume = {"Driver": "local", "Mountpoint": cfg["pg"]["volume_path"], "Labels": actual["Config"]["Labels"]}
        backend = K.backend_class(R)(cfg)
        backend.docker_json = lambda kind, _: {"container": actual, "image": image, "volume": volume}[kind]
        return backend, actual, volume

    def test_stopped_inspection_preserves_exact_identity_and_mount_contract(self):
        backend, actual, _ = self.inspector()
        with mock.patch.object(R, "trusted_directory"):
            self.assertFalse(backend.inspect("pg")["State"]["Running"])
            actual["State"].update(Running=True, Status="running", Pid=200)
            self.assertTrue(backend.inspect("pg")["State"]["Running"])
            actual["HostConfig"]["RestartPolicy"]["Name"] = "always"
            with self.assertRaisesRegex(K.Refusal, "HOST_CONFIG"):
                backend.inspect("pg")

    def app_inspector(self):
        backend, actual, _ = self.inspector()
        actual["Id"] = backend.config["app"]["container_id"]
        backend.config["app"]["config_sha256"] = R.sha(actual["Config"])
        actual["Mounts"] = [{"Destination": "/app/data", "Type": "bind", "Source": str(R.PREFIX / "rolling-recovery/app/data"), "RW": True},
                            {"Destination": "/migration-state", "Type": "bind", "Source": str(R.PREFIX / "app"), "RW": False}]
        return backend

    def filesystem(self, owners=None, modes=None):
        owners = {str(R.PREFIX / "rolling-recovery/app/data"): 1000, **(owners or {})}
        modes = {str(R.PREFIX / "rolling-recovery/app/data"): 0o700, **(modes or {})}

        def metadata(path, **_):
            return types.SimpleNamespace(st_mode=stat.S_IFDIR | modes.get(str(path), 0o755),
                                         st_uid=owners.get(str(path), 0))
        return metadata

    def test_app_private_data_uid1000_with_root_endpoint_directory_is_accepted(self):
        backend = self.app_inspector()
        metadata = self.filesystem()
        with mock.patch.object(pathlib.Path, "lstat", metadata), mock.patch.object(pathlib.Path, "stat", metadata):
            backend.inspect("app")

    def test_app_uid1000_exception_does_not_extend_to_endpoint_root_or_ancestors(self):
        backend = self.app_inspector()
        for path in (R.PREFIX / "app", R.PREFIX, R.PREFIX.parent, R.PREFIX / "rolling-recovery",
                     R.PREFIX / "rolling-recovery/app"):
            metadata = self.filesystem({str(path): 1000})
            with self.subTest(path=path), mock.patch.object(pathlib.Path, "lstat", metadata), \
                    mock.patch.object(pathlib.Path, "stat", metadata), self.assertRaises(R.Refusal):
                backend.inspect("app")

    def test_app_data_wrong_owner_or_writable_group_is_refused(self):
        backend = self.app_inspector()
        for uid, mode in ((999, 0o700), (1000, 0o770), (1000, 0o755)):
            metadata = self.filesystem({str(R.PREFIX / "rolling-recovery/app/data"): uid},
                                       {str(R.PREFIX / "rolling-recovery/app/data"): mode})
            with self.subTest(uid=uid, mode=mode), mock.patch.object(pathlib.Path, "lstat", metadata), \
                    mock.patch.object(pathlib.Path, "stat", metadata), self.assertRaises(R.Refusal):
                backend.inspect("app")

    def test_app_legacy_data_bind_or_moved_endpoint_bind_is_refused(self):
        for destination, source in (("/app/data", R.PREFIX / "app/data"),
                                    ("/migration-state", R.PREFIX / "rolling-recovery/app")):
            backend = self.app_inspector()
            actual = backend.docker_json("container", "app")
            next(row for row in actual["Mounts"] if row["Destination"] == destination)["Source"] = str(source)
            with self.subTest(destination=destination), mock.patch.object(R, "trusted_directory"), \
                    self.assertRaisesRegex(K.Refusal, "APP_MOUNT_CHANGED"):
                backend.inspect("app")

    def test_pg_and_redis_data_uid999_do_not_weaken_root_volume_ancestors(self):
        for kind in ("pg", "redis"):
            backend, actual, volume = self.inspector()
            if kind == "redis":
                actual["Id"] = backend.config[kind]["container_id"]
                actual["Config"]["Labels"]["io.sub2api.migration.run"] = "sub2api-redis-standby-20261016"
                backend.config[kind]["config_sha256"] = R.sha(actual["Config"])
                volume["Mountpoint"] = backend.config[kind]["volume_path"]
                actual["Mounts"] = [{"Destination": "/data", "Type": "volume", "Name": R.REDIS_VOLUME,
                                      "Source": volume["Mountpoint"], "RW": True}]
            data = backend.config[kind]["volume_path"]
            metadata = self.filesystem({data: 999}, {data: 0o700})
            with self.subTest(kind=kind), mock.patch.object(pathlib.Path, "lstat", metadata), \
                    mock.patch.object(pathlib.Path, "stat", metadata):
                backend.inspect(kind)
            metadata = self.filesystem({data: 999, str(pathlib.Path(data).parent): 999})
            with self.subTest(kind=kind, parent="worker-owned"), mock.patch.object(pathlib.Path, "lstat", metadata), \
                    mock.patch.object(pathlib.Path, "stat", metadata), self.assertRaises(R.Refusal):
                backend.inspect(kind)

    def test_redis_config_worker_uid999_matches_data_directory(self):
        backend, _, _ = self.inspector()
        data = backend.config["redis"]["volume_path"]
        raw = b"bind 127.0.0.1\nport 26379\n"
        metadata = self.filesystem({data: 999}, {data: 0o700})
        file_metadata = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=999, st_gid=999,
                                             st_nlink=1, st_size=len(raw))
        with mock.patch.object(pathlib.Path, "lstat", metadata), mock.patch.object(pathlib.Path, "stat", metadata), \
                mock.patch.object(R.os, "open", return_value=55), mock.patch.object(R.os, "fstat", return_value=file_metadata), \
                mock.patch.object(R.os, "read", return_value=raw), mock.patch.object(R.os, "close"):
            self.assertEqual(backend.redis_config(), (raw, (999, 999, 0o600)))

    def test_replacement_container_or_volume_or_unexpected_state_refused(self):
        for field in ("container", "volume", "mount", "state", "config"):
            backend, actual, volume = self.inspector()
            if field == "container":
                actual["Id"] = "f" * 64
            elif field == "volume":
                volume["Mountpoint"] = "/different"
            elif field == "mount":
                actual["Mounts"][0]["RW"] = False
            elif field == "config":
                actual["Config"]["Env"] = ["CHANGED=true"]
            else:
                actual["State"]["Status"] = "created"
            with self.subTest(field=field), mock.patch.object(R, "trusted_directory"), self.assertRaises(K.Refusal):
                backend.inspect("pg")

    def test_pg_boot_preflight_never_reenables_standby(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config()
            cfg["pg"]["volume_path"] = directory
            data = pathlib.Path(directory)
            (data / "PG_VERSION").write_text("18\n")
            (data / ".sub2api-seed-verified").touch()
            (data / "global").mkdir()
            (data / "global/pg_control").touch()
            backend = K.backend_class(R)(cfg)
            backend.prestart("pg", arm(cfg))
            for filename in ("standby.signal", "recovery.signal"):
                (data / filename).touch()
                with self.subTest(filename=filename), self.assertRaisesRegex(K.Refusal, "ROLE_UNPROVEN"):
                    backend.prestart("pg", arm(cfg))
                (data / filename).unlink()

    def test_persistent_completion_gate_reuses_ledger_without_admitting_new_operations(self):
        cfg = config()
        proof = arm(cfg)
        intent = {"final_lsn": "10/0"}
        proof["pg_intent_sha256"] = R.sha(intent)
        ledger = {"owner": cfg["owner"], "pg": {"authority": "TENCENT", "promotion_intent": intent},
                  "redis": {"authority": "TENCENT", "op_id": proof["redis_persist_op_id"]},
                  "operations": {proof["redis_persist_op_id"]: {"phase": "COMPLETE",
                      "request": {"action": "redis.persist-primary"}, "response": {"ok": True, "result": {"persisted": True}}}}}
        backend = K.backend_class(R)(cfg)
        backend.pg_locks = contextlib.nullcontext
        backend.pg_manifest = lambda: {"authority_mode": "PRIMARY", "promotion_intent": intent}
        backend.endpoint_state = lambda: {"v": 1, "postgres": "target", "redis": "target"}
        with mock.patch.object(R, "private_read", return_value=R.canonical(cfg)), \
                mock.patch.object(R, "FileStore") as storage:
            storage.return_value.read.return_value = ledger
            backend.persistent(proof)
            storage.return_value.update.assert_not_called()
            ledger["redis"]["authority"] = "AWS"
            with self.assertRaisesRegex(K.Refusal, "AUTHORITY_NOT_COMPLETE"):
                backend.persistent(proof)

    def test_start_budget_prevents_crash_loop(self):
        cfg = config()
        backend, storage = FakeBackend(cfg), MemoryStore(cfg)
        keeper = K.Keeper(R, cfg, backend, storage, "3" * 64)
        for index in range(K.MAX_STARTS):
            backend.resources["pg"]["State"].update(Running=False, Pid=0, FinishedAt="finish" + str(index))
            keeper.step()
        backend.resources["pg"]["State"].update(Running=False, Pid=0, FinishedAt="finish-final")
        with self.assertRaisesRegex(K.Refusal, "BUDGET_EXHAUSTED"):
            keeper.step()
        self.assertEqual(backend.events.count("start:pg"), K.MAX_STARTS)


if __name__ == "__main__":
    unittest.main()
