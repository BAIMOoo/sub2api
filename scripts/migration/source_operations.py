#!/usr/bin/env python3
"""AWS fixed-action migration backend. Import only; no remote or CLI apply path.

The caller owns route fencing, target RPC, deadlines and orchestration. Every
irreversible local action records its intent before issuing exactly one signal
or FAILOVER. An uncertain response is reconciled, never retried as a command.
"""
import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import stat
import subprocess
import time

RUN = "20261016"
SCOPE = pathlib.Path("/var/lib/sub2api-migration-20261016/runner")
NAMES = {"app": "sub2api", "postgres": "sub2api-postgres", "redis": "sub2api-redis"}
PG_UNITS = ("sub2api-pg-replica-20261016-source-monitor.timer",
            "sub2api-pg-replica-20261016-source-monitor.service",
            "sub2api-pg-replica-20261016-source-monitor-fence.service")
GUARD_UNIT = "sub2api-migration-recovery-20261016.service"
FILE_NAMES = {"manifest": "source-manifest.json", "ledger": "source-operations.json",
              "attestations": "source-attestations.json", "guard": "guard-status.json"}
HEX32, HEX40, HEX64 = r"[0-9a-f]{32}", r"[0-9a-f]{40}", r"[0-9a-f]{64}"
DIGEST = r"sha256:[0-9a-f]{64}"
LSN = r"[0-9A-F]{1,8}/[0-9A-F]{1,8}"
SELF_CLIENT = "sub2api-migration-source-operations-20261016"
MAX_BYTES = 2 * 1024 * 1024


class Refusal(RuntimeError):
    """Fixed, credential-free reason to stop before an action."""


class Unknown(Refusal):
    """An external command may have executed; inspect instead of retrying it."""


def require(condition, code):
    if not condition:
        raise Refusal(code)


def match(value, expression):
    return isinstance(value, str) and re.fullmatch(expression, value) is not None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def strict_json(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BYTES, "INVALID_JSON_SIZE")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(Refusal("NONFINITE_JSON")))
        canonical(result)
        return result
    except Refusal:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise Refusal("INVALID_JSON") from None


def lsn_number(value):
    require(match(value, LSN), "INVALID_LSN")
    high, low = value.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def trusted_parent(path, private=False):
    path = pathlib.Path(path)
    for part in reversed([path, *path.parents]):
        info = part.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                "UNTRUSTED_DIRECTORY")
    if private:
        require(stat.S_IMODE(path.stat().st_mode) == 0o700, "DIRECTORY_NOT_PRIVATE")


def private_bytes(path, mode=0o600):
    path = pathlib.Path(path)
    trusted_parent(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
                stat.S_IMODE(info.st_mode) == mode and info.st_size <= MAX_BYTES, "UNTRUSTED_FILE")
        return os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_private(path, data):
    path = pathlib.Path(path)
    trusted_parent(path.parent, private=True)
    if path.exists() or path.is_symlink():
        private_bytes(path)
    temporary = path.parent / (".source-ops-" + secrets.token_hex(16))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def pg_sampling_lock():
    """Exclude only our local guard SQL while collecting unknown-session proof.

    Do not whitelist psql/application_name: an unrelated session must still
    prevent source fencing. Hold this short lock until docker exec exits, so
    the guard's inspection connection has actually left pg_stat_activity.
    """
    trusted_parent(SCOPE, private=True)
    fd = os.open(SCOPE / "pg-sampling.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
                stat.S_IMODE(info.st_mode) == 0o600, "PG_SAMPLING_LOCK_UNTRUSTED")
        deadline = time.monotonic() + 0.75
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, "PG_SAMPLING_BUSY")
                time.sleep(0.005)
        yield
    finally:
        os.close(fd)


def validate_manifest(value):
    require(type(value) is dict and set(value) == {"v", "run", "owner", "machine_id_sha256", "containers", "postgres", "redis", "units"}, "MANIFEST_FIELDS")
    require(type(value["v"]) is int and value["v"] == 1 and value["run"] == RUN and
            match(value["owner"], HEX32) and match(value["machine_id_sha256"], HEX64), "MANIFEST_IDENTITY")
    require(type(value["containers"]) is dict and set(value["containers"]) == set(NAMES), "MANIFEST_CONTAINERS")
    for expected in value["containers"].values():
        require(type(expected) is dict and set(expected) == {"id", "config_sha256", "image"} and
                match(expected["id"], HEX64) and match(expected["config_sha256"], HEX64), "MANIFEST_CONTAINER_ID")
        image = expected["image"]
        require(type(image) is dict and set(image) == {"config_digest", "diff_ids", "os", "architecture"} and
                match(image["config_digest"], DIGEST) and type(image["diff_ids"]) is list and
                0 < len(image["diff_ids"]) < 256 and all(match(item, DIGEST) for item in image["diff_ids"]) and
                image["os"] == "linux" and image["architecture"] in ("amd64", "arm64"), "MANIFEST_IMAGE")
    pg = value["postgres"]
    require(type(pg) is dict and set(pg) == {"system_identifier", "timeline", "replication_user", "replication_application", "replication_address", "slot"} and
            match(pg["system_identifier"], r"[0-9]{1,20}") and type(pg["timeline"]) is int and pg["timeline"] >= 1 and
            pg["replication_user"] == "sub2api_mig_20261016_replica" and
            pg["replication_application"] == "sub2api-pg-replica-20261016" and
            pg["replication_address"] == "172.18.0.1" and pg["slot"] == "sub2api_mig_20261016", "MANIFEST_POSTGRES")
    redis = value["redis"]
    require(type(redis) is dict and set(redis) == {"source_run_id", "source_replid", "target_run_id"} and
            all(match(item, HEX40) for item in redis.values()), "MANIFEST_REDIS")
    require(type(value["units"]) is dict and set(value["units"]) == {*PG_UNITS, GUARD_UNIT} and
            all(match(item, HEX64) for item in value["units"].values()), "MANIFEST_UNITS")
    return copy.deepcopy(value)


# The query is fixed; no SQL, database name, role or connection string is input.
# Backend types absent from the known internal set are retained as unknown.
PG_QUERY = b"""SET statement_timeout='1s';
SELECT json_build_object(
 'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
 'timeline',(SELECT timeline_id FROM pg_control_checkpoint()),
 'in_recovery',pg_is_in_recovery(),
 'flushed_lsn',pg_current_wal_flush_lsn()::text,
 'prepared_transactions',(SELECT count(*) FROM pg_prepared_xacts),
 'sessions',COALESCE((SELECT json_agg(json_build_object('pid',pid,'user',usename,
 'application',application_name,'address',host(client_addr),'backend_type',backend_type,
 'state',state,'has_transaction',xact_start IS NOT NULL)) FROM pg_stat_activity
 WHERE pid<>pg_backend_pid() AND backend_type NOT IN
 ('autovacuum launcher','autovacuum worker','background writer','checkpointer',
  'archiver','startup','walreceiver','walwriter','logical replication launcher','io worker')),'[]'::json),
 'replication',COALESCE((SELECT json_agg(json_build_object('pid',pid,'user',usename,
 'application',application_name,'address',host(client_addr),'state',state,
 'sent_lsn',sent_lsn::text,'flush_lsn',flush_lsn::text,'replay_lsn',replay_lsn::text))
 FROM pg_stat_replication),'[]'::json),
 'slots',COALESCE((SELECT json_agg(json_build_object('name',slot_name,'active',active,
 'wal_status',wal_status,'restart_lsn',restart_lsn::text)) FROM pg_replication_slots),'[]'::json),
 'wal_cap_mb',current_setting('max_slot_wal_keep_size'));
"""


def parse_info(raw):
    result = {}
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if line and not line.startswith("#") and ":" in line:
            key, value = line.split(":", 1)
            require(key not in result, "DUPLICATE_REDIS_INFO")
            result[key] = value
    return result


def parse_pair_info(raw):
    """Keep the same Server/Replication evidence from one fixed INFO read."""
    selected, seen, lines = False, set(), []
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if line.startswith("# "):
            section = line[2:]
            selected = section in ("Server", "Replication")
            if selected:
                require(section not in seen, "DUPLICATE_REDIS_INFO")
                seen.add(section)
        elif selected:
            lines.append(line)
    require(seen == {"Server", "Replication"}, "REDIS_INFO_SECTIONS_MISSING")
    return parse_info("\n".join(lines).encode())


def parse_clients(raw):
    lines = raw.decode("utf-8", errors="strict").splitlines()
    require(lines and lines[0] == "OK", "REDIS_CLIENT_PROBE_FAILED")
    clients = []
    for line in lines[1:]:
        if not line:
            continue
        record = {}
        for item in line.split():
            require("=" in item, "INVALID_REDIS_CLIENT")
            key, value = item.split("=", 1)
            require(key not in record, "INVALID_REDIS_CLIENT")
            record[key] = value
        require({"id", "addr", "flags", "name"} <= set(record) and record["id"].isdigit(), "INVALID_REDIS_CLIENT")
        # No command args or query buffers are ever exposed to callers.
        clients.append({key: record[key] for key in ("id", "addr", "flags", "name")})
    require(sum(item["name"] == SELF_CLIENT for item in clients) == 1, "REDIS_SELF_PROBE_AMBIGUOUS")
    return clients


def parse_sockets(raw):
    states = {}
    outbound = {"postgres": 0, "redis": 0, "other": 0}
    for line in raw.decode("utf-8", errors="strict").splitlines():
        fields = line.split()
        require(len(fields) >= 5, "INVALID_SOCKET_SAMPLE")
        state, local, peer = fields[0], fields[3], fields[4]
        if state in ("LISTEN", "TIME-WAIT", "CLOSED"):
            continue
        def port(address):
            token = address.rsplit(":", 1)[-1]
            return int(token) if token.isdigit() else None
        if port(local) == 8080:
            states[state] = states.get(state, 0) + 1
        else:
            key = {5432: "postgres", 6379: "redis"}.get(port(peer), "other")
            outbound[key] += 1
    return {"inbound_connections": sum(states.values()), "inbound_states": states, "outbound_connections": outbound}


class LocalBackend:
    """Only operating-system adapter. Every path and executable is fixed here."""
    def read(self, name):
        require(name in FILE_NAMES, "UNKNOWN_FILE")
        try:
            return strict_json(private_bytes(SCOPE / FILE_NAMES[name]))
        except FileNotFoundError:
            if name == "ledger":
                return None
            raise Refusal("REQUIRED_FILE_MISSING") from None

    def write(self, value):
        atomic_private(SCOPE / FILE_NAMES["ledger"], canonical(value) + b"\n")

    def pg_sampling_lock(self):
        return pg_sampling_lock()

    @contextlib.contextmanager
    def lock(self):
        trusted_parent(SCOPE, private=True)
        fd = os.open(SCOPE / "source-operations.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
                    stat.S_IMODE(info.st_mode) == 0o600, "UNTRUSTED_LOCK")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refusal("SOURCE_OPERATION_BUSY") from None
            yield
        finally:
            os.close(fd)

    def now(self):
        return time.time()

    def machine_digest(self):
        return digest(pathlib.Path("/etc/machine-id").read_bytes().strip())

    def boot_id(self):
        return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def command(self, argv, data=None, timeout=2):
        try:
            result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise Unknown("COMMAND_OUTCOME_UNKNOWN") from None
        if result.returncode:
            raise Unknown("COMMAND_OUTCOME_UNKNOWN")
        require(len(result.stdout) <= MAX_BYTES, "COMMAND_OUTPUT_LIMIT")
        return result.stdout

    def unit_status(self, unit):
        require(unit in {*PG_UNITS, GUARD_UNIT}, "UNKNOWN_UNIT")
        raw = self.command(["/usr/bin/systemctl", "show", unit,
                            "--property=ActiveState,SubState,MainPID,Job,UnitFileState,LoadState,DropInPaths,FragmentPath"])
        pairs = [line.split("=", 1) for line in raw.decode().splitlines() if "=" in line]
        result = dict(pairs)
        require(len(result) == len(pairs), "INVALID_UNIT_STATE")
        # systemd timer units have no MainPID property; services must report it.
        if unit == PG_UNITS[0]:
            result.setdefault("MainPID", "0")
        require({"ActiveState", "SubState", "MainPID", "Job", "UnitFileState", "LoadState", "DropInPaths", "FragmentPath"} <= set(result), "INVALID_UNIT_STATE")
        require(not result["DropInPaths"] and result["FragmentPath"] in ("/etc/systemd/system/" + unit, "/dev/null"), "UNIT_OVERRIDE_OR_FRAGMENT_CHANGED")
        return result

    def unit_identity(self, unit, expected_digest, allow_masked=False):
        require(unit in {*PG_UNITS, GUARD_UNIT}, "UNKNOWN_UNIT")
        path = pathlib.Path("/etc/systemd/system") / unit
        trusted_parent(path.parent)
        if path.is_symlink():
            require(allow_masked and os.readlink(path) == "/dev/null" and path.lstat().st_uid == 0, "UNIT_IDENTITY_CHANGED")
            return "masked"
        require(digest(private_bytes(path, mode=0o644)) == expected_digest, "UNIT_IDENTITY_CHANGED")
        return "original"

    def mask_unit(self, unit, expected_digest):
        identity = self.unit_identity(unit, expected_digest, allow_masked=True)
        backup = SCOPE / ("retired-" + unit)
        if identity == "masked":
            require(digest(private_bytes(backup)) == expected_digest, "MASK_BACKUP_MISSING")
            return
        path = pathlib.Path("/etc/systemd/system") / unit
        original = private_bytes(path, mode=0o644)
        require(digest(original) == expected_digest, "UNIT_IDENTITY_CHANGED")
        atomic_private(backup, original)
        temporary = path.parent / (".sub2api-retire-" + secrets.token_hex(16))
        try:
            os.symlink("/dev/null", temporary)
            # Revalidate immediately before replacement; no --force deletion.
            require(digest(private_bytes(path, mode=0o644)) == expected_digest, "UNIT_IDENTITY_CHANGED")
            os.replace(temporary, path)
            fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


class SourceOperations:
    def __init__(self, backend=None):
        self.b = backend if backend is not None else LocalBackend()
        self.m = validate_manifest(self.b.read("manifest"))
        self._identity()

    def _identity(self):
        require(self.b.machine_digest() == self.m["machine_id_sha256"], "MACHINE_CHANGED")

    def _ledger(self):
        value = self.b.read("ledger")
        if value is None:
            return {"v": 1, "run": RUN, "owner": self.m["owner"], "operations": {}}
        require(type(value) is dict and value.get("v") == 1 and value.get("run") == RUN and
                value.get("owner") == self.m["owner"] and type(value.get("operations")) is dict, "LEDGER_IDENTITY")
        return value

    def _operation(self, state, op_id, action, arguments=None):
        require(match(op_id, HEX32), "INVALID_OPERATION_ID")
        request = {"action": action, "arguments": arguments or {}}
        operations = state["operations"]
        if op_id in operations:
            operation = operations[op_id]
            require(operation["request"] == request, "OPERATION_ID_REUSED")
            return operation
        # Prevent a caller from bypassing exactly-once signals using a new ID.
        for previous in operations.values():
            require(previous["request"]["action"] != action, "ACTION_ALREADY_HAS_OPERATION")
        operation = {"request": request, "phase": "intent", "steps": {}, "created_at": self.b.now()}
        operations[op_id] = operation
        self.b.write(state)
        return operation

    def _save(self, state, operation, phase, **fields):
        operation.update(phase=phase, checked_at=self.b.now(), **fields)
        self.b.write(state)
        return copy.deepcopy(operation)

    def _step(self, state, operation, name, function):
        steps = operation["steps"]
        if name not in steps:
            steps[name] = "intent"
            self.b.write(state)
            try:
                function()
            except (Unknown, Refusal):
                # Intent remains: a signal is never sent again, even after a
                # timeout, failed client, process death, or response loss.
                raise
            steps[name] = "accepted"
            self.b.write(state)
        return steps[name]

    def _docker(self, kind, identifier):
        result = strict_json(self.b.command(["/usr/bin/docker", kind, "inspect", identifier]))
        require(type(result) is list and len(result) == 1 and type(result[0]) is dict, "INVALID_DOCKER_INSPECTION")
        return result[0]

    def _resource(self, kind):
        self._identity()
        require(kind in NAMES, "UNKNOWN_RESOURCE")
        expected = self.m["containers"][kind]
        info = self._docker("container", NAMES[kind])
        require(info.get("Id") == expected["id"] and digest(info.get("Config")) == expected["config_sha256"], "CONTAINER_CHANGED")
        image = self._docker("image", info["Image"])
        wanted = expected["image"]
        require(image.get("RootFS", {}).get("Layers") == wanted["diff_ids"] and
                image.get("Os", "linux") == wanted["os"] and image.get("Architecture") == wanted["architecture"], "IMAGE_CHANGED")
        if image["Id"] != wanted["config_digest"]:
            def content(identifier):
                require(match(identifier, DIGEST), "INVALID_IMAGE_DIGEST")
                raw = self.b.command(["/usr/bin/ctr", "-n", "moby", "content", "get", identifier])
                require("sha256:" + digest(raw) == identifier, "IMAGE_CONTENT_DIGEST_CHANGED")
                return strict_json(raw)
            manifest = content(image["Id"])
            if "manifests" in manifest:
                candidates = [item for item in manifest["manifests"] if item.get("platform", {}).get("os") == wanted["os"] and
                              item.get("platform", {}).get("architecture") == wanted["architecture"]]
                require(len(candidates) == 1, "AMBIGUOUS_IMAGE_PLATFORM")
                manifest = content(candidates[0]["digest"])
            require(manifest.get("config", {}).get("digest") == wanted["config_digest"], "IMAGE_CONFIG_CHANGED")
            config = content(wanted["config_digest"])
            require(config.get("rootfs", {}).get("diff_ids") == wanted["diff_ids"], "IMAGE_CONFIG_ROOTFS_CHANGED")
        require(type(info.get("State", {}).get("Running")) is bool and
                info["State"].get("Status") in ("running", "exited", "created"), "RESOURCE_STATE_UNKNOWN")
        return info

    def _running(self, kind):
        info = self._resource(kind)
        require(info["State"]["Running"] is True and info["State"]["Status"] == "running" and not info["State"].get("Paused") and not info["State"].get("Restarting"), "RESOURCE_NOT_RUNNING")
        return info

    @staticmethod
    def _no_restart(info):
        require(info["HostConfig"]["RestartPolicy"]["Name"] == "no", "RESOURCE_CAN_RESTART")

    @staticmethod
    def _stopped(info):
        require(not info["State"]["Running"] and info["State"]["Status"] == "exited" and
                not info["State"].get("OOMKilled") and info["State"].get("ExitCode") == 0, "RESOURCE_NOT_CLEANLY_STOPPED")
        SourceOperations._no_restart(info)

    def _fresh(self, name):
        sample = self.b.read(name)
        require(type(sample) is dict and sample.get("v") == 1 and sample.get("run") == RUN and
                sample.get("owner") == self.m["owner"] and sample.get("boot_id") == self.b.boot_id(), "ATTESTATION_IDENTITY")
        at = sample.get("checked_at")
        require(type(at) in (int, float) and math.isfinite(at) and 0 <= self.b.now() - at <= 3, "ATTESTATION_STALE")
        return sample

    def _guard(self):
        self.b.unit_identity(GUARD_UNIT, self.m["units"][GUARD_UNIT])
        status = self.b.unit_status(GUARD_UNIT)
        require(status["ActiveState"] == "active" and status["SubState"] == "running" and
                status["MainPID"].isdigit() and int(status["MainPID"]) > 0 and not status["Job"], "RECOVERY_GUARD_NOT_RUNNING")
        sample = self._fresh("guard")
        require(sample.get("healthy") is True and sample.get("pg_wal_protected") is True, "RECOVERY_GUARD_NOT_PROTECTING")
        return sample

    def _app_sockets(self, info):
        if not info["State"]["Running"]:
            return {"inbound_connections": 0, "inbound_states": {}, "outbound_connections": {}}
        pid = info["State"].get("Pid")
        require(type(pid) is int and pid > 1, "INVALID_APP_PID")
        raw = self.b.command(["/usr/bin/nsenter", "--target", str(pid), "--net", "--", "/usr/bin/ss", "-Htanp"])
        # The PID may have exited/reused while sampling. Revalidate container.
        after = self._resource("app")
        require(after["State"]["Running"] and after["State"].get("Pid") == pid, "APP_CHANGED_DURING_SAMPLE")
        return parse_sockets(raw)

    def _pg_sample(self):
        self._running("postgres")
        with self.b.pg_sampling_lock():
            raw = self.b.command(["/usr/bin/docker", "exec", "-i", self.m["containers"]["postgres"]["id"], "psql", "-X", "-q", "-A", "-t", "-w", "-v", "ON_ERROR_STOP=1", "-U", "sub2api", "-d", "sub2api"], PG_QUERY)
        sample = strict_json(raw.strip())
        expected = self.m["postgres"]
        require(sample.get("system_identifier") == expected["system_identifier"] and sample.get("timeline") == expected["timeline"] and
                sample.get("in_recovery") is False, "PG_IDENTITY_OR_ROLE_CHANGED")
        lsn_number(sample.get("flushed_lsn"))
        require(type(sample.get("prepared_transactions")) is int and sample["prepared_transactions"] >= 0 and
                type(sample.get("sessions")) is list and type(sample.get("replication")) is list and type(sample.get("slots")) is list, "INVALID_PG_SAMPLE")
        known = lambda item: item.get("user") == expected["replication_user"] and item.get("application") == expected["replication_application"] and item.get("address") == expected["replication_address"]
        sample["unknown_sessions"] = [item for item in sample["sessions"] if not (item.get("backend_type") == "walsender" and known(item))]
        sample["owned_replication"] = [item for item in sample["replication"] if known(item)]
        sample["checked_at"] = self.b.now()
        sample["boot_id"] = self.b.boot_id()
        return sample

    def pg_final_evidence(self):
        self._identity()
        sample = self._pg_sample()
        sample["quiet"] = sample["prepared_transactions"] == 0 and not sample["unknown_sessions"]
        return sample

    def _redis(self, *command, target=False, data=None, timeout=2):
        self._running("redis")
        argv = ["/usr/bin/docker", "exec", "-i", "-e", "REDISCLI_AUTH=", self.m["containers"]["redis"]["id"], "redis-cli", "--raw"]
        if target:
            argv += ["-h", "172.18.0.1", "-p", "26379"]
        # All command tokens originate from this module, except bounded timeout.
        return self.b.command(argv + list(command), data, timeout=timeout)

    def _redis_pair(self):
        before = self._running("redis")
        pid, started = before["State"].get("Pid"), before["State"].get("StartedAt")
        require(type(pid) is int and pid > 1 and isinstance(started, str) and started, "INVALID_REDIS_PROCESS")
        # This closed read batch pins one source process. Other Redis paths,
        # including clients and mutations, keep their per-command validation.
        source_argv = ["/usr/bin/docker", "exec", "-i", "-e", "REDISCLI_AUTH=", self.m["containers"]["redis"]["id"], "redis-cli", "--raw"]
        target_argv = source_argv + ["-h", "172.18.0.1", "-p", "26379"]
        source = parse_pair_info(self.b.command(source_argv + ["INFO"]))
        source_role = self.b.command(source_argv + ["ROLE"]).decode().splitlines()[0]
        target = parse_pair_info(self.b.command(target_argv + ["INFO"]))
        target_role = self.b.command(target_argv + ["ROLE"]).decode().splitlines()[0]
        after = self._running("redis")
        require(after["Id"] == before["Id"] and after["State"].get("Pid") == pid and
                after["State"].get("StartedAt") == started, "REDIS_PROCESS_CHANGED_DURING_SAMPLE")
        require(source_role == source.get("role") and target_role == target.get("role") and
                source_role in ("master", "slave") and target_role in ("master", "slave"), "REDIS_ROLE_UNKNOWN")
        require(source.get("run_id") == self.m["redis"]["source_run_id"] and target.get("run_id") == self.m["redis"]["target_run_id"], "REDIS_PROCESS_IDENTITY_CHANGED")
        require(source.get("master_failover_state") in ("no-failover", "waiting-for-sync", "failover-in-progress"), "REDIS_FAILOVER_STATE_UNKNOWN")
        return {"source": source, "target": target}

    def _redis_clients(self, target=False):
        raw = self._redis(target=target, data=("CLIENT SETNAME " + SELF_CLIENT + "\nCLIENT LIST\n").encode())
        clients = parse_clients(raw)
        unknown = []
        replicas = []
        for item in clients:
            if item["name"] == SELF_CLIENT:
                continue
            peer = item["addr"].rsplit(":", 1)[0]
            if "S" in item["flags"] and peer == ("127.0.0.1" if target else "172.18.0.1"):
                replicas.append(item)
            elif "M" in item["flags"] and item["addr"] == ("127.0.0.1:16379" if target else "172.18.0.1:26379"):
                continue
            else:
                unknown.append(item)
        return {"clients": clients, "unknown_clients": unknown, "replica_clients": replicas}

    def redis_roles(self):
        """Read-only dual-end ROLE/INFO while the source Redis is running."""
        return self._redis_pair()

    def resource_fences(self):
        """Read-only resource identities/states for recovery, never authority."""
        self._identity()
        resources = {kind: self._resource(kind) for kind in NAMES}
        return {"v": 1, "run": RUN, "owner": self.m["owner"], "checked_at": self.b.now(),
                "resources": {kind: {"id": item["Id"], "running": item["State"]["Running"],
                                     "status": item["State"]["Status"], "restart": item["HostConfig"]["RestartPolicy"]["Name"],
                                     "exit_code": item["State"].get("ExitCode"), "oom_killed": item["State"].get("OOMKilled")}
                              for kind, item in resources.items()}}

    def inspect(self):
        self._identity()
        resources = {kind: self._resource(kind) for kind in NAMES}
        result = {"v": 1, "run": RUN, "owner": self.m["owner"], "checked_at": self.b.now(),
                  "resources": {kind: {"id": item["Id"], "running": item["State"]["Running"],
                                        "status": item["State"]["Status"], "restart": item["HostConfig"]["RestartPolicy"]["Name"],
                                        "exit_code": item["State"].get("ExitCode"), "oom_killed": item["State"].get("OOMKilled")}
                                for kind, item in resources.items()},
                  "app_connections": self._app_sockets(resources["app"]),
                  "units": {unit: self.b.unit_status(unit) for unit in (*PG_UNITS, GUARD_UNIT)},
                  "operations": self._ledger()["operations"]}
        if resources["postgres"]["State"]["Running"]:
            result["postgres"] = self.pg_final_evidence()
        if resources["redis"]["State"]["Running"]:
            result["redis"] = self._redis_pair()
            result["redis"].update(self._redis_clients())
        return result

    def disable_app_restart(self, op_id):
        with self.b.lock():
            state = self._ledger()
            operation = self._operation(state, op_id, "disable_app_restart")
            info = self._resource("app")
            if info["HostConfig"]["RestartPolicy"]["Name"] != "no":
                self._step(state, operation, "restart_no", lambda: self.b.command(["/usr/bin/docker", "update", "--restart=no", self.m["containers"]["app"]["id"]]))
            self._no_restart(self._resource("app"))
            return self._save(state, operation, "complete")

    def app_stop(self, op_id):
        with self.b.lock():
            state = self._ledger()
            operation = self._operation(state, op_id, "app_stop")
            info = self._resource("app")
            self._no_restart(info)
            if not info["State"]["Running"]:
                require("sigterm" in operation["steps"], "APP_EXITED_WITHOUT_OWNED_STOP")
                self._stopped(info)
                return self._save(state, operation, "complete", stopped=True)
            if "sigterm" in operation["steps"]:
                return self._save(state, operation, "pending", stopped=False)
            attestation = self._fresh("attestations")
            require(attestation.get("app_route_fenced") is True, "SOURCE_APP_ROUTE_NOT_FENCED")
            sockets = self._app_sockets(info)
            if sockets["inbound_connections"]:
                return self._save(state, operation, "pending", connections=sockets)
            self._step(state, operation, "sigterm", lambda: self.b.command(["/usr/bin/docker", "kill", "--signal=SIGTERM", self.m["containers"]["app"]["id"]]))
            after = self._resource("app")
            if after["State"]["Running"]:
                return self._save(state, operation, "pending", stopped=False)
            self._stopped(after)
            return self._save(state, operation, "complete", stopped=True)

    def retire_pg_monitors(self, op_id):
        with self.b.lock():
            state = self._ledger()
            operation = self._operation(state, op_id, "retire_pg_monitors")
            self._guard()
            self._resource("postgres")
            if operation["phase"] == "complete":
                require(all(operation["steps"].get(name) == "accepted" for name in
                            ("disable_timer", "stop_timer", *("mask:" + unit for unit in PG_UNITS))),
                        "MONITOR_RETIREMENT_EVIDENCE_INVALID")
                self._assert_monitors_retired()
                # A completed retirement is a fresh read-only verification.
                # Reissuing daemon-reload can transiently invalidate the guard
                # supervisor even though every original unit is already masked.
                return {**copy.deepcopy(operation), "guard_checked_at": self._guard()["checked_at"]}
            for unit in PG_UNITS:
                self.b.unit_identity(unit, self.m["units"][unit], allow_masked=("mask:" + unit) in operation["steps"])
            # Disable scheduling first. Never stop an in-flight oneshot/fence.
            if "disable_timer" not in operation["steps"] or operation["steps"]["disable_timer"] != "accepted":
                self.b.command(["/usr/bin/systemctl", "disable", PG_UNITS[0]])
                operation["steps"]["disable_timer"] = "accepted"
                self.b.write(state)
            if "stop_timer" not in operation["steps"] or operation["steps"]["stop_timer"] != "accepted":
                self.b.command(["/usr/bin/systemctl", "stop", PG_UNITS[0]])
                operation["steps"]["stop_timer"] = "accepted"
                self.b.write(state)
            statuses = {unit: self.b.unit_status(unit) for unit in PG_UNITS}
            if any(status["MainPID"] != "0" or status["Job"] or status["ActiveState"] not in ("inactive", "failed") for status in statuses.values()):
                return self._save(state, operation, "pending", units=statuses)
            require(all(status["ActiveState"] == "inactive" for status in statuses.values()), "SOURCE_MONITOR_OR_FENCE_FAILED")
            self._guard()
            for unit in PG_UNITS:
                name = "mask:" + unit
                if name not in operation["steps"]:
                    operation["steps"][name] = "intent"
                    self.b.write(state)
                self.b.mask_unit(unit, self.m["units"][unit])
                operation["steps"][name] = "accepted"
                self.b.write(state)
            self.b.command(["/usr/bin/systemctl", "daemon-reload"])
            self._assert_monitors_retired()
            return self._save(state, operation, "complete", guard_checked_at=self._guard()["checked_at"])

    def _assert_monitors_retired(self):
        for unit in PG_UNITS:
            require(self.b.unit_identity(unit, self.m["units"][unit], allow_masked=True) == "masked", "SOURCE_MONITOR_NOT_MASKED")
            status = self.b.unit_status(unit)
            require(status["LoadState"] == "masked" and status["UnitFileState"] == "masked" and
                    status["ActiveState"] == "inactive" and status["MainPID"] == "0" and not status["Job"], "SOURCE_MONITOR_NOT_IDLE_MASKED")

    def pg_stop(self, op_id):
        with self.b.lock():
            state = self._ledger()
            operation = self._operation(state, op_id, "pg_stop")
            info = self._resource("postgres")
            if not info["State"]["Running"]:
                require("sigint" in operation["steps"], "PG_EXITED_WITHOUT_OWNED_STOP")
                self._stopped(info)
                return self._save(state, operation, "complete", stopped=True, authority="SOURCE_FENCED")
            if "sigint" in operation["steps"]:
                return self._save(state, operation, "pending", stopped=False, authority="UNKNOWN")
            self._guard()
            self._assert_monitors_retired()
            self._stopped(self._resource("app"))
            attestation = self._fresh("attestations")
            pg = attestation.get("postgres", {})
            require(pg.get("writers_quiet") is True and match(pg.get("arm_id"), HEX32), "PG_WRITERS_NOT_QUIET")
            require(lsn_number(pg.get("target_replay_lsn")) >= lsn_number(pg.get("final_lsn")), "TARGET_BEHIND_FINAL_LSN")
            sample = self.pg_final_evidence()
            require(sample["quiet"] and lsn_number(sample["flushed_lsn"]) >= lsn_number(pg["final_lsn"]), "PG_NOT_QUIET")
            operation["final_lsn"] = pg["final_lsn"]
            operation["quiet_evidence_sha256"] = digest(sample)
            operation["arm_id"] = pg["arm_id"]
            self.b.write(state)
            if info["HostConfig"]["RestartPolicy"]["Name"] != "no":
                self._step(state, operation, "restart_no", lambda: self.b.command(["/usr/bin/docker", "update", "--restart=no", self.m["containers"]["postgres"]["id"]]))
            self._no_restart(self._resource("postgres"))
            # SIGINT is PostgreSQL fast shutdown: rollback open xacts, flush,
            # exit. There is no timeout-based SIGKILL or old-primary restart.
            self._step(state, operation, "sigint", lambda: self.b.command(["/usr/bin/docker", "kill", "--signal=SIGINT", self.m["containers"]["postgres"]["id"]]))
            after = self._resource("postgres")
            if after["State"]["Running"]:
                return self._save(state, operation, "pending", stopped=False, authority="UNKNOWN")
            self._stopped(after)
            return self._save(state, operation, "complete", stopped=True, authority="SOURCE_FENCED")

    def redis_failover(self, op_id, timeout_ms):
        require(type(timeout_ms) is int and 100 <= timeout_ms <= 6000, "INVALID_FAILOVER_BUDGET")
        with self.b.lock():
            state = self._ledger()
            operation = self._operation(state, op_id, "redis_failover", {"timeout_ms": timeout_ms})
            info = self._resource("redis")
            if not info["State"]["Running"]:
                # Recovery may re-enter after source retirement but before the
                # engine checkpoint. The old container can no longer execute
                # redis-cli. Only our completed handoff + owned clean SIGTERM
                # can establish this durable source fence. The caller MUST
                # obtain fresh target RPC master/run-ID proof immediately.
                require(operation.get("phase") == "complete" and operation.get("authority") == "TENCENT" and
                        "failover" in operation["steps"], "REDIS_STOPPED_WITHOUT_CONFIRMED_HANDOFF")
                self._stopped(info)
                retired = [item for item in state["operations"].values() if item["request"]["action"] == "redis_role_persist"]
                require(len(retired) == 1 and "sigterm" in retired[0]["steps"] and
                        match(retired[0].get("verified_roles_sha256"), HEX64), "REDIS_RETIREMENT_NOT_OWNED")
                evidence = operation.get("evidence", {})
                require(evidence.get("source", {}).get("role") == "slave" and evidence.get("target", {}).get("role") == "master" and
                        evidence["source"].get("run_id") == self.m["redis"]["source_run_id"] and
                        evidence["target"].get("run_id") == self.m["redis"]["target_run_id"], "REDIS_HANDOFF_EVIDENCE_INVALID")
                # A lost stop response may have left only the verified-role
                # digest + signal intent. Clean exit now completes that exact
                # retirement; this does not infer a current target role.
                self._save(state, retired[0], "complete", source_role="RETIRED_STOPPED", authority="TENCENT")
                return {**copy.deepcopy(operation), "source_retired": True, "target_verification_required": True}
            pair = self._redis_pair()
            source, target = pair["source"], pair["target"]
            if "failover" in operation["steps"]:
                if source["role"] == "slave" and target["role"] == "master" and source.get("master_host") == "172.18.0.1" and source.get("master_port") == "26379" and source.get("master_link_status") == "up":
                    return self._save(state, operation, "complete", authority="TENCENT", evidence=pair)
                return self._save(state, operation, "unknown", authority="UNKNOWN", evidence=pair)
            self._stopped(self._resource("app"))
            self._stopped(self._resource("postgres"))
            self._guard()
            redis = self._fresh("attestations").get("redis", {})
            require(redis.get("writers_quiet") is True and redis.get("consumers_paused") is True, "REDIS_WRITERS_NOT_QUIET")
            require(source["role"] == "master" and target["role"] == "slave" and
                    source["master_failover_state"] == "no-failover" and
                    source.get("master_replid") == self.m["redis"]["source_replid"] and
                    target.get("master_replid") == source["master_replid"] and target.get("master_link_status") == "up" and
                    target.get("master_sync_in_progress") == "0", "REDIS_NOT_READY_FOR_COORDINATED_FAILOVER")
            replicas = [value for key, value in source.items() if re.fullmatch(r"slave[0-9]+", key)]
            require(len(replicas) == 1 and {"ip=172.18.0.1", "port=26379", "state=online"} <= set(replicas[0].split(",")), "REDIS_TARGET_NOT_UNIQUE_ONLINE_REPLICA")
            clients = self._redis_clients()
            target_clients = self._redis_clients(target=True)
            require(not clients["unknown_clients"] and len(clients["replica_clients"]) == 1 and not target_clients["unknown_clients"], "REDIS_UNKNOWN_CLIENTS")
            info = self._resource("redis")
            if info["HostConfig"]["RestartPolicy"]["Name"] != "no":
                self._step(state, operation, "restart_no", lambda: self.b.command(["/usr/bin/docker", "update", "--restart=no", self.m["containers"]["redis"]["id"]]))
            self._no_restart(self._resource("redis"))
            reply = []
            self._step(state, operation, "failover", lambda: reply.append(self._redis("FAILOVER", "TO", "172.18.0.1", "26379", "TIMEOUT", str(timeout_ms), timeout=min(2, timeout_ms / 1000 + 0.25))))
            require(not reply or reply[0].strip() == b"OK", "REDIS_FAILOVER_REPLY_UNKNOWN")
            return self._save(state, operation, "pending", authority="UNKNOWN")

    def redis_role_persist(self, op_id):
        """Retire source Redis after verified coordinated handoff; never restart it.

        The legacy source has no proven persistent role config. A stopped,
        restart=no container plus root tombstone is the durable source fence.
        Target CONFIG REWRITE/restart verification belongs to target RPC.
        """
        with self.b.lock():
            state = self._ledger()
            operation = self._operation(state, op_id, "redis_role_persist")
            previous = [item for item in state["operations"].values() if item["request"]["action"] == "redis_failover"]
            require(len(previous) == 1 and previous[0]["phase"] == "complete" and previous[0].get("authority") == "TENCENT", "REDIS_HANDOFF_NOT_CONFIRMED")
            info = self._resource("redis")
            self._no_restart(info)
            if not info["State"]["Running"]:
                require("sigterm" in operation["steps"], "REDIS_EXITED_WITHOUT_OWNED_STOP")
                self._stopped(info)
                return self._save(state, operation, "complete", source_role="RETIRED_STOPPED", authority="TENCENT")
            if "sigterm" in operation["steps"]:
                return self._save(state, operation, "pending", authority="TENCENT")
            pair = self._redis_pair()
            require(pair["source"]["role"] == "slave" and pair["target"]["role"] == "master" and
                    pair["source"].get("master_host") == "172.18.0.1" and pair["source"].get("master_port") == "26379" and
                    pair["source"].get("master_link_status") == "up", "REDIS_HANDOFF_ROLE_CHANGED")
            require(not self._redis_clients()["unknown_clients"], "REDIS_UNKNOWN_CLIENTS")
            operation["verified_roles_sha256"] = digest(pair)
            self.b.write(state)
            self._step(state, operation, "sigterm", lambda: self.b.command(["/usr/bin/docker", "kill", "--signal=SIGTERM", self.m["containers"]["redis"]["id"]]))
            after = self._resource("redis")
            if after["State"]["Running"]:
                return self._save(state, operation, "pending", authority="TENCENT")
            self._stopped(after)
            return self._save(state, operation, "complete", source_role="RETIRED_STOPPED", authority="TENCENT")


if __name__ == "__main__":
    print(json.dumps({"mode": "import-only", "run": RUN, "commands_executed": False,
                      "manifest": str(SCOPE / FILE_NAMES["manifest"])}))
