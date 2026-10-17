#!/usr/bin/env python3
"""Fixed-action Tencent migration RPC. No arguments prints an offline plan.

Only `serve` (one stdin JSON request, EOF required) and `guard-reconcile`
perform local operations. Installation and resource adoption belong to the
reviewed root installer, never to an RPC request.
"""
import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import http.client
import ipaddress
import json
import marshal
import math
import os
import pathlib
import re
import secrets
import signal
import stat
import subprocess
import sys
import time

RUN = "20261016"
PREFIX = pathlib.Path("/var/lib/sub2api-migration-20261016")
SCOPE = PREFIX / "rpc"
AUTHORITY_SCOPE = PREFIX / "postgres-authority"
PROGRAM = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016/target_rpc.py")
ENDPOINTS = PREFIX / "app" / "endpoints.json"
ADOPTION_SCOPE = PREFIX / "rolling-recovery"
MAX_REQUEST = 16 * 1024
ARM_SECONDS = 6
HEX32, HEX64 = r"[0-9a-f]{32}", r"[0-9a-f]{64}"
PG_CONTAINER = "sub2api-migration-pg-replica-20261016"
PG_VOLUME = "sub2api-migration-pg-replica-data-20261016"
REDIS_CONTAINER = "sub2api-migration-redis-standby-20261016"
REDIS_VOLUME = "sub2api-migration-redis-data-20261016"
APP_CONTAINER = "sub2api-migration-app-20261016"
# Fixed identities reviewed for this migration run; no RPC request can select
# another Redis process, replication peer, address, or command.
REDIS_SOURCE_RUN_ID = "08e111c3724844873b08ee96322628d6a2724ee3"
REDIS_TARGET_RUN_ID = "39d29826637d6bcf89b9630612f7fd0107260394"
REDIS_SOURCE_REPLID = "6fbca65ab347b9efa6637b4c790feaa93c29d348"
REQUIRED_WRITERS = frozenset({
    "timing_wheel", "scheduled_writers", "plugins", "prompt_audit", "prompt_config", "auth_outbox", "ops_runtime",
    "usage_records", "billing_cache", "content_moderation", "detached_writers", "deferred_last_used",
    "audit_log", "ops_ingress_reject", "ops_system_log",
})
PG_DATA = "/var/lib/postgresql/data"
OWNER_LABEL = "io.sub2api.migration.owner"
CONTROLS = {"runtime.status": ("GET", "/status")}
for _component, _actions in {
    "ingress": ("status", "pause", "resume"),
    "writers": ("status", "pause", "resume"),
    "postgres": ("status", "pause", "switch", "resume"),
    "redis": ("status", "pause", "switch", "rebuild", "resume"),
    "background": ("status", "activate"),
}.items():
    for _action in _actions:
        CONTROLS[_component + "." + _action] = (
            "GET" if _action == "status" else "POST", "/data/" + _component + "/" + _action)
ACTION_FIELDS = {
    "status": set(), "app.quiet-snapshot": set(), "app.control": {"control", "control_epoch"},
    "app.observe": {"control"},
    "app.quiesce": set(), "app.restore": set(), "pg.route": set(), "redis.preroute": set(),
    "app.persist-endpoints": {"postgres", "redis"},
    "pg.arm": {"final_lsn", "quiet_attestation_sha256"},
    "pg.arm-quiet": {"final_lsn", "quiet_attestation_sha256", "quiet_nonce"},
    "pg.cancel-arm": {"arm_id"}, "pg.commit": {"arm_id", "source_fence_sha256"},
    "pg.reconcile": set(), "redis.inspect": set(), "redis.persist-primary": set(),
}
BASE_FIELDS = {"v", "run", "owner", "op_id", "expected_epoch", "action"}
READ_ACTIONS = frozenset({"status", "redis.inspect", "app.quiet-snapshot", "app.observe"})


class Refusal(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Uncertain(Refusal):
    """A command may have executed. Its outcome must be inspected, not guessed."""


def require(condition, code):
    if not condition:
        raise Refusal(code)


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "DUPLICATE_KEY")
        result[key] = value
    return result


def strict_json(raw, limit=MAX_REQUEST):
    require(isinstance(raw, bytes) and 0 < len(raw) <= limit, "INVALID_LENGTH")
    try:
        result = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(Refusal("NONFINITE_NUMBER")))
        # json.loads can overflow a finite-looking exponent into infinity.
        canonical(result)
        return result
    except Refusal:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise Refusal("INVALID_JSON") from None


def lsn_number(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}", value), "INVALID_LSN")
    high, low = value.split("/")
    return (int(high, 16) << 32) + int(low, 16)


def integer(value, minimum=0, maximum=(1 << 63) - 1):
    return type(value) is int and minimum <= value <= maximum


def matches(value, expression):
    return isinstance(value, str) and re.fullmatch(expression, value) is not None


def parse_request(raw):
    request = strict_json(raw)
    require(type(request) is dict and isinstance(request.get("action"), str) and request["action"] in ACTION_FIELDS, "UNKNOWN_ACTION")
    fields = BASE_FIELDS | ACTION_FIELDS[request["action"]]
    if request["action"] == "app.control" and str(request.get("control", "")).endswith(".switch"):
        fields = fields | {"endpoint"}
    require(set(request) == fields, "REQUEST_FIELDS")
    require(type(request["v"]) is int and request["v"] == 1 and request["run"] == RUN, "WRONG_RUN")
    require(matches(request["owner"], HEX32) and matches(request["op_id"], HEX32), "INVALID_ID")
    require(integer(request["expected_epoch"]), "INVALID_EPOCH")
    action = request["action"]
    if action == "app.control":
        require(isinstance(request["control"], str) and request["control"] in CONTROLS and integer(request["control_epoch"]), "INVALID_CONTROL")
        if "endpoint" in request:
            require(request["endpoint"] in ("source", "target"), "INVALID_ENDPOINT")
    if action == "app.observe":
        require(isinstance(request["control"], str) and request["control"] in CONTROLS and
                CONTROLS[request["control"]][0] == "GET" and request["control"].endswith(".status"),
                "INVALID_OBSERVATION_CONTROL")
    if action == "app.persist-endpoints":
        require(all(request[key] in ("source", "target") for key in ("postgres", "redis")), "INVALID_ENDPOINT")
    if "arm_id" in request:
        require(matches(request["arm_id"], HEX32), "INVALID_ARM_ID")
    if "quiet_nonce" in request:
        require(matches(request["quiet_nonce"], HEX32), "INVALID_QUIET_NONCE")
    for key in ("quiet_attestation_sha256", "source_fence_sha256"):
        if key in request:
            require(matches(request[key], HEX64), "INVALID_ATTESTATION")
    if "final_lsn" in request:
        lsn_number(request["final_lsn"])
    return request


def read_request(stream):
    # A complete bounded request includes EOF; an authorized peer cannot leave
    # a root forced command waiting on an unbounded stdin stream.
    def expired(_signum, _frame):
        raise Refusal("REQUEST_READ_TIMEOUT")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 3)
    try:
        return parse_request(stream.read(MAX_REQUEST + 1))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def trusted_directory(path, uid=0, private=False):
    path = pathlib.Path(path)
    require(path.is_absolute(), "UNTRUSTED_DIRECTORY")
    for entry in reversed([path, *path.parents]):
        info = entry.lstat()
        require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and
                info.st_uid in (0, uid) and not info.st_mode & 0o022, "UNTRUSTED_DIRECTORY")
    if private:
        require(stat.S_IMODE(path.stat().st_mode) == 0o700 and path.stat().st_uid == uid,
                "UNTRUSTED_DIRECTORY")
    return path


def private_read(path, uid=0, mode=0o600, limit=8 * 1024 * 1024):
    path = pathlib.Path(path)
    trusted_directory(path.parent, uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1 and
                stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "UNTRUSTED_FILE")
        with os.fdopen(fd, "rb", closefd=False) as source:
            return source.read(limit + 1)
    finally:
        os.close(fd)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value, uid=0, *, return_bytes=False):
    path = pathlib.Path(path)
    trusted_directory(path.parent, uid, private=True)
    if path.exists() or path.is_symlink():
        private_read(path, uid)
    temporary = path.parent / (".write-" + secrets.token_hex(16))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            raw = canonical(value)
            out.write(raw + b"\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    return raw if return_bytes else None


def read_endpoints(path=ENDPOINTS, uid=0):
    path = pathlib.Path(path)
    trusted_directory(path.parent, uid)
    require(path.parent.stat().st_uid == uid and stat.S_IMODE(path.parent.stat().st_mode) == 0o755,
            "ENDPOINT_DIRECTORY_UNTRUSTED")
    value = strict_json(private_read(path, uid, mode=0o644), 1024)
    require(type(value) is dict and set(value) == {"v", "postgres", "redis"} and
            type(value["v"]) is int and value["v"] == 1 and
            all(value[key] in ("source", "target") for key in ("postgres", "redis")), "ENDPOINT_STATE_INVALID")
    return value


def persist_endpoints(value, path=ENDPOINTS, uid=0):
    path = pathlib.Path(path)
    before = read_endpoints(path, uid)
    require(all(before[key] != "target" or value[key] == "target" for key in ("postgres", "redis")), "ENDPOINT_ROLLBACK_FORBIDDEN")
    temporary = path.parent / (".endpoints-" + secrets.token_hex(16))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o644)
            out.write(canonical(value) + b"\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    return read_endpoints(path, uid)


@contextlib.contextmanager
def file_lock(path, uid=0, nonblocking=False):
    trusted_directory(pathlib.Path(path).parent, uid, private=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1 and
                stat.S_IMODE(info.st_mode) == 0o600, "UNTRUSTED_LOCK")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            raise Uncertain("OPERATION_BUSY") from None
        yield
    finally:
        os.close(fd)


def validate_config(config):
    require(type(config) is dict and set(config) == {
        "v", "run", "owner", "program_sha256", "machine_id_sha256", "app", "pg", "redis"}, "CONFIG_FIELDS")
    require(type(config["v"]) is int and config["v"] == 1 and config["run"] == RUN and
            matches(config["owner"], HEX32), "CONFIG_IDENTITY")
    require(all(matches(config[k], HEX64) for k in ("program_sha256", "machine_id_sha256")), "CONFIG_DIGEST")
    common = {"container_id", "image_id", "config_sha256", "rootfs_diffids"}
    for kind in ("app", "pg", "redis"):
        entry = config[kind]
        extra = {"control_port"} if kind == "app" else {"owner", "volume_path"}
        if kind == "pg":
            extra |= {"system_identifier", "source_timeline"}
        require(type(entry) is dict and set(entry) == common | extra, "CONFIG_RESOURCE_FIELDS")
        require(matches(entry["container_id"], HEX64) and matches(entry["image_id"], "sha256:" + HEX64) and
                matches(entry["config_sha256"], HEX64), "CONFIG_RESOURCE_IDENTITY")
        require(type(entry["rootfs_diffids"]) is list and 0 < len(entry["rootfs_diffids"]) <= 128 and
                all(matches(item, "sha256:" + HEX64) for item in entry["rootfs_diffids"]), "CONFIG_ROOTFS")
        if kind == "app":
            require(integer(entry["control_port"], 1, 65535), "CONFIG_CONTROL_PORT")
        else:
            require(matches(entry["owner"], HEX32) and isinstance(entry["volume_path"], str) and
                    pathlib.Path(entry["volume_path"]).is_absolute() and ".." not in pathlib.Path(entry["volume_path"]).parts,
                    "CONFIG_VOLUME")
    pg = config["pg"]
    require(matches(pg["system_identifier"], r"[1-9][0-9]{0,19}") and int(pg["system_identifier"]) < (1 << 64) and
            integer(pg["source_timeline"], 1, (1 << 32) - 1), "CONFIG_PG_IDENTITY")
    return config


def load_config():
    require(os.geteuid() == 0, "ROOT_REQUIRED")
    trusted_directory(SCOPE, private=True)
    config = validate_config(strict_json(private_read(SCOPE / "config.json"), 64 * 1024))
    require(pathlib.Path(__file__).absolute() == PROGRAM, "PROGRAM_PATH")
    require(sha(private_read(PROGRAM, mode=0o700)) == config["program_sha256"], "PROGRAM_CHANGED")
    machine = pathlib.Path("/etc/machine-id")
    trusted_directory(machine.parent)
    require(not machine.is_symlink() and machine.stat().st_uid == 0 and not machine.stat().st_mode & 0o022,
            "MACHINE_ID_UNTRUSTED")
    require(sha(machine.read_bytes().strip()) == config["machine_id_sha256"], "WRONG_MACHINE")
    return config


class FileStore:
    """Short flock-protected, fsync checkpoint + append-only audit ledger."""
    def __init__(self, config, scope=SCOPE, uid=0):
        self.config, self.scope, self.uid = config, pathlib.Path(scope), uid
        self._cached_raw, self._cached_state = None, None
        trusted_directory(self.scope, uid, private=True)

    def _read(self):
        path = self.scope / "state.json"
        if not path.exists():
            require(not path.is_symlink(), "UNTRUSTED_FILE")
            return {"v": 1, "run": RUN, "owner": self.config["owner"], "epoch": 0,
                    "operations": {}, "pg": {"authority": "AWS", "arm": None, "promotion_intent": None},
                    "redis": {"authority": "AWS"}}
        # Always reopen and validate the complete file while holding ledger.lock.
        # Only exact trusted bytes can reuse this instance's immutable snapshot.
        raw = private_read(path, self.uid)
        state = (marshal.loads(self._cached_state) if raw == self._cached_raw else
                 strict_json(raw, 8 * 1024 * 1024))
        require(state["run"] == RUN and state["owner"] == self.config["owner"] and integer(state["epoch"]), "LEDGER_IDENTITY")
        if raw != self._cached_raw:
            self._cached_raw, self._cached_state = raw, marshal.dumps(state)
        return state

    @staticmethod
    def _unchanged(before, value):
        if before is None:
            return False
        # Only this update's self-generated, validated JSON snapshot is decoded.
        # A binary mismatch can be just dictionary order; canonical JSON remains
        # authoritative for changes, including numeric types and signed zero.
        try:
            if before == marshal.dumps(value):
                return True
        except (ValueError, TypeError):
            pass
        return canonical(marshal.loads(before)) == canonical(value)

    def update(self, change):
        with file_lock(self.scope / "ledger.lock", self.uid):
            state = self._read()
            before = {key: marshal.dumps(value) for key, value in state["operations"].items()}
            result = change(state)
            written = atomic_json(self.scope / "state.json", state, self.uid, return_bytes=True)
            # A callback can supply JSON-compatible tuples or subclasses. Decode
            # our own canonical output once to retain ordinary JSON read types;
            # untrusted file changes still pass strict_json in _read.
            self._cached_raw, self._cached_state = written + b"\n", marshal.dumps(json.loads(written))
            changed = [{"op_id": key, "action": value["request"]["action"], "phase": value["phase"],
                        "request_sha256": value["request_sha256"],
                        "code": value.get("response", {}).get("code")}
                       for key, value in state["operations"].items() if not self._unchanged(before.get(key), value)]
            event = {"epoch": state["epoch"], "at": time.time(), "state_sha256": sha(written),
                     "operations": changed, "pg_authority": state["pg"]["authority"],
                     "redis_authority": state["redis"]["authority"]}
            path = self.scope / "events.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and info.st_nlink == 1 and
                        stat.S_IMODE(info.st_mode) == 0o600, "UNTRUSTED_EVENT_FILE")
                with os.fdopen(fd, "wb", closefd=False) as out:
                    out.write(canonical(event) + b"\n")
                    out.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            fsync_directory(self.scope)
            return copy.deepcopy(result)

    def read(self):
        with file_lock(self.scope / "ledger.lock", self.uid):
            return self._read()

    def operation_lock(self, op_id):
        require(matches(op_id, HEX32), "INVALID_ID")
        return file_lock(self.scope / ("op-" + op_id + ".lock"), self.uid, nonblocking=True)

    def mutation_lock(self):
        return file_lock(self.scope / "mutation.lock", self.uid, nonblocking=True)


PG_QUERY = """SET statement_timeout = '1s';
SELECT json_build_object(
'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
'version_num',current_setting('server_version_num')::int,'in_recovery',pg_is_in_recovery(),
'transaction_read_only',current_setting('transaction_read_only'),
'default_transaction_read_only',current_setting('default_transaction_read_only'),
'listen_addresses',current_setting('listen_addresses'),'port',current_setting('port')::int,
'replay_lsn',pg_last_wal_replay_lsn()::text,
'current_lsn',CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_current_wal_lsn()::text END,
'source_timeline',COALESCE((SELECT received_tli FROM pg_stat_wal_receiver LIMIT 1),
 (SELECT timeline_id FROM pg_control_checkpoint())),
'wal_timeline',CASE WHEN pg_is_in_recovery() THEN NULL
 ELSE substring(pg_walfile_name(pg_current_wal_lsn()),1,8) END);
"""


class LocalBackend:
    def __init__(self, config):
        self.config = config

    def command(self, argv, data=None):
        try:
            result = subprocess.run(argv, input=data, capture_output=True, timeout=2,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
        except (subprocess.TimeoutExpired, OSError):
            raise Uncertain("COMMAND_OUTCOME_UNKNOWN") from None
        if result.returncode:
            raise Uncertain("COMMAND_OUTCOME_UNKNOWN")
        require(len(result.stdout) <= 1024 * 1024, "COMMAND_OUTPUT_LIMIT")
        return result.stdout

    def docker_json(self, kind, identifier):
        result = strict_json(self.command(["/usr/bin/docker", kind, "inspect", identifier]), 1024 * 1024)
        require(isinstance(result, list) and len(result) == 1 and type(result[0]) is dict, "RESOURCE_INSPECTION")
        return result[0]

    def resource(self, kind):
        expected = self.config[kind]
        name = {"pg": PG_CONTAINER, "redis": REDIS_CONTAINER, "app": APP_CONTAINER}[kind]
        volume_name, destination = (PG_VOLUME, PG_DATA) if kind == "pg" else (REDIS_VOLUME, "/data")
        # These independent inspections use only fixed, configured identities.
        # All complete before resource validation can permit any caller mutation.
        with ThreadPoolExecutor(max_workers=3) as executor:
            container_read = executor.submit(self.docker_json, "container", name)
            image_read = executor.submit(self.docker_json, "image", expected["image_id"])
            volume_read = executor.submit(self.docker_json, "volume", volume_name) if kind != "app" else None
            actual, image = container_read.result(), image_read.result()
            volume = volume_read.result() if volume_read else None
        require(actual["Id"] == expected["container_id"] and actual["Image"] == expected["image_id"] and
                sha(actual["Config"]) == expected["config_sha256"], "RESOURCE_IDENTITY_CHANGED")
        require(image["Id"] == expected["image_id"] and image["RootFS"]["Layers"] == expected["rootfs_diffids"], "IMAGE_CHANGED")
        require(actual["State"]["Running"] is True and actual["HostConfig"]["RestartPolicy"]["Name"] == "no",
                "RESOURCE_NOT_RUNNING_OR_RESTARTABLE")
        require(actual["HostConfig"]["NetworkMode"] == "host" and not actual["HostConfig"].get("PortBindings"),
                "RESOURCE_NETWORK_CHANGED")
        if kind != "app":
            labels = actual["Config"].get("Labels", {})
            run = "sub2api-pg-replica-20261016" if kind == "pg" else "sub2api-redis-standby-20261016"
            require(labels.get(OWNER_LABEL) == expected["owner"] and labels.get("io.sub2api.migration.run") == run,
                    "RESOURCE_OWNER_CHANGED")
            require(volume["Driver"] == "local" and volume["Mountpoint"] == expected["volume_path"] and
                    volume.get("Labels", {}).get(OWNER_LABEL) == expected["owner"] and
                    volume.get("Labels", {}).get("io.sub2api.migration.run") == run, "VOLUME_CHANGED")
            mounts = [item for item in actual["Mounts"] if item["Destination"] == destination]
            require(len(mounts) == 1 and mounts[0]["Name"] == volume_name and mounts[0]["Source"] == expected["volume_path"] and
                    mounts[0]["Type"] == "volume" and mounts[0]["RW"] is True, "VOLUME_MOUNT_CHANGED")
            require(all(not item.is_symlink() for item in [pathlib.Path(expected["volume_path"]),
                    *pathlib.Path(expected["volume_path"]).parents]), "VOLUME_SYMLINK")
        return actual

    def pg_locks(self):
        @contextlib.contextmanager
        def acquire():
            with file_lock(AUTHORITY_SCOPE / "operation.lock", nonblocking=True):
                with file_lock(AUTHORITY_SCOPE / "authority.lock", nonblocking=True):
                    yield
        return acquire()

    def pg_manifest(self):
        state = strict_json(private_read(AUTHORITY_SCOPE / "state.json"), 256 * 1024)
        cfg = self.config["pg"]
        require(state["run"] == "sub2api-pg-authority-20261016" and state["phase"] == "standby-role-aware-ready" and
                all(state[key] == cfg[key] for key in ("owner", "system_identifier", "source_timeline", "container_id", "image_id", "volume_path")),
                "PG_AUTHORITY_CHANGED")
        return state

    def pg_save_manifest(self, manifest):
        atomic_json(AUTHORITY_SCOPE / "state.json", manifest)

    def pg_sample(self):
        self.resource("pg")
        result = strict_json(self.command(["/usr/bin/docker", "exec", "--user", "postgres", "-i", self.config["pg"]["container_id"],
            "psql", "-X", "-q", "-A", "-t", "-w", "-v", "ON_ERROR_STOP=1", "-h", "/tmp", "-p", "25432",
            "-U", "sub2api", "-d", "sub2api"], PG_QUERY.encode()), 16384)
        result["free_bytes"] = __import__("shutil").disk_usage(self.config["pg"]["volume_path"]).free
        if result["wal_timeline"] is not None:
            require(matches(result["wal_timeline"], r"[0-9A-Fa-f]{8}"), "PG_TIMELINE_INVALID")
            result["wal_timeline"] = int(result["wal_timeline"], 16)
        return result

    def pg_promote(self):
        self.command(["/usr/bin/docker", "exec", "--user", "postgres", self.config["pg"]["container_id"],
                      "pg_ctl", "-D", PG_DATA, "promote", "-W"])

    def app_control(self, control, control_epoch=0, endpoint=None):
        self.resource("app")
        return self._app_request(control, self._app_token(), control_epoch, endpoint)

    def _app_token(self):
        token = private_read(SCOPE / "app-control.token", limit=512).strip()
        require(re.fullmatch(rb"[A-Za-z0-9_-]{32,256}", token), "INVALID_PRIVATE_BEARER")
        return token

    def app_restore_adoption(self, request, runtime, latest, ledger):
        """Authorize only a new restore across the one root-adopted generation.

        The root installer keeps the original ledger bytes beside this receipt.
        Current history must still contain those exact operations. No adoption
        path may rebind an existing operation, route data, or rewrite history.
        Missing, partial, changed, or uncertain evidence fails closed.
        """
        receipt = strict_json(private_read(ADOPTION_SCOPE / "controller-adoption.json"), 64 * 1024)
        fields = {"v", "run", "owner", "old_instance_id", "new_instance_id", "old_container_id",
                  "new_container_id", "new_app_config_sha256", "new_control_port", "new_control_token_sha256",
                  "controller_config_raw_sha256", "ledger_raw_sha256", "ledger_epoch", "operation_floor",
                  "source_closed_sha256", "rolling_complete_sha256", "old_clean_exit"}
        require(type(receipt) is dict and set(receipt) == fields and type(receipt["v"]) is int and
                receipt["v"] == 1 and receipt["run"] == RUN and receipt["owner"] == self.config["owner"],
                "APP_ADOPTION_RECEIPT")
        require(all(matches(receipt[key], HEX64) for key in (
                    "old_container_id", "new_container_id", "new_app_config_sha256", "new_control_token_sha256",
                    "controller_config_raw_sha256", "ledger_raw_sha256", "source_closed_sha256", "rolling_complete_sha256")) and
                all(matches(receipt[key], r"[A-Za-z0-9_-]{1,96}") for key in ("old_instance_id", "new_instance_id")) and
                receipt["old_container_id"] != receipt["new_container_id"] and
                receipt["old_instance_id"] != receipt["new_instance_id"] and
                type(receipt["ledger_epoch"]) is int and receipt["ledger_epoch"] == 14 and
                type(receipt["operation_floor"]) is int and receipt["operation_floor"] == 14 and
                type(receipt["new_control_port"]) is int and receipt["new_control_port"] == 18093,
                "APP_ADOPTION_IDENTITY")
        app = self.config["app"]
        require(receipt["new_container_id"] == app["container_id"] and
                receipt["new_app_config_sha256"] == app["config_sha256"] and
                receipt["new_control_port"] == app["control_port"] and
                receipt["new_instance_id"] == runtime["instance_id"] and
                receipt["old_instance_id"] == latest.get("instance_id") and
                sha(self._app_token()) == receipt["new_control_token_sha256"] and
                sha(private_read(SCOPE / "config.json")) == receipt["controller_config_raw_sha256"],
                "APP_ADOPTION_CONTROLLER_CHANGED")
        baseline_raw = private_read(ADOPTION_SCOPE / "controller-ledger-before.json")
        rolling_raw = private_read(ADOPTION_SCOPE / "state.json", limit=2 * 1024 * 1024)
        source_raw = private_read(ADOPTION_SCOPE / "source-controller-closed.json", limit=2 * 1024 * 1024)
        require(sha(baseline_raw) == receipt["ledger_raw_sha256"] and
                sha(rolling_raw) == receipt["rolling_complete_sha256"] and
                sha(source_raw) == receipt["source_closed_sha256"], "APP_ADOPTION_EVIDENCE_CHANGED")
        baseline = strict_json(baseline_raw, 8 * 1024 * 1024)
        rolling = strict_json(rolling_raw, 2 * 1024 * 1024)
        source = strict_json(source_raw, 2 * 1024 * 1024)
        require(baseline.get("v") == 1 and baseline.get("run") == RUN and
                baseline.get("owner") == self.config["owner"] and
                type(baseline.get("epoch")) is int and baseline["epoch"] == receipt["ledger_epoch"] and
                source.get("owner") == self.config["owner"] and source.get("run") == RUN and
                source.get("window") is None and source.get("controller_stopped") is True and
                source.get("pg") == source.get("redis") == "AWS" and
                source.get("target_ledger_sha256") == receipt["ledger_raw_sha256"], "APP_ADOPTION_SOURCE_NOT_CLOSED")
        require(rolling.get("v") == 1 and rolling.get("owner") == self.config["owner"] and
                rolling.get("phase") == "COMPLETE" and rolling.get("intent") is None,
                "APP_ADOPTION_ROLLING_NOT_COMPLETE")
        old_identity, new_identity, completed = (rolling.get(key, {}) for key in
                                               ("old_identity", "new_identity", "adoption_receipt"))
        require(old_identity.get("container_id") == receipt["old_container_id"] and
                new_identity.get("container_id") == app["container_id"] and
                new_identity.get("config_sha256") == app["config_sha256"] and
                old_identity.get("image_id") == new_identity.get("image_id") == app["image_id"] and
                old_identity.get("rootfs_diffids") == new_identity.get("rootfs_diffids") == app["rootfs_diffids"] and
                new_identity.get("control_token_sha256") == receipt["new_control_token_sha256"] and
                completed.get("v") == 1 and completed.get("owner") == self.config["owner"] and
                completed.get("old_container_id") == receipt["old_container_id"] and
                completed.get("new_app") == new_identity and completed.get("control_port") == 18093 and
                completed.get("endpoint_directory") == str(PREFIX / "app") and
                completed.get("endpoints") == {"v": 1, "postgres": "source", "redis": "source"} and
                completed.get("postgres_authority") == completed.get("redis_authority") == "AWS" and
                completed.get("old_exited_cleanly") is True and completed.get("new_process_failed") == 0 and
                completed.get("new_process_dropped") == 0, "APP_ADOPTION_ROLLING_IDENTITY")
        original = baseline.get("operations")
        current = ledger.get("operations", {})
        entry = current.get(request["op_id"], {})
        require(type(original) is dict and original and
                all(key in current and canonical(current[key]) == canonical(value) for key, value in original.items()) and
                all(value.get("phase") == "COMPLETE" or
                    (value["request"]["action"] == "app.quiesce" and value.get("cancelled") is True)
                    for value in original.values()) and
                all(value.get("cancelled") is True for value in original.values()
                    if value["request"]["action"] == "app.quiesce") and
                any(canonical(value) == canonical(latest) for value in original.values()) and
                request["action"] == "app.restore" and request["op_id"] not in original and
                integer(request["expected_epoch"], receipt["operation_floor"]) and
                entry.get("request") == request and not entry.get("instance_id") and
                ledger.get("owner") == baseline["owner"] and ledger.get("run") == RUN and
                ledger["pg"] == baseline["pg"] and ledger["redis"] == baseline["redis"] and
                ledger["pg"]["authority"] == ledger["redis"]["authority"] == "AWS" and
                not ledger["pg"].get("promotion_intent"), "APP_ADOPTION_OPERATION_BOUNDARY")
        clean = {"container_id": receipt["old_container_id"], "exit_code": 0, "oom_killed": False,
                 "pid": 0, "restart_policy": "no"}
        require(canonical(receipt["old_clean_exit"]) == canonical(clean), "APP_ADOPTION_OLD_EXIT_EVIDENCE")
        old = self.docker_json("container", receipt["old_container_id"])
        require(old["Id"] == receipt["old_container_id"] and old["Image"] == app["image_id"] and
                sha(old["Config"]) == old_identity.get("config_sha256") and
                old["State"]["Running"] is False and old["State"]["Status"] == "exited" and
                type(old["State"]["Pid"]) is int and old["State"]["Pid"] == 0 and
                type(old["State"]["ExitCode"]) is int and old["State"]["ExitCode"] == 0 and
                old["State"]["OOMKilled"] is False and old["HostConfig"]["RestartPolicy"]["Name"] == "no",
                "APP_ADOPTION_OLD_NOT_RETIRED")
        self.resource("app")
        require(self.endpoint_state() == {"v": 1, "postgres": "source", "redis": "source"},
                "APP_ADOPTION_ENDPOINTS_CHANGED")
        return True

    def _app_request(self, control, token, control_epoch=0, endpoint=None):
        method, path = CONTROLS[control]
        payload = None
        if method == "POST":
            body = {} if control == "ingress.pause" else {"expected_epoch": control_epoch}
            if endpoint is not None:
                body["endpoint"] = endpoint
            payload = canonical(body)
        connection = http.client.HTTPConnection("127.0.0.1", self.config["app"]["control_port"], timeout=2)
        try:
            connection.request(method, path, body=payload, headers={"Authorization": "Bearer " + token.decode("ascii"),
                               "Content-Type": "application/json", "Connection": "close"})
            reply = connection.getresponse()
            data = reply.read(65537)
            require(len(data) <= 65536, "CONTROL_OUTPUT_LIMIT")
            if not 200 <= reply.status < 300:
                # Never echo the application's raw diagnostic or headers.
                return {"http_status": reply.status, "body": {}}
            body = {} if reply.status == 204 and not data else strict_json(data, 65536)
            require(type(body) is dict, "CONTROL_RESPONSE_INVALID")
            return {"http_status": reply.status, "body": sanitize_control(body)}
        except (OSError, http.client.HTTPException):
            raise Uncertain("CONTROL_OUTCOME_UNKNOWN") from None
        finally:
            connection.close()

    def app_quiet_snapshot(self):
        # Fixed read-only batch: caller cannot supply a path, token, control,
        # SQL, or endpoint. Each HTTP response retains existing sanitization.
        return self.snapshot_with(self.app_session())

    def app_session(self):
        self.resource("app")
        token = self._app_token()
        return lambda control, epoch=0, endpoint=None: self._app_request(control, token, epoch, endpoint)

    def snapshot_with(self, read):
        result = {"app_before": read("runtime.status")}
        for component in ("ingress", "writers", "redis", "postgres"):
            result[component] = read(component + ".status")
        with self.pg_locks():
            self.pg_manifest()
            pg = self.pg_sample()
        result["pg"] = {key: pg.get(key) for key in ("system_identifier", "in_recovery", "replay_lsn", "wal_timeline")}
        result["app"] = read("runtime.status")
        return result

    def endpoint_state(self):
        return read_endpoints()

    def persist_endpoints(self, value):
        return persist_endpoints(value)

    def redis_target_address(self):
        actual = self.resource("app")
        values = [item.split("=", 1)[1] for item in actual["Config"].get("Env", [])
                  if item.startswith("MIGRATION_REDIS_TARGET_ADDR=")]
        require(len(values) == 1, "REDIS_TARGET_ADDRESS_UNPINNED")
        address = values[0]
        host, separator, port = address.rpartition(":")
        require(separator and matches(port, r"[0-9]{1,5}") and 1 <= int(port) <= 65535,
                "REDIS_TARGET_ADDRESS_UNPINNED")
        try:
            require(ipaddress.ip_address(host.strip("[]")).is_loopback, "REDIS_TARGET_ADDRESS_UNPINNED")
        except ValueError:
            raise Refusal("REDIS_TARGET_ADDRESS_UNPINNED") from None
        return address

    def redis_command(self, *parts):
        return self.command(["/usr/bin/docker", "exec", self.config["redis"]["container_id"], "redis-cli", "--raw", "-h", "127.0.0.1",
                             "-p", "26379", *parts]).decode("utf-8", errors="strict").strip()

    def redis_inspect(self):
        self.resource("redis")
        info = {}
        for line in self.redis_command("INFO").splitlines():
            if ":" in line and not line.startswith("#"):
                key, value = line.split(":", 1)
                info[key] = value
        require(matches(info.get("run_id"), r"[0-9a-f]{40}") and
                info.get("role") in ("master", "slave") and matches(info.get("master_replid"), r"[0-9a-f]{40}") and
                info.get("config_file") == "/data/migration.conf", "REDIS_ROLE_OR_IDENTITY_UNKNOWN")
        summary = {key: value for key, value in info.items() if re.fullmatch(r"db[0-9]+", key) and
                   re.fullmatch(r"keys=\d+,expires=\d+,avg_ttl=\d+", value)}
        result = {key: info[key] for key in ("run_id", "role", "master_replid")}
        for key in ("master_repl_offset", "slave_repl_offset", "second_repl_offset"):
            if key in info:
                require(matches(info[key], r"-?[0-9]{1,20}"), "REDIS_OFFSET_INVALID")
                result[key] = int(info[key])
        failover = info.get("master_failover_state", "no-failover")
        require(failover in ("no-failover", "waiting-for-sync", "failover-in-progress"), "REDIS_FAILOVER_UNKNOWN")
        result.update(failover_state=failover, keyspace_summary=summary, keyspace_sha256=sha(summary))
        return result

    def redis_preroute_pair(self):
        self.resource("redis")
        pair = {}
        for name, port in (("source", "16379"), ("target", "26379")):
            raw = self.command(["/usr/bin/docker", "exec", self.config["redis"]["container_id"],
                "redis-cli", "--raw", "-h", "127.0.0.1", "-p", port, "INFO"])
            info, section = {}, None
            for line in raw.decode("utf-8", errors="strict").splitlines():
                if line.startswith("# "):
                    section = line[2:]
                elif section in ("Server", "Replication") and ":" in line:
                    # INFO Modules legitimately repeats module:. Only the two
                    # identity/replication sections participate in this proof.
                    key, value = line.split(":", 1)
                    require(key not in info, "REDIS_DUPLICATE_INFO_FIELD")
                    info[key] = value
            pair[name] = info
        return pair

    def redis_persist_primary(self):
        before = self.redis_inspect()
        require(before["role"] == "master" and before["failover_state"] == "no-failover", "REDIS_NOT_PRIMARY")
        _, identity = self.redis_config()
        require(self.redis_command("CONFIG", "REWRITE") == "OK", "REDIS_REWRITE_FAILED")
        raw, rewritten_identity = self.redis_config()
        require(identity == rewritten_identity, "REDIS_CONFIG_OWNERSHIP_CHANGED")
        require(not re.search(rb"(?im)^\s*(?:replicaof|slaveof)\s", raw), "REDIS_CONFIG_STILL_REPLICA")
        after = self.redis_inspect()
        require(after["role"] == "master" and after["run_id"] == before["run_id"], "REDIS_CHANGED_DURING_REWRITE")
        return {**after, "persisted": True, "config_sha256": sha(raw)}

    def redis_config(self):
        data = pathlib.Path(self.config["redis"]["volume_path"])
        trusted_directory(data.parent)
        directory = data.lstat()
        require(stat.S_ISDIR(directory.st_mode) and directory.st_uid > 0 and not directory.st_mode & 0o022,
                "REDIS_DIRECTORY_UNTRUSTED")
        fd = os.open(data / "migration.conf", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == directory.st_uid and
                    stat.S_IMODE(info.st_mode) in (0o600, 0o644) and info.st_size <= 64 * 1024,
                    "REDIS_CONFIG_UNTRUSTED")
            raw = os.read(fd, 65537)
            require(len(raw) <= 65536, "REDIS_CONFIG_UNTRUSTED")
            return raw, (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
        finally:
            os.close(fd)


CONTROL_FIELDS = set("instance_id draining serving active_requests active_hijacked_connections background_task_coverage tasks safe_to_stop background_workers_enabled ready readiness ingress paused epoch queued_requests paused_at quiet held endpoint active_operations tracked_sockets closed pubsub_cache_ready address consumers name channel generation activated activating started pending failures active failed dropped deferred components initialized activation".split())


def sanitize_control(value):
    result = {}
    for key, item in value.items():
        if key not in CONTROL_FIELDS:
            continue
        if key in ("tasks", "readiness", "failures") and type(item) is dict:
            result[key] = {name: ("START_FAILED" if key == "failures" else
                            sanitize_control(row) if type(row) is dict else row)
                           for name, row in item.items() if matches(name, r"[A-Za-z0-9_.-]{1,96}") and
                           (key == "failures" or (key == "readiness" and type(row) is bool) or
                            (key == "tasks" and type(row) is dict))}
        elif type(item) is dict:
            result[key] = sanitize_control(item)
        elif type(item) is list and key in ("consumers", "started", "pending"):
            result[key] = [sanitize_control(row) if type(row) is dict else row for row in item
                           if type(row) is dict or matches(row, r"[A-Za-z0-9_.:-]{1,96}")]
        elif type(item) is str:
            patterns = {"instance_id": r"[A-Za-z0-9_-]{1,96}",
                        "background_task_coverage": r"unavailable|partial",
                        "endpoint": r"source|target", "address": r"[A-Za-z0-9.\[\]:-]{1,128}",
                        "name": r"[A-Za-z0-9_.:-]{1,96}", "channel": r"[A-Za-z0-9_.:-]{1,128}",
                        "paused_at": r"[0-9TZ:.,+\-]{1,48}"}
            if key in patterns and matches(item, patterns[key]):
                result[key] = item
        elif item is None or type(item) in (int, bool):
            result[key] = item
    return result


class TargetRPC:
    def __init__(self, config, backend, store, now=time.time):
        self.config, self.backend, self.store, self.now = validate_config(config), backend, store, now

    def response(self, request, code, result=None, *, epoch=None):
        state = self.store.read()
        return {"ok": code == "OK", "code": code, "run": RUN, "owner": self.config["owner"],
                "op_id": request["op_id"], "action": request["action"], "epoch": state["epoch"] if epoch is None else epoch,
                "phase": state["pg"]["authority"], "result": result or {}}

    def _record_result(self, request, code, result):
        response = self.response(request, code, result)
        def finish(state):
            op = state["operations"][request["op_id"]]
            op.update(phase="COMPLETE", response=response)
            return response
        return self.store.update(finish)

    def handle(self, request):
        request = parse_request(canonical(request))
        if request["action"] == "app.observe":
            return self._observe(request)
        require(request["owner"] == self.config["owner"], "WRONG_OWNER")
        def admit(state):
            existing = state["operations"].get(request["op_id"])
            if existing:
                require(existing["request_sha256"] == sha(request), "OP_ID_REUSED")
                return existing
            require(request["expected_epoch"] == state["epoch"], "EPOCH_CONFLICT")
            require(len(state["operations"]) < 4096 and len(canonical(state)) < 6 * 1024 * 1024, "LEDGER_CAPACITY")
            read_only = request["action"] in READ_ACTIONS or (
                request["action"] == "app.control" and CONTROLS[request["control"]][0] == "GET")
            if not read_only:
                state["epoch"] += 1
            entry = {"request": request, "request_sha256": sha(request), "phase": "INTENT", "accepted_at": self.now()}
            state["operations"][request["op_id"]] = entry
            return entry
        admitted = False
        try:
            op = self.store.update(admit)
            admitted = True
            if op["phase"] == "COMPLETE":
                return op["response"]
            with self.store.operation_lock(request["op_id"]):
                # A concurrent same-op invocation may have completed before we acquired its lock.
                current = self.store.read()["operations"][request["op_id"]]
                if current["phase"] == "COMPLETE":
                    return current["response"]
                lock = self.store.mutation_lock() if self.is_mutation(request) else contextlib.nullcontext()
                with lock:
                    result = self.perform(request)
                    return self._record_result(request, "OK", result)
        except Uncertain as error:
            return self.response(request, "UNKNOWN", {"reason": error.code, "reconcile_required": True})
        except Refusal as error:
            if admitted:
                state = self.store.read()
                if state["pg"].get("promotion_intent") and request["action"] in ("pg.commit", "pg.reconcile"):
                    return self.response(request, "UNKNOWN", {"reason": error.code, "reconcile_required": True})
                if state["redis"]["authority"] in ("COMMIT_INTENT", "TENCENT") and request["action"] == "redis.persist-primary":
                    return self.response(request, "UNKNOWN", {"reason": error.code, "reconcile_required": True})
                if request["action"] in ("app.control", "app.persist-endpoints") and state["operations"][request["op_id"]].get("effect_started"):
                    return self.response(request, "UNKNOWN", {"reason": error.code, "reconcile_required": True})
                return self._record_result(request, error.code, {})
            return self.response(request, error.code)
        except Exception:
            # Includes a crash-equivalent persistence failure after an intent
            # checkpoint. Its original operation remains available to guard.
            return self.response(request, "UNKNOWN", {"reason": "LOCAL_STATE_UNCERTAIN", "reconcile_required": True})

    def _observe(self, request):
        # Waiting for idle must not grow the durable operation/audit history.
        # This closed GET-only path deliberately never admits an operation:
        # even a repeated op_id samples the application again.
        try:
            require(request["owner"] == self.config["owner"], "WRONG_OWNER")
            require(request["expected_epoch"] == self.store.read()["epoch"], "EPOCH_CONFLICT")
            return self.response(request, "OK", self.backend.app_control(request["control"]))
        except Uncertain as error:
            return self.response(request, "UNKNOWN", {"reason": error.code, "reconcile_required": False})
        except Refusal as error:
            return self.response(request, error.code)
        except Exception:
            return self.response(request, "UNKNOWN", {"reason": "OBSERVATION_UNCERTAIN", "reconcile_required": False})

    @staticmethod
    def is_mutation(request):
        return request["action"] not in READ_ACTIONS and not (
            request["action"] == "app.control" and CONTROLS[request["control"]][0] == "GET")

    @staticmethod
    def control_satisfied(request, body, redis_target=None):
        component, action = request["control"].split(".")
        if action in ("pause", "resume"):
            field = "held" if component == "writers" else "paused"
            return body.get(field) is (action == "pause") and (
                component != "redis" or action == "resume" or body.get("quiet") is True)
        if action == "switch" and component == "postgres":
            return body.get("endpoint") == request["endpoint"] and body.get("paused") is True
        if action == "switch" and component == "redis":
            return request["endpoint"] == "target" and redis_target is not None and body.get("address") == redis_target and body.get("paused") is True and body.get("quiet") is True and body.get("active_operations") == 0 and body.get("tracked_sockets") == 0
        if action == "rebuild":
            return body.get("paused") is True and body.get("pubsub_cache_ready") is True
        if action == "activate":
            return body.get("activated") is True and body.get("activating") is False and not body.get("failures") and not body.get("pending")
        return False

    def _app_control(self, request):
        preroute = (request["control"] == "redis.switch" and request.get("endpoint") == "target" and
                    self.backend.endpoint_state()["redis"] == "source")
        try:
            return self._app_control_effect(request)
        except Refusal as error:
            entry = self.store.read()["operations"][request["op_id"]]
            if preroute and not entry.get("effect_started"):
                # The runner already durably chose forward recovery. A short
                # failed proof/status/epoch check must not terminally poison
                # its fixed operation ID. No POST has started, so replay can
                # safely re-evaluate this same immutable request's proofs.
                raise Uncertain(error.code) from None
            raise

    def _app_control_effect(self, request):
        control = request["control"]
        if CONTROLS[control][0] == "GET":
            return self.backend.app_control(control)
        current = self.backend.app_control(control.split(".")[0] + ".status")
        require(current["http_status"] == 200, "CONTROL_STATUS_UNAVAILABLE")
        entry = self.store.read()["operations"][request["op_id"]]
        if entry.get("effect_started"):
            # A lost reply is inspected, never silently retried as a fresh
            # mutation (activation and endpoint changes are not repeatable).
            redis_target = self.backend.redis_target_address() if control == "redis.switch" and request["endpoint"] == "target" else None
            if self.control_satisfied(request, current["body"], redis_target):
                return {"http_status": 200, "body": current["body"], "reconciled": True}
            raise Uncertain("CONTROL_EFFECT_UNCONFIRMED")
        require(current["body"].get("epoch") == request["control_epoch"], "CONTROL_EPOCH_CONFLICT")
        if control in ("postgres.switch", "redis.switch"):
            endpoints = self.backend.endpoint_state()
            if control == "redis.switch" and request["endpoint"] == "target" and endpoints["redis"] == "source":
                self._redis_preroute(current["body"], endpoints)
            else:
                require(endpoints[control.split(".")[0]] == request["endpoint"], "ENDPOINT_NOT_PERSISTED")
        self.store.update(lambda state: state["operations"][request["op_id"]].update(
            effect_started=True, before=current["body"]))
        result = self.backend.app_control(control, request["control_epoch"], request.get("endpoint"))
        if not 200 <= result["http_status"] < 300:
            # 409 may describe a partially performed operation. It carries no
            # proof of absence; replay only observes the fixed status endpoint.
            raise Uncertain("CONTROL_REJECTED_OR_PARTIAL")
        return result

    def _redis_preroute(self, redis, endpoints, read=None):
        # Route selection closes idle source sockets while admission remains
        # closed. It neither opens a target socket nor establishes authority.
        # The runner already fsynced its irreversible intent; this RPC's own
        # INTENT is also fsynced before these fresh proofs and effect_started.
        started = self.now()
        state = self.store.read()
        require(state["pg"]["authority"] == "TENCENT" and endpoints["postgres"] == "target" and
                state["redis"]["authority"] == "AWS", "REDIS_PREROUTE_AUTHORITY")
        with self.backend.pg_locks():
            self._pg_validate(self.backend.pg_sample(), primary=True)

        def body(control):
            result = (read or self.backend.app_control)(control)
            require(result.get("http_status") == 200 and type(result.get("body")) is dict,
                    "REDIS_PREROUTE_CONTROL_UNAVAILABLE")
            return result["body"]

        first = body("runtime.status")
        ingress, writers = body("ingress.status"), body("writers.status")
        pair = self.backend.redis_preroute_pair()
        fresh = body("runtime.status")
        tasks, readiness, embedded = fresh.get("tasks"), fresh.get("readiness"), fresh.get("ingress")
        require(isinstance(first.get("instance_id"), str) and first["instance_id"] and
                fresh.get("instance_id") == first["instance_id"] and fresh.get("serving") is True and
                fresh.get("draining") is False and fresh.get("background_workers_enabled") is False and
                all(type(fresh.get(k)) is int and fresh[k] == 0 for k in ("active_requests", "active_hijacked_connections")) and
                type(readiness) is dict and readiness.get("writer_coverage") is True and readiness.get("quiet_controls") is True and
                type(tasks) is dict and REQUIRED_WRITERS <= tasks.keys() and
                all(type(row) is dict and all(type(row.get(k)) is int and row[k] == 0
                    for k in ("active", "pending", "failed", "dropped")) for row in tasks.values()), "REDIS_PREROUTE_RUNTIME_NOT_QUIET")
        require(ingress.get("paused") is True and ingress.get("quiet") is True and integer(ingress.get("epoch")) and
                type(embedded) is dict and embedded.get("paused") is True and embedded.get("quiet") is True and
                embedded.get("epoch") == ingress["epoch"] and writers.get("held") is True and writers.get("quiet") is True and
                redis.get("paused") is True and redis.get("quiet") is True and
                type(redis.get("active_operations")) is int and redis["active_operations"] == 0,
                "REDIS_PREROUTE_CONTROLS_NOT_QUIET")
        source, target = pair["source"], pair["target"]
        require(source.get("run_id") == REDIS_SOURCE_RUN_ID and target.get("run_id") == REDIS_TARGET_RUN_ID and
                source.get("role") == "master" and target.get("role") == "slave" and
                source.get("master_replid") == target.get("master_replid") == REDIS_SOURCE_REPLID and
                source.get("master_failover_state", "no-failover") == target.get("master_failover_state", "no-failover") == "no-failover" and
                target.get("master_host") == "127.0.0.1" and target.get("master_port") == "16379" and
                target.get("master_link_status") == "up" and target.get("master_sync_in_progress") == "0" and
                source.get("connected_slaves") == "1" and target.get("connected_slaves") == "0",
                "REDIS_PREROUTE_REPLICATION_CHANGED")
        replicas = [value for key, value in source.items() if re.fullmatch(r"slave[0-9]+", key)]
        require(len(replicas) == 1 and {"ip=172.18.0.1", "port=26379", "state=online"} <= set(replicas[0].split(",")),
                "REDIS_PREROUTE_REPLICA_NOT_UNIQUE")
        require(math.isfinite(self.now()) and 0 <= self.now() - started <= 2, "REDIS_PREROUTE_SAMPLE_STALE")

    def _pg_validate(self, sample, final_lsn=None, primary=False):
        cfg = self.config["pg"]
        require(sample.get("system_identifier") == cfg["system_identifier"] and sample.get("version_num") == 180003 and
                sample.get("listen_addresses") == "127.0.0.1" and sample.get("port") == 25432,
                "PG_IDENTITY_CHANGED")
        require(sample.get("in_recovery") is (not primary), "PG_ROLE_UNKNOWN")
        if primary:
            require(integer(sample.get("wal_timeline"), cfg["source_timeline"] + 1, (1 << 32) - 1) and
                    sample.get("transaction_read_only") == sample.get("default_transaction_read_only") == "off", "PG_PRIMARY_NOT_WRITABLE")
        else:
            require(sample.get("source_timeline") == cfg["source_timeline"] and sample.get("transaction_read_only") == "on" and
                    sample.get("default_transaction_read_only") == "off" and sample.get("free_bytes", 0) >= 2 * 1024**3,
                    "PG_STANDBY_NOT_READY")
        if final_lsn is not None:
            require(lsn_number(sample.get("current_lsn" if primary else "replay_lsn")) >= lsn_number(final_lsn), "PG_REPLAY_BEHIND")

    def _pg_reconcile(self):
        accepted = self.store.read()["pg"].get("promotion_intent")
        with self.backend.pg_locks():
            manifest = self.backend.pg_manifest()
            persisted = manifest.get("promotion_intent")
            require(not persisted or accepted == persisted, "PG_INTENT_MISMATCH")
            sample = self.backend.pg_sample()
            if not accepted:
                self._pg_validate(sample)
                require(manifest["authority_mode"] == "STANDBY" and not persisted, "PG_UNAUTHORIZED_ROLE")
                return {"authority": "AWS", "in_recovery": True, "system_identifier": sample["system_identifier"]}
            self._pg_validate(sample, accepted["final_lsn"], primary=sample.get("in_recovery") is False)
            if sample["in_recovery"]:
                manifest.update(authority_mode="PROMOTION_INTENT", promotion_intent=accepted)
                self.backend.pg_save_manifest(manifest)
            else:
                manifest.update(authority_mode="PRIMARY", promotion_intent=accepted)
                self.backend.pg_save_manifest(manifest)
                self.store.update(lambda state: state["pg"].update(authority="TENCENT"))
                return {"authority": "TENCENT", "in_recovery": False, "writable": True,
                        "system_identifier": sample["system_identifier"], "wal_timeline": sample["wal_timeline"],
                        "source_fence_evidence": "trusted-runner-attestation"}
        # No network/subprocess is held under the RPC ledger lock. The monitor
        # sees the accepted intent before pg_ctl; an expired arm cannot revoke it.
        self.backend.pg_promote()
        with self.backend.pg_locks():
            manifest = self.backend.pg_manifest()
            require(manifest.get("promotion_intent") == accepted, "PG_INTENT_MISMATCH")
            sample = self.backend.pg_sample()
            if sample.get("in_recovery") is True:
                raise Uncertain("PG_PROMOTION_PENDING")
            self._pg_validate(sample, accepted["final_lsn"], primary=True)
            manifest.update(authority_mode="PRIMARY", promotion_intent=accepted)
            self.backend.pg_save_manifest(manifest)
            self.store.update(lambda state: state["pg"].update(authority="TENCENT"))
            return {"authority": "TENCENT", "in_recovery": False, "writable": True,
                    "system_identifier": sample["system_identifier"], "wal_timeline": sample["wal_timeline"],
                    "source_fence_evidence": "trusted-runner-attestation"}

    def _snapshot_result(self, sampled, started):
        controls = ("app_before", "app", "ingress", "writers", "redis", "postgres")
        require(type(sampled) is dict and all(type(sampled.get(key)) is dict and
                    sampled[key].get("http_status") == 200 and type(sampled[key].get("body")) is dict
                    for key in controls) and type(sampled.get("pg")) is dict, "QUIET_SNAPSHOT_UNAVAILABLE")
        first, fresh = sampled["app_before"]["body"], sampled["app"]["body"]
        require(isinstance(first.get("instance_id"), str) and first["instance_id"] and
                    fresh.get("instance_id") == first["instance_id"], "APP_INSTANCE_CHANGED")
        elapsed = self.now() - started
        require(math.isfinite(elapsed) and 0 <= elapsed <= 2, "QUIET_SNAPSHOT_SAMPLE_STALE")
        return {"app": sampled["app"], "pg": sampled["pg"],
                    **{key: sampled[key]["body"] for key in ("ingress", "writers", "redis", "postgres")}}

    @staticmethod
    def _body(read, control):
        result = read(control)
        require(type(result) is dict and result.get("http_status") == 200 and type(result.get("body")) is dict,
                "MACRO_STATUS_UNAVAILABLE")
        return result["body"]

    def _macro_step(self, request, name, control, read, endpoint=None):
        op_id = request["op_id"]
        step = self.store.read()["operations"][op_id].get("substeps", {}).get(name)
        if step and step.get("phase") == "COMPLETE":
            return step["body"]
        component = control.split(".")[0]
        body = self._body(read, component + ".status")
        desired = {"control": control, "endpoint": endpoint}
        target = self.backend.redis_target_address() if control == "redis.switch" else None
        def satisfied_by(value):
            result = self.control_satisfied(desired, value, target)
            if control == "ingress.pause":
                result = result and value.get("quiet") is True
            if control in ("postgres.pause", "redis.pause", "redis.switch"):
                result = result and type(value.get("active_operations")) is int and value["active_operations"] == 0
            if control == "writers.pause":
                result = result and value.get("quiet") is True
            return result
        satisfied = satisfied_by(body)
        if step and step.get("effect_started"):
            require(step["control"] == control and step.get("endpoint") == endpoint, "MACRO_STEP_CHANGED")
            if not satisfied:
                raise Uncertain("MACRO_EFFECT_UNCONFIRMED")
        elif not satisfied:
            require(integer(body.get("epoch")), "MACRO_EPOCH_MISSING")
            step = {"control": control, "endpoint": endpoint, "control_epoch": body["epoch"],
                    "effect_started": True, "phase": "INTENT", "before": body}
            self.store.update(lambda state: state["operations"][op_id].setdefault("substeps", {}).__setitem__(name, step))
            result = read(control, step["control_epoch"], endpoint)
            if not 200 <= result.get("http_status", 0) < 300:
                raise Uncertain("MACRO_EFFECT_UNCONFIRMED")
            body = self._body(read, component + ".status")
            if not satisfied_by(body):
                raise Uncertain("MACRO_EFFECT_UNCONFIRMED")
        self.store.update(lambda state: state["operations"][op_id].setdefault("substeps", {}).__setitem__(name,
            {**(step or {}), "control": control, "endpoint": endpoint, "phase": "COMPLETE", "body": body}))
        return body

    def _macro_persist(self, request, desired):
        op_id = request["op_id"]
        before = self.backend.endpoint_state()
        require(all(before[key] != "target" or desired[key] == "target" for key in ("postgres", "redis")),
                "ENDPOINT_ROLLBACK_FORBIDDEN")
        if before != desired:
            step = self.store.read()["operations"][op_id].get("substeps", {}).get("persist")
            if step and step.get("effect_started"):
                raise Uncertain("MACRO_ENDPOINT_EFFECT_UNCONFIRMED")
            self.store.update(lambda state: state["operations"][op_id].setdefault("substeps", {}).__setitem__("persist",
                {"phase": "INTENT", "effect_started": True, "desired": desired}))
            self.backend.persist_endpoints(desired)
        require(self.backend.endpoint_state() == desired, "MACRO_ENDPOINT_EFFECT_UNCONFIRMED")
        self.store.update(lambda state: state["operations"][op_id].setdefault("substeps", {}).__setitem__("persist",
            {"phase": "COMPLETE", "desired": desired}))

    def _restore_endpoints(self, read):
        state = self.store.read()
        endpoints = self.backend.endpoint_state()
        require(state["redis"]["authority"] in ("AWS", "TENCENT") and
                endpoints["redis"] == ("target" if state["redis"]["authority"] == "TENCENT" else "source") and
                state["pg"]["authority"] in ("AWS", "TENCENT") and
                endpoints["postgres"] == ("target" if state["pg"]["authority"] == "TENCENT" else "source"),
                "MACRO_AUTHORITY_UNCONFIRMED")
        postgres = self._body(read, "postgres.status")
        redis = self._body(read, "redis.status")
        require(postgres.get("endpoint") == endpoints["postgres"] and
                redis.get("address") == (self.backend.redis_target_address() if endpoints["redis"] == "target"
                                         else "127.0.0.1:16379"), "MACRO_ENDPOINT_UNCONFIRMED")
        return endpoints, postgres, redis

    def _ingress_only_restore(self, read, runtime, restored):
        # A request may start after the idle sample but before ingress.pause.
        # Never wait for that SSE/WS merely to undo an admission-only pause.
        # Existing requests remain safe only while every data component is
        # already open on the confirmed authority; this cannot resume a
        # partially completed data pause or an uncertain endpoint switch.
        require(all(integer(runtime.get(key)) for key in ("active_requests", "active_hijacked_connections")),
                "MACRO_RUNTIME_NOT_IDLE")
        readiness = runtime.get("readiness")
        require(type(readiness) is dict and
                {"postgres", "redis", "writer_coverage", "quiet_controls"} <= readiness.keys() and
                all(value is True for value in readiness.values()), "MACRO_RUNTIME_NOT_IDLE")
        _endpoints, postgres, redis = restored
        ingress = self._body(read, "ingress.status")
        writers = self._body(read, "writers.status")
        embedded = runtime.get("ingress")
        require(type(embedded) is dict and embedded.get("paused") is True and
                ingress.get("paused") is True and integer(ingress.get("epoch")) and
                integer(embedded.get("epoch")) and embedded["epoch"] == ingress["epoch"] and
                writers.get("held") is False and
                postgres.get("paused") is False and redis.get("paused") is False and
                redis.get("pubsub_cache_ready") is True, "MACRO_RUNTIME_NOT_IDLE")

    def _macro(self, request):
        action, op_id = request["action"], request["op_id"]
        read = self.backend.app_session()
        runtime = self._body(read, "runtime.status")
        entry = self.store.read()["operations"][op_id]
        require(not entry.get("cancelled"), "QUIESCE_CANCELLED")
        require(isinstance(runtime.get("instance_id"), str) and runtime["instance_id"] and
                runtime.get("serving") is True and runtime.get("draining") is False and
                runtime.get("background_workers_enabled") is False, "MACRO_RUNTIME_NOT_IDLE")
        # Preflight actual and persistent endpoints before cancellation or any
        # resume, even when idle. A paused component may be resumed only on its
        # confirmed authority; final validation would be too late.
        restored = self._restore_endpoints(read) if action == "app.restore" else None
        idle = all(type(runtime.get(key)) is int and runtime[key] == 0 for key in
                   ("active_requests", "active_hijacked_connections"))
        if not (idle or (action == "app.restore" and runtime.get("ready") is True)):
            require(action == "app.restore", "MACRO_RUNTIME_NOT_IDLE")
            self._ingress_only_restore(read, runtime, restored)
        if entry.get("instance_id"):
            require(entry["instance_id"] == runtime["instance_id"], "APP_INSTANCE_CHANGED")
        else:
            if action == "app.quiesce":
                require(runtime.get("ready") is True, "MACRO_NOT_READY")
            previous = [op for op in self.store.read()["operations"].values() if op["request"]["action"] == "app.quiesce"]
            if previous and action != "app.quiesce":
                latest = max(previous, key=lambda op: op["request"]["expected_epoch"])
                if latest.get("instance_id") not in (None, runtime["instance_id"]):
                    adoption = getattr(self.backend, "app_restore_adoption", None)
                    require(action == "app.restore" and callable(adoption), "APP_INSTANCE_CHANGED")
                    require(adoption(request, runtime, latest, self.store.read()) is True, "APP_INSTANCE_CHANGED")
            self.store.update(lambda state: state["operations"][op_id].update(instance_id=runtime["instance_id"]))
        if action == "app.quiesce":
            for component in ("ingress", "writers", "redis", "postgres"):
                self._macro_step(request, component, component + ".pause", read)
            started = self.now()
            snapshot = self._snapshot_result(self.backend.snapshot_with(read), started)
            require(snapshot["app"]["body"].get("instance_id") == runtime["instance_id"], "APP_INSTANCE_CHANGED")
            fresh = snapshot["app"]["body"]
            tasks, ready, embedded = fresh.get("tasks"), fresh.get("readiness"), fresh.get("ingress")
            require(fresh.get("serving") is True and fresh.get("draining") is False and
                    fresh.get("background_workers_enabled") is False and
                    all(type(fresh.get(k)) is int and fresh[k] == 0 for k in ("active_requests", "active_hijacked_connections")) and
                    type(embedded) is dict and embedded.get("paused") is True and embedded.get("quiet") is True and
                    integer(snapshot["ingress"].get("epoch")) and embedded.get("epoch") == snapshot["ingress"]["epoch"] and
                    type(tasks) is dict and REQUIRED_WRITERS <= tasks.keys() and
                    all(type(row) is dict and all(type(row.get(k)) is int and row[k] == 0 for k in
                        ("active", "pending", "failed", "dropped")) for row in tasks.values()) and
                    type(ready) is dict and ready.get("writer_coverage") is True and ready.get("quiet_controls") is True and
                    snapshot["ingress"].get("paused") is True and snapshot["ingress"].get("quiet") is True and
                    snapshot["writers"].get("held") is True and snapshot["writers"].get("quiet") is True and
                    snapshot["redis"].get("paused") is True and snapshot["redis"].get("quiet") is True and
                    all(type(snapshot[key].get("active_operations")) is int and snapshot[key]["active_operations"] == 0
                        for key in ("redis", "postgres")) and
                    snapshot["postgres"].get("paused") is True, "MACRO_NOT_QUIET")
            return snapshot
        if action == "pg.route":
            require(self.store.read()["pg"]["authority"] == "TENCENT", "PG_TARGET_NOT_CONFIRMED")
            with self.backend.pg_locks():
                self._pg_validate(self.backend.pg_sample(), primary=True)
            endpoints = self.backend.endpoint_state()
            self._macro_persist(request, {**endpoints, "postgres": "target"})
            body = self._macro_step(request, "switch", "postgres.switch", read, "target")
            return {"authority": "TENCENT", "body": body, "instance_id": runtime["instance_id"]}
        if action == "redis.preroute":
            step = entry.get("substeps", {}).get("switch")
            if not step or not step.get("effect_started"):
                self._redis_preroute(self._body(read, "redis.status"), self.backend.endpoint_state(), read)
            body = self._macro_step(request, "switch", "redis.switch", read, "target")
            return {"body": body, "instance_id": runtime["instance_id"]}
        require(action == "app.restore", "UNKNOWN_ACTION")
        endpoints = restored[0]
        self.store.update(lambda ledger: [op.update(cancelled=True) for key, op in ledger["operations"].items()
            if key != op_id and op["request"]["action"] == "app.quiesce"])
        for control in ("postgres.resume", "redis.rebuild", "redis.resume", "writers.resume", "ingress.resume"):
            # A never-paused/partially paused abort may already have open Redis.
            if control == "redis.rebuild" and not self._body(read, "redis.status").get("paused"):
                continue
            self._macro_step(request, control, control, read)
        final = {"app": {"http_status": 200, "body": self._body(read, "runtime.status")},
                 **{key: self._body(read, key + ".status") for key in ("ingress", "writers", "redis", "postgres")}}
        require(final["app"]["body"].get("instance_id") == runtime["instance_id"] and final["app"]["body"].get("ready") is True and
                final["ingress"].get("paused") is False and final["writers"].get("held") is False and
                final["redis"].get("paused") is False and final["redis"].get("pubsub_cache_ready") is True and
                final["redis"].get("address") == (self.backend.redis_target_address() if endpoints["redis"] == "target" else "127.0.0.1:16379") and
                final["postgres"].get("paused") is False and final["postgres"].get("endpoint") == endpoints["postgres"],
                "MACRO_RESTORE_UNCONFIRMED")
        return final

    def perform(self, request):
        action = request["action"]
        if action in ("app.quiesce", "app.restore", "pg.route", "redis.preroute"):
            try:
                return self._macro(request)
            except Refusal as error:
                if error.code == "QUIESCE_CANCELLED":
                    raise
                raise Uncertain(error.code) from None
        if action == "app.quiet-snapshot":
            started = self.now()
            return self._snapshot_result(self.backend.app_quiet_snapshot(), started)
        if action == "status":
            with self.backend.pg_locks():
                manifest, pg = self.backend.pg_manifest(), self.backend.pg_sample()
                state = self.store.read()
            return {"pg_authority": state["pg"]["authority"], "pg_mode": manifest["authority_mode"],
                    "pg_arm": {key: state["pg"]["arm"][key] for key in
                               ("arm_id", "status", "expires_at", "op_id", "final_lsn")} if state["pg"].get("arm") else None,
                    "pg_promotion_intent": {key: state["pg"]["promotion_intent"][key] for key in
                                            ("op_id", "accepted_at")} if state["pg"].get("promotion_intent") else None,
                    "pg": {key: pg.get(key) for key in ("system_identifier", "in_recovery", "replay_lsn", "wal_timeline")},
                    "redis_authority": state["redis"]["authority"], "redis": self.backend.redis_inspect(),
                    "persistent_endpoints": self.backend.endpoint_state(),
                    "app": self.backend.app_control("runtime.status"),
                    "operations": {key: {"phase": value["phase"], "action": value["request"]["action"]}
                                   for key, value in list(state["operations"].items())[-16:]}}
        if action == "app.control":
            return self._app_control(request)
        if action == "app.persist-endpoints":
            state = self.store.read()
            require(request["postgres"] != "target" or state["pg"]["authority"] == "TENCENT", "PG_TARGET_NOT_CONFIRMED")
            require(request["redis"] != "target" or state["redis"]["authority"] == "TENCENT", "REDIS_TARGET_NOT_CONFIRMED")
            before = self.backend.endpoint_state()
            require(all(before[key] != "target" or request[key] == "target" for key in ("postgres", "redis")),
                    "ENDPOINT_ROLLBACK_FORBIDDEN")
            self.store.update(lambda current: current["operations"][request["op_id"]].update(effect_started=True))
            return self.backend.persist_endpoints({"v": 1, "postgres": request["postgres"], "redis": request["redis"]})
        if action in ("pg.arm", "pg.arm-quiet"):
            started = self.now()
            with self.backend.pg_locks():
                manifest = self.backend.pg_manifest()
                require(manifest["authority_mode"] == "STANDBY" and not manifest.get("promotion_intent"), "PG_ALREADY_ACCEPTED")
                self._pg_validate(self.backend.pg_sample(), request["final_lsn"])
                def arm(state):
                    old = state["pg"].get("arm")
                    if old and old.get("op_id") == request["op_id"]:
                        return old
                    require(not state["pg"].get("promotion_intent") and
                            (not old or old["status"] == "CANCELLED"), "PG_ARM_ALREADY_EXISTS")
                    value = {"op_id": request["op_id"], "arm_id": secrets.token_hex(16), "status": "ARMED", "expires_at": self.now() + ARM_SECONDS,
                             "final_lsn": request["final_lsn"], "quiet_attestation_sha256": request["quiet_attestation_sha256"]}
                    state["pg"]["arm"] = value
                    return value
                armed = self.store.update(arm)
            if action == "pg.arm":
                return armed
            # The durable arm precedes the fresh sample. Snapshot collection
            # acquires pg_locks itself and must run outside the arm's lock.
            return {**armed, "quiet_nonce": request["quiet_nonce"],
                    "quiet_snapshot": self._snapshot_result(self.backend.app_quiet_snapshot(), started)}
        if action == "pg.cancel-arm":
            with self.backend.pg_locks():
                manifest = self.backend.pg_manifest()
                self._pg_validate(self.backend.pg_sample())
                require(manifest["authority_mode"] == "STANDBY" and not manifest.get("promotion_intent"), "PG_ALREADY_ACCEPTED")
                def cancel(state):
                    arm = state["pg"].get("arm")
                    require(arm and arm["arm_id"] == request["arm_id"] and not arm.get("commit_op") and
                            not state["pg"].get("promotion_intent"), "PG_ARM_NOT_CANCELLABLE")
                    arm["status"] = "CANCELLED"
                    return {"cancelled": True, "arm_id": arm["arm_id"], "authority": "AWS"}
                return self.store.update(cancel)
        if action == "pg.commit":
            if not self.store.read()["pg"].get("promotion_intent"):
                with self.backend.pg_locks():
                    manifest = self.backend.pg_manifest()
                    require(manifest["authority_mode"] == "STANDBY" and not manifest.get("promotion_intent"), "PG_ALREADY_ACCEPTED")
                    arm = self.store.read()["pg"].get("arm")
                    require(arm and arm["arm_id"] == request["arm_id"] and arm["status"] == "ARMED" and
                            self.now() < arm["expires_at"], "PG_ARM_EXPIRED_OR_CANCELLED")
                    self._pg_validate(self.backend.pg_sample(), arm["final_lsn"])
                    intent = {"op_id": request["op_id"], "source_fence_sha256": request["source_fence_sha256"],
                              "final_lsn": arm["final_lsn"], "system_identifier": self.config["pg"]["system_identifier"],
                              "source_timeline": self.config["pg"]["source_timeline"], "accepted_at": self.now()}
                    def accept(state):
                        current = state["pg"].get("arm")
                        require(current == arm and not state["pg"].get("promotion_intent"), "PG_ARM_CHANGED")
                        current["commit_op"] = request["op_id"]
                        state["pg"].update(authority="COMMIT_INTENT", promotion_intent=intent)
                    self.store.update(accept)
                    manifest.update(authority_mode="PROMOTION_INTENT", promotion_intent=intent)
                    self.backend.pg_save_manifest(manifest)
            else:
                require(self.store.read()["pg"]["promotion_intent"]["op_id"] == request["op_id"], "PG_COMMIT_ALREADY_ACCEPTED")
            return self._pg_reconcile()
        if action == "pg.reconcile":
            return self._pg_reconcile()
        if action == "redis.inspect":
            return self.backend.redis_inspect()
        if action == "redis.persist-primary":
            observed = self.backend.redis_inspect()
            require(observed["role"] == "master", "REDIS_NOT_PRIMARY")
            def accept_redis(state):
                accepted = state["redis"]
                if accepted["authority"] != "AWS":
                    require(accepted.get("run_id") == observed["run_id"] and
                            accepted.get("op_id") == request["op_id"], "REDIS_ACCEPTED_IDENTITY_CHANGED")
                accepted.update(authority="COMMIT_INTENT", op_id=request["op_id"], run_id=observed["run_id"])
            self.store.update(accept_redis)
            result = self.backend.redis_persist_primary()
            self.store.update(lambda state: state["redis"].update(authority="TENCENT"))
            return {**result, "authority": "TENCENT"}
        raise Refusal("UNKNOWN_ACTION")

    def guard_reconcile(self):
        state = self.store.read()
        pending = [value["request"] for value in state["operations"].values() if value["phase"] != "COMPLETE" and
                   (value["request"]["action"] in ("pg.commit", "pg.reconcile", "redis.persist-primary", "app.persist-endpoints",
                                                  "app.quiesce", "app.restore", "pg.route", "redis.preroute") or
                    (value["request"]["action"] == "app.control" and value.get("effect_started")))]
        results = [self.handle(request) for request in pending]
        # The accepted authority intent outlives any arm/transport/ledger result.
        if self.store.read()["pg"].get("promotion_intent"):
            try:
                with self.store.mutation_lock():
                    results.append({"pg_reconcile": self._pg_reconcile()})
            except Refusal as error:
                results.append({"pg_reconcile": "UNKNOWN", "reason": error.code})
        return {"guard": True, "results": results}


def main(argv=None, stdin=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(json.dumps({"offline": True, "local_operations": 0, "run": RUN,
                          "actions": sorted(ACTION_FIELDS), "requires_reviewed_root_install": True}))
        return 0
    require(argv in (["serve"], ["guard-reconcile"]), "INVALID_MODE")
    config = load_config()
    rpc = TargetRPC(config, LocalBackend(config), FileStore(config))
    result = rpc.guard_reconcile() if argv == ["guard-reconcile"] else rpc.handle(read_request(stdin or sys.stdin.buffer))
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as error:
        print(json.dumps({"ok": False, "code": error.code}))
        raise SystemExit(1)
    except Exception:
        print(json.dumps({"ok": False, "code": "LOCAL_STATE_UNCERTAIN", "reconcile_required": True}))
        raise SystemExit(1)
