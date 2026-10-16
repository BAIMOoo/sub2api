#!/usr/bin/env python3
"""Fixed Tencent-host backend for rolling_app_recovery; default CLI is offline.

Install beside the pinned core and original target_rpc.py. Root supplies private
rolling-recovery/host-config.json and the two source receipts first. `step` runs
exactly one core advance; unknown intents remain inspect-only. No RPC.handle,
controller ledger mutation, SSH, shell, docker stop, timeout kill, DNS or Caddy
operation exists here. The private command runner is only called by fixed local
operations; it never receives a CLI-supplied command, path, or container name.
"""
import argparse
import copy
import hashlib
import http.client
import importlib.util
import json
import os
import pathlib
import re
import secrets
import shlex
import stat
import subprocess
import sys

ROOT = pathlib.Path("/var/lib/sub2api-migration-20261016")
SCOPE = ROOT / "rolling-recovery"
LIB = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016")
OLD_NAME = "sub2api-migration-app-20261016"
OLD_ID = "cd5c29cf86b2595fffc41fc068e024d3f790f69bb8bdaaacc63eaba1ac9bcd82"
IMAGE = "sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a"
NEW_NAME = "sub2api-migration-recovery-app-20261017"
MAX_LOG = 64 * 1024 * 1024
MIB = 1024 ** 2
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}


class Refusal(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise Refusal(code)


def raw_sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def value_sha(value):
    return raw_sha(canonical(value))


def private_read(path, *, uid=0, mode=0o600, limit=2 * MIB):
    path = pathlib.Path(path)
    for parent in [path.parent, *path.parent.parents]:
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, uid) and not info.st_mode & 0o022,
             "UNTRUSTED_PARENT")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "UNTRUSTED_FILE")
        raw = b""
        while len(raw) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        need(len(raw) <= limit, "FILE_TOO_LARGE")
        return raw
    finally:
        os.close(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, raw, *, uid=0, gid=0):
    path = pathlib.Path(path)
    if path.exists() or path.is_symlink():
        need(private_read(path, uid=uid, limit=max(len(raw), 2 * MIB)) == raw, "ARCHIVE_OR_PRIVATE_FILE_CHANGED")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as out:
        os.fchmod(out.fileno(), 0o600)
        os.fchown(out.fileno(), uid, gid)
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    sync_directory(path.parent)


def directory_new(path, *, uid=0, gid=0, mode=0o700):
    path = pathlib.Path(path)
    try:
        path.mkdir(mode=mode)
        os.chown(path, uid, gid)
        os.chmod(path, mode)
        sync_directory(path.parent)
    except FileExistsError:
        pass
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and info.st_gid == gid and
         stat.S_IMODE(info.st_mode) == mode, "DIRECTORY_CHANGED")


def process_start_ticks(raw):
    # comm may contain spaces and ')'. The final ') ' ends stat field 2.
    try:
        tail = raw.rsplit(") ", 1)[1].split()
        return int(tail[19])
    except (IndexError, ValueError):
        raise Refusal("PROCESS_STAT_INVALID") from None


def tcp_inbound_count(pid, start_ticks, *, proc=pathlib.Path("/proc"), port=8082):
    """Count accepted sockets owned by this pinned PID, including idle/closing."""
    proc = pathlib.Path(proc)
    def current_start():
        return process_start_ticks((proc / str(pid) / "stat").read_text())
    def inodes():
        found = set()
        for fd in (proc / str(pid) / "fd").iterdir():
            try:
                match = re.fullmatch(r"socket:\[([0-9]+)\]", os.readlink(fd))
            except FileNotFoundError:
                continue
            if match:
                found.add(match.group(1))
        return found
    need(current_start() == start_ticks, "PROCESS_ID_REUSED")
    before = inodes()
    accepted = set()
    for name in ("tcp", "tcp6"):
        rows = (proc / "net" / name).read_text().splitlines()
        need(len(rows) > 0, "TCP_TABLE_MISSING")
        for row in rows[1:]:
            fields = row.split()
            need(len(fields) >= 10, "TCP_TABLE_INVALID")
            try:
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
            except (ValueError, IndexError):
                raise Refusal("TCP_ADDRESS_INVALID") from None
            if local_port == port and fields[3] != "0A" and fields[9] in before:
                accepted.add(fields[9])
    after = inodes()
    need(current_start() == start_ticks, "PROCESS_ID_REUSED")
    # FD churn cannot establish zero, even when a just-accepted socket was not
    # represented in the separately read kernel table.
    return len(accepted) if before == after else max(1, len(accepted))


def normalize_owned_rule(parts, owned_rule):
    if owned_rule is None:
        return parts
    def attributes(row):
        values, modules = {}, []
        index = 0
        while index < len(row):
            key = row[index]
            arity = 2 if key == "--tcp-flags" else 1
            if index + arity >= len(row):
                return None
            value = tuple(row[index + 1:index + 3]) if arity == 2 else row[index + 1]
            index += arity + 1
            if key == "-m":
                modules.append(value)
            elif key in values or key not in {"-A", "-d", "-p", "--ctstate", "--dport", "--tcp-flags", "--uid-owner", "--comment", "-j", "--to-ports"}:
                return None
            else:
                values[key] = value
        if len(set(modules)) != len(modules):
            return None
        if values.get("-d") == "127.0.0.1":
            values["-d"] = "127.0.0.1/32"
        return values, sorted(modules)
    # Only this exact scoped rule is normalized. Every unrelated NAT rule
    # retains its original token order and attributes for baseline comparison.
    wanted = attributes(owned_rule)
    return list(owned_rule) if wanted is not None and attributes(parts) == wanted else parts


def parse_nat(raw, owned_rule=None):
    result = {"output": [], "other": []}
    inside = False
    complete = False
    for line in raw.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        if line == "*nat":
            need(not inside and not complete, "NAT_TABLE_DUPLICATE")
            inside = True
            continue
        if line == "COMMIT":
            need(inside, "NAT_COMMIT_INVALID")
            inside, complete = False, True
            continue
        need(inside, "NAT_TABLE_INVALID")
        if line.startswith(":"):
            match = re.fullmatch(r":([^ ]+) ([^ ]+) \[[0-9]+:[0-9]+\]", line)
            need(match is not None, "NAT_POLICY_INVALID")
            result["other"].append(["policy", match[1], match[2]])
        else:
            parts = shlex.split(line)
            need(len(parts) >= 3 and parts[0] == "-A", "NAT_RULE_INVALID")
            result["output" if parts[1] == "OUTPUT" else "other"].append(normalize_owned_rule(parts, owned_rule))
    need(complete and not inside, "NAT_TABLE_INCOMPLETE")
    return result


def work_summary(runtime, required):
    """The fixed image's idle moderation workers hold gate slots while polling.

    content_moderation.go:worker holds the gate around workerOnce, including its
    empty queue wait. Every queued task first owns a detached-task token until
    its work returns (enqueue/dequeue paths at 1200/1237/1278). Consequently only
    the exact known 32-worker baseline, with zero pending moderation AND zero
    detached active/pending, can be discounted. Raw values remain in evidence.
    """
    tasks = runtime.get("tasks")
    need(type(tasks) is dict and required <= tasks.keys(), "WRITER_COVERAGE_MISSING")
    for row in tasks.values():
        need(type(row) is dict and all(type(row.get(k, 0)) is int and row.get(k, 0) >= 0
             for k in ("active", "pending", "failed", "dropped", "deferred")), "WRITER_STATS_INVALID")
    active = sum(row["active"] for row in tasks.values())
    pending = sum(row["pending"] + row.get("deferred", 0) for row in tasks.values())
    moderation, detached = tasks["content_moderation"], tasks["detached_writers"]
    discounted = (moderation["active"] == 32 and moderation["pending"] == 0 and
                  moderation.get("deferred", 0) == 0 and detached["active"] == detached["pending"] == 0)
    if discounted:
        active -= 32
    ops = tasks["ops_system_log"]
    work = {"http": runtime["active_requests"], "hijacked": runtime["active_hijacked_connections"],
            "business_active": active, "business_pending": pending,
            "ops_active": ops["active"], "ops_pending": ops["pending"] + ops.get("deferred", 0)}
    need(all(type(value) is int and value >= 0 for value in work.values()), "RUNTIME_COUNTS_INVALID")
    return work, discounted


class HostBackend:
    def __init__(self, core, rpc, config, *, runner=subprocess.run, proc=pathlib.Path("/proc")):
        self.m, self.r, self.config = core, rpc, config
        self.run_command, self.proc = runner, pathlib.Path(proc)
        self.rpc_config = rpc.load_config()
        need(self.rpc_config["owner"] == config["owner"] and
             self.rpc_config["app"]["container_id"] == OLD_ID and
             self.rpc_config["app"]["image_id"] == IMAGE and self.rpc_config["app"]["control_port"] == 18092,
             "ORIGINAL_RPC_PINS_REQUIRED")
        self.read_backend = rpc.LocalBackend(self.rpc_config)

    def _command(self, argv, *, limit=2 * MIB, timeout=20):
        # argv is constructed only in the fixed methods below. Never exposed
        # as a request field, CLI flag or public backend operation.
        try:
            result = self.run_command(argv, capture_output=True, timeout=timeout, env=SAFE_ENV)
        except (OSError, subprocess.TimeoutExpired):
            raise Refusal("FIXED_COMMAND_OUTCOME_UNKNOWN") from None
        need(result.returncode == 0 and len(result.stdout) <= limit and len(result.stderr) <= limit,
             "FIXED_COMMAND_OUTCOME_UNKNOWN")
        return result

    def _docker(self, kind, identity):
        need((kind == "image" and identity == IMAGE) or
             (kind == "container" and (identity in (OLD_ID, NEW_NAME) or self.m.digest(identity))), "FIXED_DOCKER_ID_REQUIRED")
        values = self.r.strict_json(self._command(["/usr/bin/docker", kind, "inspect", identity]).stdout, 2 * MIB)
        need(type(values) is list and len(values) == 1 and type(values[0]) is dict, "DOCKER_INSPECT_INVALID")
        return values[0]

    def _new_optional(self):
        try:
            return self._docker("container", NEW_NAME)
        except Refusal:
            names = self._command(["/usr/bin/docker", "container", "ls", "-a", "--format", "{{.Names}}"])
            need(NEW_NAME.encode() not in names.stdout.splitlines(), "NEW_CONTAINER_INSPECTION_UNKNOWN")
            return None

    def _nat(self):
        return parse_nat(self._command(["/usr/sbin/iptables-save", "-t", "nat"]).stdout, self.m.REDIRECT)

    def _environment(self, container):
        rows = container["Config"]["Env"]
        need(type(rows) is list and all(type(row) is str and "=" in row for row in rows), "ENVIRONMENT_INVALID")
        values = dict(row.split("=", 1) for row in rows)
        need(len(values) == len(rows), "ENVIRONMENT_DUPLICATES")
        return values

    def _controls(self, port, token_file):
        token = private_read(token_file, limit=512).strip()
        need(re.fullmatch(rb"[A-Za-z0-9_-]{32,256}", token), "TOKEN_INVALID")
        values = {}
        for name, path in [("runtime_before", "/status"), ("ingress", "/data/ingress/status"),
                           ("writers", "/data/writers/status"), ("postgres", "/data/postgres/status"),
                           ("redis", "/data/redis/status"), ("background", "/data/background/status"),
                           ("runtime", "/status")]:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("GET", path, headers={"Authorization": "Bearer " + token.decode("ascii"), "Connection": "close"})
                reply = connection.getresponse()
                raw = reply.read(65537)
                need(reply.status == 200 and len(raw) <= 65536, "PRIVATE_GET_FAILED")
                value = self.r.strict_json(raw, 65536)
                need(type(value) is dict, "PRIVATE_GET_INVALID")
                values[name] = self.r.sanitize_control(value)
            except (OSError, http.client.HTTPException):
                raise Refusal("PRIVATE_GET_OUTCOME_UNKNOWN") from None
            finally:
                connection.close()
        runtime = values["runtime"]
        need(values["runtime_before"]["instance_id"] == runtime["instance_id"], "RUNTIME_INSTANCE_CHANGED")
        need(values["postgres"].get("endpoint") == "source" and values["postgres"].get("paused") is False and
             values["redis"].get("address") == "127.0.0.1:16379" and values["redis"].get("paused") is False and
             values["redis"].get("pubsub_cache_ready") is True and values["writers"].get("held") is False and
             values["ingress"].get("paused") is False and values["background"].get("activated") is False and
             values["background"].get("activating") is False and runtime.get("background_workers_enabled") is False,
             "APP_SOURCE_CONTROLS_REQUIRED")
        return values

    def _safety(self):
        ledger_raw = self.r.private_read(self.r.SCOPE / "state.json", limit=8 * MIB)
        ledger = self.r.strict_json(ledger_raw, 8 * MIB)
        closure_raw = private_read(SCOPE / "source-controller-closed.json")
        need(raw_sha(closure_raw) == self.config["source_controller_sha256"], "SOURCE_CLOSURE_CHANGED")
        closure = self.r.strict_json(closure_raw, 2 * MIB)
        need(closure.get("owner") == self.config["owner"] and closure.get("run") == "20261016" and
             closure.get("window") is None and closure.get("controller_stopped") is True and
             closure.get("pg") == closure.get("redis") == "AWS" and
             closure.get("target_ledger_sha256") == raw_sha(ledger_raw), "SOURCE_CONTROLLER_CLOSURE_REQUIRED")
        arm = ledger["pg"].get("arm")
        cancelled_arm = type(arm) is dict and arm.get("status") == "CANCELLED"
        need(ledger.get("owner") == self.config["owner"] and ledger.get("run") == "20261016" and
             ledger["pg"]["authority"] == ledger["redis"]["authority"] == "AWS" and
             (arm is None or cancelled_arm) and ledger["pg"].get("promotion_intent") is None,
             "SOURCE_AUTHORITY_REQUIRED")
        quiesces = [op for op in ledger["operations"].values() if op["request"]["action"] == "app.quiesce"]
        need(len(quiesces) > 0 and all(op.get("cancelled") is True for op in quiesces), "QUIESCE_NOT_CANCELLED")
        for op in ledger["operations"].values():
            need(op.get("phase") == "COMPLETE" or
                 (op["request"]["action"] == "app.quiesce" and op.get("cancelled") is True), "UNRESOLVED_RPC_OPERATION")
        need(self.read_backend.endpoint_state() == self.m.ENDPOINTS, "SOURCE_ENDPOINT_CHECKPOINT_REQUIRED")
        # These original helpers only inspect fixed resources and read their
        # existing standby manifest; no RPC handle or FileStore.update is used.
        self.read_backend.resource("pg")
        self.read_backend.resource("redis")
        manifest = self.read_backend.pg_manifest()
        need(manifest.get("authority_mode") == "STANDBY" and manifest.get("promotion_intent") is None,
             "TARGET_PG_STANDBY_REQUIRED")
        pg = self.read_backend.pg_sample()
        redis = self.read_backend.redis_inspect()
        need(pg.get("in_recovery") is True and pg.get("system_identifier") == self.rpc_config["pg"]["system_identifier"] and
             pg.get("source_timeline") == self.rpc_config["pg"]["source_timeline"], "ACTUAL_TARGET_PG_NOT_STANDBY")
        need(redis.get("role") == "slave" and redis.get("run_id") == self.r.REDIS_TARGET_RUN_ID and
             redis.get("failover_state") == "no-failover", "ACTUAL_TARGET_REDIS_NOT_STANDBY")
        return ledger

    def _process(self, container):
        state = container["State"]
        if state["Running"] is False:
            need(state["Pid"] == 0, "STOPPED_PROCESS_PID_INVALID")
            return {"pid": 0, "start_ticks": None, "started_at": state["StartedAt"]}
        pid = state["Pid"]
        need(type(pid) is int and pid > 1, "PROCESS_PID_INVALID")
        ticks = process_start_ticks((self.proc / str(pid) / "stat").read_text())
        return {"pid": pid, "start_ticks": ticks, "started_at": state["StartedAt"]}

    def _app(self, container, *, new=False):
        env = self._environment(container)
        image = self._docker("image", IMAGE)
        need(container["Image"] == image["Id"] == IMAGE and container["HostConfig"]["NetworkMode"] == "host" and
             container["HostConfig"]["RestartPolicy"]["Name"] == "no", "APP_IMAGE_OR_HOST_CHANGED")
        if not new:
            wanted = self.rpc_config["app"]
            need(container["Id"] == OLD_ID and value_sha(container["Config"]) == wanted["config_sha256"] and
                 image["RootFS"]["Layers"] == wanted["rootfs_diffids"], "OLD_APP_PIN_CHANGED")
        else:
            labels = container["Config"].get("Labels") or {}
            need(container.get("Name") == "/" + NEW_NAME and labels.get("io.sub2api.migration.owner") == self.config["owner"] and
                 labels.get("io.sub2api.migration.run") == "sub2api-rolling-recovery-20261017" and
                 container["HostConfig"]["Memory"] == container["HostConfig"]["MemorySwap"] == 512 * MIB and
                 container["HostConfig"]["NanoCpus"] == 1000000000 and container["HostConfig"]["PidsLimit"] == 256,
                 "NEW_APP_HOST_PIN_CHANGED")
        mounts = sorted([{"source": item["Source"], "destination": item["Destination"], "rw": item["RW"]}
                         for item in container["Mounts"]], key=lambda item: item["destination"])
        datas = [item for item in mounts if item["destination"] == "/app/data"]
        need(len(datas) == 1 and datas[0]["rw"] is True and
             datas[0]["source"] == str(self.m.DATA if new else ROOT / "app" / "data"), "APP_DATA_MOUNT_CHANGED")
        files = {name: raw_sha(private_read(pathlib.Path(datas[0]["source"]) / name, uid=1000, limit=MIB)) for name in self.m.FILES}
        process = self._process(container)
        token_file = self.m.TOKEN_FILE if new else self.r.SCOPE / "app-control.token"
        token = private_read(token_file, limit=512).strip()
        need(env.get("MIGRATION_RUNTIME_TOKEN", "").encode() == token, "CONTROL_TOKEN_ENV_CHANGED")
        app = {"container_id": container["Id"], "image_id": IMAGE, "config_sha256": value_sha(container["Config"]),
               "rootfs_diffids": image["RootFS"]["Layers"], "environment_sha256": value_sha(env),
               "business_env_sha256": value_sha({k: v for k, v in env.items() if k not in self.m.ENV_DIFFERENCES}),
               "control_token_sha256": raw_sha(token), "files": files, "endpoints": copy.deepcopy(self.m.ENDPOINTS),
               "state": container["State"]["Status"], "exit_code": container["State"].get("ExitCode"),
               "oom_killed": container["State"].get("OOMKilled"), "name": container["Name"].lstrip("/"),
               "settings": {key: env.get(key) for key in self.m.SETTINGS}, "mounts": mounts,
               "restart": "no", "network": "host", "entrypoint": container["Config"]["Entrypoint"],
               "command": container["Config"]["Cmd"], "control_token_file": str(token_file), "process": process}
        if container["State"]["Running"]:
            controls = self._controls(18093 if new else 18092, token_file)
            runtime = controls["runtime"]
            work, discounted = work_summary(runtime, self.r.REQUIRED_WRITERS)
            work["inbound_tcp"] = 0 if new else tcp_inbound_count(process["pid"], process["start_ticks"], proc=self.proc)
            tasks, logs = runtime["tasks"], runtime["tasks"]["ops_system_log"]
            failures = sum(row.get("failed", 0) for row in tasks.values())
            drops = sum(row.get("dropped", 0) for row in tasks.values())
            if not new:
                need(failures == 1 and drops == 3 and logs.get("failed") == 1 and logs.get("dropped") == 3,
                     "OLD_HISTORICAL_WRITER_SCOPE_CHANGED")
            app.update(ready=runtime.get("ready"), serving=runtime.get("serving"), draining=runtime.get("draining"),
                       ops_failed=logs.get("failed", 0), ops_dropped=logs.get("dropped", 0),
                       all_writer_failures=failures, all_writer_drops=drops, work=work,
                       tcp_scope="all-accepted-including-idle", raw_tasks=tasks,
                       moderation_idle_32_discounted=discounted, instance_id=runtime["instance_id"])
        else:
            app.update(ready=False, serving=False, draining=False, ops_failed=0 if new else 1,
                       ops_dropped=0 if new else 3, all_writer_failures=0 if new else 1,
                       all_writer_drops=0 if new else 3)
        pin_path = SCOPE / ("new-process.json" if new else "history/old-process.json")
        if container["State"]["Running"] and pin_path.exists():
            need(self.r.strict_json(private_read(pin_path), 4096) == process, "APP_PROCESS_RESTARTED")
        elif not new and pin_path.exists():
            pin = self.r.strict_json(private_read(pin_path), 4096)
            need(pin.get("started_at") == process["started_at"], "OLD_PROCESS_RESTARTED_BEFORE_EXIT")
        elif new and container["State"]["Running"]:
            # During start_app only, the core intent authorizes creation of
            # this independent process receipt, never an unknown replay.
            need(getattr(self, "starting_new", False), "NEW_PROCESS_RECEIPT_REQUIRED")
        return app

    def observe(self):
        self._safety()
        old = self._app(self._docker("container", OLD_ID))
        actual_new = self._new_optional()
        new = None if actual_new is None else self._app(actual_new, new=True)
        self._safety()
        return {"window": None, "quiesce_cancelled": True, "pg_authority": "AWS", "redis_authority": "AWS",
                "endpoints": copy.deepcopy(self.m.ENDPOINTS), "sample_id": secrets.token_hex(16),
                "boot_id": (self.proc / "sys/kernel/random/boot_id").read_text().strip(),
                "old": old, "new": new, "nat": self._nat()}

    def _intent(self, kind):
        state = self.r.strict_json(private_read(SCOPE / "state.json"), 2 * MIB)
        need(state.get("owner") == self.config["owner"] and state.get("intent", {}).get("kind") == kind and
             state["intent"].get("status") == "INTENT", "DURABLE_FRESH_INTENT_REQUIRED")
        o = self.observe()
        need(self.m.Recovery._identity(o["old"]) == state["old_identity"] and o["old"]["state"] == "running",
             "OLD_APP_CHANGED_BEFORE_EFFECT")
        if "new_identity" in state:
            self.m.Recovery(self, None, self.config["owner"])._new(state, o["new"], running=kind in ("route", "term"))
        return state, o

    def archive_history(self, spec):
        state, o = self._intent("archive")
        need(spec["old_id"] == OLD_ID and spec["owner"] == self.config["owner"], "ARCHIVE_SPEC_CHANGED")
        source = private_read(SCOPE / "source-recovery-receipt.json")
        need(raw_sha(source) == self.config["source_recovery_sha256"], "SOURCE_RECOVERY_RECEIPT_CHANGED")
        directory_new(self.m.HISTORY_DIRECTORY)
        write_new(self.m.HISTORY_DIRECTORY / "source-recovery-receipt.json", source)
        write_new(self.m.HISTORY_DIRECTORY / "old-runtime.json", canonical(o["old"]) + b"\n")
        write_new(self.m.HISTORY_DIRECTORY / "old-process.json", canonical(o["old"]["process"]) + b"\n")
        logs = self._command(["/usr/bin/docker", "logs", "--timestamps", OLD_ID], limit=MAX_LOG)
        write_new(self.m.HISTORY_DIRECTORY / "dockerlogs.stdout", logs.stdout)
        write_new(self.m.HISTORY_DIRECTORY / "dockerlogs.stderr", logs.stderr)
        manifest = {"v": 1, "owner": self.config["owner"], "historical_failed": 1, "historical_dropped": 3,
                    "files": {name: raw_sha(private_read(self.m.HISTORY_DIRECTORY / name, limit=MAX_LOG)) for name in
                              ("old-runtime.json", "old-process.json", "dockerlogs.stdout", "dockerlogs.stderr")}}
        write_new(self.m.HISTORY_DIRECTORY / "logs-manifest.json", canonical(manifest) + b"\n")
        receipt = {"failed": 1, "dropped": 3, "logs_sha256": raw_sha(canonical(manifest) + b"\n"),
                   "recovery_receipt_sha256": raw_sha(source), "archive_directory": str(self.m.HISTORY_DIRECTORY), "durable": True}
        write_new(self.m.HISTORY_DIRECTORY / "receipt.json", canonical(receipt) + b"\n")

    def inspect_history(self):
        path = self.m.HISTORY_DIRECTORY / "receipt.json"
        if not path.exists():
            need(not path.is_symlink(), "ARCHIVE_SYMLINK")
            return None
        receipt = self.r.strict_json(private_read(path), 4096)
        raw = private_read(self.m.HISTORY_DIRECTORY / "logs-manifest.json")
        need(raw_sha(raw) == receipt["logs_sha256"], "ARCHIVE_MANIFEST_CHANGED")
        manifest = self.r.strict_json(raw, 2 * MIB)
        need(manifest.get("owner") == self.config["owner"] and manifest.get("historical_failed") == 1 and
             manifest.get("historical_dropped") == 3, "ARCHIVE_IDENTITY_CHANGED")
        for name, wanted in manifest["files"].items():
            need(name in ("old-runtime.json", "old-process.json", "dockerlogs.stdout", "dockerlogs.stderr") and
                 raw_sha(private_read(self.m.HISTORY_DIRECTORY / name, limit=MAX_LOG)) == wanted, "ARCHIVE_CONTENT_CHANGED")
        need(set(manifest["files"]) == {"old-runtime.json", "old-process.json", "dockerlogs.stdout", "dockerlogs.stderr"},
             "ARCHIVE_FILES_MISSING")
        need(raw_sha(private_read(self.m.HISTORY_DIRECTORY / "source-recovery-receipt.json")) ==
             receipt["recovery_receipt_sha256"] == self.config["source_recovery_sha256"], "RECOVERY_ARCHIVE_CHANGED")
        return receipt

    def _capacity(self):
        memory = dict(line.split(":", 1) for line in (self.proc / "meminfo").read_text().splitlines())
        need(int(memory["MemAvailable"].split()[0]) * 1024 > 700 * MIB, "MEMAVAILABLE_UNDER_700MIB")
        disk = os.statvfs(SCOPE)
        need(disk.f_bavail * disk.f_frsize > 1024 * MIB, "DISK_UNDER_1GIB")

    def _create_argv(self):
        return ["/usr/bin/docker", "create", "--pull", "never", "--name", NEW_NAME,
                "--label", "io.sub2api.migration.owner=" + self.config["owner"],
                "--label", "io.sub2api.migration.run=sub2api-rolling-recovery-20261017",
                "--network", "host", "--restart", "no", "--memory", str(512 * MIB), "--memory-swap", str(512 * MIB),
                "--cpus", "1", "--pids-limit", "256", "--security-opt", "no-new-privileges:true", "--cap-drop", "NET_RAW",
                "--log-opt", "max-size=8m", "--log-opt", "max-file=2", "--env-file", str(SCOPE / "app.env"),
                "--mount", "type=bind,src=" + str(self.m.DATA) + ",dst=/app/data",
                "--mount", "type=bind,src=" + str(self.m.ENDPOINT_DIRECTORY) + ",dst=/migration-state,readonly", IMAGE]

    def prepare_app(self, spec):
        state, o = self._intent("prepare")
        need(o["new"] is None and self.inspect_history() == state["history"], "PREPARE_PREREQUISITES_CHANGED")
        expected = self.m.Recovery(self, None, self.config["owner"])._spec(state)
        need(spec == expected, "PREPARE_SPEC_CHANGED")
        self._capacity()
        old = self._docker("container", OLD_ID)
        environment = self._environment(old)
        directory_new(SCOPE / "app")
        directory_new(self.m.DATA, uid=1000, gid=1000)
        for name, wanted in spec["files"].items():
            raw = private_read(ROOT / "app" / "data" / name, uid=1000, limit=MIB)
            need(raw_sha(raw) == wanted, "SOURCE_BUSINESS_FILE_CHANGED")
            write_new(self.m.DATA / name, raw, uid=1000, gid=1000)
        token = secrets.token_urlsafe(48)
        write_new(self.m.TOKEN_FILE, token.encode() + b"\n")
        environment.update(SERVER_PORT="8083", MIGRATION_RUNTIME_ADDR="127.0.0.1:18093", MIGRATION_RUNTIME_TOKEN=token)
        need(all(isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and
                 isinstance(v, str) and not any(c in v for c in "\r\n\x00") for k, v in environment.items()), "ENV_FILE_INVALID")
        env_raw = ("\n".join(k + "=" + v for k, v in sorted(environment.items())) + "\n").encode()
        write_new(SCOPE / "app.env", env_raw)
        # All controls/settings except the three explicitly permitted env keys
        # come verbatim from the pinned old container, including credentials.
        need({k: environment.get(k) for k in self.m.SETTINGS} == self.m.SETTINGS, "INHERITED_MIGRATION_MODE_INVALID")
        self._command(self._create_argv())

    def start_app(self, container_id):
        state, o = self._intent("start")
        need(container_id == o["new"]["container_id"] and o["new"]["state"] == "created", "NEW_NOT_CREATED")
        self._capacity()
        self._command(["/usr/bin/docker", "start", container_id])
        self.starting_new = True
        try:
            # Only inspect here; public readiness can take multiple seconds.
            process = self._process(self._docker("container", NEW_NAME))
            need(process["pid"] > 1, "NEW_PROCESS_START_UNKNOWN")
            write_new(SCOPE / "new-process.json", canonical(process) + b"\n")
        finally:
            self.starting_new = False

    def install_redirect(self, rule, nat_before):
        state, o = self._intent("route")
        need(rule == self.m.REDIRECT and nat_before == state["nat_before"] == o["nat"] and
             rule not in nat_before["output"], "ROUTE_BASELINE_CHANGED")
        proof = self._ingress_owner_check(o["old"]["process"])
        write_new(SCOPE / "route-prerequisites.json", canonical(proof) + b"\n")
        self._command(["/usr/sbin/iptables", "-w", "2", "-t", "nat", "-I", "OUTPUT", "1"] + rule[2:])
        expected = copy.deepcopy(nat_before)
        expected["output"] = [rule] + expected["output"]
        need(self._nat() == expected, "ROUTE_INSERTION_OUTCOME_UNKNOWN")

    def _ingress_owner_check(self, old_process):
        result = self._command(["/usr/bin/systemctl", "show", "sub2api-tencent-entry.service", "--property=MainPID", "--value"])
        need(re.fullmatch(rb"[1-9][0-9]*\n?", result.stdout), "ENTRY_PROCESS_UNKNOWN")
        entry_pid = int(result.stdout)
        status = (self.proc / str(entry_pid) / "status").read_text()
        uid_line = [line for line in status.splitlines() if line.startswith("Uid:")]
        need(len(uid_line) == 1 and [int(x) for x in uid_line[0].split()[1:]] == [1000] * 4,
             "ENTRY_UID_NOT_1000")
        pid = old_process["pid"]
        need(process_start_ticks((self.proc / str(pid) / "stat").read_text()) == old_process["start_ticks"], "OLD_PROCESS_CHANGED")
        inodes = set()
        for fd in (self.proc / str(pid) / "fd").iterdir():
            try:
                match = re.fullmatch(r"socket:\[([0-9]+)\]", os.readlink(fd))
                if match:
                    inodes.add(match[1])
            except FileNotFoundError:
                pass
        listeners, clients = [], []
        for name in ("tcp", "tcp6"):
            for line in (self.proc / "net" / name).read_text().splitlines()[1:]:
                parts = line.split()
                need(len(parts) >= 10, "TCP_TABLE_INVALID")
                local, remote = parts[1].rsplit(":", 1), parts[2].rsplit(":", 1)
                if int(local[1], 16) == 8082 and parts[3] == "0A" and parts[9] in inodes:
                    need(name == "tcp" and local[0] == "0100007F", "OLD_LISTENER_NOT_LOOPBACK")
                    listeners.append(parts[9])
                if int(remote[1], 16) == 8082 and remote[0] == "0100007F" and parts[3] not in ("0A", "06", "07"):
                    need(int(parts[7]) == 1000, "EXISTING_FORWARD_CLIENT_UID_NOT_1000")
                    clients.append({"inode": parts[9], "uid": int(parts[7]), "state": parts[3]})
        need(len(listeners) == 1 and process_start_ticks((self.proc / str(pid) / "stat").read_text()) == old_process["start_ticks"],
             "PINNED_OLD_LISTENER_REQUIRED")
        return {"old_process": old_process, "old_listener_inodes": listeners, "entry_pid": entry_pid,
                "entry_uid": 1000, "existing_forward_clients": clients}

    def signal_old_once(self, container_id):
        state, o = self._intent("term")
        need(container_id == OLD_ID and o["old"].get("work") and not any(o["old"]["work"].values()), "OLD_NOT_DRAINED")
        expected = copy.deepcopy(state["nat_before"])
        expected["output"] = [self.m.REDIRECT] + expected["output"]
        need(o["nat"] == expected and self.inspect_history() == state["history"], "TERM_PREREQUISITES_CHANGED")
        self._command(["/usr/bin/docker", "kill", "--signal", "TERM", OLD_ID])


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_host():
    need(os.geteuid() == 0 and pathlib.Path(__file__).absolute() == LIB / "rolling_app_host.py", "INSTALLED_ROOT_PROGRAM_REQUIRED")
    config = json.loads(private_read(SCOPE / "host-config.json"))
    fields = {"v", "owner", "host_sha256", "core_sha256", "rpc_sha256", "source_recovery_sha256", "source_controller_sha256"}
    need(type(config) is dict and set(config) == fields and config["v"] == 1 and
         re.fullmatch(r"[0-9a-f]{32}", config["owner"]) and
         all(re.fullmatch(r"[0-9a-f]{64}", config[key]) for key in fields - {"v", "owner"}), "HOST_CONFIG_INVALID")
    for filename, key in [("rolling_app_host.py", "host_sha256"), ("rolling_app_recovery.py", "core_sha256"),
                          ("target_rpc.py", "rpc_sha256")]:
        need(raw_sha(private_read(LIB / filename, mode=0o700)) == config[key], "PROGRAM_PIN_CHANGED")
    core = load_module("rolling_recovery_core", LIB / "rolling_app_recovery.py")
    rpc = load_module("rolling_recovery_rpc", LIB / "target_rpc.py")
    backend = HostBackend(core, rpc, config)
    return core, backend, config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("step", "inspect"))
    args = parser.parse_args(argv)
    if args.action is None:
        print(json.dumps({"offline": True, "apply": False, "fixed_old_id": OLD_ID, "fixed_new_name": NEW_NAME}, sort_keys=True))
        return 0
    try:
        core, backend, config = load_host()
        result = backend.observe() if args.action == "inspect" else core.Recovery(
            backend, core.FileStore(SCOPE / "state.json", uid=0), config["owner"]).advance()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # Never print exceptions, Docker inspect, inherited env, stderr or GET
        # bodies. Root can inspect the private state and pinned evidence files.
        code = str(exc) if isinstance(exc, Refusal) else "ROLLING_HOST_CHECK_OR_EFFECT_UNCERTAIN"
        print(json.dumps({"ok": False, "code": code, "inspect_only_if_intent_exists": True}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
