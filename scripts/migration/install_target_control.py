#!/usr/bin/env python3
"""Install only inert Tencent controls. Default is offline; no remote driver.

Bootstrap this exact reviewed file into the fixed root libexec path first.
--install-inert consumes one bounded approval-metadata JSON object from stdin.
"""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
import types

RUN = "20261016"
PREFIX = pathlib.Path("/var/lib/sub2api-migration-20261016")
SCOPE = PREFIX / "control-install"
RPC = PREFIX / "rpc"
PREP = PREFIX / "app-prepare"
AUTHORITY = PREFIX / "postgres-authority/state.json"
LIBEXEC = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016")
STAGED = pathlib.Path("/home/ubuntu/sub2api-migration-20261016")
PROGRAM_STAGE = STAGED / "control-staging"
REDIS_OWNERSHIP = STAGED / "redis-standby/ownership.json"
PROGRAMS = {"install_target_control.py", "prepare_live_app.py", "target_rpc.py"}
RUNTIME_SHA256 = "5570f29f0954706128798f2d4575be4302a0508739d2e21c39e364c4d1dc16fd"
PG_CONFIG = "sha256:ff4a089dadda5e50a5f09604b2e2bc16f97d5befb6b05c328fc21159da96c602"
REDIS_CONFIG = "sha256:b95f59534a9112e82c68677a05afa0c69dd4d75c9fdc7114b98594a28ad79f13"
GUARD = "sub2api-migration-target-guard-20261016"
KEY_TAG = "sub2api-migration-rpc-20261016"
AUTHORIZED = pathlib.Path("/root/.ssh/authorized_keys")
HEX32, HEX64, DIGEST = r"[0-9a-f]{32}", r"[0-9a-f]{64}", r"sha256:[0-9a-f]{64}"
MAX_BYTES = 2 * 1024 * 1024


class Refusal(RuntimeError):
    pass


class Unknown(Refusal):
    pass


def need(condition, code):
    if not condition:
        raise Refusal(code)


def matches(value, expression):
    return isinstance(value, str) and re.fullmatch(expression, value) is not None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def strict_json(raw, limit=MAX_BYTES):
    need(isinstance(raw, bytes) and 0 < len(raw) <= limit, "INPUT_SIZE")
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "DUPLICATE_KEY")
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(Refusal("NONFINITE_JSON")))
        canonical(result)
        return result
    except Refusal:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise Refusal("INVALID_JSON") from None


def public_key(value):
    need(type(value) is dict and set(value) == {"algorithm", "base64", "sha256"} and
         value["algorithm"] == "ssh-ed25519" and matches(value["base64"], r"[A-Za-z0-9+/]{32,128}={0,2}") and
         matches(value["sha256"], HEX64), "PUBLIC_KEY_FIELDS")
    try:
        raw = base64.b64decode(value["base64"], validate=True)
    except ValueError:
        raise Refusal("PUBLIC_KEY_ENCODING") from None
    algorithm = b"ssh-ed25519"
    need(len(raw) == 4 + len(algorithm) + 4 + 32 and raw[:4] == len(algorithm).to_bytes(4, "big") and
         raw[4:4 + len(algorithm)] == algorithm and raw[4 + len(algorithm):8 + len(algorithm)] == (32).to_bytes(4, "big") and
         base64.b64encode(raw).decode() == value["base64"] and sha(raw) == value["sha256"], "PUBLIC_KEY_IDENTITY")
    return "ssh-ed25519 " + value["base64"]


def validate_input(value):
    need(type(value) is dict and set(value) == {"v", "run", "programs", "source_runtime_sha256", "initial_manifest_sha256", "redis_ownership_sha256", "public_key"}, "APPROVAL_FIELDS")
    need(type(value["v"]) is int and value["v"] == 1 and value["run"] == RUN and
         type(value["programs"]) is dict and set(value["programs"]) == PROGRAMS and
         all(matches(row, HEX64) for row in value["programs"].values()) and
         all(matches(value[key], HEX64) for key in ("source_runtime_sha256", "initial_manifest_sha256", "redis_ownership_sha256")), "APPROVAL_IDENTITY")
    need(value["source_runtime_sha256"] == RUNTIME_SHA256, "UNREVIEWED_RUNTIME")
    public_key(value["public_key"])
    return value


def key_line(value):
    return ('from="13.231.143.136",restrict,command="/usr/bin/python3 -I ' +
            str(LIBEXEC / "target_rpc.py") + ' serve" ' + public_key(value) + ' ' + KEY_TAG).encode()


def merge_key(original, value):
    need(isinstance(original, bytes) and len(original) <= MAX_BYTES and b"\x00" not in original, "AUTHORIZED_KEYS_INVALID")
    managed = key_line(value)
    blob = value["base64"].encode()
    found = 0
    for line in original.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        if KEY_TAG.encode() in line:
            need(line == managed, "MANAGED_KEY_CHANGED")
            found += 1
        elif blob in line.split():
            raise Refusal("SAME_KEY_HAS_OTHER_ROOT_AUTHORIZATION")
    need(found <= 1, "DUPLICATE_MANAGED_KEY")
    if found:
        return original
    separator = b"" if not original or original.endswith(b"\n") else b"\n"
    return original + separator + managed + b"\n"


def guard_units():
    service = ("[Unit]\nDescription=Sub2API target migration intent recovery\nAfter=docker.service\n"
               "[Service]\nType=oneshot\nUser=root\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
               "TimeoutStartSec=8\nExecStart=/usr/bin/python3 -I " + str(LIBEXEC / "target_rpc.py") + " guard-reconcile\n")
    timer = ("[Unit]\nDescription=Sub2API target migration recovery timer\n[Timer]\nOnBootSec=1\n"
             "OnUnitActiveSec=1\nAccuracySec=100ms\nUnit=" + GUARD + ".service\n[Install]\nWantedBy=timers.target\n")
    return {GUARD + ".service": service.encode(), GUARD + ".timer": timer.encode()}


class LocalBackend:
    def directory(self, path, *, private=True, create=False):
        path = pathlib.Path(path)
        for item in reversed([path, *path.parents]):
            if not item.exists() and not item.is_symlink():
                need(create, "DIRECTORY_MISSING")
                item.mkdir(mode=0o700)
            info = item.lstat()
            need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "UNTRUSTED_DIRECTORY")
        if private:
            need(stat.S_IMODE(path.stat().st_mode) == 0o700, "DIRECTORY_NOT_PRIVATE")
        return path

    def read(self, path, *, staged=False, mode=0o600, missing=False):
        path = pathlib.Path(path)
        try:
            if staged:
                for item in reversed([path.parent, *path.parent.parents]):
                    info = item.lstat()
                    need(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, 1000) and not info.st_mode & 0o022, "UNTRUSTED_STAGING_DIRECTORY")
            else:
                self.directory(path.parent, private=False)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            need(missing and not path.is_symlink(), "FILE_MISSING")
            return None
        try:
            info = os.fstat(fd)
            owners = (0, 1000) if staged else (0,)
            need(stat.S_ISREG(info.st_mode) and info.st_uid in owners and info.st_nlink == 1 and
                 stat.S_IMODE(info.st_mode) == mode and info.st_size <= MAX_BYTES, "UNTRUSTED_FILE")
            return os.read(fd, MAX_BYTES + 1)
        finally:
            os.close(fd)

    def _sync_dir(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write(self, path, raw, *, mode=0o600, replace=False, expected=None):
        path = pathlib.Path(path)
        self.directory(path.parent, private=(path.parent in (SCOPE, RPC, PREP, LIBEXEC, AUTHORIZED.parent)))
        previous = self.read(path, mode=mode, missing=True)
        if expected is not None:
            need((previous or b"") == expected, "CONCURRENT_FILE_CHANGE")
        if previous is not None:
            if not replace:
                need(previous == raw, "EXISTING_FILE_DIFFERS")
                return
        temporary = path.parent / (".control-install-" + secrets.token_hex(16))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if expected is not None:
                need((self.read(path, mode=mode, missing=True) or b"") == expected, "CONCURRENT_FILE_CHANGE")
            os.replace(temporary, path)
            self._sync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(SCOPE / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, "UNTRUSTED_LOCK")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Unknown("INSTALLER_BUSY") from None
            yield
        finally:
            os.close(fd)

    def root(self):
        need(os.geteuid() == 0, "ROOT_REQUIRED")

    def machine(self):
        path = pathlib.Path("/etc/machine-id")
        self.directory(path.parent, private=False)
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "MACHINE_ID_UNTRUSTED")
        return sha(path.read_bytes().strip())

    def self_source(self):
        need(pathlib.Path(__file__).absolute() == LIBEXEC / "install_target_control.py", "BOOTSTRAP_ROOT_PATH_REQUIRED")
        return self.read(LIBEXEC / "install_target_control.py", mode=0o700)

    def command(self, argv, data=None):
        try:
            result = subprocess.run(argv, input=data, capture_output=True, timeout=8,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise Unknown("LOCAL_COMMAND_UNKNOWN") from None
        if result.returncode:
            raise Unknown("LOCAL_COMMAND_UNKNOWN")
        need(len(result.stdout) <= MAX_BYTES, "COMMAND_OUTPUT_LIMIT")
        return result.stdout

    def inspect(self, kind, name):
        value = strict_json(self.command(["/usr/bin/docker", kind, "inspect", name]))
        need(type(value) is list and len(value) == 1 and type(value[0]) is dict, "RESOURCE_INSPECTION")
        return value[0]

    def ssh_policy(self):
        route = strict_json(self.command(["/usr/sbin/ip", "-j", "route", "get", "13.231.143.136"]))
        need(type(route) is list and len(route) == 1 and isinstance(route[0].get("prefsrc"), str), "LOCAL_SSH_ADDRESS_UNKNOWN")
        address = str(ipaddress.IPv4Address(route[0]["prefsrc"]))
        context = "user=root,addr=13.231.143.136,host=13.231.143.136,laddr=" + address + ",lport=22"
        raw = self.command(["/usr/sbin/sshd", "-T", "-C", context])
        policy = {}
        used = ("permitrootlogin", "pubkeyauthentication", "authenticationmethods", "authorizedkeysfile")
        for line in raw.decode().splitlines():
            fields = line.split(None, 1)
            if len(fields) == 2 and fields[0] in used:
                need(fields[0] not in policy, "DUPLICATE_SSH_POLICY")
                policy[fields[0]] = fields[1]
        need(policy.get("permitrootlogin") in ("yes", "prohibit-password", "without-password", "forced-commands-only") and
             policy.get("pubkeyauthentication") == "yes" and policy.get("authenticationmethods") in ("any", "publickey") and
             any(row in (".ssh/authorized_keys", "/root/.ssh/authorized_keys") for row in policy.get("authorizedkeysfile", "").split()),
             "ROOT_FORCED_PUBLIC_KEY_NOT_PERMITTED")
        return {key: policy[key] for key in used}


def load_reviewed(raw, name):
    need(name in PROGRAMS, "UNAPPROVED_PROGRAM_NAME")
    module = types.ModuleType("reviewed_" + name[:-3])
    module.__file__ = str(LIBEXEC / name)
    exec(compile(raw, str(LIBEXEC / name), "exec"), module.__dict__)
    return module


class Installer:
    def __init__(self, approval, backend=None):
        self.a = validate_input(approval)
        self.b = backend if backend is not None else LocalBackend()

    def _save(self, state, phase):
        state.update(phase=phase, checked_at=time.time())
        self.b.write(SCOPE / "state.json", canonical(state) + b"\n", replace=True)

    def _programs(self):
        result = {}
        for name in sorted(PROGRAMS):
            raw = self.b.read(PROGRAM_STAGE / name, staged=True)
            need(sha(raw) == self.a["programs"][name], "APPROVED_PROGRAM_CHANGED")
            result[name] = raw
        need(sha(self.b.self_source()) == self.a["programs"]["install_target_control.py"], "INSTALLER_SELF_CHANGED")
        return result

    def _image(self, image, expected_config=None):
        need(matches(image.get("Id"), DIGEST) and image.get("Architecture") == "amd64" and image.get("Os") == "linux" and
             type(image.get("RootFS", {}).get("Layers")) is list and 0 < len(image["RootFS"]["Layers"]) <= 128 and
             all(matches(row, DIGEST) for row in image["RootFS"]["Layers"]), "IMAGE_IDENTITY")
        if expected_config is not None and image["Id"] != expected_config:
            def content(identifier):
                need(matches(identifier, DIGEST), "IMAGE_DESCRIPTOR_INVALID")
                raw = self.b.command(["/usr/bin/ctr", "-n", "moby", "content", "get", identifier])
                need("sha256:" + sha(raw) == identifier, "IMAGE_CONTENT_CHANGED")
                return strict_json(raw)
            manifest = content(image["Id"])
            if "manifests" in manifest:
                candidates = [row for row in manifest["manifests"] if row.get("platform", {}).get("architecture") == "amd64" and row.get("platform", {}).get("os") == "linux"]
                need(len(candidates) == 1, "IMAGE_PLATFORM_AMBIGUOUS")
                manifest = content(candidates[0]["digest"])
            need(manifest.get("config", {}).get("digest") == expected_config, "IMAGE_CONFIG_CHANGED")
            config = content(expected_config)
            need(config.get("rootfs", {}).get("diff_ids") == image["RootFS"]["Layers"], "IMAGE_DIFFIDS_CHANGED")
        return image["RootFS"]["Layers"]

    def _resource_entry(self, container, *, expected_config=None):
        need(matches(container.get("Id"), HEX64) and matches(container.get("Image"), DIGEST), "CONTAINER_IDENTITY")
        image = self.b.inspect("image", container["Image"])
        need(image.get("Id") == container["Image"], "IMAGE_INSPECT_CHANGED")
        return {"container_id": container["Id"], "image_id": container["Image"], "config_sha256": sha(container["Config"]),
                "rootfs_diffids": self._image(image, expected_config)}

    def _rpc_config(self, state, adoption, rpc):
        app = self.b.inspect("container", rpc.APP_CONTAINER)
        need(app["Id"] == adoption["container_id"] and app["Image"] == adoption["image_id"] and
             app["State"]["Running"] is False and app["State"]["Status"] == "created" and app["State"].get("Pid") == 0 and
             app["HostConfig"]["RestartPolicy"]["Name"] == "no" and sha(app["Config"]) == adoption["config_sha256"] and
             adoption["owner"] == state["owner"] and adoption["control_port"] == 18092, "APP_NOT_OWNED_INERT")
        app_entry = {**self._resource_entry(app), "control_port": 18092}
        need(app_entry["rootfs_diffids"] == adoption["rootfs_diffids"], "APP_IMAGE_CHANGED")
        authority = strict_json(self.b.read(AUTHORITY))
        need(authority.get("run") == "sub2api-pg-authority-20261016" and authority.get("phase") == "standby-role-aware-ready" and
             authority.get("authority_mode") == "STANDBY" and authority.get("promotion_intent") is None and
             matches(authority.get("owner"), HEX32), "PG_AUTHORITY_NOT_STANDBY")
        pg = self.b.inspect("container", rpc.PG_CONTAINER)
        need(pg["Id"] == authority["container_id"] and pg["Image"] == authority["image_id"], "PG_AUTHORITY_CONTAINER_CHANGED")
        pg_entry = {**self._resource_entry(pg, expected_config=PG_CONFIG), "owner": authority["owner"], "volume_path": authority["volume_path"],
                    "system_identifier": authority["system_identifier"], "source_timeline": authority["source_timeline"]}
        raw = self.b.read(REDIS_OWNERSHIP, staged=True)
        need(sha(raw) == self.a["redis_ownership_sha256"], "REDIS_OWNERSHIP_CHANGED")
        ownership = strict_json(raw)
        need(type(ownership) is dict and set(ownership) == {"owner", "run", "source_run_id", "source_replid", "container", "volume"} and
             ownership["run"] == "sub2api-redis-standby-20261016" and matches(ownership["owner"], HEX32) and
             ownership["container"] == rpc.REDIS_CONTAINER and ownership["volume"] == rpc.REDIS_VOLUME, "REDIS_OWNERSHIP_INVALID")
        redis = self.b.inspect("container", rpc.REDIS_CONTAINER)
        volume = self.b.inspect("volume", rpc.REDIS_VOLUME)
        redis_entry = {**self._resource_entry(redis, expected_config=REDIS_CONFIG), "owner": ownership["owner"], "volume_path": volume["Mountpoint"]}
        cfg = {"v": 1, "run": RUN, "owner": state["owner"], "program_sha256": self.a["programs"]["target_rpc.py"],
               "machine_id_sha256": state["machine_id_sha256"], "app": app_entry, "pg": pg_entry, "redis": redis_entry}
        rpc.validate_config(cfg)
        # The reviewed backend performs full volume/label/mount/restart checks.
        local = rpc.LocalBackend(cfg)
        local.resource("pg")
        local.resource("redis")
        local.pg_manifest()
        sample = local.pg_sample()
        rpc.TargetRPC(cfg, local, None)._pg_validate(sample)
        observed = local.redis_inspect()
        need(observed["role"] == "slave" and observed["failover_state"] == "no-failover", "REDIS_NOT_STANDBY")
        # INFO replication includes the actual channel relation, not just role.
        replication = dict(line.split(":", 1) for line in local.redis_command("INFO", "replication").splitlines() if line and not line.startswith("#") and ":" in line)
        need(replication.get("master_host") == "127.0.0.1" and replication.get("master_port") == "16379" and
             replication.get("master_link_status") == "up" and replication.get("master_sync_in_progress") == "0", "REDIS_REPLICATION_NOT_READY")
        upstream = self.b.command(["/usr/bin/docker", "exec", "-e", "REDISCLI_AUTH=", redis["Id"], "redis-cli", "--raw", "-h", "127.0.0.1", "-p", "16379", "INFO"])
        source = dict(line.split(":", 1) for line in upstream.decode().splitlines() if line and not line.startswith("#") and ":" in line)
        need(matches(ownership.get("source_run_id"), r"[0-9a-f]{40}") and matches(ownership.get("source_replid"), r"[0-9a-f]{40}") and
             source.get("run_id") == ownership["source_run_id"] and source.get("role") == "master" and
             source.get("master_failover_state", "no-failover") == "no-failover" and
             source.get("master_replid") == observed["master_replid"], "REDIS_SOURCE_LINEAGE_CHANGED")
        state["adopted"] = {"app_container_id": app["Id"], "pg_container_id": pg["Id"], "redis_container_id": redis["Id"],
                            "pg_owner": authority["owner"], "redis_owner": ownership["owner"], "redis_run_id": observed["run_id"],
                            "rpc_config_sha256": sha(cfg)}
        return cfg

    def install(self):
        self.b.root()
        programs = self._programs()
        machine = self.b.machine()
        policy = self.b.ssh_policy()
        for path in (PREFIX, SCOPE, RPC, PREP, LIBEXEC):
            self.b.directory(path, create=True)
        with self.b.lock():
            raw = self.b.read(SCOPE / "state.json", missing=True)
            if raw is None:
                state = {"v": 1, "run": RUN, "owner": secrets.token_hex(16), "machine_id_sha256": machine,
                         "approval_sha256": sha(self.a), "public_key_sha256": self.a["public_key"]["sha256"], "created_at": time.time()}
                self._save(state, "INSTALL_INTENT")
            else:
                state = strict_json(raw)
                need(state.get("v") == 1 and state.get("run") == RUN and matches(state.get("owner"), HEX32) and
                     state.get("machine_id_sha256") == machine and state.get("approval_sha256") == sha(self.a), "INSTALL_STATE_CHANGED")
            # An inert installer never resets a live or previously accepted RPC.
            existing = self.b.read(RPC / "state.json", missing=True)
            if existing is not None:
                ledger = strict_json(existing)
                need(type(ledger) is dict and set(ledger) == {"v", "run", "owner", "epoch", "operations", "pg", "redis"} and
                     type(ledger.get("v")) is int and ledger["v"] == 1 and
                     type(ledger.get("epoch")) is int and ledger["epoch"] == 0 and
                     ledger.get("owner") == state["owner"] and ledger.get("run") == RUN and
                     ledger.get("operations") == {} and ledger.get("pg") == {"authority": "AWS", "arm": None, "promotion_intent": None} and
                     ledger.get("redis") == {"authority": "AWS"}, "RPC_ALREADY_ACTIVE")
            for name, raw in programs.items():
                self.b.write(LIBEXEC / name, raw, mode=0o700)
            prep = load_reviewed(programs["prepare_live_app.py"], "prepare_live_app.py")
            rpc = load_reviewed(programs["target_rpc.py"], "target_rpc.py")
            cfg = {"v": 1, "run": RUN, "owner": state["owner"], "machine_id_sha256": machine,
                   "source_runtime_sha256": self.a["source_runtime_sha256"], "initial_manifest_sha256": self.a["initial_manifest_sha256"]}
            self.b.write(PREP / "config.json", canonical(cfg) + b"\n")
            actual_cfg, runtime, payload = prep.load_inputs()
            need(actual_cfg == cfg, "APP_PREP_CONFIG_CHANGED")
            self._save(state, "APP_CREATE_INTENT")
            adoption = prep.prepare(actual_cfg, runtime, payload, prep.Docker())
            cfg = self._rpc_config(state, adoption, rpc)
            self.b.write(RPC / "config.json", canonical(cfg) + b"\n")
            token = self.b.read(PREP / "app-control.token")
            need(matches(token.strip().decode("ascii"), r"[A-Za-z0-9_-]{32,256}"), "APP_CONTROL_TOKEN_INVALID")
            self.b.write(RPC / "app-control.token", token)
            initial = {"v": 1, "run": RUN, "owner": state["owner"], "epoch": 0, "operations": {},
                       "pg": {"authority": "AWS", "arm": None, "promotion_intent": None}, "redis": {"authority": "AWS"}}
            self.b.write(RPC / "state.json", canonical(initial) + b"\n")
            # Key adoption happens only after a valid RPC config and guard state
            # exist. Preserve every byte of unrelated root authorization.
            self.b.ssh_policy()
            self.b.directory(AUTHORIZED.parent, create=True)
            original = self.b.read(AUTHORIZED, missing=True) or b""
            merged = merge_key(original, self.a["public_key"])
            self._save(state, "ROOT_KEY_INSTALL_INTENT")
            self.b.write(AUTHORIZED, merged, replace=True, expected=original)
            for unit, raw in guard_units().items():
                self.b.write(pathlib.Path("/etc/systemd/system") / unit, raw, mode=0o644)
            self._save(state, "GUARD_ENABLE_INTENT")
            self.b.command(["/usr/bin/systemctl", "daemon-reload"])
            for unit in guard_units():
                status = self.b.command(["/usr/bin/systemctl", "show", unit, "--property=LoadState,DropInPaths,FragmentPath"]).decode()
                loaded = dict(line.split("=", 1) for line in status.splitlines() if "=" in line)
                need(loaded.get("LoadState") == "loaded" and loaded.get("DropInPaths") == "" and
                     loaded.get("FragmentPath") == "/etc/systemd/system/" + unit, "GUARD_UNIT_OVERRIDDEN")
            self.b.command(["/usr/bin/systemctl", "enable", "--now", GUARD + ".timer"])
            status = self.b.command(["/usr/bin/systemctl", "show", GUARD + ".timer", "--property=ActiveState,UnitFileState,Job,DropInPaths,FragmentPath"]).decode()
            fields = dict(line.split("=", 1) for line in status.splitlines() if "=" in line)
            need(fields.get("ActiveState") == "active" and fields.get("UnitFileState") == "enabled" and not fields.get("Job") and
                 fields.get("DropInPaths") == "" and fields.get("FragmentPath") == "/etc/systemd/system/" + GUARD + ".timer", "GUARD_TIMER_NOT_VERIFIED")
            state["ssh_policy"] = policy
            self._save(state, "INSTALLED_INERT")
            return {"v": 1, "run": RUN, "owner": state["owner"], "phase": state["phase"], "initial_epoch": 0,
                    "machine_id_sha256": machine, "public_key_sha256": state["public_key_sha256"], **state["adopted"],
                    "application_started": False, "data_services_restarted": False, "promotion": False, "dns_or_entry_changed": False}


def read_approval(stream):
    def expired(_signum, _frame):
        raise Refusal("APPROVAL_READ_TIMEOUT")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 3)
    try:
        return validate_input(strict_json(stream.read(65537), limit=65536))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def main(argv=None, stdin=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-inert", action="store_true")
    args = parser.parse_args(argv)
    if not args.install_inert:
        print(json.dumps({"offline": True, "production_actions": False, "run": RUN, "starts_application": False,
                          "requires_root_bootstrap": str(LIBEXEC / "install_target_control.py")}))
        return 0
    print(json.dumps(Installer(read_approval(stdin or sys.stdin.buffer)).install(), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as error:
        print(json.dumps({"ok": False, "code": str(error), "application_started": False}), file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print(json.dumps({"ok": False, "code": "INSTALL_STATE_UNCERTAIN", "inspect_retained_resources": True}), file=sys.stderr)
        raise SystemExit(1)
