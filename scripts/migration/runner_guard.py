#!/usr/bin/env python3
"""Fixed AWS WAL guard and asynchronous recovery supervisor. CLI is offline by default."""
import copy
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import stat
import subprocess
import sys
import threading
import time

_spec = importlib.util.spec_from_file_location("migration_source_operations", pathlib.Path(__file__).with_name("source_operations.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)
_budget_spec = importlib.util.spec_from_file_location("migration_budget_engine", pathlib.Path(__file__).with_name("durable_runner.py"))
E = importlib.util.module_from_spec(_budget_spec)
_budget_spec.loader.exec_module(E)
RUN, SCOPE = S.RUN, S.SCOPE
LIBEXEC = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016")
MAIN_UNIT = "sub2api-migration-runner-20261016.service"
GUARD_UNIT = S.GUARD_UNIT
DATA = "/var/lib/postgresql/data"
ROLE, SLOT = "sub2api_mig_20261016_replica", "sub2api_mig_20261016"
APPLICATION, ADDRESS = "sub2api-pg-replica-20261016", "172.18.0.1"
WAL_CAP, RETAIN_LIMIT, FREE_MIN = 512*1024**2, 384*1024**2, 2*1024**3
SAMPLE_INTERVAL, SAMPLE_FRESH, SAMPLE_TIMEOUT = 0.25, 1.5, 1.25
PROBE_FAILURE_COUNT, PROBE_FAILURE_DURATION, PROBE_UNAVAILABLE_LIMIT = 3, 3.0, 4.0
TRANSIENT_PROBE_ERRORS = frozenset({
    "GUARD_COMMAND_TIMEOUT", "GUARD_COMMAND_UNAVAILABLE", "GUARD_LOCAL_COMMAND_FAILED",
    "GUARD_RESOURCE_PROBE_UNAVAILABLE", "GUARD_PG_SAMPLING_BUSY", "GUARD_SAMPLE_TIMEOUT",
})
# The cumulative internal budget stays eight seconds. Taking over with 0.5
# remaining leaves 2.5 seconds to the user limit, including cgroup isolation.
HEARTBEAT_INTERVAL, RECOVERY_RESERVE = 0.1, 0.5
MAIN_CGROUP = "/system.slice/" + MAIN_UNIT


def main_unit_text():
    return """[Unit]
Description=Sub2API migration single-owner runner 20261016
After=docker.service sub2api-migration-recovery-20261016.service
Requires=sub2api-migration-recovery-20261016.service

[Service]
Type=simple
User=root
UMask=0077
WorkingDirectory=/var/lib/sub2api-migration-20261016/runner
ExecStart=/usr/bin/python3 -I /usr/local/libexec/sub2api-migration-20261016/runner_service.py continue
Restart=no
TimeoutStopSec=0.4
KillMode=control-group
SendSIGKILL=yes

[Install]
WantedBy=multi-user.target
"""


def unit_text():
    return """[Unit]
Description=Sub2API independent WAL and recovery guard 20261016
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=root
UMask=0077
WorkingDirectory=/var/lib/sub2api-migration-20261016/runner
ExecStart=/usr/bin/python3 -I /usr/local/libexec/sub2api-migration-20261016/runner_guard.py guard
Restart=always
RestartSec=0.2
TimeoutStopSec=1
KillMode=control-group

[Install]
WantedBy=multi-user.target
"""


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_config(value):
    S.require(type(value) is dict and set(value) == {"v", "run", "owner", "source_manifest_sha256",
              "role_owner_comment_sha256", "pg_data_host_path", "slot_create_intended"}, "GUARD_CONFIG_FIELDS")
    S.require(type(value["v"]) is int and value["v"] == 1 and value["run"] == RUN and
              S.match(value["owner"], S.HEX32) and S.match(value["source_manifest_sha256"], S.HEX64) and
              S.match(value["role_owner_comment_sha256"], S.HEX64) and value["slot_create_intended"] is True,
              "GUARD_CONFIG_IDENTITY")
    path = value["pg_data_host_path"]
    S.require(type(path) is str and len(path) < 1024 and path.startswith("/") and
              path != "/" and pathlib.PurePosixPath(path).as_posix() == path and
              ".." not in pathlib.PurePosixPath(path).parts and not any(ord(c) < 32 for c in path), "GUARD_DATA_PATH")
    return copy.deepcopy(value)


PG_SAMPLE = b"""SET statement_timeout='450ms'; SET lock_timeout='100ms';
SELECT json_build_object(
'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
'timeline',(SELECT timeline_id FROM pg_control_checkpoint()),'in_recovery',pg_is_in_recovery(),
'data_directory',current_setting('data_directory'),
'wal_cap_bytes',CASE WHEN current_setting('max_slot_wal_keep_size')='-1' THEN -1 ELSE pg_size_bytes(current_setting('max_slot_wal_keep_size')) END,
'role',(SELECT json_build_object('name',rolname,'comment',shobj_description(oid,'pg_authid'),
'replication',rolreplication,'login',rolcanlogin,'superuser',rolsuper,'createdb',rolcreatedb,
'createrole',rolcreaterole,'bypassrls',rolbypassrls) FROM pg_roles WHERE rolname='sub2api_mig_20261016_replica'),
'slots',COALESCE((SELECT json_agg(json_build_object('name',slot_name,'type',slot_type,'temporary',temporary,'active',active,
'restart_lsn',restart_lsn::text,'retained_bytes',pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)::bigint))
FROM pg_replication_slots WHERE slot_name='sub2api_mig_20261016'),'[]'::json));
"""


class ProbeUnavailable(S.Refusal):
    """A bounded read failed without proving a WAL or identity violation."""


class ThreadJob:
    """Only the worker waits for local commands. The heartbeat thread never joins."""
    def __init__(self, function, now, monotonic=time.monotonic):
        self.started, self.completed_at = now, None
        self.done = threading.Event()
        self.result, self.error = None, None
        def work():
            try:
                self.result = function()
            except S.Refusal as error:
                # Refusals use fixed codes, never SQL, stderr or credentials.
                code = str(error)
                self.error = code if S.match(code, r"[A-Z][A-Z0-9_]{0,95}") else "GUARD_LOCAL_PROBE_FAILED"
            except Exception:
                self.error = "GUARD_LOCAL_PROBE_FAILED"
            finally:
                # Publish completion before done, independently of when the
                # supervisor gets CPU to observe it.
                self.completed_at = monotonic()
                self.done.set()
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        return self.done.is_set()


class LocalBackend:
    def __init__(self):
        S.require(os.geteuid() == 0, "ROOT_REQUIRED")
        S.trusted_parent(SCOPE, private=True)
        self.config = validate_config(S.strict_json(S.private_bytes(SCOPE / "guard-config.json")))
        raw = S.private_bytes(SCOPE / S.FILE_NAMES["manifest"])
        S.require(hashlib.sha256(raw).hexdigest() == self.config["source_manifest_sha256"], "GUARD_MANIFEST_CHANGED")
        self.manifest = S.validate_manifest(S.strict_json(raw))
        S.require(self.manifest["owner"] == self.config["owner"], "GUARD_OWNER_MISMATCH")
        self.source = S.SourceOperations(S.LocalBackend())
        self.validate_units()

    def validate_units(self):
        for unit, expected in [(MAIN_UNIT, S.digest(main_unit_text().encode())),
                               (GUARD_UNIT, self.manifest["units"][GUARD_UNIT])]:
            S.require(S.digest(S.private_bytes(pathlib.Path("/etc/systemd/system") / unit, mode=0o644)) == expected,
                      "GUARD_UNIT_CHANGED")
        S.require(self.manifest["units"][GUARD_UNIT] == S.digest(unit_text().encode()), "GUARD_UNIT_NOT_OWNED")

    def boot_id(self):
        return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    @contextlib.contextmanager
    def singleton(self):
        fd = os.open(SCOPE / "guard.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            S.require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
                      stat.S_IMODE(info.st_mode) == 0o600, "GUARD_LOCK_UNTRUSTED")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise S.Refusal("GUARD_ALREADY_RUNNING") from None
            yield
        finally:
            os.close(fd)

    def read(self, name, missing=False):
        path = SCOPE / name
        if missing and not path.exists() and not path.is_symlink():
            return None
        return S.strict_json(S.private_bytes(path))

    def write(self, name, value):
        S.atomic_private(SCOPE / name, S.canonical(value)+b"\n")

    def command(self, argv, data=None, timeout=0.65):
        try:
            result = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=timeout, check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
        except subprocess.TimeoutExpired:
            raise ProbeUnavailable("GUARD_COMMAND_TIMEOUT") from None
        except OSError:
            raise ProbeUnavailable("GUARD_COMMAND_UNAVAILABLE") from None
        S.require(len(result.stdout) <= S.MAX_BYTES, "GUARD_COMMAND_OUTPUT_LIMIT")
        if result.returncode != 0:
            raise ProbeUnavailable("GUARD_LOCAL_COMMAND_FAILED")
        return result.stdout

    def _resource(self):
        self.source._identity()
        raw = S.private_bytes(SCOPE / S.FILE_NAMES["manifest"])
        S.require(hashlib.sha256(raw).hexdigest() == self.config["source_manifest_sha256"], "GUARD_MANIFEST_CHANGED")
        try:
            info = self.source._resource("postgres")
        except S.Unknown:
            # SourceOperations uses this code for its read-only docker client.
            raise ProbeUnavailable("GUARD_RESOURCE_PROBE_UNAVAILABLE") from None
        mounts = [m for m in info.get("Mounts", []) if m.get("Destination") == DATA]
        S.require(len(mounts) == 1 and mounts[0].get("Source") == self.config["pg_data_host_path"] and
                  mounts[0].get("RW") is True, "GUARD_MOUNT_CHANGED")
        path = pathlib.Path(self.config["pg_data_host_path"])
        S.require(path.resolve(strict=True) == path and path.is_dir(), "GUARD_DATA_PATH_CHANGED")
        return info

    def _sql(self, query):
        cid = self.manifest["containers"]["postgres"]["id"]
        try:
            with S.pg_sampling_lock():
                return self.command(["/usr/bin/docker", "exec", "-i", cid, "psql", "-X", "-q", "-A", "-t", "-w",
                                     "-v", "ON_ERROR_STOP=1", "-U", "sub2api", "-d", "sub2api"], query)
        except S.Refusal as error:
            if str(error) == "PG_SAMPLING_BUSY":
                raise ProbeUnavailable("GUARD_PG_SAMPLING_BUSY") from None
            raise

    def _cluster(self, sample):
        pg = self.manifest["postgres"]
        S.require(sample.get("system_identifier") == pg["system_identifier"] and
                  sample.get("timeline") == pg["timeline"] and sample.get("in_recovery") is False and
                  sample.get("data_directory") == DATA, "GUARD_CLUSTER_CHANGED")

    def _role(self, sample):
        role = sample.get("role")
        S.require(type(role) is dict and role.get("name") == ROLE and type(role.get("comment")) is str and
                  hashlib.sha256(role["comment"].encode()).hexdigest() == self.config["role_owner_comment_sha256"] and
                  role.get("replication") is True and all(role.get(key) is False for key in
                      ("superuser", "createdb", "createrole", "bypassrls")), "GUARD_ROLE_NOT_OWNED")
        return role

    def _slots(self, sample):
        slots = sample.get("slots")
        S.require(type(slots) is list and len(slots) <= 1 and all(type(s) is dict and s.get("name") == SLOT and
                  s.get("type") == "physical" and s.get("temporary") is False for s in slots), "GUARD_SLOT_NOT_OWNED")
        return slots

    def _owned_stop(self, info, free):
        S.require(info["State"]["Running"] is False, "GUARD_PG_STILL_RUNNING")
        self.source._stopped(info)
        ledger = self.source._ledger()
        stops = [op for op in ledger["operations"].values() if op["request"]["action"] == "pg_stop"]
        S.require(len(stops) == 1 and "sigint" in stops[0]["steps"] and
                  S.match(stops[0].get("arm_id"), S.HEX32) and S.match(stops[0].get("final_lsn"), S.LSN),
                  "GUARD_PG_STOP_NOT_OWNED")
        return {"pg_wal_protected": True, "mode": "OWNED_CLEAN_STOP", "free_bytes": free}

    def sample(self):
        info = self._resource()
        stats = os.statvfs(self.config["pg_data_host_path"])
        free = stats.f_bavail * stats.f_frsize
        if not info["State"]["Running"]:
            return self._owned_stop(info, free)
        # A failed transport must not hide an independently observed disk trip.
        S.require(free >= FREE_MIN, "GUARD_DISK_LOW")
        try:
            facts = S.strict_json(self._sql(PG_SAMPLE).strip())
        except Exception:
            # An owned fast shutdown can finish between inspect and psql.
            # A clean, owned stopped resource is independently conclusive.
            # Preserve the original fixed failure for a still-running process.
            # A timeout is unavailable evidence, not proof of unsafe WAL.
            current = self._resource()
            if not current["State"]["Running"]:
                return self._owned_stop(current, free)
            raise
        self._cluster(facts)
        role, slots = self._role(facts), self._slots(facts)
        S.require(type(facts.get("wal_cap_bytes")) is int and 0 <= facts["wal_cap_bytes"] <= WAL_CAP, "GUARD_WAL_CAP_UNBOUNDED")
        if not slots:
            fenced = self.read("guard-fence.json", missing=True)
            S.require(role.get("login") is False and fenced and fenced.get("owner") == self.config["owner"] and
                      fenced.get("fenced") is True, "GUARD_SLOT_MISSING_WITHOUT_FENCE")
            return {"pg_wal_protected": True, "mode": "OWNED_REPLICA_FENCED", "free_bytes": free}
        retained = slots[0].get("retained_bytes")
        S.require(type(retained) is int and 0 <= retained < RETAIN_LIMIT and
                  S.match(slots[0].get("restart_lsn"), S.LSN), "GUARD_WAL_RETENTION_HIGH")
        return {"pg_wal_protected": True, "mode": "RUNNING_BOUNDED", "retained_bytes": retained,
                "wal_cap_bytes": facts["wal_cap_bytes"], "free_bytes": free}

    def fence(self):
        """Never stop PostgreSQL. Revalidate every retry; SQL names are literals."""
        last = None
        for _ in range(3):
            try:
                info = self._resource()
                if not info["State"]["Running"]:
                    return self.sample()
                facts = S.strict_json(self._sql(PG_SAMPLE).strip())
                self._cluster(facts)
                self._role(facts)
                self._slots(facts)
                pg = self.manifest["postgres"]
                # Recheck ownership inside the same transaction as NOLOGIN.
                # Only validated hex/numeric manifest fields enter this fixed SQL.
                sql = ("SET statement_timeout='450ms'; SET lock_timeout='100ms'; BEGIN;\n"
                    "DO $guard$ BEGIN IF (SELECT system_identifier::text FROM pg_control_system()) <> '"+pg["system_identifier"]+"' OR "
                    "(SELECT timeline_id FROM pg_control_checkpoint()) <> "+str(pg["timeline"])+" OR pg_is_in_recovery() OR "
                    "current_setting('data_directory') <> '/var/lib/postgresql/data' OR "
                    "NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sub2api_mig_20261016_replica' AND rolreplication AND NOT rolsuper "
                    "AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolbypassrls AND encode(sha256(convert_to(coalesce(shobj_description(oid,'pg_authid'),''),'UTF8')),'hex')='"+
                    self.config["role_owner_comment_sha256"]+"') OR EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='sub2api_mig_20261016' "
                    "AND (slot_type <> 'physical' OR temporary)) THEN RAISE EXCEPTION 'GUARD_OWNERSHIP_CHANGED'; END IF; "
                    "ALTER ROLE sub2api_mig_20261016_replica NOLOGIN; END $guard$;\n"
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='sub2api_mig_20261016_replica' "
                    "AND backend_type='walsender' AND application_name='sub2api-pg-replica-20261016' AND host(client_addr)='172.18.0.1';\n"
                    "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name='sub2api_mig_20261016' "
                    "AND slot_type='physical' AND NOT temporary AND NOT active; COMMIT;\n")
                self._sql(sql.encode())
                verify = S.strict_json(self._sql(PG_SAMPLE).strip())
                self._cluster(verify)
                role = self._role(verify)
                S.require(role.get("login") is False and self._slots(verify) == [], "GUARD_FENCE_PENDING")
                proof = {"v": 1, "run": RUN, "owner": self.config["owner"], "boot_id": self.boot_id(),
                         "checked_at": time.time(), "fenced": True}
                self.write("guard-fence.json", proof)
                return {"pg_wal_protected": True, "mode": "OWNED_REPLICA_FENCED"}
            except Exception as error:
                last = error
                time.sleep(0.05)
        raise S.Refusal("GUARD_FENCE_UNCONFIRMED") from last

    def start_job(self, kind, now):
        return ThreadJob(self.sample if kind == "sample" else self.fence, now)

    def main_status(self):
        self.validate_units()
        output = self.command(["/usr/bin/systemctl", "show", MAIN_UNIT,
                               "--property=MainPID,Job,ControlGroup,ActiveState,FragmentPath,DropInPaths,LoadState,NeedDaemonReload,Restart,KillMode,TimeoutStopUSec"], timeout=0.2)
        values = {}
        for line in output.decode().splitlines():
            key, value = line.split("=", 1)
            S.require(key not in values, "GUARD_UNIT_STATUS_INVALID")
            values[key] = value
        S.require(set(values) == {"MainPID", "Job", "ControlGroup", "ActiveState", "FragmentPath", "DropInPaths",
                                 "LoadState", "NeedDaemonReload", "Restart", "KillMode", "TimeoutStopUSec"} and
                  values["MainPID"].isdigit() and values["ControlGroup"] in ("", MAIN_CGROUP), "GUARD_MAIN_IDENTITY")
        S.require(values["FragmentPath"] == "/etc/systemd/system/"+MAIN_UNIT and not values["DropInPaths"] and
                  values["LoadState"] == "loaded" and values["NeedDaemonReload"] == "no" and
                  values["Restart"] == "no" and values["KillMode"] == "control-group" and
                  values["TimeoutStopUSec"] in ("400ms", "400000us"), "GUARD_MAIN_OVERRIDE_OR_POLICY_CHANGED")
        path = pathlib.Path("/sys/fs/cgroup"+MAIN_CGROUP)
        empty = not path.exists()
        if not empty:
            events = dict(line.split() for line in (path / "cgroup.events").read_text().splitlines())
            empty = events.get("populated") == "0"
        return {"running": int(values["MainPID"]) > 0 and values["ActiveState"] == "active",
                "quiescent": values["MainPID"] == "0" and values["Job"] == "" and empty}

    def stop_main(self):
        self.validate_units()
        self.command(["/usr/bin/systemctl", "--no-block", "stop", MAIN_UNIT], timeout=0.2)

    def kill_main(self):
        self.validate_units()
        self.command(["/usr/bin/systemctl", "kill", "--kill-who=all", "--signal=SIGKILL", MAIN_UNIT], timeout=0.2)

    def spawn_recover(self):
        S.require(self.main_status()["quiescent"], "GUARD_MAIN_STILL_ALIVE")
        S.private_bytes(LIBEXEC / "runner_service.py", mode=0o644)
        return subprocess.Popen(["/usr/bin/python3", "-I", str(LIBEXEC / "runner_service.py"), "recover"],
                                cwd=SCOPE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})


class Guard:
    def __init__(self, backend, now=time.time, monotonic=time.monotonic, record_reader=None):
        self.b, self.now, self.monotonic = backend, now, monotonic
        self.record_reader = record_reader if record_reader is not None else getattr(backend, "budget_record", None)
        self.owner, self.boot = backend.config["owner"], backend.boot_id()
        self.trip = backend.read("guard-trip.json", missing=True)
        self.trip_persisted = self.trip is not None
        if self.trip:
            S.require(self.trip.get("v") == 1 and self.trip.get("run") == RUN and self.trip.get("owner") == self.owner,
                      "GUARD_TRIP_IDENTITY")
        self.sample_job = self.fence_job = self.recovery = None
        self.sample_at, self.last_sample = None, None
        self.next_sample, self.next_recovery = 0, 0
        self.fence_attempts, self.takeover_at, self.kill_sent = 0, None, False
        self.reason = None
        self.probe_failure_at, self.probe_failure_wall = None, None
        self.probe_failures, self.probe_reason = 0, None
        self.observed_at = self.monotonic()

    def fail(self, reason):
        if self.trip is None:
            self.trip = {"v": 1, "run": RUN, "owner": self.owner, "boot_id": self.boot,
                         "checked_at": self.now(), "reason": reason}
            if self.probe_failure_at is not None:
                self.trip.update(probe_failures=self.probe_failures, probe_last_reason=self.probe_reason,
                                 probe_first_failed_at=self.probe_failure_wall)
            try:
                self.b.write("guard-trip.json", self.trip)
                self.trip_persisted = True
            except Exception:
                # Full disk must still trigger the isolated owned-object fence.
                self.reason = "GUARD_TRIP_PERSIST_FAILED"
        self.last_sample, self.sample_at = None, None

    def probe_unavailable(self, reason, *, count=True):
        self.last_sample, self.sample_at = None, None
        if self.probe_failure_at is None:
            self.probe_failure_at, self.probe_failure_wall = self.monotonic(), self.now()
        self.probe_failures += int(count)
        self.probe_reason = reason

    def remaining(self, state):
        S.require(type(state) is dict and state.get("v") == 1 and state.get("run") == RUN, "GUARD_STATE_INVALID")
        window = state.get("window")
        try:
            used = E.budget_accounting(state, self.record_reader)["cohort_pause_seconds"]
        except E.Refusal:
            raise S.Refusal("GUARD_COHORT_ACCOUNTING_UNCERTAIN") from None
        S.require(finite(used) and used >= 0, "GUARD_STATE_BUDGET_INVALID")
        if window is None:
            return None
        S.require(type(window) is dict, "GUARD_WINDOW_INVALID")
        if window.get("boot_id") == self.boot:
            start = window.get("started_monotonic")
            S.require(finite(start), "GUARD_WINDOW_CLOCK_INVALID")
            elapsed = self.monotonic()-start
        else:
            start, floor = window.get("started_at"), window.get("elapsed_floor", 0)
            S.require(finite(start) and finite(floor), "GUARD_WINDOW_CLOCK_INVALID")
            elapsed = self.now()-start
            S.require(elapsed >= floor, "GUARD_CLOCK_UNCERTAIN")
        S.require(elapsed >= 0, "GUARD_CLOCK_UNCERTAIN")
        return 8.0-used-elapsed

    def supervise(self):
        # A fresh WAL sample alone cannot attest that recovery is available.
        # Check the loaded main-unit policy even before the runner has started
        # and while no admission window is open. An inactive, empty unit is a
        # valid initial state; an unavailable/modified control plane is not.
        main = self.b.main_status()
        state = self.b.read("state.json", missing=True)
        if state is None:
            return
        reason = None
        try:
            remaining = self.remaining(state)
        except S.Refusal:
            remaining = -1 if state.get("window") is not None else None
            reason = "GUARD_STATE_OR_CLOCK_UNCERTAIN"
        if self.recovery is not None and self.recovery.poll() is not None:
            self.recovery, self.next_recovery = None, self.monotonic()+0.1
        if remaining is None:
            self.takeover_at, self.kill_sent = None, False
            return reason
        if self.takeover_at is None and (remaining <= RECOVERY_RESERVE or not main["running"]):
            self.takeover_at = self.monotonic()
            self.b.stop_main()
        if self.takeover_at is None:
            return reason
        if not main["quiescent"]:
            if not self.kill_sent and self.monotonic()-self.takeover_at >= 0.6:
                self.b.kill_main()
                self.kill_sent = True
            return reason
        if self.recovery is None and self.monotonic() >= self.next_recovery:
            self.recovery = self.b.spawn_recover()
        return reason

    def tick(self):
        # Transient diagnostics describe this tick. Only the WAL trip is
        # latched; a successful later supervisor check must clear its error.
        self.reason = None
        tick = self.monotonic()
        if self.sample_job is not None:
            if self.sample_job.poll():
                completed = self.sample_job.completed_at
                if not finite(completed) or not self.sample_job.started <= completed <= self.monotonic():
                    self.fail("GUARD_SAMPLE_TIME_INVALID")
                elif self.sample_job.error and self.sample_job.error not in TRANSIENT_PROBE_ERRORS:
                    self.fail(self.sample_job.error)
                elif self.sample_job.error:
                    self.probe_unavailable(self.sample_job.error)
                elif type(self.sample_job.result) is not dict or self.sample_job.result.get("pg_wal_protected") is not True:
                    self.fail("GUARD_SAMPLE_INVALID")
                elif completed-self.sample_job.started > SAMPLE_TIMEOUT:
                    self.probe_unavailable("GUARD_SAMPLE_TIMEOUT")
                else:
                    self.last_sample, self.sample_at = self.sample_job.result, completed
                    self.observed_at = completed
                    self.probe_failure_at, self.probe_failure_wall = None, None
                    self.probe_failures, self.probe_reason = 0, None
                self.sample_job, self.next_sample = None, tick+SAMPLE_INTERVAL
            elif tick-self.sample_job.started > SAMPLE_TIMEOUT:
                self.probe_unavailable("GUARD_SAMPLE_TIMEOUT", count=False)
        # Count completed failures, not heartbeat ticks. A single hung worker
        # cannot manufacture retries or postpone fencing indefinitely.
        if self.probe_failure_at is not None and self.probe_failures >= PROBE_FAILURE_COUNT and \
                tick-self.probe_failure_at >= PROBE_FAILURE_DURATION:
            self.fail(self.probe_reason)
        if tick-self.observed_at >= PROBE_UNAVAILABLE_LIMIT:
            self.fail("GUARD_PROBE_UNAVAILABLE_DEADLINE")
        if self.sample_job is None and tick >= self.next_sample:
            self.sample_job = self.b.start_job("sample", tick)
        if self.trip is not None and not self.trip_persisted:
            try:
                self.b.write("guard-trip.json", self.trip)
                self.trip_persisted = True
            except Exception:
                self.reason = "GUARD_TRIP_PERSIST_FAILED"
        if self.trip is not None and self.fence_job is None and self.fence_attempts < 3:
            self.fence_job = self.b.start_job("fence", tick)
            self.fence_attempts += 1
        if self.fence_job is not None and self.fence_job.poll():
            if self.fence_job.error is None:
                self.fence_attempts = 3
            self.fence_job = None
        try:
            supervisor_reason = self.supervise()
        except Exception:
            supervisor_reason = "GUARD_SUPERVISOR_UNCERTAIN"
        self.reason = supervisor_reason or self.reason or (self.trip.get("reason") if self.trip else self.probe_reason)
        checked = self.monotonic()
        fresh = self.sample_at is not None and 0 <= checked-self.sample_at <= SAMPLE_FRESH
        protected = bool(fresh and self.last_sample and self.last_sample.get("pg_wal_protected") is True)
        status = {"v": 1, "run": RUN, "owner": self.owner, "boot_id": self.boot, "checked_at": self.now(),
                  "healthy": bool(protected and self.trip is None and supervisor_reason is None), "pg_wal_protected": protected,
                  "tripped": self.trip is not None, "sample": copy.deepcopy(self.last_sample),
                  "recovering": self.takeover_at is not None, "reason": self.reason,
                  "probe_failures": self.probe_failures}
        try:
            self.b.write("guard-status.json", status)
        except Exception:
            # Leave the old heartbeat to expire; keep the isolated fence alive.
            self.reason = "GUARD_HEARTBEAT_PERSIST_FAILED"
        return status


def main():
    if sys.argv[1:] != ["guard"]:
        print(json.dumps({"offline": True, "run": RUN, "local_actions": False,
                          "guard_unit": GUARD_UNIT, "main_unit": MAIN_UNIT}))
        return
    backend = LocalBackend()
    # Guard has its own singleton lock; execution.lock belongs to the runner.
    with backend.singleton():
        guard = Guard(backend)
        while True:
            guard.tick()
            time.sleep(HEARTBEAT_INTERVAL)


if __name__ == "__main__":
    main()
