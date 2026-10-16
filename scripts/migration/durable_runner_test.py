#!/usr/bin/env python3
"""Fault tests for the data window; no SSH, Docker, filesystem apply, or sleep.

The default fake exposes both the engine draft's old normalized aliases and the
actual wire fields so algorithm faults can be tested independently of adapter
bugs. Strict-wire tests remove those aliases. They MUST pass before release;
they are deliberately not marked expectedFailure when the draft is incomplete.
"""
import copy
import hashlib
import importlib.util
import json
import pathlib
import stat
import types
import unittest
from unittest import mock


def load(name):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(name+".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load("durable_runner")


class ProcessDeath(BaseException):
    """Uncatchable by the engine's in-process Exception recovery path."""


class Clock:
    def __init__(self, wall=1000.0, monotonic=100.0):
        self.wall, self.tick = wall, monotonic

    def now(self):
        return self.wall

    def monotonic(self):
        return self.tick

    def advance(self, seconds):
        self.wall += seconds
        self.tick += seconds


def initial(**overrides):
    return {"v": 1, "run": M.RUN, "pg": "AWS", "redis": "AWS", "attempt": 0,
            "pause_seconds": 0.0, "window": None, "route_fenced": True, **overrides}


class Backend:
    """Physical effects survive engine/process replacement; ledgers deduplicate.

    The control gates model the actual application ordering, not the draft's
    preferred result. Paused Redis pre-routing retires source pooled sockets;
    persisted startup routing changes only after actual target authority.
    """
    def __init__(self, clock, *, rpc_wire=False, source_wire=False):
        self.clock, self.rpc_wire, self.source_wire = clock, rpc_wire, source_wire
        self.events, self.faults, self.delays = [], {}, {}
        self.saved = initial()
        self.ingress = {"epoch": 0, "paused": False, "quiet": False}
        self.writers = {"epoch": 0, "held": False, "quiet": False}
        self.pg = {"epoch": 0, "paused": False, "endpoint": "source", "active_operations": 0}
        self.redis = {"epoch": 1, "paused": False, "quiet": False, "active_operations": 0,
                      "address": "127.0.0.1:16379", "pubsub_cache_ready": True, "tracked_sockets": 3}
        self.endpoints = {"v": 1, "postgres": "source", "redis": "source"}
        self.pg_authority = self.redis_authority = "AWS"
        self.pg_source_running = True
        self.source_redis_role, self.target_redis_role = "master", "slave"
        self.target_redis_run_id = "f"*40
        self.source_redis_running = True
        self.source_app_exited, self.route_fenced = True, True
        self.arm, self.accepted = None, False
        self.promotions = self.stop_commands = self.failover_commands = 0
        self.source_op_labels, self.source_cache, self.rpc_cache, self.control_cache = {}, {}, {}, {}
        self.rpc_requests = {}
        self.source_started, self.source_pending = set(), {}
        self.task_overrides = {}
        self.active_requests = self.active_websockets = 0
        self.background_workers = False
        self.partial_redis_pause = False
        self.current_pause_start, self.request_wait_seconds = None, 0.0
        self.boot = "test-boot-1"

    def boot_id(self):
        return self.boot

    def engine(self, state=None, clock=None):
        clock = clock or self.clock
        return M.DataHandover(copy.deepcopy(self.saved if state is None else state), self,
                              now=clock.now, monotonic=clock.monotonic)

    def fault(self, operation, *, when="after", exception=M.Unknown):
        self.faults[(operation, when)] = exception

    def hit(self, operation, when, label=None):
        self.events.append((operation, when, label, self.clock.now()))
        if when == "before":
            self.clock.advance(self.delays.get(operation, 0.001))
        exception = self.faults.pop((operation, when), None)
        if exception:
            raise exception("injected-"+operation+"-"+when)

    def save(self, state):
        self.hit("save", "before")
        self.saved = copy.deepcopy(state)
        self.hit("save", "after")

    def attest(self, state, runtime, snapshot=None, sampled_started=None):
        self.hit("attest", "before")
        if snapshot is None:
            self.rpc(None, "app.quiet-snapshot")
        self.attestation = copy.deepcopy({"state": state, "runtime": runtime})
        self.hit("attest", "after")

    def healthy_endpoints(self):
        pg_ok = ((self.pg["endpoint"] == "source" and self.pg_source_running) or
                 (self.pg["endpoint"] == "target" and self.pg_authority == "TENCENT"))
        redis_ok = ((self.redis["address"] == "127.0.0.1:16379" and self.source_redis_running and
                     self.source_redis_role == "master") or
                    (self.redis["address"] == "127.0.0.1:26379" and self.target_redis_role == "master"))
        return pg_ok and redis_ok and not self.pg["paused"] and not self.redis["paused"] and self.redis["pubsub_cache_ready"]

    def runtime(self):
        tasks = {name: {"active": 0, "pending": 0, "failed": 0, "dropped": 0} for name in
            ["timing_wheel", "scheduled_writers", "plugins", "prompt_audit", "prompt_config", "auth_outbox",
             "ops_runtime", "usage_records", "billing_cache", "content_moderation", "detached_writers",
             "deferred_last_used", "audit_log", "ops_ingress_reject", "ops_system_log"]}
        tasks.update(copy.deepcopy(self.task_overrides))
        return {"instance_id": "fixed-instance", "draining": False, "ingress": copy.deepcopy(self.ingress),
                "ready": self.healthy_endpoints() and not self.ingress["paused"], "serving": True,
                "active_requests": self.active_requests, "active_hijacked_connections": self.active_websockets,
                "background_workers_enabled": self.background_workers, "tasks": tasks,
                "readiness": {"postgres": not self.pg["paused"], "redis": not self.redis["paused"],
                              "writer_coverage": True, "quiet_controls": True}}

    def control(self, label, name, endpoint=None):
        operation = "control."+name
        self.hit(operation, "before", label)
        component, action = name.split(".")
        attribute = "pg" if component == "postgres" else component
        if action == "status":
            result = self.runtime() if component == "runtime" else copy.deepcopy(getattr(self, attribute))
        elif label in self.control_cache:
            result = copy.deepcopy(self.control_cache[label])
        else:
            body = getattr(self, attribute)
            if component == "ingress":
                if action == "pause":
                    body.update(paused=True, quiet=self.active_requests == self.active_websockets == 0)
                    body["epoch"] += 1
                    if self.current_pause_start is None:
                        self.current_pause_start = self.clock.now()
                else:
                    M.need(not self.writers["held"] and self.healthy_endpoints(), "fake-ingress-resume-guard")
                    body.update(paused=False, quiet=False)
                    body["epoch"] += 1
                    if self.current_pause_start is not None:
                        self.request_wait_seconds += self.clock.now()-self.current_pause_start
                        self.current_pause_start = None
            elif component == "writers":
                M.need(self.ingress["paused"] and self.ingress["quiet"], "fake-writer-global-quiet-guard")
                if action == "pause":
                    body.update(held=True, quiet=True)
                else:
                    M.need(self.healthy_endpoints(), "fake-writer-endpoint-guard")
                    body.update(held=False, quiet=False)
                body["epoch"] += 1
            elif component == "postgres":
                M.need(self.ingress["paused"] and self.writers["held"] and self.redis["paused"] and self.redis["quiet"],
                       "fake-postgres-before-redis-guard")
                if action == "switch":
                    M.need(body["paused"] and self.endpoints["postgres"] == endpoint and self.pg_authority == "TENCENT",
                           "fake-postgres-switch-guard")
                    body.update(endpoint=endpoint, epoch=body["epoch"]+1)
                else:
                    body["paused"] = action == "pause"
            elif component == "redis":
                M.need(self.ingress["paused"] and self.writers["held"], "fake-redis-global-quiet-guard")
                if action == "pause":
                    body.update(paused=True, quiet=not self.partial_redis_pause, pubsub_cache_ready=False)
                elif action == "switch":
                    preroute = (self.saved["redis"] == "COMMIT_INTENT" and self.saved["window"]["irreversible"] is True and
                                self.saved["pg"] == "TENCENT" and self.endpoints["redis"] == "source" and
                                self.target_redis_role == "slave")
                    confirmed = self.endpoints["redis"] == endpoint and self.target_redis_role == "master"
                    M.need(body["paused"] and body["quiet"] and endpoint == "target" and
                           (preroute or confirmed), "fake-redis-switch-guard")
                    body.update(address="127.0.0.1:26379", epoch=body["epoch"]+1, pubsub_cache_ready=False, tracked_sockets=0)
                elif action == "rebuild":
                    M.need(body["paused"] and body["active_operations"] == 0 and not self.pg["paused"], "fake-cache-rebuild-guard")
                    body["pubsub_cache_ready"] = True
                elif action == "resume":
                    M.need(body["pubsub_cache_ready"], "fake-unrebuilt-redis-resume")
                    body.update(paused=False, quiet=False)
            result = copy.deepcopy(body)
            self.control_cache[label] = copy.deepcopy(result)
        self.hit(operation, "after", label)
        return result

    def quiet_snapshot(self):
        return {"app": {"http_status": 200, "body": self.runtime()},
                "pg": {"replay_lsn": "10/2", "in_recovery": self.pg_authority != "TENCENT"},
                **{key: copy.deepcopy(getattr(self, "pg" if key == "postgres" else key))
                   for key in ("ingress", "writers", "redis", "postgres")}}

    def rpc(self, label, action, **fields):
        operation = "rpc."+action
        self.hit(operation, "before", label)
        if label is not None and action not in {"status", "redis.inspect"}:
            request = (action, copy.deepcopy(fields))
            previous = self.rpc_requests.setdefault(label, request)
            M.need(previous == request, "fake-same-rpc-key-different-request")
            # Target may durably record a request and then lose its transport
            # before acceptance/effect. Its original arguments stay immutable.
            self.hit(operation, "recorded", label)
        if action == "status":
            pg = {"system_identifier": "12345", "in_recovery": self.pg_authority != "TENCENT",
                  "replay_lsn": "10/2", "wal_timeline": 2 if self.pg_authority == "TENCENT" else None}
            if not self.rpc_wire:
                pg["authority"] = self.pg_authority  # Draft-only alias; strict test removes it.
            result = {"pg_authority": self.pg_authority, "pg": pg, "redis_authority": self.redis_authority,
                      "persistent_endpoints": copy.deepcopy(self.endpoints),
                      "pg_arm": copy.deepcopy(self.arm),
                      "pg_promotion_intent": {"op_id": "accepted-op", "accepted_at": self.clock.now()} if self.accepted else None,
                      "redis": {"role": self.target_redis_role, "failover_state": "no-failover"}}
        elif action == "redis.inspect":
            result = {"role": self.target_redis_role, "failover_state": "no-failover", "run_id": self.target_redis_run_id}
        elif action == "app.quiet-snapshot":
            result = self.quiet_snapshot()
        elif label in self.rpc_cache:
            old_fields, result = self.rpc_cache[label]
            M.need(old_fields == fields, "fake-same-rpc-key-different-fields")
            result = copy.deepcopy(result)
        elif action == "app.quiesce":
            for key in ("ingress", "writers", "redis", "postgres"):
                self.control(str(label)+":"+key, key+".pause")
            for row in self.runtime()["tasks"].values():
                M.need(all(type(row.get(key)) is int and row[key] == 0 for key in ("active", "pending", "failed", "dropped")), "fake-not-quiet")
            result = self.quiet_snapshot()
        elif action == "pg.route":
            M.need(self.pg_authority == "TENCENT", "fake-pg-authority-unconfirmed")
            self.endpoints["postgres"] = "target"
            body = self.control(str(label)+":switch", "postgres.switch", endpoint="target")
            result = {"authority": "TENCENT", "body": body}
        elif action == "redis.preroute":
            body = self.control(str(label)+":switch", "redis.switch", endpoint="target")
            result = {"body": body}
        elif action == "app.restore":
            if self.pg["paused"]:
                self.control(str(label)+":pg", "postgres.resume")
            if self.redis["paused"]:
                self.control(str(label)+":rebuild", "redis.rebuild")
                self.control(str(label)+":redis", "redis.resume")
            if self.writers["held"]:
                self.control(str(label)+":writers", "writers.resume")
            self.control(str(label)+":ingress", "ingress.resume")
            result = self.quiet_snapshot()
        elif action in ("pg.arm", "pg.arm-quiet"):
            M.need(not self.accepted and (not self.arm or self.arm["status"] == "CANCELLED"), "fake-PG_ARM_ALREADY_EXISTS")
            self.arm = {"arm_id": "a"*32, "expires_at": self.clock.now()+6, "status": "ARMED", "op_id": label}
            result = copy.deepcopy(self.arm)
            if action == "pg.arm-quiet":
                result.update(quiet_nonce=fields["quiet_nonce"], quiet_snapshot=self.quiet_snapshot())
        elif action == "pg.cancel-arm":
            M.need(not self.accepted and self.arm and self.arm["arm_id"] == fields["arm_id"], "fake-PG_ALREADY_ACCEPTED")
            self.arm["status"] = "CANCELLED"
            result = {"cancelled": True, "authority": "AWS", "arm_id": fields["arm_id"]}
        elif action == "pg.commit":
            M.need(not self.pg_source_running and self.arm and self.arm["expires_at"] > self.clock.now(), "fake-commit-without-source-fence-or-arm")
            self.accepted, self.pg_authority = True, "COMMIT_INTENT"
            self.hit("rpc.pg.commit", "accepted", label)
            self.promotions += 1
            self.pg_authority = "TENCENT"
            result = {"authority": "TENCENT", "writable": True}
        elif action == "pg.reconcile":
            M.need(self.accepted and not self.pg_source_running, "fake-reconcile-without-accepted-intent")
            if self.pg_authority != "TENCENT":
                self.promotions += 1
            self.pg_authority = "TENCENT"
            result = {"authority": "TENCENT", "writable": True}
        elif action == "redis.persist-primary":
            M.need(self.target_redis_role == "master", "fake-redis-not-primary")
            self.redis_authority = "TENCENT"
            result = {"role": "master", "persisted": True, "config_sha256": "c"*64, "run_id": "f"*40}
            result["authority"] = "TENCENT"
        elif action == "app.persist-endpoints":
            for key in ["postgres", "redis"]:
                M.need(self.endpoints[key] != "target" or fields[key] == "target", "fake-endpoint-rollback-forbidden")
                authority = self.pg_authority if key == "postgres" else self.redis_authority
                M.need(fields[key] != "target" or authority == "TENCENT", "fake-unconfirmed-target-endpoint")
            self.endpoints.update(fields)
            result = copy.deepcopy(self.endpoints)
        else:
            raise AssertionError("Unexpected fixed RPC action "+action)
        if label is not None and action not in {"status", "redis.inspect"}:
            self.rpc_cache[label] = (copy.deepcopy(fields), copy.deepcopy(result))
        self.hit(operation, "after", label)
        return result

    def source(self, label, method, **fields):
        operation = "source."+method
        self.hit(operation, "before", label)
        irreversible = method in {"pg_stop", "redis_failover", "persist_source_redis_role", "redis_role_persist"}
        if irreversible:
            old = self.source_op_labels.setdefault(method, label)
            M.need(old == label, "fake-new-source-id-for-already-initiated-action")
        if irreversible and method in self.source_cache:
            result = copy.deepcopy(self.source_cache[method])
        elif method in ("inspect", "resource_fences"):
            result = {"resources": {"app": {"running": not self.source_app_exited, "status": "exited", "restart": "no",
                                           "exit_code": 0, "oom_killed": False},
                                    "postgres": {"running": self.pg_source_running, "restart": "no",
                                                 "exit_code": 0, "oom_killed": False},
                                    "redis": {"running": self.source_redis_running, "restart": "no",
                                              "exit_code": 0, "oom_killed": False}},
                      "app_connections": {"inbound_connections": 0, "outbound_connections": 0},
                      "redis": {"source": {"role": self.source_redis_role, "master_failover_state": "no-failover"},
                                "target": {"role": self.target_redis_role, "master_failover_state": "no-failover"}}}
            if not self.source_wire:
                result.update(app_exited=self.source_app_exited, route_fenced=self.route_fenced)
        elif method == "pg_final_evidence":
            result = {"quiet": True, "prepared_transactions": 0, "unknown_sessions": [],
                      "flushed_lsn": "10/2", "system_identifier": "12345", "timeline": 1}
            if not self.source_wire:
                result["final_lsn"] = result["flushed_lsn"]
        elif method == "pg_stop":
            if method not in self.source_started:
                self.stop_commands += 1
                self.source_started.add(method)
            phases = self.source_pending.setdefault(method, [])
            if phases:
                result = {"phase": phases.pop(0), "stopped": False, "authority": "UNKNOWN"}
            else:
                self.pg_source_running = False
                result = {"phase": "complete", "stopped": True, "authority": "SOURCE_FENCED", "final_lsn": "10/2", "arm_id": "a"*32}
            if not self.source_wire:
                result["restart_disabled"] = True
        elif method == "redis_roles":
            result = {"source": {"role": self.source_redis_role, "master_failover_state": "no-failover"},
                      "target": {"role": self.target_redis_role, "master_failover_state": "no-failover"}}
        elif method == "redis_failover":
            M.need(self.redis["tracked_sockets"] == 0, "fake-redis-unknown-source-clients")
            if method not in self.source_started:
                self.failover_commands += 1
                self.source_started.add(method)
            phases = self.source_pending.setdefault(method, [])
            if phases:
                result = {"phase": phases.pop(0), "authority": "UNKNOWN"}
            else:
                self.source_redis_role, self.target_redis_role = "slave", "master"
                result = {"phase": "complete", "authority": "TENCENT", "evidence": {
                    "source": {"role": "slave", "master_failover_state": "no-failover"},
                "target": {"role": "master", "master_failover_state": "no-failover", "run_id": self.target_redis_run_id}}}
            if not self.source_wire:
                result.update(role="slave", failover_state="no-failover")
        elif method in {"persist_source_redis_role", "redis_role_persist"}:
            M.need(self.target_redis_role == "master", "fake-source-retirement-before-target")
            phases = self.source_pending.setdefault(method, [])
            if phases:
                result = {"phase": phases.pop(0), "authority": "TENCENT"}
            else:
                self.source_redis_running = False
                result = {"phase": "complete", "source_role": "RETIRED_STOPPED", "authority": "TENCENT"}
        else:
            raise AssertionError("Unexpected fixed source action "+method)
        if irreversible and result.get("phase") == "complete":
            self.source_cache[method] = copy.deepcopy(result)
        if irreversible:
            # Actual SourceOperations._save revalidates the owned resource and
            # refreshes checked_at even when the original effect is complete.
            result["checked_at"] = self.clock.now()
        self.hit(operation, "after", label)
        return result

    def prime(self, state, *, pg_source_stopped=False, redis_transferred=False):
        self.saved = copy.deepcopy(state)
        self.ingress.update(paused=True, quiet=True)
        self.writers.update(held=True, quiet=True)
        self.redis.update(paused=True, quiet=True, pubsub_cache_ready=False)
        self.pg.update(paused=True)
        if state["pg"] == "TENCENT":
            self.pg_source_running, self.pg_authority = False, "TENCENT"
            self.accepted = True
            self.pg.update(endpoint="target", paused=False)
            self.endpoints["postgres"] = "target"
        if pg_source_stopped:
            self.pg_source_running = False
        if redis_transferred:
            self.source_redis_role, self.target_redis_role = "slave", "master"
            self.redis["tracked_sockets"] = 0


def window_state(clock, *, pg="COMMIT_INTENT", redis="AWS", irreversible=True, pause_seconds=0):
    return initial(pg=pg, redis=redis, attempt=1, pause_seconds=pause_seconds,
        window={"kind": "pg" if pg != "TENCENT" else "redis", "started_at": clock.now(),
                "started_monotonic": clock.monotonic(), "boot_id": "test-boot-1", "elapsed_floor": 0.0,
                "deadline": clock.now()+M.TOTAL_PAUSE_BUDGET-pause_seconds, "irreversible": irreversible},
        final_lsn="10/2", quiet_sha256="b"*64,
        pg_arm={"arm_id": "a"*32, "status": "ARMED", "expires_at": clock.now()+6})


class DurableRunnerTests(unittest.TestCase):
    def make(self, **kwargs):
        clock = Clock()
        sleeper = mock.patch.object(M.time, "sleep", side_effect=clock.advance)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        backend = Backend(clock, **kwargs)
        return clock, backend, backend.engine(initial())

    def calls(self, backend, name):
        return [entry for entry in backend.events if entry[0] == name and entry[1] == "before"]

    def assert_order(self, backend, before, after):
        operations = [event[0] for event in backend.events if event[1] == "before"]
        self.assertLess(operations.index(before), operations.index(after), (before, after))

    def test_combined_handover_uses_one_window_and_fixed_macro_route(self):
        clock, backend, engine = self.make(rpc_wire=True, source_wire=True)
        engine.handover_all()
        self.assertEqual((engine.state["pg"], engine.state["redis"]), ("TENCENT", "TENCENT"))
        self.assertIsNone(engine.state["window"])
        self.assertEqual(engine.state["attempt"], 1)
        self.assertTrue(backend.runtime()["ready"])
        calls = [event[0] for event in backend.events if event[1] == "before" and event[0].startswith("rpc.")]
        self.assertEqual(calls, ["rpc."+action for action in (
            "app.quiesce", "pg.arm-quiet", "pg.commit", "pg.route",
            "redis.preroute", "app.quiet-snapshot", "redis.inspect", "redis.persist-primary",
            "app.persist-endpoints", "app.restore")])
        self.assertEqual(backend.promotions, 1)
        self.assertEqual(backend.stop_commands, 1)
        self.assertEqual(backend.failover_commands, 1)
        self.assert_order(backend, "rpc.pg.route", "rpc.redis.preroute")
        self.assert_order(backend, "rpc.app.persist-endpoints", "rpc.app.restore")

    def test_combined_precommit_timeout_aborts_with_one_restore_and_narrow_fences(self):
        clock, backend, engine = self.make()
        backend.delays["rpc.pg.arm-quiet"] = 6
        with self.assertRaisesRegex(M.Refusal, "abort-before-source-stop-budget"):
            engine.handover_all()
        self.assertTrue(backend.runtime()["ready"])
        self.assertEqual((engine.state["pg"], engine.state["redis"]), ("AWS", "AWS"))
        self.assertEqual(backend.stop_commands, 0)
        self.assertEqual(backend.failover_commands, 0)
        self.assertFalse(self.calls(backend, "source.inspect"))
        self.assertEqual(len(self.calls(backend, "rpc.app.restore")), 1)
        self.assertGreater(engine.state["pause_seconds"], 6)
        self.assertEqual(backend.arm["status"], "CANCELLED")

    def test_combined_rejects_wrong_quiet_nonce_before_stopping_source(self):
        clock, backend, engine = self.make()
        original = backend.rpc
        def mismatch(label, action, **fields):
            result = original(label, action, **fields)
            if action == "pg.arm-quiet":
                result["quiet_nonce"] = "wrong-nonce"
            return result
        with mock.patch.object(backend, "rpc", side_effect=mismatch), \
             self.assertRaisesRegex(M.Refusal, "missing-or-replayed"):
            engine.handover_all()
        self.assertEqual(backend.stop_commands, 0)
        self.assertEqual(backend.promotions, 0)
        self.assertEqual(backend.arm["status"], "CANCELLED")
        self.assertIsNone(engine.state["window"])
        self.assertTrue(backend.runtime()["ready"])

    def test_combined_same_attempt_reexecution_uses_new_nonce_and_refuses_cached_arm(self):
        clock, backend, engine = self.make()
        backend.fault("rpc.pg.arm-quiet", when="after", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_all()
        label = engine.label("pg.arm-quiet")
        old = copy.deepcopy(backend.rpc_requests[label][1])
        with self.assertRaisesRegex(M.Refusal, "same-rpc-key-different-request"):
            backend.rpc(label, "pg.arm-quiet", **{**old, "quiet_nonce": "f"*32})
        self.assertEqual(backend.stop_commands, 0)
        restarted = backend.engine()
        restarted.recover()
        self.assertTrue(backend.runtime()["ready"])
        self.assertEqual(backend.arm["status"], "CANCELLED")

    def test_combined_requires_five_seconds_remaining_before_source_stop(self):
        clock, backend, engine = self.make()
        backend.delays["rpc.pg.arm-quiet"] = 3.05
        with self.assertRaisesRegex(M.Refusal, "abort-before-source-stop-budget"):
            engine.handover_all()
        self.assertEqual(backend.stop_commands, 0)
        self.assertEqual(backend.promotions, 0)
        self.assertEqual(backend.failover_commands, 0)
        self.assertFalse(self.calls(backend, "source.pg_stop"))
        self.assertFalse(self.calls(backend, "rpc.pg.commit"))
        self.assertEqual(backend.arm["status"], "CANCELLED")
        self.assertEqual(len(self.calls(backend, "rpc.app.restore")), 1)
        self.assertEqual((engine.state["pg"], engine.state["redis"]), ("AWS", "AWS"))
        self.assertIsNone(engine.state["window"])
        self.assertTrue(backend.runtime()["ready"])
        self.assertGreater(engine.remaining(), M.PRECOMMIT_RESERVE)
        self.assertLess(engine.remaining(), 5.0)

    def test_combined_quiet_proof_blocks_failed_accepted_work_before_authority_change(self):
        clock, backend, engine = self.make()
        backend.task_overrides["usage_records"] = {"active": 0, "pending": 0, "failed": 1, "dropped": 0}
        with self.assertRaises(M.Refusal):
            engine.handover_all()
        self.assertEqual(backend.stop_commands, 0)
        self.assertEqual(backend.failover_commands, 0)
        self.assertFalse(self.calls(backend, "rpc.pg.arm-quiet"))
        self.assertTrue(backend.runtime()["ready"])

    def test_combined_process_death_after_pg_stop_recovers_forward_through_both_data(self):
        clock, backend, engine = self.make()
        backend.fault("source.pg_stop", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_all()
        self.assertFalse(backend.pg_source_running)
        recovered = backend.engine()
        recovered.recover()
        self.assertEqual((recovered.state["pg"], recovered.state["redis"]), ("TENCENT", "TENCENT"))
        self.assertIsNone(recovered.state["window"])
        self.assertTrue(backend.runtime()["ready"])
        self.assertEqual(backend.stop_commands, 1)
        self.assertEqual(backend.failover_commands, 1)
        self.assertFalse(self.calls(backend, "source.inspect"))

    def test_combined_process_death_after_pg_checkpoint_repairs_route_then_redis(self):
        clock, backend, engine = self.make()
        backend.fault("rpc.pg.route", when="before", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_all()
        self.assertEqual(backend.saved["pg"], "TENCENT")
        self.assertEqual(backend.saved["redis"], "AWS")
        recovered = backend.engine()
        recovered.recover()
        self.assertEqual((recovered.state["pg"], recovered.state["redis"]), ("TENCENT", "TENCENT"))
        self.assertTrue(backend.runtime()["ready"])

    def test_combined_failover_process_death_recovers_with_retirement_deferred(self):
        clock, backend, engine = self.make()
        backend.fault("source.redis_failover", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_all()
        recovered = backend.engine()
        recovered.recover()
        self.assertEqual(recovered.state["redis"], "TENCENT")
        self.assertTrue(backend.runtime()["ready"])
        self.assertEqual(backend.failover_commands, 1)
        self.assertEqual(backend.promotions, 1)
        self.assertTrue(backend.source_redis_running)
        self.assertFalse(self.calls(backend, "source.redis_role_persist"))
        recovered.retire_source_redis()
        self.assertFalse(backend.source_redis_running)

    def test_source_redis_retirement_is_after_admission_and_lost_reply_reuses_same_signal(self):
        clock, backend, engine = self.make()
        engine.handover_all()
        self.assertTrue(backend.runtime()["ready"])
        self.assertTrue(backend.source_redis_running)
        self.assertEqual(backend.source_redis_role, "slave")
        self.assertEqual(backend.target_redis_role, "master")
        self.assertEqual(engine.state["redis_target_run_id"], backend.target_redis_run_id)
        backend.fault("source.redis_role_persist", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.retire_source_redis()
        self.assertFalse(backend.source_redis_running)
        self.assertIsNone(backend.saved["window"])
        backend.engine().retire_source_redis()
        self.assertTrue(backend.runtime()["ready"])
        self.assert_order(backend, "rpc.app.restore", "source.redis_role_persist")
        self.assertEqual(len({e[2] for e in self.calls(backend, "source.redis_role_persist")}), 1)

    def test_deferred_retirement_refuses_missing_or_replaced_target_identity(self):
        clock, backend, engine = self.make()
        engine.handover_all()
        expected = engine.state.pop("redis_target_run_id")
        with self.assertRaisesRegex(M.Refusal, "redis-retirement-identity-missing"):
            engine.retire_source_redis()
        self.assertTrue(backend.source_redis_running)
        engine.state["redis_target_run_id"] = expected
        backend.target_redis_run_id = "e"*40
        with self.assertRaisesRegex(M.Refusal, "target-redis-process-changed"):
            engine.retire_source_redis()
        self.assertIsNone(engine.state["window"])

    def test_combined_quiet_controller_hang_recovers_with_status_fences_and_restore_only(self):
        clock, backend, engine = self.make()
        engine.begin_combined()
        backend.events.clear()
        clock.advance(7.5)
        recovered = backend.engine()
        recovered.recover()
        self.assertTrue(backend.runtime()["ready"])
        self.assertEqual([event[0] for event in backend.events if event[1] == "before" and event[0].startswith("rpc.")],
                         ["rpc.status", "rpc.app.restore"])
        self.assertEqual(len(self.calls(backend, "source.resource_fences")), 1)
        self.assertLess(recovered.state["pause_seconds"], M.TOTAL_PAUSE_BUDGET)

    def test_recovery_past_internal_budget_is_not_a_ten_second_breach(self):
        for previously_used in (0, 2):
            with self.subTest(previously_used=previously_used):
                clock, backend, _ = self.make()
                engine = backend.engine(initial(pause_seconds=previously_used))
                engine.begin_combined()
                clock.advance(8.1-previously_used)
                engine.recover()
                self.assertGreater(engine.state["pause_seconds"], M.TOTAL_PAUSE_BUDGET)
                self.assertLess(engine.state["pause_seconds"], M.USER_LIMIT)
                self.assertFalse(engine.state.get("deadline_breached", False))
                self.assertFalse(backend.saved.get("deadline_breached", False))
                self.assertIsNone(engine.state["window"])
                self.assertTrue(backend.runtime()["ready"])

    def test_recovery_ten_second_breach_is_durable_before_failed_restore(self):
        clock, backend, engine = self.make()
        engine.begin_combined()
        clock.advance(10.1)
        backend.fault("rpc.app.restore", when="before")
        with self.assertRaises(M.Unknown):
            engine.recover()
        self.assertTrue(backend.saved["deadline_breached"])
        self.assertIsNotNone(backend.saved["window"])
        with self.assertRaisesRegex(M.Refusal, "user-wait-deadline-breached"):
            backend.engine().recover()
        self.assertTrue(backend.saved["deadline_breached"])
        self.assertGreater(backend.saved["pause_seconds"], M.USER_LIMIT)
        self.assertIsNone(backend.saved["window"])
        self.assertTrue(backend.runtime()["ready"])

    def test_a_single_request_accumulates_wait_across_pg_and_redis_windows(self):
        clock, backend, engine = self.make()
        engine.begin("pg")
        clock.advance(2)
        engine.finish()
        first = engine.state["pause_seconds"]
        engine.begin("redis")
        clock.advance(2.1)
        engine.finish()
        self.assertGreater(engine.state["pause_seconds"], first+2.1)
        self.assertGreater(backend.request_wait_seconds, 4.1)
        self.assertLessEqual(backend.request_wait_seconds, engine.state["pause_seconds"])
        self.assertLess(engine.remaining(), M.PRECOMMIT_RESERVE+1.5)
        previous_pauses = len(self.calls(backend, "control.ingress.pause"))
        with self.assertRaises(M.Refusal):
            engine.begin("pg")
        self.assertEqual(len(self.calls(backend, "control.ingress.pause")), previous_pauses)

    def test_abort_retry_does_not_reset_used_budget(self):
        clock, backend, engine = self.make()
        engine.begin("pg")
        clock.advance(1.1)
        engine.recover()
        used = engine.state["pause_seconds"]
        attempt = engine.state["attempt"]
        engine.begin("pg")
        self.assertEqual(engine.state["attempt"], attempt+1)
        self.assertGreaterEqual(engine.state["pause_seconds"], used)
        clock.advance(1)
        engine.recover()
        self.assertGreater(engine.state["pause_seconds"], used+1)

    def test_restart_preserves_active_window_start_and_previously_used_budget(self):
        clock, backend, _ = self.make()
        state = window_state(clock, pg="AWS", irreversible=False, pause_seconds=2)
        backend.prime(state)
        clock.advance(1.5)
        backend.boot = "test-boot-2"
        restarted_clock = Clock(clock.now(), monotonic=0)
        restarted = backend.engine(clock=restarted_clock)
        self.assertAlmostEqual(restarted.remaining(), M.TOTAL_PAUSE_BUDGET-3.5)
        self.assertEqual(restarted.state["window"]["started_at"], state["window"]["started_at"])

    def test_wall_clock_rollback_cannot_grant_more_budget_inside_process(self):
        clock, backend, engine = self.make()
        engine.begin("pg")
        clock.advance(3)
        before = engine.remaining()
        clock.tick += 2
        clock.wall -= 2  # Still after started_at; the simple elapsed>=0 check misses this.
        try:
            after = engine.remaining()
        except M.Refusal:
            return  # Fail-closed is also acceptable.
        self.assertLessEqual(after, before, "wall rollback granted a fresh allowance despite advancing monotonic time")

    def test_partial_redis_pause_recovers_by_same_epoch_source_rebuild(self):
        clock, backend, engine = self.make()
        backend.partial_redis_pause = True
        backend.fault("control.redis.pause", when="after")
        with self.assertRaises(M.Unknown):
            engine.handover_postgres()
        self.assertEqual(engine.state["pg"], "AWS")
        self.assertEqual(backend.redis["epoch"], 1)
        self.assertTrue(backend.redis["pubsub_cache_ready"])
        self.assertFalse(backend.ingress["paused"])
        self.assert_order(backend, "control.redis.rebuild", "control.redis.resume")
        self.assertEqual(backend.stop_commands, 0)
        self.assertEqual(backend.failover_commands, 0)

    def test_live_stream_websocket_background_worker_or_old_app_blocks_pause(self):
        for field, value in [("active_requests", 1), ("active_websockets", 1),
                             ("background_workers", True), ("source_app_exited", False),
                             ("route_fenced", False)]:
            with self.subTest(field=field):
                clock, backend, engine = self.make()
                if field == "route_fenced":
                    engine.state[field] = value
                else:
                    setattr(backend, field, value)
                with self.assertRaises(M.Refusal):
                    engine.handover_postgres()
                self.assertFalse(self.calls(backend, "control.ingress.pause"))
                self.assertEqual(backend.stop_commands, 0)
                self.assertEqual(backend.promotions, 0)

    def test_source_pg_stop_lost_reply_reconciles_forward_once(self):
        clock, backend, engine = self.make()
        backend.fault("source.pg_stop", when="after")
        try:
            engine.handover_postgres()
        except M.Unknown:
            pass  # In-process reconciliation may either preserve or absorb the transport error.
        self.assertEqual(engine.state["pg"], "TENCENT")
        self.assertFalse(backend.pg_source_running)
        self.assertEqual(backend.stop_commands, 1)
        self.assertEqual(backend.promotions, 1)
        self.assertFalse(backend.ingress["paused"])
        self.assertEqual(len({event[2] for event in self.calls(backend, "source.pg_stop")}), 1)

    def test_process_death_after_source_stop_recovers_without_new_stop_signal(self):
        clock, backend, engine = self.make()
        backend.fault("source.pg_stop", when="after", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_postgres()
        self.assertEqual(backend.saved["pg"], "COMMIT_INTENT")
        self.assertTrue(backend.saved["window"]["irreversible"])
        self.assertTrue(backend.ingress["paused"])
        clock.advance(.1)
        resumed = backend.engine()
        resumed.recover()
        self.assertEqual(resumed.state["pg"], "TENCENT")
        self.assertEqual(backend.stop_commands, 1)
        self.assertFalse(backend.pg_source_running)
        self.assertFalse(backend.ingress["paused"])

    def test_pending_postgres_stop_reconciles_without_another_signal(self):
        clock, backend, engine = self.make()
        backend.source_pending["pg_stop"] = ["pending", "pending", "pending"]
        try:
            engine.handover_postgres()
        except M.Refusal:
            self.assertTrue(backend.ingress["paused"])
            self.assertEqual(engine.state["pg"], "COMMIT_INTENT")
            self.assertFalse(self.calls(backend, "control.ingress.resume"))
        for _ in range(4):
            if engine.state["window"] is None:
                break
            engine.recover()
        self.assertEqual(engine.state["pg"], "TENCENT")
        self.assertFalse(backend.ingress["paused"])
        self.assertEqual(backend.stop_commands, 1)
        self.assertEqual(len({e[2] for e in self.calls(backend, "source.pg_stop")}), 1)

    def test_accepted_target_intent_survives_process_death_and_arm_expiry(self):
        clock, backend, engine = self.make()
        backend.fault("rpc.pg.commit", when="accepted", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_postgres()
        self.assertEqual(backend.pg_authority, "COMMIT_INTENT")
        clock.advance(6.1)
        resumed = backend.engine()
        resumed.recover()
        self.assertEqual(resumed.state["pg"], "TENCENT")
        self.assertEqual(backend.promotions, 1)
        self.assertFalse(self.calls(backend, "rpc.pg.cancel-arm"))
        self.assertFalse(backend.pg_source_running)

    def test_lost_commit_responses_reconcile_accepted_intent_forward(self):
        for stage in ["accepted", "after"]:
            with self.subTest(stage=stage):
                clock, backend, engine = self.make()
                backend.fault("rpc.pg.commit", when=stage)
                try:
                    engine.handover_postgres()
                except M.Unknown:
                    pass
                self.assertEqual(engine.state["pg"], "TENCENT")
                self.assertEqual(backend.promotions, 1)
                self.assertEqual(backend.stop_commands, 1)
                self.assertFalse(backend.pg_source_running)
                self.assertFalse(backend.ingress["paused"])
                self.assertFalse(self.calls(backend, "rpc.pg.cancel-arm"))

    def test_recorded_commit_without_acceptance_reuses_original_fence_digest(self):
        clock, backend, engine = self.make()
        backend.fault("rpc.pg.commit", when="recorded")
        try:
            engine.handover_postgres()
        except M.Unknown:
            pass
        self.assertEqual(engine.state["pg"], "TENCENT")
        self.assertEqual(backend.promotions, 1)
        self.assertFalse(backend.ingress["paused"])
        self.assertEqual(len({e[2] for e in self.calls(backend, "rpc.pg.commit")}), 1)

    def prepare_redis(self, backend, engine):
        engine.handover_postgres()
        self.assertEqual(engine.state["pg"], "TENCENT")

    def test_redis_failover_lost_reply_only_moves_forward(self):
        clock, backend, engine = self.make()
        self.prepare_redis(backend, engine)
        backend.fault("source.redis_failover", when="after")
        try:
            engine.handover_redis()
        except M.Unknown:
            pass
        self.assertEqual(engine.state["redis"], "TENCENT")
        self.assertEqual(backend.failover_commands, 1)
        self.assertEqual(backend.redis["address"], "127.0.0.1:26379")
        self.assertFalse(backend.source_redis_running)
        self.assertFalse(backend.ingress["paused"])

    def test_unknown_redis_roles_hold_ingress_until_same_operation_reconciles(self):
        clock, backend, engine = self.make()
        self.prepare_redis(backend, engine)
        backend.source_pending["redis_failover"] = ["pending", "unknown", "unknown"]
        previous_resumes = len(self.calls(backend, "control.ingress.resume"))
        try:
            engine.handover_redis()
        except M.Refusal:
            self.assertTrue(backend.ingress["paused"])
            self.assertEqual(engine.state["redis"], "COMMIT_INTENT")
            self.assertEqual(len(self.calls(backend, "control.ingress.resume")), previous_resumes)
        for _ in range(4):
            if engine.state["window"] is None:
                break
            engine.recover()
        self.assertEqual(engine.state["redis"], "TENCENT")
        self.assertFalse(backend.ingress["paused"])
        self.assertEqual(backend.failover_commands, 1)
        self.assertEqual(len({e[2] for e in self.calls(backend, "source.redis_failover")}), 1)

    def test_pending_old_redis_retirement_does_not_open_admission(self):
        clock, backend, engine = self.make()
        self.prepare_redis(backend, engine)
        backend.source_pending["redis_role_persist"] = ["pending", "pending"]
        previous_resumes = len(self.calls(backend, "control.ingress.resume"))
        try:
            engine.handover_redis()
        except M.Refusal:
            self.assertTrue(backend.ingress["paused"])
            self.assertTrue(backend.source_redis_running)
            self.assertEqual(len(self.calls(backend, "control.ingress.resume")), previous_resumes)
        engine.recover()
        self.assertEqual(engine.state["redis"], "TENCENT")
        self.assertFalse(backend.source_redis_running)
        self.assertFalse(backend.ingress["paused"])
        self.assertEqual(backend.failover_commands, 1)

    def test_process_death_after_redis_role_transfer_recovers_without_reissuing_failover(self):
        clock, backend, engine = self.make()
        self.prepare_redis(backend, engine)
        backend.fault("source.redis_failover", when="after", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_redis()
        resumed = backend.engine()
        resumed.recover()
        self.assertEqual(resumed.state["redis"], "TENCENT")
        self.assertEqual(backend.failover_commands, 1)
        self.assertEqual(backend.endpoints, {"v": 1, "postgres": "target", "redis": "target"})

    def test_process_death_after_source_redis_retirement_recovers_from_completed_source_ledger(self):
        clock, backend, engine = self.make()
        self.prepare_redis(backend, engine)
        backend.fault("source.redis_role_persist", when="after", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_redis()
        self.assertEqual(backend.saved["redis"], "COMMIT_INTENT")
        self.assertFalse(backend.source_redis_running)
        self.assertEqual(backend.source_cache["redis_failover"]["phase"], "complete")
        self.assertTrue(backend.ingress["paused"])
        resumed = backend.engine()
        resumed.recover()
        self.assertEqual(resumed.state["redis"], "TENCENT")
        self.assertEqual(backend.failover_commands, 1)
        self.assertFalse(backend.source_redis_running)
        self.assertFalse(backend.ingress["paused"])

    def test_replacement_target_redis_process_cannot_reuse_retired_source_proof(self):
        clock, backend, engine = self.make()
        self.prepare_redis(backend, engine)
        backend.fault("source.redis_role_persist", when="after", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_redis()
        backend.target_redis_run_id = "c"*40
        previous_resumes = len(self.calls(backend, "control.ingress.resume"))
        resumed = backend.engine()
        with self.assertRaises(M.Refusal):
            resumed.recover()
        self.assertTrue(backend.ingress["paused"])
        self.assertEqual(len(self.calls(backend, "control.ingress.resume")), previous_resumes)
        self.assertEqual(backend.failover_commands, 1)

    def test_durable_intent_precedes_redis_preroute_and_authority_precedes_persistence(self):
        clock, backend, engine = self.make()
        engine.handover_postgres()
        engine.handover_redis()
        pg_switch = next(i for i, e in enumerate(backend.events) if e[:2] == ("control.postgres.switch", "before"))
        redis_switch = next(i for i, e in enumerate(backend.events) if e[:2] == ("control.redis.switch", "before"))
        persists = [i for i, e in enumerate(backend.events) if e[:2] == ("rpc.app.persist-endpoints", "after")]
        self.assertTrue(any(i < pg_switch for i in persists))
        failover = next(i for i, e in enumerate(backend.events) if e[:2] == ("source.redis_failover", "before"))
        self.assertLess(redis_switch, failover)
        self.assertTrue(any(i > failover for i in persists))
        self.assertEqual(backend.redis["tracked_sockets"], 0)

    def test_process_death_between_checkpoint_and_switch_repairs_target_before_open(self):
        clock, backend, engine = self.make()
        backend.fault("control.postgres.switch", when="before", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_postgres()
        self.assertEqual(backend.endpoints["postgres"], "target")
        self.assertEqual(backend.pg["endpoint"], "source")
        self.assertTrue(backend.ingress["paused"])
        resumed = backend.engine()
        resumed.recover()
        self.assertEqual(backend.pg["endpoint"], "target")
        self.assertFalse(backend.ingress["paused"])
        self.assertFalse(backend.pg_source_running)

    def test_process_death_before_redis_runtime_switch_repairs_target_before_open(self):
        clock, backend, engine = self.make()
        self.prepare_redis(backend, engine)
        backend.fault("control.redis.switch", when="before", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_redis()
        self.assertEqual(backend.endpoints["redis"], "source")
        self.assertEqual(backend.saved["redis"], "COMMIT_INTENT")
        self.assertTrue(backend.saved["window"]["irreversible"])
        self.assertEqual(backend.redis["address"], "127.0.0.1:16379")
        self.assertTrue(backend.ingress["paused"])
        resumed = backend.engine()
        resumed.recover()
        self.assertEqual(backend.redis["address"], "127.0.0.1:26379")
        self.assertFalse(backend.source_redis_running)
        self.assertFalse(backend.ingress["paused"])

    def test_process_death_after_redis_preroute_keeps_paused_source_authority_then_rolls_forward(self):
        clock, backend, engine = self.make()
        self.prepare_redis(backend, engine)
        backend.fault("control.redis.switch", when="after", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_redis()
        self.assertEqual(backend.saved["redis"], "COMMIT_INTENT")
        self.assertTrue(backend.saved["window"]["irreversible"])
        self.assertEqual(backend.endpoints["redis"], "source")
        self.assertEqual(backend.target_redis_role, "slave")
        self.assertEqual(backend.redis["address"], "127.0.0.1:26379")
        self.assertEqual(backend.redis["tracked_sockets"], 0)
        self.assertTrue(backend.ingress["paused"])
        self.assertEqual(backend.failover_commands, 0)
        resumed = backend.engine()
        resumed.recover()
        self.assertEqual(backend.failover_commands, 1)
        self.assertEqual(backend.endpoints["redis"], "target")
        self.assertFalse(backend.source_redis_running)
        self.assertFalse(backend.ingress["paused"])

    def test_unconfirmed_target_does_not_reopen_ingress(self):
        clock, backend, _ = self.make()
        state = window_state(clock)
        backend.prime(state, pg_source_stopped=True)
        backend.arm = copy.deepcopy(state["pg_arm"])
        backend.accepted, backend.pg_authority = True, "COMMIT_INTENT"
        backend.fault("rpc.pg.reconcile", when="before")
        engine = backend.engine()
        with self.assertRaises(M.Unknown):
            engine.recover()
        self.assertTrue(backend.ingress["paused"])
        self.assertFalse(self.calls(backend, "control.ingress.resume"))

    def test_failed_cache_rebuild_keeps_admission_closed(self):
        clock, backend, engine = self.make()
        engine.begin("pg")
        backend.fault("control.redis.rebuild", when="before")
        with self.assertRaises(M.Unknown):
            engine.recover()
        self.assertTrue(backend.ingress["paused"])
        self.assertTrue(backend.writers["held"])
        self.assertFalse(self.calls(backend, "control.ingress.resume"))

    def test_failed_or_dropped_accepted_writes_block_irreversible_actions(self):
        for field in ["active", "pending", "failed", "dropped"]:
            with self.subTest(field=field):
                clock, backend, engine = self.make()
                backend.task_overrides["usage_records"] = {"active": 0, "pending": 0, "failed": 0, "dropped": 0, field: 1}
                try:
                    engine.handover_postgres()
                except M.Refusal:
                    pass
                self.assertEqual(backend.stop_commands, 0, field+" counter was ignored before source stop")
                self.assertEqual(backend.promotions, 0)

    def test_excessive_first_window_cannot_grant_redis_a_fresh_budget(self):
        clock, backend, engine = self.make()
        engine.begin("pg")
        clock.advance(6.2)
        engine.finish()
        engine.state["pg"] = "TENCENT"
        paused_before = len(self.calls(backend, "control.ingress.pause"))
        with self.assertRaises(M.Refusal):
            engine.handover_redis()
        self.assertEqual(len(self.calls(backend, "control.ingress.pause")), paused_before)
        self.assertEqual(backend.failover_commands, 0)

    def test_slow_precommit_aborts_before_stopping_source_and_keeps_total_budget(self):
        clock, backend, engine = self.make()
        backend.delays["rpc.pg.arm"] = 5.5
        with self.assertRaises(M.Refusal):
            engine.handover_postgres()
        self.assertEqual(backend.stop_commands, 0)
        self.assertEqual(backend.promotions, 0)
        self.assertEqual(backend.arm["status"], "CANCELLED")
        self.assertFalse(backend.ingress["paused"])
        self.assertGreater(engine.state["pause_seconds"], 5.5)
        self.assertGreater(engine.remaining(), 0)
        self.assertLess(backend.request_wait_seconds, M.USER_LIMIT)

    def test_hard_10_second_breach_is_durable_even_if_eventual_resume_succeeds(self):
        clock, backend, engine = self.make()
        engine.begin("pg")
        clock.advance(10.1)
        with self.assertRaises(M.Refusal):
            engine.finish()
        self.assertTrue(engine.state["deadline_breached"])
        self.assertTrue(backend.saved["deadline_breached"])
        self.assertGreater(backend.saved["pause_seconds"], M.USER_LIMIT)

    def test_status_reads_never_reuse_mutation_operation_keys(self):
        clock, backend, engine = self.make()
        engine.handover_postgres()
        engine.handover_redis()
        for name, when, label, at in backend.events:
            if when == "before" and (name.endswith(".status") or name == "rpc.redis.inspect"):
                self.assertIsNone(label, name+" reused a stable read key")

    def test_expired_arm_is_cancelled_before_rearming_after_source_stop(self):
        clock, backend, _ = self.make()
        state = window_state(clock)
        state["pg_arm"]["expires_at"] = clock.now()-.1
        backend.prime(state, pg_source_stopped=True)
        backend.arm = copy.deepcopy(state["pg_arm"])
        engine = backend.engine()
        engine.recover()
        self.assertEqual(engine.state["pg"], "TENCENT")
        self.assert_order(backend, "rpc.pg.cancel-arm", "rpc.pg.arm")
        self.assertEqual(len({e[2] for e in self.calls(backend, "source.pg_stop")}), 1)

    def test_lost_arm_reply_is_cancelled_before_reopening_aws(self):
        clock, backend, engine = self.make()
        backend.fault("rpc.pg.arm", when="after")
        try:
            engine.handover_postgres()
        except M.Unknown:
            pass
        self.assertEqual(backend.stop_commands, 0)
        self.assertEqual(backend.promotions, 0)
        self.assertEqual(backend.arm["status"], "CANCELLED",
                         "remote PG arm survived rollback after its reply was lost")
        self.assertFalse(backend.ingress["paused"])
        self.assert_order(backend, "rpc.pg.cancel-arm", "control.ingress.resume")

    def test_process_death_after_remote_arm_recovers_without_local_arm_checkpoint(self):
        clock, backend, engine = self.make()
        backend.fault("rpc.pg.arm", when="after", exception=ProcessDeath)
        with self.assertRaises(ProcessDeath):
            engine.handover_postgres()
        self.assertNotIn("pg_arm", backend.saved)
        self.assertEqual(backend.arm["status"], "ARMED")
        resumed = backend.engine()
        resumed.recover()
        self.assertEqual(backend.arm["status"], "CANCELLED")
        self.assertFalse(backend.ingress["paused"])
        self.assertEqual(backend.stop_commands, 0)

    def test_unknown_arm_cancellation_holds_ingress_until_same_cancel_reconciles(self):
        clock, backend, engine = self.make()
        backend.fault("rpc.pg.arm", when="after")
        backend.fault("rpc.pg.cancel-arm", when="after")
        with self.assertRaises(M.Unknown):
            engine.handover_postgres()
        self.assertEqual(backend.arm["status"], "CANCELLED")
        self.assertTrue(backend.ingress["paused"])
        self.assertFalse(self.calls(backend, "control.ingress.resume"))
        engine.recover()
        self.assertFalse(backend.ingress["paused"])
        self.assertEqual(backend.stop_commands, 0)
        self.assertEqual(len({e[2] for e in self.calls(backend, "rpc.pg.cancel-arm")}), 1)

    def test_recovery_must_inspect_accepted_target_intent_before_opening_original_endpoint(self):
        clock, backend, _ = self.make()
        state = window_state(clock, pg="AWS", irreversible=False)
        state.pop("pg_arm")
        backend.prime(state)
        # Contradictory checkpoint is uncertain, not proof that a remote intent
        # never happened. A rollback copy/restore must not silently revive AWS.
        backend.accepted, backend.pg_authority = True, "COMMIT_INTENT"
        engine = backend.engine()
        try:
            engine.recover()
        except M.Refusal:
            pass
        self.assertTrue(backend.ingress["paused"] or backend.pg["endpoint"] == "target",
                        "recovery reopened AWS despite an accepted target promotion intent")

    def test_actual_rpc_status_shape_supports_forward_pg_recovery(self):
        clock, backend, _ = self.make(rpc_wire=True)
        state = window_state(clock)
        backend.prime(state, pg_source_stopped=True)
        backend.arm = copy.deepcopy(state["pg_arm"])
        engine = backend.engine()
        engine.recover()
        self.assertEqual(engine.state["pg"], "TENCENT")

    def test_actual_redis_persist_result_requires_fresh_authority_status(self):
        clock, backend, _ = self.make(rpc_wire=True)
        state = window_state(clock, pg="TENCENT", redis="COMMIT_INTENT")
        backend.prime(state, redis_transferred=True)
        engine = backend.engine()
        engine.recover()
        self.assertEqual(engine.state["redis"], "TENCENT")
        self.assertTrue(self.calls(backend, "rpc.status"), "persisted:true alone does not carry authority")

    def test_actual_source_fence_shape_is_accepted_without_invented_restart_field(self):
        clock, backend, _ = self.make(source_wire=True)
        state = window_state(clock)
        backend.prime(state, pg_source_stopped=True)
        backend.arm = copy.deepcopy(state["pg_arm"])
        engine = backend.engine()
        engine.recover()
        self.assertEqual(engine.state["pg"], "TENCENT")


def authorized_cohort(previous=None, identifier="01"*16):
    previous = previous or initial(owner="ab"*16, phase="SOURCE_DATA", attempt=1,
                                  pause_seconds=7.0820385516, deadline_breached=False)
    record = {"v": 1, "run": M.RUN, "owner": previous["owner"], "id": identifier,
              "previous_state": copy.deepcopy(previous), "previous_state_sha256": M.digest(previous),
              "proof_sha256": "cd"*32}
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()+b"\n"
    state = copy.deepcopy(previous)
    state["budget_cohort"] = {"id": identifier, "record_sha256": hashlib.sha256(raw).hexdigest()}
    return state, record, raw


class CohortAccountingTests(unittest.TestCase):
    def setUp(self):
        self.state, self.record, self.raw = authorized_cohort()

    def account(self, state=None, raw=None):
        return M.budget_accounting(self.state if state is None else state,
                                   lambda ref: self.raw if raw is None else raw)

    def signed_change(self, mutation):
        record = copy.deepcopy(self.record); mutation(record)
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()+b"\n"
        state = copy.deepcopy(self.state)
        state["budget_cohort"]["record_sha256"] = hashlib.sha256(raw).hexdigest()
        return state, raw

    def test_legacy_budget_never_reads_an_unreferenced_record(self):
        reader = mock.Mock(side_effect=AssertionError("must-not-read"))
        result = M.budget_accounting(initial(pause_seconds=7.08), reader)
        reader.assert_not_called()
        self.assertEqual(result, {"total_pause_seconds": 7.08, "cohort_pause_seconds": 7.08,
                                  "max_cohort_pause_seconds": 7.08, "cohort_id": None})

    def test_authorized_boundary_retains_global_total_and_prior_maximum(self):
        self.state["pause_seconds"] += 6
        result = self.account()
        self.assertAlmostEqual(result["total_pause_seconds"], 13.0820385516)
        self.assertAlmostEqual(result["cohort_pause_seconds"], 6)
        self.assertAlmostEqual(result["max_cohort_pause_seconds"], 7.0820385516)

    def test_unknown_corrupt_or_changed_record_never_grants_budget(self):
        with self.assertRaisesRegex(M.Refusal, "record-unavailable"):
            M.budget_accounting(self.state, lambda ref: (_ for _ in ()).throw(FileNotFoundError()))
        with self.assertRaisesRegex(M.Refusal, "digest-mismatch"):
            self.account(raw=self.raw+b" ")
        self.state["budget_cohort"]["id"] = "02"*16
        with self.assertRaisesRegex(M.Refusal, "record-identity"):
            self.account()

    def test_no_freeform_offset_null_or_path_selected_reference(self):
        references = [None, {"id": "../escape", "record_sha256": "a"*64},
                      {"id": "01"*16, "record_sha256": "a"*64, "base_pause_seconds": 7.08}]
        for ref in references:
            with self.subTest(ref=ref), self.assertRaises(M.Refusal):
                self.account({**self.state, "budget_cohort": ref})

    def test_forged_record_identity_previous_snapshot_or_extra_base_is_rejected(self):
        changes = [lambda r: r.update(v=2), lambda r: r.update(owner="ff"*16),
                   lambda r: r.update(base_pause_seconds=7.08),
                   lambda r: r["previous_state"].update(pause_seconds=0),
                   lambda r: r.update(proof_sha256="not-a-digest")]
        for index, change in enumerate(changes):
            state, raw = self.signed_change(change)
            with self.subTest(index=index), self.assertRaises(M.Refusal):
                self.account(state, raw)

    def test_unsafe_previous_state_cannot_authorize_even_with_matching_digest(self):
        changes = [lambda p: p.update(phase="PG_TARGET_CONFIRMED"), lambda p: p.update(pg="COMMIT_INTENT"),
                   lambda p: p.update(window={"irreversible": False}), lambda p: p.update(route_fenced=False),
                   lambda p: p.update(deadline_breached=True), lambda p: p.update(budget_accounting_unknown=True),
                   lambda p: p.update(pause_seconds=10.01), lambda p: p.update(attempt=0)]
        for index, change in enumerate(changes):
            previous = copy.deepcopy(self.record["previous_state"]); change(previous)
            state, record, raw = authorized_cohort(previous)
            with self.subTest(index=index), self.assertRaises(M.Refusal):
                self.account(state, raw)

    def test_total_or_attempt_rollback_is_rejected(self):
        for field, value in (("pause_seconds", 7), ("attempt", 0)):
            with self.subTest(field=field), self.assertRaisesRegex(M.Refusal, "history-regressed"):
                self.account({**self.state, field: value})

    def test_chain_derives_prior_maximum_without_trusting_a_declared_maximum(self):
        records = {self.state["budget_cohort"]["id"]: self.raw}
        previous = copy.deepcopy(self.state); previous.update(pause_seconds=16.0820385516, attempt=2)
        next_state, _, raw = authorized_cohort(previous, "02"*16)
        records["02"*16] = raw; next_state["pause_seconds"] += 3
        result = M.budget_accounting(next_state, lambda ref: records[ref["id"]])
        self.assertAlmostEqual(result["total_pause_seconds"], 19.0820385516)
        self.assertAlmostEqual(result["cohort_pause_seconds"], 3)
        self.assertAlmostEqual(result["max_cohort_pause_seconds"], 9)

    def test_duplicate_keys_nonfinite_cycles_and_overlong_chains_are_rejected(self):
        raw = self.raw.replace(b'"v":1', b'"v":1,"v":1', 1)
        state = copy.deepcopy(self.state); state["budget_cohort"]["record_sha256"] = hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(M.Refusal, "duplicate-key"): self.account(state, raw)
        for bad in (float("nan"), float("inf"), -1, True, 10**1000):
            with self.subTest(bad=bad), self.assertRaises(M.Refusal): self.account({**self.state, "pause_seconds": bad})
        previous = copy.deepcopy(self.state)
        state, _, raw = authorized_cohort(previous)
        with self.assertRaisesRegex(M.Refusal, "cycle"): self.account(state, raw)
        state = initial(owner="ab"*16, phase="SOURCE_DATA", attempt=1, pause_seconds=1)
        records = {}
        for index in range(M.MAX_COHORT_DEPTH+1):
            state, _, raw = authorized_cohort(state, format(index+1, "032x"))
            records[state["budget_cohort"]["id"]] = raw
        with self.assertRaisesRegex(M.Refusal, "chain-too-deep"):
            M.budget_accounting(state, lambda ref: records[ref["id"]])

    def test_default_reader_requires_private_root_file_and_rejects_symlink_directory(self):
        directory = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)
        regular = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1, st_size=len(self.raw))
        with mock.patch.object(M.pathlib.Path, "lstat", return_value=directory), \
             mock.patch.object(M.pathlib.Path, "stat", return_value=directory), \
             mock.patch.object(M.os, "open", return_value=99) as opening, \
             mock.patch.object(M.os, "fstat", return_value=regular), \
             mock.patch.object(M.os, "read", return_value=self.raw), mock.patch.object(M.os, "close"):
            self.assertEqual(M.read_budget_record(self.state["budget_cohort"]), self.raw)
            self.assertTrue(opening.call_args.args[1] & M.os.O_NOFOLLOW)
            regular.st_mode = stat.S_IFREG | 0o644
            with self.assertRaisesRegex(M.Refusal, "file-untrusted"):
                M.read_budget_record(self.state["budget_cohort"])
        link = types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
        with mock.patch.object(M.pathlib.Path, "lstat", return_value=link), self.assertRaisesRegex(M.Refusal, "directory-untrusted"):
            M.read_budget_record(self.state["budget_cohort"])


class CohortWindowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.backend = Backend(self.clock)
        self.state, _, self.raw = authorized_cohort()
        self.backend.budget_record = lambda ref: self.raw
        self.engine = self.backend.engine(self.state)

    def test_successful_handover_can_exceed_global_ten_without_exceeding_any_cohort(self):
        self.backend.delays["rpc.pg.commit"] = 3.1
        self.engine.handover_all()
        self.assertGreater(self.engine.state["pause_seconds"], 10)
        self.assertLess(M.budget_accounting(self.engine.state, self.backend.budget_record)["max_cohort_pause_seconds"], 10)
        self.assertFalse(self.engine.state["deadline_breached"])
        self.assertEqual(self.engine.state["pg"], "TENCENT")
        self.assertEqual(self.engine.state["redis"], "TENCENT")
        self.assertEqual(self.backend.stop_commands, 1)
        self.assertEqual(self.backend.failover_commands, 1)

    def test_recovery_closes_window_by_adding_to_global_total(self):
        self.engine.begin_combined(); self.clock.advance(8.1)
        self.engine.recover()
        self.assertGreater(self.engine.state["pause_seconds"], 15)
        self.assertFalse(self.engine.state["deadline_breached"])
        self.assertTrue(self.backend.runtime()["ready"])

    def test_ten_second_breach_and_unknown_flag_are_monotonic_across_restoration(self):
        self.engine.begin_combined(); self.clock.advance(10.1)
        with self.assertRaisesRegex(M.Refusal, "user-wait-deadline-breached"): self.engine.recover()
        self.assertTrue(self.engine.state["deadline_breached"])
        self.assertIsNone(self.engine.state["window"])
        self.assertTrue(self.backend.runtime()["ready"])
        with self.assertRaises(M.Refusal): self.backend.engine().begin_combined()

    def test_restart_and_unknown_rpc_do_not_reissue_cohort_budget(self):
        self.engine.begin_combined(); self.clock.advance(1.1); self.engine.recover()
        used = self.engine.state["pause_seconds"]
        restarted = self.backend.engine()
        self.assertAlmostEqual(restarted.remaining(), 8-(used-self.state["pause_seconds"]))
        restarted.begin_combined(); self.clock.advance(1.1); restarted.recover()
        self.assertGreater(restarted.state["pause_seconds"], used+1.1)
        self.assertGreater(restarted.state["attempt"], self.engine.state["attempt"])
        self.assertEqual(restarted.state["budget_cohort"], self.state["budget_cohort"])

    def test_record_disappearing_inside_window_restores_admission_but_cannot_renew_budget(self):
        self.engine.begin_combined(); self.clock.advance(1)
        self.backend.budget_record = lambda ref: (_ for _ in ()).throw(FileNotFoundError())
        self.engine.record_reader = self.backend.budget_record
        with self.assertRaisesRegex(M.Refusal, "user-wait-deadline-breached"): self.engine.recover()
        self.assertTrue(self.backend.runtime()["ready"])
        self.assertTrue(self.engine.state["deadline_breached"])
        self.assertTrue(self.engine.state["budget_accounting_unknown"])
        self.assertGreater(self.engine.state["pause_seconds"], self.state["pause_seconds"]+1)
        self.backend.budget_record = lambda ref: self.raw
        with self.assertRaises(M.Refusal): self.backend.engine().begin_combined()

    def test_missing_record_before_begin_has_no_pause_or_source_effect(self):
        self.engine.record_reader = lambda ref: (_ for _ in ()).throw(FileNotFoundError())
        with self.assertRaises(M.Refusal): self.engine.begin_combined()
        self.assertEqual(self.backend.events, [])
        self.assertIsNone(self.engine.state["window"])


if __name__ == "__main__":
    unittest.main()
