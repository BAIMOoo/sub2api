#!/usr/bin/env python3
"""Adopt fixed Tencent services; recover stopped originals after trusted COMPLETE.

No arguments is entirely offline. Only installed root `run` can start anything.
The leader transfers arm.json after fresh AWS COMPLETE/source-retirement proof.
This program never stops, signals, recreates, promotes, or changes a container.
"""
import importlib.util
import json
import math
import os
import pathlib
import re
import stat
import sys
import time

RUN = "20261016"
PROGRAM = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016/target_keeper.py")
RPC_PROGRAM = PROGRAM.with_name("target_rpc.py")
ROUTE_PROGRAM = PROGRAM.with_name("rolling_app_route.py")
ROUTE_SHA = "e958f4a975db00b7b5a55facaa43f99e9511949aa29727a7d6523682c798c8f1"
SCOPE = pathlib.Path("/var/lib/sub2api-migration-20261016/keeper")
ENTRY = pathlib.Path("/home/ubuntu/sub2api-migration-20261016/entry")
ENTRY_UNIT = "sub2api-tencent-entry.service"
ENTRY_UNIT_SHA = "0ea178533c2522b39136eb17da18471896201b15930a4b8e35a38c4788e6e893"
CADDY_SHA = "9681d054b6e5137d53f6d027188b349e0603fd12ea6334bd0d49da6755432f1b"
ENTRY_CONFIG_SHA = "077de5dac0181a7319c724e9131727bca6b0357e86762442f27308b8eaf56f45"
HEX64 = r"[0-9a-f]{64}"
MAX_STARTS = 3  # Per component and arm, including failed/ambiguous starts.


class Refusal(Exception):
    pass


def need(condition, code):
    if not condition:
        raise Refusal(code)


def exact(value, fields, code):
    need(type(value) is dict and set(value) == set(fields.split()), code)


def normalized_redis(raw, sha):
    """Allow only the reviewed startup's parameter-free module normalization."""
    need(isinstance(raw, bytes) and len(raw) <= 65536, "REDIS_CONFIG_INVALID")
    lines = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(b"#"):
            continue
        parts = line.split()
        name = parts[0].lower()
        need(name not in (b"include", b"replicaof", b"slaveof"), "REDIS_REPLICA_CONFIG_FORBIDDEN")
        if name == b"loadmodule":
            need(len(parts) == 2 and re.fullmatch(rb"/usr/local/lib/redis/modules/+[^/\s]+\.so", parts[1]),
                 "REDIS_MODULE_CONFIG_CHANGED")
            continue
        lines.append(b" ".join(parts))
    need(bool(lines), "REDIS_CONFIG_EMPTY")
    return sha(b"\n".join(lines) + b"\n")


# Exact evidence for the retired legacy shell wrapper. Redis saved and exited;
# the wrapper subsequently returned 127. Keep the original pending operation and
# actual Docker exit code rather than rewriting either as a successful signal.
SOURCE_REDIS_SHELL_EXIT = {
    "container_id": "e2f41a97ee0a36a9a5bd3e36b8f65874411419b3dae3867097323b3e730c6835",
    "container_exit_code": 127,
    "shutdown_ack_sha256": "d3d04d6c1629590847db0c77200e1795b3b3d27a7a4fc37183d77e97ba5b7b18",
    "shutdown_log_sha256": "739b00e22555c6a01e4af138dcd1e3b0cf0b021eede6bff2d98f6cb1a5d41e74",
    "wrapper_log_sha256": "9383ce5789fec964a23935052fa1a9f06850125a518043ef4d4a4b4d096bf510",
    "prior_op_id": "1cbf783b8b944a2814839cfa9c51946f",
    "prior_op_phase": "pending",
}


def validate_arm(arm, cfg, rpc, program_sha):
    exact(arm, "v run owner machine_id_sha256 rpc_config_sha256 program_sha256 armed_at complete "
          "pg_intent_sha256 redis_persist_op_id redis_normalized_config_sha256", "ARM_FIELDS")
    need(type(arm["v"]) is int and arm["v"] == 1 and arm["run"] == RUN and
         arm["owner"] == cfg["owner"] and arm["machine_id_sha256"] == cfg["machine_id_sha256"] and
         arm["rpc_config_sha256"] == rpc.sha(cfg) and arm["program_sha256"] == program_sha,
         "ARM_IDENTITY_CHANGED")
    for field in ("pg_intent_sha256", "redis_normalized_config_sha256"):
        need(rpc.matches(arm[field], HEX64), "ARM_DIGEST_INVALID")
    need(rpc.matches(arm["redis_persist_op_id"], rpc.HEX32), "ARM_REDIS_OP_INVALID")
    complete = arm["complete"]
    need(type(complete) is dict, "COMPLETE_FIELDS")
    wait_breach = complete.get("phase") == "COMPLETE_WITH_WAIT_BREACH"
    shell_exit = "source_redis_shell_exit" in complete
    exact(complete, "phase pg redis window verified_at state_sha256 verification_sha256 "
          "source_machine_id_sha256 source_inspect_sha256 source_tombstones_sha256 source_resources" +
          (" wait_budget_breach" if wait_breach else "") +
          (" source_redis_shell_exit" if shell_exit else ""), "COMPLETE_FIELDS")
    if shell_exit:
        need(wait_breach and type(complete["source_redis_shell_exit"]) is dict and
             rpc.canonical(complete["source_redis_shell_exit"]) == rpc.canonical(SOURCE_REDIS_SHELL_EXIT),
             "SOURCE_SHELL_EXIT_EVIDENCE_INVALID")
    need(complete["phase"] in ("COMPLETE", "COMPLETE_WITH_WAIT_BREACH") and
         complete["pg"] == complete["redis"] == "TENCENT" and
         complete["window"] is None, "COMPLETE_REQUIRED")
    if wait_breach:
        breach = complete["wait_budget_breach"]
        exact(breach, "deadline_breached max_cohort_pause_seconds user_limit_seconds", "WAIT_BREACH_FIELDS")
        need(breach["deadline_breached"] is True and
             type(breach["user_limit_seconds"]) in (int, float) and breach["user_limit_seconds"] == 10 and
             type(breach["max_cohort_pause_seconds"]) in (int, float) and
             math.isfinite(breach["max_cohort_pause_seconds"]) and breach["max_cohort_pause_seconds"] > 10,
             "WAIT_BREACH_INVALID")
    for field in ("state_sha256", "verification_sha256", "source_machine_id_sha256",
                  "source_inspect_sha256", "source_tombstones_sha256"):
        need(rpc.matches(complete[field], HEX64), "COMPLETE_DIGEST_INVALID")
    for value in (arm["armed_at"], complete["verified_at"]):
        need(type(value) in (int, float) and math.isfinite(value) and value > 0, "ARM_TIME_INVALID")
    # Freshness is checked at transfer; an accepted arm intentionally survives boots.
    need(0 <= arm["armed_at"] - complete["verified_at"] <= 30, "COMPLETE_PROOF_WAS_NOT_FRESH")
    exact(complete["source_resources"], "app postgres redis", "SOURCE_RESOURCE_FIELDS")
    need(len({row.get("id") for row in complete["source_resources"].values() if type(row) is dict}) == 3,
         "SOURCE_RESOURCE_IDS_NOT_DISTINCT")
    for kind, row in complete["source_resources"].items():
        exact(row, "id running status restart exit_code oom_killed", "SOURCE_RETIREMENT_FIELDS")
        expected_exit = 0
        if shell_exit and kind == "redis":
            need(row["id"] == SOURCE_REDIS_SHELL_EXIT["container_id"], "SOURCE_SHELL_EXIT_CONTAINER_CHANGED")
            expected_exit = 127
        need(rpc.matches(row["id"], HEX64) and row["running"] is False and row["status"] == "exited" and
             row["restart"] == "no" and type(row["exit_code"]) is int and row["exit_code"] == expected_exit and
             row["oom_killed"] is False, "SOURCE_NOT_CLEANLY_RETIRED")
    return arm


class Store:
    def __init__(self, rpc, cfg, scope=SCOPE, uid=0):
        self.r, self.cfg, self.scope, self.uid = rpc, cfg, pathlib.Path(scope), uid

    def arm(self, program_sha):
        self.r.trusted_directory(self.scope, self.uid, private=True)
        value = self.r.strict_json(self.r.private_read(self.scope / "arm.json", self.uid), 65536)
        return validate_arm(value, self.cfg, self.r, program_sha)

    def journal(self, arm):
        try:
            value = self.r.strict_json(self.r.private_read(self.scope / "state.json", self.uid), 1024 * 1024)
        except FileNotFoundError:
            value = {"v": 1, "run": RUN, "owner": self.cfg["owner"], "arm_sha256": self.r.sha(arm),
                     "starts": {}, "activations": {}}
        exact(value, "v run owner arm_sha256 starts activations", "KEEPER_STATE_FIELDS")
        need(type(value["v"]) is int and value["v"] == 1 and value["run"] == RUN and
             value["owner"] == self.cfg["owner"] and value["arm_sha256"] == self.r.sha(arm) and
             type(value["starts"]) is dict and type(value["activations"]) is dict and
             len(value["starts"]) <= MAX_STARTS * 4 and len(value["activations"]) < 1024, "KEEPER_STATE_CHANGED")
        return value

    def save(self, value):
        need(len(self.r.canonical(value)) < 1024 * 1024, "KEEPER_STATE_FULL")
        self.r.atomic_json(self.scope / "state.json", value, self.uid)


def backend_class(rpc):
    class Backend(rpc.LocalBackend):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.entry_files = {}

        def boot_id(self):
            value = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            need(re.fullmatch(r"[0-9a-f-]{36}", value), "BOOT_ID_INVALID")
            return value

        def inspect(self, kind):
            expected = self.config[kind]
            name = {"pg": rpc.PG_CONTAINER, "redis": rpc.REDIS_CONTAINER, "app": rpc.APP_CONTAINER}[kind]
            actual = self.docker_json("container", name)
            need(actual["Id"] == expected["container_id"] and actual["Image"] == expected["image_id"] and
                 rpc.sha(actual["Config"]) == expected["config_sha256"], "RESOURCE_IDENTITY_CHANGED")
            image = self.docker_json("image", expected["image_id"])
            need(image["Id"] == expected["image_id"] and image["RootFS"]["Layers"] == expected["rootfs_diffids"], "IMAGE_CHANGED")
            host, state = actual["HostConfig"], actual["State"]
            need(host["NetworkMode"] == "host" and not host.get("PortBindings") and
                 host["RestartPolicy"]["Name"] == "no", "RESOURCE_HOST_CONFIG_CHANGED")
            need(type(state["Running"]) is bool and rpc.integer(state["Pid"]) and
                 not state.get("Paused") and not state.get("Restarting") and
                 not state.get("Dead") and ((state["Running"] is True and state["Status"] == "running" and state["Pid"] > 0) or
                 (state["Running"] is False and state["Status"] == "exited" and state["Pid"] == 0)), "RESOURCE_STATE_UNCERTAIN")
            if kind != "app":
                # Reuse the complete RPC volume/label checks even for a stopped
                # original. The running precondition is reproduced separately.
                volume_name, destination = (rpc.PG_VOLUME, rpc.PG_DATA) if kind == "pg" else (rpc.REDIS_VOLUME, "/data")
                run = "sub2api-pg-replica-20261016" if kind == "pg" else "sub2api-redis-standby-20261016"
                labels, volume = actual["Config"].get("Labels", {}), self.docker_json("volume", volume_name)
                need(labels.get(rpc.OWNER_LABEL) == expected["owner"] and labels.get("io.sub2api.migration.run") == run and
                     volume["Driver"] == "local" and volume["Mountpoint"] == expected["volume_path"] and
                     volume.get("Labels", {}).get(rpc.OWNER_LABEL) == expected["owner"] and
                     volume.get("Labels", {}).get("io.sub2api.migration.run") == run, "RESOURCE_VOLUME_CHANGED")
                mounts = [item for item in actual["Mounts"] if item["Destination"] == destination]
                need(len(mounts) == 1 and mounts[0]["Type"] == "volume" and mounts[0]["Name"] == volume_name and
                     mounts[0]["Source"] == expected["volume_path"] and mounts[0]["RW"] is True, "RESOURCE_MOUNT_CHANGED")
                rpc.trusted_directory(pathlib.Path(expected["volume_path"]).parent)
                need(not pathlib.Path(expected["volume_path"]).is_symlink(), "RESOURCE_VOLUME_SYMLINK")
            else:
                expected_mounts = {"/app/data": (str(rpc.PREFIX / "rolling-recovery/app/data"), True),
                                   "/migration-state": (str(rpc.PREFIX / "app"), False)}
                need(len(actual["Mounts"]) == 2 and {item["Destination"] for item in actual["Mounts"]} == set(expected_mounts),
                     "APP_MOUNT_CHANGED")
                for item in actual["Mounts"]:
                    need(item["Destination"] in expected_mounts and item["Type"] == "bind" and
                         (item["Source"], item["RW"]) == expected_mounts[item["Destination"]], "APP_MOUNT_CHANGED")
                # Only the private writable app data leaf belongs to uid1000.
                # The endpoint directory and all its ancestors stay root-only.
                rpc.trusted_directory(rpc.PREFIX / "app")
                rpc.trusted_directory(rpc.PREFIX / "rolling-recovery/app")
                rpc.trusted_directory(rpc.PREFIX / "rolling-recovery/app/data", uid=1000, private=True)
            return actual

        def persistent(self, arm):
            cfg = rpc.validate_config(rpc.strict_json(rpc.private_read(rpc.SCOPE / "config.json"), 65536))
            need(rpc.sha(cfg) == arm["rpc_config_sha256"], "RPC_CONFIG_CHANGED")
            need(self.endpoint_state() == {"v": 1, "postgres": "target", "redis": "target"}, "TARGET_ENDPOINTS_REQUIRED")
            ledger = rpc.FileStore(cfg).read()
            need(ledger["owner"] == cfg["owner"] and ledger["pg"]["authority"] == ledger["redis"]["authority"] == "TENCENT" and
                 rpc.sha(ledger["pg"]["promotion_intent"]) == arm["pg_intent_sha256"], "AUTHORITY_NOT_COMPLETE")
            operation = ledger["operations"].get(arm["redis_persist_op_id"], {})
            need(ledger["redis"].get("op_id") == arm["redis_persist_op_id"] and operation.get("phase") == "COMPLETE" and
                 operation.get("request", {}).get("action") == "redis.persist-primary" and
                 operation.get("response", {}).get("ok") is True and
                 operation.get("response", {}).get("result", {}).get("persisted") is True, "REDIS_PERSISTENCE_UNPROVEN")
            need(not any(item.get("phase") != "COMPLETE" and rpc.TargetRPC.is_mutation(item["request"])
                         for item in ledger["operations"].values()), "MIGRATION_MUTATION_PENDING")
            with self.pg_locks():
                manifest = self.pg_manifest()
                need(manifest["authority_mode"] == "PRIMARY" and
                     rpc.sha(manifest["promotion_intent"]) == arm["pg_intent_sha256"], "PG_AUTHORITY_NOT_PRIMARY")

        def prestart(self, kind, arm):
            if kind == "pg":
                data = pathlib.Path(self.config["pg"]["volume_path"])
                need((data / "PG_VERSION").read_text().strip() == "18" and
                     (data / ".sub2api-seed-verified").is_file() and
                     (data / "global/pg_control").is_file() and
                     not (data / "standby.signal").exists() and not (data / "recovery.signal").exists(), "PG_START_ROLE_UNPROVEN")
                need(not any(item.is_symlink() for item in (data / "PG_VERSION", data / ".sub2api-seed-verified",
                         data / "standby.signal", data / "recovery.signal", data / "global", data / "global/pg_control")), "PG_DATA_SYMLINK")
            elif kind == "redis":
                raw, _ = self.redis_config()
                need(normalized_redis(raw, rpc.sha) == arm["redis_normalized_config_sha256"], "REDIS_CONFIG_CHANGED")
            else:
                self.primary(arm)
                self.master(arm)

        def primary(self, arm):
            with self.pg_locks():
                manifest, sample = self.pg_manifest(), self.pg_sample()
                intent = manifest["promotion_intent"]
                need(manifest["authority_mode"] == "PRIMARY" and rpc.sha(intent) == arm["pg_intent_sha256"], "PG_INTENT_CHANGED")
                rpc.TargetRPC(self.config, self, None)._pg_validate(sample, intent["final_lsn"], primary=True)

        def master(self, arm):
            raw, _ = self.redis_config()
            need(normalized_redis(raw, rpc.sha) == arm["redis_normalized_config_sha256"], "REDIS_CONFIG_CHANGED")
            info = self.redis_inspect()
            need(info["role"] == "master" and info["failover_state"] == "no-failover", "REDIS_NOT_MASTER")

        def start(self, kind):
            self.command(["/usr/bin/docker", "start", self.config[kind]["container_id"]])

        def ensure_route(self):
            # Execute exactly the trusted bytes just hashed, without a second
            # source read or a cached bytecode import. load_route binds the
            # current RPC config rather than host-config's historical RPC pin.
            raw = rpc.private_read(ROUTE_PROGRAM, mode=0o700, limit=2 * 1024 * 1024)
            need(rpc.sha(raw) == ROUTE_SHA, "ROUTE_PROGRAM_CHANGED")
            spec = importlib.util.spec_from_file_location("keeper_reviewed_route", ROUTE_PROGRAM)
            route = importlib.util.module_from_spec(spec)
            exec(compile(raw, str(ROUTE_PROGRAM), "exec"), route.__dict__)
            try:
                result = route.load_route().ensure()
            except route.Refusal as error:
                raise Refusal(str(error)) from None
            need(type(result) is dict and result.get("ok") is True and result.get("status") == "present",
                 "ROUTE_NOT_PROVEN_PRESENT")
            return result

        def control(self, name, epoch=0):
            response = self.app_control(name, epoch)
            need(response.get("http_status") == 200 and type(response.get("body")) is dict, "APP_CONTROL_UNAVAILABLE")
            return response["body"]

        def entry(self):
            for path, uid, mode, expected in ((pathlib.Path("/etc/systemd/system") / ENTRY_UNIT, 0, 0o644, ENTRY_UNIT_SHA),
                    (ENTRY / "Caddyfile", 1000, 0o600, ENTRY_CONFIG_SHA), (ENTRY / "caddy", 1000, 0o755, CADDY_SHA)):
                info = path.lstat()
                stamp = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if self.entry_files.get(path) != stamp:
                    need(rpc.sha(rpc.private_read(path, uid=uid, mode=mode, limit=128 * 1024**2)) == expected, "ENTRY_FILE_CHANGED")
                    self.entry_files[path] = stamp
            keys = "LoadState ActiveState SubState MainPID Job FragmentPath DropInPaths NeedDaemonReload".split()
            raw = self.command(["/usr/bin/systemctl", "show", ENTRY_UNIT, "--property=" + ",".join(keys)]).decode()
            value = dict(line.split("=", 1) for line in raw.splitlines())
            need(set(value) == set(keys) and value["LoadState"] == "loaded" and not value["Job"] and
                 value["FragmentPath"] == "/etc/systemd/system/" + ENTRY_UNIT and not value["DropInPaths"] and
                 value["NeedDaemonReload"] == "no" and value["MainPID"].isdigit(), "ENTRY_UNIT_CHANGED")
            return value

        def start_entry(self):
            self.command(["/usr/bin/systemctl", "start", ENTRY_UNIT])

    return Backend


class Keeper:
    def __init__(self, rpc, cfg, backend, store, program_sha):
        self.r, self.cfg, self.b, self.store, self.program_sha = rpc, cfg, backend, store, program_sha

    def save(self):
        self.store.save(self.state)

    def ensure_running(self, kind):
        actual = self.b.inspect(kind)
        if actual["State"]["Running"]:
            return actual
        self.b.prestart(kind, self.arm)
        key = self.r.sha({"kind": kind, "boot_id": self.b.boot_id(), "id": actual["Id"],
                          "started_at": actual["State"]["StartedAt"], "finished_at": actual["State"]["FinishedAt"]})
        starts = self.state["starts"]
        need(key not in starts, "START_EFFECT_UNCONFIRMED_OR_ALREADY_ATTEMPTED")
        need(sum(row["kind"] == kind for row in starts.values()) < MAX_STARTS, "START_BUDGET_EXHAUSTED")
        starts[key] = {"kind": kind, "phase": "INTENT", "container_id": actual["Id"]}
        self.save()  # Never send a start if fsync failed.
        try:
            self.b.start(kind)
        except Exception:
            pass  # The exact immutable ID is inspected even if the reply was lost.
        actual = self.b.inspect(kind)
        need(actual["State"]["Running"] is True, "START_EFFECT_UNCONFIRMED")
        starts[key]["phase"] = "COMPLETE"
        self.save()
        return actual

    def activate(self, actual):
        runtime = self.b.control("runtime.status")
        pg, redis = self.b.control("postgres.status"), self.b.control("redis.status")
        writers, ingress = self.b.control("writers.status"), self.b.control("ingress.status")
        need(runtime.get("serving") is True and runtime.get("draining") is False and runtime.get("ready") is True and
             pg.get("endpoint") == "target" and pg.get("paused") is False and
             redis.get("address") == "127.0.0.1:26379" and redis.get("paused") is False and
             redis.get("pubsub_cache_ready") is True and writers.get("held") is False and ingress.get("paused") is False,
             "APP_NOT_READY_ON_TARGETS")
        instance = runtime.get("instance_id")
        need(self.r.matches(instance, r"[A-Za-z0-9_.:-]{1,128}"), "APP_INSTANCE_INVALID")
        generation = {"container_id": actual["Id"], "boot_id": self.b.boot_id(),
                      "started_at": actual["State"]["StartedAt"], "pid": actual["State"]["Pid"], "instance_id": instance}
        key = self.r.sha(generation)
        status = self.b.control("background.status")
        need(type(status.get("pending")) is list and type(status.get("started")) is list and
             not status.get("failures"), "BACKGROUND_FAILED_OR_INVALID")
        complete = status.get("activated") is True and status.get("activating") is False and not status["pending"]
        previous = self.state["activations"].get(key)
        if complete:
            need(runtime.get("background_workers_enabled") is True, "BACKGROUND_RUNTIME_NOT_ENABLED")
            if not previous or previous["phase"] != "COMPLETE":
                self.state["activations"][key] = {**generation, "phase": "COMPLETE", "adopted": previous is None}
                self.save()
            return
        need(previous is None, "BACKGROUND_EFFECT_UNCONFIRMED")
        need(status.get("activated") is False and status.get("activating") is False and
             self.r.integer(status.get("epoch")), "BACKGROUND_ACTIVATION_STATE_UNCERTAIN")
        # Recheck both authorities and the same process immediately before POST.
        self.b.persistent(self.arm)
        self.b.primary(self.arm)
        self.b.master(self.arm)
        again = self.b.inspect("app")
        need(all(again["State"][field] == actual["State"][field] for field in ("Running", "StartedAt", "Pid")) and
             self.b.control("runtime.status").get("instance_id") == instance,
             "APP_GENERATION_CHANGED")
        self.state["activations"][key] = {**generation, "phase": "INTENT", "epoch": status["epoch"]}
        self.save()
        try:
            self.b.control("background.activate", status["epoch"])
        except Exception:
            pass  # Only observe afterward; never repeat this POST for this generation.
        after = self.b.control("background.status")
        need(after.get("activated") is True and after.get("activating") is False and
             type(after.get("pending")) is list and not after["pending"] and not after.get("failures") and
             self.b.control("runtime.status").get("instance_id") == instance and
             self.b.control("runtime.status").get("background_workers_enabled") is True,
             "BACKGROUND_EFFECT_UNCONFIRMED")
        self.state["activations"][key]["phase"] = "COMPLETE"
        self.save()

    def step(self):
        self.arm = self.store.arm(self.program_sha)
        self.state = self.store.journal(self.arm)
        self.b.persistent(self.arm)
        self.b.ensure_route()
        self.ensure_running("pg")
        self.b.primary(self.arm)
        self.ensure_running("redis")
        self.b.master(self.arm)
        actual = self.ensure_running("app")
        self.activate(actual)
        entry = self.b.entry()
        if entry["ActiveState"] == "active" and entry["SubState"] == "running" and int(entry["MainPID"]) > 0:
            return {"ready": True, "adopted": True}
        need(entry["ActiveState"] in ("inactive", "failed") and entry["MainPID"] == "0", "ENTRY_STATE_UNCERTAIN")
        # Recheck after component startup/activation, before recording or
        # issuing the entry start. Unknown route outcomes remain inspect-only.
        self.b.ensure_route()
        key = "entry:" + self.b.boot_id()
        need(key not in self.state["starts"], "ENTRY_START_ALREADY_ATTEMPTED")
        need(sum(row["kind"] == "entry" for row in self.state["starts"].values()) < MAX_STARTS, "START_BUDGET_EXHAUSTED")
        self.state["starts"][key] = {"kind": "entry", "phase": "INTENT"}
        self.save()
        try:
            self.b.start_entry()
        except Exception:
            pass
        entry = self.b.entry()
        need(entry["ActiveState"] == "active" and entry["SubState"] == "running" and int(entry["MainPID"]) > 0,
             "ENTRY_START_EFFECT_UNCONFIRMED")
        self.state["starts"][key]["phase"] = "COMPLETE"
        self.save()
        return {"ready": True, "adopted": False}


def unit():
    return f'''[Unit]
Description=Sub2API Tencent fixed runtime keeper after completed migration
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
UMask=0077
ExecStart=/usr/bin/python3 -I {PROGRAM} run
Restart=on-failure
RestartSec=5
KillMode=process
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(json.dumps({"offline": True, "network_access": False, "production_actions": False,
                          "requires_leader_complete_arm": True, "stop_signals_or_restart_running": False}))
        return 0
    need(argv == ["run"] and os.geteuid() == 0 and pathlib.Path(__file__).absolute() == PROGRAM,
         "INSTALLED_ROOT_RUN_REQUIRED")
    for path in RPC_PROGRAM.parents:
        info = path.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
             "RPC_IMPORT_DIRECTORY_UNTRUSTED")
    info = RPC_PROGRAM.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
         stat.S_IMODE(info.st_mode) == 0o700, "RPC_IMPORT_FILE_UNTRUSTED")
    spec = importlib.util.spec_from_file_location("keeper_reviewed_rpc", RPC_PROGRAM)
    rpc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rpc)
    cfg = rpc.load_config()
    store = Store(rpc, cfg)
    program_sha = rpc.sha(rpc.private_read(PROGRAM, mode=0o700))
    rpc.trusted_directory(SCOPE, private=True)
    with rpc.file_lock(SCOPE / "keeper.lock", nonblocking=True):
        keeper = Keeper(rpc, cfg, backend_class(rpc)(cfg), store, program_sha)
        last = None
        while True:
            try:
                keeper.step()
                code = "READY"
            except FileNotFoundError:
                code = "UNARMED_OR_REQUIRED_FILE_MISSING"
            except Exception as error:
                code = error.code if isinstance(error, rpc.Refusal) else str(error) if isinstance(error, Refusal) else "KEEPER_STATE_UNCERTAIN"
            if code != last:
                print(json.dumps({"keeper": code}), flush=True)
                last = code
            time.sleep(2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"keeper": "KEEPER_REFUSED"}), file=sys.stderr)
        raise SystemExit(1)
