#!/usr/bin/env python3
"""Durable data-handover state machine. Default CLI only describes the plan.

The installer supplies a fixed-resource backend. This module never accepts a
host, command, SQL, URL, DSN or container name from an operation request.
"""
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import stat
import time

RUN = "20261016"
# Bound all admission intervals within the same proven request cohort. Global
# pause_seconds never resets. Only an immutable root-authorized record proving
# the previous cohort ended may establish a new accounting baseline.
TOTAL_PAUSE_BUDGET = 8.0
USER_LIMIT = 10.0
PRECOMMIT_RESERVE = 2.5
COMBINED_FORWARD_RESERVE = 5.0
BUDGET_COHORTS = pathlib.Path("/var/lib/sub2api-migration-20261016/runner/budget-cohorts")
MAX_COHORT_DEPTH = 16


class Refusal(RuntimeError):
    pass


class Unknown(Refusal):
    pass


def need(value, reason):
    if not value:
        raise Refusal(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def read_budget_record(reference):
    """Read the fixed root-owned immutable record; no state-selected path."""
    identifier = reference.get("id") if type(reference) is dict else None
    need(isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier), "budget-cohort-reference-invalid")
    path = BUDGET_COHORTS / (identifier + ".json")
    for part in [path.parent, *path.parent.parents]:
        info = part.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
             "budget-cohort-directory-untrusted")
    need(stat.S_IMODE(path.parent.stat().st_mode) == 0o700, "budget-cohort-directory-not-private")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == 0o600 and 0 < info.st_size <= 2 * 1024**2,
             "budget-cohort-file-untrusted")
        return os.read(fd, 2 * 1024**2 + 1)
    finally:
        os.close(fd)


def budget_accounting(state, record_reader=None):
    """Derive cohort use without resetting global history or trusting an offset.

    record_reader(reference)->raw bytes is injectable for backend/fault tests.
    With no state reference no record is read, preserving legacy accounting.
    Root's installer owns the admission-drain proof, archive and state CAS.
    """
    reader = read_budget_record if record_reader is None else record_reader
    visited = set()

    def calculate(value, depth):
        need(type(value) is dict and _finite(value.get("pause_seconds")), "budget-pause-invalid")
        total = value["pause_seconds"]
        if "budget_cohort" not in value:
            return {"total_pause_seconds": total, "cohort_pause_seconds": total,
                    "max_cohort_pause_seconds": total, "cohort_id": None}
        need(depth < MAX_COHORT_DEPTH, "budget-cohort-chain-too-deep")
        reference = value["budget_cohort"]
        need(type(value.get("v")) is int and value["v"] == 1 and
             type(reference) is dict and set(reference) == {"id", "record_sha256"} and
             isinstance(reference["id"], str) and re.fullmatch(r"[0-9a-f]{32}", reference["id"]) and
             isinstance(reference["record_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", reference["record_sha256"]),
             "budget-cohort-reference-invalid")
        need(reference["id"] not in visited, "budget-cohort-cycle")
        visited.add(reference["id"])
        try:
            raw = reader(reference)
        except Exception:
            raise Refusal("budget-cohort-record-unavailable") from None
        need(type(raw) is bytes and 0 < len(raw) <= 2 * 1024**2 and
             hashlib.sha256(raw).hexdigest() == reference["record_sha256"], "budget-cohort-record-digest-mismatch")
        def pairs(items):
            result = {}
            for key, item in items:
                need(key not in result, "budget-cohort-duplicate-key")
                result[key] = item
            return result
        try:
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                                parse_constant=lambda _: (_ for _ in ()).throw(Refusal("budget-cohort-nonfinite")))
            need(type(record) is dict and set(record) == {"v", "run", "owner", "id", "previous_state",
                                                          "previous_state_sha256", "proof_sha256"},
                 "budget-cohort-record-fields")
            need(type(record["v"]) is int and record["v"] == 1 and record["run"] == value.get("run") == RUN and
                 record["owner"] == value.get("owner") and isinstance(record["owner"], str) and
                 re.fullmatch(r"[0-9a-f]{32}", record["owner"]) and record["id"] == reference["id"] and
                 isinstance(record["proof_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", record["proof_sha256"]),
                 "budget-cohort-record-identity")
            previous = record["previous_state"]
            need(type(previous) is dict and digest(previous) == record["previous_state_sha256"],
                 "budget-cohort-previous-state-digest")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise Refusal("budget-cohort-record-json-invalid") from None
        need(type(previous.get("v")) is int and previous["v"] == 1 and previous.get("run") == RUN and
             previous.get("owner") == record["owner"] and previous.get("phase") == "SOURCE_DATA" and
             previous.get("pg") == previous.get("redis") == "AWS" and previous.get("window") is None and
             previous.get("route_fenced") is True and type(previous.get("attempt")) is int and previous["attempt"] >= 1 and
             previous.get("deadline_breached", False) is False and previous.get("budget_accounting_unknown", False) is False,
             "budget-cohort-previous-state-not-closed-source")
        prior = calculate(previous, depth + 1)
        need(prior["max_cohort_pause_seconds"] <= USER_LIMIT, "budget-cohort-prior-deadline-breached")
        need(total >= prior["total_pause_seconds"] and type(value.get("attempt")) is int and
             value["attempt"] >= previous["attempt"], "budget-cohort-history-regressed")
        current = total - prior["total_pause_seconds"]
        return {"total_pause_seconds": total, "cohort_pause_seconds": current,
                "max_cohort_pause_seconds": max(prior["max_cohort_pause_seconds"], current),
                "cohort_id": reference["id"]}

    return calculate(state, 0)


def lsn_number(value):
    high, low = value.split("/")
    return (int(high, 16) << 32) + int(low, 16)


class DataHandover:
    """Single-owner orchestrator; independent guard calls recover(), not run().

    backend.control(label, name, endpoint=None) returns a fresh app JSON body;
    backend.rpc(label, action, **fields) returns a verified target result;
    backend.source(label, method, **fields) calls fixed SourceOperations;
    backend.attest(state, runtime) writes source-attestations durably;
    backend.save(state) fsyncs checkpoint and audit record using short locks.
    Call labels are stable idempotency keys. A transport timeout raises Unknown,
    preserves the original operation and must never mean "not executed".
    """
    def __init__(self, state, backend, now=time.time, monotonic=time.monotonic, record_reader=None):
        self.state, self.io, self.now, self.monotonic = state, backend, now, monotonic
        self.record_reader = record_reader if record_reader is not None else getattr(backend, "budget_record", None)
        need(state.get("v") == 1 and state.get("run") == RUN, "wrong-run")
        need(state.get("pg") in ("AWS", "COMMIT_INTENT", "TENCENT") and
             state.get("redis") in ("AWS", "COMMIT_INTENT", "TENCENT"), "unknown-authority")
        state.setdefault("pause_seconds", 0.0)
        state.setdefault("window", None)
        state.setdefault("attempt", 0)

    def save(self, **fields):
        self.state.update(fields)
        self.io.save(self.state)

    def label(self, name):
        return str(self.state["attempt"]) + ":" + name

    def control(self, name, endpoint=None):
        return self.io.control(self.label(name), name, endpoint=endpoint)

    def status(self, component):
        # Read calls must never reuse a previously completed operation ID.
        return self.io.control(None, component + ".status")

    def rpc(self, action, **fields):
        return self.io.rpc(self.label(action), action, **fields)

    def source(self, method, **fields):
        # SourceOperations permits one op_id per lifecycle action across all
        # attempts. Never let a PG arm retry change a source stop identity.
        return self.io.source("source:" + method, method, **fields)

    def source_complete(self, method, **fields):
        # Normal shutdown/FAILOVER returns before the process or role change
        # has finished. Repeatedly inspect the SAME lifecycle operation.
        for _ in range(40):
            try:
                result = self.source(method, **fields)
                if result.get("phase") == "complete":
                    return result
            except Unknown:
                pass
            time.sleep(0.025)
        raise Unknown("source-operation-outcome-still-pending-" + method)

    def window_elapsed(self):
        window = self.state["window"]
        if window:
            # Across a reboot wall time cannot grant a new allowance. A clock
            # moving backwards is uncertain and fails closed.
            if window.get("boot_id") == self.io.boot_id():
                elapsed = self.monotonic() - window["started_monotonic"]
            else:
                elapsed = self.now() - window["started_at"]
                need(elapsed >= window.get("elapsed_floor", 0), "clock-uncertain-after-reboot")
            need(elapsed >= 0, "clock-moved-backwards")
            window["elapsed_floor"] = max(elapsed, window.get("elapsed_floor", 0))
            return elapsed
        return 0.0

    def remaining(self):
        need(not self.state.get("deadline_breached") and not self.state.get("budget_accounting_unknown"),
             "prior-budget-deadline-or-accounting-unknown")
        used = budget_accounting(self.state, self.record_reader)["cohort_pause_seconds"] + self.window_elapsed()
        return TOTAL_PAUSE_BUDGET - used

    def begin(self, kind):
        need(self.state["window"] is None, "previous-window-not-recovered")
        need(self.remaining() >= PRECOMMIT_RESERVE + 1.5, "cumulative-budget-insufficient")
        runtime = self.status("runtime")
        need(runtime.get("ready") is True and runtime.get("serving") is True and
             runtime.get("active_requests") == 0 and runtime.get("active_hijacked_connections") == 0 and
             runtime.get("background_workers_enabled") is False, "not-idle-and-ready")
        source = self.source("inspect")
        app = source["resources"]["app"]
        need(app.get("running") is False and app.get("exit_code") == 0 and
             app.get("restart") == "no" and not app.get("oom_killed") and
             source["app_connections"].get("inbound_connections") == 0 and
             self.state.get("route_fenced") is True,
             "old-application-or-entry-not-fenced")
        self.save(attempt=self.state["attempt"] + 1,
                  window={"kind": kind, "started_at": self.now(),
                          "started_monotonic": self.monotonic(), "boot_id": self.io.boot_id(),
                          "elapsed_floor": 0.0, "deadline": self.now() + self.remaining(), "irreversible": False},
                  phase=kind.upper() + "_WINDOW")
        ingress = self.control("ingress.pause")
        need(ingress.get("paused") is True and ingress.get("quiet") is True,
             "live-request-arrived-before-pause")
        writers = self.control("writers.pause")
        need(writers.get("held") is True and writers.get("quiet") is True,
             "writers-not-fully-drained")
        redis = self.control("redis.pause")
        need(redis.get("paused") is True and redis.get("quiet") is True and
             redis.get("active_operations") == 0, "redis-not-paused")
        runtime = self.status("runtime")
        need(runtime.get("active_requests") == 0 and runtime.get("active_hijacked_connections") == 0,
             "request-still-active")
        for counters in runtime.get("tasks", {}).values():
            need(counters.get("active", 0) == counters.get("pending", 0) == 0,
                 "detached-or-queued-work-still-active")
            need(counters.get("failed", 0) == counters.get("dropped", 0) == 0,
                 "accepted-work-failed-or-dropped")
        self.io.attest(self.state, runtime)

    def begin_combined(self):
        """One admission interval; target-local macros retain every barrier."""
        need(self.state["window"] is None, "previous-window-not-recovered")
        need(self.remaining() >= PRECOMMIT_RESERVE + 1.5, "cumulative-budget-insufficient")
        runtime = self.status("runtime")
        need(runtime.get("ready") is True and runtime.get("serving") is True and
             all(type(runtime.get(k)) is int and runtime[k] == 0 for k in
                 ("active_requests", "active_hijacked_connections")) and
             runtime.get("background_workers_enabled") is False, "not-idle-and-ready")
        # Old HTTP/WS draining and inbound proof already completed in Service.
        # A stopped pinned app cannot accept or write additional work.
        app = self.source("resource_fences")["resources"]["app"]
        need(app.get("running") is False and app.get("restart") == "no" and
             app.get("exit_code") == 0 and app.get("oom_killed") is False and
             self.state.get("route_fenced") is True, "old-application-or-entry-not-fenced")
        remaining = self.remaining()
        self.save(attempt=self.state["attempt"] + 1, phase="COMBINED_WINDOW",
                  window={"kind": "combined", "started_at": self.now(),
                          "started_monotonic": self.monotonic(), "boot_id": self.io.boot_id(),
                          "elapsed_floor": 0.0, "deadline": self.now() + remaining, "irreversible": False})
        started = self.now()
        snapshot = self.rpc("app.quiesce")
        fresh = snapshot.get("app", {}).get("body", {})
        need(fresh.get("instance_id") == runtime.get("instance_id"), "app-instance-changed")
        self.save(quiet_instance_id=fresh["instance_id"])
        self.io.attest(self.state, {"instance_id": self.state["quiet_instance_id"]},
                       snapshot=snapshot, sampled_started=started)

    def quiet_runtime(self):
        # attest() obtains all counters afresh; only instance identity is cached.
        instance = self.state.get("quiet_instance_id")
        need(isinstance(instance, str) and instance, "quiet-instance-missing")
        return {"instance_id": instance}

    def handover_all(self):
        need(self.state["pg"] == self.state["redis"] == "AWS", "wrong-combined-phase")
        try:
            self.begin_combined()
            proof = self.source("pg_final_evidence")
            need(proof.get("quiet") is True and type(proof.get("prepared_transactions")) is int and
                 proof["prepared_transactions"] == 0 and proof.get("unknown_sessions") == [], "source-pg-not-quiet")
            self.save(final_lsn=proof["flushed_lsn"], quiet_sha256=digest(proof), pg_arm_generation=0)
            # A nonce created for this invocation prevents a cached arm reply
            # from being mistaken for a fresh quiet snapshot after a restart.
            # Mutation transport is single-shot; unknown outcomes recover.
            nonce, started = secrets.token_hex(16), self.now()
            result = self.rpc("pg.arm-quiet", final_lsn=proof["flushed_lsn"],
                              quiet_attestation_sha256=digest(proof), quiet_nonce=nonce)
            need(result.get("quiet_nonce") == nonce and type(result.get("quiet_snapshot")) is dict,
                 "target-arm-quiet-proof-missing-or-replayed")
            arm = {key: value for key, value in result.items() if key not in ("quiet_nonce", "quiet_snapshot")}
            need(arm.get("arm_id"), "target-arm-missing")
            self.save(pg_arm=arm)
            self.io.attest(self.state, self.quiet_runtime(), snapshot=result["quiet_snapshot"],
                           sampled_started=started)
            need(self.remaining() >= COMBINED_FORWARD_RESERVE, "abort-before-source-stop-budget")
            self.state["window"]["irreversible"] = True
            self.save(pg="COMMIT_INTENT", phase="PG_SOURCE_STOP_INTENT")
            self.complete_postgres(fast=True, freshly_attested=True)
            self.save(redis="COMMIT_INTENT", phase="REDIS_FAILOVER_INTENT")
            self.complete_redis(fast=True)
            self.finish(fast=True)
        except Exception:
            self.recover()
            raise

    def finish(self, fast=False):
        if fast:
            result = self.rpc("app.restore")
            need(result.get("app", {}).get("body", {}).get("ready") is True and
                 result.get("ingress", {}).get("paused") is False, "admission-not-restored")
            return self.close_window()
        # Always rebuild, even after an aborted pause at the same Redis epoch.
        # Cache rebuilds query SQL: PostgreSQL must be resumed first.
        pg = self.status("postgres")
        if pg.get("paused"):
            self.control("postgres.resume")
        redis = self.status("redis")
        if redis.get("paused"):
            self.control("redis.rebuild")
            self.control("redis.resume")
        if self.status("writers").get("held"):
            self.control("writers.resume")
        self.control("ingress.resume")
        ingress = self.status("ingress")
        runtime = self.status("runtime")
        need(ingress.get("paused") is False and runtime.get("ready") is True,
             "admission-not-restored")
        self.close_window()

    def close_window(self):
        window = self.state["window"]
        if window:
            # The ledger remains a lifetime total; only budget interpretation
            # changes at a proven request-cohort boundary.
            used = self.state["pause_seconds"] + self.window_elapsed()
            closed = {**self.state, "pause_seconds": used, "window": None}
            try:
                breached = budget_accounting(closed, self.record_reader)["max_cohort_pause_seconds"] > USER_LIMIT
            except Refusal:
                breached = True
                self.state["budget_accounting_unknown"] = True
            self.save(pause_seconds=used, window=None,
                      deadline_breached=self.state.get("deadline_breached", False) or breached,
                      phase="DATA_TENCENT" if self.state["pg"] == self.state["redis"] == "TENCENT"
                      else "PG_TENCENT_REDIS_AWS" if self.state["pg"] == "TENCENT" else "SOURCE_DATA")
            need(not self.state["deadline_breached"], "user-wait-deadline-breached")

    def persist_endpoints(self):
        fields = {"postgres": "target" if self.state["pg"] == "TENCENT" else "source",
                  "redis": "target" if self.state["redis"] == "TENCENT" else "source"}
        return self.io.rpc(self.label("app.persist-endpoints." + digest(fields)[:16]),
                           "app.persist-endpoints", **fields)

    def handover_postgres(self):
        need(self.state["pg"] == "AWS" and self.state["redis"] == "AWS", "wrong-pg-phase")
        try:
            self.begin("pg")
            pg = self.control("postgres.pause")
            need(pg.get("paused") is True and pg.get("active_operations") == 0, "postgres-not-drained")
            proof = self.source("pg_final_evidence")
            need(proof.get("quiet") is True and proof.get("prepared_transactions") == 0 and
                 proof.get("unknown_sessions") == [], "source-pg-not-quiet")
            self.save(final_lsn=proof["flushed_lsn"], quiet_sha256=digest(proof), pg_arm_generation=0)
            arm = self.rpc("pg.arm", final_lsn=proof["flushed_lsn"], quiet_attestation_sha256=digest(proof))
            need(arm.get("arm_id"), "target-arm-missing")
            self.save(pg_arm=arm)
            self.io.attest(self.state, self.status("runtime"))
            need(self.remaining() >= PRECOMMIT_RESERVE, "abort-before-source-stop-budget")
            # Mark BEFORE source stop. A lost stop reply may already have fenced
            # the old primary; recovery then only moves forward to Tencent.
            self.state["window"]["irreversible"] = True
            self.save(pg="COMMIT_INTENT", phase="PG_SOURCE_STOP_INTENT")
            self.complete_postgres()
            self.finish()
        except Exception:
            self.recover()
            raise

    def complete_postgres(self, fast=False, freshly_attested=False):
        if not freshly_attested:
            self.io.attest(self.state, self.quiet_runtime() if fast else self.status("runtime"))
        fence = self.source_complete("pg_stop")
        need(fence.get("stopped") is True and fence.get("phase") == "complete",
             "source-pg-stop-not-confirmed")
        # SourceOperations refreshes its observation timestamp on every read.
        # Once a commit request is recorded, its attestation must remain byte
        # identical even if the source stop is reconciled after a lost reply.
        if not self.state.get("source_fence_sha256"):
            self.save(source_fence_sha256=digest(fence))
        # The normal path just armed this exact commit and has not sent any
        # target promotion request yet. commit independently validates the
        # original arm/authority/fence. Recovery must freshly observe authority.
        authority = "AWS" if fast and freshly_attested else self.io.rpc(None, "status")["pg_authority"]
        if authority == "AWS":
            arm = self.state.get("pg_arm")
            if not arm or arm.get("expires_at", 0) <= self.now() + 0.2:
                # This path follows a lost controller/process, with the old PG
                # already stopped. A new arm does not revive the old authority.
                if arm:
                    cancelled = self.io.rpc(self.label("pg.cancel-arm." + str(self.state.get("pg_arm_generation", 0))),
                                            "pg.cancel-arm", arm_id=arm["arm_id"])
                    need(cancelled.get("cancelled") is True, "expired-arm-not-cancelled")
                generation = self.state.get("pg_arm_generation", 0) + 1
                self.save(pg_arm_generation=generation)
                arm = self.io.rpc(self.label("pg.arm." + str(generation)), "pg.arm",
                                  final_lsn=self.state["final_lsn"],
                                  quiet_attestation_sha256=self.state["quiet_sha256"])
                self.save(pg_arm=arm)
            result = self.io.rpc(self.label("pg.commit." + str(self.state.get("pg_arm_generation", 0))),
                                 "pg.commit", arm_id=arm["arm_id"], source_fence_sha256=self.state["source_fence_sha256"])
        else:
            result = self.rpc("pg.reconcile")
        need(result.get("authority") == "TENCENT", "target-pg-authority-unconfirmed")
        self.save(pg="TENCENT", phase="PG_TARGET_CONFIRMED")
        if fast:
            result = self.rpc("pg.route")
            need(result.get("authority") == "TENCENT" and result.get("body", {}).get("endpoint") == "target",
                 "target-pg-route-unconfirmed")
            return
        self.persist_endpoints()
        pg = self.status("postgres")
        if pg.get("endpoint") != "target":
            self.control("postgres.switch", endpoint="target")

    def handover_redis(self):
        need(self.state["pg"] == "TENCENT" and self.state["redis"] == "AWS", "wrong-redis-phase")
        try:
            self.begin("redis")
            source = self.source("inspect")["redis"]["source"]
            target = self.io.rpc(None, "redis.inspect")
            need(source.get("role") == "master" and source.get("master_failover_state") == "no-failover" and
                 target.get("role") == "slave", "redis-pair-not-ready")
            need(self.remaining() >= PRECOMMIT_RESERVE, "abort-before-redis-failover-budget")
            self.state["window"]["irreversible"] = True
            self.save(redis="COMMIT_INTENT", phase="REDIS_FAILOVER_INTENT")
            self.complete_redis()
            self.finish()
        except Exception:
            self.recover()
            raise

    def complete_redis(self, fast=False):
        need(self.state["redis"] == "COMMIT_INTENT" and self.state["window"]["irreversible"] is True,
             "redis-preroute-without-durable-intent")
        # Pause does not close go-redis's idle pool. Selecting the target while
        # all users/consumers are held retires those source sockets without
        # opening a target connection. Persisted startup routing stays source
        # until actual target authority is confirmed below.
        if fast:
            result = self.rpc("redis.preroute")
            need(result.get("body", {}).get("address") == "127.0.0.1:26379", "target-redis-preroute-unconfirmed")
        elif (self.status("redis").get("address") != "127.0.0.1:26379" and
                self.io.rpc(None, "redis.inspect").get("role") == "slave"):
            self.control("redis.switch", endpoint="target")
        # Retry/reconcile the SAME source operation. The SourceOperations ledger
        # must not issue another role command after any unknown result.
        self.io.attest(self.state, self.quiet_runtime() if fast else self.status("runtime"))
        result = self.source_complete("redis_failover", timeout_ms=1200)
        need(result.get("phase") == "complete", "source-redis-failover-not-confirmed")
        source = result["evidence"]["source"]
        expected_target_run_id = result["evidence"]["target"].get("run_id")
        target = self.io.rpc(None, "redis.inspect")
        need(source.get("role") == "slave" and target.get("role") == "master" and
             isinstance(expected_target_run_id, str) and len(expected_target_run_id) == 40 and
             target.get("run_id") == expected_target_run_id and
             source.get("master_failover_state") == target.get("failover_state") == "no-failover",
             "redis-authority-unknown")
        result = self.rpc("redis.persist-primary")
        need(result.get("persisted") is True and
             (result.get("authority") == "TENCENT" and result.get("run_id") == expected_target_run_id if fast else
              self.io.rpc(None, "status").get("redis_authority") == "TENCENT"), "target-redis-not-persisted")
        if not fast:
            retired = self.source_complete("redis_role_persist")
            need(retired.get("phase") == "complete", "old-redis-not-safely-retired")
            # The old Redis process is now unavailable. Cached role persistence is
            # insufficient to authorize opening a replacement target process.
            target = self.io.rpc(None, "redis.inspect")
            need(target.get("role") == "master" and target.get("failover_state") == "no-failover" and
                 target.get("run_id") == expected_target_run_id, "target-redis-process-changed")
        self.save(redis="TENCENT", phase="REDIS_TARGET_CONFIRMED", redis_target_run_id=expected_target_run_id)
        self.persist_endpoints()
        # Recovery may observe an already completed handoff with an older
        # runtime route. Authority is confirmed before repairing that route.
        if not fast and self.status("redis").get("address") != "127.0.0.1:26379":
            self.control("redis.switch", endpoint="target")

    def retire_source_redis(self):
        # Coordinated FAILOVER already left one master. Retiring its old
        # replica can finish after admission opens, before background workers.
        need(self.state["pg"] == self.state["redis"] == "TENCENT" and self.state["window"] is None,
             "redis-retirement-before-authority-or-admission")
        expected = self.state.get("redis_target_run_id")
        need(isinstance(expected, str) and len(expected) == 40, "redis-retirement-identity-missing")
        retired = self.source_complete("redis_role_persist")
        need(retired.get("phase") == "complete", "old-redis-not-safely-retired")
        target = self.io.rpc(None, "redis.inspect")
        need(target.get("role") == "master" and target.get("failover_state") == "no-failover" and
             target.get("run_id") == expected, "target-redis-process-changed")

    def recover(self):
        window = self.state["window"]
        if window is None:
            return
        # Local checkpoints can lag a remotely accepted operation. Inspect the
        # target ledger BEFORE even considering an abort to the old endpoint.
        observed = self.io.rpc(None, "status")
        source = self.source("resource_fences")
        fast = window["kind"] == "combined"
        pg_resource = source["resources"]["postgres"]
        pg_accepted = observed.get("pg_authority") in ("COMMIT_INTENT", "TENCENT")
        pg_stopped = pg_resource.get("running") is False and pg_resource.get("restart") == "no"
        if (pg_accepted or (window["kind"] in ("pg", "combined") and pg_stopped)) and self.state["pg"] != "TENCENT":
            window["irreversible"] = True
            self.save(pg="COMMIT_INTENT")
        redis_accepted = observed.get("redis_authority") in ("COMMIT_INTENT", "TENCENT")
        redis_target = observed.get("redis", {}).get("role") == "master"
        if (redis_accepted or (window["kind"] in ("redis", "combined") and redis_target)) and self.state["redis"] != "TENCENT":
            window["irreversible"] = True
            self.save(redis="COMMIT_INTENT")
        try:
            over_limit = (budget_accounting(self.state, self.record_reader)["cohort_pause_seconds"] +
                          self.window_elapsed()) > USER_LIMIT
        except Refusal:
            over_limit = True
            self.state["budget_accounting_unknown"] = True
        if over_limit:
            self.save(deadline_breached=True)
        if window["irreversible"]:
            if self.state["pg"] == "COMMIT_INTENT":
                self.complete_postgres(fast=fast)
            elif fast and self.state["pg"] == "TENCENT":
                self.rpc("pg.route")
            if fast and self.state["pg"] == "TENCENT" and self.state["redis"] == "AWS":
                # Once SQL has moved, this single interval continues forward.
                self.save(redis="COMMIT_INTENT", phase="REDIS_FAILOVER_INTENT")
            if self.state["redis"] == "COMMIT_INTENT":
                self.complete_redis(fast=fast)
            # A reply can be lost after the authority checkpoint but before the
            # live endpoint switch. Repair to the durable authority before open.
            self.persist_endpoints()
            if not fast and self.state["pg"] == "TENCENT" and self.status("postgres").get("endpoint") != "target":
                self.control("postgres.switch", endpoint="target")
            if not fast and self.state["redis"] == "TENCENT" and self.status("redis").get("address") != "127.0.0.1:26379":
                self.control("redis.switch", endpoint="target")
        else:
            arm = observed.get("pg_arm") or self.state.get("pg_arm")
            if arm:
                result = self.rpc("pg.cancel-arm", arm_id=arm["arm_id"])
                need(result.get("cancelled") is True, "target-pg-arm-cancellation-unknown")
        self.finish(fast=fast)


if __name__ == "__main__":
    print(json.dumps({"offline": True, "remote_mutations": False, "run": RUN,
                      "sum_of_pause_windows_seconds": TOTAL_PAUSE_BUDGET,
                      "requires": ["reviewed fixed backend", "independent recovery guard",
                                   "real end-to-end rehearsal", "installer attestation"]}))
