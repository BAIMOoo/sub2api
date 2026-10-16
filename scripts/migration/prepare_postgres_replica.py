#!/usr/bin/env python3
"""Prepare an isolated PostgreSQL 18 physical standby; default is an offline dry run.

No promotion, business writes, source restart, app change, or DNS change exists
in this tool. Read POSTGRES_REPLICA.md before the explicitly gated --apply mode.
"""
import argparse
import base64
import datetime
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import pathlib
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time

RUN = "sub2api-pg-replica-20261016"
ROLE = "sub2api_mig_20261016_replica"
SLOT = "sub2api_mig_20261016"
CONTAINER = "sub2api-migration-pg-replica-20261016"
VOLUME = "sub2api-migration-pg-replica-data-20261016"
IMAGE = "postgres:migration-20261016-ff4a089dadda"
REVIEWED_CONFIG_DIGEST = "sha256:ff4a089dadda5e50a5f09604b2e2bc16f97d5befb6b05c328fc21159da96c602"
SOURCE_CONTAINER = "sub2api-postgres"
DATABASE = "sub2api"
ADMIN = "sub2api"
SOURCE_SCOPE = "/home/ubuntu/.local/share/sub2api-migration-20261016/postgres-replica"
TARGET_SCOPE = "/home/ubuntu/sub2api-migration-20261016/postgres-replica"
DATA = "/var/lib/postgresql/data"
PORT = 25432
CHANNEL_PORT = 15432
SOURCE_CHANNEL_ADDRESS = "172.18.0.1/32"
LABEL = "io.sub2api.migration.owner"
GIB = 1024**3
MIB = 1024**2
WAL_CAP_MB = 512
WAL_TRIP_BYTES = 384*MIB
DISK_TRIP_BYTES = 2*GIB
SAMPLE_SECONDS = 5
UNIT_SOURCE = RUN+"-source-monitor"
UNIT_TARGET = RUN+"-target-monitor"


class Refusal(RuntimeError):
    """An intentionally public, credential-free refusal."""


def require(condition, code):
    if not condition:
        raise Refusal(code)


def quoted(value):
    return "'"+str(value).replace("'", "''")+"'"


def private_dir(path, *, create=False):
    p = pathlib.Path(path)
    require(p.is_absolute(), "private-directory-must-be-absolute")
    # Refuse symlinks in any existing path component, including parents.
    for component in [p, *p.parents]:
        require(not component.is_symlink(), "private-directory-symlink")
    if create:
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(p.is_dir(), "private-directory-missing")
    require(stat.S_IMODE(p.stat().st_mode) == 0o700, "private-directory-not-0700")
    require(p.stat().st_uid == os.geteuid(), "private-directory-owner-mismatch")
    return p


def atomic_private(path, data, mode=0o600):
    path = pathlib.Path(path)
    require(not path.is_symlink(), "private-file-symlink")
    require(not path.exists() or path.is_file(), "private-file-not-regular")
    temporary = path.parent/("."+path.name+"."+secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data.encode() if isinstance(data, str) else data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)


def save_json(path, value):
    atomic_private(path, json.dumps(value, sort_keys=True)+"\n")


def read_json(path):
    path = pathlib.Path(path)
    require(not path.is_symlink(), "state-file-symlink")
    require(stat.S_IMODE(path.stat().st_mode) == 0o600, "state-file-not-0600")
    require(path.stat().st_uid == os.geteuid(), "state-file-owner-mismatch")
    return json.loads(path.read_text())


def scram_verifier(password, salt=None):
    salt = secrets.token_bytes(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 4096)
    client = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored = hashlib.sha256(client).digest()
    server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda x: base64.b64encode(x).decode()
    return "SCRAM-SHA-256$4096:"+b64(salt)+"$"+b64(stored)+":"+b64(server)


def hba_block(owner):
    require(len(owner) == 32 and all(c in "0123456789abcdef" for c in owner), "invalid-owner")
    return (f"# BEGIN {RUN} owner={owner}\n"
            f"host replication {ROLE} {SOURCE_CHANNEL_ADDRESS} scram-sha-256\n"
            f"# END {RUN} owner={owner}\n").encode()


def add_hba(old, owner):
    block = hba_block(owner)
    if block in old:
        require(old.count(block) == 1, "duplicate-owned-hba-block")
        return old
    require(RUN.encode() not in old, "foreign-hba-marker")
    # Prepend only this exact replication identity; existing rule order survives.
    return block+old


def remove_hba(old, owner):
    block = hba_block(owner)
    if block not in old:
        require(RUN.encode() not in old, "changed-owned-hba-block")
        return old
    require(old.count(block) == 1, "duplicate-owned-hba-block")
    return old.replace(block, b"", 1)


def backup_command():
    # PGPASSFILE is private; neither password nor password-bearing DSN is in argv.
    return ["pg_basebackup", "--host=127.0.0.1", f"--port={CHANNEL_PORT}",
            f"--username={ROLE}", f"--pgdata={DATA}", "--format=plain",
            "--wal-method=stream", f"--slot={SLOT}", "--write-recovery-conf",
            "--checkpoint=spread", "--max-rate=8M", "--manifest-checksums=SHA256",
            "--no-password"]


def bootstrap_shell():
    backup = shlex.join(backup_command())
    verify = shlex.join(["pg_verifybackup", "--exit-on-error", DATA])
    postgres = shlex.join(["postgres", "-D", DATA,
        "-c", "listen_addresses=127.0.0.1", "-c", f"port={PORT}",
        "-c", "unix_socket_directories=/tmp", "-c", "hot_standby=on",
        "-c", "default_transaction_read_only=on", "-c", "ssl=off",
        "-c", "shared_buffers=128MB", "-c", "archive_mode=off",
        "-c", "archive_command=", "-c", "restore_command=",
        "-c", "recovery_target_timeline=latest"])
    return f'''set -eu
umask 077
if command -v gosu >/dev/null 2>&1; then drop=gosu
elif command -v su-exec >/dev/null 2>&1; then drop=su-exec
else exit 71; fi
mkdir -p /run/sub2api-migration {DATA}
cp /run/replication-input.pgpass /run/sub2api-migration/replication.pgpass
chmod 700 /run/sub2api-migration
chmod 600 /run/sub2api-migration/replication.pgpass
chown -R postgres:postgres /run/sub2api-migration
chown postgres:postgres {DATA}
chmod 700 {DATA}
export PGDATA={DATA}
export PGPASSFILE=/run/sub2api-migration/replication.pgpass
export PGAPPNAME={RUN}
export PGSSLMODE=disable
export PGCONNECT_TIMEOUT=10
if [ ! -f {DATA}/.sub2api-seed-verified ]; then
    [ -z "$(ls -A {DATA})" ] || exit 72
    "$drop" postgres {backup}
    "$drop" postgres {verify}
    [ -f {DATA}/standby.signal ] || exit 73
    touch {DATA}/.sub2api-seed-verified
    chown postgres:postgres {DATA}/.sub2api-seed-verified
    sync
fi
[ -f {DATA}/standby.signal ] || exit 74
exec "$drop" postgres {postgres}
'''


def container_command(owner, scope):
    return ["docker", "create", "--name", CONTAINER,
        "--label", LABEL+"="+owner, "--label", "io.sub2api.migration.run="+RUN,
        "--network", "host", "--memory", "768m", "--memory-swap", "768m",
        "--cpus", "1", "--pids-limit", "128", "--restart", "no",
        "--security-opt", "no-new-privileges:true", "--cap-drop", "NET_RAW",
        "--tmpfs", "/run/sub2api-migration:rw,noexec,nosuid,size=1m,mode=0700",
        "--mount", f"type=volume,src={VOLUME},dst={DATA},volume-nocopy",
        "--mount", f"type=bind,src={scope}/replication.pgpass,dst=/run/replication-input.pgpass,readonly",
        "--entrypoint", "/bin/sh", IMAGE, "-ceu", bootstrap_shell()]


def monitor_units(side, scope):
    name = UNIT_SOURCE if side == "source" else UNIT_TARGET
    service = f'''[Unit]
Description=Sub2API owned PostgreSQL replica {side} safety monitor
After=docker.service
{('OnFailure='+UNIT_SOURCE+'-fence.service') if side == 'source' else ''}
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {scope}/monitor.py
User=root
UMask=0077
TimeoutStartSec=30
NoNewPrivileges=true
PrivateTmp=true
'''
    timer = f'''[Unit]
Description=Sub2API owned PostgreSQL replica {side} safety timer
{('OnFailure='+UNIT_SOURCE+'-fence.service') if side == 'source' else ''}
[Timer]
OnBootSec=1
OnUnitActiveSec={SAMPLE_SECONDS}
AccuracySec=1
Unit={name}.service
[Install]
WantedBy=timers.target
'''
    return name, service, timer


def failure_fence_unit(scope):
    return f'''[Unit]
Description=Sub2API owned PostgreSQL replica emergency source fence
After=docker.service
StartLimitIntervalSec=0
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {scope}/emergency-fence.py
User=root
UMask=0077
TimeoutStartSec=20
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
'''


class Remote:
    """All subprocess output stays private; exceptions contain fixed codes only."""
    def __init__(self, payload):
        self.p = payload
        self.side = payload["side"]
        self.scope = pathlib.Path(SOURCE_SCOPE if self.side == "source" else TARGET_SCOPE)
        self.log = self.scope/"private-errors.log"

    def run(self, argv, data=None, *, timeout=30, allow_failure=False):
        try:
            result = subprocess.run(argv, input=data, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.record_error(exc.stderr or "command timed out")
            raise Refusal("remote-command-timeout") from None
        if result.returncode:
            self.record_error(result.stderr)
            if not allow_failure:
                raise Refusal("remote-command-failed")
        return result

    def record_error(self, message):
        # Inspect never creates remote files. Apply/status can retain private diagnostics.
        if self.p["action"] == "inspect" or not self.scope.is_dir():
            return
        require(not self.log.is_symlink(), "private-log-symlink")
        if self.log.exists() and self.log.stat().st_size > 8*MIB:
            os.replace(self.log, self.scope/"private-errors.previous.log")
        fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "ab") as out:
            out.write((message.encode() if isinstance(message, str) else message)+b"\n")

    def sql(self, query, *, target=False):
        name = CONTAINER if target else SOURCE_CONTAINER
        args = ["docker", "exec", "-i", name, "psql", "-X", "-A", "-t", "-w",
                "-v", "ON_ERROR_STOP=1", "-U", ADMIN, "-d", DATABASE]
        if target:
            args += ["-h", "/tmp", "-p", str(PORT)]
        return self.run(args, "SET statement_timeout='8s';\n"+query, timeout=15).stdout.strip()

    def query(self, query, *, target=False):
        # -q suppresses SET's command tag; the JSON is the last output line.
        output = self.sql(query, target=target)
        return json.loads(output.splitlines()[-1])

    def docker_inspect(self, name, *, kind="container", timeout=10):
        result = self.run(["docker", kind, "inspect", name], allow_failure=True, timeout=timeout)
        if result.returncode:
            require("no such" in result.stderr.lower() or "not found" in result.stderr.lower(), "docker-inspect-failed")
            return None
        return json.loads(result.stdout)[0]

    def image_identity(self, image):
        # Docker 28 classic .Id is a Config digest; Docker 29 containerd .Id
        # can be a manifest digest. Compare Config + ordered uncompressed
        # RootFS DiffIDs rather than those incompatible local identifiers.
        local_id = image["Id"]
        layers = image["RootFS"]["Layers"]
        require(layers and all(len(x) == 71 and x.startswith("sha256:") for x in layers), "invalid-image-rootfs-diffids")
        config_digest = local_id
        if local_id != REVIEWED_CONFIG_DIGEST:
            def content(digest):
                result = self.run(["ctr", "-n", "moby", "content", "get", digest])
                require("sha256:"+hashlib.sha256(result.stdout.encode()).hexdigest() == digest, "containerd-content-digest-mismatch")
                return json.loads(result.stdout)
            manifest = content(local_id)
            if "manifests" in manifest:
                matches = [item for item in manifest["manifests"] if item.get("platform", {}).get("architecture") == image["Architecture"] and
                           item.get("platform", {}).get("os") == image.get("Os", "linux")]
                require(len(matches) == 1, "ambiguous-containerd-platform-manifest")
                manifest = content(matches[0]["digest"])
            config_digest = manifest.get("config", {}).get("digest", "")
            require(config_digest == REVIEWED_CONFIG_DIGEST, "unreviewed-postgres-image-config")
            config = content(config_digest)
            require(config.get("rootfs", {}).get("diff_ids") == layers, "containerd-inspect-rootfs-mismatch")
        require(config_digest == REVIEWED_CONFIG_DIGEST, "unreviewed-postgres-image-config")
        return {"config_digest": config_digest, "rootfs_diff_ids": layers,
                "architecture": image["Architecture"], "os": image.get("Os", "linux")}

    def state(self, *, create=False, allow_cleaned=False):
        private_dir(self.scope, create=create)
        path = self.scope/"state.json"
        if path.exists():
            state = read_json(path)
            require(state["owner"] == self.p["owner"] and state["run"] == RUN, "remote-state-owner-mismatch")
            require(allow_cleaned or not state.get("cleaned"), "remote-state-already-cleaned")
            return state
        require(create, "remote-state-missing")
        state = {"owner": self.p["owner"], "run": RUN, "side": self.side, "created_at": time.time(), "phase": "created"}
        save_json(path, state)
        return state

    def checkpoint(self, state, phase):
        state["phase"] = phase
        state["updated_at"] = time.time()
        save_json(self.scope/"state.json", state)

    def no_trip(self):
        require(not (self.scope/"safety-tripped.json").exists(), "safety-monitor-tripped-explicit-cleanup-required")

    def wait_wal_setting(self, expected, error):
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            if self.query("SELECT to_json(setting) FROM pg_settings WHERE name='max_slot_wal_keep_size';") == expected:
                return
            time.sleep(0.1)
        raise Refusal(error)

    def owned_container(self):
        info = self.docker_inspect(CONTAINER)
        if info:
            require(info["Config"].get("Labels", {}).get(LABEL) == self.p["owner"], "target-container-owner-mismatch")
            require(info["Config"].get("Labels", {}).get("io.sub2api.migration.run") == RUN, "target-container-run-mismatch")
        return info

    def owned_volume(self):
        info = self.docker_inspect(VOLUME, kind="volume")
        if info:
            require(info.get("Labels", {}).get(LABEL) == self.p["owner"], "target-volume-owner-mismatch")
        return info

    def map_source_file(self, container, filename):
        matches = []
        for mount in container["Mounts"]:
            dest = pathlib.PurePosixPath(mount["Destination"])
            try:
                relative = pathlib.PurePosixPath(filename).relative_to(dest)
                matches.append((len(dest.parts), pathlib.Path(mount["Source"])/str(relative)))
            except ValueError:
                pass
        require(matches, "source-file-not-on-persistent-mount")
        path = sorted(matches, key=lambda item: item[0])[-1][1]
        require(not path.is_symlink(), "source-file-symlink")
        require(not any(m["Destination"] == filename and path.is_file() for m in container["Mounts"]),
                "direct-file-bind-mount-needs-reviewed-reload-strategy")
        return path

    def source_facts(self, *, full_size=True):
        container = self.docker_inspect(SOURCE_CONTAINER)
        require(container and container["State"]["Running"], "source-postgres-not-running")
        facts = self.query("""SELECT json_build_object(
'version_num',current_setting('server_version_num')::int,
'admin',current_user,'admin_superuser',(SELECT rolsuper FROM pg_roles WHERE rolname=current_user),
'in_recovery',pg_is_in_recovery(),'transaction_read_only',current_setting('transaction_read_only'),
'default_transaction_read_only',current_setting('default_transaction_read_only'),
'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
'timeline',(SELECT timeline_id FROM pg_control_checkpoint()),
'data_directory',current_setting('data_directory'),'hba_file',current_setting('hba_file'),
'config_file',current_setting('config_file'),
'wal_level',current_setting('wal_level'),'max_wal_senders',current_setting('max_wal_senders')::int,
'max_replication_slots',current_setting('max_replication_slots')::int,
'ssl',current_setting('ssl'),'archive_mode',current_setting('archive_mode'),
'max_slot_wal_keep_size',(SELECT setting FROM pg_settings WHERE name='max_slot_wal_keep_size'),
'max_slot_wal_keep_pending_restart',(SELECT pending_restart FROM pg_settings WHERE name='max_slot_wal_keep_size'),
'wal_lsn',pg_current_wal_lsn()::text,'wal_bytes',(SELECT wal_bytes::text FROM pg_stat_wal),
'active_other_sessions',(SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND state='active'),
'custom_tablespaces',(SELECT coalesce(json_agg(spcname),'[]'::json) FROM pg_tablespace WHERE pg_tablespace_location(oid)<>''),
'file_setting_errors',(SELECT count(*) FROM pg_file_settings WHERE error IS NOT NULL),
'auto_wal_settings',(SELECT coalesce(json_agg(json_build_object('setting',setting,'applied',applied)),'[]'::json) FROM pg_file_settings WHERE sourcefile=current_setting('data_directory')||'/postgresql.auto.conf' AND name='max_slot_wal_keep_size'),
'hba_errors',(SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL),
'slots',(SELECT coalesce(json_agg(json_build_object('name',slot_name,'type',slot_type,'active',active,'active_pid',active_pid,'restart_lsn',restart_lsn::text,'wal_status',wal_status,'safe_wal_size',safe_wal_size,'retained_bytes',coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn),0))),'[]'::json) FROM pg_replication_slots),
'replicas',(SELECT coalesce(json_agg(json_build_object('application_name',application_name,'user',usename,'client_addr',client_addr::text,'state',state,'sync_state',sync_state,'replay_lsn',replay_lsn::text,'lag_bytes',coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(),replay_lsn),0))),'[]'::json) FROM pg_stat_replication),
'role',(SELECT json_build_object('name',rolname,'superuser',rolsuper,'replication',rolreplication,'login',rolcanlogin,'createdb',rolcreatedb,'createrole',rolcreaterole,'bypassrls',rolbypassrls,'comment',shobj_description(oid,'pg_authid')) FROM pg_roles WHERE rolname='"""+ROLE+"""')
);""")
        data = self.map_source_file(container, facts["data_directory"])
        hba = self.map_source_file(container, facts["hba_file"])
        require(data.is_dir() and hba.is_file(), "source-persistent-path-invalid")
        require(not (data/"pg_wal").is_symlink(), "external-wal-directory-needs-reviewed-mapping")
        facts.update(container_id=container["Id"], image_id=container["Image"], source_free_bytes=shutil.disk_usage(data).free,
                     hba_sha256=hashlib.sha256(hba.read_bytes()).hexdigest(), sampled_at=time.time(), data_host_path=str(data))
        # No full backup or archive is written to the source. Size reads only.
        total, wal = 0, 0
        for parent, dirs, files in os.walk(data if full_size else data/"pg_wal", followlinks=False):
            for filename in files:
                p = pathlib.Path(parent)/filename
                try:
                    if not p.is_symlink():
                        size = p.stat().st_size
                        total += size
                        if "pg_wal" in p.relative_to(data).parts:
                            wal += size
                except FileNotFoundError:
                    pass
        facts.update(pgdata_bytes=total, pgwal_bytes=wal)
        if full_size:
            image = self.docker_inspect(container["Image"], kind="image")
            require(image is not None, "source-image-inspection-failed")
            facts["image_identity"] = self.image_identity(image)
            facts["postgres_uid"] = int(self.run(["docker", "exec", SOURCE_CONTAINER, "id", "-u", "postgres"]).stdout.strip())
            facts["postgres_gid"] = int(self.run(["docker", "exec", SOURCE_CONTAINER, "id", "-g", "postgres"]).stdout.strip())
            for binary in ["pg_basebackup", "pg_verifybackup"]:
                version = self.run(["docker", "exec", SOURCE_CONTAINER, binary, "--version"]).stdout.strip()
                require("18.3" in version, "physical-backup-binary-version-mismatch")
                facts[binary+"_version"] = version
        return facts

    def target_facts(self):
        root = pathlib.Path(TARGET_SCOPE).parent
        require(root.is_dir() and not root.is_symlink(), "target-stage-scope-missing")
        require(stat.S_IMODE(root.stat().st_mode) == 0o700, "target-stage-scope-not-0700")
        image = self.docker_inspect(IMAGE, kind="image")
        require(image, "locked-target-image-missing")
        container = self.docker_inspect(CONTAINER)
        volume = self.docker_inspect(VOLUME, kind="volume")
        bound = False
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", PORT))
            except OSError:
                bound = True
        mem = {}
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            mem[key] = int(value.strip().split()[0])*1024
        channel = False
        try:
            with socket.create_connection(("127.0.0.1", CHANNEL_PORT), timeout=3):
                channel = True
        except OSError:
            pass
        return {"image_id": image["Id"], "image_identity": self.image_identity(image), "architecture": image["Architecture"],
                "target_free_bytes": shutil.disk_usage(root).free, "memory_total_bytes": mem["MemTotal"],
                "memory_available_bytes": mem["MemAvailable"], "port_bound": bound,
                "source_pg_channel_tcp": channel,
                "container_exists": container is not None, "container_owner": (container or {}).get("Config", {}).get("Labels", {}).get(LABEL),
                "volume_exists": volume is not None, "volume_owner": (volume or {}).get("Labels", {}).get(LABEL)}

    def validate_source(self, facts):
        require(facts["version_num"] == 180003, "source-version-is-not-reviewed-18.3")
        require(facts["admin"] == ADMIN and facts["admin_superuser"], "source-admin-role-not-reviewed-superuser")
        require(not facts["in_recovery"] and facts["transaction_read_only"] == "off" and facts["default_transaction_read_only"] == "off", "source-not-writable-primary")
        require(facts["data_directory"] == DATA, "unexpected-source-data-directory")
        require(facts["hba_file"] == DATA+"/pg_hba.conf" and facts["config_file"] == DATA+"/postgresql.conf", "external-config-needs-reviewed-mapping")
        require(facts["wal_level"] == "replica" and facts["max_wal_senders"] >= 3 and facts["max_replication_slots"] >= 1, "source-replication-config-insufficient")
        require(facts["ssl"] == "off" and facts["archive_mode"] == "off", "unreviewed-source-ssl-or-archive-config")
        require(not facts["custom_tablespaces"], "custom-tablespaces-need-reviewed-mappings")
        require(facts["file_setting_errors"] == 0 and facts["hba_errors"] == 0, "source-config-already-invalid")
        require(not facts["max_slot_wal_keep_pending_restart"], "source-replication-setting-pending-restart")
        require(facts["source_free_bytes"] >= DISK_TRIP_BYTES+GIB, "source-disk-margin-insufficient")
        require(all(s["name"] == SLOT and s["type"] == "physical" for s in facts["slots"]), "other-source-slots-present")
        require(all(r["user"] == ROLE and r["application_name"] == RUN for r in facts["replicas"]), "other-source-replicas-present")

    def role_owned(self, facts):
        role = facts.get("role")
        if role:
            require(role["comment"] == "migration-owner:"+self.p["owner"], "source-role-owner-mismatch")
            require(role["replication"] and not any(role[k] for k in ["superuser", "createdb", "createrole", "bypassrls"]), "source-role-privileges-changed")
        return role

    def install_monitor(self, state):
        name, service, timer = monitor_units(self.side, str(self.scope))
        monitor_payload = {k: self.p[k] for k in ["side", "owner"]}
        monitor_payload["action"] = "monitor"
        source = "REMOTE_PAYLOAD = "+repr(monitor_payload)+"\n"+self.p["program"]
        atomic_private(self.scope/"monitor.py", source, 0o700)
        units = {name+".service": service, name+".timer": timer}
        if self.side == "source":
            units[name+"-fence.service"] = failure_fence_unit(str(self.scope))
            fence_payload = {"side": "source", "owner": self.p["owner"], "action": "fence"}
            atomic_private(self.scope/"emergency-fence.py", "REMOTE_PAYLOAD = "+repr(fence_payload)+"\n"+self.p["program"], 0o700)
        for filename, body in units.items():
            path = pathlib.Path("/etc/systemd/system")/filename
            require(not path.is_symlink(), "monitor-unit-symlink")
            require(not path.exists() or path.read_text() == body, "existing-monitor-unit-differs")
            atomic_private(path, body, 0o644)
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "enable", "--now", name+".timer"])
        self.run(["systemctl", "start", name+".service"])
        self.run(["systemctl", "is-active", "--quiet", name+".timer"])
        sample = read_json(self.scope/"monitor-status.json")
        require(time.time()-sample["sampled_at"] < 30 and sample.get("monitor_ok"), "independent-monitor-not-healthy")
        self.no_trip()
        self.checkpoint(state, "monitor-installed")

    def source_prepare(self):
        facts = self.source_facts()
        self.validate_source(facts)
        state = self.state(create=True)
        self.no_trip()
        self.role_owned(facts)
        if "system_identifier" in state:
            require(state["system_identifier"] == facts["system_identifier"] and state["container_id"] == facts["container_id"] and state["timeline"] == facts["timeline"], "source-identity-changed")
        else:
            require(not facts["slots"] and not facts["role"], "source-object-exists-without-owned-state")
            require(len(facts["auto_wal_settings"]) <= 1, "ambiguous-auto-conf-wal-setting")
            state.update(system_identifier=facts["system_identifier"], container_id=facts["container_id"], image_id=facts["image_id"], image_identity=facts["image_identity"], timeline=facts["timeline"], data_host_path=facts["data_host_path"],
                         old_wal_setting=facts["max_slot_wal_keep_size"], old_auto_wal=facts["auto_wal_settings"], hba_file=facts["hba_file"],
                         hba_before_sha256=facts["hba_sha256"], guc_changed=False)
            self.checkpoint(state, "source-identity-recorded")
        self.install_monitor(state)  # Must precede any slot that retains WAL.
        if int(facts["max_slot_wal_keep_size"]) < 0 or int(facts["max_slot_wal_keep_size"]) > WAL_CAP_MB:
            # Save intent BEFORE ALTER SYSTEM so an interrupted reload is recoverable.
            state["guc_changed"] = True
            state["our_wal_setting"] = str(WAL_CAP_MB)
            self.checkpoint(state, "wal-cap-change-intended")
            self.sql(f"ALTER SYSTEM SET max_slot_wal_keep_size = '{WAL_CAP_MB}MB';")
            self.query("SELECT to_json(pg_reload_conf());")
            self.wait_wal_setting(str(WAL_CAP_MB), "bounded-wal-setting-not-effective-no-slot-created")
        bounded = int(self.query("SELECT to_json(setting) FROM pg_settings WHERE name='max_slot_wal_keep_size';"))
        require(0 < bounded <= WAL_CAP_MB, "source-wal-cap-not-finite-positive")
        self.checkpoint(state, "wal-cap-bounded")
        self.no_trip()
        if not facts["role"]:
            verifier = self.p["verifier"]
            require(verifier.startswith("SCRAM-SHA-256$4096:"), "invalid-scram-verifier")
            expiry = (datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=30)).isoformat()
            self.sql(f"BEGIN; CREATE ROLE {ROLE} WITH LOGIN REPLICATION NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS CONNECTION LIMIT 4 PASSWORD {quoted(verifier)} VALID UNTIL {quoted(expiry)};\nCOMMENT ON ROLE {ROLE} IS {quoted('migration-owner:'+self.p['owner'])}; COMMIT;")
        require(self.query(f"SELECT to_json(rolpassword={quoted(self.p['verifier'])} AND rolcanlogin) FROM pg_authid WHERE rolname={quoted(ROLE)};"), "replication-credential-or-login-mismatch")
        self.checkpoint(state, "replication-role-ready")
        container = self.docker_inspect(SOURCE_CONTAINER)
        hba = self.map_source_file(container, state["hba_file"])
        original = hba.read_bytes()
        if hba_block(self.p["owner"]) not in original:
            require(hashlib.sha256(original).hexdigest() == state["hba_before_sha256"], "source-hba-changed-before-install")
            atomic_private(self.scope/"pg_hba.before", original)
            self.write_hba(hba, add_hba(original, self.p["owner"]))
        require(self.query("SELECT to_json(count(*)) FROM pg_hba_file_rules WHERE error IS NOT NULL;") == 0, "new-hba-rule-invalid")
        require(self.query("SELECT to_json(pg_reload_conf());"), "source-hba-reload-failed")
        self.checkpoint(state, "replication-hba-ready")
        self.no_trip()
        slots = self.query("SELECT coalesce(json_agg(slot_name),'[]'::json) FROM pg_replication_slots;")
        require(all(name == SLOT for name in slots), "other-source-slot-appeared")
        if SLOT not in slots:
            require(not state.get("slot_created"), "owned-slot-disappeared-explicit-reseed-required")
            state["slot_create_intended"] = True
            self.checkpoint(state, "slot-create-intended")
            self.sql(f"SELECT pg_create_physical_replication_slot({quoted(SLOT)}, true);")
        state["slot_created"] = True
        self.checkpoint(state, "source-prepared")
        return {"phase": state["phase"], "system_identifier": state["system_identifier"], "image_identity": state["image_identity"], "wal_cap_mb": bounded, "monitor": "active"}

    def write_hba(self, path, content):
        # Replace inside the existing parent and preserve owner/mode exactly.
        before = path.stat()
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "unsafe-hba-file")
        temporary = path.parent/("."+path.name+"."+self.p["owner"])
        require(not temporary.exists() and not temporary.is_symlink(), "hba-temp-already-exists")
        try:
            atomic_private(temporary, content, stat.S_IMODE(before.st_mode))
            os.chown(temporary, before.st_uid, before.st_gid)
            os.chmod(temporary, stat.S_IMODE(before.st_mode))
            fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    def target_prepare(self):
        facts = self.target_facts()
        require(facts["image_identity"] == self.p["source_image_identity"], "target-image-is-not-exact-source-content")
        require(facts["source_pg_channel_tcp"], "encrypted-source-pg-channel-unreachable")
        require(facts["target_free_bytes"] >= (DISK_TRIP_BYTES if facts["volume_exists"] else self.p["pgdata_bytes"]+4*GIB), "target-disk-margin-insufficient")
        require(facts["memory_total_bytes"] >= 1800*MIB and (facts["container_exists"] or facts["memory_available_bytes"] >= 900*MIB), "target-memory-margin-insufficient")
        state = self.state(create=True)
        self.no_trip()
        info = self.owned_container()
        volume = self.owned_volume()
        require(not facts["port_bound"] or info is not None, "target-25432-is-already-in-use")
        if "system_identifier" in state:
            require(state["system_identifier"] == self.p["system_identifier"] and state["image_id"] == facts["image_id"], "target-stored-identity-mismatch")
        else:
            require(info is None and volume is None, "target-objects-exist-without-owned-state")
            state.update(system_identifier=self.p["system_identifier"], image_id=facts["image_id"])
            self.checkpoint(state, "target-identity-recorded")
        password = self.p["password"]
        require(all(c.isalnum() or c in "-_" for c in password) and len(password) >= 40, "invalid-replication-secret")
        atomic_private(self.scope/"replication.pgpass", f"127.0.0.1:{CHANNEL_PORT}:replication:{ROLE}:{password}\n")
        self.install_monitor(state)
        if not volume:
            self.run(["docker", "volume", "create", "--label", LABEL+"="+self.p["owner"], "--label", "io.sub2api.migration.run="+RUN, VOLUME])
        if info is None:
            self.checkpoint(state, "container-create-intended")
            self.run(container_command(self.p["owner"], str(self.scope)))
            self.checkpoint(state, "container-created")
        else:
            require(info["Image"] == state["image_id"] and info["HostConfig"]["NetworkMode"] == "host", "owned-container-config-changed")
            require(info["HostConfig"]["Memory"] == 768*MIB and info["HostConfig"]["MemorySwap"] == 768*MIB and
                    not info["HostConfig"].get("PortBindings") and info["Config"]["Entrypoint"] == ["/bin/sh"] and
                    info["Config"]["Cmd"] == ["-ceu", bootstrap_shell()], "owned-container-resource-or-command-changed")
            require(info["State"]["Status"] in ["created", "running"], "stopped-or-failed-seed-needs-explicit-cleanup")
        if info is None or not info["State"]["Running"]:
            self.run(["docker", "start", CONTAINER])
        self.checkpoint(state, "seeding-or-streaming")
        return {"phase": state["phase"], "container": CONTAINER, "volume": VOLUME, "port": "127.0.0.1:"+str(PORT), "promoted": False}

    def source_status(self):
        state = self.state()
        facts = self.source_facts(full_size=False)
        require(facts["system_identifier"] == state["system_identifier"] and facts["container_id"] == state["container_id"] and state["timeline"] == facts["timeline"], "source-identity-changed")
        self.role_owned(facts)
        return {"sampled_at": time.time(), "system_identifier": facts["system_identifier"], "timeline": facts["timeline"], "in_recovery": facts["in_recovery"],
                "source_free_bytes": facts["source_free_bytes"], "pgwal_bytes": facts["pgwal_bytes"],
                "wal_lsn": facts["wal_lsn"], "wal_cap_mb": int(facts["max_slot_wal_keep_size"]),
                "slots": facts["slots"], "replicas": facts["replicas"], "role_login": bool((facts.get("role") or {}).get("login")),
                "safety_tripped": (self.scope/"safety-tripped.json").exists(), "monitor": self.monitor_health()}

    def monitor_health(self):
        name = UNIT_SOURCE if self.side == "source" else UNIT_TARGET
        active = self.run(["systemctl", "is-active", "--quiet", name+".timer"], allow_failure=True).returncode == 0
        try:
            sample = read_json(self.scope/"monitor-status.json")
            age = max(0, time.time()-sample["sampled_at"])
            healthy = active and age <= 20 and sample.get("monitor_ok") is True
        except (Refusal, OSError, ValueError, KeyError):
            age, healthy = None, False
        return {"timer_active": active, "sample_age_seconds": age, "healthy": healthy}

    def target_status(self):
        state = self.state()
        info = self.owned_container()
        out = {"sampled_at": time.time(), "target_free_bytes": shutil.disk_usage(self.scope).free,
               "container_state": None if info is None else info["State"]["Status"], "standby_ready": False,
               "safety_tripped": (self.scope/"safety-tripped.json").exists(), "monitor": self.monitor_health()}
        if info is None or not info["State"]["Running"]:
            return out
        # During basebackup the diagnostic socket does not exist yet.
        probe = self.run(["docker", "exec", CONTAINER, "test", "-f", DATA+"/.sub2api-seed-verified"], allow_failure=True)
        if probe.returncode:
            out["phase"] = "basebackup-or-verification"
            return out
        try:
            pg = self.query("""SELECT json_build_object(
'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
'in_recovery',pg_is_in_recovery(),'transaction_read_only',current_setting('transaction_read_only'),
'default_transaction_read_only',current_setting('default_transaction_read_only'),
'version_num',current_setting('server_version_num')::int,
'listen_addresses',current_setting('listen_addresses'),'port',current_setting('port')::int,
'primary_slot_name',current_setting('primary_slot_name'),
'receive_lsn',pg_last_wal_receive_lsn()::text,'replay_lsn',pg_last_wal_replay_lsn()::text,
'receiver_status',(SELECT status FROM pg_stat_wal_receiver LIMIT 1));""", target=True)
        except Refusal:
            out["phase"] = "standby-starting-or-diagnostic-failed"
            return out
        out.update(pg)
        out["standby_ready"] = (pg["system_identifier"] == state["system_identifier"] and pg["in_recovery"] and
            pg["transaction_read_only"] == "on" and pg["default_transaction_read_only"] == "on" and
            pg["version_num"] == 180003 and pg["listen_addresses"] == "127.0.0.1" and pg["port"] == PORT and
            pg["primary_slot_name"] == SLOT and pg["receiver_status"] == "streaming" and pg["replay_lsn"] is not None)
        return out

    def monitor(self):
        state = self.state()
        try:
            if self.side == "source":
                # Disk protection does not depend on the large stats query,
                # HBA metadata, systemctl, or a second full facts collection.
                if shutil.disk_usage(state["data_host_path"]).free < DISK_TRIP_BYTES:
                    self.emergency_fence("source-disk-low-before-sampling")
                sample = self.source_status()
                slots = [s for s in sample["slots"] if s["name"] == SLOT]
                reason = None
                if sample["source_free_bytes"] < DISK_TRIP_BYTES:
                    reason = "source-disk-low"
                elif slots and (slots[0]["retained_bytes"] >= WAL_TRIP_BYTES or slots[0]["wal_status"] in ["lost", "unreserved"]):
                    reason = "owned-slot-wal-budget"
                elif slots and not 0 < sample["wal_cap_mb"] <= WAL_CAP_MB:
                    reason = "owned-slot-wal-cap-became-unbounded"
                elif sample["in_recovery"]:
                    reason = "source-no-longer-primary"
                if reason or sample["safety_tripped"]:
                    if not sample["safety_tripped"]:
                        save_json(self.scope/"safety-tripped.json", {"reason": reason, "at": time.time()})
                    self.fence_source_objects(drop_slot=True)
                    sample["safety_tripped"] = True
                sample["monitor_ok"] = not sample["safety_tripped"]
            else:
                sample = self.target_status()
                # A receiver reconnect/lag temporarily makes readiness false;
                # it does not violate standby identity or justify stopping it.
                invariant_ok = (sample.get("in_recovery") is True and sample.get("transaction_read_only") == "on" and
                    sample.get("default_transaction_read_only") == "on" and sample.get("listen_addresses") == "127.0.0.1" and
                    sample.get("port") == PORT and sample.get("primary_slot_name") == SLOT and
                    sample.get("system_identifier") == self.state()["system_identifier"] and sample.get("version_num") == 180003)
                unsafe = sample["target_free_bytes"] < DISK_TRIP_BYTES or ("in_recovery" in sample and not invariant_ok)
                if unsafe:
                    save_json(self.scope/"safety-tripped.json", {"reason": "target-standby-invariant-or-disk", "at": time.time()})
                    info = self.owned_container()
                    if info and info["State"]["Running"]:
                        self.run(["docker", "stop", "--time", "20", CONTAINER], timeout=25)
                    sample["safety_tripped"] = True
                sample["monitor_ok"] = not sample["safety_tripped"]
            save_json(self.scope/"monitor-status.json", sample)
            # Append bounded structured telemetry; raw psql/docker output is never logged here.
            log = self.scope/"telemetry.jsonl"
            if log.exists() and log.stat().st_size > 8*MIB:
                os.replace(log, self.scope/"telemetry.previous.jsonl")
            fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as out:
                out.write(json.dumps(sample, sort_keys=True)+"\n")
            return {"monitor_ok": sample["monitor_ok"]}
        except Exception:
            try:
                save_json(self.scope/"monitor-status.json", {"sampled_at": time.time(), "monitor_ok": False, "error": "monitor-sample-failed-see-private-errors"})
            except OSError:
                pass
            if self.side == "source":
                try:
                    self.emergency_fence("source-monitor-sampling-failed")
                except Exception:
                    # The unit's independent OnFailure service retries this
                    # minimal fence even if the main monitor timed out/crashed.
                    self.record_error("emergency fence did not complete; independent service must retry")
            raise Refusal("monitor-sample-failed") from None

    def emergency_fence(self, reason):
        require(self.side == "source", "emergency-fence-is-source-only")
        self.state()
        try:
            if not (self.scope/"safety-tripped.json").exists():
                save_json(self.scope/"safety-tripped.json", {"reason": reason, "at": time.time()})
        except OSError:
            pass  # A full filesystem must not prevent attempting the fence.
        self.fence_source_objects(drop_slot=True)
        return {"fenced": True, "side": "source", "promoted": False}

    def guard_sql(self, query, *, as_json=False):
        argv = ["docker", "exec", "-i", SOURCE_CONTAINER, "psql", "-X", "-A", "-t", "-w",
                "-v", "ON_ERROR_STOP=1", "-U", ADMIN, "-d", DATABASE]
        output = self.run(argv, "SET statement_timeout='1500ms'; SET lock_timeout='500ms';\n"+query, timeout=3).stdout.strip()
        return json.loads(output.splitlines()[-1]) if as_json else output

    def fence_source_objects(self, *, drop_slot):
        state = self.state()
        container = self.docker_inspect(SOURCE_CONTAINER, timeout=3)
        require(container and container["Id"] == state["container_id"] and container["Image"] == state["image_id"], "source-container-identity-changed")
        facts = self.guard_sql("""SELECT json_build_object(
'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
'timeline',(SELECT timeline_id FROM pg_control_checkpoint()),'in_recovery',pg_is_in_recovery(),
'role',(SELECT json_build_object('comment',shobj_description(oid,'pg_authid'),'replication',rolreplication,
'superuser',rolsuper,'createdb',rolcreatedb,'createrole',rolcreaterole,'bypassrls',rolbypassrls) FROM pg_roles WHERE rolname='"""+ROLE+"""'),
'slots',(SELECT coalesce(json_agg(json_build_object('name',slot_name,'type',slot_type)),'[]'::json) FROM pg_replication_slots WHERE slot_name='"""+SLOT+"""'));""", as_json=True)
        require(facts["system_identifier"] == state["system_identifier"] and state["timeline"] == facts["timeline"] and not facts["in_recovery"], "source-cluster-identity-changed")
        role = self.role_owned(facts)
        if role:
            self.guard_sql(f"ALTER ROLE {ROLE} NOLOGIN;")
            self.guard_sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename={quoted(ROLE)} AND backend_type='walsender';")
        if drop_slot and any(s["name"] == SLOT for s in facts["slots"]):
            require(state.get("slot_create_intended"), "slot-not-owned-in-state")
            require(all(s["type"] == "physical" for s in facts["slots"]), "owned-slot-type-changed")
            for _ in range(5):
                active = self.guard_sql(f"SELECT to_json(coalesce((SELECT active FROM pg_replication_slots WHERE slot_name={quoted(SLOT)}), false));", as_json=True)
                if not active:
                    break
                time.sleep(0.1)
            self.guard_sql(f"SELECT pg_drop_replication_slot({quoted(SLOT)});")

    def remove_monitor(self):
        name, service, timer = monitor_units(self.side, str(self.scope))
        units = {name+".service": service, name+".timer": timer}
        if self.side == "source":
            units[name+"-fence.service"] = failure_fence_unit(str(self.scope))
        for filename, body in units.items():
            path = pathlib.Path("/etc/systemd/system")/filename
            require(not path.is_symlink(), "monitor-unit-symlink")
            require(not path.exists() or path.read_text() == body, "changed-monitor-unit-refuse-cleanup")
        self.run(["systemctl", "disable", "--now", name+".timer"], allow_failure=True)
        self.run(["systemctl", "stop", name+".service"], allow_failure=True)
        if self.side == "source":
            self.run(["systemctl", "stop", name+"-fence.service"], allow_failure=True)
        for filename in units:
            (pathlib.Path("/etc/systemd/system")/filename).unlink(missing_ok=True)
        self.run(["systemctl", "daemon-reload"])

    def cleanup(self):
        if not (self.scope/"state.json").exists():
            if self.side == "target":
                require(self.docker_inspect(CONTAINER) is None and self.docker_inspect(VOLUME, kind="volume") is None,
                        "target-objects-exist-without-state-refuse-cleanup")
            else:
                facts = self.source_facts(full_size=False)
                require(facts["role"] is None and not any(s["name"] == SLOT for s in facts["slots"]),
                        "source-objects-exist-without-state-refuse-cleanup")
            return {"side": self.side, "cleaned": True, "not_prepared": True}
        state = self.state(allow_cleaned=True)
        if state.get("cleaned"):
            return {"side": self.side, "cleaned": True, "already_cleaned": True}
        if "system_identifier" not in state:
            state["cleaned"] = True
            self.checkpoint(state, "cleaned")
            return {"side": self.side, "cleaned": True, "not_prepared": True}
        if self.side == "target":
            info = self.owned_container()
            volume = self.owned_volume()
            if volume:
                data = pathlib.Path(volume["Mountpoint"])
                require(not data.is_symlink(), "owned-replica-volume-symlink")
                if (data/".sub2api-seed-verified").exists():
                    require((data/"PG_VERSION").is_file() and (data/"PG_VERSION").read_text().strip() == "18" and
                            (data/"standby.signal").is_file() and not (data/"standby.signal").is_symlink(),
                            "completed-copy-without-standby-proof-refuse-cleanup")
                elif any(data.iterdir()):
                    require(info is not None and info["Config"]["Entrypoint"] == ["/bin/sh"] and
                            info["Config"]["Cmd"] == ["-ceu", bootstrap_shell()],
                            "unverified-nonempty-volume-without-seed-container-refuse-cleanup")
            if info:
                # Explicit cleanup is permitted only for an unpromoted owned copy.
                status = self.target_status()
                require(status.get("in_recovery") is not False, "promoted-copy-refuse-destructive-cleanup")
                require(volume is not None, "owned-replica-volume-missing")
                if info["State"]["Running"]:
                    self.run(["docker", "stop", "--time", "30", CONTAINER], timeout=40)
                self.run(["docker", "rm", CONTAINER])
            if self.owned_volume():
                self.run(["docker", "volume", "rm", VOLUME])
            (self.scope/"replication.pgpass").unlink(missing_ok=True)
            self.remove_monitor()
        else:
            self.fence_source_objects(drop_slot=True)
            facts = self.source_facts(full_size=False)
            hba = self.map_source_file(self.docker_inspect(SOURCE_CONTAINER), state["hba_file"])
            current = hba.read_bytes()
            restored = remove_hba(current, self.p["owner"])
            if restored != current:
                self.write_hba(hba, restored)
                require(self.query("SELECT to_json(count(*)) FROM pg_hba_file_rules WHERE error IS NOT NULL;") == 0, "hba-invalid-during-cleanup")
                self.query("SELECT to_json(pg_reload_conf());")
            if self.role_owned(facts):
                self.sql(f"DROP ROLE {ROLE};")
            if state.get("guc_changed") and not state.get("guc_restored"):
                require(not facts["slots"], "new-source-slot-exists-refuse-guc-restore")
                old = state["old_auto_wal"]
                already_restored = state.get("guc_restore_intended") and facts["max_slot_wal_keep_size"] == state["old_wal_setting"] and facts["auto_wal_settings"] == old
                if not already_restored:
                    require(facts["max_slot_wal_keep_size"] == state["our_wal_setting"], "wal-cap-changed-by-other-owner-refuse-restore")
                    require(len(facts["auto_wal_settings"]) == 1 and facts["auto_wal_settings"][0]["setting"] == str(WAL_CAP_MB)+"MB", "auto-conf-wal-cap-changed-refuse-restore")
                    state["guc_restore_intended"] = True
                    self.checkpoint(state, "guc-restore-intended")
                    if old:
                        self.sql("ALTER SYSTEM SET max_slot_wal_keep_size = "+quoted(old[0]["setting"])+";")
                    else:
                        self.sql("ALTER SYSTEM RESET max_slot_wal_keep_size;")
                    self.query("SELECT to_json(pg_reload_conf());")
                    self.wait_wal_setting(state["old_wal_setting"], "original-wal-setting-not-restored-monitor-retained")
                state["guc_restored"] = True
                self.checkpoint(state, "guc-restored")
            self.remove_monitor()
        state["cleaned"] = True
        self.checkpoint(state, "cleaned")
        return {"side": self.side, "cleaned": True, "evidence_retained": str(self.scope)}


def remote_main(payload):
    try:
        require(os.geteuid() == 0, "remote-root-required")
        require(payload["side"] in ["source", "target"], "invalid-side")
        remote = Remote(payload)
        action = payload["action"]
        if action == "inspect":
            result = remote.source_facts() if payload["side"] == "source" else remote.target_facts()
        elif action == "prepare":
            result = remote.source_prepare() if payload["side"] == "source" else remote.target_prepare()
        elif action == "status":
            result = remote.source_status() if payload["side"] == "source" else remote.target_status()
        elif action == "monitor":
            result = remote.monitor()
        elif action == "fence":
            result = remote.emergency_fence("independent-monitor-failure-service")
        elif action == "cleanup":
            result = remote.cleanup()
        else:
            raise Refusal("invalid-remote-action")
        print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    except Refusal as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1)
    except Exception:
        # Tracebacks can include SQL, connection strings or file contents.
        print(json.dumps({"ok": False, "error": "remote-internal-error-see-private-diagnostics"}))
        raise SystemExit(1)


def channel_commands():
    path = pathlib.Path(__file__).with_name("prepare_channel.py")
    spec = importlib.util.spec_from_file_location("migration_channel", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(module.SOURCE_IP == "13.231.143.136" and module.TARGET_IP == "43.165.177.136", "channel-endpoints-changed")
    return {"source": module.SSH_SOURCE, "target": module.TARGET}


class Controller:
    def __init__(self, directory):
        self.directory = pathlib.Path(directory)
        self.program = pathlib.Path(__file__).read_text()
        self.commands = channel_commands()

    def call(self, side, action, state=None, **extra):
        payload = {"side": side, "action": action, **extra}
        if state is not None:
            payload["owner"] = state["owner"]
        if action == "prepare":
            payload["program"] = self.program
        script = "REMOTE_PAYLOAD = "+repr(payload)+"\n"+self.program
        try:
            result = subprocess.run(self.commands[side]+["sudo -n python3 -"], input=script,
                                    text=True, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            self.private_error(side, exc.stderr or "SSH command timed out")
            raise Refusal(side+"-ssh-timeout-remote-work-may-continue-check-status") from None
        if result.stderr:
            self.private_error(side, result.stderr)
        try:
            reply = json.loads(result.stdout)
        except ValueError:
            self.private_error(side, result.stdout)
            raise Refusal(side+"-unrecognized-reply-see-private-diagnostics") from None
        require(result.returncode == 0 and reply.get("ok"), side+"-"+reply.get("error", "remote-command-failed"))
        return reply["result"]

    def private_error(self, side, message):
        directory = private_dir(self.directory, create=True)
        path = directory/(side+"-private-errors.log")
        require(not path.is_symlink(), "local-private-log-symlink")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "ab") as out:
            out.write((message.encode() if isinstance(message, str) else message)+b"\n")

    def state(self, create=False):
        directory = private_dir(self.directory, create=create)
        path = directory/"state.json"
        if path.exists():
            state = read_json(path)
            require(state["run"] == RUN, "local-state-run-mismatch")
            require(not state.get("cleaned"), "cleaned-state-archive-before-new-run")
            return state
        require(create, "local-state-missing")
        password = secrets.token_urlsafe(32)
        state = {"run": RUN, "owner": secrets.token_hex(16), "created_at": time.time(), "verifier": scram_verifier(password), "phase": "created"}
        atomic_private(directory/"replication-password", password+"\n")
        save_json(path, state)
        return state

    def checkpoint(self, state, phase):
        state["phase"] = phase
        state["updated_at"] = time.time()
        save_json(self.directory/"state.json", state)

    def inspect(self):
        source = self.call("source", "inspect")
        target = self.call("target", "inspect")
        return {"source": source, "target": target, "mutations": False}

    def status(self, state):
        source = self.call("source", "status", state)
        target = self.call("target", "status", state)
        own_slot = next((s for s in source["slots"] if s["name"] == SLOT), None)
        own_replica = next((r for r in source["replicas"] if r["user"] == ROLE and r["application_name"] == RUN), None)
        ready = bool(target["standby_ready"] and source["system_identifier"] == target.get("system_identifier") and
                     not source["in_recovery"] and source["role_login"] and 0 < source["wal_cap_mb"] <= WAL_CAP_MB and
                     own_slot and own_slot["active"] and own_slot["wal_status"] in ["reserved", "extended"] and
                     own_replica and own_replica["state"] == "streaming" and own_replica["sync_state"] == "async" and
                     own_replica["lag_bytes"] <= 64*MIB and source["source_free_bytes"] >= DISK_TRIP_BYTES and
                     target["target_free_bytes"] >= DISK_TRIP_BYTES and source["monitor"]["healthy"] and target["monitor"]["healthy"] and
                     not source["safety_tripped"] and not target["safety_tripped"])
        result = {"source": source, "target": target, "standby_ready": ready, "production_traffic_changed": False, "promoted": False}
        save_json(self.directory/"last-status.json", result)
        return result

    def apply(self, wait_seconds):
        state = self.state(create=True)
        facts = self.inspect()
        require(facts["source"]["image_identity"] == facts["target"]["image_identity"], "source-target-image-content-mismatch")
        Remote({"side": "source"}).validate_source(facts["source"])
        target = facts["target"]
        require(not target["container_exists"] or target["container_owner"] == state["owner"], "preflight-target-container-not-owned")
        require(not target["volume_exists"] or target["volume_owner"] == state["owner"], "preflight-target-volume-not-owned")
        require(not target["port_bound"] or target["container_exists"], "preflight-target-port-unavailable")
        require(target["source_pg_channel_tcp"], "preflight-source-channel-unavailable")
        require(target["target_free_bytes"] >= (DISK_TRIP_BYTES if target["volume_exists"] else facts["source"]["pgdata_bytes"]+4*GIB), "preflight-target-disk-insufficient")
        require(target["memory_total_bytes"] >= 1800*MIB and (target["container_exists"] or target["memory_available_bytes"] >= 900*MIB), "preflight-target-memory-insufficient")
        # Persist the inspected source identity before any remote mutation.
        if "system_identifier" in state:
            require(state["system_identifier"] == facts["source"]["system_identifier"], "local-source-identity-changed")
        state["system_identifier"] = facts["source"]["system_identifier"]
        self.checkpoint(state, "preflight-inspected")
        prepared = self.call("source", "prepare", state, verifier=state["verifier"])
        self.checkpoint(state, "source-prepared")
        password_path = self.directory/"replication-password"
        require(stat.S_IMODE(password_path.stat().st_mode) == 0o600 and not password_path.is_symlink(), "local-password-file-unsafe")
        self.call("target", "prepare", state, password=password_path.read_text().strip(),
                  system_identifier=prepared["system_identifier"], source_image_identity=prepared["image_identity"], pgdata_bytes=facts["source"]["pgdata_bytes"])
        self.checkpoint(state, "seeding-or-streaming")
        deadline = time.monotonic()+wait_seconds
        healthy = 0
        while time.monotonic() < deadline:
            result = self.status(state)
            healthy = healthy+1 if result["standby_ready"] else 0
            if healthy >= 3:
                self.checkpoint(state, "standby-streaming-verified")
                return result
            require(not result["source"]["safety_tripped"] and not result["target"]["safety_tripped"], "safety-monitor-tripped-check-private-status")
            require(result["target"]["container_state"] in ["created", "running"], "seed-container-exited-evidence-retained-explicit-cleanup-required")
            time.sleep(SAMPLE_SECONDS)
        raise Refusal("standby-not-yet-verified-remote-work-and-monitors-remain-check-status")

    def cleanup(self):
        state = self.state()
        # Stop the owned target before removing its source access/slot.
        target = self.call("target", "cleanup", state)
        self.checkpoint(state, "target-cleaned")
        source = self.call("source", "cleanup", state)
        state["cleaned"] = True
        self.checkpoint(state, "cleaned")
        (self.directory/"replication-password").unlink(missing_ok=True)
        return {"target": target, "source": source, "cleaned": True}


def plan(cleanup=False):
    if cleanup:
        actions = ["verify ownership; stop/remove only owned standby and volume", "fence only owned replication role; drop only owned slot", "remove exact owned HBA block; restore unchanged owned GUC", "remove only owned monitor units; retain evidence"]
    else:
        actions = ["read-only source/target identity, capacity, slots, channel and port preflight", "install independent source WAL/disk guard before retention", "record old GUC; reload finite 512 MB slot cap; create isolated role/HBA/slot", "install target invariant monitor; allocate isolated named volume/container", "stream pg_basebackup directly through encrypted loopback channel; verify manifest", "run unpromoted 768 MiB standby bound to 127.0.0.1:25432", "verify recovery/read_only/system identifier/source streaming/lag/disk/WAL"]
    return {"dry_run": True, "remote_commands_executed": False, "run": RUN, "actions": actions,
            "source": "sub2api / 13.231.143.136", "target": "43.165.177.136", "image": IMAGE,
            "container": CONTAINER, "volume": VOLUME, "role": ROLE, "slot": SLOT,
            "mutation_gate": "--apply"+(" --cleanup" if cleanup else ""), "promotes": False, "production_traffic_changed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true", help="read-only remote preflight; no preparation")
    mode.add_argument("--status", action="store_true", help="read current owned standby/source state")
    parser.add_argument("--apply", action="store_true", help="explicitly authorize replica preparation mutations")
    parser.add_argument("--cleanup", action="store_true", help="with --apply, remove only this owned unpromoted replica")
    parser.add_argument("--state-dir", type=pathlib.Path, default=pathlib.Path.home()/".local/share/sub2api-migration-20261016/postgres-replica")
    parser.add_argument("--wait-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    require(not ((args.inspect or args.status) and (args.apply or args.cleanup)), "inspect-status-cannot-authorize-mutations")
    require(0 < args.wait_seconds <= 7200, "wait-seconds-out-of-range")
    if not args.apply and not args.inspect and not args.status:
        print(json.dumps(plan(args.cleanup), indent=2, sort_keys=True))
        return 0
    controller = Controller(args.state_dir)
    if args.inspect:
        print(json.dumps(controller.inspect(), indent=2, sort_keys=True))
        return 0
    directory = private_dir(args.state_dir, create=args.apply)
    lock_path = directory/"controller.lock"
    require(not lock_path.is_symlink(), "local-lock-symlink")
    with open(lock_path, "a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("another-controller-is-running") from None
        if args.status:
            result = controller.status(controller.state())
        elif args.cleanup:
            result = controller.cleanup()
        else:
            result = controller.apply(args.wait_seconds)
    print(json.dumps(result, indent=2, sort_keys=True))
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
        except KeyboardInterrupt:
            print(json.dumps({"ok": False, "error": "interrupted-remote-work-may-continue-use-status", "no_automatic_rollback": True}), file=sys.stderr)
            raise SystemExit(130)
        except Exception:
            print(json.dumps({"ok": False, "error": "local-internal-error-no-secret-traceback", "no_automatic_rollback": True}), file=sys.stderr)
            raise SystemExit(1)
