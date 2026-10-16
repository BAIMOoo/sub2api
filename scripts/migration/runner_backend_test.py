"""Offline runner-backend contracts and lost-response fault tests."""
import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("runner_backend", pathlib.Path(__file__).with_name("runner_backend.py"))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
OWNER = "ab" * 16
CONFIG = {"v": 1, "run": m.RUN, "owner": OWNER, "machine_id_sha256": "a" * 64,
          "initial_target_epoch": 0, "ssh_key_sha256": "b" * 64, "known_hosts_sha256": "c" * 64}


class MemoryStore:
    def __init__(self):
        self.config = copy.deepcopy(CONFIG)
        self.transport = {"v": 1, "run": m.RUN, "owner": OWNER, "target_epoch": 0, "operations": {}, "controls": {}, "sources": {}}
        self.snapshots, self.engine, self.attestation = [], None, None
        self.locked = False

    def read(self, kind):
        return copy.deepcopy(self.config if kind == "config" else self.transport)

    def update_transport(self, function):
        assert not self.locked
        self.locked = True
        try:
            result = function(self.transport)
            self.snapshots.append(copy.deepcopy(self.transport))
            return copy.deepcopy(result)
        finally:
            self.locked = False

    def save_state(self, value):
        self.engine = copy.deepcopy(value)

    def attest(self, value):
        self.attestation = copy.deepcopy(value)


class FakeSource:
    def __init__(self):
        self.m = {"owner": OWNER, "machine_id_sha256": "a" * 64}
        self.b = types.SimpleNamespace(boot_id=lambda: "boot-1")
        self.calls = []
        self.phase = "complete"
        self.error = None

    def _identity(self):
        pass

    def __getattr__(self, method):
        def call(*args, **fields):
            self.calls.append((method, args, fields))
            if self.error:
                raise self.error
            return {"fresh": len(self.calls)} if method in m.SOURCE_READS else {"phase": self.phase, "stopped": self.phase == "complete"}
        return call


class FakeTransport:
    def __init__(self, store):
        self.store = store
        self.epoch = 0
        self.operations = {}
        self.calls = []
        self.timeout_after = None
        self.transform = None
        self.runtime = {"instance_id": "migration-app", "ready": True, "serving": True, "draining": False, "active_requests": 0,
                        "active_hijacked_connections": 0, "background_workers_enabled": False,
                        "readiness": {"postgres": True, "redis": True, "writer_coverage": True, "quiet_controls": True},
                        "tasks": {name: {"active": 0, "pending": 0, "failed": 0, "dropped": 0} for name in m.REQUIRED_WRITERS}}
        self.bodies = {"runtime": self.runtime, "ingress": {"epoch": 1, "paused": False, "quiet": True},
                       "writers": {"epoch": 1, "held": False, "quiet": False},
                       "redis": {"epoch": 1, "paused": False, "quiet": False, "active_operations": 0},
                       "postgres": {"epoch": 1, "paused": False, "active_operations": 0, "endpoint": "source"},
                       "background": {"epoch": 1, "activated": False}}
        self.runtime["ingress"] = self.bodies["ingress"]
        self.closed = False
        self.code = None
        self.reply_204 = False

    def response(self, request, code="OK", result=None):
        result = {"ok": code == "OK", "code": code, "run": m.RUN, "owner": OWNER, "op_id": request["op_id"],
                  "action": request["action"], "epoch": self.epoch, "phase": "AWS", "result": result or {}}
        return result

    def request(self, request):
        assert not self.store.locked, "network must not run under filesystem lock"
        if request["action"] == "app.observe":
            assert request["op_id"] not in self.store.transport["operations"]
            self.calls.append(copy.deepcopy(request))
            code = self.code or ("OK" if request["expected_epoch"] == self.epoch else "EPOCH_CONFLICT")
            result = {"http_status": 200, "body": copy.deepcopy(self.bodies[request["control"].split(".")[0]])}
            reply = self.response(request, code, result)
            return m.canonical(self.transform(reply) if self.transform else reply)
        assert self.store.transport["operations"][request["op_id"]]["request"] == request, "intent must be saved before transport"
        self.calls.append(copy.deepcopy(request))
        previous = self.operations.get(request["op_id"])
        if previous:
            assert previous[0] == request, "remote requires exact replay"
            return m.canonical(previous[1])
        if request["expected_epoch"] != self.epoch:
            return m.canonical(self.response(request, "EPOCH_CONFLICT"))
        readonly = m.RunnerBackend._is_read(request["action"], request)
        if not readonly:
            self.epoch += 1
        result = {}
        if request["action"] == "app.control":
            component, operation = request["control"].split(".")
            body = self.bodies[component]
            if operation != "status":
                if body["epoch"] != request["control_epoch"]:
                    return m.canonical(self.response(request, "CONTROL_EPOCH_CONFLICT"))
                if operation in ("pause", "resume"):
                    field = "held" if component == "writers" else "paused"
                    body[field] = operation == "pause"
                    body["quiet"] = operation == "pause"
                if operation == "switch":
                    body["endpoint"] = request["endpoint"]
                body["epoch"] += 1
            result = {"http_status": 204 if self.reply_204 and operation == "resume" else 200,
                      "body": {} if self.reply_204 and operation == "resume" else copy.deepcopy(body)}
        elif request["action"] == "status":
            result = {"pg_authority": "AWS", "redis_authority": "AWS", "pg": {"replay_lsn": "1/200"},
                      "app": {"http_status": 200, "body": copy.deepcopy(self.runtime)}}
        elif request["action"] == "app.quiet-snapshot":
            result = {"pg": {"replay_lsn": "1/200"}, "app": {"http_status": 200, "body": copy.deepcopy(self.runtime)},
                      **{key: copy.deepcopy(self.bodies[key]) for key in ("ingress", "writers", "redis", "postgres")}}
        elif request["action"] == "pg.arm":
            result = {"arm_id": "d" * 32}
        elif request["action"] == "redis.inspect":
            result = {"role": "slave", "run_id": "c" * 40}
        reply = self.response(request, self.code or "OK", result)
        self.operations[request["op_id"]] = (copy.deepcopy(request), copy.deepcopy(reply))
        if self.timeout_after is not None and self.timeout_after in (request["action"], request.get("control")):
            self.timeout_after = None
            raise m.Unknown("injected-response-loss")
        if self.transform:
            reply = self.transform(copy.deepcopy(reply))
        return m.canonical(reply)

    def close(self):
        self.closed = True
        return {"closed": True, "socket_present": False}


class RunnerBackendTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.source = FakeSource()
        self.transport = FakeTransport(self.store)
        self.backend = m.RunnerBackend(CONFIG, self.store, self.transport, self.source, now=lambda: 1000.0)

    def assert_refuses(self, reason, function, kind=m.Refusal):
        with self.assertRaises(kind) as error:
            function()
        self.assertEqual(str(error.exception), reason)

    def test_default_command_is_offline(self):
        output = subprocess.check_output([sys.executable, str(pathlib.Path(m.__file__))])
        self.assertFalse(json.loads(output)["production_actions"])

    def test_closed_configuration(self):
        config = {**CONFIG, "host": "evil.example"}
        self.assert_refuses("CONFIG_FIELDS", lambda: m.RunnerBackend(config, self.store, self.transport, self.source))

    def test_backend_requires_same_source_owner(self):
        self.source.m["owner"] = "f" * 32
        self.assert_refuses("SOURCE_CONFIG_IDENTITY", lambda: m.RunnerBackend(CONFIG, self.store, self.transport, self.source))

    def test_save_engine_never_overwrites_transport(self):
        previous = copy.deepcopy(self.store.transport)
        state = {"v": 1, "run": m.RUN, "owner": OWNER, "pg": "AWS"}
        self.backend.save(state)
        self.assertEqual(self.store.engine, state)
        self.assertEqual(self.store.transport, previous)

    def test_save_identity_guard(self):
        self.assert_refuses("ENGINE_STATE_IDENTITY", lambda: self.backend.save({"v": 1, "run": m.RUN, "owner": "a" * 32}))

    def test_source_reads_never_cache_or_take_op_id(self):
        first = self.backend.source("source:inspect", "inspect")
        second = self.backend.source("source:inspect", "inspect")
        self.assertNotEqual(first, second)
        self.assertEqual(self.source.calls[0], ("inspect", (), {}))
        self.assertFalse(self.store.transport["sources"])

    def test_observer_reads_fresh_without_any_ledger_or_epoch_writes(self):
        before = copy.deepcopy(self.store.transport)
        first = self.backend.observe_control("runtime.status")
        self.transport.runtime["active_requests"] = 1
        second = self.backend.observe_control("runtime.status")
        self.assertEqual(first["active_requests"], 0)
        self.assertEqual(second["active_requests"], 1)
        self.assertEqual(self.store.transport, before)
        self.assertEqual(self.store.snapshots, [])
        self.assertEqual(self.transport.operations, {})
        self.assertNotEqual(self.transport.calls[0]["op_id"], self.transport.calls[1]["op_id"])
        self.assertEqual(set(self.transport.calls[0]), m.rpc_module.BASE_FIELDS | {"control"})

    def test_observer_rejects_all_mutations_before_transport(self):
        for name, (method, _) in m.rpc_module.CONTROLS.items():
            if method != "GET":
                self.assert_refuses("OBSERVATION_REQUIRES_STATUS", lambda: self.backend.observe_control(name))
        self.assert_refuses("OBSERVATION_REQUIRES_STATUS", lambda: self.backend.observe_control("unknown.status"))
        self.assert_refuses("USE_OBSERVE_CONTROL", lambda: self.backend.rpc("x", "app.observe", control="runtime.status"))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.store.snapshots, [])

    def test_observer_epoch_conflict_cannot_adopt_epoch_or_retry(self):
        self.transport.epoch = 1
        self.assert_refuses("OBSERVATION_NOT_CONFIRMED", lambda: self.backend.observe_control("runtime.status"))
        self.assertEqual(self.store.transport["target_epoch"], 0)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.store.snapshots, [])
        self.transport.epoch = 0
        self.transport.transform = lambda reply: {**reply, "epoch": 1}
        self.assert_refuses("OBSERVATION_NOT_CONFIRMED", lambda: self.backend.observe_control("runtime.status"))

    def test_observer_validates_identity_and_body_without_writes_on_failure(self):
        mutations = ({"owner": "f"*32}, {"run": "other"}, {"op_id": "f"*32},
                     {"action": "app.control"}, {"epoch": -1}, {"ok": False},
                     {"result": {"http_status": 503, "body": {"error": "unready"}}},
                     {"result": {"http_status": 200, "body": {}}}, {"phase": "INVALID"})
        for fields in mutations:
            with self.subTest(fields=fields):
                self.transport.transform = lambda reply: {**reply, **fields}
                with self.assertRaises((m.Refusal, m.Unknown)):
                    self.backend.observe_control("runtime.status")
        self.assertEqual(self.store.snapshots, [])
        self.assertFalse(self.store.transport["operations"])

    def test_observer_timeout_or_unknown_never_creates_reconciliation_intent(self):
        with mock.patch.object(self.transport, "request", side_effect=subprocess.TimeoutExpired(["ssh"], 3)):
            self.assert_refuses("RPC_TRANSPORT_UNKNOWN", lambda: self.backend.observe_control("runtime.status"), m.Unknown)
        self.transport.code = "UNKNOWN"
        self.assert_refuses("OBSERVATION_NOT_CONFIRMED", lambda: self.backend.observe_control("runtime.status"))
        self.assertEqual(self.store.snapshots, [])

    def test_observer_validates_local_transport_identity_before_network(self):
        self.store.transport["owner"] = "f"*32
        self.assert_refuses("TRANSPORT_LEDGER_IDENTITY", lambda: self.backend.observe_control("runtime.status"))
        self.assertEqual(self.transport.calls, [])

    def test_resource_fences_is_closed_fresh_read_without_lifecycle_id(self):
        self.backend.source("source:resource_fences", "resource_fences")
        self.backend.source("source:resource_fences", "resource_fences")
        self.assertEqual(self.source.calls, [("resource_fences", (), {}), ("resource_fences", (), {})])
        self.assertFalse(self.store.transport["sources"])
        self.assert_refuses("SOURCE_FIELDS", lambda: self.backend.source("a", "resource_fences", containers=[]))

    def test_source_identity_stable_across_backend_restart(self):
        self.backend.source("source:pg_stop", "pg_stop")
        backend = m.RunnerBackend(CONFIG, self.store, self.transport, self.source)
        backend.source("source:pg_stop", "pg_stop")
        self.assertEqual(self.source.calls[0][1], self.source.calls[1][1])
        self.assertRegex(self.source.calls[0][1][0], "^[0-9a-f]{32}$")

    def test_source_pending_is_unknown_and_still_reconciles(self):
        self.source.phase = "pending"
        self.assert_refuses("SOURCE_OPERATION_PENDING", lambda: self.backend.source("source:pg_stop", "pg_stop"), m.Unknown)
        self.source.phase = "complete"
        self.assertEqual(self.backend.source("source:pg_stop", "pg_stop")["phase"], "complete")
        self.assertEqual(self.source.calls[0][1], self.source.calls[1][1])

    def test_source_unknown_maps_engine_unknown(self):
        self.source.error = m.source_module.Unknown("secret-not-relayed")
        self.assert_refuses("SOURCE_OPERATION_UNKNOWN", lambda: self.backend.source("source:pg_stop", "pg_stop"), m.Unknown)

    def test_source_method_and_field_allowlist(self):
        self.assert_refuses("UNKNOWN_SOURCE_METHOD", lambda: self.backend.source("a", "shell", command="rm"))
        self.assert_refuses("SOURCE_FIELDS", lambda: self.backend.source("a", "pg_stop", container="other"))

    def test_source_label_cannot_change_request(self):
        self.backend.source("a", "pg_stop")
        self.assert_refuses("SOURCE_LABEL_REUSED", lambda: self.backend.source("a", "app_stop"))

    def test_rpc_intent_before_network_and_exact_replay_after_loss(self):
        self.transport.timeout_after = "pg.arm"
        fields = {"final_lsn": "1/100", "quiet_attestation_sha256": "a" * 64}
        self.assert_refuses("injected-response-loss", lambda: self.backend.rpc("1:pg.arm", "pg.arm", **fields), m.Unknown)
        self.assertEqual(self.backend.rpc("1:pg.arm", "pg.arm", **fields)["arm_id"], "d" * 32)
        self.assertEqual(self.transport.calls[0], self.transport.calls[1])
        self.assertEqual(self.transport.epoch, 1)

    def test_rpc_stable_across_restart_and_epoch_advance(self):
        fields = {"final_lsn": "1/100", "quiet_attestation_sha256": "a" * 64}
        self.backend.rpc("1:pg.arm", "pg.arm", **fields)
        self.backend.rpc("1:endpoints", "app.persist-endpoints", postgres="source", redis="source")
        backend = m.RunnerBackend(CONFIG, self.store, self.transport, self.source)
        backend.rpc("1:pg.arm", "pg.arm", **fields)
        self.assertEqual(self.transport.calls[0], self.transport.calls[-1])

    def test_rpc_mutation_label_cannot_change_fields(self):
        fields = {"final_lsn": "1/100", "quiet_attestation_sha256": "a" * 64}
        self.backend.rpc("1:pg.arm", "pg.arm", **fields)
        fields["final_lsn"] = "1/101"
        self.assert_refuses("RPC_LABEL_REUSED", lambda: self.backend.rpc("1:pg.arm", "pg.arm", **fields))

    def test_read_calls_always_fresh_even_same_label(self):
        self.backend.rpc("cached?", "status")
        self.backend.rpc("cached?", "status")
        self.assertNotEqual(self.transport.calls[0]["op_id"], self.transport.calls[1]["op_id"])

    def test_read_epoch_conflict_reconciles_with_new_id(self):
        self.transport.epoch = 4
        self.backend.rpc(None, "status")
        self.assertEqual([call["expected_epoch"] for call in self.transport.calls], [0, 4])
        self.assertNotEqual(self.transport.calls[0]["op_id"], self.transport.calls[1]["op_id"])

    def test_mutating_epoch_conflict_is_never_rebased(self):
        self.transport.epoch = 4
        fields = {"final_lsn": "1/100", "quiet_attestation_sha256": "a" * 64}
        self.assert_refuses("RPC_EPOCH_CONFLICT", lambda: self.backend.rpc("1:pg.arm", "pg.arm", **fields))
        self.assert_refuses("RPC_EPOCH_CONFLICT", lambda: self.backend.rpc("1:pg.arm", "pg.arm", **fields))
        self.assertEqual(self.transport.calls[0], self.transport.calls[1])

    def test_code_unknown_raises_unknown(self):
        self.transport.code = "UNKNOWN"
        self.assert_refuses("RPC_OPERATION_UNKNOWN", lambda: self.backend.rpc(None, "status"), m.Unknown)

    def test_code_refusal_is_not_ok(self):
        self.transport.code = "PG_REPLAY_BEHIND"
        self.assert_refuses("RPC_PG_REPLAY_BEHIND", lambda: self.backend.rpc(None, "status"))

    def test_bad_response_identity_never_means_success(self):
        self.transport.transform = lambda reply: {**reply, "owner": "f" * 32}
        self.assert_refuses("RPC_RESPONSE_IDENTITY", lambda: self.backend.rpc(None, "status"), m.Unknown)

    def test_error_minimal_envelope_remains_unknown(self):
        self.transport.transform = lambda reply: {"ok": False, "code": "LOCAL_STATE_UNCERTAIN"}
        self.assert_refuses("RPC_ENVELOPE_UNCERTAIN", lambda: self.backend.rpc(None, "status"), m.Unknown)

    def test_request_rejects_extra_argv_or_commands(self):
        self.assert_refuses("UNKNOWN_RPC_ACTION", lambda: self.backend.rpc("a", "shell", command="true"))
        self.assert_refuses("RPC_REQUEST_FIELDS", lambda: self.backend.rpc("a", "pg.arm", final_lsn="1/100", quiet_attestation_sha256="a" * 64, container="other"))
        self.assertFalse(self.transport.calls)

    def test_control_get_is_always_fresh(self):
        self.backend.control("1:status", "writers.status")
        self.transport.bodies["writers"]["epoch"] = 4
        result = self.backend.control("1:status", "writers.status")
        self.assertEqual(result["epoch"], 4)
        self.assertNotEqual(self.transport.calls[0]["op_id"], self.transport.calls[1]["op_id"])

    def test_control_lost_reply_keeps_original_epoch_and_request(self):
        self.transport.timeout_after = "writers.pause"
        self.assert_refuses("injected-response-loss", lambda: self.backend.control("1:writers.pause", "writers.pause"), m.Unknown)
        entry = copy.deepcopy(self.store.transport["controls"]["1:writers.pause"])
        result = self.backend.control("1:writers.pause", "writers.pause")
        self.assertTrue(result["held"])
        self.assertEqual(self.store.transport["controls"]["1:writers.pause"], entry)
        mutations = [call for call in self.transport.calls if call.get("control") == "writers.pause"]
        self.assertEqual(mutations[0], mutations[1])
        self.assertEqual(mutations[0]["control_epoch"], 1)

    def test_control_204_fetches_new_status_body(self):
        self.backend.control("1:writers.pause", "writers.pause")
        self.transport.reply_204 = True
        result = self.backend.control("1:writers.resume", "writers.resume")
        self.assertFalse(result["held"])
        self.assertEqual(result["epoch"], 3)
        self.assertEqual(self.transport.calls[-1]["control"], "writers.status")
        stored = self.store.transport["controls"]["1:writers.resume"]
        self.assertEqual(stored["request"]["control_epoch"], 2)

    def test_control_label_cannot_change_endpoint(self):
        self.backend.control("1:switch", "postgres.switch", endpoint="target")
        self.assert_refuses("CONTROL_LABEL_REUSED", lambda: self.backend.control("1:switch", "postgres.switch", endpoint="source"))

    def test_control_endpoint_allowlist(self):
        self.assert_refuses("CONTROL_ENDPOINT", lambda: self.backend.control("a", "postgres.switch", endpoint="postgres://secret"))
        self.assert_refuses("CONTROL_ENDPOINT", lambda: self.backend.control("a", "writers.pause", endpoint="target"))

    def quiet(self):
        self.transport.bodies["ingress"].update(paused=True, quiet=True)
        self.transport.bodies["writers"].update(held=True, quiet=True)
        self.transport.bodies["redis"].update(paused=True, quiet=True)
        self.transport.bodies["postgres"].update(paused=True)
        # Actual Go readiness is false for closed ingress/paused data. Wiring
        # readiness remains true and the independent controls prove quiescence.
        self.transport.runtime["ready"] = False
        self.transport.runtime["readiness"].update(postgres=False, redis=False)
        return {"v": 1, "run": m.RUN, "owner": OWNER, "route_fenced": True,
                "final_lsn": "1/100", "pg_arm": {"arm_id": "d" * 32}}

    def test_attestation_requires_live_writer_redis_and_ingress_status(self):
        state = self.quiet()
        result = self.backend.attest(state, self.transport.runtime)
        self.assertTrue(result["postgres"]["writers_quiet"])
        self.assertEqual(result["postgres"]["target_replay_lsn"], "1/200")
        self.assertEqual(result["postgres"]["arm_id"], "d" * 32)
        self.assertEqual(result["boot_id"], "boot-1")
        self.assertEqual(self.store.attestation, result)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["action"], "app.quiet-snapshot")
        self.assertEqual(self.transport.epoch, 0)

    def test_attestation_snapshot_is_one_fresh_read_each_time(self):
        state = self.quiet()
        self.backend.attest(state, self.transport.runtime)
        self.transport.bodies["redis"]["quiet"] = False
        self.assertFalse(self.backend.attest(state, self.transport.runtime)["redis"]["consumers_paused"])
        self.assertEqual(len(self.transport.calls), 2)
        self.assertNotEqual(self.transport.calls[0]["op_id"], self.transport.calls[1]["op_id"])
        self.assertEqual(self.transport.epoch, 0)

    def test_attestation_reuses_just_returned_macro_snapshot_with_same_strict_validation(self):
        state = self.quiet()
        state["window"] = {"boot_id": "boot-1"}
        sample = self.backend.rpc(None, "app.quiet-snapshot")
        self.transport.calls.clear()
        result = self.backend.attest(state, {"instance_id": "migration-app"}, snapshot=sample, sampled_started=999.5)
        self.assertTrue(result["postgres"]["writers_quiet"])
        self.assertEqual(self.transport.calls, [])
        self.assert_refuses("ATTESTATION_SAMPLE_STALE", lambda: self.backend.attest(state, self.transport.runtime, snapshot=sample, sampled_started=997.9))
        self.assert_refuses("ATTESTATION_SAMPLE_INPUT", lambda: self.backend.attest({**state, "window": {"boot_id": "different"}}, self.transport.runtime, snapshot=sample, sampled_started=999.5))
        sample["redis"]["active_operations"] = False
        result = self.backend.attest(state, self.transport.runtime, snapshot=sample, sampled_started=999.5)
        self.assertFalse(result["redis"]["consumers_paused"])

    def run_combined_engine(self, arm_delay=0):
        # Real RunnerBackend counts pre-POST GET and 204 handling too. Fixed
        # target macro internals are covered separately in target_rpc_test.
        spec = importlib.util.spec_from_file_location("combined_physical_fixture", pathlib.Path(__file__).with_name("durable_runner_test.py"))
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        clock = fixture.Clock()
        physical = fixture.Backend(clock, rpc_wire=True, source_wire=True)
        source = FakeSource()
        for method in m.SOURCE_READS | m.SOURCE_MUTATIONS:
            setattr(source, method, lambda *args, method=method, **fields: physical.source(
                args[0] if args else "read:"+method, method, **fields))
        source.b.boot_id = physical.boot_id
        store = MemoryStore()
        def save(value):
            store.engine = copy.deepcopy(value)
            physical.saved = copy.deepcopy(value)
        store.save_state = save
        class Transport:
            def __init__(self):
                self.epoch, self.calls = 0, []
            def request(self, request):
                self.calls.append(copy.deepcopy(request))
                assert request["expected_epoch"] == self.epoch
                if not m.RunnerBackend._is_read(request["action"], request):
                    self.epoch += 1
                if request["action"] == "app.control":
                    result = {"http_status": 200, "body": physical.control(None, request["control"])}
                else:
                    fields = {key: value for key, value in request.items() if key not in
                              {"v", "run", "owner", "op_id", "expected_epoch", "action"}}
                    result = physical.rpc(request["op_id"], request["action"], **fields)
                return m.canonical({"ok": True, "code": "OK", "run": m.RUN, "owner": OWNER,
                    "op_id": request["op_id"], "action": request["action"], "epoch": self.epoch, "phase": physical.pg_authority, "result": result})
        transport = Transport()
        backend = m.RunnerBackend(CONFIG, store, transport, source, now=clock.now)
        engine = m.engine.DataHandover(fixture.initial(owner=OWNER), backend, now=clock.now, monotonic=clock.monotonic)
        if arm_delay:
            physical.delays["rpc.pg.arm-quiet"] = arm_delay
            with self.assertRaisesRegex(m.Refusal, "ATTESTATION_SAMPLE_STALE"):
                engine.handover_all()
            self.assertEqual(physical.stop_commands, 0)
            self.assertEqual(physical.promotions, 0)
            self.assertEqual(physical.arm["status"], "CANCELLED")
            self.assertTrue(physical.runtime()["ready"])
            return
        engine.handover_all()
        self.assertEqual(len(transport.calls), 11)
        self.assertEqual(transport.calls[0]["control"], "runtime.status")
        self.assertEqual([call["action"] for call in transport.calls[1:]], [
            "app.quiesce", "pg.arm-quiet", "pg.commit", "pg.route",
            "redis.preroute", "app.quiet-snapshot", "redis.inspect", "redis.persist-primary",
            "app.persist-endpoints", "app.restore"])
        self.assertTrue(physical.runtime()["ready"])
        self.assertTrue(store.attestation["redis"]["consumers_paused"])

    def test_combined_engine_uses_eleven_calls_ten_inside_window_with_real_adapter(self):
        self.run_combined_engine()

    def test_arm_quiet_arriving_after_two_seconds_cannot_stop_source(self):
        self.run_combined_engine(arm_delay=2.01)

    def test_attestation_missing_snapshot_component_is_rejected(self):
        state = self.quiet()
        self.transport.transform = lambda reply: {**reply, "result": {key: row for key, row in reply["result"].items() if key != "writers"}}
        self.assert_refuses("ATTESTATION_SNAPSHOT_INVALID", lambda: self.backend.attest(state, self.transport.runtime))
        self.assertIsNone(self.store.attestation)

    def test_runtime_zero_counters_alone_cannot_prove_writer_fence(self):
        state = self.quiet()
        self.transport.bodies["writers"].update(held=False, quiet=False)
        result = self.backend.attest(state, self.transport.runtime)
        self.assertFalse(result["postgres"]["writers_quiet"])
        self.assertFalse(result["redis"]["writers_quiet"])

    def test_redis_quiet_is_independent_proof(self):
        state = self.quiet()
        self.transport.bodies["redis"]["quiet"] = False
        result = self.backend.attest(state, self.transport.runtime)
        self.assertFalse(result["redis"]["consumers_paused"])
        self.assertFalse(result["postgres"]["writers_quiet"])

    def test_missing_pg_arm_uses_safe_sentinels_before_window_ready(self):
        state = {"v": 1, "run": m.RUN, "owner": OWNER, "route_fenced": True}
        result = self.backend.attest(state, self.transport.runtime)
        self.assertEqual(result["postgres"]["final_lsn"], "0/0")
        self.assertEqual(result["postgres"]["arm_id"], "0" * 32)
        self.assertFalse(result["postgres"]["writers_quiet"])

    def test_live_failed_task_blocks_quiet(self):
        state = self.quiet()
        self.transport.runtime["tasks"]["usage_records"]["failed"] = 1
        self.assertFalse(self.backend.attest(state, self.transport.runtime)["postgres"]["writers_quiet"])

    def test_actual_go_paused_runtime_proves_quiet_without_public_readiness(self):
        state = self.quiet()
        self.assertFalse(self.transport.runtime["ready"])
        self.assertFalse(self.transport.runtime["readiness"]["postgres"])
        self.assertFalse(self.transport.runtime["readiness"]["redis"])
        result = self.backend.attest(state, self.transport.runtime)
        self.assertTrue(result["postgres"]["writers_quiet"])
        self.assertTrue(result["redis"]["writers_quiet"])
        self.assertTrue(result["redis"]["consumers_paused"])

    def test_incomplete_go_writer_registry_blocks_quiet(self):
        state = self.quiet()
        complete = copy.deepcopy(self.transport.runtime["tasks"])
        for missing in m.REQUIRED_WRITERS:
            with self.subTest(missing=missing):
                self.transport.runtime["tasks"] = {name: row for name, row in complete.items() if name != missing}
                result = self.backend.attest(state, self.transport.runtime)
                self.assertFalse(result["postgres"]["writers_quiet"])
                self.assertFalse(result["redis"]["writers_quiet"])

    def test_missing_or_failed_wiring_readiness_blocks_quiet(self):
        state = self.quiet()
        complete = copy.deepcopy(self.transport.runtime["readiness"])
        for name in ("writer_coverage", "quiet_controls"):
            for value in (False, None):
                with self.subTest(name=name, value=value):
                    self.transport.runtime["readiness"] = {**complete, name: value}
                    self.assertFalse(self.backend.attest(state, self.transport.runtime)["postgres"]["writers_quiet"])

    def test_paused_runtime_requires_serving_and_clean_actual_counters(self):
        state = self.quiet()
        complete = copy.deepcopy(self.transport.runtime)
        for name, value in (("serving", False), ("draining", True), ("active_requests", 1),
                            ("active_hijacked_connections", 1), ("active_requests", False),
                            ("background_workers_enabled", True)):
            with self.subTest(name=name, value=value):
                self.transport.runtime.clear()
                self.transport.runtime.update(copy.deepcopy(complete))
                self.transport.runtime[name] = value
                self.assertFalse(self.backend.attest(state, self.transport.runtime)["postgres"]["writers_quiet"])

    def test_runtime_ingress_must_match_independent_quiet_epoch(self):
        state = self.quiet()
        before = copy.deepcopy(self.transport.runtime["ingress"])
        for changed in ({"paused": False}, {"quiet": False}, {"epoch": before["epoch"] + 1}):
            with self.subTest(changed=changed):
                self.transport.runtime["ingress"] = {**before, **changed}
                self.assertFalse(self.backend.attest(state, self.transport.runtime)["postgres"]["writers_quiet"])

    def test_required_writer_contract_matches_go_registry(self):
        import re
        source = pathlib.Path(__file__).parents[2] / "backend/internal/service/migration_writer_quiesce.go"
        block = source.read_text().split("var migrationRequiredWriters = []string{", 1)[1].split("}", 1)[0]
        self.assertEqual(m.REQUIRED_WRITERS, set(re.findall(r'"([a-z_]+)"', block)))

    def test_attestation_app_instance_change_refuses(self):
        state = self.quiet()
        self.assert_refuses("APP_INSTANCE_CHANGED", lambda: self.backend.attest(state, {"instance_id": "old-instance"}))

    def test_close_master_has_independent_lifecycle(self):
        self.assertTrue(self.backend.close_transport()["closed"])
        self.assertTrue(self.transport.closed)
        self.assertFalse(self.source.calls)

    def test_fixed_ssh_argv_has_no_user_config_or_agent(self):
        transport = m.SSHTransport(CONFIG)
        with mock.patch.object(transport, "_verify"):
            argv = transport.argv()
            close = transport.argv(close=True)
        self.assertEqual(argv[:3], ["/usr/bin/ssh", "-F", "/dev/null"])
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("IdentityAgent=none", argv)
        self.assertIn("ClearAllForwardings=yes", argv)
        self.assertEqual(argv[-5:], [m.TARGET, "/usr/bin/python3", "-I", m.REMOTE_PROGRAM, "serve"])
        self.assertEqual(close[-3:], ["-O", "exit", m.TARGET])
        self.assertNotIn("serve", close)

    def test_ssh_timeout_always_unknown_and_no_stderr_disclosure(self):
        transport = m.SSHTransport(CONFIG)
        with mock.patch.object(transport, "_verify"), mock.patch.object(m.subprocess, "run", side_effect=subprocess.TimeoutExpired(["ssh"], 3, stderr=b"secret")):
            self.assert_refuses("RPC_TRANSPORT_UNKNOWN", lambda: transport.request({}), m.Unknown)

    def test_ssh_non_rpc_exit_unknown(self):
        transport = m.SSHTransport(CONFIG)
        with mock.patch.object(transport, "_verify"), mock.patch.object(m.subprocess, "run", return_value=types.SimpleNamespace(returncode=255, stdout=b"", stderr=b"key secret")):
            self.assert_refuses("RPC_TRANSPORT_UNKNOWN", lambda: transport.request({}), m.Unknown)

    def test_checkpoint_failure_after_atomic_write_is_unknown(self):
        store = m.FileStore()
        store.config = CONFIG
        with mock.patch.object(m.rpc_module, "atomic_json") as write, mock.patch.object(store, "_audit", side_effect=OSError("disk detail")):
            self.assert_refuses("STATE_PERSISTENCE_UNCERTAIN", lambda: store._write("state", {"v": 1}), m.Unknown)
        write.assert_called_once()

    def test_audit_failure_after_checkpoint_cannot_be_misreported_refusal(self):
        store = m.FileStore()
        store.config = CONFIG
        with mock.patch.object(m.rpc_module, "atomic_json"), mock.patch.object(store, "_audit", side_effect=m.Refusal("AUDIT_WRITE_FAILED")):
            self.assert_refuses("STATE_PERSISTENCE_UNCERTAIN", lambda: store._write("state", {"v": 1}), m.Unknown)

    def test_unknown_state_paths_rejected_before_filesystem_access(self):
        with mock.patch.object(m.rpc_module, "private_read") as read:
            self.assert_refuses("UNKNOWN_STATE_FILE", lambda: m.FileStore().read("../../etc/passwd"))
        read.assert_not_called()

    def test_corrupt_response_nonfinite_or_duplicate_is_unknown(self):
        for raw in (b'{"ok":true,"ok":false}', b'{"x":1e999}', b'not JSON'):
            request = {"op_id": "a" * 32, "action": "status", "expected_epoch": 0}
            self.assert_refuses("RPC_RESPONSE_INVALID", lambda: self.backend._response(request, raw), m.Unknown)

    def test_attestation_sampling_too_slow_is_not_refreshed(self):
        state = self.quiet()
        clock = iter((1000.0, 1002.1))
        self.backend.now = lambda: next(clock)
        self.assert_refuses("ATTESTATION_SAMPLE_STALE", lambda: self.backend.attest(state, self.transport.runtime))
        self.assertIsNone(self.store.attestation)

    def test_ssh_invocation_uses_sanitized_environment_and_stdin_json(self):
        transport = m.SSHTransport(CONFIG)
        with mock.patch.object(transport, "_verify"), mock.patch.object(m.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=b"{}", stderr=b"")) as run:
            self.assertEqual(transport.request({"v": 1}), b"{}")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["input"], b'{"v":1}\n')
        self.assertNotIn("SSH_AUTH_SOCK", kwargs["env"])
        self.assertNotIn("HOME", kwargs["env"])
        self.assertNotIn("shell", kwargs)

    def test_owner_namespace_and_label_determine_stable_operation(self):
        first = m.operation_id(OWNER, "rpc", "1:pg.arm")
        self.assertEqual(first, m.operation_id(OWNER, "rpc", "1:pg.arm"))
        self.assertNotEqual(first, m.operation_id(OWNER, "source", "1:pg.arm"))
        self.assertNotEqual(first, m.operation_id("f" * 32, "rpc", "1:pg.arm"))


if __name__ == "__main__":
    unittest.main()
