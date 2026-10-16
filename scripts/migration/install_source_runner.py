#!/usr/bin/env python3
"""Install the fixed AWS controller, inactive main service, and active guard.

Root must first import these reviewed programs by SHA into LIBEXEC. The only
production changes here are new root-owned files/units and the protective guard;
no app/data process, business route, DNS record or container policy is changed.
"""
import base64
import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import time

LIBEXEC = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016")
SCOPE = pathlib.Path("/var/lib/sub2api-migration-20261016/runner")
INPUT = pathlib.Path("/home/ubuntu/.local/share/sub2api-migration-20261016")
PROGRAMS = {"install_source_runner.py", "durable_runner.py", "runner_backend.py", "runner_guard.py",
            "runner_service.py", "source_operations.py", "target_rpc.py"}
EXPECTED = {"app": "cba57e76f48d0e4cfb0280bcc963205dc910a0d0b4bd752e555bb2fa44b9f55e",
            "postgres": "c99ec20ad4468b29698083cdc6827c853141717f0cf0a7a98bfed32e788414ee",
            "redis": "e2f41a97ee0a36a9a5bd3e36b8f65874411419b3dae3867097323b3e730c6835"}
PUBLIC_WIRE_SHA = "4dc1f33491f9f3f5b96ec178ccf5c02ac275b1e12c104fa69d7b59fc56e1e76b"


def need(value, code):
    if not value:
        raise RuntimeError(code)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def verify_replica_seed(raw, expected_sha, pg_data_host_path):
    import re
    need(sha(raw) == expected_sha, "REPLICA_STATE_CHANGED")
    value = json.loads(raw)
    need(type(value) is dict and value.get("run") == "sub2api-pg-replica-20261016" and
         value.get("side") == "source" and re.fullmatch(r"[0-9a-f]{32}", value.get("owner", "")) and
         value.get("container_id") == EXPECTED["postgres"] and
         value.get("system_identifier") == "7626048206782038050" and value.get("timeline") == 1 and
         value.get("slot_create_intended") is True and value.get("data_host_path") == pg_data_host_path,
         "REPLICA_STATE_NOT_OWNED")
    return value


def load(name):
    path = LIBEXEC / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(argv, data=None):
    try:
        value = subprocess.run(argv, input=data, capture_output=True, timeout=8,
                               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("INSTALL_ACTION_OUTCOME_UNKNOWN") from None
    need(value.returncode == 0, "INSTALL_ACTION_FAILED")
    return value.stdout


def immutable_write(path, data, mode=0o600):
    path = pathlib.Path(path)
    need(not path.is_symlink(), "SYMLINK_REFUSED")
    if path.exists():
        info = path.stat()
        need(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == mode and path.read_bytes() == data, "EXISTING_INSTALL_DIFFERS")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def directory(path):
    for part in reversed([path, *path.parents]):
        need(not part.is_symlink(), "SYMLINK_PARENT_REFUSED")
        if not part.exists():
            part.mkdir(mode=0o700)
        info = part.stat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "UNTRUSTED_PARENT")
    need(stat.S_IMODE(path.stat().st_mode) == 0o700, "SCOPE_NOT_PRIVATE")


def install(request):
    need(os.geteuid() == 0 and sys.platform == "linux", "LINUX_ROOT_REQUIRED")
    need(type(request) is dict and set(request) == {"v", "run", "owner", "initial_target_epoch", "known_hosts_sha256", "source_replica_state_sha256", "programs"}, "INSTALL_FIELDS")
    import re
    need(request["v"] == 1 and request["run"] == "20261016" and
         re.fullmatch(r"[0-9a-f]{32}", request["owner"]) and type(request["initial_target_epoch"]) is int and
         request["initial_target_epoch"] >= 0 and all(re.fullmatch(r"[0-9a-f]{64}", request[key]) for key in
             ("known_hosts_sha256", "source_replica_state_sha256")) and
         type(request["programs"]) is dict and set(request["programs"]) == PROGRAMS, "INSTALL_IDENTITY")
    for name, digest in request["programs"].items():
        need(re.fullmatch(r"[0-9a-f]{64}", digest), "PROGRAM_DIGEST_INVALID")
        path = LIBEXEC / name
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == 0o644 and sha(path.read_bytes()) == digest, "ROOT_PROGRAM_UNTRUSTED")
    S, G = load("source_operations"), load("runner_guard")
    directory(SCOPE)
    # Import the private key on AWS only; no bytes leave this host. Derive its
    # public wire identity before allowing the controller to use it.
    for original, output, expected_sha in (("tunnel_ed25519", "target-rpc.key", None),
                                           ("known_hosts", "target-rpc.known_hosts", request["known_hosts_sha256"])):
        path = INPUT / original
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 1000 and info.st_nlink == 1 and not info.st_mode & 0o077,
             "CHANNEL_INPUT_UNTRUSTED")
        raw = path.read_bytes()
        need(expected_sha is None or sha(raw) == expected_sha, "KNOWN_HOSTS_CHANGED")
        immutable_write(SCOPE / output, raw)
    pub = run(["/usr/bin/ssh-keygen", "-y", "-f", str(SCOPE / "target-rpc.key")]).split()
    need(len(pub) >= 2 and pub[0] == b"ssh-ed25519" and
         sha(base64.b64decode(pub[1], validate=True)) == PUBLIC_WIRE_SHA, "PRIVATE_KEY_IDENTITY_CHANGED")
    containers = {}
    raw_inspects = {}
    for kind, name in S.NAMES.items():
        value = json.loads(run(["/usr/bin/docker", "container", "inspect", name]))[0]
        need(value["Id"] == EXPECTED[kind] and value["State"]["Running"] is True, "SOURCE_CONTAINER_CHANGED")
        image = json.loads(run(["/usr/bin/docker", "image", "inspect", value["Image"]]))[0]
        cfg = image.get("Id", "")
        need(cfg.startswith("sha256:") and len(cfg) == 71 and image["Os"] == "linux" and
             image["Architecture"] == "amd64", "SOURCE_IMAGE_CHANGED")
        containers[kind] = {"id": value["Id"], "config_sha256": S.digest(value["Config"]),
                            "image": {"config_digest": cfg, "diff_ids": image["RootFS"]["Layers"],
                                      "os": "linux", "architecture": "amd64"}}
        raw_inspects[kind] = value
    guard_unit = G.unit_text().encode()
    main_unit = G.main_unit_text().encode()
    units = {}
    for unit in S.PG_UNITS:
        path = pathlib.Path("/etc/systemd/system") / unit
        units[unit] = sha(S.private_bytes(path, mode=0o644))
    units[S.GUARD_UNIT] = sha(guard_unit)
    manifest = {"v": 1, "run": "20261016", "owner": request["owner"],
                "machine_id_sha256": sha(pathlib.Path("/etc/machine-id").read_bytes().strip()),
                "containers": containers,
                "postgres": {"system_identifier": "7626048206782038050", "timeline": 1,
                             "replication_user": "sub2api_mig_20261016_replica", "replication_application": "sub2api-pg-replica-20261016",
                             "replication_address": "172.18.0.1", "slot": "sub2api_mig_20261016"},
                "redis": {"source_run_id": "08e111c3724844873b08ee96322628d6a2724ee3",
                          "source_replid": "6fbca65ab347b9efa6637b4c790feaa93c29d348", "target_run_id": "39d29826637d6bcf89b9630612f7fd0107260394"},
                "units": units}
    S.validate_manifest(manifest)
    immutable_write(SCOPE / "source-manifest.json", canonical(manifest))
    config = {"v": 1, "run": "20261016", "owner": request["owner"], "machine_id_sha256": manifest["machine_id_sha256"],
              "initial_target_epoch": request["initial_target_epoch"], "ssh_key_sha256": sha((SCOPE / "target-rpc.key").read_bytes()),
              "known_hosts_sha256": request["known_hosts_sha256"]}
    immutable_write(SCOPE / "runner-config.json", canonical(config))
    data_mounts = [row for row in raw_inspects["postgres"]["Mounts"] if row["Destination"] == "/var/lib/postgresql/data"]
    need(len(data_mounts) == 1 and data_mounts[0]["RW"] is True, "PG_DATA_MOUNT_CHANGED")
    seed_path = INPUT / "postgres-replica/state.json"
    for part in [seed_path.parent, *seed_path.parent.parents]:
        info = part.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, 1000) and not info.st_mode & 0o022,
             "SEED_STATE_PARENT_UNTRUSTED")
    fd = os.open(seed_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == 0o600 and info.st_size < 1024 * 1024, "SEED_STATE_FILE_UNTRUSTED")
        seed_raw = os.read(fd, 1024 * 1024)
    finally:
        os.close(fd)
    seed = verify_replica_seed(seed_raw, request["source_replica_state_sha256"], data_mounts[0]["Source"])
    immutable_write(SCOPE / "original-replica-state.json", seed_raw)
    pg = S.SourceOperations()._pg_sample()
    need(pg["system_identifier"] == manifest["postgres"]["system_identifier"] and pg["timeline"] == 1, "SOURCE_PG_IDENTITY_CHANGED")
    query = b"SELECT coalesce(shobj_description(oid,'pg_authid'),'') FROM pg_roles WHERE rolname='sub2api_mig_20261016_replica';\n"
    comment = run(["/usr/bin/docker", "exec", "-i", EXPECTED["postgres"], "psql", "-XqAtw", "-U", "sub2api", "-d", "sub2api"], query).strip()
    need(comment == ("migration-owner:" + seed["owner"]).encode(), "REPLICATION_ROLE_OWNER_MISMATCH")
    guard_config = {"v": 1, "run": "20261016", "owner": request["owner"],
                    "source_manifest_sha256": sha(canonical(manifest)), "role_owner_comment_sha256": sha(comment),
                    "pg_data_host_path": data_mounts[0]["Source"], "slot_create_intended": seed["slot_create_intended"]}
    G.validate_config(guard_config)
    immutable_write(SCOPE / "guard-config.json", canonical(guard_config))
    state = {"v": 1, "run": "20261016", "owner": request["owner"], "pg": "AWS", "redis": "AWS",
             "phase": "ENTRY_PREPARATION", "attempt": 0, "pause_seconds": 0.0, "window": None, "route_fenced": False}
    immutable_write(SCOPE / "state.json", canonical(state))
    for unit, raw in ((S.GUARD_UNIT, guard_unit), (G.MAIN_UNIT, main_unit)):
        immutable_write(pathlib.Path("/etc/systemd/system") / unit, raw, 0o644)
    run(["/usr/bin/systemctl", "daemon-reload"])
    started = time.monotonic()
    probe = G.LocalBackend().sample()
    sample_seconds = time.monotonic() - started
    need(probe.get("pg_wal_protected") is True and sample_seconds < 1.0, "GUARD_PROBE_EXCEEDS_BUDGET")
    run(["/usr/bin/systemctl", "enable", "--now", S.GUARD_UNIT])
    # Deliberately leave the main inactive. It is gated by a separate, concrete
    # live acceptance record and explicit start after entry preparation.
    return {"installed": True, "owner": request["owner"], "guard_started": True,
            "main_started": False, "production_app_or_data_changed": False, "guard_probe_seconds": sample_seconds}


def main():
    if not sys.argv[1:]:
        print(json.dumps({"offline": True, "production_actions": False}))
        return
    need(sys.argv[1:] == ["install-inert"], "UNKNOWN_MODE")
    request = json.loads(sys.stdin.buffer.read(32769))
    print(json.dumps(install(request), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = str(error) if isinstance(error, RuntimeError) else "INSTALL_OUTCOME_UNCERTAIN"
        print(json.dumps({"ok": False, "code": code if code.isupper() else "INSTALL_OUTCOME_UNCERTAIN"}), file=sys.stderr)
        raise SystemExit(1)
