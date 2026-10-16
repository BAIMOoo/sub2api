#!/usr/bin/env python3
"""One durable attempt to insert the fixed AWS NEW-connection bridge.

Default is offline. Installed root apply/inspect only; no SSH runner, service
lifecycle changes, conntrack changes, deletion, restore, or rollback exists.
"""
import ast
import hashlib
import http.client
import importlib.util
import json
import os
import pathlib
import shlex
import stat
import subprocess
import sys

RUN = "20261016"
LIBEXEC = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016")
PROGRAM = LIBEXEC / "activate_entry_bridge.py"
SCOPE = pathlib.Path("/var/lib/sub2api-migration-20261016/runner/entry-bridge")
COMMENT = "sub2api-live-migration-20261016"
RULE = ["-p", "tcp", "-d", "127.0.0.1/32", "--dport", "8081", "-m", "owner", "--uid-owner", "997",
        "-m", "conntrack", "--ctstate", "NEW", "-m", "comment", "--comment", COMMENT,
        "-j", "REDIRECT", "--to-ports", "18081"]
BASE = ["/usr/sbin/iptables", "-w", "1", "-t", "nat"]
MAIN_UNIT = "sub2api-migration-runner-20261016.service"
PROCESS_UNITS = ("caddy.service", "codex-remote-relay.service", "sub2api-migration-channel-20261016.service")
INSPECTION_REASONS = frozenset({
    "NAT_SNAPSHOT_INVALID", "AMBIGUOUS_BRIDGE_COMMENT", "BEFORE_ALREADY_HAS_BRIDGE",
    "BRIDGE_NOT_FIRST_AND_UNIQUE", "OTHER_NAT_RULES_CHANGED", "BEFORE_SNAPSHOT_CHANGED",
    "SOURCE_PROCESS_CHANGED", "CADDY_PID_INVALID", "CADDY_UID_NOT_997",
    "NAT_INSPECTION_FAILED", "NAT_CHECK_UNCERTAIN", "LOCAL_COMMAND_OUTCOME_UNKNOWN",
    "COMMAND_OUTPUT_LIMIT", "UNKNOWN_UNIT", "UNIT_INSPECTION_FAILED", "UNIT_INSPECTION_INVALID",
})


class Refusal(Exception):
    pass


def need(condition, code):
    if not condition:
        raise Refusal(code)


def sha(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(value).hexdigest()


def verify_controller_rule(raw):
    """Read only literal assignments; never evaluate controller Python."""
    values = {}
    for node in ast.parse(raw).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if name == "COMMENT":
            need(name not in values, "CONTROLLER_RULE_DUPLICATE")
            values[name] = ast.literal_eval(node.value)
        elif name == "RULE":
            need(name not in values and isinstance(node.value, ast.List), "CONTROLLER_RULE_NOT_LITERAL")
            values[name] = [values["COMMENT"] if isinstance(item, ast.Name) and item.id == "COMMENT"
                            else ast.literal_eval(item) for item in node.value.elts]
    need(values == {"COMMENT": COMMENT, "RULE": RULE}, "CONTROLLER_RULE_CHANGED")
    return sha(RULE)


def rule_lines(raw):
    need(isinstance(raw, bytes) and len(raw) < 2 * 1024**2, "NAT_SNAPSHOT_INVALID")
    lines = raw.decode("utf-8").splitlines()
    owned = []
    output = []
    for line in lines:
        tokens = shlex.split(line)
        if tokens[:2] == ["-A", "OUTPUT"]:
            output.append(line)
        if COMMENT in tokens:
            need(tokens.count(COMMENT) == 1 and tokens.index(COMMENT) > 0 and
                 tokens[tokens.index(COMMENT) - 1] == "--comment", "AMBIGUOUS_BRIDGE_COMMENT")
            owned.append(line)
    return lines, owned, output


def verify_nat(before, after, check):
    old, old_owned, _ = rule_lines(before)
    new, owned, output = rule_lines(after)
    need(not old_owned, "BEFORE_ALREADY_HAS_BRIDGE")
    need(check == 0 and len(owned) == 1 and output and output[0] == owned[0], "BRIDGE_NOT_FIRST_AND_UNIQUE")
    need([line for line in new if line != owned[0]] == old, "OTHER_NAT_RULES_CHANGED")


def validate_preflight(value):
    state, target = value["state"], value["target"]
    need(state.get("owner") == value["owner"] and state.get("pg") == state.get("redis") == "AWS" and
         state.get("window") is None and state.get("route_fenced") is False, "SOURCE_NOT_INITIAL_AWS_STATE")
    main = value["main"]
    need(main.get("ActiveState") == "inactive" and main.get("MainPID") == "0" and not main.get("Job"), "MAIN_NOT_INACTIVE")
    need(value.get("selftest_armed") is False and value.get("gates_accepted") is True and
         value.get("guard_healthy") is True and value.get("bridge_health") == 200, "ENTRY_GATES_NOT_ACCEPTED")
    need(set(value["resources"]) == {"app", "postgres", "redis"} and
         all(row.get("running") is True and type(row.get("pid")) is int and row["pid"] > 1
             for row in value["resources"].values()), "SOURCE_RESOURCES_NOT_RUNNING")
    need(target.get("pg_authority") == target.get("redis_authority") == "AWS" and not target.get("pg_arm") and
         not target.get("pg_promotion_intent") and target.get("pg", {}).get("in_recovery") is True and
         target.get("redis", {}).get("role") == "slave" and
         target.get("persistent_endpoints") == {"v": 1, "postgres": "source", "redis": "source"}, "TARGET_NOT_AWS_AWS")
    runtime, pg, redis, background = (value[key] for key in ("runtime", "postgres", "redis", "background"))
    need(runtime.get("ready") is True and runtime.get("serving") is True and runtime.get("draining") is False and
         runtime.get("background_workers_enabled") is False and pg.get("endpoint") == "source" and pg.get("paused") is False and
         redis.get("address") == "127.0.0.1:16379" and redis.get("paused") is False and redis.get("pubsub_cache_ready") is True and
         background.get("activated") is False and background.get("activating") is False and
         background.get("started") == [] and not background.get("failures"),
         "TARGET_NOT_READY_WITH_BACKGROUND_OFF")
    need(set(value["processes"]) == set(PROCESS_UNITS), "PROTECTED_PROCESS_FIELDS")
    for row in value["processes"].values():
        need(row.get("ActiveState") == "active" and row.get("SubState") == "running" and
             row.get("MainPID", "").isdigit() and int(row["MainPID"]) > 1 and not row.get("Job"), "PROTECTED_PROCESS_NOT_RUNNING")
    return value


class LocalBackend:
    def __init__(self, service):
        self.service, self.io = service, service.B.RunnerBackend()
        self.S = service.S

    def command(self, argv):
        try:
            result = subprocess.run(argv, input=b"", capture_output=True, timeout=3,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise Refusal("LOCAL_COMMAND_OUTCOME_UNKNOWN") from None
        need(len(result.stdout) <= 2 * 1024**2, "COMMAND_OUTPUT_LIMIT")
        return result

    def unit(self, name):
        need(name in (*PROCESS_UNITS, MAIN_UNIT), "UNKNOWN_UNIT")
        keys = {"ActiveState", "SubState", "MainPID", "Job", "ExecMainStartTimestampMonotonic"}
        result = self.command(["/usr/bin/systemctl", "show", name, "--property=" + ",".join(sorted(keys))])
        need(result.returncode == 0, "UNIT_INSPECTION_FAILED")
        pairs = [line.split("=", 1) for line in result.stdout.decode().splitlines()]
        value = dict(pairs)
        need(len(value) == len(pairs) and set(value) == keys, "UNIT_INSPECTION_INVALID")
        return value

    def processes(self):
        value = {name: self.unit(name) for name in PROCESS_UNITS}
        pid = value["caddy.service"]["MainPID"]
        need(pid.isdigit() and int(pid) > 1, "CADDY_PID_INVALID")
        rows = pathlib.Path("/proc") / pid / "status"
        uids = [line.split()[1:] for line in rows.read_text().splitlines() if line.startswith("Uid:")]
        need(uids == [["997"] * 4], "CADDY_UID_NOT_997")
        return value

    def resources(self):
        value = {}
        for kind in ("app", "postgres", "redis"):
            info = self.io.source_ops._running(kind)
            if kind == "app":
                self.io.source_ops._no_restart(info)
            value[kind] = {"id": info["Id"], "running": info["State"]["Running"], "pid": info["State"]["Pid"]}
        return value

    def bridge_health(self):
        conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=2)
        try:
            conn.request("GET", "/health", headers={"Connection": "close"})
            reply = conn.getresponse()
            need(len(reply.read(65537)) <= 65536, "BRIDGE_HEALTH_RESPONSE_LIMIT")
            return reply.status
        finally:
            conn.close()

    def preflight(self):
        owner = self.io.config["owner"]
        gates = self.service.read_gates(owner)
        value = {"owner": owner, "machine_id_sha256": self.io.config["machine_id_sha256"],
                 "boot_id": self.io.boot_id(), "gates_sha256": sha(gates), "gates_accepted": gates["accepted"],
                 "target": self.io.rpc(None, "status"), "runtime": self.io.control(None, "runtime.status"),
                 "postgres": self.io.control(None, "postgres.status"), "redis": self.io.control(None, "redis.status"),
                 "background": self.io.control(None, "background.status"), "bridge_health": self.bridge_health(),
                 "resources": self.resources(), "processes": self.processes(), "state": self.io.store.read("state"),
                 "selftest_armed": self.service.selftest_armed(owner), "main": self.unit(MAIN_UNIT)}
        # Read guard last, after bounded remote probes, so its accepted sample is fresh.
        self.io.source_ops._guard()
        value["guard_healthy"] = True
        return validate_preflight(value)

    def nat(self):
        result = self.command(BASE + ["-S"])
        need(result.returncode == 0, "NAT_INSPECTION_FAILED")
        return result.stdout

    def check(self):
        result = self.command(BASE + ["-C", "OUTPUT"] + RULE)
        need(result.returncode in (0, 1), "NAT_CHECK_UNCERTAIN")
        return result.returncode

    def insert(self):
        result = self.command(BASE + ["-I", "OUTPUT", "1"] + RULE)
        need(result.returncode == 0, "NAT_INSERT_OUTCOME_UNKNOWN")


class Store:
    def __init__(self, source, owner, scope=SCOPE):
        self.S, self.owner, self.scope = source, owner, pathlib.Path(scope)

    def read(self):
        path = self.scope / "state.json"
        if not path.exists() and not path.is_symlink():
            return None
        value = self.S.strict_json(self.S.private_bytes(path))
        need(value.get("v") == 1 and value.get("run") == RUN and value.get("owner") == self.owner and
             value.get("rule_sha256") == sha(RULE) and value.get("attempted") is True and
             value.get("phase") in ("INTENT", "UNKNOWN", "COMPLETE"), "BRIDGE_STATE_CHANGED")
        return value

    def snapshot(self, before, nat):
        for name, raw in (("before.json", self.S.canonical(before) + b"\n"), ("before-nat.rules", nat)):
            path = self.scope / name
            if path.exists() or path.is_symlink():
                need(self.S.private_bytes(path) == raw, "EXISTING_BRIDGE_SNAPSHOT_DIFFERS")
            else:
                self.S.atomic_private(path, raw)

    def before_nat(self):
        return self.S.private_bytes(self.scope / "before-nat.rules")

    def save(self, value):
        self.S.atomic_private(self.scope / "state.json", self.S.canonical(value) + b"\n")


class Driver:
    def __init__(self, backend, store):
        self.b, self.s = backend, store

    def inspect(self, state):
        try:
            after, check = self.b.nat(), self.b.check()
            processes, resources = self.b.processes(), self.b.resources()
            before_nat = self.s.before_nat()
            need(sha(before_nat) == state["before_nat_sha256"], "BEFORE_SNAPSHOT_CHANGED")
            verify_nat(before_nat, after, check)
            need(processes == state["before"]["processes"] and resources == state["before"]["resources"], "SOURCE_PROCESS_CHANGED")
            state.update(phase="COMPLETE", reason="VERIFIED", after_nat_sha256=sha(after),
                         processes_unchanged=True, source_resources_unchanged=True)
        except Exception as error:
            reason = str(error) if isinstance(error, Refusal) and str(error) in INSPECTION_REASONS else "INSPECTION_FAILED"
            state.pop("after_nat_sha256", None)
            state.update(phase="UNKNOWN", reason=reason, processes_unchanged=False, source_resources_unchanged=False)
        self.s.save(state)
        return {"phase": state["phase"], "rule_sha256": state["rule_sha256"], "insert_attempted": True,
                "reason": state["reason"], "requires_manual_inspection": state["phase"] != "COMPLETE",
                "automatic_rollback": False}

    def apply(self):
        state = self.s.read()
        if state is not None:
            return self.inspect(state)  # Every replay is observation only.
        before = self.b.preflight()
        nat = self.b.nat()
        need(self.b.check() == 1 and not rule_lines(nat)[1], "BRIDGE_ALREADY_PRESENT_WITHOUT_OWNED_INTENT")
        self.s.snapshot(before, nat)
        fresh = self.b.preflight()
        need(all(fresh[key] == before[key] for key in ("owner", "machine_id_sha256", "boot_id", "gates_sha256", "resources", "processes")),
             "BRIDGE_PREFLIGHT_CHANGED")
        need(self.b.nat() == nat and self.b.check() == 1, "NAT_CHANGED_BEFORE_INSERT")
        state = {"v": 1, "run": RUN, "owner": before["owner"], "rule_sha256": sha(RULE), "before": before,
                 "before_nat_sha256": sha(nat), "phase": "INTENT", "attempted": True}
        self.s.save(state)  # fsync is the final prerequisite to the sole -I.
        try:
            self.b.insert()
        except Exception:
            pass  # A timeout/nonzero is not proof that insertion did not happen.
        return self.inspect(state)


def load_service():
    for parent in [LIBEXEC, *LIBEXEC.parents]:
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "UNTRUSTED_PROGRAM_DIRECTORY")
    for name in ("activate_entry_bridge.py", "runner_service.py", "runner_backend.py", "source_operations.py", "durable_runner.py", "target_rpc.py"):
        path = LIBEXEC / name
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == 0o644, "UNTRUSTED_PROGRAM_FILE")
    verify_controller_rule((LIBEXEC / "runner_service.py").read_bytes())
    spec = importlib.util.spec_from_file_location("bridge_reviewed_service", LIBEXEC / "runner_service.py")
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    need(service.RULE == RULE and service.COMMENT == COMMENT, "IMPORTED_RULE_CHANGED")
    return service


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(json.dumps({"offline": True, "production_actions": False, "rule_sha256": sha(RULE),
                          "protected_units": PROCESS_UNITS, "insert_attempted": False}))
        return 0
    need(argv in (["apply"], ["inspect"]) and os.geteuid() == 0 and sys.platform == "linux" and
         pathlib.Path(__file__).absolute() == PROGRAM, "INSTALLED_AWS_ROOT_REQUIRED")
    service = load_service()
    with service.execution_lock():
        backend = LocalBackend(service)
        service.S.trusted_parent(SCOPE.parent, private=True)
        if not SCOPE.exists() and not SCOPE.is_symlink():
            need(argv == ["apply"], "BRIDGE_INTENT_MISSING")
            SCOPE.mkdir(mode=0o700)
            service.S.fsync_directory(SCOPE.parent)
        service.S.trusted_parent(SCOPE, private=True)
        driver = Driver(backend, Store(service.S, backend.io.config["owner"]))
        if argv == ["apply"]:
            result = driver.apply()
        else:
            state = driver.s.read()
            need(state is not None, "BRIDGE_INTENT_MISSING")
            result = driver.inspect(state)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["phase"] == "COMPLETE" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"phase": "UNKNOWN_OR_REFUSED", "inspect_before_any_further_action": True,
                          "automatic_rollback": False}), file=sys.stderr)
        raise SystemExit(1)
