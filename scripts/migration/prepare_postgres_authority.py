#!/usr/bin/env python3
"""Adopt the owned PostgreSQL standby into a role-aware, restartable specification.

Default is an offline plan. --inspect is read-only. --apply replaces only the
owned Tencent standby container, preserving its volume and physical replication.
There is no promotion, source mutation, application or DNS operation.
"""
import argparse
import contextlib
import hashlib
import json
import math
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import time


def load_replica_library():
    # The controller transfers the reviewed local source, not a program from a
    # mutable remote home directory. A non-main namespace prevents its CLI.
    source = globals().get("REPLICA_LIBRARY")
    if source is None:
        source = pathlib.Path(__file__).with_name("prepare_postgres_replica.py").read_text()
    namespace = {"__name__": "reviewed_replica_library", "__file__": __file__}
    exec(compile(source, "<reviewed-replica-library>", "exec"), namespace)
    return source, namespace


LIBRARY_SOURCE, LIB = load_replica_library()
Base = LIB["Remote"]
Refusal, require = LIB["Refusal"], LIB["require"]
atomic_private, save_json, read_json = LIB["atomic_private"], LIB["save_json"], LIB["read_json"]
RUN = "sub2api-pg-authority-20261016"
SCOPE = "/var/lib/sub2api-migration-20261016/postgres-authority"
OLD_SCOPE = LIB["TARGET_SCOPE"]
CONTAINER, VOLUME, IMAGE = LIB["CONTAINER"], LIB["VOLUME"], LIB["IMAGE"]
ARCHIVE = "sub2api-migration-pg-preauthority-20261016"
DATA, PORT, SLOT, LABEL = LIB["DATA"], LIB["PORT"], LIB["SLOT"], LIB["LABEL"]
AUTHORITY_LABEL = "io.sub2api.migration.authority"
UNIT = RUN+"-monitor"
SYSTEM_UNITS = "/etc/systemd/system"
DISK_MIN = LIB["DISK_TRIP_BYTES"]
MIB = LIB["MIB"]


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def require_root():
    require(os.geteuid() == 0, "target-root-required")


def lsn_number(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}", value), "invalid-wal-lsn")
    high, low = value.split("/")
    return (int(high, 16) << 32)+int(low, 16)


def root_directory(path, create=False):
    path = pathlib.Path(path)
    require(path.is_absolute(), "root-scope-must-be-absolute")
    for item in reversed([path, *path.parents]):
        require(not item.is_symlink(), "root-scope-symlink")
        if not item.exists():
            require(create, "root-scope-missing")
            item.mkdir(mode=0o700)
        mode = item.stat()
        require(stat.S_ISDIR(mode.st_mode) and mode.st_uid == 0 and not mode.st_mode & 0o022,
                "root-scope-parent-not-trusted")
    require(stat.S_IMODE(path.stat().st_mode) == 0o700, "root-scope-not-0700")
    return path


@contextlib.contextmanager
def locked(path, *, nonblocking=False):
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1,
                "unsafe-lock-file")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError:
            raise Refusal("another-authority-operation-is-running") from None
        yield
    finally:
        os.close(fd)


def role_bootstrap():
    # Initialization and verification belong to the original preparation only.
    # Recovery is inherently read-only. An explicit off default allows a later
    # separately-authorized promotion to serve writes and restart as primary.
    command = ["postgres", "-D", DATA,
        "-c", "listen_addresses=127.0.0.1", "-c", f"port={PORT}",
        "-c", "unix_socket_directories=/tmp", "-c", "hot_standby=on",
        "-c", "default_transaction_read_only=off", "-c", "ssl=off",
        "-c", "shared_buffers=128MB", "-c", "archive_mode=off",
        "-c", "archive_command=", "-c", "restore_command=",
        "-c", "recovery_target_timeline=latest"]
    return f'''set -eu
umask 077
if command -v gosu >/dev/null 2>&1; then drop=gosu
elif command -v su-exec >/dev/null 2>&1; then drop=su-exec
else exit 71; fi
[ -f {DATA}/.sub2api-seed-verified ] || exit 72
[ "$(cat {DATA}/PG_VERSION)" = 18 ] || exit 73
mkdir -p /run/sub2api-migration
if [ -f /run/replication-input.pgpass ]; then
    cp /run/replication-input.pgpass /run/sub2api-migration/replication.pgpass
    chmod 600 /run/sub2api-migration/replication.pgpass
    chown postgres:postgres /run/sub2api-migration/replication.pgpass
fi
chown postgres:postgres /run/sub2api-migration
chmod 700 /run/sub2api-migration
export PGDATA={DATA}
export PGPASSFILE=/run/sub2api-migration/replication.pgpass
export PGAPPNAME={LIB["RUN"]}
export PGSSLMODE=disable
export PGCONNECT_TIMEOUT=10
exec "$drop" postgres {shlex.join(command)}
'''


def container_command(owner, image_id):
    return ["docker", "create", "--name", CONTAINER,
        "--label", LABEL+"="+owner, "--label", "io.sub2api.migration.run="+LIB["RUN"],
        "--label", AUTHORITY_LABEL+"="+RUN,
        "--network", "host", "--memory", "768m", "--memory-swap", "768m",
        "--cpus", "1", "--pids-limit", "128", "--restart", "no",
        "--security-opt", "no-new-privileges:true", "--cap-drop", "NET_RAW",
        "--tmpfs", "/var/lib/postgresql:rw,noexec,nosuid,size=1m,mode=0755",
        "--tmpfs", "/run/sub2api-migration:rw,noexec,nosuid,size=1m,mode=0700",
        "--mount", f"type=volume,src={VOLUME},dst={DATA},volume-nocopy",
        "--mount", f"type=bind,src={SCOPE}/replication.pgpass,dst=/run/replication-input.pgpass,readonly",
        "--entrypoint", "/bin/sh", image_id, "-ceu", role_bootstrap()]


def monitor_units():
    service = f'''[Unit]
Description=Sub2API PostgreSQL role-aware authority monitor
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {SCOPE}/monitor.py
User=root
UMask=0077
TimeoutStartSec=15
NoNewPrivileges=true
PrivateTmp=true
'''
    timer = f'''[Unit]
Description=Sub2API PostgreSQL role-aware authority timer
[Timer]
OnBootSec=1
OnUnitActiveSec=5
AccuracySec=1
Unit={UNIT}.service
[Install]
WantedBy=timers.target
'''
    return {UNIT+".service": service, UNIT+".timer": timer}


def valid_intent(state):
    intent = state.get("promotion_intent")
    return bool(isinstance(intent, dict) and
        re.fullmatch(r"[0-9a-f]{32}", str(intent.get("op_id", ""))) and
        re.fullmatch(r"[0-9a-f]{64}", str(intent.get("source_fence_sha256", ""))) and
        re.fullmatch(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}", str(intent.get("final_lsn", ""))) and
        intent.get("system_identifier") == state["system_identifier"] and
        intent.get("source_timeline") == state["source_timeline"] and
        type(intent.get("accepted_at")) in [int, float] and math.isfinite(intent["accepted_at"]) and intent["accepted_at"] > 0)


def classify(state, sample):
    """Pure monitoring decision. No primary or unknown role is ever stopped."""
    if not sample.get("running"):
        return {"healthy": False, "role": "not-running", "stop_standby": False}
    identity = (sample.get("system_identifier") == state["system_identifier"] and
        sample.get("version_num") == 180003 and sample.get("port") == PORT and
        sample.get("listen_addresses") == "127.0.0.1")
    if not identity or not isinstance(sample.get("in_recovery"), bool):
        return {"healthy": False, "role": "unknown", "stop_standby": False}
    standby = sample["in_recovery"]
    mode = state.get("authority_mode")
    intent = valid_intent(state)
    low_disk = sample["target_free_bytes"] < DISK_MIN
    if not standby:
        authorized = (mode in ["PROMOTION_INTENT", "PRIMARY"] and intent and
            sample.get("wal_timeline", 0) > state["source_timeline"])
        writable = sample.get("transaction_read_only") == "off" and sample.get("default_transaction_read_only") == "off"
        return {"healthy": authorized and writable and not low_disk,
                "role": "primary" if authorized else "unexpected-primary", "stop_standby": False}
    allowed = (mode in ["ADOPTING", "STANDBY"] and state.get("promotion_intent") is None) or (mode == "PROMOTION_INTENT" and intent)
    streaming = (sample.get("receiver_status") == "streaming" and sample.get("primary_slot_name") == SLOT and
        sample.get("sender_host") == "127.0.0.1" and sample.get("sender_port") == LIB["CHANNEL_PORT"] and
        sample.get("received_tli") == state["source_timeline"] and sample.get("replay_lsn") is not None)
    default_ok = mode == "ADOPTING" or sample.get("default_transaction_read_only") == "off"
    return {"healthy": bool(allowed and streaming and default_ok and sample.get("transaction_read_only") == "on" and not low_disk),
            "role": "standby", "stop_standby": low_disk and mode in ["ADOPTING", "STANDBY"] and state.get("promotion_intent") is None}


class Authority(Base):
    def __init__(self, payload):
        super().__init__({"side": "target", "action": "inspect" if payload["action"] == "inspect" else payload["action"]})
        self.p = payload
        self.scope = pathlib.Path(SCOPE)
        self.log = self.scope/"private-errors.log"

    def state(self):
        root_directory(self.scope)
        state = read_json(self.scope/"state.json")
        require(state["run"] == RUN and re.fullmatch(r"[0-9a-f]{32}", state["owner"]), "authority-state-identity")
        require(state["library_sha256"] == digest(LIBRARY_SOURCE), "reviewed-library-changed")
        return state

    def checkpoint(self, state, phase):
        state["phase"], state["updated_at"] = phase, time.time()
        save_json(self.scope/"state.json", state)

    def prepared_state(self):
        LIB["private_dir"](OLD_SCOPE)
        state = read_json(pathlib.Path(OLD_SCOPE)/"state.json")
        require(state["run"] == LIB["RUN"] and state["side"] == "target" and not state.get("cleaned"), "prepared-state-invalid")
        require(re.fullmatch(r"[0-9a-f]{32}", state["owner"]), "prepared-owner-invalid")
        require(not (pathlib.Path(OLD_SCOPE)/"safety-tripped.json").exists(), "original-standby-safety-tripped")
        return state

    def object(self, name, state, *, original=False):
        info = self.docker_inspect(name)
        if info is None:
            return None
        labels = info["Config"].get("Labels", {})
        require(labels.get(LABEL) == state["owner"] and labels.get("io.sub2api.migration.run") == LIB["RUN"], "container-ownership-mismatch")
        require(info["Image"] == state["image_id"], "container-image-changed")
        host = info["HostConfig"]
        require(host["NetworkMode"] == "host" and not host.get("PortBindings") and host["Memory"] == 768*MIB and
                host["MemorySwap"] == 768*MIB and host["PidsLimit"] == 128 and host["NanoCpus"] == 1000000000 and
                host["RestartPolicy"]["Name"] == "no", "container-runtime-spec-changed")
        require(info["Config"]["Entrypoint"] == ["/bin/sh"], "container-entrypoint-changed")
        shell = LIB["bootstrap_shell"]() if original else role_bootstrap()
        require(info["Config"]["Cmd"] == ["-ceu", shell], "container-command-changed")
        if not original:
            require(labels.get(AUTHORITY_LABEL) == RUN, "container-authority-label-missing")
            require(not state.get("container_id") or info["Id"] == state["container_id"], "authority-container-id-changed")
            require(host.get("Tmpfs") == {
                "/var/lib/postgresql": "rw,noexec,nosuid,size=1m,mode=0755",
                "/run/sub2api-migration": "rw,noexec,nosuid,size=1m,mode=0700"}, "container-tmpfs-spec-changed")
        mounts = info["Mounts"]
        pg = [m for m in mounts if m["Destination"] == DATA]
        require(len(pg) == 1 and pg[0]["Type"] == "volume" and pg[0]["Name"] == VOLUME and pg[0]["RW"] and
                pg[0]["Source"] == state["volume_path"], "container-pgdata-mount-changed")
        secret = [m for m in mounts if m["Destination"] == "/run/replication-input.pgpass"]
        expected = str(pathlib.Path(OLD_SCOPE if original else SCOPE)/"replication.pgpass")
        require(len(secret) == 1 and secret[0]["Type"] == "bind" and secret[0]["Source"] == expected and
                secret[0]["RW"] is False, "container-pgpass-mount-changed")
        # PostgreSQL 18 declares a parent image VOLUME even with explicit PGDATA
        # nested below it. Pin that legacy anonymous mount but never delete or
        # repurpose it. New containers use an explicit empty tmpfs parent.
        parent = [m for m in mounts if m["Destination"] == "/var/lib/postgresql"]
        if original:
            require(parent == state.get("legacy_parent_mount", []), "legacy-parent-mount-changed")
        else:
            require(not parent or (len(parent) == 1 and parent[0]["Type"] == "tmpfs"), "unexpected-parent-mount")
        allowed = {DATA, "/var/lib/postgresql", "/run/replication-input.pgpass", "/run/sub2api-migration"}
        require(all(m["Destination"] in allowed for m in mounts), "unexpected-container-mount")
        return info

    def owned_volume(self, state):
        volume = self.docker_inspect(VOLUME, kind="volume")
        require(volume and volume.get("Labels", {}).get(LABEL) == state["owner"] and
                volume.get("Labels", {}).get("io.sub2api.migration.run") == LIB["RUN"] and volume["Driver"] == "local" and
                volume["Mountpoint"] == state["volume_path"], "volume-identity-changed")
        path = pathlib.Path(volume["Mountpoint"])
        require(not any(p.is_symlink() for p in [path, *path.parents]), "volume-path-symlink")
        require((path/"PG_VERSION").read_text().strip() == "18" and (path/".sub2api-seed-verified").is_file(), "verified-seed-missing")
        return path

    def sample(self, state):
        volume = self.owned_volume(state)
        out = {"sampled_at": time.time(), "target_free_bytes": __import__("shutil").disk_usage(volume).free,
               "running": False}
        info = self.docker_inspect(CONTAINER)
        if not info:
            return out
        original = info["Id"] == state["original_container_id"]
        info = self.object(CONTAINER, state, original=original)
        if not info["State"]["Running"]:
            return out
        out["running"] = True
        out["container_id"] = info["Id"]
        query = """SELECT json_build_object(
'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
'version_num',current_setting('server_version_num')::int,
'in_recovery',pg_is_in_recovery(),'transaction_read_only',current_setting('transaction_read_only'),
'default_transaction_read_only',current_setting('default_transaction_read_only'),
'listen_addresses',current_setting('listen_addresses'),'port',current_setting('port')::int,
'primary_slot_name',current_setting('primary_slot_name'),
'replay_lsn',pg_last_wal_replay_lsn()::text,'receive_lsn',pg_last_wal_receive_lsn()::text,
'receiver_status',(SELECT status FROM pg_stat_wal_receiver LIMIT 1),
'sender_host',(SELECT sender_host FROM pg_stat_wal_receiver LIMIT 1),
'sender_port',(SELECT sender_port FROM pg_stat_wal_receiver LIMIT 1),
'received_tli',(SELECT received_tli FROM pg_stat_wal_receiver LIMIT 1),
'wal_timeline',CASE WHEN pg_is_in_recovery() THEN NULL ELSE substring(pg_walfile_name(pg_current_wal_lsn()),1,8) END);"""
        out.update(self.query(query, target=True))
        if out["wal_timeline"] is not None:
            out["wal_timeline"] = int(out["wal_timeline"], 16)
        return out

    def require_standby(self, state, *, running=True):
        path = self.owned_volume(state)
        require((path/"standby.signal").is_file() and not (path/"standby.signal").is_symlink(), "not-an-unpromoted-volume")
        require(state["authority_mode"] in ["ADOPTING", "STANDBY"] and state.get("promotion_intent") is None, "authority-already-handed-to-cutover")
        require(not (self.scope/"safety-tripped.json").exists(), "authority-safety-tripped")
        if running:
            sample = self.sample(state)
            decision = classify(state, sample)
            require(decision["healthy"] and decision["role"] == "standby", "standby-identity-or-streaming-not-ready")
            return sample
        require(__import__("shutil").disk_usage(path).free >= DISK_MIN, "target-disk-low")

    def old_units(self):
        name, service, timer = LIB["monitor_units"]("target", OLD_SCOPE)
        return {name+".service": service, name+".timer": timer}

    def legacy_unit_status(self):
        result = {}
        for name, expected in self.old_units().items():
            path = pathlib.Path(SYSTEM_UNITS)/name
            masked = path.is_symlink() and os.readlink(path) == "/dev/null"
            require(masked or (not path.is_symlink() and path.is_file() and path.read_text() == expected), "legacy-monitor-unit-changed")
            details = self.run(["systemctl", "show", name, "--property=ActiveState,MainPID,Job,Restart"], timeout=5).stdout
            values = dict(line.split("=", 1) for line in details.splitlines() if "=" in line)
            if name.endswith(".timer"):
                values.setdefault("MainPID", "0")
            result[name] = {"masked": masked, **values}
        return result

    def monitor(self):
        state = self.state()
        free_bytes = None
        try:
            free_bytes = __import__("shutil").disk_usage(self.owned_volume(state)).free
            sample = self.sample(state)
            result = {**sample, **classify(state, sample)}
            if result["stop_standby"]:
                # Future promotion RPC must take this same lock before saving
                # intent. Re-read both role and intent immediately before stop.
                with locked(self.scope/"authority.lock", nonblocking=True):
                    current = self.state()
                    latest = self.sample(current)
                    if classify(current, latest)["stop_standby"]:
                        save_json(self.scope/"safety-tripped.json", {"reason": "owned-standby-disk-low", "at": time.time()})
                        self.stop_standby(current)
                        result["standby_stopped"] = True
            result["monitor_ok"] = result["healthy"]
        except Exception:
            result = {"sampled_at": time.time(), "monitor_ok": False, "healthy": False,
                      "role": "unknown", "stop_standby": False, "target_free_bytes": free_bytes,
                      "disk_low": None if free_bytes is None else free_bytes < DISK_MIN,
                      "error": "monitor-check-failed-see-private-state"}
        save_json(self.scope/"monitor-status.json", result)
        return result

    def stop_standby(self, state):
        sample = self.sample(state)
        require(sample.get("in_recovery") is True and sample.get("system_identifier") == state["system_identifier"] and
                state.get("promotion_intent") is None and state["authority_mode"] in ["ADOPTING", "STANDBY"], "refuse-stop-primary-or-uncertain-role")
        self.run(["docker", "kill", "--signal=SIGINT", CONTAINER], timeout=5)
        end = time.monotonic()+20
        while time.monotonic() < end:
            info = self.docker_inspect(CONTAINER)
            require(info and info["Id"] == sample["container_id"], "container-changed-during-stop")
            if not info["State"]["Running"]:
                require(info["State"]["ExitCode"] == 0 and not info["State"].get("OOMKilled"), "standby-did-not-exit-cleanly")
                return
            time.sleep(0.2)
        raise Refusal("standby-stop-pending-no-sigkill-was-sent")

    def install_monitor(self, state):
        program = bundled_program({"action": "monitor"}, self.p["program"], LIBRARY_SOURCE)
        path = self.scope/"monitor.py"
        if path.exists():
            require(path.read_text() == program and not path.is_symlink(), "authority-monitor-program-changed")
        else:
            atomic_private(path, program, 0o700)
        for name, body in monitor_units().items():
            unit = pathlib.Path(SYSTEM_UNITS)/name
            require(not unit.is_symlink() and (not unit.exists() or unit.read_text() == body), "authority-monitor-unit-changed")
            atomic_private(unit, body, 0o644)
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "enable", "--now", UNIT+".timer"])
        self.run(["systemctl", "is-active", "--quiet", UNIT+".timer"])
        result = self.monitor()
        require(result["monitor_ok"], "new-monitor-not-ready")
        self.checkpoint(state, "new-monitor-active")

    def retire_old_monitor(self, state):
        before = self.legacy_unit_status()
        timer = LIB["UNIT_TARGET"]+".timer"
        service = LIB["UNIT_TARGET"]+".service"
        if not before[timer]["masked"]:
            self.run(["systemctl", "disable", "--now", timer])
        # Stopping a timer leaves its already-running oneshot alone. Wait for
        # that invocation rather than killing its Python/docker subprocesses.
        end = time.monotonic()+35
        while time.monotonic() < end:
            statuses = self.legacy_unit_status()
            if all(s.get("ActiveState") in ["inactive", "failed"] and s.get("MainPID") == "0" and
                   s.get("Job", "") in ["", "0"] for s in statuses.values()):
                break
            time.sleep(0.2)
        else:
            raise Refusal("legacy-monitor-still-running-no-container-replacement")
        for name, body in self.old_units().items():
            unit = pathlib.Path(SYSTEM_UNITS)/name
            backup = self.scope/(name+".before")
            if backup.exists():
                require(not backup.is_symlink() and backup.read_text() == body, "legacy-monitor-backup-changed")
            else:
                atomic_private(backup, body)
            if not unit.is_symlink():
                require(unit.read_text() == body, "legacy-monitor-changed-during-retirement")
                temporary = unit.parent/("."+name+"."+state["owner"])
                require(not temporary.exists() and not temporary.is_symlink(), "mask-temporary-exists")
                os.symlink("/dev/null", temporary)
                os.replace(temporary, unit)
                parent_fd = os.open(unit.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
        self.run(["systemctl", "daemon-reload"])
        require(all(s["masked"] and s.get("ActiveState") in ["inactive", "failed"] and s.get("MainPID") == "0"
                    for s in self.legacy_unit_status().values()), "legacy-monitor-mask-not-effective")
        self.checkpoint(state, "legacy-monitor-masked-and-idle")

    def verify_source(self):
        source = self.p["source"]
        require(0 <= time.time()-source["sampled_at"] <= 120, "source-inspection-stale")
        require(not source["in_recovery"] and source["role_login"] and source["monitor"]["healthy"] and
                not source["safety_tripped"] and source["source_free_bytes"] >= DISK_MIN and 0 < source["wal_cap_mb"] <= 512,
                "source-replication-protection-not-ready")
        slot = next((x for x in source["slots"] if x["name"] == SLOT), None)
        require(slot and slot["wal_status"] in ["reserved", "extended"], "owned-source-slot-not-usable")
        return source

    def adopt_state(self, source):
        prepared = self.prepared_state()
        require(prepared["owner"] == source["owner"] and prepared["system_identifier"] == source["system_identifier"], "source-prepared-identity-mismatch")
        require(any(s["name"] == SLOT and s["active"] for s in source["slots"]), "initial-source-slot-not-active")
        legacy = self.legacy_unit_status()
        require(all(not s["masked"] for s in legacy.values()) and legacy[LIB["UNIT_TARGET"]+".timer"].get("ActiveState") == "active",
                "initial-legacy-monitor-not-active")
        old_sample = read_json(pathlib.Path(OLD_SCOPE)/"monitor-status.json")
        require(old_sample.get("monitor_ok") is True and 0 <= time.time()-old_sample["sampled_at"] <= 20,
                "initial-legacy-monitor-sample-stale")
        volume = self.docker_inspect(VOLUME, kind="volume")
        require(volume and volume.get("Labels", {}).get(LABEL) == prepared["owner"] and volume["Driver"] == "local", "prepared-volume-not-owned")
        state = {"run": RUN, "owner": prepared["owner"], "system_identifier": source["system_identifier"],
                 "source_timeline": source["timeline"], "image_id": prepared["image_id"], "volume_path": volume["Mountpoint"],
                 "authority_mode": "ADOPTING", "phase": "adoption-intent", "promotion_intent": None,
                 "library_sha256": digest(LIBRARY_SOURCE), "created_at": time.time()}
        image = self.docker_inspect(state["image_id"], kind="image")
        require(image and self.image_identity(image) == source["image_identity"], "reviewed-source-target-image-mismatch")
        info = self.docker_inspect(CONTAINER)
        require(info and info["State"]["Running"], "original-standby-not-running")
        state["original_container_id"] = info["Id"]
        state["legacy_parent_mount"] = [x for x in info["Mounts"] if x["Destination"] == "/var/lib/postgresql"]
        require(not state["legacy_parent_mount"] or (len(state["legacy_parent_mount"]) == 1 and
                state["legacy_parent_mount"][0]["Type"] == "volume"), "unreviewed-legacy-parent-mount")
        self.object(CONTAINER, state, original=True)
        self.require_standby(state)
        require(self.docker_inspect(ARCHIVE) is None, "archive-container-already-exists")
        root_directory(self.scope, create=True)
        save_json(self.scope/"state.json", state)
        return state

    def start_new(self, state):
        self.require_standby(state, running=False)
        require(all(s["masked"] and s.get("MainPID") == "0" and s.get("ActiveState") in ["inactive", "failed"]
                    for s in self.legacy_unit_status().values()), "legacy-monitor-not-retired")
        current = self.docker_inspect(CONTAINER)
        archive = self.docker_inspect(ARCHIVE)
        if current and current["Id"] == state["original_container_id"]:
            self.object(CONTAINER, state, original=True)
            if current["State"]["Running"]:
                self.require_standby(state)
                self.checkpoint(state, "original-stop-intended")
                self.stop_standby(state)
            else:
                require(current["State"]["ExitCode"] == 0 and not current["State"].get("OOMKilled"),
                        "original-standby-did-not-exit-cleanly")
            require(archive is None, "archive-name-conflict")
            self.checkpoint(state, "original-rename-intended")
            self.run(["docker", "rename", CONTAINER, ARCHIVE])
            current = None
        archive = self.object(ARCHIVE, state, original=True)
        require(archive and archive["Id"] == state["original_container_id"] and not archive["State"]["Running"], "original-archive-missing-or-running")
        if current is None:
            self.checkpoint(state, "replacement-create-intended")
            self.run(container_command(state["owner"], state["image_id"]))
        info = self.object(CONTAINER, state)
        require(info is not None, "replacement-container-missing")
        if state.get("container_id"):
            require(info["Id"] == state["container_id"], "replacement-container-id-changed")
        state["container_id"] = info["Id"]
        self.checkpoint(state, "replacement-created")
        if not info["State"]["Running"]:
            self.require_standby(state, running=False)
            self.run(["docker", "start", CONTAINER])
        end = time.monotonic()+40
        while time.monotonic() < end:
            try:
                sample = self.require_standby(state)
                require(sample["default_transaction_read_only"] == "off", "new-default-is-not-writable-after-recovery")
                require(lsn_number(sample["replay_lsn"]) >= lsn_number(self.p["source"]["wal_lsn"]),
                        "replacement-replay-has-not-reached-initial-source-lsn")
                return sample
            except Refusal:
                self.require_standby(state, running=False)
                time.sleep(0.3)
        raise Refusal("replacement-start-or-replication-pending-retry-apply")

    def apply(self):
        require_root()
        source = self.verify_source()
        root_directory(self.scope, create=True)
        with locked(self.scope/"operation.lock", nonblocking=True):
            if (self.scope/"state.json").exists():
                state = self.state()
                require(state["owner"] == source["owner"] and state["system_identifier"] == source["system_identifier"] and
                        state["source_timeline"] == source["timeline"], "retry-source-identity-changed")
                self.require_standby(state, running=False)
            else:
                state = self.adopt_state(source)
            self.p["owner"] = state["owner"]
            secret = pathlib.Path(OLD_SCOPE)/"replication.pgpass"
            require(not secret.is_symlink() and secret.is_file() and secret.stat().st_uid == os.geteuid() and
                    stat.S_IMODE(secret.stat().st_mode) == 0o600, "original-pgpass-not-private")
            target = self.scope/"replication.pgpass"
            if target.exists():
                require(not target.is_symlink() and target.stat().st_uid == os.geteuid() and
                        stat.S_IMODE(target.stat().st_mode) == 0o600 and target.read_bytes() == secret.read_bytes(), "replication-secret-changed")
            else:
                atomic_private(target, secret.read_bytes())
            # A retry may already have renamed/stopped the original. The new
            # monitor can be active while the container is temporarily absent.
            if state["phase"] == "adoption-intent":
                self.install_monitor(state)
            else:
                self.ensure_monitor_installed(state)
            self.retire_old_monitor(state)
            self.start_new(state)
            state["authority_mode"] = "STANDBY"
            self.checkpoint(state, "standby-role-aware-ready")
            sample = self.monitor()
            require(sample["monitor_ok"], "role-aware-monitor-not-ready")
            return self.report(state, sample)

    def ensure_monitor_installed(self, state):
        expected = bundled_program({"action": "monitor"}, self.p["program"], LIBRARY_SOURCE)
        require((self.scope/"monitor.py").read_text() == expected, "authority-monitor-program-changed")
        for name, body in monitor_units().items():
            path = pathlib.Path(SYSTEM_UNITS)/name
            require(not path.is_symlink() and path.read_text() == body, "authority-monitor-unit-changed")
        self.run(["systemctl", "enable", "--now", UNIT+".timer"])
        self.run(["systemctl", "is-active", "--quiet", UNIT+".timer"])

    def report(self, state, sample):
        legacy = self.legacy_unit_status()
        active = self.run(["systemctl", "is-active", "--quiet", UNIT+".timer"], allow_failure=True).returncode == 0
        return {"phase": state["phase"], "authority_mode": state["authority_mode"], "owner": state["owner"],
                "system_identifier": state["system_identifier"], "source_timeline": state["source_timeline"],
                "container_id": state.get("container_id"), "volume": VOLUME, "sample": sample,
                "legacy_monitors_masked_idle": all(s["masked"] and s.get("MainPID") == "0" and
                    s.get("ActiveState") in ["inactive", "failed"] for s in legacy.values()),
                "role_aware_timer_active": active, "promoted": sample.get("in_recovery") is False,
                "promotion_executed_by_this_tool": False, "source_mutations": False}

    def inspect(self):
        prepared = self.prepared_state()
        if (self.scope/"state.json").exists():
            state = self.state()
            require(state["owner"] == prepared["owner"], "prepared-authority-owner-mismatch")
            return self.report(state, {**self.sample(state), "inspection_only": True})
        info = self.docker_inspect(CONTAINER)
        source = self.p["source"]
        require(info and prepared["owner"] == source["owner"] and prepared["system_identifier"] == source["system_identifier"],
                "prepared-source-identity-mismatch")
        volume = self.docker_inspect(VOLUME, kind="volume")
        require(volume is not None, "prepared-volume-missing")
        state = {"owner": prepared["owner"], "system_identifier": prepared["system_identifier"],
                 "source_timeline": source["timeline"], "image_id": prepared["image_id"],
                 "volume_path": volume["Mountpoint"], "original_container_id": info["Id"],
                 "legacy_parent_mount": [x for x in info["Mounts"] if x["Destination"] == "/var/lib/postgresql"],
                 "authority_mode": "ADOPTING", "promotion_intent": None}
        image = self.docker_inspect(state["image_id"], kind="image")
        require(image and self.image_identity(image) == source["image_identity"], "prepared-image-identity-mismatch")
        sample = self.sample(state)
        return {"adopted": False, "prepared_owner": prepared["owner"], "system_identifier": prepared["system_identifier"],
                "container_id": None if info is None else info["Id"], "legacy_monitors": self.legacy_unit_status(),
                "sample": sample, "standby_eligible": classify(state, sample)["healthy"],
                "mutations": False}


def source_inspect():
    # Read the existing manifest solely to select its owned replication role.
    LIB["private_dir"](LIB["SOURCE_SCOPE"])
    state = read_json(pathlib.Path(LIB["SOURCE_SCOPE"])/"state.json")
    remote = Base({"side": "source", "action": "inspect", "owner": state["owner"]})
    result = remote.source_status()
    result["owner"] = state["owner"]
    facts = remote.source_facts(full_size=False)
    image = remote.docker_inspect(facts["image_id"], kind="image")
    require(image is not None, "source-image-missing")
    result["image_identity"] = remote.image_identity(image)
    return result


def bundled_program(payload, program, library):
    return "REPLICA_LIBRARY = "+repr(library)+"\nREMOTE_PAYLOAD = "+repr(payload)+"\n"+program


def remote_main(payload):
    try:
        action = payload["action"]
        if action == "source-inspect":
            result = source_inspect()
        elif action == "inspect":
            result = Authority(payload).inspect()
        elif action == "apply":
            result = Authority(payload).apply()
        elif action == "monitor":
            result = Authority(payload).monitor()
        else:
            raise Refusal("invalid-authority-action")
        print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    except Refusal as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1)
    except Exception:
        print(json.dumps({"ok": False, "error": "authority-internal-error-inspect-private-state"}))
        raise SystemExit(1)


class Controller:
    def __init__(self):
        self.commands = LIB["channel_commands"]()
        self.program = pathlib.Path(__file__).read_text()

    def call(self, side, action, **extra):
        payload = {"action": action, **extra}
        if action == "apply":
            payload["program"] = self.program
        script = bundled_program(payload, self.program, LIBRARY_SOURCE)
        try:
            result = subprocess.run(self.commands[side]+["sudo -n python3 -"], input=script,
                                    text=True, capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise Refusal("ssh-timeout-remote-outcome-unknown-inspect-and-retry") from None
        try:
            response = json.loads(result.stdout)
        except ValueError:
            raise Refusal("invalid-remote-response-inspect-private-state") from None
        require(result.returncode == 0 and response.get("ok"), response.get("error", "authority-remote-failed"))
        return response["result"]


def plan():
    return {"dry_run": True, "remote_commands_executed": False, "promotes": False, "source_mutations": False,
            "container": CONTAINER, "retained_stopped_container": ARCHIVE, "same_volume": VOLUME,
            "actions": ["verify source/target physical replication identity read-only",
                "install root-owned role-aware monitor before legacy monitor retirement",
                "disable legacy target timer; await oneshot; persistently mask exact legacy units",
                "naturally stop and archive only the owned standby container; preserve its volume",
                "start role-aware specification with writable default but recovery-enforced read-only",
                "verify source streaming and role-aware disk/authority monitoring"], "mutation_gate": "--apply"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.inspect and not args.apply:
        print(json.dumps(plan(), indent=2))
        return 0
    controller = Controller()
    source = controller.call("source", "source-inspect")
    target = controller.call("target", "apply" if args.apply else "inspect", source=source)
    print(json.dumps({"source": source, "target": target, "mutations": args.apply}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    if "REMOTE_PAYLOAD" in globals():
        remote_main(REMOTE_PAYLOAD)
    else:
        try:
            raise SystemExit(main())
        except Refusal as exc:
            print(json.dumps({"ok": False, "error": str(exc), "no_automatic_rollback": True}), file=sys.stderr)
            raise SystemExit(1)
        except Exception:
            print(json.dumps({"ok": False, "error": "authority-local-error-no-secret-traceback"}), file=sys.stderr)
            raise SystemExit(1)
