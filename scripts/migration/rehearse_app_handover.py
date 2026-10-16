#!/usr/bin/env python3
"""Isolated actual-app handover rehearsal. Default invocation is OFFLINE.

--apply creates fresh, owned resources on Tencent only. It does not touch any
running production service, standby, tunnel, route, firewall or DNS record.
Every container is stopped at completion; all volumes and evidence are retained.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import io
import json
import os
import pathlib
import re
import secrets
import signal
import statistics
import subprocess
import sys
import threading
import time

MIB = 1024**2
GIB = 1024**3
SCOPE = "/home/ubuntu/sub2api-migration-20261016"
COLD_CONTAINER = "sub2api-migration-20261016-pg-rehearsal-130132"
COLD_VOLUME = "sub2api-migration-20261016-pg-rehearsal-130132-data"
PG_IMAGE = "postgres:migration-20261016-ff4a089dadda"
PG_CONFIG = "sha256:ff4a089dadda5e50a5f09604b2e2bc16f97d5befb6b05c328fc21159da96c602"
REDIS_IMAGE = "redis:migration-20261016-b95f59534a91"
REDIS_CONFIG = "sha256:b95f59534a9112e82c68677a05afa0c69dd4d75c9fdc7114b98594a28ad79f13"
LABEL = "io.sub2api.migration.app-rehearsal"
PORTS = {"pg1": 35432, "pg2": 35433, "redis1": 36379, "redis2": 36380,
         "app": 38082, "control": 38092}
LIMITS = {"pg1": 256*MIB, "pg2": 256*MIB, "redis1": 64*MIB,
          "redis2": 64*MIB, "app": 368*MIB, "anchor": 16*MIB, "clone": 64*MIB}
RUNNING_MEMORY_LIMIT = sum(memory for role, memory in LIMITS.items() if role != "clone")
REQUIRED_CONSUMERS = frozenset({"auth", "subscription", "channel", "error_passthrough",
                                "tls_fingerprint", "prompt_audit"})
REQUIRED_WRITERS = frozenset({"timing_wheel", "scheduled_writers", "plugins", "prompt_audit",
    "prompt_config", "auth_outbox", "ops_runtime", "usage_records", "billing_cache",
    "content_moderation", "detached_writers", "deferred_last_used", "audit_log",
    "ops_ingress_reject", "ops_system_log"})
WAIT_BUDGET_SECONDS = 10.0
TARGET = ["ssh", "-oProxyCommand=nc -X connect -x 127.0.0.1:7897 %h %p", "-oProxyJump=none",
    "-oBatchMode=yes", "-oConnectTimeout=10", "-oStrictHostKeyChecking=yes",
    "-i", str(pathlib.Path.home()/".ssh/tokyo.pem"), "-oIdentitiesOnly=yes", "ubuntu@43.165.177.136"]


class Refusal(RuntimeError):
    """Public error codes contain no command output, application data or secrets."""


def need(condition, code):
    if not condition:
        raise Refusal(code)


def names(run_id):
    need(isinstance(run_id, str) and re.fullmatch(r"[0-9a-f]{16}", run_id), "invalid-run-id")
    prefix = "sub2api-app-rehearsal-"+run_id
    return {role: prefix+"-"+role for role in LIMITS}


def no_symlinks(path):
    need(not any(p.is_symlink() for p in [path, *path.parents]), "refuse-symlink-path")


def private_file(path, data, *, replace=False):
    no_symlinks(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if replace else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(data if isinstance(data, bytes) else data.encode())
        handle.flush()
        os.fsync(handle.fileno())


def public_json(path, value, *, replace=False):
    private_file(path, json.dumps(value, sort_keys=True, indent=2)+"\n", replace=replace)


def env_text(values):
    rows = []
    for key, value in sorted(values.items()):
        need(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key), "invalid-environment-name")
        need(isinstance(value, str) and not any(c in value for c in "\r\n\x00"), "unsupported-environment-value")
        rows.append(key+"="+value)
    return "\n".join(rows)+"\n"


def cold_clone_mounts(clone_volume):
    need(re.fullmatch(r"sub2api-app-rehearsal-[0-9a-f]{16}-pg1-data", clone_volume), "unowned-cold-clone-destination")
    return ["--mount", "type=volume,src="+COLD_VOLUME+",dst=/input,readonly",
            "--mount", "type=volume,src="+clone_volume+",dst=/clone,volume-nocopy"]


def atomic_endpoints(directory, value, *, expected=None):
    """Durable, non-secret checkpoint; mount the DIRECTORY, never this inode."""
    need(set(value) == {"v", "postgres", "redis"} and value["v"] == 1 and
         all(value[k] in {"source", "target"} for k in ["postgres", "redis"]), "invalid-endpoint-checkpoint")
    no_symlinks(directory)
    need(directory.is_dir() and directory.stat().st_mode & 0o777 == 0o755, "endpoint-directory-mode-mismatch")
    path = directory/"endpoints.json"
    no_symlinks(path)
    if expected is None:
        need(not path.exists(), "initial-endpoint-checkpoint-already-exists")
    else:
        need(path.is_file() and json.loads(path.read_text()) == expected, "endpoint-checkpoint-phase-mismatch")
    pending = directory/(".endpoints-"+secrets.token_hex(8)+".pending")
    private_file(pending, json.dumps(value, sort_keys=True)+"\n")
    pending.chmod(0o644)
    with pending.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(pending, path)
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def app_environment(source, token):
    need(isinstance(source, list) and all(isinstance(x, str) and "=" in x for x in source), "invalid-source-runtime")
    result = dict(x.split("=", 1) for x in source)
    need(len(result) == len(source), "duplicate-source-environment")
    need(result.get("DATABASE_USER") == result.get("DATABASE_DBNAME") == "sub2api", "unreviewed-database-identity")
    need(bool(result.get("DATABASE_PASSWORD")), "database-password-missing")
    # Egress is physically absent. Also remove proxy targets and stale migration
    # switches so neither a future network change nor snapshot drift can route out.
    result = {k: v for k, v in result.items() if "proxy" not in k.lower() and not k.startswith("MIGRATION_")}
    password = result["DATABASE_PASSWORD"]
    escaped_password = password.replace("\\", "\\\\").replace("'", "\\'")
    result.update({
        "SERVER_HOST": "127.0.0.1", "SERVER_PORT": str(PORTS["app"]),
        "AUTO_SETUP": "false", "DATABASE_HOST": "127.0.0.1", "DATABASE_PORT": str(PORTS["pg1"]),
        "DATABASE_SSLMODE": "disable", "DATABASE_MAX_OPEN_CONNS": "12", "DATABASE_MAX_IDLE_CONNS": "3",
        "REDIS_HOST": "127.0.0.1", "REDIS_PORT": str(PORTS["redis1"]), "REDIS_USERNAME": "",
        "REDIS_PASSWORD": "", "REDIS_DB": "0", "REDIS_ENABLE_TLS": "false",
        "REDIS_POOL_SIZE": "24", "REDIS_MIN_IDLE_CONNS": "2",
        "API_KEY_AUTH_CACHE_L1_SIZE": "256", "API_KEY_AUTH_CACHE_L1_TTL_SECONDS": "15",
        "API_KEY_AUTH_CACHE_NEGATIVE_TTL_SECONDS": "30", "SUBSCRIPTION_CACHE_L1_SIZE": "256",
        "SUBSCRIPTION_CACHE_L1_TTL_SECONDS": "10", "GOMEMLIMIT": "300MiB", "GOGC": "50",
        "MIGRATION_HANDOVER_MODE": "quiet", "MIGRATION_BACKGROUND_WORKERS": "false",
        "MIGRATION_SHARED_REDIS": "true", "MIGRATION_POSTGRES_ENABLED": "true",
        "MIGRATION_POSTGRES_TARGET_DSN": "host=127.0.0.1 port="+str(PORTS["pg2"])+
            " user=sub2api dbname=sub2api password='"+escaped_password+"' sslmode=disable connect_timeout=1",
        "MIGRATION_REDIS_ENABLED": "true", "MIGRATION_REDIS_TARGET_ADDR": "127.0.0.1:"+str(PORTS["redis2"]),
        "MIGRATION_RUNTIME_ADDR": "127.0.0.1:"+str(PORTS["control"]), "MIGRATION_RUNTIME_TOKEN": token,
        "MIGRATION_ENDPOINT_STATE_FILE": "/migration-state/endpoints.json",
    })
    env_text(result)  # Reject multiline values before anything is allocated.
    return result


class _ResponseSocket:
    def __init__(self, raw):
        self.raw = io.BytesIO(raw)

    def makefile(self, *args, **kwargs):
        return self.raw


def parse_http(raw):
    need(len(raw) <= 1024*1024, "control-response-too-large")
    try:
        response = http.client.HTTPResponse(_ResponseSocket(raw))
        response.begin()
        body = response.read(1024*1024)
        need(response.isclosed(), "incomplete-http-response")
        if body and response.getheader("Content-Type", "").startswith("application/json"):
            body = json.loads(body)
        return response.status, body
    except (ValueError, OSError, http.client.HTTPException):
        raise Refusal("invalid-private-http-response") from None


BASELINE_APP_WARNINGS = {
    b"TOTP encryption key auto-generated. Consider setting a fixed key for production.",
    b"security.url_allowlist.enabled=false; allowlist/SSRF checks disabled (minimal format validation only).",
    b"[Pricing] Fallback merge skipped: open ./resources/model-pricing/model_prices_and_context_window.json: no such file or directory",
    b"payment encryption/signing key is not explicitly configured; set TOTP_ENCRYPTION_KEY to enable payment resume tokens",
    b"Warning: server.trusted_proxies is not configured; disabling the forwarded-IP compatibility switch will use direct peer addresses only",
    b"Warning: CORS allowed_origins not configured; cross-origin requests will be rejected.",
}


def warning_count(raw, *, expected_pg_shutdown=False, isolated_role=None):
    # No log content is returned or persisted. A warning/error is a blocker;
    # there is deliberately no broad startup-warning allowlist.
    count = 0
    for line in raw.splitlines():
        if isolated_role == "app":
            fields = line.split(b"\t")
            # These exact messages were read back from the original production
            # process on 2026-10-16. No migration changes their configuration.
            if len(fields) >= 5 and fields[1] == b"WARN" and fields[-2] in BASELINE_APP_WARNINGS:
                continue
        if isolated_role in {"pg1", "pg2"} and re.search(
                rb"FATAL:  (?:the database system is starting up|the database system is not yet accepting connections)\s*$", line):
            # The isolated test's own startup readiness probes cause these.
            continue
        if isolated_role in {"redis1", "redis2"}:
            if re.search(rb" \* <bf>\s+\{ bf-error-rate\s+:\s+0\.01 \}\s*$", line):
                continue  # INFO configuration, not a logged error.
            if line.endswith(b"# WARNING: Redis does not require authentication. Redis will accept connections from any local client."):
                continue  # All test Redis listeners are in network=none.
        # These exact messages were observed during clean source shutdown in
        # the owned PG18.3 clone. The caller enables them only AFTER checking
        # that fixed source container exited cleanly; a role-change intent is
        # insufficient. Other roles, ports, errors and severities still block.
        if expected_pg_shutdown and isolated_role in {"pg1", "pg2"}:
            if re.search(rb"FATAL:  terminating (?:connection|walreceiver process|walsender process) due to administrator command\s*$", line):
                continue
            if isolated_role == "pg1" and re.search(rb"FATAL:  the database system is shutting down\s*$", line):
                continue
            if isolated_role == "pg2" and (re.search(
                    rb"FATAL:  could not send end-of-streaming message to primary: server closed the connection unexpectedly\s*$", line) or re.search(
                    rb'FATAL:  streaming replication receiver "walreceiver" could not connect to the primary server: connection to server at "127\.0\.0\.1", port 35432 failed: (?:FATAL:  the database system is shutting down|Connection refused)\s*$', line)):
                continue
        count += bool(re.search(rb"(?i)\b(?:warn(?:ing)?|error|fatal|panic)\b", line))
    return count


def validate_snapshot(snapshot, *, ready=False, quiet=False):
    need(snapshot.get("background_workers_enabled") is False, "unexpected-background-workers")
    tasks = snapshot.get("tasks", {})
    need(REQUIRED_WRITERS <= set(tasks), "writer-manifest-incomplete")
    need(all(isinstance(t, dict) and t.get("failed", 0) == 0 and t.get("dropped", 0) == 0
             for t in tasks.values()), "writer-warning-or-loss-counter")
    if quiet:
        need(snapshot.get("active_requests") == snapshot.get("active_hijacked_connections") == 0,
             "http-or-websocket-not-drained")
        need(snapshot.get("ingress", {}).get("paused") is True and snapshot["ingress"].get("quiet") is True,
             "ingress-quiet-proof-missing")
        need(all(t.get("active") == t.get("pending") == 0 for t in tasks.values()), "accepted-writers-not-drained")
    if ready:
        need(snapshot.get("ready") is True, "app-not-ready")
        need(all(snapshot.get("readiness", {}).get(k) is True
                 for k in ["postgres", "redis", "quiet_controls", "writer_coverage"]), "readiness-proof-missing")


def validate_consumers(status, *, resumed):
    consumers = status.get("consumers", [])
    need(len(consumers) == 6 and {c.get("name") for c in consumers} == REQUIRED_CONSUMERS,
         "six-consumer-manifest-incomplete")
    need(all(c.get("generation") == status.get("epoch") for c in consumers), "consumer-epoch-not-acknowledged")
    need(status.get("pubsub_cache_ready") is True, "redis-cache-generation-not-ready")
    need(status.get("paused") is (not resumed), "redis-admission-state-mismatch")


class Rehearsal:
    def __init__(self, payload):
        self.payload = payload
        self.run_id = payload["run_id"]
        self.names = names(self.run_id)
        self.scope = pathlib.Path(SCOPE)/("app-handover-"+self.run_id)
        self.ids = {}
        self.volumes = {}
        self.image_ids = {}
        self.token = secrets.token_urlsafe(48)
        self.events = []
        self.scenarios = []
        self.current = None
        self.role_changed = False
        self.pg_fence_verified = False
        self.lock = None
        self.baseline = 0.0
        self.endpoint_state = {"v": 1, "postgres": "source", "redis": "source"}
        self.report = {"run_id": self.run_id, "ok": False, "isolation": "shared-network-none",
            "real_model_first_token_latency_proven": False, "business_billing_semantics_proven": False,
            "volumes_deleted": False, "production_mutated": False}

    def run(self, argv, *, data=None, timeout=20, check=True):
        result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
        if check and result.returncode:
            # Command output may include credentials or copied user data. It
            # never enters an exception, terminal, report, or diagnostic file.
            raise Refusal("isolated-command-failed")
        return result

    def inspect(self, kind, resource):
        return json.loads(self.run(["docker", kind, "inspect", resource]).stdout)[0]

    def exists(self, kind, resource):
        result = self.run(["docker", kind, "inspect", resource], check=False)
        if result.returncode == 0:
            return True
        need(result.returncode == 1 and result.stdout.strip() == b"[]" and
             (b"No such" in result.stderr or b"no such" in result.stderr), "resource-inspection-failed")
        return False

    def exec(self, role, argv, *, data=None, timeout=10, user=None, check=True):
        need(role in self.ids, "unknown-owned-container")
        command = ["docker", "exec", "-i"]
        if user:
            command += ["--user", user]
        return self.run(command+[self.ids[role], *argv], data=data, timeout=timeout, check=check)

    def config_digest(self, image):
        if image["Id"] in {PG_CONFIG, REDIS_CONFIG}:
            return image["Id"]
        descriptor = json.loads(self.run(["ctr", "-n", "moby", "content", "get", image["Id"]]).stdout)
        return descriptor["config"]["digest"]

    def cold_fingerprint(self):
        cold = self.inspect("container", COLD_CONTAINER)
        need(cold["State"]["Running"] is False, "cold-input-must-remain-stopped")
        mounts = [m for m in cold["Mounts"] if m.get("Name") == COLD_VOLUME and m.get("Destination") == "/var/lib/postgresql"]
        need(len(mounts) == 1, "cold-input-mount-changed")
        volume = self.inspect("volume", COLD_VOLUME)
        need(volume["Driver"] == volume["Scope"] == "local", "unreviewed-cold-volume")
        path = pathlib.Path(volume["Mountpoint"])
        no_symlinks(path)
        pgdata = path/"data"
        need((pgdata/"PG_VERSION").read_text().strip() == "18", "cold-postgres-version-mismatch")
        need(not (pgdata/"postmaster.pid").exists() and not any((pgdata/"pg_tblspc").iterdir()), "cold-input-unsafe-or-tablespaces")
        # Compare every file's identity, size and mtime before/after; hashing the
        # small control/config files adds a content check without reading rows.
        rows, size = [], 0
        for directory, dirs, files in os.walk(path):
            for name in dirs+files:
                item = pathlib.Path(directory)/name
                need(not item.is_symlink(), "cold-volume-contains-symlink")
            for name in files:
                item = pathlib.Path(directory)/name
                st = item.stat()
                size += st.st_size
                rows.append((str(item.relative_to(path)), st.st_ino, st.st_size, st.st_mtime_ns))
        selected = {name: hashlib.sha256((pgdata/name).read_bytes()).hexdigest()
                    for name in ["PG_VERSION", "global/pg_control", "postgresql.conf", "postgresql.auto.conf", "pg_hba.conf"]}
        return {"container_id": cold["Id"], "volume": COLD_VOLUME, "bytes": size,
                "tree_metadata_sha256": hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(),
                "control_config_sha256": selected}

    def prepare(self):
        need(sys.platform == "linux" and os.geteuid() == 0, "remote-linux-root-required")
        no_symlinks(self.scope)
        need(not self.scope.exists(), "run-scope-already-exists-resources-retained")
        lockpath = pathlib.Path(SCOPE)/"app-rehearsal.lock"
        no_symlinks(lockpath)
        self.lock = os.fdopen(os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+")
        need(os.fstat(self.lock.fileno()).st_uid == 0 and os.fstat(self.lock.fileno()).st_mode & 0o077 == 0,
             "rehearsal-lock-must-be-root-owned-private")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("another-app-rehearsal-is-running") from None
        need(re.fullmatch(r"sub2api:migration-compat-20261016-[0-9a-f]{12}", self.payload["app_image"]), "unreviewed-app-image-name")
        need(re.fullmatch(r"sha256:[0-9a-f]{64}", self.payload["app_image_id"]), "immutable-app-image-id-required")
        need(re.fullmatch(r"[0-9a-f]{64}", self.payload["binary_sha256"]), "binary-sha256-required")
        for role, tag, expected in [("pg", PG_IMAGE, PG_CONFIG), ("redis", REDIS_IMAGE, REDIS_CONFIG),
                                   ("app", self.payload["app_image"], self.payload["app_image_id"])]:
            image = self.inspect("image", tag)
            need(image["Architecture"] == "amd64" and image["Os"] == "linux", "image-platform-mismatch")
            need((image["Id"] if role == "app" else self.config_digest(image)) == expected, "image-identity-mismatch")
            if role == "app":
                labels = image["Config"].get("Labels") or {}
                need(labels.get("io.sub2api.migration.binary") == self.payload["binary_sha256"] and
                     labels.get("io.sub2api.migration.baseline") == "c69fdbeb0fa26543cc5bf115d2e5048426041e84", "app-build-provenance-mismatch")
            self.image_ids[role] = image["Id"]
        self.cold_before = self.cold_fingerprint()
        mem = dict(line.split(":", 1) for line in pathlib.Path("/proc/meminfo").read_text().splitlines())
        available = int(mem["MemAvailable"].split()[0])*1024
        need(available >= RUNNING_MEMORY_LIMIT+256*MIB, "insufficient-memory-with-production-headroom")
        disk = os.statvfs(self.scope.parent)
        need(disk.f_bavail*disk.f_frsize >= self.cold_before["bytes"]*3+3*GIB, "insufficient-disk-with-production-headroom")
        runtime_path = pathlib.Path(SCOPE)/"app-runtime/source-runtime.json"
        no_symlinks(runtime_path)
        need(runtime_path.stat().st_mode & 0o077 == 0, "runtime-snapshot-must-be-private")
        source = json.loads(runtime_path.read_text())
        self.environment = app_environment(source["environment"], self.token)
        for role, name in self.names.items():
            need(not self.exists("container", name), "container-name-already-exists")
            if role not in {"clone", "anchor"}:
                need(not self.exists("volume", name+"-data"), "volume-name-already-exists")
        self.scope.mkdir(mode=0o700)
        endpoints = self.scope/"endpoint-state"
        endpoints.mkdir(mode=0o755)
        endpoints.chmod(0o755)
        need(endpoints.stat().st_uid == 0, "endpoint-directory-must-be-root-owned")
        atomic_endpoints(endpoints, self.endpoint_state)
        public_json(self.scope/"ownership.json", {"run_id": self.run_id, "names": self.names,
                    "cold_readonly_input": COLD_VOLUME, "images": self.image_ids})
        private_file(self.scope/"app.env", env_text(self.environment))
        self.report.update({"scope": str(self.scope), "app_image_id": self.image_ids["app"],
            "binary_sha256": self.payload["binary_sha256"], "memory_limit_bytes": RUNNING_MEMORY_LIMIT,
            "available_memory_before_bytes": available, "cold_before": self.cold_before})
        self.write_report()
        for role in ["pg1", "pg2", "redis1", "redis2", "app"]:
            name = self.names[role]+"-data"
            self.run(["docker", "volume", "create", "--label", LABEL+"="+self.run_id, name])
            volume = self.inspect("volume", name)
            need(volume.get("Labels", {}).get(LABEL) == self.run_id and volume["Driver"] == volume["Scope"] == "local", "volume-owner-or-driver-mismatch")
            path = pathlib.Path(volume["Mountpoint"])
            no_symlinks(path)
            need(path.is_dir() and not any(path.iterdir()), "owned-volume-not-empty")
            self.volumes[role] = {"name": name, "path": path}
        self.create("clone", "pg", cold_clone_mounts(self.volumes["pg1"]["name"])+
            ["--entrypoint", "/bin/sh"], ["-ceu", "cp -a /input/. /clone/"])
        self.start("clone")
        waited = self.run(["docker", "wait", self.ids["clone"]], timeout=180).stdout.strip()
        need(waited == b"0", "readonly-cold-clone-failed")
        clone = self.volumes["pg1"]["path"]/"data"
        need((clone/"PG_VERSION").read_text().strip() == "18", "cloned-pgdata-missing")
        need(not (clone/"standby.signal").exists() and not (clone/"recovery.signal").exists(), "clone-not-a-cold-primary")
        # This HBA is legal ONLY in the owned clone and network-none namespace.
        private_file(clone/"pg_hba.conf", "local all all trust\nhost all all 127.0.0.1/32 trust\nhost replication all 127.0.0.1/32 trust\n", replace=True)
        st = (clone/"PG_VERSION").stat()
        os.chown(clone/"pg_hba.conf", st.st_uid, st.st_gid)
        self.pg_user = str(st.st_uid)+":"+str(st.st_gid)
        pg2 = self.volumes["pg2"]["path"]
        os.chown(pg2, st.st_uid, st.st_gid)
        (pg2/"data").mkdir(mode=0o700)
        os.chown(pg2/"data", st.st_uid, st.st_gid)
        self.stage_app_files()

    def stage_app_files(self):
        source = pathlib.Path(SCOPE)/"app-initial"
        target = self.volumes["app"]["path"]
        app_image = self.inspect("image", self.image_ids["app"])
        # The reviewed runtime entrypoint drops root to sub2api and owns /app/data.
        need(app_image["Config"].get("Entrypoint") == ["/app/docker-entrypoint.sh"], "unreviewed-app-entrypoint")
        manifest_path = pathlib.Path(SCOPE)/"initial-api-files-verification.json"
        no_symlinks(manifest_path)
        manifest = json.loads(manifest_path.read_text())
        fingerprints = {}
        for name in ["config.yaml", ".installed", "model_pricing.json", "model_pricing.sha256"]:
            item = source/name
            no_symlinks(item)
            need(item.is_file() and item.stat().st_size <= MIB, "invalid-initial-app-file")
            raw = item.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            need(manifest["app-initial/"+name]["sha256"] == digest, "initial-app-file-hash-drift")
            private_file(target/name, raw)
            fingerprints[name] = digest
        self.report["app_initial_file_sha256"] = fingerprints

    def create(self, role, image_role, options, command):
        need(role in self.names and role not in self.ids, "invalid-owned-container-create")
        memory = LIMITS[role]
        network = "none" if role in {"clone", "anchor"} else "container:"+self.ids["anchor"]
        argv = ["docker", "create", "--name", self.names[role], "--label", LABEL+"="+self.run_id,
            "--network", network, "--memory", str(memory), "--memory-swap", str(memory),
            "--cpus", "0.75", "--pids-limit", "128", "--restart", "no", "--security-opt",
            "no-new-privileges:true", "--cap-drop", "NET_RAW", "--log-opt", "max-size=8m", "--log-opt", "max-file=2"]
        raw = self.run(argv+options+[self.image_ids[image_role], *command]).stdout.decode().strip()
        need(re.fullmatch(r"[0-9a-f]{64}", raw), "created-container-id-invalid")
        created = self.inspect("container", raw)
        need(created["Config"]["Labels"].get(LABEL) == self.run_id and created["Image"] == self.image_ids[image_role], "created-container-identity-mismatch")
        need(created["HostConfig"]["NetworkMode"] == network and not created["HostConfig"].get("PortBindings"), "created-container-network-isolation-failed")
        need(created["HostConfig"]["Memory"] == memory and created["HostConfig"]["MemorySwap"] == memory,
             "created-container-resource-limits-mismatch")
        self.ids[role] = raw
        self.write_report()

    def start(self, role):
        self.run(["docker", "start", self.ids[role]])

    def volume_mount(self, role, destination):
        return ["--mount", "type=volume,src="+self.volumes[role]["name"]+",dst="+destination+",volume-nocopy"]

    @staticmethod
    def postgres_args(role):
        args = ["postgres", "-D", "/var/lib/postgresql/data", "-p", str(PORTS[role])]
        for option in ["listen_addresses=127.0.0.1", "shared_buffers=16MB", "max_connections=30",
            "max_wal_senders=4", "wal_level=replica", "hot_standby=on", "max_replication_slots=4",
            "unix_socket_directories=/tmp", "log_min_messages=warning", "logging_collector=off",
            "ssl=off", "huge_pages=off", "timezone=Asia/Shanghai", "log_timezone=Asia/Shanghai"]:
            args += ["-c", option]
        return args

    def sql(self, role, text, *, timeout=4):
        result = self.exec(role, ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
            "-h", "127.0.0.1", "-p", str(PORTS[role]), "-U", "sub2api", "-d", "sub2api"],
            data=text.encode(), timeout=timeout)
        return result.stdout.decode().strip()

    def wait(self, label, condition, timeout=20):
        deadline = time.monotonic()+timeout
        while True:
            try:
                if condition():
                    return
            except (Refusal, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                pass
            need(time.monotonic() < deadline, label+"-timeout")
            time.sleep(0.05)

    def redis(self, role, *parts, timeout=4):
        # Isolated Redis has no authentication. REDISCLI_AUTH from any inherited
        # image environment is cleared explicitly; no credentials enter argv.
        result = self.exec(role, ["env", "-u", "REDISCLI_AUTH", "redis-cli", "--raw", "-e",
            "-h", "127.0.0.1", "-p", str(PORTS[role]), *map(str, parts)], timeout=timeout)
        return result.stdout.decode().strip()

    def redis_info(self, role):
        return dict(line.split(":", 1) for line in self.redis(role, "INFO", "replication").splitlines()
                    if line and not line.startswith("#") and ":" in line)

    def launch_data(self):
        # Keep the isolated namespace alive independently of either authority.
        # An app restart must not depend on restarting the fenced PG1 primary.
        self.create("anchor", "pg", ["--entrypoint", "/bin/sleep", "--read-only", "--mount",
            "type=tmpfs,dst=/var/lib/postgresql,tmpfs-size=1048576"], ["900"])
        self.start("anchor")
        self.verify_anchor()
        self.create("pg1", "pg", self.volume_mount("pg1", "/var/lib/postgresql")+
                    ["-e", "PGDATA=/var/lib/postgresql/data", "-e", "TZ=Asia/Shanghai"], self.postgres_args("pg1"))
        self.start("pg1")
        self.wait("cloned-primary-start", lambda: self.sql("pg1", "SELECT NOT pg_is_in_recovery();") == "t")
        need(self.sql("pg1", "SHOW server_version;") == "18.3", "postgres-version-drift")
        need(self.sql("pg1", "SELECT rolsuper FROM pg_roles WHERE rolname='sub2api';") == "t", "clone-user-cannot-replicate")
        self.sql("pg1", "CREATE SCHEMA migration_rehearsal; CREATE TABLE migration_rehearsal.writes "
            "(id text PRIMARY KEY, marker text NOT NULL); BEGIN; INSERT INTO migration_rehearsal.writes "
            "VALUES ('before_basebackup', 'confirmed_commit'); COMMIT;")
        # Use a real physical base backup, rather than starting PG2 from a second
        # independent cold copy. Trust authentication exists only in this clone.
        import shlex
        bootstrap = "pg_basebackup -h 127.0.0.1 -p "+str(PORTS["pg1"])+" -U sub2api -D /var/lib/postgresql/data -R -X stream --checkpoint=fast; exec "+shlex.join(self.postgres_args("pg2"))
        self.create("pg2", "pg", self.volume_mount("pg2", "/var/lib/postgresql")+
            ["--user", self.pg_user, "--entrypoint", "/bin/sh"], ["-ceu", bootstrap])
        self.start("pg2")
        self.wait("physical-replica-start", lambda: self.sql("pg2", "SELECT pg_is_in_recovery();") == "t", timeout=120)
        need(self.sql("pg1", "SELECT system_identifier FROM pg_control_system();") ==
             self.sql("pg2", "SELECT system_identifier FROM pg_control_system();"), "physical-system-identity-mismatch")
        need(self.sql("pg1", "SELECT count(*) FROM pg_stat_replication WHERE state='streaming';") == "1", "physical-stream-not-connected")
        for role in ["redis1", "redis2"]:
            directory = self.volumes[role]["path"]
            config = "bind 127.0.0.1\nprotected-mode yes\nport "+str(PORTS[role])+"\ndir /data\nsave \"\"\nappendonly no\nmaxmemory 32mb\nmaxmemory-policy noeviction\nrepl-diskless-sync yes\nrepl-diskless-sync-delay 0\nreplica-announce-ip 127.0.0.1\nreplica-announce-port "+str(PORTS[role])+"\n"
            if role == "redis2":
                config += "replicaof 127.0.0.1 "+str(PORTS["redis1"])+"\n"
            private_file(directory/"rehearsal.conf", config)
            # This exact pinned Redis image uses uid 999 / gid 1000, previously
            # verified during standby preparation. Its entrypoint deliberately
            # skips unknown config files, so own this private file explicitly.
            os.chown(directory, 999, 1000)
            os.chown(directory/"rehearsal.conf", 999, 1000)
            # Delegate to the official entrypoint once. No CONFIG REWRITE or
            # restart is performed, avoiding duplicate automatic module loads.
            self.create(role, "redis", self.volume_mount(role, "/data"), ["redis-server", "/data/rehearsal.conf"])
            self.start(role)
            self.wait(role+"-start", lambda role=role: self.redis(role, "PING") == "PONG")
            need("redis_version:8.6.2" in self.redis(role, "INFO", "server"), "redis-version-drift")
        self.wait("redis-physical-sync", lambda: self.redis_info("redis2").get("master_link_status") == "up" and
                  self.redis_info("redis2").get("master_sync_in_progress") == "0")
        need(self.redis("redis1", "SET", "migration:rehearsal:"+self.run_id, "confirmed-before-handover") == "OK", "redis-marker-write-failed")
        # WAIT must share the SET connection to prove that write's offset. The
        # private stdin session avoids mistaking a fresh client's zero offset.
        out = self.exec("redis1", ["env", "-u", "REDISCLI_AUTH", "redis-cli", "--raw", "-e", "-p", str(PORTS["redis1"])],
            data=("SET migration:rehearsal:"+self.run_id+" confirmed-before-handover\nWAIT 1 2000\n").encode(), timeout=4).stdout.decode().splitlines()
        need(out == ["OK", "1"], "redis-marker-not-acknowledged-by-replica")

    def http(self, path, *, method="GET", body=None, public=False, token=True, timeout=4):
        need(re.fullmatch(r"/[A-Za-z0-9_/-]*", path), "invalid-control-path")
        payload = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        port = PORTS["app"] if public else PORTS["control"]
        headers = [method+" "+path+" HTTP/1.1", "Host: 127.0.0.1", "Connection: close",
            "Accept-Encoding: identity", "Content-Type: application/json", "Content-Length: "+str(len(payload))]
        if token and not public:
            headers.append("Authorization: Bearer "+self.token)
        request = ("\r\n".join(headers)+"\r\n\r\n").encode()+payload
        # BusyBox nc can close the TCP write half immediately at stdin EOF;
        # net/http then cancels a deliberately queued request. Keep a real
        # socket open for the response, inside this owned isolated namespace.
        info = self.inspect("container", self.ids["app"])
        need(info["State"]["Running"] and info["Config"]["Labels"].get(LABEL) == self.run_id,
             "http-namespace-not-owned-running-app")
        program = ("import socket,sys;request=sys.stdin.buffer.read();"
                   "s=socket.create_connection(('127.0.0.1',"+str(port)+"),"+str(timeout)+");"
                   "s.settimeout("+str(timeout)+");s.sendall(request);"
                   "out=s.makefile('rb');sys.stdout.buffer.write(out.read(1048577));s.close()")
        raw = self.run(["nsenter", "--target", str(info["State"]["Pid"]), "--net", "--",
                        "/usr/bin/python3", "-c", program], data=request, timeout=timeout+1).stdout
        return parse_http(raw)

    def get(self, component):
        path = "/status" if component == "runtime" else "/data/"+component+"/status"
        status, body = self.http(path)
        need(status == 200 and isinstance(body, dict), "private-status-not-json")
        return body

    def post(self, component, operation, *, expected=200, epoch=None, endpoint=None):
        if epoch is None and not (component == "ingress" and operation == "pause"):
            epoch = self.get(component)["epoch"]
        body = {} if epoch is None else {"expected_epoch": epoch}
        if endpoint is not None:
            body["endpoint"] = endpoint
        status, result = self.http("/data/"+component+"/"+operation, method="POST", body=body)
        if status != expected:
            self.report["failed_control"] = {"component": component, "operation": operation,
                "http_status": status, "expected_status": expected,
                "response": result.decode(errors="replace")[:256] if isinstance(result, bytes) else result,
                "runtime": self.get("runtime"), "writers": self.get("writers")}
            self.write_report()
        need(status == expected, "control-rejected-or-unexpectedly-accepted-"+component+"-"+operation)
        return result

    def launch_app(self):
        self.create("app", "app", self.volume_mount("app", "/app/data")+
            ["--env-file", str(self.scope/"app.env"), "--mount",
             "type=bind,src="+str(self.scope/"endpoint-state")+",dst=/migration-state,readonly,bind-propagation=rprivate"], ["/app/sub2api"])
        self.start("app")
        actual = self.exec("app", ["sha256sum", "/app/sub2api"]).stdout.decode().split()[0]
        need(actual == self.payload["binary_sha256"], "running-app-binary-hash-mismatch")
        self.wait("app-private-initialization", lambda: self.http("/ready")[0] == 200, timeout=45)
        need(self.http("/status", token=False)[0] == 401, "private-control-authentication-not-enforced")
        # The embedded baseline serves index.html for unknown SPA routes. A
        # public HTML fallback is not the private JSON control handler.
        public_status, public_body = self.http("/data/redis/status", public=True)
        html_fallback = (public_status == 200 and isinstance(public_body, bytes) and
                         b"<html" in public_body.lower() and b"</html>" in public_body.lower())
        need(public_status == 404 or html_fallback, "private-control-exposed-on-business-listener")
        before = self.get("ingress")
        self.http("/data/ingress/pause", method="POST", body={}, public=True)
        need(self.get("ingress") == before, "public-route-executed-private-transition")
        self.check_ready()
        need(self.get("postgres")["endpoint"] == "source" and self.get("redis")["address"] ==
             "127.0.0.1:"+str(PORTS["redis1"]), "initial-app-endpoint-selection-mismatch")
        values = []
        for _ in range(3):
            started = time.monotonic()
            need(self.http("/health", public=True)[0] == 200, "isolated-health-failed")
            values.append(time.monotonic()-started)
        self.baseline = statistics.median(values)
        self.report["baseline_health_seconds"] = values
        self.assert_no_warnings()

    def check_ready(self):
        validate_snapshot(self.get("runtime"), ready=True)
        validate_consumers(self.get("redis"), resumed=True)

    def assert_no_warnings(self):
        validate_snapshot(self.get("runtime"))
        counts = {}
        for role, container in self.ids.items():
            result = self.run(["docker", "logs", container], check=False)
            need(result.returncode == 0, "cannot-inspect-owned-container-warnings")
            counts[role] = warning_count(result.stdout+b"\n"+result.stderr,
                expected_pg_shutdown=self.pg_fence_verified, isolated_role=role)
        self.report["warning_counts"] = counts
        need(not any(counts.values()), "isolated-runtime-warning-resources-retained")

    def step(self, name, action):
        started = time.monotonic()
        try:
            result = action()
            self.events.append({"scenario": self.current["name"] if self.current else "preparation",
                                "step": name, "seconds": time.monotonic()-started, "ok": True})
            return result
        except BaseException:
            self.events.append({"scenario": self.current["name"] if self.current else "preparation",
                                "step": name, "seconds": time.monotonic()-started, "ok": False})
            raise

    def start_scenario(self, name):
        self.check_ready()
        self.assert_no_warnings()
        self.current = {"name": name, "start": time.monotonic(), "ok": False}
        self.scenarios.append(self.current)
        self.step("pause_ingress", lambda: self.post("ingress", "pause"))
        self.queue_result = {}
        self.queue_started = threading.Event()
        def queued():
            started = time.monotonic()
            self.queue_started.set()
            try:
                self.queue_result["status"] = self.http("/health", public=True, timeout=18)[0]
                self.queue_result["seconds"] = time.monotonic()-started
            except BaseException:
                self.queue_result["error"] = "queued-health-failed"
        self.queue_thread = threading.Thread(target=queued, daemon=True)
        self.queue_thread.start()
        need(self.queue_started.wait(1), "queued-request-thread-not-started")
        self.step("observe_queued_request", lambda: self.wait("queued-admission", lambda:
            self.get("ingress").get("queued_requests") == 1, timeout=1))
        ingress = self.get("ingress")
        need(ingress.get("quiet") is True, "ingress-did-not-reach-quiet")

    def finish_scenario(self):
        self.step("resume_writers", lambda: self.post("writers", "resume"))
        self.step("resume_ingress", lambda: self.post("ingress", "resume", expected=204))
        elapsed = time.monotonic()-self.current["start"]
        self.queue_thread.join(2)
        need(not self.queue_thread.is_alive() and self.queue_result.get("status") == 200, "queued-request-failed-or-not-released")
        additional = max(0.0, self.queue_result["seconds"]-self.baseline)
        self.current.update({"admission_pause_seconds": elapsed, "queued_health_seconds": self.queue_result["seconds"],
            "baseline_health_median_seconds": self.baseline, "additional_health_wait_seconds": additional,
            "within_10_seconds": elapsed <= WAIT_BUDGET_SECONDS and additional <= WAIT_BUDGET_SECONDS})
        self.check_ready()
        self.assert_no_warnings()
        need(self.get("ingress").get("queued_requests") == 0, "queued-admission-not-empty")
        need(self.current["within_10_seconds"], "additional-wait-budget-exceeded")
        self.current["ok"] = True
        self.current = None
        self.write_report()

    def epoch_and_order_rejections(self):
        for component, operation in [("writers", "pause"), ("redis", "pause"), ("postgres", "pause"), ("ingress", "resume")]:
            self.step("reject_wrong_epoch_"+component, lambda c=component, op=operation:
                self.post(c, op, epoch=self.get(c)["epoch"]+99, expected=409))
        self.step("reject_writers_with_open_ingress", lambda: self.post("writers", "pause", expected=409))
        self.step("reject_redis_without_writer_fence", lambda: self.post("redis", "pause", expected=409))
        self.step("reject_postgres_without_global_quiet", lambda: self.post("postgres", "pause", expected=409))

    def same_epoch_abort(self):
        self.start_scenario("same_epoch_abort_rebuild_resume")
        self.step("pause_writers", lambda: self.post("writers", "pause"))
        validate_snapshot(self.get("runtime"), quiet=True)
        self.step("reject_postgres_before_redis_pause", lambda: self.post("postgres", "pause", expected=409))
        self.step("reject_redis_switch_before_pause", lambda: self.post("redis", "switch", endpoint="target", expected=409))
        self.step("pause_redis", lambda: self.post("redis", "pause"))
        epoch = self.get("redis")["epoch"]
        pg_epoch = self.get("postgres")["epoch"]
        self.step("pause_postgres_before_same_authority_abort", lambda: self.post("postgres", "pause"))
        self.step("resume_postgres_on_same_authority_abort", lambda: self.post("postgres", "resume"))
        need(self.get("postgres")["epoch"] == pg_epoch, "same-source-abort-changed-postgres-epoch")
        self.step("reject_unrebuilt_redis_resume", lambda: self.post("redis", "resume", expected=409))
        self.step("reject_writers_with_paused_redis", lambda: self.post("writers", "resume", expected=409))
        self.step("reject_ingress_with_writer_fence", lambda: self.post("ingress", "resume", expected=409))
        self.step("reject_wrong_rebuild_epoch", lambda: self.post("redis", "rebuild", epoch=epoch+99, expected=409))
        self.step("rebuild_same_source_epoch", lambda: self.post("redis", "rebuild", epoch=epoch))
        need(self.get("redis")["epoch"] == epoch, "same-source-abort-changed-redis-epoch")
        validate_consumers(self.get("redis"), resumed=False)
        self.step("resume_source_redis", lambda: self.post("redis", "resume", epoch=epoch))
        self.finish_scenario()

    def synchronized_pg_lsn(self):
        lsn = self.sql("pg1", "SELECT pg_current_wal_flush_lsn();")
        need(re.fullmatch(r"[0-9A-F]+/[0-9A-F]+", lsn), "invalid-final-flush-lsn")
        self.wait("final-physical-replay", lambda: self.sql("pg2",
            "SELECT pg_last_wal_replay_lsn() >= '"+lsn+"'::pg_lsn;") == "t", timeout=1.5)
        self.current["confirmed_flush_lsn"] = lsn

    def fence_pg1(self):
        before = self.inspect("container", self.ids["pg1"])
        need(before["Id"] == self.ids["pg1"] and before["Config"]["Labels"].get(LABEL) == self.run_id and
             before["State"]["Running"] is True and before["HostConfig"]["RestartPolicy"]["Name"] == "no",
             "refuse-fence-nonowned-or-inactive-clone")
        self.report["postgres_source_fence"] = {"container_id": self.ids["pg1"], "verified": False}
        self.write_report()
        # The container's PID1 is postgres. Once it exits, Docker may terminate
        # this pg_ctl exec before pg_ctl can return success. Its exit code (or
        # timeout) does not prove whether the requested stop took effect.
        returncode = None
        try:
            result = self.exec("pg1", ["pg_ctl", "-D", "/var/lib/postgresql/data", "stop", "-m", "fast", "-w", "-t", "2"],
                               user=self.pg_user, timeout=3, check=False)
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            pass
        after = self.inspect("container", self.ids["pg1"])
        state = after["State"]
        need(after["Id"] == self.ids["pg1"] and after["Config"]["Labels"].get(LABEL) == self.run_id and
             after["HostConfig"]["RestartPolicy"]["Name"] == "no" and state["Running"] is False and
             state["Status"] == "exited" and state["ExitCode"] == 0 and not state.get("OOMKilled", False),
             "old-clone-primary-not-cleanly-fenced")
        self.pg_fence_verified = True
        self.report["postgres_source_fence"] = {"container_id": self.ids["pg1"], "verified": True,
            "exit_code": state["ExitCode"], "oom_killed": False, "finished_at": state["FinishedAt"],
            "pg_ctl_exec_returncode": returncode}
        self.write_report()

    def promote_pg2(self):
        self.exec("pg2", ["pg_ctl", "-D", "/var/lib/postgresql/data", "promote", "-w", "-t", "2"], user=self.pg_user, timeout=3)
        need(self.sql("pg2", "SELECT NOT pg_is_in_recovery();") == "t", "target-clone-not-promoted")

    def failover_redis(self):
        before = self.redis_info("redis1")
        replica = self.redis_info("redis2")
        need(before.get("role") == "master" and before.get("master_failover_state") == "no-failover" and
             replica.get("role") == "slave" and replica.get("master_link_status") == "up", "redis-not-ready-for-coordinated-failover")
        need(self.redis("redis1", "FAILOVER", "TO", "127.0.0.1", PORTS["redis2"], "TIMEOUT", "1500") == "OK",
             "coordinated-redis-failover-refused")
        self.wait("redis-role-swap", lambda: self.redis_info("redis1").get("role") == "slave" and
                  self.redis_info("redis2").get("role") == "master" and
                  self.redis_info("redis1").get("master_link_status") == "up", timeout=2)
        need(self.redis("redis2", "GET", "migration:rehearsal:"+self.run_id) == "confirmed-before-handover",
             "confirmed-redis-write-lost")

    def checkpoint_endpoints(self, component, endpoint):
        need(component in {"postgres", "redis"} and endpoint == "target", "unreviewed-endpoint-checkpoint-transition")
        next_state = {**self.endpoint_state, component: endpoint}
        atomic_endpoints(self.scope/"endpoint-state", next_state, expected=self.endpoint_state)
        self.endpoint_state = next_state
        self.report["durable_endpoint_checkpoint"] = dict(next_state)
        self.write_report()

    def switch_app_endpoint(self, component):
        # Persist before runtime switch: a crash between these operations must
        # restart against the newly established authority, never the old one.
        self.checkpoint_endpoints(component, "target")
        return self.post(component, "switch", endpoint="target")

    def handover(self):
        # This commit happens after physical replication and app initialization;
        # an independent target query must observe it after old-primary fencing.
        self.sql("pg1", "BEGIN; INSERT INTO migration_rehearsal.writes VALUES "
            "('after_app_init', 'confirmed_commit'); COMMIT;")
        need(self.sql("pg1", "SELECT count(*) FROM migration_rehearsal.writes WHERE marker='confirmed_commit';") == "2", "source-commit-not-confirmed")
        self.start_scenario("postgres_and_redis_handover")
        self.step("pause_writers", lambda: self.post("writers", "pause"))
        validate_snapshot(self.get("runtime"), quiet=True)
        self.step("pause_redis_before_postgres", lambda: self.post("redis", "pause"))
        self.step("pause_postgres", lambda: self.post("postgres", "pause"))
        need(self.get("postgres")["active_operations"] == 0, "postgres-operations-not-drained")
        self.step("reject_unpromoted_target", lambda: self.post("postgres", "switch", endpoint="target", expected=409))
        self.step("final_postgres_replay", self.synchronized_pg_lsn)
        self.step("pre_role_change_warning_gate", self.assert_no_warnings)
        # Abort BEFORE either role change when the remaining budget cannot cover
        # recovery and reopening. Once roles change, never guess an old authority.
        need(time.monotonic()-self.current["start"] < 5.5, "insufficient-role-change-time-budget")
        self.role_changed = True
        self.step("fence_old_postgres", self.fence_pg1)
        self.step("promote_postgres_target", self.promote_pg2)
        self.step("persist_and_switch_app_postgres", lambda: self.switch_app_endpoint("postgres"))
        self.step("resume_app_postgres", lambda: self.post("postgres", "resume"))
        self.step("coordinated_redis_failover", self.failover_redis)
        self.step("persist_and_switch_app_redis", lambda: self.switch_app_endpoint("redis"))
        self.step("rebuild_target_redis_generation", lambda: self.post("redis", "rebuild"))
        validate_consumers(self.get("redis"), resumed=False)
        self.step("resume_app_redis", lambda: self.post("redis", "resume"))
        self.step("pre_admission_warning_gate", self.assert_no_warnings)
        need(self.sql("pg2", "SELECT count(*) FROM migration_rehearsal.writes WHERE marker='confirmed_commit';") == "2", "confirmed-postgres-write-lost")
        self.finish_scenario()
        need(self.get("postgres")["endpoint"] == "target" and self.get("redis")["address"] == "127.0.0.1:"+str(PORTS["redis2"]), "app-not-on-target-data-authorities")
        self.sql("pg2", "BEGIN; INSERT INTO migration_rehearsal.writes VALUES ('after_handover', 'confirmed_commit'); COMMIT;")
        need(self.sql("pg2", "SELECT count(*) FROM migration_rehearsal.writes;") == "3", "new-primary-write-failed")
        need(self.redis("redis2", "SET", "migration:rehearsal:after:"+self.run_id, "confirmed-after-handover") == "OK", "new-redis-primary-write-failed")
        self.wait("old-redis-follows-new-primary", lambda: self.redis("redis1", "GET",
            "migration:rehearsal:after:"+self.run_id) == "confirmed-after-handover", timeout=2)
        self.report["synthetic_committed_postgres_writes_retained"] = 3
        self.report["synthetic_confirmed_redis_writes_retained"] = True

    def verify_anchor(self):
        anchor = self.inspect("container", self.ids["anchor"])
        need(anchor["Id"] == self.ids["anchor"] and anchor["Config"]["Labels"].get(LABEL) == self.run_id and
             anchor["State"]["Running"] is True and anchor["HostConfig"]["NetworkMode"] == "none" and
             anchor["HostConfig"]["RestartPolicy"]["Name"] == "no", "isolated-network-anchor-not-owned-running")

    def source_endpoints_unreachable(self):
        self.verify_anchor()
        info = self.inspect("container", self.ids["app"])
        need(info["Id"] == self.ids["app"] and info["State"]["Running"] and
             info["Config"]["Labels"].get(LABEL) == self.run_id, "source-probe-namespace-not-owned-running-app")
        program = ("import json,socket,sys\nresult={}\n"
            "for role,port in json.load(sys.stdin).items():\n"
            " try:\n  s=socket.create_connection(('127.0.0.1',port),0.3)\n"
            " except ConnectionRefusedError:\n  result[role]='connection-refused'\n"
            " else:\n  s.close();sys.exit(1)\nprint(json.dumps(result))\n")
        result = self.run(["nsenter", "--target", str(info["State"]["Pid"]), "--net", "--",
            "/usr/bin/python3", "-c", program], data=json.dumps({r: PORTS[r] for r in ["pg1", "redis1"]}).encode(), timeout=2)
        evidence = json.loads(result.stdout)
        need(evidence == {"pg1": "connection-refused", "redis1": "connection-refused"}, "old-source-endpoint-still-reachable")
        return evidence

    def restart_on_persisted_targets(self):
        need(self.endpoint_state == {"v": 1, "postgres": "target", "redis": "target"}, "target-checkpoint-not-complete")
        need(not self.inspect("container", self.ids["pg1"])["State"]["Running"], "old-postgres-source-still-running")
        # Removing BOTH old endpoints from the isolated namespace makes a stale
        # startup selection fail, rather than silently reaching a replica.
        self.run(["docker", "stop", "--time", "5", self.ids["redis1"]], timeout=8)
        need(not self.inspect("container", self.ids["redis1"])["State"]["Running"], "old-redis-source-still-running")
        self.source_endpoints_unreachable()
        self.run(["docker", "stop", "--time", "5", self.ids["app"]], timeout=8)
        self.verify_anchor()
        started = time.monotonic()
        self.start("app")
        self.wait("restart-on-target-authorities", lambda: self.http("/ready")[0] == 200, timeout=45)
        self.check_ready()
        postgres, redis = self.get("postgres"), self.get("redis")
        validate_consumers(redis, resumed=True)
        need(postgres["endpoint"] == "target" and redis["address"] ==
             "127.0.0.1:"+str(PORTS["redis2"]), "app-restarted-on-old-endpoints")
        need(not self.inspect("container", self.ids["pg1"])["State"]["Running"] and
             not self.inspect("container", self.ids["redis1"])["State"]["Running"], "old-endpoint-unexpectedly-restarted")
        need(self.http("/health", public=True)[0] == 200, "restarted-app-health-failed")
        need(self.sql("pg2", "SELECT count(*) FROM migration_rehearsal.writes;") == "3", "restart-lost-confirmed-writes")
        self.report["restart_on_persisted_targets"] = {"passed": True, "seconds": time.monotonic()-started,
            "both_sources_stopped": True, "counts_toward_cutover_10_seconds": False,
            "postgres_endpoint": postgres["endpoint"], "redis_address": redis["address"],
            "redis_epoch": redis["epoch"], "redis_consumers": sorted(redis["consumers"], key=lambda c: c["name"]),
            "source_endpoint_probes": self.source_endpoints_unreachable(), "anchor_id": self.ids["anchor"]}
        self.assert_no_warnings()

    def write_report(self):
        if not self.scope.is_dir():
            return
        public_json(self.scope/"report.json", {**self.report, "events": self.events, "scenarios": self.scenarios,
            "containers": dict(self.ids), "volumes": {k: v["name"] for k, v in self.volumes.items()}}, replace=True)

    def stop_owned(self):
        failures = []
        for role in ["app", "redis2", "redis1", "pg2", "pg1", "clone", "anchor"]:
            if role not in self.ids:
                continue
            try:
                container = self.inspect("container", self.ids[role])
                need(container["Config"]["Labels"].get(LABEL) == self.run_id, "refuse-stop-nonowned-container")
                if container["State"]["Running"]:
                    result = self.run(["docker", "stop", "--time", "5", self.ids[role]], timeout=8, check=False)
                    need(result.returncode == 0, "owned-container-stop-failed")
                container = self.inspect("container", self.ids[role])
                need(not container["State"]["Running"], "owned-container-still-running")
                need(not container["State"].get("OOMKilled", False), "owned-container-hit-memory-limit")
            except BaseException:
                failures.append(role)
        self.report["all_owned_containers_stopped"] = not failures
        self.report["stop_or_oom_failures"] = failures
        return not failures

    def execute(self):
        try:
            self.step("prepare_owned_clone", self.prepare)
            self.step("launch_real_replicated_data", self.launch_data)
            self.step("launch_actual_compatibility_app", self.launch_app)
            self.epoch_and_order_rejections()
            self.same_epoch_abort()
            self.handover()
            self.step("restart_app_with_both_old_sources_stopped", self.restart_on_persisted_targets)
            self.assert_no_warnings()
            self.report["cold_after"] = self.cold_fingerprint()
            need(self.report["cold_after"] == self.cold_before, "cold-input-changed")
            self.report["ok"] = True
        except BaseException as exc:
            self.report["error"] = str(exc) if isinstance(exc, Refusal) else "isolated-rehearsal-failed-no-secret-output"
            if self.current:
                self.current["failed_at_seconds"] = time.monotonic()-self.current["start"]
                self.current["within_10_seconds"] = False
        finally:
            # Stop immutable IDs owned by this run only; retain all volumes and
            # reports. In an isolated test, closing the app is safer than trying
            # to recover a possibly promoted old endpoint after an error.
            stopped = self.stop_owned()
            if not stopped:
                self.report["ok"] = False
            if hasattr(self, "cold_before"):
                try:
                    self.report["cold_after"] = self.cold_fingerprint()
                    if self.report["cold_after"] != self.cold_before:
                        self.report.update(ok=False, error="cold-input-changed")
                except BaseException:
                    self.report.update(ok=False, error="cold-input-final-verification-failed")
            self.write_report()
            if self.lock is not None:
                self.lock.close()
        return {"ok": self.report["ok"], "run_id": self.run_id, "scope": str(self.scope),
            "error": self.report.get("error"), "all_owned_containers_stopped": self.report.get("all_owned_containers_stopped"),
            "real_model_first_token_latency_proven": False, "production_mutated": False,
            "durable_endpoint_checkpoint": self.report.get("durable_endpoint_checkpoint"),
            "restart_on_persisted_targets": self.report.get("restart_on_persisted_targets"),
            "scenarios": [{k: v for k, v in s.items() if k != "start"} for s in self.scenarios]}


def remote_main(payload):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    def interrupted(signum, frame):
        raise Refusal("remote-run-interrupted-resources-retained")
    signal.signal(signal.SIGTERM, interrupted)
    result = Rehearsal(payload).execute()
    print(json.dumps(result, sort_keys=True), flush=True)


def remote_source(payload):
    # __name__ prevents the offline CLI from running inside the remote worker.
    source = pathlib.Path(__file__).read_text()
    return "__name__ = 'isolated_remote_worker'\n"+source.replace("from __future__ import annotations\n", "")+"\nremote_main("+repr(payload)+")\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--app-image")
    parser.add_argument("--app-image-id")
    parser.add_argument("--binary-sha256")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"dry_run": True, "network_access": False, "remote_mutation": False,
            "required_apply_inputs": ["--app-image", "--app-image-id", "--binary-sha256"],
            "isolated_ports": PORTS, "cold_input_readonly": COLD_VOLUME,
            "aggregate_running_memory_limit_bytes": RUNNING_MEMORY_LIMIT,
            "extra_headroom_bytes": 256*MIB, "rehearses": ["six_consumer_initialization", "same_epoch_abort",
                "postgres_physical_promotion", "redis_coordinated_failover", "queued_health", "epoch_and_order_rejections"],
            "persistent_endpoint_selection_and_restart": True,
            "real_model_first_token_latency_proven": False, "production_mutated": False}))
        return 0
    need(args.app_image and args.app_image_id and args.binary_sha256, "reviewed-compatibility-image-inputs-required")
    payload = {"run_id": secrets.token_hex(8), "app_image": args.app_image,
               "app_image_id": args.app_image_id, "binary_sha256": args.binary_sha256}
    local = pathlib.Path.home()/".codex/backups"/("sub2api-app-handover-"+payload["run_id"])
    no_symlinks(local)
    local.mkdir(mode=0o700, parents=True)
    public_json(local/"request.json", payload)
    result = subprocess.run(TARGET+["sudo -n python3 -"], input=remote_source(payload),
                            text=True, capture_output=True, timeout=540)
    # Raw stderr is deliberately omitted. It can contain a Python exception
    # whose text includes an inherited environment value or SQL connection.
    need(result.returncode == 0, "remote-rehearsal-failed-review-owned-scope")
    try:
        summary = json.loads(result.stdout)
    except ValueError:
        raise Refusal("remote-rehearsal-summary-invalid") from None
    public_json(local/"summary.json", summary)
    print(json.dumps({**summary, "local_summary": str(local/"summary.json")}, sort_keys=True))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refusal) else
                          "rehearsal-operation-failed-no-secret-output"}), file=sys.stderr)
        raise SystemExit(1)
