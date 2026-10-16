#!/usr/bin/env python3
"""Maintain one reviewed rolling-recovery NAT rule. No arguments is offline.

Root installs this fixed program and creates route-keeper root:root 0700.
`inspect` reads only. `ensure` takes a separate private lock, verifies adopted
container evidence, then either reads an exact unique first rule or records a
boot-specific durable intent before one insertion. An uncertain same-boot
intent never repeats a missing insertion. There is no deletion, flush,
conntrack operation, app lifecycle action, Caddy operation or controller call.
"""
import argparse
import copy
import hashlib
import importlib.util
import ipaddress
import json
import os
import pathlib
import re
import stat
import sys

ROOT = pathlib.Path("/var/lib/sub2api-migration-20261016")
SCOPE = ROOT / "rolling-recovery"
STATE = SCOPE / "route-keeper"
LIB = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016")
PROGRAM = LIB / "rolling_app_route.py"


class Refusal(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise Refusal(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def trusted_read(path, mode=0o600, limit=2 * 1024 * 1024):
    """Bootstrap the hash-pinned modules without executing unverified code."""
    path = pathlib.Path(path)
    for parent in [path.parent, *path.parent.parents]:
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "UNTRUSTED_PARENT")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "UNTRUSTED_FILE")
        raw = b""
        while len(raw) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        need(len(raw) <= limit, "FILE_TOO_LARGE")
        return raw
    finally:
        os.close(fd)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "DUPLICATE_KEY")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(Refusal("JSON_CONSTANT")))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def route_status(nat, rule):
    """Normalize only the exact owned rule; reject other uses of its comment."""
    comment = rule[rule.index("--comment") + 1]
    owned = []
    for section in ("output", "other"):
        for index, row in enumerate(nat[section]):
            if any(row[i] == "--comment" and row[i + 1] == comment for i in range(len(row) - 1)):
                owned.append((section, index, row))
    need(len(owned) <= 1 and all(row == rule and section == "output" for section, _, row in owned), "OWNED_ROUTE_CONFLICT")
    if owned:
        need(owned[0][1] == 0, "OWNED_ROUTE_NOT_FIRST")
        return "present"
    for row in nat["output"]:
        if touches_app_destination(row):
            need(False, "OUTPUT_CHAIN_CONFLICT")
    return "absent"


def touches_app_destination(row):
    """A disjoint destination/protocol/port proves an unrelated OUTPUT rule.

    Unknown or broad jumps remain conflicts. Docker's usual !127/8 OUTPUT
    jump is demonstrably disjoint and is preserved byte-for-byte as tokens.
    """
    for index, token in enumerate(row[:-1]):
        value = row[index + 1]
        negated = index > 0 and row[index - 1] == "!"
        if token in ("-d", "--destination"):
            try:
                matches = ipaddress.ip_address("127.0.0.1") in ipaddress.ip_network(value)
            except ValueError:
                return True
            if matches == negated:
                return False
        elif token in ("-p", "--protocol") and not negated and value not in ("tcp", "6", "all", "0"):
            return False
        elif token in ("--dport", "--destination-port") and not negated:
            try:
                low, high = value.split(":", 1) if ":" in value else (value, value)
                if not int(low or "0") <= 8082 <= int(high or "65535"):
                    return False
            except ValueError:
                return True
        elif token == "--uid-owner" and not negated and value.isdigit() and int(value) != 1000:
            return False
    return True


def validate_adoption(receipt, rolling, config, old, new, image, *, controller_raw, rolling_raw, source_raw, ledger_raw,
                      token_sha, mounts, core):
    fields = {"v", "run", "owner", "old_instance_id", "new_instance_id", "old_container_id", "new_container_id",
              "new_app_config_sha256", "new_control_port", "new_control_token_sha256", "controller_config_raw_sha256",
              "ledger_raw_sha256", "ledger_epoch", "operation_floor", "source_closed_sha256", "rolling_complete_sha256", "old_clean_exit"}
    need(type(receipt) is dict and set(receipt) == fields and receipt["v"] == 1 and receipt["run"] == "20261016" and
         receipt["owner"] == config["owner"] and receipt["old_container_id"] == core.OLD_ID and
         receipt["new_container_id"] != receipt["old_container_id"] and
         type(receipt["ledger_epoch"]) is int and receipt["ledger_epoch"] == 14 and
         type(receipt["operation_floor"]) is int and receipt["operation_floor"] == 14 and
         all(isinstance(receipt[key], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", receipt[key])
             for key in ("old_instance_id", "new_instance_id")) and receipt["old_instance_id"] != receipt["new_instance_id"],
         "ADOPTION_RECEIPT_INVALID")
    app = config["app"]
    need(receipt["controller_config_raw_sha256"] == sha(controller_raw) and receipt["rolling_complete_sha256"] == sha(rolling_raw) and
         receipt["source_closed_sha256"] == sha(source_raw) and receipt["ledger_raw_sha256"] == sha(ledger_raw), "ADOPTION_EVIDENCE_CHANGED")
    need(receipt["new_container_id"] == app["container_id"] == new["Id"] and
         receipt["new_app_config_sha256"] == app["config_sha256"] == core.sha(new["Config"]) and
         receipt["new_control_port"] == app["control_port"] == 18093 and
         receipt["new_control_token_sha256"] == token_sha and new["Name"] == "/sub2api-migration-app-20261016",
         "ADOPTED_APPLICATION_CHANGED")
    need(rolling.get("v") == 1 and rolling.get("owner") == config["owner"] and rolling.get("phase") == "COMPLETE" and
         rolling.get("intent") is None, "ROLLING_NOT_COMPLETE")
    old_identity, new_identity, completed = (rolling.get(key, {}) for key in ("old_identity", "new_identity", "adoption_receipt"))
    need(old_identity.get("container_id") == old["Id"] == receipt["old_container_id"] and
         old_identity.get("config_sha256") == core.sha(old["Config"]) and
         new_identity.get("container_id") == new["Id"] and new_identity.get("config_sha256") == app["config_sha256"] and
         old_identity.get("image_id") == new_identity.get("image_id") == app["image_id"] == old["Image"] == new["Image"] == image["Id"] and
         old_identity.get("rootfs_diffids") == new_identity.get("rootfs_diffids") == app["rootfs_diffids"] == image["RootFS"]["Layers"] and
         new_identity.get("control_token_sha256") == token_sha, "ADOPTED_IMAGE_OR_IDENTITY_CHANGED")
    need(completed.get("v") == 1 and completed.get("owner") == config["owner"] and completed.get("new_app") == new_identity and
         completed.get("old_container_id") == old["Id"] and completed.get("redirect") == core.REDIRECT and
         completed.get("control_port") == 18093 and completed.get("control_token_file") == str(core.TOKEN_FILE) and
         completed.get("data_directory") == str(core.DATA) and completed.get("endpoint_directory") == str(ROOT / "app") and
         completed.get("old_exited_cleanly") is True and completed.get("new_process_failed") == 0 and
         completed.get("new_process_dropped") == 0 and mounts == core.MOUNTS, "ROLLING_ADOPTION_CHANGED")
    clean = {"container_id": old["Id"], "exit_code": 0, "oom_killed": False, "pid": 0, "restart_policy": "no"}
    need(receipt["old_clean_exit"] == clean and old["State"]["Running"] is False and old["State"]["Status"] == "exited" and
         old["State"]["Pid"] == 0 and old["State"]["ExitCode"] == 0 and old["State"]["OOMKilled"] is False and
         old["HostConfig"]["RestartPolicy"]["Name"] == "no", "OLD_APPLICATION_NOT_RETIRED")
    need(new["HostConfig"]["RestartPolicy"]["Name"] == "no" and new["HostConfig"]["NetworkMode"] == "host" and
         not new["HostConfig"].get("PortBindings") and new["State"]["Status"] in ("created", "running", "exited") and
         new["State"]["OOMKilled"] is False, "NEW_APPLICATION_LIFECYCLE_CHANGED")


class Route:
    def __init__(self, backend, store, rule, owner, binding):
        self.b, self.s, self.rule, self.owner, self.binding = backend, store, list(rule), owner, binding

    def inspect(self):
        self.b.verify()
        nat = self.b.nat()
        return {"ok": True, "status": route_status(nat, self.rule), "boot_id": self.s.boot_id,
                "intent": self.s.read(), "nat": nat, "production_mutation": False}

    def ensure(self):
        with self.s.lock():
            self.b.verify()
            before = self.b.nat()
            status, intent = route_status(before, self.rule), self.s.read()
            if intent is not None:
                need(type(intent) is dict and intent.get("v") == 1 and intent.get("owner") == self.owner and
                     intent.get("boot_id") == self.s.boot_id and intent.get("binding") == self.binding and
                     intent.get("rule") == self.rule and intent.get("status") in ("INTENT", "UNKNOWN", "COMPLETE"),
                     "BOOT_ROUTE_INTENT_CHANGED")
            if status == "present":
                if intent is not None and intent["status"] in ("INTENT", "UNKNOWN"):
                    expected = copy.deepcopy(intent["nat_before"])
                    expected["output"].insert(0, self.rule)
                    need(before == expected, "UNKNOWN_ROUTE_BASELINE_CHANGED")
                    intent.update(status="COMPLETE", nat_after=before)
                    self.s.write(intent)
                return {"ok": True, "status": "present", "boot_id": self.s.boot_id, "inserted": False}
            need(intent is None, "SAME_BOOT_ROUTE_MISSING_INSPECT_ONLY")
            intent = {"v": 1, "owner": self.owner, "boot_id": self.s.boot_id, "binding": self.binding,
                      "status": "INTENT", "rule": self.rule, "nat_before": before}
            self.s.write(intent)
            try:
                self.b.verify()
                need(self.b.nat() == before, "NAT_CHANGED_BEFORE_INSERT")
                self.b.insert()
                after = self.b.nat()
                expected = copy.deepcopy(before)
                expected["output"].insert(0, self.rule)
                need(after == expected and route_status(after, self.rule) == "present", "NAT_INSERT_RESULT_UNKNOWN")
                intent.update(status="COMPLETE", nat_after=after)
                self.s.write(intent)
                return {"ok": True, "status": "present", "boot_id": self.s.boot_id, "inserted": True}
            except Exception:
                intent["status"] = "UNKNOWN"
                try:
                    self.s.write(intent)
                except Exception:
                    pass
                raise Refusal("ROUTE_OUTCOME_UNKNOWN_INSPECT_ONLY") from None


class FileStore:
    def __init__(self, rpc):
        self.r = rpc
        rpc.trusted_directory(STATE, private=True)
        self.boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        need(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", self.boot_id), "BOOT_ID_INVALID")
        self.path = STATE / ("intent-" + self.boot_id + ".json")

    def lock(self):
        return self.r.file_lock(STATE / "operation.lock", nonblocking=True)

    def read(self):
        if not self.path.exists():
            need(not self.path.is_symlink(), "UNTRUSTED_INTENT")
            return None
        return self.r.strict_json(self.r.private_read(self.path), 2 * 1024 * 1024)

    def write(self, value):
        self.r.atomic_json(self.path, value)


class Backend:
    def __init__(self, core, host, rpc, config, controller_raw, binding):
        self.m, self.h, self.r, self.config = core, host, rpc, config
        self.controller_raw, self.binding = controller_raw, binding
        self.local = rpc.LocalBackend(config)

    def verify(self):
        for filename, key in (("rolling_app_route.py", "route_program_sha256"), ("rolling_app_recovery.py", "core_sha256"),
                              ("rolling_app_host.py", "host_sha256")):
            need(sha(self.h.private_read(LIB / filename, mode=0o700)) == self.binding[key], "ROUTE_PROGRAM_BINDING_CHANGED")
        need(self.r.load_config() == self.config and self.r.private_read(self.r.SCOPE / "config.json") == self.controller_raw,
             "CONTROLLER_CONFIG_CHANGED")
        receipt_raw = self.h.private_read(SCOPE / "controller-adoption.json")
        need(sha(receipt_raw) == self.binding["adoption_sha256"], "ADOPTION_RECEIPT_CHANGED")
        receipt = self.r.strict_json(receipt_raw, 64 * 1024)
        rolling_raw = self.h.private_read(SCOPE / "state.json")
        rolling = self.r.strict_json(rolling_raw, 2 * 1024 * 1024)
        old, new = (self.local.docker_json("container", cid) for cid in (self.m.OLD_ID, self.r.APP_CONTAINER))
        image = self.local.docker_json("image", self.config["app"]["image_id"])
        mounts = sorted([{"source": x["Source"], "destination": x["Destination"], "rw": x["RW"]}
                         for x in new["Mounts"]], key=lambda item: item["destination"])
        token_sha = sha(self.local._app_token())
        need(token_sha == sha(self.h.private_read(self.m.TOKEN_FILE, uid=0).strip()), "CONTROL_TOKEN_CHANGED")
        validate_adoption(receipt, rolling, self.config, old, new, image, controller_raw=self.controller_raw, rolling_raw=rolling_raw,
                          source_raw=self.h.private_read(SCOPE / "source-controller-closed.json"),
                          ledger_raw=self.h.private_read(SCOPE / "controller-ledger-before.json"), token_sha=token_sha,
                          mounts=mounts, core=self.m)

    def nat(self):
        return self.h.parse_nat(self.local.command(["/usr/sbin/iptables-save", "-t", "nat"]), self.m.REDIRECT)

    def insert(self):
        self.local.command(["/usr/sbin/iptables", "-w", "2", "-t", "nat", "-I", "OUTPUT", "1"] + self.m.REDIRECT[2:])


def load_route():
    need(sys.platform == "linux" and os.geteuid() == 0 and pathlib.Path(__file__).absolute() == PROGRAM,
         "INSTALLED_LINUX_ROOT_PROGRAM_REQUIRED")
    program_sha = sha(trusted_read(PROGRAM, 0o700))
    host_config = strict_json(trusted_read(SCOPE / "host-config.json"))
    fields = {"v", "owner", "host_sha256", "core_sha256", "rpc_sha256", "source_recovery_sha256", "source_controller_sha256"}
    need(type(host_config) is dict and set(host_config) == fields and host_config["v"] == 1 and
         re.fullmatch(r"[0-9a-f]{32}", host_config["owner"]), "HOST_CONFIG_INVALID")
    for name, key in (("rolling_app_host.py", "host_sha256"), ("rolling_app_recovery.py", "core_sha256")):
        need(sha(trusted_read(LIB / name, 0o700)) == host_config[key], "CORE_OR_HOST_PROGRAM_CHANGED")
    controller_raw = trusted_read(ROOT / "rpc" / "config.json")
    bootstrap_config = strict_json(controller_raw)
    need(re.fullmatch(r"[0-9a-f]{64}", bootstrap_config["program_sha256"]) and
         sha(trusted_read(LIB / "target_rpc.py", 0o700)) == bootstrap_config["program_sha256"], "RPC_PROGRAM_CHANGED")
    core = load_module("route_core", LIB / "rolling_app_recovery.py")
    host = load_module("route_host", LIB / "rolling_app_host.py")
    rpc = load_module("route_rpc", LIB / "target_rpc.py")
    config = rpc.load_config()
    need(config == bootstrap_config and config["owner"] == host_config["owner"], "CONTROLLER_IDENTITY_CHANGED")
    receipt_sha = sha(host.private_read(SCOPE / "controller-adoption.json"))
    binding = {"route_program_sha256": program_sha, "core_sha256": host_config["core_sha256"],
               "host_sha256": host_config["host_sha256"], "rpc_sha256": config["program_sha256"],
               "controller_config_sha256": sha(controller_raw), "adoption_sha256": receipt_sha}
    return Route(Backend(core, host, rpc, config, controller_raw, binding), FileStore(rpc), core.REDIRECT, config["owner"], binding)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("inspect", "ensure"))
    args = parser.parse_args(argv)
    if args.action is None:
        print(json.dumps({"offline": True, "apply": False, "scope": str(STATE)}, sort_keys=True))
        return 0
    try:
        route = load_route()
        result = route.inspect() if args.action == "inspect" else route.ensure()
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # Never echo container env, private token, raw inspect or exception text.
        code = str(exc) if isinstance(exc, Refusal) else "ROUTE_CHECK_OR_EFFECT_UNCERTAIN"
        print(json.dumps({"ok": False, "code": code, "same_boot_unknown_is_inspect_only": True}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
