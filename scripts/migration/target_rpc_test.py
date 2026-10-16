#!/usr/bin/env python3
"""Offline fault-injection tests: no Docker, SSH, network, or host changes."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import pathlib
import stat
import tempfile
import threading
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("target_rpc", pathlib.Path(__file__).with_name("target_rpc.py"))
rpcmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpcmod)


def config():
    common = {"container_id": "c" * 64, "image_id": "sha256:" + "d" * 64,
              "config_sha256": "e" * 64, "rootfs_diffids": ["sha256:" + "f" * 64]}
    return {"v": 1, "run": rpcmod.RUN, "owner": "a" * 32, "program_sha256": "1" * 64,
            "machine_id_sha256": "2" * 64, "app": {**common, "control_port": 29001},
            "pg": {**common, "owner": "b" * 32, "volume_path": "/var/lib/docker/volumes/fixed-pg/_data",
                   "system_identifier": "12345", "source_timeline": 1},
            "redis": {**common, "owner": "b" * 32, "volume_path": "/var/lib/docker/volumes/fixed-redis/_data"}}


class FakeBackend:
    def __init__(self, cfg):
        self.manifest = {"authority_mode": "STANDBY", "promotion_intent": None}
        self.sample = {"system_identifier": "12345", "version_num": 180003,
                       "listen_addresses": "127.0.0.1", "port": 25432, "in_recovery": True,
                       "transaction_read_only": "on", "default_transaction_read_only": "off",
                       "source_timeline": 1, "replay_lsn": "10/0", "current_lsn": None,
                       "wal_timeline": None, "free_bytes": 3 * 1024**3}
        self.promotions, self.rewrites = 0, 0
        self.pg_lost_reply = False
        self.manifest_lost_write = False
        self.redis_lost_reply = False
        self.redis_refusal = False
        self.redis = {"role": "master", "run_id": "f" * 40, "failover_state": "no-failover"}
        self.control = {"epoch": 0, "paused": False, "held": False, "quiet": False,
                        "endpoint": "source", "pubsub_cache_ready": False, "active_operations": 0, "tracked_sockets": 0}
        self.posts = []
        self.control_lost_reply = False
        self.endpoints = {"v": 1, "postgres": "source", "redis": "source"}
        self.before_promote = lambda: None
        self.component_controls = None
        self.macro_mode = False

    def pg_locks(self):
        return contextlib.nullcontext()

    def pg_manifest(self):
        return copy.deepcopy(self.manifest)

    def pg_save_manifest(self, value):
        if self.manifest_lost_write:
            self.manifest_lost_write = False
            raise OSError("simulated durable disk reply loss")
        self.manifest = copy.deepcopy(value)

    def pg_sample(self):
        return copy.deepcopy(self.sample)

    def pg_promote(self):
        self.before_promote()
        self.promotions += 1
        self.sample.update(in_recovery=False, transaction_read_only="off", current_lsn="10/1", wal_timeline=2,
                           replay_lsn=None)
        if self.pg_lost_reply:
            self.pg_lost_reply = False
            raise rpcmod.Uncertain("COMMAND_OUTCOME_UNKNOWN")

    def redis_inspect(self):
        return copy.deepcopy(self.redis)

    def redis_persist_primary(self):
        self.rewrites += 1
        if self.redis_lost_reply:
            self.redis_lost_reply = False
            raise rpcmod.Uncertain("COMMAND_OUTCOME_UNKNOWN")
        if self.redis_refusal:
            self.redis_refusal = False
            raise rpcmod.Refusal("REDIS_CONFIG_STILL_REPLICA")
        return {**self.redis, "persisted": True, "config_sha256": "c" * 64}

    def app_control(self, control, control_epoch=0, endpoint=None):
        body = self.control if self.component_controls is None else self.component_controls[control.split(".")[0]]
        if control == "runtime.status" and self.macro_mode:
            body["ingress"] = copy.deepcopy(self.component_controls["ingress"])
            body["ready"] = not any(self.component_controls[key].get("paused") for key in ("ingress", "redis", "postgres")) and not self.component_controls["writers"].get("held")
        if control.endswith(".status"):
            return {"http_status": 200, "body": copy.deepcopy(body)}
        self.posts.append((control, control_epoch, endpoint))
        if control.endswith(".pause"):
            body.update(paused=True, held=True, quiet=True)
            if control == "redis.pause":
                body["pubsub_cache_ready"] = False
        elif control.endswith(".resume"):
            body.update(paused=False, held=False, quiet=False)
        elif control.endswith(".switch"):
            body.update(endpoint=endpoint, paused=True, address="127.0.0.1:26379", tracked_sockets=0)
        elif control.endswith(".rebuild"):
            body.update(pubsub_cache_ready=True)
        elif control.endswith(".activate"):
            self.control.update(activated=True, activating=False, failures={}, pending=[])
        body["epoch"] += 1
        if self.control_lost_reply:
            self.control_lost_reply = False
            raise rpcmod.Uncertain("CONTROL_OUTCOME_UNKNOWN")
        return {"http_status": 200, "body": copy.deepcopy(body)}

    def redis_preroute_pair(self):
        return copy.deepcopy(self.preroute_pair)

    def app_session(self):
        return self.app_control

    def app_quiet_snapshot(self):
        return self.snapshot_with(self.app_session())

    def snapshot_with(self, read):
        sampled = {"app_before": read("runtime.status")}
        for key in ("ingress", "writers", "redis", "postgres"):
            sampled[key] = read(key + ".status")
        pg = self.pg_sample()
        sampled["pg"] = {key: pg.get(key) for key in ("system_identifier", "in_recovery", "replay_lsn", "wal_timeline")}
        sampled["app"] = read("runtime.status")
        return sampled

    def endpoint_state(self):
        return copy.deepcopy(self.endpoints)

    def redis_target_address(self):
        return "127.0.0.1:26379"

    def persist_endpoints(self, value):
        rpcmod.require(all(self.endpoints[k] != "target" or value[k] == "target" for k in ("postgres", "redis")),
                       "ENDPOINT_ROLLBACK_FORBIDDEN")
        self.endpoints = copy.deepcopy(value)
        return copy.deepcopy(value)


class RequestAssertions:
    def request(self, **extra):
        return {"v": 1, "run": rpcmod.RUN, "owner": "a" * 32, "op_id": "0" * 32,
                "expected_epoch": 0, "action": "status", **extra}

    def refusal(self, function, code=None):
        with self.assertRaises(rpcmod.Refusal) as caught:
            function()
        if code:
            self.assertEqual(caught.exception.code, code)


class ContractTests(RequestAssertions, unittest.TestCase):
    def test_observe_is_a_closed_get_status_only_request(self):
        for control, (method, _path) in rpcmod.CONTROLS.items():
            request = self.request(action="app.observe", control=control)
            with self.subTest(control=control):
                if method == "GET" and control.endswith(".status"):
                    self.assertEqual(rpcmod.parse_request(rpcmod.canonical(request)), request)
                    self.assertFalse(rpcmod.TargetRPC.is_mutation(request))
                else:
                    self.refusal(lambda: rpcmod.parse_request(rpcmod.canonical(request)),
                                 "INVALID_OBSERVATION_CONTROL")
        valid = self.request(action="app.observe", control="runtime.status")
        for extra in ({"control_epoch": 0}, {"endpoint": "target"}, {"url": "http://elsewhere"}):
            self.refusal(lambda extra=extra: rpcmod.parse_request(rpcmod.canonical({**valid, **extra})), "REQUEST_FIELDS")
        for control in (None, [], "GET /status", "http://localhost/status", "runtime.status/../pause"):
            self.refusal(lambda control=control: rpcmod.parse_request(rpcmod.canonical({**valid, "control": control})),
                         "INVALID_OBSERVATION_CONTROL")

    def test_arm_quiet_requires_its_own_fixed_nonce_schema(self):
        request = self.request(action="pg.arm-quiet", final_lsn="10/0",
                               quiet_attestation_sha256="b" * 64, quiet_nonce="c" * 32)
        self.assertEqual(rpcmod.parse_request(rpcmod.canonical(request)), request)
        self.assertTrue(rpcmod.TargetRPC.is_mutation(request))
        for nonce in (None, True, "a" * 31, "A" * 32, "g" * 32):
            self.refusal(lambda nonce=nonce: rpcmod.parse_request(rpcmod.canonical({**request, "quiet_nonce": nonce})),
                         "INVALID_QUIET_NONCE")
        self.refusal(lambda: rpcmod.parse_request(rpcmod.canonical({k: v for k, v in request.items()
                                                                  if k != "quiet_nonce"})), "REQUEST_FIELDS")
        self.refusal(lambda: rpcmod.parse_request(rpcmod.canonical({**request, "action": "pg.arm"})), "REQUEST_FIELDS")

    def test_quiet_snapshot_is_closed_read_only_action(self):
        request = self.request(action="app.quiet-snapshot")
        self.assertEqual(rpcmod.parse_request(rpcmod.canonical(request)), request)
        self.assertFalse(rpcmod.TargetRPC.is_mutation(request))
        for extra in ({"controls": ["ingress.resume"]}, {"endpoint": "target"}, {"token": "anything"}):
            self.refusal(lambda extra=extra: rpcmod.parse_request(rpcmod.canonical({**request, **extra})), "REQUEST_FIELDS")

    def test_default_is_entirely_offline(self):
        with mock.patch.object(rpcmod, "load_config", side_effect=AssertionError("must remain offline")), \
             mock.patch.object(rpcmod.subprocess, "run", side_effect=AssertionError("no command")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(rpcmod.main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["local_operations"], 0)

    def test_strict_json_and_request_boundaries(self):
        raw = rpcmod.canonical(self.request())
        self.assertEqual(rpcmod.read_request(io.BytesIO(raw)), self.request())
        self.assertEqual(rpcmod.parse_request(raw + b" " * (16384 - len(raw))), self.request())
        for invalid in [raw + b" " * 16384, raw + b"{}", b'{"v":1,"v":1}', b'{"x":NaN}',
                        b'{"x":1e999}', b'\xff', b'[]', b'', raw + b'\x00']:
            self.refusal(lambda invalid=invalid: rpcmod.parse_request(invalid))
        for changes in [{"extra": 1}, {"v": True}, {"expected_epoch": True}, {"expected_epoch": -1},
                        {"run": "another-run"}, {"op_id": "../anything"}, {"action": "exec"}, {"action": []}]:
            self.refusal(lambda changes=changes: rpcmod.parse_request(rpcmod.canonical(self.request(**changes))))
        for changes in [{"action": "app.control", "control": "postgres.switch", "control_epoch": 0},
                        {"action": "app.control", "control": "postgres.pause", "control_epoch": 0, "endpoint": "target"},
                        {"action": "app.persist-endpoints", "postgres": "other", "redis": "source"}]:
            self.refusal(lambda changes=changes: rpcmod.parse_request(rpcmod.canonical(self.request(**changes))))

    def test_closed_config_and_numeric_lsn(self):
        self.assertEqual(rpcmod.validate_config(config())["run"], rpcmod.RUN)
        bad = config(); bad["app"]["command"] = "sh"
        self.refusal(lambda: rpcmod.validate_config(bad), "CONFIG_RESOURCE_FIELDS")
        bad = config(); bad["app"]["control_port"] = True
        self.refusal(lambda: rpcmod.validate_config(bad), "CONFIG_CONTROL_PORT")
        self.assertLess(rpcmod.lsn_number("F/FFFFFFFF"), rpcmod.lsn_number("10/0"))
        for invalid in ["10/000000000", "0/ff", "1;DROP/0", None]:
            self.refusal(lambda invalid=invalid: rpcmod.lsn_number(invalid))

    def test_control_sanitizer_drops_diagnostics(self):
        value = rpcmod.sanitize_control({"epoch": 2, "password": "secret", "readiness": {"postgres": True, "bad": "secret"},
                  "failures": {"worker": "secret stack trace"}, "endpoint": "http://credential:secret@host",
                  "consumers": [{"name": "auth", "channel": "auth:cache", "password": "secret"}],
                  "started": ["worker", "secret words with spaces"], "active": "secret"})
        self.assertNotIn("secret", json.dumps(value).replace("START_FAILED", ""))
        self.assertEqual(value["readiness"], {"postgres": True})
        self.assertEqual(value["failures"], {"worker": "START_FAILED"})

    def test_read_request_has_bounded_eof_deadline(self):
        class BlockingStream:
            def read(self, _limit):
                raise rpcmod.Refusal("REQUEST_READ_TIMEOUT")
        with mock.patch.object(rpcmod.signal, "signal") as handler, mock.patch.object(rpcmod.signal, "setitimer") as timer:
            self.refusal(lambda: rpcmod.read_request(BlockingStream()), "REQUEST_READ_TIMEOUT")
        self.assertEqual(timer.call_args_list[0].args[1], 3)
        self.assertEqual(timer.call_args_list[-1].args[1], 0)

    def test_program_path_and_digest_are_required(self):
        with mock.patch.object(rpcmod.os, "geteuid", return_value=0), \
             mock.patch.object(rpcmod, "trusted_directory"), \
             mock.patch.object(rpcmod, "private_read", return_value=rpcmod.canonical(config())):
            self.refusal(rpcmod.load_config, "PROGRAM_PATH")
        with mock.patch.object(rpcmod.os, "geteuid", return_value=0), \
             mock.patch.object(rpcmod, "trusted_directory"), \
             mock.patch.object(rpcmod, "PROGRAM", pathlib.Path(rpcmod.__file__).absolute()), \
             mock.patch.object(rpcmod, "private_read", side_effect=[rpcmod.canonical(config()), b"changed program"]):
            self.refusal(rpcmod.load_config, "PROGRAM_CHANGED")


class StateTests(RequestAssertions, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rpc-isolated-test-", dir=pathlib.Path.home())
        self.scope = pathlib.Path(self.temp.name)
        self.cfg = config()
        self.store = rpcmod.FileStore(self.cfg, self.scope, os.geteuid())
        self.backend = FakeBackend(self.cfg)
        self.clock = 1000.0
        self.rpc = rpcmod.TargetRPC(self.cfg, self.backend, self.store, lambda: self.clock)
        self.sequence = 1

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, **extra):
        request = self.request(action=action, op_id=f"{self.sequence:032x}",
                               expected_epoch=self.store.read()["epoch"], **extra)
        self.sequence += 1
        return request, self.rpc.handle(request)

    def arm(self):
        request, reply = self.call("pg.arm", final_lsn="F/FFFFFFFF", quiet_attestation_sha256="b" * 64)
        self.assertEqual(reply["code"], "OK")
        return request, reply["result"]["arm_id"]

    def prepare_preroute(self):
        self.store.update(lambda state: state["pg"].update(authority="TENCENT"))
        self.backend.sample.update(in_recovery=False, transaction_read_only="off", wal_timeline=2, current_lsn="10/1")
        self.backend.endpoints["postgres"] = "target"
        ingress = {"epoch": 2, "paused": True, "quiet": True}
        self.backend.component_controls = {
            "runtime": {"instance_id": "same-instance", "serving": True, "draining": False,
                "active_requests": 0, "active_hijacked_connections": 0, "background_workers_enabled": False,
                "ready": False, "readiness": {"writer_coverage": True, "quiet_controls": True},
                "ingress": ingress, "tasks": {name: {"active": 0, "pending": 0, "failed": 0, "dropped": 0}
                                                for name in rpcmod.REQUIRED_WRITERS}},
            "ingress": ingress, "writers": {"held": True, "quiet": True},
            "postgres": {"epoch": 0, "paused": True, "active_operations": 0, "endpoint": "target"},
            "redis": {"epoch": 0, "paused": True, "quiet": True, "active_operations": 0,
                      "tracked_sockets": 3, "address": "127.0.0.1:16379"}}
        self.backend.preroute_pair = {
            "source": {"run_id": rpcmod.REDIS_SOURCE_RUN_ID, "role": "master", "master_replid": rpcmod.REDIS_SOURCE_REPLID,
                       "connected_slaves": "1", "slave0": "ip=172.18.0.1,port=26379,state=online,offset=10,lag=0"},
            "target": {"run_id": rpcmod.REDIS_TARGET_RUN_ID, "role": "slave", "master_replid": rpcmod.REDIS_SOURCE_REPLID,
                       "connected_slaves": "0", "master_host": "127.0.0.1", "master_port": "16379",
                       "master_link_status": "up", "master_sync_in_progress": "0"}}

    def prepare_quiesce(self):
        self.prepare_preroute()
        self.store.update(lambda state: state["pg"].update(authority="AWS"))
        self.backend.sample.update(in_recovery=True, transaction_read_only="on", wal_timeline=None, current_lsn=None)
        self.backend.endpoints.update(postgres="source", redis="source")
        for key in ("ingress", "writers", "redis", "postgres"):
            self.backend.component_controls[key].update(epoch=0, paused=False, held=False, quiet=False)
        self.backend.component_controls["postgres"]["endpoint"] = "source"
        self.backend.component_controls["redis"].update(pubsub_cache_ready=True, tracked_sockets=3)
        self.backend.macro_mode = True

    def test_observe_repeated_id_is_fresh_without_writing_any_operation_or_audit(self):
        self.prepare_quiesce()
        before = {path.name: path.read_bytes() for path in self.scope.iterdir() if path.is_file()}
        request = self.request(action="app.observe", control="runtime.status")
        with mock.patch.object(self.store, "update", side_effect=AssertionError("observe must not update")), \
             mock.patch.object(self.store, "operation_lock", side_effect=AssertionError("observe must not admit")), \
             mock.patch.object(self.store, "mutation_lock", side_effect=AssertionError("observe must not mutate")):
            first = self.rpc.handle(request)
            self.backend.component_controls["runtime"]["active_requests"] = 7
            second = self.rpc.handle(request)
        self.assertEqual(first["code"], "OK")
        self.assertEqual(second["code"], "OK")
        self.assertEqual(first["result"]["body"]["active_requests"], 0)
        self.assertEqual(second["result"]["body"]["active_requests"], 7)
        self.assertEqual(set(first), {"ok", "code", "run", "owner", "op_id", "action", "epoch", "phase", "result"})
        self.assertEqual(first["epoch"], second["epoch"])
        self.assertEqual(self.backend.posts, [])
        self.assertEqual({path.name: path.read_bytes() for path in self.scope.iterdir() if path.is_file()}, before)

    def test_observe_refuses_wrong_owner_epoch_and_post_without_backend_or_ledger_effects(self):
        self.prepare_quiesce()
        before = self.store.read()
        with mock.patch.object(self.store, "update", side_effect=AssertionError("no writes")), \
             mock.patch.object(self.backend, "app_control", side_effect=AssertionError("no control call")):
            for changes, code in (({"owner": "b" * 32}, "WRONG_OWNER"), ({"expected_epoch": 1}, "EPOCH_CONFLICT")):
                request = self.request(action="app.observe", control="runtime.status", **changes)
                reply = self.rpc.handle(request)
                self.assertEqual(reply["code"], code)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["action"], "app.observe")
            self.refusal(lambda: self.rpc.handle(self.request(action="app.observe", control="ingress.resume")),
                         "INVALID_OBSERVATION_CONTROL")
        self.assertEqual(self.store.read(), before)

    def test_observe_backend_errors_return_envelopes_without_pending_operations(self):
        self.prepare_quiesce()
        before = self.store.read()
        for error, code, reason in ((rpcmod.Uncertain("CONTROL_OUTCOME_UNKNOWN"), "UNKNOWN", "CONTROL_OUTCOME_UNKNOWN"),
                                    (rpcmod.Refusal("APP_CONTAINER_CHANGED"), "APP_CONTAINER_CHANGED", None),
                                    (RuntimeError("sensitive diagnostic"), "UNKNOWN", "OBSERVATION_UNCERTAIN")):
            with self.subTest(code=code, reason=reason), \
                 mock.patch.object(self.store, "update", side_effect=AssertionError("no writes")), \
                 mock.patch.object(self.backend, "app_control", side_effect=error):
                result = self.rpc.handle(self.request(action="app.observe", control="runtime.status"))
                self.assertEqual(result["code"], code)
                self.assertEqual(result["result"].get("reason"), reason)
                if code == "UNKNOWN":
                    self.assertIs(result["result"]["reconcile_required"], False)
                self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual(self.store.read(), before)

    def test_observe_ignores_cached_operation_ids_but_legacy_get_stays_durable(self):
        self.prepare_quiesce()
        old, first = self.call("app.control", control="runtime.status", control_epoch=0)
        self.assertEqual(first["code"], "OK")
        ledger = self.store.read()
        self.assertIn(old["op_id"], ledger["operations"])
        self.backend.component_controls["runtime"]["active_requests"] = 3
        request = {key: value for key, value in old.items() if key != "control_epoch"}
        request["action"] = "app.observe"
        fresh = self.rpc.handle(request)
        self.assertEqual(fresh["result"]["body"]["active_requests"], 3)
        self.assertEqual(self.store.read(), ledger)
        self.assertEqual(self.rpc.handle(old), first)

    def test_fixed_macros_accept_no_caller_controls_or_endpoints(self):
        for action in ("app.quiesce", "app.restore", "pg.route", "redis.preroute"):
            request = self.request(action=action)
            self.assertEqual(rpcmod.parse_request(rpcmod.canonical(request)), request)
            self.assertTrue(self.rpc.is_mutation(request))
            for extra in ({"endpoint": "source"}, {"controls": []}, {"control_epoch": 0}):
                self.refusal(lambda extra=extra: rpcmod.parse_request(rpcmod.canonical({**request, **extra})), "REQUEST_FIELDS")

    def test_quiesce_and_restore_fixed_order_with_durable_epochs(self):
        self.prepare_quiesce()
        original = self.backend.app_control
        def checked(control, epoch=0, endpoint=None):
            if not control.endswith(".status"):
                op = self.store.read()["operations"][request["op_id"]]
                step = next(step for step in op["substeps"].values() if step.get("control") == control)
                self.assertTrue(step["effect_started"])
                self.assertEqual(step["control_epoch"], epoch)
                self.assertEqual(step["phase"], "INTENT")
            return original(control, epoch, endpoint)
        self.backend.app_control = checked
        request = self.request(action="app.quiesce", op_id="a"*32)
        result = self.rpc.handle(request)
        self.assertEqual(result["code"], "OK")
        self.assertFalse(result["result"]["app"]["body"]["ready"])
        self.assertEqual([post[0] for post in self.backend.posts], [key+".pause" for key in ("ingress", "writers", "redis", "postgres")])
        self.backend.app_control = original
        _, result = self.call("app.restore")
        self.assertEqual(result["code"], "OK")
        self.assertTrue(result["result"]["app"]["body"]["ready"])
        self.assertEqual([post[0] for post in self.backend.posts][4:],
            ["postgres.resume", "redis.rebuild", "redis.resume", "writers.resume", "ingress.resume"])
        self.assertTrue(self.store.read()["operations"][request["op_id"]]["cancelled"])

    def test_macro_restore_observes_lost_sql_resume_and_accepts_ingress_204(self):
        self.prepare_quiesce()
        self.call("app.quiesce")
        self.backend.control_lost_reply = True
        request, first = self.call("app.restore")
        self.assertEqual(first["code"], "UNKNOWN")
        original = self.backend.app_control
        def with_204(control, epoch=0, endpoint=None):
            result = original(control, epoch, endpoint)
            return {"http_status": 204, "body": {}} if control == "ingress.resume" else result
        self.backend.app_control = with_204
        self.assertEqual(self.rpc.handle(request)["code"], "OK")
        self.assertEqual([post[0] for post in self.backend.posts].count("postgres.resume"), 1)
        self.assertTrue(self.backend.component_controls["runtime"]["ready"])

    def test_restore_lost_final_reply_reconciles_while_new_requests_are_already_served(self):
        self.prepare_quiesce()
        self.call("app.quiesce")
        original = self.backend.app_control
        def lost_final(control, epoch=0, endpoint=None):
            result = original(control, epoch, endpoint)
            if control == "ingress.resume":
                self.backend.component_controls["runtime"]["active_requests"] = 1
                raise rpcmod.Uncertain("CONTROL_OUTCOME_UNKNOWN")
            return result
        self.backend.app_control = lost_final
        request, first = self.call("app.restore")
        self.assertEqual(first["code"], "UNKNOWN")
        self.backend.app_control = original
        posts = list(self.backend.posts)
        restored = self.rpc.handle(request)
        self.assertEqual(restored["code"], "OK")
        self.assertEqual(self.backend.posts, posts)
        self.assertTrue(restored["result"]["app"]["body"]["ready"])
        self.assertEqual(restored["result"]["app"]["body"]["active_requests"], 1)

    def test_macro_restore_can_cancel_quiesce_rejected_before_instance_pin(self):
        self.prepare_quiesce()
        self.backend.component_controls["runtime"]["active_requests"] = 1
        request, first = self.call("app.quiesce")
        self.assertEqual(first["code"], "UNKNOWN")
        self.assertNotIn("instance_id", self.store.read()["operations"][request["op_id"]])
        self.assertEqual(self.backend.posts, [])
        self.backend.component_controls["runtime"]["active_requests"] = 0
        _, restored = self.call("app.restore")
        self.assertEqual(restored["code"], "OK")
        self.assertTrue(self.store.read()["operations"][request["op_id"]]["cancelled"])

    def prepare_adopted_generation(self):
        self.prepare_quiesce()
        self.call("app.quiesce")
        self.call("app.restore")
        history = copy.deepcopy(self.store.read()["operations"])
        self.backend.component_controls["runtime"]["instance_id"] = "adopted-instance"
        self.backend.posts.clear()
        return history

    def test_adopted_generation_abort_before_new_quiesce_restores_without_rebinding_history(self):
        history = self.prepare_adopted_generation()
        self.backend.component_controls["runtime"]["active_requests"] = 1
        adoption = mock.Mock(return_value=True)
        self.backend.app_restore_adoption = adoption
        request, reply = self.call("app.restore")
        self.assertEqual(reply["code"], "OK")
        adoption.assert_called_once()
        self.assertEqual(adoption.call_args.args[0], request)
        ledger = self.store.read()
        self.assertEqual({key: ledger["operations"][key] for key in history}, history)
        self.assertEqual(ledger["operations"][request["op_id"]]["instance_id"], "adopted-instance")
        self.assertEqual(self.backend.posts, [])
        self.assertEqual(reply["result"]["app"]["body"]["active_requests"], 1)

    def test_changed_generation_without_receipt_keeps_strict_refusal(self):
        self.prepare_adopted_generation()
        _, reply = self.call("app.restore")
        self.assertEqual(reply["code"], "UNKNOWN")
        self.assertEqual(reply["result"]["reason"], "APP_INSTANCE_CHANGED")
        self.assertEqual(self.backend.posts, [])

    def test_adoption_cannot_rebind_an_existing_old_instance_operation(self):
        self.prepare_adopted_generation()
        request, _ = self.call("app.restore")
        self.store.update(lambda ledger: ledger["operations"][request["op_id"]].update(instance_id="same-instance"))
        self.backend.app_restore_adoption = mock.Mock(return_value=True)
        reply = self.rpc.handle(request)
        self.assertEqual(reply["code"], "UNKNOWN")
        self.assertEqual(reply["result"]["reason"], "APP_INSTANCE_CHANGED")
        self.backend.app_restore_adoption.assert_not_called()
        self.assertEqual(self.backend.posts, [])

    def test_adoption_cannot_bypass_new_quiesce_for_data_routes(self):
        self.prepare_adopted_generation()
        self.backend.app_restore_adoption = mock.Mock(return_value=True)
        for action in ("pg.route", "redis.preroute"):
            with self.subTest(action=action):
                _, reply = self.call(action)
                self.assertEqual(reply["code"], "UNKNOWN")
                self.assertEqual(reply["result"]["reason"], "APP_INSTANCE_CHANGED")
        self.backend.app_restore_adoption.assert_not_called()
        self.assertEqual(self.backend.posts, [])

    def test_new_quiesce_then_restore_keeps_original_generation_rules(self):
        self.prepare_adopted_generation()
        self.backend.app_restore_adoption = mock.Mock(side_effect=AssertionError("new quiesce needs no adoption"))
        request, reply = self.call("app.quiesce")
        self.assertEqual(reply["code"], "OK")
        _, reply = self.call("app.restore")
        self.assertEqual(reply["code"], "OK")
        self.assertEqual(self.store.read()["operations"][request["op_id"]]["instance_id"], "adopted-instance")
        self.backend.app_restore_adoption.assert_not_called()

    def test_adopted_restore_lost_resume_is_observed_without_reauthorizing_or_replaying(self):
        self.prepare_adopted_generation()
        self.backend.component_controls["postgres"]["paused"] = True
        self.backend.app_restore_adoption = mock.Mock(return_value=True)
        self.backend.control_lost_reply = True
        request, reply = self.call("app.restore")
        self.assertEqual(reply["code"], "UNKNOWN")
        self.backend.app_restore_adoption.assert_called_once()
        reply = self.rpc.handle(request)
        self.assertEqual(reply["code"], "OK")
        self.assertEqual([row[0] for row in self.backend.posts].count("postgres.resume"), 1)
        self.backend.app_restore_adoption.assert_called_once()

    def prepare_ingress_only_race(self, counter="active_requests", value=1):
        self.prepare_quiesce()
        self.backend.posts.clear()
        self.backend.component_controls["runtime"]["readiness"].update(postgres=True, redis=True)
        original = self.backend.app_control
        def request_arrives_before_pause(control, control_epoch=0, endpoint=None):
            if control == "ingress.pause":
                self.backend.component_controls["runtime"][counter] = value
                result = original(control, control_epoch, endpoint)
                self.backend.component_controls["ingress"]["quiet"] = False
                result["body"]["quiet"] = False
                return result
            return original(control, control_epoch, endpoint)
        self.backend.app_control = request_arrives_before_pause
        try:
            request, reply = self.call("app.quiesce")
        finally:
            self.backend.app_control = original
        self.assertEqual(reply["code"], "UNKNOWN")
        self.assertEqual(reply["result"]["reason"], "MACRO_EFFECT_UNCONFIRMED")
        self.assertEqual([row[0] for row in self.backend.posts], ["ingress.pause"])
        return request

    def test_restore_reopens_ingress_only_pause_while_racing_sse_or_hijacked_work_continues(self):
        for counter in ("active_requests", "active_hijacked_connections"):
            with self.subTest(counter=counter):
                quiesce = self.prepare_ingress_only_race(counter)
                _, restored = self.call("app.restore")
                self.assertEqual(restored["code"], "OK")
                self.assertEqual(restored["result"]["app"]["body"][counter], 1)
                self.assertTrue(restored["result"]["app"]["body"]["ready"])
                self.assertEqual([row[0] for row in self.backend.posts], ["ingress.pause", "ingress.resume"])
                self.assertTrue(self.store.read()["operations"][quiesce["op_id"]]["cancelled"])
                self.assertEqual(self.rpc.handle(quiesce)["code"], "QUIESCE_CANCELLED")
                posts = list(self.backend.posts)
                self.rpc.guard_reconcile()
                self.assertEqual(self.backend.posts, posts)
                self.assertFalse(self.backend.component_controls["ingress"]["paused"])

    def test_ingress_only_restore_lost_resume_reply_reconciles_without_duplicate_post(self):
        self.prepare_ingress_only_race()
        self.backend.control_lost_reply = True
        request, first = self.call("app.restore")
        self.assertEqual(first["code"], "UNKNOWN")
        self.assertFalse(self.backend.component_controls["ingress"]["paused"])
        restored = self.rpc.handle(request)
        self.assertEqual(restored["code"], "OK")
        self.assertEqual(restored["result"]["app"]["body"]["active_requests"], 1)
        self.assertEqual([row[0] for row in self.backend.posts], ["ingress.pause", "ingress.resume"])

    def test_ingress_only_restore_refuses_any_data_pause_or_endpoint_uncertainty(self):
        mutations = (
            lambda: self.backend.component_controls["writers"].update(held=True),
            lambda: self.backend.component_controls["postgres"].update(paused=True),
            lambda: self.backend.component_controls["redis"].update(paused=True),
            lambda: self.backend.component_controls["redis"].update(pubsub_cache_ready=False),
            lambda: self.backend.component_controls["postgres"].update(endpoint="target"),
            lambda: self.backend.component_controls["redis"].update(address="127.0.0.1:26379"),
            lambda: self.backend.endpoints.update(postgres="target"),
            lambda: self.backend.endpoints.update(redis="target"),
            lambda: self.store.update(lambda state: state["pg"].update(authority="COMMIT_INTENT")),
            lambda: self.store.update(lambda state: state["redis"].update(authority="COMMIT_INTENT")),
        )
        for i, mutate in enumerate(mutations):
            with self.subTest(mutation=i):
                self.store.update(lambda state: state["redis"].update(authority="AWS"))
                quiesce = self.prepare_ingress_only_race()
                mutate()
                _, restored = self.call("app.restore")
                self.assertEqual(restored["code"], "UNKNOWN")
                self.assertEqual([row[0] for row in self.backend.posts], ["ingress.pause"])
                self.assertTrue(self.backend.component_controls["ingress"]["paused"])
                self.assertFalse(self.store.read()["operations"][quiesce["op_id"]].get("cancelled", False))

    def test_ingress_only_restore_refuses_invalid_runtime_identity_readiness_or_counts(self):
        mutations = (
            lambda: self.backend.component_controls["runtime"].update(instance_id="replaced"),
            lambda: self.backend.component_controls["runtime"].update(serving=False),
            lambda: self.backend.component_controls["runtime"].update(draining=True),
            lambda: self.backend.component_controls["runtime"].update(background_workers_enabled=True),
            lambda: self.backend.component_controls["runtime"]["readiness"].update(postgres=False),
            lambda: self.backend.component_controls["runtime"]["readiness"].pop("redis"),
            lambda: self.backend.component_controls["runtime"].update(active_requests=True),
            lambda: self.backend.component_controls["runtime"].update(active_hijacked_connections=-1),
            lambda: self.backend.component_controls["writers"].update(held=None),
        )
        for i, mutate in enumerate(mutations):
            with self.subTest(mutation=i):
                self.prepare_ingress_only_race()
                mutate()
                _, restored = self.call("app.restore")
                self.assertEqual(restored["code"], "UNKNOWN")
                self.assertEqual([row[0] for row in self.backend.posts], ["ingress.pause"])

    def test_ingress_only_restore_refuses_mixed_ingress_epoch(self):
        self.prepare_ingress_only_race()
        original = self.backend.app_control
        def changed_epoch(control, control_epoch=0, endpoint=None):
            result = original(control, control_epoch, endpoint)
            if control == "ingress.status":
                result["body"]["epoch"] += 1
            return result
        self.backend.app_control = changed_epoch
        _, restored = self.call("app.restore")
        self.assertEqual(restored["code"], "UNKNOWN")
        self.assertEqual([row[0] for row in self.backend.posts], ["ingress.pause"])

    def test_idle_restore_rejects_actual_endpoint_mismatch_before_resume_or_quiesce_cancellation(self):
        for component, field, bad in (("postgres", "endpoint", "target"),
                                      ("redis", "address", "127.0.0.1:26379")):
            for paused in (False, True):
                with self.subTest(component=component, paused=paused):
                    self.prepare_quiesce()
                    quiesce, quiet = self.call("app.quiesce")
                    self.assertEqual(quiet["code"], "OK")
                    self.assertEqual(self.backend.component_controls["runtime"]["active_requests"], 0)
                    if not paused:
                        for key in ("postgres", "redis"):
                            self.backend.component_controls[key]["paused"] = False
                        self.backend.component_controls["redis"]["pubsub_cache_ready"] = True
                    self.backend.component_controls[component][field] = bad
                    before = list(self.backend.posts)
                    _, restored = self.call("app.restore")
                    self.assertEqual(restored["code"], "UNKNOWN")
                    self.assertEqual(restored["result"]["reason"], "MACRO_ENDPOINT_UNCONFIRMED")
                    self.assertEqual(self.backend.posts, before)
                    self.assertFalse(self.store.read()["operations"][quiesce["op_id"]].get("cancelled", False))
                    self.assertTrue(self.backend.component_controls["ingress"]["paused"])

    def test_macro_quiesce_rejects_boolean_operation_count_and_mixed_ingress_epoch(self):
        for mutate in (lambda sample: sample["redis"]["body"].update(active_operations=False),
                       lambda sample: sample["app"]["body"]["ingress"].update(epoch=999)):
            with self.subTest(mutate=mutate):
                self.prepare_quiesce()
                original = self.backend.snapshot_with
                def changed(read):
                    result = original(read)
                    mutate(result)
                    return result
                self.backend.snapshot_with = changed
                _, reply = self.call("app.quiesce")
                self.assertEqual(reply["code"], "UNKNOWN")
                self.backend.snapshot_with = original

    def test_quiesce_lost_reply_observes_completed_effect_without_second_post(self):
        self.prepare_quiesce()
        self.backend.control_lost_reply = True
        request, first = self.call("app.quiesce")
        self.assertEqual(first["code"], "UNKNOWN")
        self.assertEqual([post[0] for post in self.backend.posts], ["ingress.pause"])
        self.assertEqual(self.rpc.handle(request)["code"], "OK")
        self.assertEqual([post[0] for post in self.backend.posts].count("ingress.pause"), 1)

    def test_restore_cancels_unexecuted_quiesce_intent_and_guard_cannot_repause(self):
        self.prepare_quiesce()
        original = self.backend.app_control
        def fail_before_effect(control, epoch=0, endpoint=None):
            if control == "writers.pause":
                raise rpcmod.Uncertain("CONTROL_OUTCOME_UNKNOWN")
            return original(control, epoch, endpoint)
        self.backend.app_control = fail_before_effect
        request, first = self.call("app.quiesce")
        self.assertEqual(first["code"], "UNKNOWN")
        self.assertEqual(self.rpc.handle(request)["code"], "UNKNOWN")
        self.assertEqual([post[0] for post in self.backend.posts], ["ingress.pause"])
        self.backend.app_control = original
        _, restored = self.call("app.restore")
        self.assertEqual(restored["code"], "OK")
        self.assertEqual(self.rpc.handle(request)["code"], "QUIESCE_CANCELLED")
        self.rpc.guard_reconcile()
        self.assertFalse(self.backend.component_controls["ingress"]["paused"])
        self.assertFalse(self.backend.component_controls["writers"]["held"])

    def test_restore_after_ingress_and_writers_pause_supersedes_unexecuted_redis_step(self):
        self.prepare_quiesce()
        original = self.backend.app_control
        def fail_before_effect(control, epoch=0, endpoint=None):
            if control == "redis.pause":
                raise rpcmod.Uncertain("CONTROL_OUTCOME_UNKNOWN")
            return original(control, epoch, endpoint)
        self.backend.app_control = fail_before_effect
        request, first = self.call("app.quiesce")
        self.assertEqual(first["code"], "UNKNOWN")
        self.assertEqual([row[0] for row in self.backend.posts], ["ingress.pause", "writers.pause"])
        self.backend.app_control = original
        _, result = self.call("app.restore")
        self.assertEqual(result["code"], "OK")
        restored_posts = list(self.backend.posts)
        self.rpc.guard_reconcile()
        self.assertEqual(self.backend.posts, restored_posts)
        self.assertFalse(self.backend.component_controls["ingress"]["paused"])
        self.assertFalse(self.backend.component_controls["writers"]["held"])
        self.assertEqual(self.rpc.handle(request)["code"], "QUIESCE_CANCELLED")

    def test_macros_refuse_changed_instance_and_unconfirmed_authority(self):
        self.prepare_quiesce()
        _, first = self.call("app.quiesce")
        self.assertEqual(first["code"], "OK")
        _, route = self.call("pg.route")
        self.assertEqual(route["code"], "UNKNOWN")
        self.assertEqual(self.backend.endpoints["postgres"], "source")
        self.backend.component_controls["runtime"]["instance_id"] = "replaced"
        _, restored = self.call("app.restore")
        self.assertEqual(restored["code"], "UNKNOWN")
        self.assertTrue(self.backend.component_controls["ingress"]["paused"])

    def test_pg_route_persists_only_confirmed_target_before_paused_switch(self):
        self.prepare_quiesce()
        self.call("app.quiesce")
        self.store.update(lambda state: state["pg"].update(authority="TENCENT"))
        self.backend.sample.update(in_recovery=False, transaction_read_only="off", wal_timeline=2, current_lsn="10/1")
        request, routed = self.call("pg.route")
        self.assertEqual(routed["code"], "OK")
        self.assertEqual(self.backend.endpoints, {"v": 1, "postgres": "target", "redis": "source"})
        self.assertEqual(routed["result"]["body"]["endpoint"], "target")
        self.assertEqual(self.rpc.handle(request)["code"], "OK")
        self.assertEqual([row[0] for row in self.backend.posts].count("postgres.switch"), 1)

    def test_redis_preroute_macro_retains_strict_proofs_and_at_most_once_switch(self):
        self.prepare_preroute()
        self.backend.control_lost_reply = True
        request, first = self.call("redis.preroute")
        self.assertEqual(first["code"], "UNKNOWN")
        self.backend.preroute_pair["target"]["role"] = "master"
        self.assertEqual(self.rpc.handle(request)["code"], "OK")
        self.assertEqual(len(self.backend.posts), 1)
        self.assertEqual(self.backend.endpoints["redis"], "source")

    def test_quiet_snapshot_keeps_existing_shapes_and_read_epoch(self):
        self.prepare_preroute()
        self.backend.sample.update(in_recovery=True, replay_lsn="10/9")
        _, reply = self.call("app.quiet-snapshot")
        self.assertEqual(reply["code"], "OK")
        self.assertEqual(reply["epoch"], 0)
        result = reply["result"]
        self.assertEqual(set(result), {"app", "pg", "ingress", "writers", "redis", "postgres"})
        self.assertEqual(result["pg"]["replay_lsn"], "10/9")
        self.assertFalse(result["app"]["body"]["ready"])
        self.assertTrue(result["redis"]["quiet"])
        self.assertEqual(self.backend.posts, [])

    def test_quiet_snapshot_refuses_mixed_instances_unavailable_component_or_slow_batch(self):
        for fault in ("instance", "status", "time"):
            with self.subTest(fault=fault):
                self.prepare_preroute()
                original = self.backend.pg_sample
                def sample():
                    if fault == "instance":
                        self.backend.component_controls["runtime"]["instance_id"] = "replacement"
                    if fault == "time":
                        self.clock += 2.01
                    return original()
                original_control = self.backend.app_control
                def control(name, *args):
                    if fault == "status" and name == "writers.status":
                        return {"http_status": 503, "body": {}}
                    return original_control(name, *args)
                with mock.patch.object(self.backend, "pg_sample", side_effect=sample), \
                     mock.patch.object(self.backend, "app_control", side_effect=control):
                    _, reply = self.call("app.quiet-snapshot")
                self.assertEqual(reply["code"], {"instance": "APP_INSTANCE_CHANGED", "status": "QUIET_SNAPSHOT_UNAVAILABLE", "time": "QUIET_SNAPSHOT_SAMPLE_STALE"}[fault])
                self.assertEqual(self.backend.posts, [])

    def test_paused_redis_preroute_closes_pool_without_persisting_unconfirmed_target(self):
        self.prepare_preroute()
        original = self.backend.app_control
        def control(name, *args):
            if name == "redis.switch":
                entry = next(value for value in self.store.read()["operations"].values()
                             if value["request"].get("control") == name)
                self.assertEqual(entry["phase"], "INTENT")
                self.assertTrue(entry["effect_started"])
            return original(name, *args)
        self.backend.app_control = control
        _, reply = self.call("app.control", control="redis.switch", control_epoch=0, endpoint="target")
        self.assertEqual(reply["code"], "OK")
        self.assertEqual(reply["result"]["body"]["tracked_sockets"], 0)
        self.assertEqual(self.backend.endpoints["redis"], "source")
        _, reply = self.call("app.persist-endpoints", postgres="target", redis="target")
        self.assertEqual(reply["code"], "REDIS_TARGET_NOT_CONFIRMED")

    def test_paused_redis_preroute_lost_reply_only_observes_effect(self):
        self.prepare_preroute()
        self.backend.control_lost_reply = True
        request, reply = self.call("app.control", control="redis.switch", control_epoch=0, endpoint="target")
        self.assertEqual(reply["code"], "UNKNOWN")
        self.assertEqual(self.rpc.handle(request)["code"], "OK")
        self.assertEqual(len(self.backend.posts), 1)
        self.assertEqual(self.backend.endpoints["redis"], "source")

    def test_preroute_failed_proof_replays_same_intent_after_condition_recovers(self):
        for fault in ("quiet", "status", "epoch", "stale"):
            with self.subTest(fault=fault):
                self.prepare_preroute()
                redis = self.backend.component_controls["redis"]
                if fault == "quiet":
                    redis["quiet"] = False
                if fault == "epoch":
                    redis["epoch"] = 1
                original = self.backend.app_control
                def control(name, *args):
                    if fault == "status" and name == "redis.status":
                        return {"http_status": 503, "body": {}}
                    if fault == "stale" and name == "runtime.status":
                        self.clock += 3
                    return original(name, *args)
                with mock.patch.object(self.backend, "app_control", side_effect=control):
                    request, reply = self.call("app.control", control="redis.switch", control_epoch=0, endpoint="target")
                self.assertEqual(reply["code"], "UNKNOWN")
                entry = self.store.read()["operations"][request["op_id"]]
                self.assertEqual(entry["phase"], "INTENT")
                self.assertNotIn("effect_started", entry)
                self.assertEqual(self.backend.posts, [])
                redis.update(quiet=True, epoch=0)
                self.assertEqual(self.rpc.handle(request)["code"], "OK")
                self.assertEqual(len(self.backend.posts), 1)
                self.backend.posts.clear()

    def test_paused_redis_preroute_rejects_incomplete_quiet_or_replication_proofs(self):
        changes = [
            lambda: self.backend.sample.update(in_recovery=True),
            lambda: self.backend.component_controls["runtime"].update(draining=True),
            lambda: self.backend.component_controls["runtime"]["tasks"].pop("usage_records"),
            lambda: self.backend.component_controls["runtime"]["tasks"]["usage_records"].update(pending=1),
            lambda: self.backend.component_controls["runtime"]["readiness"].update(quiet_controls=False),
            lambda: self.backend.component_controls["writers"].update(held=False),
            lambda: self.backend.component_controls["ingress"].update(paused=False),
            lambda: self.backend.component_controls["redis"].update(active_operations=True),
            lambda: self.backend.preroute_pair["target"].update(run_id="e" * 40),
            lambda: self.backend.preroute_pair["source"].update(run_id="e" * 40),
            lambda: self.backend.preroute_pair["target"].update(role="master"),
            lambda: self.backend.preroute_pair["target"].update(master_host="other"),
            lambda: self.backend.preroute_pair["target"].update(master_link_status="down"),
            lambda: self.backend.preroute_pair["target"].update(master_sync_in_progress="1"),
            lambda: self.backend.preroute_pair["source"].update(connected_slaves="2"),
            lambda: self.backend.preroute_pair["source"].update(slave1="ip=other,port=26379,state=online"),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.prepare_preroute()
                change()
                _, reply = self.call("app.control", control="redis.switch", control_epoch=0, endpoint="target")
                self.assertNotEqual(reply["code"], "OK")
                self.assertEqual(self.backend.posts, [])

    def test_paused_redis_preroute_rejects_instance_change_and_stale_sample(self):
        for fault in ("instance", "time"):
            with self.subTest(fault=fault):
                self.prepare_preroute()
                original = self.backend.redis_preroute_pair
                def sample():
                    if fault == "instance":
                        self.backend.component_controls["runtime"]["instance_id"] = "replacement"
                    else:
                        self.clock += 3
                    return original()
                with mock.patch.object(self.backend, "redis_preroute_pair", side_effect=sample):
                    _, reply = self.call("app.control", control="redis.switch", control_epoch=0, endpoint="target")
                self.assertNotEqual(reply["code"], "OK")
                self.assertEqual(self.backend.posts, [])

    def test_owner_epoch_and_replay_preserve_original(self):
        request, reply = self.call("app.control", control="ingress.pause", control_epoch=0)
        self.assertEqual(reply["code"], "OK")
        self.assertEqual(reply["epoch"], 1)
        self.assertEqual(self.rpc.handle(request), reply)
        changed = {**request, "control": "ingress.resume"}
        self.assertEqual(self.rpc.handle(changed)["code"], "OP_ID_REUSED")
        self.assertEqual(self.rpc.handle(request), reply)
        self.assertEqual(len(self.backend.posts), 1)
        bad = self.request(op_id="f" * 32, expected_epoch=0)
        self.assertEqual(self.rpc.handle(bad)["code"], "EPOCH_CONFLICT")
        self.refusal(lambda: self.rpc.handle({**bad, "owner": "b" * 32}), "WRONG_OWNER")
        reloaded = rpcmod.FileStore(self.cfg, self.scope, os.geteuid())
        self.assertEqual(reloaded.read(), self.store.read())
        events = [json.loads(line) for line in (self.scope / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["operations"] and event["operations"][0]["phase"] == "INTENT" for event in events))
        self.assertTrue(any(event["operations"] and event["operations"][0]["code"] == "OK" for event in events))
        self.assertEqual(stat.S_IMODE((self.scope / "state.json").stat().st_mode), 0o600)

    def test_arm_replay_after_result_loss_and_expiry(self):
        original = self.rpc._record_result
        self.rpc._record_result = mock.Mock(side_effect=OSError("lost result"))
        request, reply = self.call("pg.arm", final_lsn="F/FFFFFFFF", quiet_attestation_sha256="b" * 64)
        self.assertEqual(reply["code"], "UNKNOWN")
        saved = self.store.read()["pg"]["arm"]
        self.rpc._record_result = original
        replay = self.rpc.handle(request)
        self.assertEqual(replay["result"]["arm_id"], saved["arm_id"])
        self.clock += 7
        _, reply = self.call("pg.commit", arm_id=saved["arm_id"], source_fence_sha256="c" * 64)
        self.assertEqual(reply["code"], "PG_ARM_EXPIRED_OR_CANCELLED")
        self.assertEqual(self.backend.promotions, 0)

    def test_arm_quiet_persists_before_snapshot_outside_pg_lock_and_preserves_old_arm(self):
        self.prepare_quiesce()
        self.call("app.quiesce")
        original_snapshot = self.backend.app_quiet_snapshot
        held = False
        @contextlib.contextmanager
        def exclusive():
            nonlocal held
            self.assertFalse(held, "nested PostgreSQL lock")
            held = True
            try:
                yield
            finally:
                held = False
        def snapshot():
            self.assertFalse(held)
            saved = self.store.read()["pg"]["arm"]
            self.assertEqual(saved["status"], "ARMED")
            self.assertEqual(saved["quiet_attestation_sha256"], "b" * 64)
            with exclusive():
                return original_snapshot()
        self.backend.pg_locks = exclusive
        with mock.patch.object(self.backend, "app_quiet_snapshot", side_effect=snapshot):
            request, reply = self.call("pg.arm-quiet", final_lsn="10/0",
                quiet_attestation_sha256="b" * 64, quiet_nonce="c" * 32)
        self.assertEqual(reply["code"], "OK")
        self.assertEqual(reply["result"]["quiet_nonce"], request["quiet_nonce"])
        self.assertEqual(set(reply["result"]["quiet_snapshot"]),
                         {"app", "pg", "ingress", "writers", "redis", "postgres"})
        self.assertTrue(reply["result"]["quiet_snapshot"]["writers"]["held"])
        saved = self.store.read()["pg"]["arm"]
        self.assertNotIn("quiet_nonce", saved)
        self.assertNotIn("quiet_snapshot", saved)
        self.call("pg.cancel-arm", arm_id=saved["arm_id"])
        with mock.patch.object(self.backend, "app_quiet_snapshot", side_effect=AssertionError("old action changed")):
            _, old = self.call("pg.arm", final_lsn="10/0", quiet_attestation_sha256="b" * 64)
        self.assertEqual(old["code"], "OK")
        self.assertNotIn("quiet_snapshot", old["result"])
        self.assertNotIn("quiet_nonce", old["result"])

    def test_arm_quiet_cached_reply_cannot_adopt_new_nonce_or_refresh_expiry(self):
        self.prepare_quiesce()
        with mock.patch.object(self.backend, "app_quiet_snapshot", wraps=self.backend.app_quiet_snapshot) as snapshot:
            request, first = self.call("pg.arm-quiet", final_lsn="10/0",
                quiet_attestation_sha256="b" * 64, quiet_nonce="c" * 32)
            saved = self.store.read()["pg"]["arm"]
            self.clock += 100
            self.assertEqual(self.rpc.handle(request), first)
            conflict = self.rpc.handle({**request, "quiet_nonce": "d" * 32})
            self.assertEqual(conflict["code"], "OP_ID_REUSED")
            self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(self.store.read()["pg"]["arm"], saved)
        self.assertEqual(self.backend.promotions, 0)

    def test_arm_quiet_snapshot_failure_retains_cancellable_arm_and_no_guard_rearm(self):
        self.prepare_quiesce()
        with mock.patch.object(self.backend, "app_quiet_snapshot", side_effect=rpcmod.Uncertain("SAMPLE_LOST")) as snapshot:
            request, first = self.call("pg.arm-quiet", final_lsn="10/0",
                quiet_attestation_sha256="b" * 64, quiet_nonce="c" * 32)
            self.assertEqual(first["code"], "UNKNOWN")
            saved = self.store.read()["pg"]["arm"]
            self.clock += 100
            self.rpc.guard_reconcile()
            self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(self.store.read()["pg"]["arm"], saved)
        _, status = self.call("status")
        self.assertEqual(status["result"]["pg_arm"]["arm_id"], saved["arm_id"])
        _, cancelled = self.call("pg.cancel-arm", arm_id=saved["arm_id"])
        self.assertEqual(cancelled["code"], "OK")
        self.assertEqual(self.store.read()["pg"]["arm"]["status"], "CANCELLED")
        self.assertEqual(self.backend.promotions, 0)

    def test_arm_quiet_snapshot_time_limit_includes_arm_and_sample_work(self):
        self.prepare_quiesce()
        original = self.backend.app_quiet_snapshot
        def slow():
            self.clock += 2.01
            return original()
        with mock.patch.object(self.backend, "app_quiet_snapshot", side_effect=slow):
            _, reply = self.call("pg.arm-quiet", final_lsn="10/0",
                quiet_attestation_sha256="b" * 64, quiet_nonce="c" * 32)
        self.assertEqual(reply["code"], "QUIET_SNAPSHOT_SAMPLE_STALE")
        arm = self.store.read()["pg"]["arm"]
        self.assertEqual(arm["status"], "ARMED")
        _, cancelled = self.call("pg.cancel-arm", arm_id=arm["arm_id"])
        self.assertEqual(cancelled["code"], "OK")

    def test_cancel_prevents_late_commit(self):
        _, arm_id = self.arm()
        _, reply = self.call("pg.cancel-arm", arm_id=arm_id)
        self.assertEqual(reply["code"], "OK")
        _, reply = self.call("pg.commit", arm_id=arm_id, source_fence_sha256="c" * 64)
        self.assertEqual(reply["code"], "PG_ARM_EXPIRED_OR_CANCELLED")
        self.assertEqual(self.backend.promotions, 0)

    def test_status_exposes_cancellable_arm_and_accepted_intent_summary(self):
        arm_request, arm_id = self.arm()
        _, reply = self.call("status")
        self.assertEqual(set(reply["result"]["pg_arm"]), {"arm_id", "status", "expires_at", "op_id", "final_lsn"})
        self.assertEqual(reply["result"]["pg_arm"]["op_id"], arm_request["op_id"])
        self.assertIsNone(reply["result"]["pg_promotion_intent"])
        commit, _ = self.call("pg.commit", arm_id=arm_id, source_fence_sha256="c" * 64)
        _, reply = self.call("status")
        self.assertEqual(reply["result"]["pg_promotion_intent"], {"op_id": commit["op_id"], "accepted_at": 1000.0})
        self.assertNotIn("source_fence_sha256", reply["result"]["pg_promotion_intent"])

    def test_arm_rejects_behind_or_wrong_identity(self):
        self.backend.sample["replay_lsn"] = "F/FFFFFFFE"
        _, reply = self.call("pg.arm", final_lsn="10/0", quiet_attestation_sha256="b" * 64)
        self.assertEqual(reply["code"], "PG_REPLAY_BEHIND")
        self.backend.sample["system_identifier"] = "9999"
        _, reply = self.call("pg.arm", final_lsn="0/1", quiet_attestation_sha256="b" * 64)
        self.assertEqual(reply["code"], "PG_IDENTITY_CHANGED")

    def test_pg_intent_precedes_promotion_lost_reply_rolls_forward_after_expiry(self):
        _, arm_id = self.arm()
        def verify_before_promote():
            intent = self.store.read()["pg"]["promotion_intent"]
            self.assertIsNotNone(intent)
            self.assertEqual(set(intent), {"op_id", "source_fence_sha256", "final_lsn", "system_identifier", "source_timeline", "accepted_at"})
            self.assertEqual(self.backend.manifest["authority_mode"], "PROMOTION_INTENT")
            self.assertEqual(self.backend.manifest["promotion_intent"], intent)
        self.backend.before_promote = verify_before_promote
        self.backend.pg_lost_reply = True
        request, reply = self.call("pg.commit", arm_id=arm_id, source_fence_sha256="c" * 64)
        self.assertEqual(reply["code"], "UNKNOWN")
        self.assertEqual(self.store.read()["pg"]["authority"], "COMMIT_INTENT")
        self.clock += 100
        restarted = rpcmod.TargetRPC(self.cfg, self.backend, rpcmod.FileStore(self.cfg, self.scope, os.geteuid()), lambda: self.clock)
        restarted.guard_reconcile()
        self.assertEqual(restarted.handle(request)["code"], "OK")
        self.assertEqual(self.backend.promotions, 1)
        self.assertEqual(self.store.read()["pg"]["authority"], "TENCENT")
        self.assertEqual(self.backend.manifest["authority_mode"], "PRIMARY")
        self.assertEqual(restarted.handle(request)["result"]["source_fence_evidence"], "trusted-runner-attestation")

    def test_guard_recovers_ledger_intent_before_manifest_write(self):
        _, arm_id = self.arm()
        self.backend.manifest_lost_write = True
        request, reply = self.call("pg.commit", arm_id=arm_id, source_fence_sha256="c" * 64)
        self.assertEqual(reply["code"], "UNKNOWN")
        self.assertIsNotNone(self.store.read()["pg"]["promotion_intent"])
        self.assertEqual(self.backend.manifest["authority_mode"], "STANDBY")
        self.clock += 100
        self.rpc.guard_reconcile()
        self.assertEqual(self.rpc.handle(request)["code"], "OK")
        self.assertEqual(self.backend.promotions, 1)

    def test_cancel_after_commit_cannot_revoke_authority(self):
        _, arm_id = self.arm()
        self.backend.pg_lost_reply = True
        self.call("pg.commit", arm_id=arm_id, source_fence_sha256="c" * 64)
        _, reply = self.call("pg.cancel-arm", arm_id=arm_id)
        self.assertNotEqual(reply["code"], "OK")
        self.assertIsNotNone(self.store.read()["pg"]["promotion_intent"])
        self.rpc.guard_reconcile()
        self.assertEqual(self.store.read()["pg"]["authority"], "TENCENT")

    def test_busy_same_operation_never_duplicates_effect(self):
        request = self.request(op_id="d" * 32, action="pg.arm", final_lsn="F/FFFFFFFF", quiet_attestation_sha256="b" * 64)
        with self.store.operation_lock(request["op_id"]):
            reply = self.rpc.handle(request)
        self.assertEqual(reply["code"], "UNKNOWN")
        self.assertIsNone(self.store.read()["pg"]["arm"])
        self.assertEqual(self.rpc.handle(request)["code"], "OK")

    def test_simultaneous_epoch_admission_accepts_only_one(self):
        requests = [self.request(op_id=f"{100 + n:032x}", action="app.control", control="ingress.pause", control_epoch=0)
                    for n in range(2)]
        replies = []
        gate = threading.Barrier(2)
        def invoke(request):
            gate.wait()
            replies.append(self.rpc.handle(request))
        threads = [threading.Thread(target=invoke, args=(request,)) for request in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(reply["code"] for reply in replies), ["EPOCH_CONFLICT", "OK"])
        self.assertEqual(len(self.backend.posts), 1)

    def test_app_unknown_reply_observes_without_second_mutation(self):
        self.backend.control_lost_reply = True
        request, reply = self.call("app.control", control="ingress.pause", control_epoch=0)
        self.assertEqual(reply["code"], "UNKNOWN")
        replay = self.rpc.handle(request)
        self.assertEqual(replay["code"], "OK")
        self.assertTrue(replay["result"]["reconciled"])
        self.assertEqual(len(self.backend.posts), 1)

    def test_app_unproven_effect_stays_unknown(self):
        self.backend.endpoints["redis"] = "target"
        self.backend.control_lost_reply = True
        request, reply = self.call("app.control", control="redis.switch", control_epoch=0, endpoint="target")
        self.assertEqual(reply["code"], "UNKNOWN")
        self.backend.control["address"] = "127.0.0.1:9999"
        self.assertEqual(self.rpc.handle(request)["code"], "UNKNOWN")
        self.assertEqual(len(self.backend.posts), 1)

    def test_redis_switch_lost_reply_uses_pinned_target_address(self):
        self.backend.endpoints["redis"] = "target"
        self.backend.control["quiet"] = True
        self.backend.control_lost_reply = True
        request, reply = self.call("app.control", control="redis.switch", control_epoch=0, endpoint="target")
        self.assertEqual(reply["code"], "UNKNOWN")
        self.assertEqual(self.rpc.handle(request)["code"], "OK")
        self.assertEqual(len(self.backend.posts), 1)

    def test_persistent_endpoints_require_confirmed_authority_and_precede_switch(self):
        _, reply = self.call("app.persist-endpoints", postgres="target", redis="source")
        self.assertEqual(reply["code"], "PG_TARGET_NOT_CONFIRMED")
        _, reply = self.call("app.control", control="postgres.switch", control_epoch=0, endpoint="target")
        self.assertEqual(reply["code"], "ENDPOINT_NOT_PERSISTED")
        self.store.update(lambda state: state["pg"].update(authority="TENCENT"))
        request, reply = self.call("app.persist-endpoints", postgres="target", redis="source")
        self.assertEqual(reply["code"], "OK")
        self.assertEqual(self.rpc.handle(request), reply)
        _, reply = self.call("app.control", control="postgres.switch", control_epoch=0, endpoint="target")
        self.assertEqual(reply["code"], "OK")
        _, reply = self.call("app.persist-endpoints", postgres="source", redis="source")
        self.assertEqual(reply["code"], "ENDPOINT_ROLLBACK_FORBIDDEN")

    def test_redis_acceptance_unknown_and_refusal_keep_forward_recovery(self):
        self.backend.redis_lost_reply = True
        request, reply = self.call("redis.persist-primary")
        self.assertEqual(reply["code"], "UNKNOWN")
        self.assertEqual(self.store.read()["redis"]["authority"], "COMMIT_INTENT")
        self.backend.redis_refusal = True
        self.assertEqual(self.rpc.handle(request)["code"], "UNKNOWN")
        self.assertEqual(self.store.read()["operations"][request["op_id"]]["phase"], "INTENT")
        self.rpc.guard_reconcile()
        self.assertEqual(self.rpc.handle(request)["code"], "OK")
        self.assertEqual(self.store.read()["redis"]["authority"], "TENCENT")

    def test_redis_run_id_change_does_not_adopt_another_master(self):
        self.backend.redis_lost_reply = True
        request, _ = self.call("redis.persist-primary")
        self.backend.redis["run_id"] = "e" * 40
        self.assertEqual(self.rpc.handle(request)["code"], "UNKNOWN")
        self.assertEqual(self.backend.rewrites, 1)

    def test_files_reject_symlinks_modes_and_public_endpoint_downgrade(self):
        uid = os.geteuid()
        private = self.scope / "private"
        private.write_text("secret"); private.chmod(0o600)
        self.assertEqual(rpcmod.private_read(private, uid), b"secret")
        link = self.scope / "link"; link.symlink_to(private)
        with self.assertRaises((OSError, rpcmod.Refusal)):
            rpcmod.private_read(link, uid)
        private.chmod(0o644)
        self.refusal(lambda: rpcmod.private_read(private, uid), "UNTRUSTED_FILE")
        public = self.scope / "app"; public.mkdir(mode=0o755)
        path = public / "endpoints.json"
        path.write_bytes(rpcmod.canonical({"v": 1, "postgres": "source", "redis": "source"})); path.chmod(0o644)
        with mock.patch.object(rpcmod.os, "fsync", wraps=os.fsync) as sync:
            result = rpcmod.persist_endpoints({"v": 1, "postgres": "target", "redis": "source"}, path, uid)
        self.assertEqual(result["postgres"], "target")
        self.assertGreaterEqual(sync.call_count, 2)
        self.refusal(lambda: rpcmod.persist_endpoints({"v": 1, "postgres": "source", "redis": "source"}, path, uid),
                     "ENDPOINT_ROLLBACK_FORBIDDEN")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)


def large_ledger():
    """331 operations with the observed runtime-status history and nested shape."""
    body = {"instance_id": "pinned-instance", "draining": False, "serving": True,
            "active_requests": 0, "active_hijacked_connections": 0,
            "background_workers_enabled": False, "ready": True,
            "tasks": {name: {"active": 0, "pending": 0, "failed": 0, "dropped": 0,
                              "safe_to_stop": True} for name in rpcmod.REQUIRED_WRITERS},
            "readiness": {"postgres": True, "redis": True, "quiet_controls": True},
            "ingress": {"paused": False, "queued_requests": 0, "epoch": 2}}
    state = {"v": 1, "run": rpcmod.RUN, "owner": config()["owner"], "epoch": 2,
             "operations": {}, "pg": {"authority": "AWS", "arm": None, "promotion_intent": None},
             "redis": {"authority": "AWS"}}
    for index in range(331):
        request = {"v": 1, "run": rpcmod.RUN, "owner": state["owner"], "op_id": f"{index:032x}",
                   "action": "app.control", "expected_epoch": 2,
                   "control": "runtime.status" if index < 279 else "writers.status", "control_epoch": 0}
        state["operations"][request["op_id"]] = {
            "request": request, "request_sha256": rpcmod.sha(request), "phase": "COMPLETE",
            "response": {"v": 1, "run": rpcmod.RUN, "op_id": request["op_id"], "code": "OK",
                         "outcome": "COMPLETE", "epoch": 2, "body": copy.deepcopy(body)}}
    padding = max(0, (691 * 1024 - len(rpcmod.canonical(state))) // 331)
    for op in state["operations"].values():
        op["response"]["body"]["instance_id"] += "x" * padding
    return state


class FileStoreHistoryTests(RequestAssertions, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rpc-ledger-test-", dir=pathlib.Path.home())
        self.addCleanup(self.temp.cleanup)
        self.scope = pathlib.Path(self.temp.name)
        self.uid = os.geteuid()
        self.store = rpcmod.FileStore(config(), self.scope, self.uid)

    def seed(self, state):
        rpcmod.atomic_json(self.scope / "state.json", state, self.uid)

    def events(self):
        return [json.loads(line) for line in (self.scope / "events.jsonl").read_bytes().splitlines()]

    def test_large_history_nested_update_preserves_all_rows_and_exact_audit_hash(self):
        state = large_ledger()
        self.assertGreater(len(rpcmod.canonical(state)), 690 * 1024)
        self.seed(state)
        changed_id = f"{330:032x}"
        def change(current):
            op = current["operations"][changed_id]
            op["response"]["body"]["tasks"]["usage_records"]["pending"] = 1
            op["phase"] = "INTENT"
            return op
        with mock.patch.object(rpcmod, "canonical", wraps=rpcmod.canonical) as encode, \
             mock.patch.object(rpcmod, "sha", wraps=rpcmod.sha) as digest:
            result = self.store.update(change)
        # History is never individually JSON-encoded or hashed on the fast path.
        operation_encodes = [call.args[0] for call in encode.call_args_list
                             if isinstance(call.args[0], dict) and "request_sha256" in call.args[0]]
        self.assertEqual(len(operation_encodes), 2)
        self.assertTrue(all(op["request"]["op_id"] == changed_id for op in operation_encodes))
        self.assertTrue(all(isinstance(call.args[0], bytes) for call in digest.call_args_list))
        saved = self.store.read()
        self.assertEqual(set(saved["operations"]), set(state["operations"]))
        for key, op in state["operations"].items():
            if key != changed_id:
                self.assertEqual(rpcmod.canonical(saved["operations"][key]), rpcmod.canonical(op))
        event = self.events()[-1]
        self.assertEqual([op["op_id"] for op in event["operations"]], [changed_id])
        self.assertEqual(event["state_sha256"], rpcmod.sha((self.scope / "state.json").read_bytes()[:-1]))
        result["phase"] = "caller-mutated"
        self.assertEqual(self.store.read()["operations"][changed_id]["phase"], "INTENT")
        self.store.update(lambda current: None)
        self.assertEqual(self.events()[-1]["operations"], [])

    def test_canonical_change_semantics_include_numeric_types_signed_zero_and_order(self):
        class JSONList(list):
            pass
        cases = [(1, True, True), (1, 1.0, True), (-0.0, 0.0, True),
                 ({"a": 1, "b": 2}, {"b": 2, "a": 1}, False),
                 ([1, 2], (1, 2), False), ([1, 2], JSONList([1, 2]), False)]
        for before, after, changed in cases:
            with self.subTest(before=before, after=after):
                state = self.store._read()
                op_id = "a" * 32
                request = self.request(op_id=op_id)
                state["operations"] = {op_id: {"request": request, "request_sha256": rpcmod.sha(request),
                    "phase": "COMPLETE", "response": {"code": "OK", "payload": before}}}
                self.seed(state)
                self.store.update(lambda current: current["operations"][op_id]["response"].update(payload=after))
                self.assertEqual(bool(self.events()[-1]["operations"]), changed)

    def test_large_history_unknown_retry_is_idempotent_without_second_effect(self):
        history = large_ledger(); self.seed(history)
        backend = FakeBackend(config()); backend.control_lost_reply = True
        rpc = rpcmod.TargetRPC(config(), backend, self.store)
        request = self.request(op_id="f" * 32, expected_epoch=2, action="app.control",
                               control="ingress.pause", control_epoch=0)
        self.assertEqual(rpc.handle(request)["code"], "UNKNOWN")
        reply = rpc.handle(request)
        self.assertEqual(reply["code"], "OK")
        self.assertEqual(rpc.handle(request), reply)
        self.assertEqual(len(backend.posts), 1)
        saved = self.store.read()
        self.assertEqual(len(saved["operations"]), 332)
        for key, value in history["operations"].items():
            self.assertEqual(saved["operations"][key], value)

    def test_trust_invalid_json_and_audit_failure_keep_existing_durability_order(self):
        self.seed(self.store._read())
        path = self.scope / "state.json"; original = path.read_bytes()
        path.chmod(0o644)
        self.refusal(lambda: self.store.update(lambda state: None), "UNTRUSTED_FILE")
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.store.update(lambda state: state.update(invalid=float("nan")))
        self.assertEqual(path.read_bytes(), original)
        event = self.scope / "events.jsonl"; event.write_bytes(b""); event.chmod(0o644)
        self.refusal(lambda: self.store.update(lambda state: state["redis"].update(authority="TENCENT")),
                     "UNTRUSTED_EVENT_FILE")
        self.assertEqual(self.store.read()["redis"]["authority"], "TENCENT")
        self.assertEqual(event.read_bytes(), b"")

    def test_atomic_json_default_return_is_unchanged_and_opt_in_bytes_match_disk(self):
        path = self.scope / "state.json"; state = self.store._read()
        self.assertIsNone(rpcmod.atomic_json(path, state, self.uid))
        raw = rpcmod.atomic_json(path, state, self.uid, return_bytes=True)
        self.assertEqual(raw, rpcmod.canonical(state))
        self.assertEqual(path.read_bytes(), raw + b"\n")


class AdoptionBackendTests(RequestAssertions, unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.cfg["app"]["control_port"] = 18093
        self.backend = rpcmod.LocalBackend(self.cfg)
        self.old_id = "6" * 64
        self.old_config = {"Env": ["old"]}
        self.old = {"Id": self.old_id, "Image": self.cfg["app"]["image_id"], "Config": self.old_config,
                    "State": {"Running": False, "Status": "exited", "Pid": 0, "ExitCode": 0, "OOMKilled": False},
                    "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        self.backend.docker_json = mock.Mock(return_value=self.old)
        self.backend.resource = mock.Mock()
        self.backend._app_token = mock.Mock(return_value=b"private-test-token")
        self.backend.endpoint_state = mock.Mock(return_value={"v": 1, "postgres": "source", "redis": "source"})
        old_request = self.request(op_id="1" * 32, action="app.quiesce", expected_epoch=12)
        self.latest = {"request": old_request, "request_sha256": rpcmod.sha(old_request),
                       "phase": "COMPLETE", "instance_id": "old-instance", "cancelled": True}
        self.baseline = {"v": 1, "run": rpcmod.RUN, "owner": self.cfg["owner"], "epoch": 14,
                         "operations": {old_request["op_id"]: self.latest},
                         "pg": {"authority": "AWS", "arm": None, "promotion_intent": None},
                         "redis": {"authority": "AWS"}}
        self.request_value = self.request(op_id="2" * 32, action="app.restore", expected_epoch=14)
        self.runtime = {"instance_id": "new-instance"}
        self.ledger = copy.deepcopy(self.baseline)
        self.ledger["epoch"] = 15
        self.ledger["operations"][self.request_value["op_id"]] = {
            "request": self.request_value, "request_sha256": rpcmod.sha(self.request_value), "phase": "INTENT"}
        old_identity = {"container_id": self.old_id, "image_id": self.cfg["app"]["image_id"],
                        "rootfs_diffids": self.cfg["app"]["rootfs_diffids"], "config_sha256": rpcmod.sha(self.old_config)}
        new_identity = {"container_id": self.cfg["app"]["container_id"], "image_id": self.cfg["app"]["image_id"],
                        "rootfs_diffids": self.cfg["app"]["rootfs_diffids"],
                        "config_sha256": self.cfg["app"]["config_sha256"],
                        "control_token_sha256": rpcmod.sha(b"private-test-token")}
        self.rolling = {"v": 1, "owner": self.cfg["owner"], "phase": "COMPLETE", "intent": None,
                        "old_identity": old_identity, "new_identity": new_identity,
                        "adoption_receipt": {"v": 1, "owner": self.cfg["owner"], "old_container_id": self.old_id,
                            "new_app": new_identity, "control_port": 18093,
                            "endpoint_directory": str(rpcmod.PREFIX / "app"),
                            "endpoints": {"v": 1, "postgres": "source", "redis": "source"},
                            "postgres_authority": "AWS", "redis_authority": "AWS", "old_exited_cleanly": True,
                            "new_process_failed": 0, "new_process_dropped": 0}}
        self.baseline_raw = rpcmod.canonical(self.baseline)+b"\n"
        self.source = {"owner": self.cfg["owner"], "run": rpcmod.RUN, "window": None, "controller_stopped": True,
                       "pg": "AWS", "redis": "AWS", "target_ledger_sha256": rpcmod.sha(self.baseline_raw)}
        self.receipt = {"v": 1, "run": rpcmod.RUN, "owner": self.cfg["owner"],
                        "old_instance_id": "old-instance", "new_instance_id": "new-instance",
                        "old_container_id": self.old_id, "new_container_id": self.cfg["app"]["container_id"],
                        "new_app_config_sha256": self.cfg["app"]["config_sha256"], "new_control_port": 18093,
                        "new_control_token_sha256": rpcmod.sha(b"private-test-token"),
                        "controller_config_raw_sha256": rpcmod.sha(rpcmod.canonical(self.cfg)),
                        "ledger_raw_sha256": rpcmod.sha(self.baseline_raw), "ledger_epoch": 14, "operation_floor": 14,
                        "source_closed_sha256": rpcmod.sha(rpcmod.canonical(self.source)),
                        "rolling_complete_sha256": rpcmod.sha(rpcmod.canonical(self.rolling)),
                        "old_clean_exit": {"container_id": self.old_id, "exit_code": 0, "oom_killed": False,
                                           "pid": 0, "restart_policy": "no"}}

    def read(self, path, **kwargs):
        return {rpcmod.ADOPTION_SCOPE/"controller-adoption.json": rpcmod.canonical(self.receipt),
                rpcmod.ADOPTION_SCOPE/"controller-ledger-before.json": self.baseline_raw,
                rpcmod.ADOPTION_SCOPE/"state.json": rpcmod.canonical(self.rolling),
                rpcmod.ADOPTION_SCOPE/"source-controller-closed.json": rpcmod.canonical(self.source),
                rpcmod.SCOPE/"config.json": rpcmod.canonical(self.cfg)}[path]

    def validate(self):
        with mock.patch.object(rpcmod, "private_read", side_effect=self.read):
            return self.backend.app_restore_adoption(self.request_value, self.runtime, self.latest, self.ledger)

    def test_complete_root_receipt_authorizes_only_new_restore_without_writes(self):
        before = copy.deepcopy(self.ledger)
        with mock.patch.object(rpcmod, "atomic_json", side_effect=AssertionError("adoption must not write")):
            self.assertTrue(self.validate())
        self.assertEqual(self.ledger, before)
        self.backend.docker_json.assert_called_once_with("container", self.old_id)
        self.backend.resource.assert_called_once_with("app")

    def test_receipt_identity_config_token_or_proof_drift_refuses(self):
        for field, value in [("owner", "0" * 32), ("new_instance_id", "elsewhere"),
                             ("old_instance_id", "elsewhere"), ("new_container_id", "7" * 64),
                             ("new_app_config_sha256", "8" * 64), ("new_control_port", 18092),
                             ("new_control_token_sha256", "9" * 64), ("controller_config_raw_sha256", "0" * 64),
                             ("ledger_raw_sha256", "0" * 64), ("source_closed_sha256", "0" * 64),
                             ("rolling_complete_sha256", "0" * 64), ("operation_floor", 13), ("ledger_epoch", True)]:
            original = copy.deepcopy(self.receipt)
            self.receipt[field] = value
            with self.subTest(field=field), self.assertRaises(rpcmod.Refusal):
                self.validate()
            self.receipt = original

    def test_missing_or_untrusted_root_receipt_never_authorizes(self):
        for error in (FileNotFoundError(), rpcmod.Refusal("UNTRUSTED_FILE")):
            with self.subTest(error=type(error).__name__), mock.patch.object(rpcmod, "private_read", side_effect=error):
                with self.assertRaises(type(error)):
                    self.backend.app_restore_adoption(self.request_value, self.runtime, self.latest, self.ledger)
        self.backend.docker_json.assert_not_called()

    def test_history_rewrite_existing_operation_or_wrong_action_cannot_use_receipt(self):
        original_request, original_ledger = copy.deepcopy(self.request_value), copy.deepcopy(self.ledger)
        faults = ("history", "old-op", "bound", "floor", "pg-route", "redis-route", "authority")
        for fault in faults:
            self.request_value, self.ledger = copy.deepcopy(original_request), copy.deepcopy(original_ledger)
            if fault == "history":
                self.ledger["operations"]["1" * 32]["instance_id"] = "new-instance"
            elif fault == "old-op":
                self.request_value = self.latest["request"]
            elif fault == "bound":
                self.ledger["operations"]["2" * 32]["instance_id"] = "old-instance"
            elif fault == "floor":
                self.request_value["expected_epoch"] = 13
            elif fault in ("pg-route", "redis-route"):
                self.request_value["action"] = "pg.route" if fault == "pg-route" else "redis.preroute"
            else:
                self.ledger["pg"]["authority"] = "TENCENT"
            with self.subTest(fault=fault), self.assertRaises(rpcmod.Refusal):
                self.validate()

    def test_incomplete_rolling_source_or_old_exit_is_not_sufficient(self):
        for fault in ("rolling", "source", "exit", "oom", "restart", "exit-receipt"):
            self.setUp()
            if fault == "rolling":
                self.rolling["phase"] = "DRAINING"
                self.receipt["rolling_complete_sha256"] = rpcmod.sha(rpcmod.canonical(self.rolling))
            elif fault == "source":
                self.source["controller_stopped"] = False
                self.receipt["source_closed_sha256"] = rpcmod.sha(rpcmod.canonical(self.source))
            elif fault == "exit":
                self.old["State"]["ExitCode"] = 1
            elif fault == "oom":
                self.old["State"]["OOMKilled"] = True
            elif fault == "restart":
                self.old["HostConfig"]["RestartPolicy"]["Name"] = "always"
            else:
                self.receipt["old_clean_exit"]["exit_code"] = 1
            with self.subTest(fault=fault), self.assertRaises(rpcmod.Refusal):
                self.validate()


class BackendTests(RequestAssertions, unittest.TestCase):
    def test_quiet_snapshot_checks_app_once_reuses_one_private_token_and_fixed_gets(self):
        backend = rpcmod.LocalBackend(config())
        backend.resource = mock.Mock()
        backend._app_token = mock.Mock(return_value=b"private-token-never-returned")
        backend._app_request = mock.Mock(return_value={"http_status": 200, "body": {"instance_id": "same"}})
        backend.pg_locks = mock.Mock(return_value=contextlib.nullcontext())
        backend.pg_manifest = mock.Mock()
        backend.pg_sample = mock.Mock(return_value={"system_identifier": "12345", "in_recovery": True, "replay_lsn": "1/200", "wal_timeline": None})
        result = backend.app_quiet_snapshot()
        backend.resource.assert_called_once_with("app")
        backend._app_token.assert_called_once_with()
        backend.pg_manifest.assert_called_once_with()
        backend.pg_sample.assert_called_once_with()
        self.assertEqual([call.args for call in backend._app_request.call_args_list], [
            (name, b"private-token-never-returned", 0, None) for name in
            ("runtime.status", "ingress.status", "writers.status", "redis.status", "postgres.status", "runtime.status")])
        self.assertTrue(all(rpcmod.CONTROLS[call.args[0]][0] == "GET" for call in backend._app_request.call_args_list))
        self.assertEqual(result["pg"]["replay_lsn"], "1/200")
        self.assertNotIn("private-token", json.dumps(result))

    def test_preroute_reads_only_two_fixed_ports_in_pinned_container(self):
        cfg = config()
        backend = rpcmod.LocalBackend(cfg)
        backend.resource = mock.Mock()
        backend.command = mock.Mock(return_value=b"# Server\r\nrun_id:fixed\r\n# Replication\r\nrole:slave\r\n# Modules\r\nmodule:name=one\r\nmodule:name=two\r\n")
        self.assertEqual(backend.redis_preroute_pair()["target"]["role"], "slave")
        self.assertEqual([call.args[0] for call in backend.command.call_args_list], [
            ["/usr/bin/docker", "exec", cfg["redis"]["container_id"], "redis-cli", "--raw", "-h", "127.0.0.1", "-p", port, "INFO"]
            for port in ("16379", "26379")])
        backend.resource.assert_called_once_with("redis")
        backend.command.return_value = b"# Replication\r\nrole:slave\r\nrole:master\r\n"
        self.refusal(backend.redis_preroute_pair, "REDIS_DUPLICATE_INFO_FIELD")

    def test_preroute_ignores_legitimate_module_repetitions_but_pins_identity_sections(self):
        backend = rpcmod.LocalBackend(config())
        backend.resource = mock.Mock()
        original = b"# Server\r\nrun_id:fixed\r\n# Replication\r\nrole:slave\r\nmaster_replid:pinned\r\n# Modules\r\nmodule:one\r\nmodule:two\r\nrole:master\r\n"
        backend.command = mock.Mock(return_value=original)
        for body in backend.redis_preroute_pair().values():
            self.assertEqual(body, {"run_id": "fixed", "role": "slave", "master_replid": "pinned"})
        for duplicate in (b"# Server\r\nrun_id:one\r\nrun_id:two\r\n", b"# Replication\r\nrole:slave\r\nrole:master\r\n"):
            backend.command.return_value = duplicate
            self.refusal(backend.redis_preroute_pair, "REDIS_DUPLICATE_INFO_FIELD")

    def test_resource_identity_checks_pin_image_config_and_runtime(self):
        cfg = config()
        actual = {"Id": cfg["app"]["container_id"], "Image": cfg["app"]["image_id"], "Config": {"Env": []},
                  "State": {"Running": True}, "HostConfig": {"RestartPolicy": {"Name": "no"}, "NetworkMode": "host"}}
        cfg["app"]["config_sha256"] = rpcmod.sha(actual["Config"])
        image = {"Id": cfg["app"]["image_id"], "RootFS": {"Layers": cfg["app"]["rootfs_diffids"]}}
        backend = rpcmod.LocalBackend(cfg)
        backend.docker_json = mock.Mock(side_effect=lambda kind, name: image if kind == "image" else actual)
        self.assertEqual(backend.resource("app"), actual)
        actual["Id"] = "9" * 64
        self.refusal(lambda: backend.resource("app"), "RESOURCE_IDENTITY_CHANGED")
        actual["Id"] = cfg["app"]["container_id"]; actual["HostConfig"]["RestartPolicy"]["Name"] = "always"
        self.refusal(lambda: backend.resource("app"), "RESOURCE_NOT_RUNNING_OR_RESTARTABLE")

    def test_fixed_pg_argv_and_no_request_controlled_command(self):
        backend = rpcmod.LocalBackend(config())
        backend.command = mock.Mock(return_value=b"")
        backend.pg_promote()
        self.assertEqual(backend.command.call_args.args[0], ["/usr/bin/docker", "exec", "--user", "postgres",
            config()["pg"]["container_id"], "pg_ctl", "-D", rpcmod.PG_DATA, "promote", "-W"])
        backend.redis_command("PING")
        self.assertEqual(backend.command.call_args.args[0][2], config()["redis"]["container_id"])
        with mock.patch.object(rpcmod.subprocess, "run", side_effect=__import__("subprocess").TimeoutExpired("fixed", 2)):
            self.refusal(lambda: rpcmod.LocalBackend(config()).command(["/usr/bin/docker", "version"]), "COMMAND_OUTCOME_UNKNOWN")

    def test_control_204_and_private_bearer_never_in_response(self):
        backend = rpcmod.LocalBackend(config()); backend.resource = mock.Mock()
        reply = mock.Mock(status=204); reply.read.return_value = b""
        conn = mock.Mock(); conn.getresponse.return_value = reply
        with mock.patch.object(rpcmod, "private_read", return_value=b"A" * 64), \
             mock.patch.object(rpcmod.http.client, "HTTPConnection", return_value=conn):
            result = backend.app_control("ingress.resume", 7)
        self.assertEqual(result, {"http_status": 204, "body": {}})
        self.assertEqual(conn.request.call_args.args[:2], ("POST", "/data/ingress/resume"))
        self.assertEqual(json.loads(conn.request.call_args.kwargs["body"]), {"expected_epoch": 7})
        self.assertNotIn("A" * 64, json.dumps(result))

    def test_redis_inspect_and_rewrite_never_role_change(self):
        backend = rpcmod.LocalBackend(config()); backend.resource = mock.Mock()
        info = "\r\n".join(["run_id:" + "a" * 40, "role:master", "master_replid:" + "b" * 40,
              "config_file:/data/migration.conf", "master_repl_offset:42", "second_repl_offset:-1",
              "master_failover_state:no-failover", "db0:keys=2,expires=1,avg_ttl=1000"])
        backend.redis_command = mock.Mock(side_effect=lambda *argv: info if argv == ("INFO",) else "OK")
        backend.redis_config = mock.Mock(return_value=(b"port 26379\n", (999, 999, 0o644)))
        result = backend.redis_persist_primary()
        self.assertEqual(result["master_repl_offset"], 42)
        self.assertEqual(result["keyspace_sha256"], rpcmod.sha({"db0": "keys=2,expires=1,avg_ttl=1000"}))
        self.assertTrue(result["persisted"])
        self.assertEqual(backend.redis_command.call_args_list[1].args, ("CONFIG", "REWRITE"))
        backend.redis_config.return_value = (b"replicaof old 6379\n", (999, 999, 0o644))
        self.refusal(backend.redis_persist_primary, "REDIS_CONFIG_STILL_REPLICA")
        self.assertFalse(any("REPLICAOF" in str(call).upper() or "FAILOVER" in str(call).upper()
                             for call in backend.redis_command.call_args_list))


if __name__ == "__main__":
    unittest.main()
