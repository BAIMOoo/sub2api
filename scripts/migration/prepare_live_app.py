#!/usr/bin/env python3
"""Create only the fixed inert Tencent application container. Default is offline."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import subprocess
import sys

RUN = "20261016"
ROOT = pathlib.Path("/var/lib/sub2api-migration-20261016")
SCOPE = ROOT / "app-prepare"
APP = ROOT / "app"
STAGED = pathlib.Path("/home/ubuntu/sub2api-migration-20261016")
RUNTIME = STAGED / "app-runtime/source-runtime.json"
MANIFEST = STAGED / "initial-api-files-verification.json"
SOURCE_IMAGE = "sha256:7f83a7aa3937e47473df037a341e48104318f6ad7fa6c5de00504a12cbe87742"
RUNTIME_SHA256 = "5570f29f0954706128798f2d4575be4302a0508739d2e21c39e364c4d1dc16fd"
IMAGE = "sub2api:migration-compat-20261016-0bdcaa371aa4"
IMAGE_ID = "sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a"
BINARY_SHA256 = "0bdcaa371aa45abfa1eb61c763d2da10f133d718c4ad93251587ca0cc6191724"
BASELINE = "c69fdbeb0fa26543cc5bf115d2e5048426041e84"
CONTAINER = "sub2api-migration-app-20261016"
LABEL = "io.sub2api.migration.owner"
FILES = ("config.yaml", ".installed", "model_pricing.json", "model_pricing.sha256")
MIB = 1024**2
HEX64 = r"[0-9a-f]{64}"


class Refusal(RuntimeError):
    pass


class Unknown(Refusal):
    pass


def require(condition, code):
    if not condition:
        raise Refusal(code)


def matches(value, expression):
    return isinstance(value, str) and re.fullmatch(expression, value) is not None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def strict_json(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= 2*MIB, "INPUT_SIZE")
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "DUPLICATE_KEY")
            value[key] = item
        return value
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(Refusal("NONFINITE_JSON")))
        canonical(result)
        return result
    except Refusal:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise Refusal("INVALID_JSON") from None


def directory(path, *, uid=0, private=False):
    path = pathlib.Path(path)
    for item in reversed([path, *path.parents]):
        info = item.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, uid) and not info.st_mode & 0o022,
                "UNTRUSTED_DIRECTORY")
    if private:
        require(path.stat().st_uid == uid and stat.S_IMODE(path.stat().st_mode) == 0o700, "PRIVATE_DIRECTORY_REQUIRED")
    return path


def read_bytes(path, *, uid=0, mode=0o600):
    path = pathlib.Path(path)
    directory(path.parent, uid=uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1 and
                stat.S_IMODE(info.st_mode) == mode and info.st_size <= 2*MIB, "UNTRUSTED_FILE")
        raw = os.read(fd, 2*MIB+1)
        require(len(raw) <= 2*MIB, "INPUT_SIZE")
        return raw
    finally:
        os.close(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, raw, mode=0o600, *, uid=0, gid=0):
    path = pathlib.Path(path)
    if path.exists() or path.is_symlink():
        require(read_bytes(path, uid=uid, mode=mode) == raw, "EXISTING_FILE_DIFFERS")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as out:
        os.fchmod(out.fileno(), mode)
        os.fchown(out.fileno(), uid, gid)
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    sync_directory(path.parent)


def checkpoint(path, value):
    path = pathlib.Path(path)
    directory(path.parent, private=True)
    if path.exists() or path.is_symlink():
        read_bytes(path)
    pending = path.parent / (".checkpoint-" + secrets.token_hex(16))
    try:
        write_new(pending, canonical(value) + b"\n")
        os.replace(pending, path)
        sync_directory(path.parent)
    finally:
        if pending.exists():
            pending.unlink()


def validate_config(value):
    require(type(value) is dict and set(value) == {"v", "run", "owner", "machine_id_sha256",
            "source_runtime_sha256", "initial_manifest_sha256"}, "CONFIG_FIELDS")
    require(type(value["v"]) is int and value["v"] == 1 and value["run"] == RUN and
            matches(value["owner"], r"[0-9a-f]{32}"), "CONFIG_IDENTITY")
    require(all(matches(value[key], HEX64) for key in ("machine_id_sha256", "source_runtime_sha256", "initial_manifest_sha256")),
            "CONFIG_DIGEST")
    require(value["source_runtime_sha256"] == RUNTIME_SHA256, "UNREVIEWED_SOURCE_RUNTIME")
    return value


def env_text(values):
    result = []
    for key, value in sorted(values.items()):
        require(matches(key, r"[A-Za-z_][A-Za-z0-9_]*") and isinstance(value, str) and
                not any(char in value for char in "\r\n\x00"), "INVALID_ENVIRONMENT")
        result.append(key + "=" + value)
    return ("\n".join(result) + "\n").encode()


def environment(source, token):
    require(type(source) is list and all(isinstance(row, str) and "=" in row for row in source), "SOURCE_ENVIRONMENT")
    result = dict(row.split("=", 1) for row in source)
    require(len(result) == len(source), "DUPLICATE_SOURCE_ENVIRONMENT")
    require(result.get("DATABASE_USER") == result.get("DATABASE_DBNAME") == "sub2api" and
            bool(result.get("DATABASE_PASSWORD")), "SOURCE_DATABASE_IDENTITY")
    require(matches(token, r"[A-Za-z0-9_-]{32,256}"), "INVALID_CONTROL_TOKEN")
    # Business credentials/options remain byte-for-byte. Stale migration switches
    # are not business configuration and may not override this reviewed mode.
    result = {key: value for key, value in result.items() if not key.startswith("MIGRATION_")}
    escaped = result["DATABASE_PASSWORD"].replace("\\", "\\\\").replace("'", "\\'")
    result.update({"SERVER_HOST": "127.0.0.1", "SERVER_PORT": "8082", "AUTO_SETUP": "false",
        "DATABASE_HOST": "127.0.0.1", "DATABASE_PORT": "15432", "DATABASE_SSLMODE": "disable",
        "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "16379", "REDIS_USERNAME": "", "REDIS_PASSWORD": "",
        "REDIS_ENABLE_TLS": "false", "GOMEMLIMIT": "400MiB",
        "MIGRATION_HANDOVER_MODE": "quiet", "MIGRATION_BACKGROUND_WORKERS": "false", "MIGRATION_SHARED_REDIS": "true",
        "MIGRATION_POSTGRES_ENABLED": "true", "MIGRATION_POSTGRES_TARGET_DSN":
            "host=127.0.0.1 port=25432 user=sub2api dbname=sub2api password='" + escaped + "' sslmode=disable connect_timeout=1",
        "MIGRATION_REDIS_ENABLED": "true", "MIGRATION_REDIS_TARGET_ADDR": "127.0.0.1:26379",
        "MIGRATION_RUNTIME_ADDR": "127.0.0.1:18092", "MIGRATION_RUNTIME_TOKEN": token,
        "MIGRATION_ENDPOINT_STATE_FILE": "/migration-state/endpoints.json", "MIGRATION_USAGE_ENABLED": "false"})
    env_text(result)
    return result


def verify_runtime(value):
    require(type(value) is dict and set(value) == {"container_id", "image_id", "environment", "command", "entrypoint"}, "RUNTIME_FIELDS")
    require(matches(value["container_id"], HEX64) and value["image_id"] == SOURCE_IMAGE and
            value["entrypoint"] == ["/app/docker-entrypoint.sh"] and value["command"] == ["/app/sub2api"], "SOURCE_RUNTIME_IDENTITY")
    return value


def create_argv(owner):
    require(matches(owner, r"[0-9a-f]{32}"), "INVALID_OWNER")
    return ["/usr/bin/docker", "create", "--pull", "never", "--name", CONTAINER, "--label", LABEL + "=" + owner,
            "--label", "io.sub2api.migration.run=sub2api-live-app-20261016", "--network", "host", "--restart", "no",
            "--memory", str(512*MIB), "--memory-swap", str(512*MIB), "--cpus", "1", "--pids-limit", "256",
            "--security-opt", "no-new-privileges:true", "--cap-drop", "NET_RAW", "--log-opt", "max-size=8m",
            "--log-opt", "max-file=2", "--env-file", str(SCOPE / "app.env"),
            "--mount", "type=bind,src=" + str(APP / "data") + ",dst=/app/data",
            "--mount", "type=bind,src=" + str(APP) + ",dst=/migration-state,readonly", IMAGE_ID]


class Docker:
    def command(self, argv):
        try:
            result = subprocess.run(argv, capture_output=True, timeout=20,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise Unknown("DOCKER_OUTCOME_UNKNOWN") from None
        if result.returncode:
            raise Unknown("DOCKER_OUTCOME_UNKNOWN")
        require(len(result.stdout) <= 2*MIB, "DOCKER_OUTPUT_LIMIT")
        return result.stdout

    def inspect(self, kind, name, missing=False):
        try:
            value = strict_json(self.command(["/usr/bin/docker", kind, "inspect", name]))
        except Unknown:
            # An inspect failure does not prove absence. Only a successful
            # complete fixed-name listing proves that create is safe to attempt.
            if not missing:
                raise
            names = self.command(["/usr/bin/docker", "container", "ls", "-a", "--format", "{{.Names}}"])
            require(CONTAINER.encode() not in names.splitlines(), "CONTAINER_EXISTENCE_UNKNOWN")
            return None
        require(type(value) is list and len(value) == 1 and type(value[0]) is dict, "DOCKER_INSPECTION")
        return value[0]


def verify_image(image):
    require(image["Id"] == IMAGE_ID and image["Architecture"] == "amd64" and image["Os"] == "linux", "IMAGE_IDENTITY")
    labels = image["Config"].get("Labels") or {}
    require(labels.get("io.sub2api.migration.binary") == BINARY_SHA256 and labels.get("io.sub2api.migration.baseline") == BASELINE,
            "IMAGE_PROVENANCE")
    require(image["Config"].get("Entrypoint") == ["/app/docker-entrypoint.sh"] and
            image["Config"].get("Cmd") == ["/app/sub2api"], "IMAGE_ENTRYPOINT")
    require(type(image["RootFS"]["Layers"]) is list and len(image["RootFS"]["Layers"]) > 0 and
            all(matches(row, "sha256:" + HEX64) for row in image["RootFS"]["Layers"]), "IMAGE_ROOTFS")


def verify_created(actual, cfg, expected_env):
    require(matches(actual["Id"], HEX64) and actual["Image"] == IMAGE_ID, "CONTAINER_IMAGE_CHANGED")
    labels = actual["Config"].get("Labels") or {}
    require(labels.get(LABEL) == cfg["owner"] and labels.get("io.sub2api.migration.run") == "sub2api-live-app-20261016", "CONTAINER_OWNER_CHANGED")
    require(actual["State"]["Status"] == "created" and actual["State"]["Running"] is False and actual["State"]["Pid"] == 0,
            "CONTAINER_NOT_INERT")
    host = actual["HostConfig"]
    require(host["NetworkMode"] == "host" and not host.get("PortBindings") and host["RestartPolicy"]["Name"] == "no" and
            host["Memory"] == host["MemorySwap"] == 512*MIB and host["NanoCpus"] == 1000000000 and host["PidsLimit"] == 256,
            "CONTAINER_HOST_CONFIG_CHANGED")
    require(actual["Config"]["Entrypoint"] == ["/app/docker-entrypoint.sh"] and actual["Config"]["Cmd"] == ["/app/sub2api"],
            "CONTAINER_COMMAND_CHANGED")
    rows = actual["Config"]["Env"]
    require(type(rows) is list and all(isinstance(row, str) and "=" in row for row in rows), "CONTAINER_ENVIRONMENT")
    observed = dict(row.split("=", 1) for row in rows)
    require(len(rows) == len(observed) and observed == expected_env, "CONTAINER_ENVIRONMENT_CHANGED")
    mounts = actual["Mounts"]
    require(len(mounts) == 2, "UNEXPECTED_CONTAINER_MOUNT")
    for destination, source, writable in [("/app/data", APP / "data", True), ("/migration-state", APP, False)]:
        match = [row for row in mounts if row["Destination"] == destination]
        require(len(match) == 1 and match[0]["Type"] == "bind" and match[0]["Source"] == str(source) and match[0]["RW"] is writable,
                "CONTAINER_MOUNT_CHANGED")


def load_inputs():
    require(os.geteuid() == 0, "ROOT_REQUIRED")
    directory(SCOPE, private=True)
    cfg = validate_config(strict_json(read_bytes(SCOPE / "config.json")))
    machine = pathlib.Path("/etc/machine-id")
    directory(machine.parent)
    info = machine.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            "MACHINE_ID_UNTRUSTED")
    require(sha(machine.read_bytes().strip()) == cfg["machine_id_sha256"], "WRONG_MACHINE")
    raw = read_bytes(RUNTIME, uid=1000)
    require(sha(raw) == cfg["source_runtime_sha256"], "RUNTIME_SNAPSHOT_CHANGED")
    runtime = verify_runtime(strict_json(raw))
    raw = read_bytes(MANIFEST, uid=1000)
    require(sha(raw) == cfg["initial_manifest_sha256"], "INITIAL_MANIFEST_CHANGED")
    manifest = strict_json(raw)
    payload = {}
    for name in FILES:
        raw = read_bytes(STAGED / "app-initial" / name, uid=1000)
        require(len(raw) <= MIB and manifest["app-initial/" + name]["sha256"] == sha(raw) and
                manifest["app-initial/" + name]["size"] == len(raw), "INITIAL_FILE_CHANGED")
        payload[name] = raw
    return cfg, runtime, payload


def app_directory():
    if not APP.exists():
        APP.mkdir(mode=0o755)
    directory(APP)
    require(APP.stat().st_uid == 0 and stat.S_IMODE(APP.stat().st_mode) == 0o755, "APP_DIRECTORY_MODE")


def prepare(cfg, runtime, payload, docker):
    lockfd = os.open(SCOPE / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        lock = os.fstat(lockfd)
        require(stat.S_ISREG(lock.st_mode) and lock.st_uid == 0 and lock.st_nlink == 1 and
                stat.S_IMODE(lock.st_mode) == 0o600, "UNTRUSTED_LOCK")
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Unknown("PREPARATION_BUSY") from None
        return _prepare_locked(cfg, runtime, payload, docker)
    finally:
        os.close(lockfd)


def _prepare_locked(cfg, runtime, payload, docker):
    image = docker.inspect("image", IMAGE_ID)
    verify_image(image)
    state_path = SCOPE / "state.json"
    state = strict_json(read_bytes(state_path)) if state_path.exists() or state_path.is_symlink() else None
    existing = docker.inspect("container", CONTAINER, missing=True)
    if state is None:
        require(existing is None, "UNOWNED_CONTAINER_EXISTS")
        state = {"v": 1, "run": RUN, "owner": cfg["owner"], "phase": "PREPARING", "image_id": IMAGE_ID,
                 "source_runtime_sha256": cfg["source_runtime_sha256"], "initial_manifest_sha256": cfg["initial_manifest_sha256"],
                 "app_files_sha256": {name: sha(raw) for name, raw in payload.items()}}
        checkpoint(state_path, state)
    require(state["run"] == RUN and state["owner"] == cfg["owner"] and state["image_id"] == IMAGE_ID and
            state["source_runtime_sha256"] == cfg["source_runtime_sha256"] and
            state["initial_manifest_sha256"] == cfg["initial_manifest_sha256"] and
            state["app_files_sha256"] == {name: sha(raw) for name, raw in payload.items()}, "PREPARATION_IDENTITY_CHANGED")
    require(not state.get("container_id") or (existing is not None and existing["Id"] == state["container_id"]),
            "PREPARED_CONTAINER_CHANGED")
    app_directory()
    endpoints = APP / "endpoints.json"
    initial = {"v": 1, "postgres": "source", "redis": "source"}
    write_new(endpoints, canonical(initial) + b"\n", 0o644)
    data = APP / "data"
    if not data.exists():
        data.mkdir(mode=0o700)
        os.chown(data, 1000, 1000)
        sync_directory(APP)
    directory(data, uid=1000, private=True)
    require(set(row.name for row in data.iterdir()) <= set(FILES), "APP_DATA_NOT_INITIAL")
    for name, raw in payload.items():
        write_new(data / name, raw, uid=1000, gid=1000)
    token_path = SCOPE / "app-control.token"
    if token_path.exists() or token_path.is_symlink():
        token = read_bytes(token_path).strip().decode("ascii")
    else:
        token = secrets.token_urlsafe(48)
        write_new(token_path, token.encode() + b"\n")
    desired = environment(runtime["environment"], token)
    write_new(SCOPE / "app.env", env_text(desired))
    image_env = dict(row.split("=", 1) for row in image["Config"].get("Env", []))
    expected_env = {**image_env, **desired}
    state.update(phase="CREATING", env_sha256=sha(env_text(desired)))
    checkpoint(state_path, state)
    if existing is None:
        result = docker.command(create_argv(cfg["owner"]))
        require(matches(result.decode("ascii").strip(), HEX64), "CREATE_REPLY_INVALID")
        existing = docker.inspect("container", CONTAINER)
    verify_created(existing, cfg, expected_env)
    metadata = {"v": 1, "run": RUN, "owner": cfg["owner"], "container": CONTAINER,
                "container_id": existing["Id"], "image": IMAGE, "image_id": IMAGE_ID,
                "binary_sha256": BINARY_SHA256, "config_sha256": sha(existing["Config"]),
                "rootfs_diffids": image["RootFS"]["Layers"], "control_port": 18092,
                "token_path": str(token_path), "env_sha256": sha(env_text(desired)),
                "app_files_sha256": state["app_files_sha256"], "created_inert": True,
                "application_started": False, "source_modified": False}
    checkpoint(SCOPE / "adoption.json", metadata)
    state.update(phase="CREATED_INERT", container_id=existing["Id"])
    checkpoint(state_path, state)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-inert", action="store_true")
    args = parser.parse_args(argv)
    if not args.create_inert:
        print(json.dumps({"offline": True, "local_operations": 0, "creates_only_inert_container": True,
                          "container": CONTAINER, "image_id": IMAGE_ID, "starts_application": False,
                          "source_modified": False}))
        return 0
    cfg, runtime, payload = load_inputs()
    print(json.dumps(prepare(cfg, runtime, payload, Docker()), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as error:
        print(json.dumps({"ok": False, "code": str(error), "application_started": False}), file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print(json.dumps({"ok": False, "code": "LOCAL_STATE_UNCERTAIN", "inspect_retained_resources": True}), file=sys.stderr)
        raise SystemExit(1)
