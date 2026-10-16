#!/usr/bin/env python3
"""Fixed AWS migration service. No arguments is an offline plan.

Entry preparation and acceptance are separate, reversible steps. This service
only starts after the target has passed its live canary and the exact bridge
has been installed. The data windows and their recovery need no local client.
"""
import contextlib
import fcntl
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import time


def sibling(name):
    path = pathlib.Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


B = sibling("runner_backend")
E, S, R = B.engine, B.source_module, B.rpc_module
SCOPE = B.SCOPE
COMMENT = "sub2api-live-migration-20261016"
SELFTEST_ATTEMPT = 1000001
RULE = ["-p", "tcp", "-d", "127.0.0.1/32", "--dport", "8081", "-m", "owner",
        "--uid-owner", "997", "-m", "conntrack", "--ctstate", "NEW", "-m", "comment",
        "--comment", COMMENT, "-j", "REDIRECT", "--to-ports", "18081"]


def command(argv):
    try:
        result = subprocess.run(argv, input=b"", capture_output=True, timeout=1.5,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise E.Unknown("ENTRY_PROOF_UNAVAILABLE") from None
    E.need(result.returncode == 0, "ENTRY_PROOF_FAILED")
    return result.stdout


def verify_entry():
    # Never reload the shared Caddy, close its sockets, or alter a conntrack
    # entry. Only new upstream TCP connections match this previously tested rule.
    base = ["/usr/sbin/iptables", "-w", "1", "-t", "nat"]
    command(base + ["-C", "OUTPUT"] + RULE)
    lines = command(base + ["-S", "OUTPUT"]).decode().splitlines()
    rules = [line for line in lines if line.startswith("-A OUTPUT ")]
    E.need(rules and COMMENT in rules[0] and sum(COMMENT in row for row in rules) == 1,
           "ENTRY_RULE_NOT_UNIQUE_AND_FIRST")
    return True


@contextlib.contextmanager
def execution_lock():
    # This is process ownership, not a checkpoint lock. The guard first makes
    # the previous runner cgroup empty; it never waits on a hung owner here.
    S.trusted_parent(SCOPE, private=True)
    fd = os.open(SCOPE / "execution.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        E.need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
               stat.S_IMODE(info.st_mode) == 0o600, "EXECUTION_LOCK_UNTRUSTED")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise E.Refusal("ANOTHER_RUNNER_OWNS_EXECUTION") from None
        yield
    finally:
        os.close(fd)


def read_gates(owner):
    value = S.strict_json(S.private_bytes(SCOPE / "deployment-gates.json"))
    expected = {"v", "run", "owner", "accepted", "app_image_id", "app_binary_sha256",
                "rehearsal_sha256", "live_canary_sha256", "entry_tls_sha256", "source_deployment_paths_fenced"}
    E.need(type(value) is dict and set(value) == expected and value["v"] == 1 and
           value["run"] == E.RUN and value["owner"] == owner and value["accepted"] is True and
           value["source_deployment_paths_fenced"] is True and
           value["app_image_id"] == "sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a" and
           value["app_binary_sha256"] == "0bdcaa371aa45abfa1eb61c763d2da10f133d718c4ad93251587ca0cc6191724" and
           all(S.match(value[key], S.HEX64) for key in ("rehearsal_sha256", "live_canary_sha256", "entry_tls_sha256")),
           "DEPLOYMENT_ACCEPTANCE_MISSING")
    return value


def selftest_armed(owner):
    path = SCOPE / "controller-selftest.json"
    if not path.exists() and not path.is_symlink():
        return False
    value = S.strict_json(S.private_bytes(path))
    E.need(type(value) is dict and set(value) == {"v", "run", "owner", "armed"} and
           value["v"] == 1 and value["run"] == E.RUN and value["owner"] == owner and
           type(value["armed"]) is bool, "SELFTEST_CONFIGURATION_INVALID")
    return value["armed"]


class Service:
    def __init__(self, backend, state, entry=verify_entry, sleep=time.sleep, monotonic=time.monotonic):
        self.io, self.state, self.entry, self.sleep, self.monotonic = backend, state, entry, sleep, monotonic
        self.engine = E.DataHandover(state, backend)

    def save(self, **fields):
        self.engine.save(**fields)

    def source(self, method, **fields):
        return self.io.source("source:" + method, method, **fields)

    def target_runtime(self):
        observer = getattr(self.io, "observe_control", None)
        return observer("runtime.status") if observer else self.io.control(None, "runtime.status")

    @staticmethod
    def ready(runtime):
        return runtime.get("ready") is True and runtime.get("serving") is True

    @staticmethod
    def idle(runtime):
        return (Service.ready(runtime) and runtime.get("active_requests") == 0 and
                runtime.get("active_hijacked_connections") == 0 and
                runtime.get("background_workers_enabled") is False)

    def wait_old_application(self):
        stable = None
        while True:
            self.entry()
            runtime = self.target_runtime()
            E.need(self.ready(runtime), "TARGET_NOT_READY_FOR_SOURCE_DRAIN")
            observed = self.source("inspect")
            app = observed["resources"]["app"]
            E.need(app.get("restart") == "no", "SOURCE_APP_CAN_RESTART")
            if not app.get("running"):
                E.need(app.get("exit_code") == 0 and not app.get("oom_killed"), "SOURCE_APP_EXIT_NOT_CLEAN")
                # The signal may have returned pending before the clean exit.
                # Reconcile the same owned operation; a stopped resource never
                # receives another signal from SourceOperations.app_stop.
                result = self.source("app_stop")
                E.need(result.get("phase") == "complete", "SOURCE_APP_STOP_NOT_RECONCILED")
                return
            if observed["app_connections"].get("inbound_connections") == 0:
                stable = self.monotonic() if stable is None else stable
                if self.monotonic() - stable >= 5:
                    self.save(route_fenced=True, phase="OLD_APP_STOP_INTENT")
                    self.io.attest(self.state, runtime)
                    try:
                        self.source("app_stop")
                    except E.Unknown:
                        pass  # One signal; later loops reconcile that same ID.
            else:
                stable = None
            self.sleep(0.3)

    def retire_monitors(self):
        # This happens outside the admission window; the independent guard
        # already protects retained WAL while the old timer is retired.
        while True:
            try:
                result = self.source("retire_pg_monitors")
                if result.get("phase") == "complete":
                    return
            except E.Unknown:
                pass
            self.sleep(0.2)

    def wait_idle(self, seconds=1.0):
        stable = None
        while True:
            self.entry()
            runtime = self.target_runtime()
            # The real runtime embeds an atomic ingress/request snapshot. The
            # fallback is for older offline fixtures, never a second live read.
            ingress = runtime.get("ingress")
            if ingress is None:
                observer = getattr(self.io, "observe_control", None)
                ingress = observer("ingress.status") if observer else self.io.control(None, "ingress.status")
            if self.idle(runtime) and ingress.get("paused") is False and ingress.get("queued_requests", 0) == 0:
                stable = self.monotonic() if stable is None else stable
                if self.monotonic() - stable >= seconds:
                    return
            else:
                stable = None
            self.sleep(0.25)

    def recover(self):
        # Recovery is permitted even if the entry/gates check is unavailable:
        # restoring an already paused, single-authority application is urgent.
        self.engine.recover()
        return {"phase": self.state.get("phase"), "window_closed": self.state["window"] is None}

    def fault_rehearsal(self):
        # Exercise the actual AWS service/cgroup/target RPC and independent
        # recovery before any customer route reaches the target application.
        # No promotion arm, source lifecycle signal or authority action exists
        # in this path. Root must explicitly arm this once-only rehearsal.
        E.need(self.state.get("phase") == "ENTRY_PREPARATION" and self.state["pg"] == self.state["redis"] == "AWS" and
               self.state["window"] is None and self.state.get("attempt", 0) == 0 and
               self.state.get("route_fenced") is False, "SELFTEST_AFTER_ENTRY_FORBIDDEN")
        # An exact bridge rule must be absent, not merely unverifiable.
        result = subprocess.run(["/usr/sbin/iptables", "-w", "1", "-t", "nat", "-C", "OUTPUT"] + RULE,
                                capture_output=True, timeout=1.5)
        E.need(result.returncode == 1, "SELFTEST_REQUIRES_NO_CUSTOMER_BRIDGE")
        observed = self.source("inspect")
        E.need(all(observed["resources"][key].get("running") is True for key in ("app", "postgres", "redis")),
               "SELFTEST_SOURCE_NOT_LIVE")
        target = self.io.rpc(None, "status")
        E.need(target.get("pg_authority") == target.get("redis_authority") == "AWS" and
               not target.get("pg_arm") and not target.get("pg_promotion_intent") and
               target.get("pg", {}).get("in_recovery") is True and
               target.get("redis", {}).get("role") == "slave" and self.idle(self.target_runtime()),
               "SELFTEST_TARGET_NOT_INITIAL_AND_IDLE")
        self.save(attempt=SELFTEST_ATTEMPT, phase="RECOVERY_SELFTEST", window={"kind": "combined",
                  "started_at": time.time(), "started_monotonic": self.monotonic(), "boot_id": self.io.boot_id(),
                  "elapsed_floor": 0.0, "irreversible": False})
        started = time.time()
        snapshot = self.engine.rpc("app.quiesce")
        instance = snapshot.get("app", {}).get("body", {}).get("instance_id")
        E.need(isinstance(instance, str) and instance, "SELFTEST_INSTANCE_MISSING")
        self.save(quiet_instance_id=instance)
        # Reuse the fixed macro's complete fresh snapshot, including transport.
        proof = self.io.attest(self.state, {"instance_id": instance}, snapshot=snapshot, sampled_started=started)
        E.need(proof["postgres"]["writers_quiet"] is True and proof["redis"]["consumers_paused"] is True,
               "SELFTEST_QUIET_ATTESTATION_FAILED")
        self.save(selftest_paused_at=time.time())
        # Simulate a lost/hung main controller. The independent guard, not this
        # process, must kill the main cgroup and resume the current AWS data.
        while True:
            self.sleep(0.2)

    def run(self):
        if self.state["window"]:
            self.recover()
        if self.state.get("phase") == "COMPLETE":
            return self.verify_complete()
        self.entry()
        self.save(route_fenced=True)
        self.wait_old_application()
        if self.state["pg"] == self.state["redis"] == "AWS":
            self.retire_monitors()
            self.wait_idle()
            self.engine.handover_all()
        E.need(self.state["pg"] == self.state["redis"] == "TENCENT" and not self.state["window"],
               "DATA_AUTHORITY_NOT_COMPLETE")
        self.engine.retire_source_redis()
        observed = self.source("inspect")
        for key in ("app", "postgres", "redis"):
            row = observed["resources"][key]
            E.need(row.get("running") is False and row.get("restart") == "no" and
                   row.get("exit_code") == 0 and not row.get("oom_killed"), "SOURCE_RESOURCE_NOT_RETIRED")
        activation = self.io.control("final:background.activate", "background.activate")
        E.need(activation.get("activated") is True and activation.get("activating") is False and
               not activation.get("pending") and not activation.get("failures"), "BACKGROUND_ACTIVATION_INCOMPLETE")
        self.save(phase="COMPLETE", completed_at=time.time())
        return self.verify_complete()

    def verify_complete(self):
        budget = E.budget_accounting(self.state)
        E.need(not self.state.get("deadline_breached") and
               budget["max_cohort_pause_seconds"] <= E.USER_LIMIT,
               "FINAL_WAIT_BUDGET_FAILED")
        observed = self.io.rpc(None, "status")
        runtime = self.target_runtime()
        pg = self.io.control(None, "postgres.status")
        redis = self.io.control(None, "redis.status")
        ingress = self.io.control(None, "ingress.status")
        E.need(self.state["pg"] == self.state["redis"] == "TENCENT" and not self.state["window"] and
               observed.get("pg_authority") == observed.get("redis_authority") == "TENCENT" and
               observed.get("pg", {}).get("in_recovery") is False and
               observed.get("redis", {}).get("role") == "master" and
               self.ready(runtime) and runtime.get("background_workers_enabled") is True and
               pg.get("endpoint") == "target" and pg.get("paused") is False and
               redis.get("address") == "127.0.0.1:26379" and redis.get("paused") is False and
               redis.get("pubsub_cache_ready") is True and ingress.get("paused") is False,
               "FINAL_FRESH_VERIFICATION_FAILED")
        return {"phase": "COMPLETE", "pg": "TENCENT", "redis": "TENCENT",
                "pause_seconds": self.state["pause_seconds"], "budget": budget,
                "deadline_breached": self.state.get("deadline_breached", False)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(json.dumps({"offline": True, "production_actions": False, "run": E.RUN,
                          "entry_and_canary_acceptance_required": True}))
        return 0
    E.need(argv in (["continue"], ["recover"]), "UNKNOWN_SERVICE_MODE")
    E.need(os.geteuid() == 0, "ROOT_REQUIRED")
    with execution_lock():
        backend = B.RunnerBackend()
        state = backend.store.read("state")
        E.need(state.get("owner") == backend.config["owner"], "STATE_OWNER_CHANGED")
        service = Service(backend, state)
        if argv == ["continue"]:
            if selftest_armed(backend.config["owner"]):
                result = service.fault_rehearsal()
            else:
                read_gates(backend.config["owner"])
                result = service.run()
        else:
            result = service.recover()
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # No subprocess stdout, request bodies, DSNs or token-bearing exception
        # text is journalled. Full root ledgers retain the actionable stage.
        code = str(error) if isinstance(error, E.Refusal) else "SERVICE_OUTCOME_UNCERTAIN"
        if not all(char.isalnum() or char in "_-" for char in code):
            code = "SERVICE_OUTCOME_UNCERTAIN"
        print(json.dumps({"ok": False, "code": code}), file=sys.stderr)
        raise SystemExit(1)
