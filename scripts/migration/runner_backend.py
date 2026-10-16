#!/usr/bin/env python3
"""Fixed AWS backend for durable_runner; importing or direct CLI is offline.

No installer or production entry point lives here. The root installer owns
configuration, key adoption, unit lifecycle and the independent recovery guard.
"""
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import secrets
import stat
import subprocess
import sys
import time


def _sibling(name):
    if name not in ("durable_runner", "source_operations", "target_rpc"):
        raise RuntimeError("UNKNOWN_SIBLING")
    path = pathlib.Path(__file__).with_name(name + ".py")
    for key in (name, "_sub2api_migration_" + name):
        existing = sys.modules.get(key)
        if existing is not None and pathlib.Path(getattr(existing, "__file__", "/missing")).resolve() == path.resolve():
            return existing
    key = name if name not in sys.modules else "_sub2api_migration_" + name
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


engine = _sibling("durable_runner")
source_module = _sibling("source_operations")
rpc_module = _sibling("target_rpc")
Refusal, Unknown = engine.Refusal, engine.Unknown
RUN = "20261016"
SCOPE = pathlib.Path("/var/lib/sub2api-migration-20261016/runner")
TARGET = "root@43.165.177.136"
REMOTE_PROGRAM = "/usr/local/libexec/sub2api-migration-20261016/target_rpc.py"
KEY_PATH = SCOPE / "target-rpc.key"
KNOWN_HOSTS_PATH = SCOPE / "target-rpc.known_hosts"
SOCKET_PATH = SCOPE / "rpc-control.sock"
FILES = {"config": "runner-config.json", "state": "state.json", "transport": "transport-state.json",
         "attestations": "source-attestations.json"}
SOURCE_READS = {"inspect", "resource_fences", "pg_final_evidence", "redis_roles"}
SOURCE_MUTATIONS = {"disable_app_restart", "app_stop", "retire_pg_monitors", "pg_stop", "redis_failover", "redis_role_persist"}
# The real Go runtime registers these probes from migrationRequiredWriters.
# A partial or empty task map cannot prove that accepted writes are drained.
REQUIRED_WRITERS = frozenset({
    "timing_wheel", "scheduled_writers", "plugins", "prompt_audit", "prompt_config", "auth_outbox", "ops_runtime",
    "usage_records", "billing_cache", "content_moderation", "detached_writers", "deferred_last_used",
    "audit_log", "ops_ingress_reject", "ops_system_log",
})
LABEL = r"[A-Za-z0-9_.:-]{1,160}"
MAX_STORAGE = 8 * 1024 * 1024


def need(value, code):
    if not value:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def integer(value):
    return type(value) is int and 0 <= value < 2**63


def validate_config(value):
    need(type(value) is dict and set(value) == {"v", "run", "owner", "machine_id_sha256", "initial_target_epoch", "ssh_key_sha256", "known_hosts_sha256"}, "CONFIG_FIELDS")
    need(type(value["v"]) is int and value["v"] == 1 and value["run"] == RUN and
         source_module.match(value["owner"], source_module.HEX32) and integer(value["initial_target_epoch"]) and
         all(source_module.match(value[key], source_module.HEX64) for key in ("machine_id_sha256", "ssh_key_sha256", "known_hosts_sha256")), "CONFIG_IDENTITY")
    return copy.deepcopy(value)


def operation_id(owner, namespace, label):
    need(source_module.match(label, LABEL), "INVALID_OPERATION_LABEL")
    return sha({"run": RUN, "owner": owner, "namespace": namespace, "label": label})[:32]


class FileStore:
    """Short local flock protects fsync writes only; never spans an RPC."""
    def __init__(self):
        self.config = None

    def read(self, kind):
        need(kind in FILES, "UNKNOWN_STATE_FILE")
        try:
            return rpc_module.strict_json(rpc_module.private_read(SCOPE / FILES[kind], limit=MAX_STORAGE), MAX_STORAGE)
        except FileNotFoundError:
            if kind == "transport":
                return None
            raise Refusal("STATE_FILE_MISSING") from None
        except rpc_module.Refusal:
            raise Refusal("STATE_FILE_UNTRUSTED") from None

    @contextlib.contextmanager
    def lock(self):
        fd = None
        try:
            source_module.trusted_parent(SCOPE, private=True)
            fd = os.open(SCOPE / "backend-state.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, "UNTRUSTED_STATE_LOCK")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        except source_module.Refusal:
            raise Refusal("UNTRUSTED_STATE_DIRECTORY") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _audit(self, kind, value):
        path = SCOPE / "audit.jsonl"
        if path.exists() or path.is_symlink():
            rpc_module.private_read(path, limit=MAX_STORAGE)
        record = canonical({"v": 1, "run": RUN, "owner": self.config["owner"], "at": time.time(),
                            "kind": kind, "sha256": sha(value)}) + b"\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, "UNTRUSTED_AUDIT")
            remaining = memoryview(record)
            while remaining:
                written = os.write(fd, remaining)
                need(written > 0, "AUDIT_WRITE_FAILED")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        source_module.fsync_directory(SCOPE)

    def _write(self, kind, value):
        need(kind in ("state", "transport", "attestations") and len(canonical(value)) < MAX_STORAGE, "INVALID_STATE_WRITE")
        try:
            rpc_module.atomic_json(SCOPE / FILES[kind], value)
            self._audit(kind, value)
        except (OSError, rpc_module.Refusal, source_module.Refusal, Refusal):
            raise Unknown("STATE_PERSISTENCE_UNCERTAIN") from None

    def save_state(self, value):
        with self.lock():
            self._write("state", value)

    def attest(self, value):
        with self.lock():
            self._write("attestations", value)

    def update_transport(self, change):
        with self.lock():
            state = self.read("transport")
            if state is None:
                state = {"v": 1, "run": RUN, "owner": self.config["owner"], "target_epoch": self.config["initial_target_epoch"],
                         "operations": {}, "controls": {}, "sources": {}}
            need(type(state) is dict and set(state) == {"v", "run", "owner", "target_epoch", "operations", "controls", "sources"} and
                 state["v"] == 1 and state["run"] == RUN and state["owner"] == self.config["owner"] and integer(state["target_epoch"]) and
                 all(type(state[key]) is dict for key in ("operations", "controls", "sources")), "TRANSPORT_LEDGER_IDENTITY")
            result = change(state)
            self._write("transport", state)
            return copy.deepcopy(result)


class SSHTransport:
    """Fixed forced-command peer. Key material never enters argv or exceptions."""
    def __init__(self, config):
        self.config = config

    def _verify(self):
        try:
            source_module.trusted_parent(SCOPE, private=True)
            key = source_module.private_bytes(KEY_PATH)
            known_hosts = source_module.private_bytes(KNOWN_HOSTS_PATH)
            need(sha(key) == self.config["ssh_key_sha256"] and sha(known_hosts) == self.config["known_hosts_sha256"], "SSH_MATERIAL_CHANGED")
            if SOCKET_PATH.exists() or SOCKET_PATH.is_symlink():
                info = SOCKET_PATH.lstat()
                need(stat.S_ISSOCK(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o077, "UNTRUSTED_CONTROL_SOCKET")
        except (OSError, source_module.Refusal):
            raise Refusal("SSH_MATERIAL_UNTRUSTED") from None

    def argv(self, close=False):
        self._verify()
        options = ["/usr/bin/ssh", "-F", "/dev/null", "-p", "22", "-i", str(KEY_PATH),
                   "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                   "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(KNOWN_HOSTS_PATH),
                   "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=2", "-o", "ConnectionAttempts=1",
                   "-o", "ServerAliveInterval=1", "-o", "ServerAliveCountMax=2", "-o", "LogLevel=ERROR",
                   "-o", "ControlMaster=" + ("no" if close else "auto"), "-o", "ControlPersist=90s",
                   "-o", "ControlPath=" + str(SOCKET_PATH), "-o", "RequestTTY=no", "-o", "ClearAllForwardings=yes",
                   "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "PermitLocalCommand=no",
                   "-o", "ProxyCommand=none", "-o", "ProxyJump=none"]
        if close:
            return options + ["-O", "exit", TARGET]
        return options + [TARGET, "/usr/bin/python3", "-I", REMOTE_PROGRAM, "serve"]

    def request(self, request):
        argv = self.argv()
        try:
            result = subprocess.run(argv, input=canonical(request) + b"\n", capture_output=True, timeout=3,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise Unknown("RPC_TRANSPORT_UNKNOWN") from None
        if len(result.stdout) > 1024 * 1024:
            raise Unknown("RPC_OUTPUT_LIMIT")
        if result.returncode not in (0, 1):
            raise Unknown("RPC_TRANSPORT_UNKNOWN")
        return result.stdout

    def close(self):
        """Close this dedicated ControlMaster only; never touch the data tunnel."""
        argv = self.argv(close=True)
        try:
            result = subprocess.run(argv, input=b"", capture_output=True, timeout=2,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise Unknown("CONTROL_MASTER_CLOSE_UNKNOWN") from None
        return {"closed": result.returncode == 0, "socket_present": SOCKET_PATH.exists()}


class RunnerBackend:
    def __init__(self, config=None, store=None, transport=None, source=None, now=time.time):
        self.store = store if store is not None else FileStore()
        self.config = validate_config(self.store.read("config") if config is None else config)
        self.store.config = self.config
        self.transport = transport if transport is not None else SSHTransport(self.config)
        self.source_ops = source if source is not None else source_module.SourceOperations()
        self.now = now
        need(self.source_ops.m["owner"] == self.config["owner"] and
             self.source_ops.m["machine_id_sha256"] == self.config["machine_id_sha256"], "SOURCE_CONFIG_IDENTITY")
        try:
            self.source_ops._identity()
        except source_module.Refusal as error:
            raise Refusal(str(error)) from None

    def boot_id(self):
        return self.source_ops.b.boot_id()

    def save(self, state):
        need(type(state) is dict and type(state.get("v")) is int and state["v"] == 1 and state.get("run") == RUN and
             state.get("owner") == self.config["owner"], "ENGINE_STATE_IDENTITY")
        canonical(state)
        self.store.save_state(copy.deepcopy(state))

    def _source_request(self, label, method, fields):
        need(source_module.match(label, LABEL), "INVALID_SOURCE_LABEL")
        request = {"method": method, "fields": fields}
        def remember(state):
            entry = state["sources"].get(label)
            if entry is not None:
                need(entry["request"] == request, "SOURCE_LABEL_REUSED")
                return entry
            entry = {"op_id": operation_id(self.config["owner"], "source", label), "request": request, "phase": "intent"}
            state["sources"][label] = entry
            return entry
        return self.store.update_transport(remember)

    def source(self, label, method, **fields):
        need(method in SOURCE_READS | SOURCE_MUTATIONS, "UNKNOWN_SOURCE_METHOD")
        expected = {"timeout_ms"} if method == "redis_failover" else set()
        need(set(fields) == expected, "SOURCE_FIELDS")
        if method in SOURCE_READS:
            try:
                return getattr(self.source_ops, method)()
            except source_module.Unknown:
                raise Unknown("SOURCE_INSPECTION_UNKNOWN") from None
            except source_module.Refusal as error:
                raise Refusal(str(error)) from None
        entry = self._source_request(label, method, fields)
        try:
            result = getattr(self.source_ops, method)(entry["op_id"], **fields)
        except source_module.Unknown:
            raise Unknown("SOURCE_OPERATION_UNKNOWN") from None
        except source_module.Refusal as error:
            raise Refusal(str(error)) from None
        need(type(result) is dict and result.get("phase") in ("complete", "pending", "unknown"), "SOURCE_RESULT_INVALID")
        self.store.update_transport(lambda state: state["sources"][label].update(phase=result["phase"], result=result))
        if result["phase"] != "complete":
            raise Unknown("SOURCE_OPERATION_PENDING" if result["phase"] == "pending" else "SOURCE_OPERATION_UNKNOWN")
        return result

    @staticmethod
    def _is_read(action, fields):
        control = fields.get("control")
        return action in ("status", "redis.inspect", "app.quiet-snapshot") or (action == "app.control" and isinstance(control, str) and control.endswith(".status"))

    def _remember_rpc(self, label, action, fields):
        readonly = self._is_read(action, fields)
        if readonly:
            label = None
        else:
            need(source_module.match(label, LABEL), "MUTATION_REQUIRES_LABEL")
        op_id = secrets.token_hex(16) if label is None else operation_id(self.config["owner"], "rpc", label)
        logical = {"action": action, "fields": fields}
        def remember(state):
            existing = state["operations"].get(op_id)
            if existing is not None:
                need(existing["logical"] == logical and existing["label"] == label, "RPC_LABEL_REUSED")
                return existing["request"]
            request = {"v": 1, "run": RUN, "owner": self.config["owner"], "op_id": op_id,
                       "expected_epoch": state["target_epoch"], "action": action, **fields}
            try:
                rpc_module.parse_request(canonical(request))
            except rpc_module.Refusal:
                raise Refusal("RPC_REQUEST_FIELDS") from None
            state["operations"][op_id] = {"label": label, "logical": logical, "request": request,
                                          "request_sha256": sha(request), "phase": "intent"}
            return request
        return self.store.update_transport(remember)

    def _validated_response(self, request, raw):
        try:
            reply = rpc_module.strict_json(raw, 1024 * 1024)
        except rpc_module.Refusal:
            raise Unknown("RPC_RESPONSE_INVALID") from None
        if type(reply) is not dict:
            raise Unknown("RPC_RESPONSE_INVALID")
        expected = {"ok", "code", "run", "owner", "op_id", "action", "epoch", "phase", "result"}
        if set(reply) != expected:
            raise Unknown("RPC_ENVELOPE_UNCERTAIN")
        valid = (reply["run"] == RUN and reply["owner"] == self.config["owner"] and reply["op_id"] == request["op_id"] and
             reply["action"] == request["action"] and integer(reply["epoch"]) and type(reply["result"]) is dict and
             type(reply["ok"]) is bool and source_module.match(reply["code"], r"[A-Z][A-Z0-9_]{0,95}") and
             reply["ok"] is (reply["code"] == "OK") and reply["epoch"] >= request["expected_epoch"])
        if not valid:
            raise Unknown("RPC_RESPONSE_IDENTITY")
        return reply

    def _response(self, request, raw):
        reply = self._validated_response(request, raw)
        def record(state):
            state["target_epoch"] = max(state["target_epoch"], reply["epoch"])
            state["operations"][request["op_id"]].update(phase="complete" if reply["code"] != "UNKNOWN" else "unknown", response=reply)
        self.store.update_transport(record)
        return reply

    def observe_control(self, name):
        """Fresh waiting-loop observation, with no operation or epoch writes.

        Authority changes and handover evidence retain the durable RPC path.
        A changed peer epoch fails closed instead of silently adopting it.
        """
        need(name in rpc_module.CONTROLS and rpc_module.CONTROLS[name][0] == "GET" and
             name.endswith(".status"), "OBSERVATION_REQUIRES_STATUS")
        state = self.store.read("transport")
        need(type(state) is dict and set(state) == {"v", "run", "owner", "target_epoch", "operations", "controls", "sources"} and
             type(state["v"]) is int and state["v"] == 1 and state["run"] == RUN and state["owner"] == self.config["owner"] and
             integer(state["target_epoch"]) and all(type(state[key]) is dict for key in ("operations", "controls", "sources")),
             "TRANSPORT_LEDGER_IDENTITY")
        request = {"v": 1, "run": RUN, "owner": self.config["owner"], "op_id": secrets.token_hex(16),
                   "expected_epoch": state["target_epoch"], "action": "app.observe", "control": name}
        try:
            rpc_module.parse_request(canonical(request))
        except rpc_module.Refusal:
            raise Refusal("RPC_REQUEST_FIELDS") from None
        try:
            raw = self.transport.request(copy.deepcopy(request))
        except (OSError, subprocess.TimeoutExpired):
            raise Unknown("RPC_TRANSPORT_UNKNOWN") from None
        reply = self._validated_response(request, raw)
        need(reply["code"] == "OK" and reply["epoch"] == request["expected_epoch"] and
             reply["phase"] in ("AWS", "TENCENT", "COMMIT_INTENT"), "OBSERVATION_NOT_CONFIRMED")
        result = reply["result"]
        need(set(result) == {"http_status", "body"} and type(result["http_status"]) is int and
             result["http_status"] == 200 and type(result["body"]) is dict and bool(result["body"]),
             "OBSERVATION_RESULT_INVALID")
        return result["body"]

    def rpc(self, label, action, **fields):
        need(action in rpc_module.ACTION_FIELDS, "UNKNOWN_RPC_ACTION")
        need(action != "app.observe", "USE_OBSERVE_CONTROL")
        readonly = self._is_read(action, fields)
        for _ in range(2 if readonly else 1):
            request = self._remember_rpc(label, action, fields)
            try:
                raw = self.transport.request(copy.deepcopy(request))
            except Unknown:
                raise
            except (OSError, subprocess.TimeoutExpired):
                raise Unknown("RPC_TRANSPORT_UNKNOWN") from None
            reply = self._response(request, raw)
            if reply["code"] == "OK":
                return reply["result"]
            if readonly and reply["code"] == "EPOCH_CONFLICT":
                continue
            if reply["code"] == "UNKNOWN" or reply["result"].get("reconcile_required") is True:
                raise Unknown("RPC_OPERATION_UNKNOWN")
            raise Refusal("RPC_" + reply["code"])
        raise Unknown("RPC_READ_EPOCH_UNSTABLE")

    def control(self, label, name, endpoint=None):
        need(name in rpc_module.CONTROLS, "UNKNOWN_CONTROL")
        need(endpoint is None or (name.endswith(".switch") and endpoint in ("source", "target")), "CONTROL_ENDPOINT")
        if name.endswith(".switch"):
            need(endpoint is not None, "CONTROL_ENDPOINT_REQUIRED")
        readonly = name.endswith(".status")
        if readonly:
            need(endpoint is None, "CONTROL_STATUS_ENDPOINT")
            result = self.rpc(None, "app.control", control=name, control_epoch=0)
        else:
            need(source_module.match(label, LABEL), "CONTROL_REQUIRES_LABEL")
            def lookup(state):
                entry = state["controls"].get(label)
                if entry is not None:
                    need(entry["name"] == name and entry["endpoint"] == endpoint, "CONTROL_LABEL_REUSED")
                return entry
            entry = self.store.update_transport(lookup)
            if entry is None:
                current = self.control(None, name.split(".")[0] + ".status")
                need(integer(current.get("epoch")), "CONTROL_EPOCH_MISSING")
                fields = {"control": name, "control_epoch": current["epoch"]}
                if endpoint is not None:
                    fields["endpoint"] = endpoint
                def remember(state):
                    existing = state["controls"].get(label)
                    if existing is not None:
                        need(existing["name"] == name and existing["endpoint"] == endpoint, "CONTROL_LABEL_REUSED")
                        return existing
                    entry = {"name": name, "endpoint": endpoint, "request": fields, "rpc_label": "control:" + label}
                    state["controls"][label] = entry
                    return entry
                entry = self.store.update_transport(remember)
            result = self.rpc(entry["rpc_label"], "app.control", **entry["request"])
        need(type(result) is dict and type(result.get("http_status")) is int and type(result.get("body")) is dict, "CONTROL_RESULT_INVALID")
        if not 200 <= result["http_status"] < 300:
            raise Unknown("CONTROL_HTTP_UNCERTAIN")
        # Resume endpoints may return 204. Preserve the original mutation and
        # fetch a NEW status operation, never regenerate its epoch/request.
        if result["http_status"] == 204 or not result["body"]:
            need(not readonly, "CONTROL_STATUS_EMPTY")
            return self.control(None, name.split(".")[0] + ".status")
        return result["body"]

    def attest(self, state, runtime, snapshot=None, sampled_started=None):
        need(type(state) is dict and state.get("run") == RUN and state.get("owner") == self.config["owner"] and type(runtime) is dict, "ATTESTATION_INPUT")
        started, boot = self.now(), self.boot_id()
        if snapshot is None:
            need(sampled_started is None, "ATTESTATION_SAMPLE_INPUT")
            target = self.rpc(None, "app.quiet-snapshot")
        else:
            # Only the just-returned fixed quiesce RPC may reuse its snapshot.
            # Its source-side start time includes HTTP work and transport.
            need(type(sampled_started) in (int, float) and math.isfinite(sampled_started) and
                 state.get("window", {}).get("boot_id") == boot, "ATTESTATION_SAMPLE_INPUT")
            started, target = sampled_started, snapshot
        need(all(type(target.get(key)) is dict for key in ("ingress", "writers", "redis", "postgres", "pg")), "ATTESTATION_SNAPSHOT_INVALID")
        ingress, writers, redis, postgres = (target[key] for key in ("ingress", "writers", "redis", "postgres"))
        fresh = target.get("app", {})
        need(type(fresh) is dict and fresh.get("http_status") == 200 and type(fresh.get("body")) is dict, "ATTESTATION_RUNTIME_UNAVAILABLE")
        fresh = fresh["body"]
        need(runtime.get("instance_id") == fresh.get("instance_id") and isinstance(fresh.get("instance_id"), str) and fresh["instance_id"], "APP_INSTANCE_CHANGED")
        tasks = fresh.get("tasks")
        readiness, fresh_ingress = fresh.get("readiness"), fresh.get("ingress")
        # Public readiness deliberately becomes false while ingress/data are
        # paused. Use the independent quiet controls and complete writer
        # registry instead of requiring the application to accept new work.
        runtime_quiet = (fresh.get("serving") is True and fresh.get("draining") is False and
                         all(type(fresh.get(field)) is int and fresh[field] == 0 for field in ("active_requests", "active_hijacked_connections")) and
                         fresh.get("background_workers_enabled") is False and type(readiness) is dict and
                         readiness.get("writer_coverage") is True and readiness.get("quiet_controls") is True and
                         type(tasks) is dict and REQUIRED_WRITERS <= tasks.keys() and
                         all(type(row) is dict and all(type(row.get(field)) is int and row[field] == 0 for field in ("active", "pending", "failed", "dropped")) for row in tasks.values()))
        base_quiet = (runtime_quiet and ingress.get("paused") is True and ingress.get("quiet") is True and
                      type(fresh_ingress) is dict and fresh_ingress.get("paused") is True and fresh_ingress.get("quiet") is True and
                      integer(ingress.get("epoch")) and fresh_ingress.get("epoch") == ingress["epoch"] and
                      writers.get("held") is True and writers.get("quiet") is True)
        redis_paused = (redis.get("paused") is True and redis.get("quiet") is True and type(redis.get("active_operations")) is int and redis["active_operations"] == 0)
        pg_paused = (postgres.get("paused") is True and type(postgres.get("active_operations")) is int and
                     postgres["active_operations"] == 0)
        final_lsn = state.get("final_lsn", "0/0")
        replay_lsn = target.get("pg", {}).get("replay_lsn") or "0/0"
        arm_id = (state.get("pg_arm") or {}).get("arm_id", "0" * 32)
        try:
            source_module.lsn_number(final_lsn)
            source_module.lsn_number(replay_lsn)
        except source_module.Refusal:
            raise Refusal("ATTESTATION_LSN_INVALID") from None
        need(source_module.match(arm_id, source_module.HEX32), "ATTESTATION_ARM_INVALID")
        now = self.now()
        need(boot == self.boot_id() and math.isfinite(now) and 0 <= now - started <= 2, "ATTESTATION_SAMPLE_STALE")
        value = {"v": 1, "run": RUN, "owner": self.config["owner"], "boot_id": boot, "checked_at": now,
                 "app_route_fenced": state.get("route_fenced") is True,
                 "postgres": {"writers_quiet": bool(base_quiet and redis_paused and pg_paused), "final_lsn": final_lsn,
                              "target_replay_lsn": replay_lsn, "arm_id": arm_id},
                 "redis": {"writers_quiet": bool(base_quiet and redis_paused), "consumers_paused": bool(redis_paused)}}
        self.store.attest(value)
        return value

    def close_transport(self):
        return self.transport.close()


if __name__ == "__main__":
    print(json.dumps({"offline": True, "run": RUN, "production_actions": False,
                      "config": str(SCOPE / FILES["config"])}))
