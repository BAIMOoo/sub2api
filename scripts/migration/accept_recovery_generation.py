#!/usr/bin/env python3
"""Accept a proven replacement generation, then request the AWS main once.

Default is offline. --apply preserves runner code/schema and all history;
--inspect reads the durable receipt/start intent without resubmitting a start.
No target mutation, endpoint change, pause, signal or container action occurs
here. The separately supervised existing runner performs the later handover.
"""
import argparse
import base64
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
import types

RUN, OWNER = "20261016", "30bda011defad8149621c2630a2bd89c"
ROOT = pathlib.Path("/var/lib/sub2api-migration-20261016")
LIB = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016")
SCOPE = ROOT / "runner"
SAVED = SCOPE / "generation-acceptance-20261017"
FOLDER = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
DELTA_FILE = "recovery-generation-delta-tests-v2-20261017.json"
DELTA_SHA = "cbc5893a888f79d61cd9da0d545572bd876076ef089674771a8ad74f101ed3bf"
CANARY_SHA = "bf6c195f2410cedb5c86681aef6d93723a7ad1936573afec3053d20515261e47"
RPC_SHA = "8180879f75169f130b726536ee16957832a9d2bf9e193f93ad380ebd8264f6ec"
GUARD_SHA = "72cb81b27bcf8c4f316511ca0460f44af453ab51276d53cda82ba55e2cee8d05"
LEDGER_SHA = "abd784df64eaf0fb3d5d622f8025a221657078b731cf97d9859cee3ba8991ce3"
R2_DIR = SCOPE / "passed-selftest-20261017-r2"
R2_SHA = "769ebf8a2a81bfb0f7c8977234a1d5e82c08e3a90d4fc14a96e19c00183da32e"
R2_CLOSURE_SHA = "b07f7e0557ae82d886e2b4d866a84c8c4c89f6fafdabe3b56b0af3d445fe014b"
LIFETIME_PAUSE = 14.656846920028329
SOURCE_PROGRAMS = {"target_rpc.py", "runner_service.py", "runner_guard.py", "durable_runner.py"}
FROZEN = ("state.json", "transport-state.json", "source-operations.json", "runner-config.json",
          "guard-config.json", "source-manifest.json")
TEST_FILES = {"target_rpc_test.py", "adopt_recovery_app_test.py", "authorize_recovery_cohort_test.py",
              "runner_guard_test.py", "runner_service_test.py", "durable_runner_test.py", "rolling_app_host_test.py",
              "rolling_app_route_test.py", "target_keeper_test.py", "recovery_generation_canary_test.py"}


class Refusal(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def strict(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "duplicate-json-key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Refusal("nonfinite-json")))


def root_directory(path, private=False):
    path = pathlib.Path(path)
    for item in [path, *path.parents]:
        info = item.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "untrusted-directory")
    need(not private or stat.S_IMODE(path.stat().st_mode) == 0o700, "private-directory-required")


def root_read(path, mode=0o600, limit=8 * 1024**2):
    path = pathlib.Path(path)
    root_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "untrusted-file")
        raw = b""
        while len(raw) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        need(len(raw) <= limit, "file-too-large")
        return raw
    finally:
        os.close(fd)


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    sync(path.parent)


def load_bytes(name, path, raw):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def load_canary(raw):
    need(sha(raw) == CANARY_SHA, "reviewed-canary-program-changed")
    return load_bytes("acceptance_reviewed_canary", LIB / "recovery_generation_canary.py", raw)


def validate_delta(raw, folder=None):
    need(sha(raw) == DELTA_SHA, "reviewed-delta-declaration-changed")
    value = strict(raw)
    need(value.get("v") == 1 and value.get("run") == RUN and value.get("owner") == OWNER and value.get("ok") is True and
         value["programs"].get("target_rpc.py") == RPC_SHA and value["programs"].get("runner_guard.py") == GUARD_SHA and
         SOURCE_PROGRAMS <= value["programs"].keys() and
         {row["file"] for row in value["tests"]} == TEST_FILES and len(value["tests"]) == len(TEST_FILES) and
         all(row["exit_code"] == 0 and sha(row["output"].encode()) == row["output_sha256"] for row in value["tests"]),
         "delta-tests-not-complete-and-successful")
    if folder is not None:
        for name, expected in value["programs"].items():
            need(re.fullmatch(r"[a-z_]+\.py", name) and sha((folder / name).read_bytes()) == expected,
                 "local-tested-program-changed")
        for row in value["tests"]:
            need(sha((folder / row["file"]).read_bytes()) == row["test_sha256"], "local-tested-test-changed")
    return value


def target_check(canary):
    """Direct local reads only; never admit a request to the RPC ledger."""
    need(os.geteuid() == 0 and sys.platform == "linux", "target-linux-root-required")
    before = root_read(ROOT / "rpc/state.json")
    ledger = strict(before)
    need(sha(before) == LEDGER_SHA and ledger["epoch"] == 14 and
         all(row.get("phase") == "COMPLETE" for row in ledger["operations"].values()), "original-target-ledger-required")
    proof = canary.target_preflight()
    canary.validate_target_proof(proof)
    need(proof["target_ledger_sha256"] == LEDGER_SHA and proof["ledger_epoch"] == 14 and
         root_read(ROOT / "rpc/state.json") == before, "target-ledger-mutated-or-raced")
    return proof


def validate_guard(guard, now):
    need(guard.get("v") == 1 and guard.get("run") == RUN and guard.get("owner") == OWNER and
         guard.get("healthy") is True and guard.get("pg_wal_protected") is True and
         guard.get("tripped") is False and guard.get("recovering") is False and
         guard.get("sample", {}).get("mode") == "RUNNING_BOUNDED" and
         type(guard.get("checked_at")) in (int, float) and math.isfinite(guard["checked_at"]) and
         0 <= now - guard["checked_at"] <= 1, "fresh-healthy-source-guard-required")


def acceptance_evidence(snapshot, target, engine, canary, now):
    """Validate the complete immutable chain before the first acceptance write."""
    canary.validate_target_proof(target)
    need(-5 <= now - target["checked_at"] <= 20 and target["target_ledger_sha256"] == LEDGER_SHA and
         target["ledger_epoch"] == 14, "fresh-original-target-proof-required")
    state = strict(snapshot["state.json"])
    need(state.get("v") == 1 and state.get("run") == RUN and state.get("owner") == OWNER and
         state.get("phase") == "SOURCE_DATA" and state.get("attempt") == 3 and state.get("route_fenced") is True and
         state.get("pg") == state.get("redis") == "AWS" and state.get("window") is None and
         state.get("pause_seconds") == LIFETIME_PAUSE and state.get("deadline_breached") is False and
         state.get("budget_accounting_unknown", False) is False, "closed-recovered-source-boundary-required")
    budget = engine.budget_accounting(state)
    record = strict(snapshot["cohort-record.json"])
    previous_budget = engine.budget_accounting(record["previous_state"])
    cohort = strict(snapshot["cohort-verification.json"])
    drain = strict(snapshot["cohort-drain-proof.json"])
    expected_state = {**record["previous_state"], "budget_cohort": state["budget_cohort"]}
    need(sha(snapshot["cohort-record.json"]) == state["budget_cohort"]["record_sha256"] and
         state == expected_state and state.get("quiet_instance_id") == drain.get("old_instance_id") and
         record["id"] == state["budget_cohort"]["id"] == cohort.get("cohort_id") and record["owner"] == OWNER and
         record["run"] == RUN and record["proof_sha256"] == sha(snapshot["cohort-drain-proof.json"]) and
         engine.digest(record["previous_state"]) == record["previous_state_sha256"] and
         budget["cohort_pause_seconds"] == 0 and budget["total_pause_seconds"] == previous_budget["total_pause_seconds"] and
         budget["max_cohort_pause_seconds"] == previous_budget["max_cohort_pause_seconds"] <= 10 and
         cohort.get("ok") is True and cohort.get("request_pause") is False and cohort.get("main_started") is False and
         cohort.get("budget") == budget and cohort.get("lifetime_pause_seconds_preserved") is True and
         cohort.get("quiet_instance_id_preserved") is True and
         cohort.get("operation_ledgers_preserved") is True and drain.get("owner") == OWNER and
         drain.get("new_instance_id") == target["instance_id"] and drain.get("new_container_id") == canary.APP_ID and
         drain.get("adoption_sha256") == target["adoption_sha256"] and drain.get("ledger_sha256") == LEDGER_SHA and
         drain.get("ledger_epoch") == 14, "new-zero-budget-cohort-not-bound-to-generation")
    need(sha(snapshot["historical-r2-result.json"]) == R2_SHA and
         sha(snapshot["historical-r2-closure.json"]) == R2_CLOSURE_SHA and
         strict(snapshot["historical-r2-result.json"]).get("ok") is True and
         strict(snapshot["historical-r2-closure.json"]).get("ok") is True, "historical-r2-evidence-changed")
    validate_delta(snapshot["delta-tests.json"])
    report = strict(snapshot["canary.json"])
    need(report.get("v") == 1 and report.get("run") == RUN and report.get("lane") == "recovery-generation" and
         report.get("phase") == "COMPLETE" and report.get("ok") is True and
         report.get("awaiting_target_postflight") is False and report.get("request_attempted") is True and
         report.get("post_intent_durable") is True and report.get("no_automatic_retry") is True and
         report.get("production_routes_changed") is False and not report.get("error") and not report.get("stream_error") and
         type(report.get("finished_unix_seconds")) in (int, float) and
         0 <= now - report["finished_unix_seconds"] <= 300, "fresh-successful-new-generation-canary-required")
    for field in ("target_preflight", "target_postflight"):
        canary.validate_target_proof(report[field], fresh=False)
        need({k: v for k, v in report[field].items() if k != "checked_at"} ==
             {k: v for k, v in target.items() if k != "checked_at"}, "canary-generation-or-authority-changed")
    expected_cohort = {"id": record["id"], "record_sha256": sha(snapshot["cohort-record.json"]),
        "proof_sha256": sha(snapshot["cohort-drain-proof.json"]), "adoption_sha256": target["adoption_sha256"],
        "instance_id": target["instance_id"]}
    stream, billing = report.get("stream", {}), report.get("billing", {})
    need(report.get("source_cohort") == expected_cohort and
         stream.get("terminal_event") == "response.completed" and stream.get("eof_after_terminal") is True and
         billing.get("usage_row_count") == billing.get("billing_dedup_row_count") == 1 and
         billing.get("tokens_match_stream") is True and
         billing.get("request_id") == "client:" + stream.get("effective_client_request_id", "") and
         all(report.get(field, {}).get("primary") is True and
             report[field].get("system_identifier") == canary.SYSTEM_ID
             for field in ("source_preflight", "source_postflight", "source_finalflight")), "canary-stream-billing-or-cohort-unproven")
    validate_guard(snapshot["guard"], now)
    gates = strict(snapshot["deployment-gates.json"])
    need(type(gates) is dict and set(gates) == {"v", "run", "owner", "accepted", "app_image_id", "app_binary_sha256",
         "rehearsal_sha256", "live_canary_sha256", "entry_tls_sha256", "source_deployment_paths_fenced"} and
         gates["v"] == 1 and gates["run"] == RUN and gates["owner"] == OWNER and gates["accepted"] is True and
         gates["source_deployment_paths_fenced"] is True and gates["app_image_id"] == canary.APP_IMAGE and
         gates["app_binary_sha256"] == "0bdcaa371aa45abfa1eb61c763d2da10f133d718c4ad93251587ca0cc6191724" and
         all(isinstance(gates[key], str) and re.fullmatch(r"[0-9a-f]{64}", gates[key])
             for key in ("rehearsal_sha256", "live_canary_sha256", "entry_tls_sha256")), "original-deployment-gates-invalid")
    live = {"v": 1, "kind": "accepted-recovery-generation-live-canary", "canary_sha256": sha(snapshot["canary.json"]),
            "adoption_sha256": target["adoption_sha256"], "cohort": expected_cohort, "target_proof_sha256": sha(canonical(target))}
    rehearsal = {"v": 1, "kind": "historical-r2-plus-recovery-delta", "r2_sha256": R2_SHA,
                 "r2_closure_sha256": R2_CLOSURE_SHA, "delta_tests_sha256": DELTA_SHA, "guard_program_sha256": GUARD_SHA}
    updated = {**gates, "live_canary_sha256": sha(canonical(live)), "rehearsal_sha256": sha(canonical(rehearsal))}
    receipt = {"v": 1, "run": RUN, "owner": OWNER, "accepted": True, "accepted_at": now,
        "target": target, "cohort": expected_cohort, "budget": budget, "lifetime_and_max_history_preserved": True,
        "live_evidence": live, "rehearsal_evidence": rehearsal, "canary_program_sha256": CANARY_SHA,
        "guard": snapshot["guard"], "guard_program_sha256": GUARD_SHA,
        "source_files_sha256": {name: sha(snapshot[name]) for name in FROZEN},
        "gates_before_sha256": sha(snapshot["deployment-gates.json"]), "gates_after_sha256": sha(canonical(updated)),
        "no_runner_code_or_schema_change": True}
    return receipt, canonical(updated)


def accept_generation(host, target):
    """Durable single attempt; an existing directory or start intent is inspect-only."""
    with host.execution_lock():
        need(not host.exists(), "generation-acceptance-already-attempted-inspect-only")
        snapshot = host.snapshot()
        receipt, gates_raw = acceptance_evidence(snapshot, target, host.engine, host.canary, host.now())
        host.create()
        for name, raw in snapshot.items():
            if name != "guard":
                host.write(name, raw)
        host.write("target-fresh-proof.json", canonical(target))
        host.write("generation-acceptance.json", canonical(receipt))
        host.recheck(snapshot, snapshot["deployment-gates.json"])
        host.canary.validate_target_proof(target)
        need(-5 <= host.now() - target["checked_at"] <= 20, "target-proof-expired-before-gates-CAS")
        host.write("gates-update.intent.json", canonical({"before_sha256": receipt["gates_before_sha256"],
                                                        "after_sha256": receipt["gates_after_sha256"]}))
        host.cas_gates(snapshot["deployment-gates.json"], gates_raw)
        host.write("gates-update.complete.json", canonical({"gates_sha256": sha(gates_raw)}))
        host.recheck(snapshot, gates_raw)
        need(-5 <= host.now() - target["checked_at"] <= 20, "target-proof-expired-before-start-intent")
        host.write("main-start.intent.json", canonical({"v": 1, "run": RUN, "owner": OWNER, "at": host.now(),
            "acceptance_sha256": sha(canonical(receipt)), "unit": host.main_unit, "no_repeat": True}))
    # The existing runner must be able to acquire execution.lock immediately.
    # Any ambiguous start remains recorded; neither this process nor a rerun
    # may resubmit it. Inspection does not infer that an absent job never ran.
    outcome = "acknowledged"
    try:
        host.start_main()
    except Exception:
        outcome = "unknown-inspect-only"
    result = {"ok": outcome == "acknowledged", "start_outcome": outcome, "start_attempted_once": True,
              "acceptance_sha256": sha(canonical(receipt)), "gates_sha256": sha(gates_raw), "at": host.now()}
    try:
        result["main_observation"] = host.observe_main()
    except Exception:
        result["main_observation"] = {"unavailable": True}
    host.write("main-start.observation.json", canonical(result))
    return result


class SourceHost:
    def __init__(self, payload, canary):
        need(os.geteuid() == 0 and sys.platform == "linux", "source-linux-root-required")
        self.canary, self.delta_raw = canary, base64.b64decode(payload["delta"], validate=True)
        delta = validate_delta(self.delta_raw)
        for name in SOURCE_PROGRAMS:
            need(sha(root_read(LIB / name, 0o644)) == delta["programs"][name], "tested-source-program-changed")
        self.service = load_bytes("acceptance_source_service", LIB / "runner_service.py", root_read(LIB / "runner_service.py", 0o644))
        self.guard_module = load_bytes("acceptance_source_guard", LIB / "runner_guard.py", root_read(LIB / "runner_guard.py", 0o644))
        self.backend, self.guard = self.service.B.RunnerBackend(), self.guard_module.LocalBackend()
        self.engine, self.main_unit = self.service.E, self.guard_module.MAIN_UNIT
        need(self.backend.config["owner"] == OWNER, "source-owner-changed")

    now = staticmethod(time.time)

    def execution_lock(self):
        return self.service.execution_lock()

    def exists(self):
        return SAVED.exists() or SAVED.is_symlink()

    def check_boundary(self):
        need(self.guard.main_status()["quiescent"] and not self.service.selftest_armed(OWNER), "main-or-selftest-not-quiescent")
        for name in ("guard-trip.json", "guard-fence.json"):
            path = SCOPE / name
            need(not path.exists() and not path.is_symlink(), "source-trip-or-fence-present")
        sample = self.backend.source_ops._guard()
        validate_guard(sample, self.now())
        need(sample["boot_id"] == self.guard.boot_id(), "source-guard-boot-changed")
        self.service.verify_entry()
        self.backend.source_ops._assert_monitors_retired()
        operations = self.backend.source_ops._ledger()["operations"].values()
        need(not any(row["request"]["action"] in ("pg_stop", "redis_failover", "redis_role_persist") for row in operations),
             "source-irreversible-operation-already-intended")
        for kind, pid in (("app", 0), ("postgres", 972839), ("redis", 972827)):
            row = self.backend.source_ops._resource(kind)
            need(row["State"]["Pid"] == pid and row["State"]["Running"] == (kind != "app") and
                 row["State"]["OOMKilled"] is False, "source-resource-changed")
            if kind == "app":
                need(row["State"]["Status"] == "exited" and row["State"]["ExitCode"] == 0 and
                     row["HostConfig"]["RestartPolicy"]["Name"] == "no", "source-app-not-clean-retired")
        return sample

    def snapshot(self):
        guard = self.check_boundary()
        self.service.read_gates(OWNER)
        frozen = {name: root_read(SCOPE / name) for name in FROZEN}
        state = strict(frozen["state.json"])
        need(strict(frozen["transport-state.json"])["target_epoch"] == 14, "source-transport-epoch-changed")
        identifier = state.get("budget_cohort", {}).get("id")
        need(isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier), "source-cohort-id-invalid")
        cohort = SCOPE / "recovery-cohort-20261017"
        return {**frozen, "guard": guard, "deployment-gates.json": root_read(SCOPE / "deployment-gates.json"),
            "cohort-record.json": root_read(SCOPE / "budget-cohorts" / (identifier + ".json")),
            "cohort-verification.json": root_read(cohort / "verification.json"),
            "cohort-drain-proof.json": root_read(cohort / "drain-proof.json"),
            "historical-r2-result.json": root_read(R2_DIR / "controller-selftest-result-r2.json"),
            "historical-r2-closure.json": root_read(R2_DIR / "closure-verification.json"),
            "delta-tests.json": self.delta_raw, "canary.json": root_read(self.canary.SCOPE / self.canary.JOURNAL)}

    def create(self):
        root_directory(SCOPE, private=True)
        SAVED.mkdir(mode=0o700)
        sync(SCOPE)

    def write(self, name, raw):
        write_new(SAVED / name, raw)

    def recheck(self, snapshot, gates_raw):
        self.check_boundary()
        need(all(root_read(SCOPE / name) == snapshot[name] for name in FROZEN), "source-frozen-file-CAS-changed")
        need(root_read(SCOPE / "deployment-gates.json") == gates_raw, "deployment-gates-CAS-changed")

    def cas_gates(self, before, after):
        with self.backend.store.lock():
            need(root_read(SCOPE / "deployment-gates.json") == before, "deployment-gates-CAS-changed")
            pending = SAVED / "deployment-gates.pending.json"
            write_new(pending, after)
            os.replace(pending, SCOPE / "deployment-gates.json")
            sync(SCOPE)
        self.service.read_gates(OWNER)

    def start_main(self):
        result = subprocess.run(["/usr/bin/systemctl", "start", "--no-block", self.main_unit],
                                input=b"", capture_output=True, timeout=3)
        need(result.returncode == 0, "main-start-result-unknown-inspect-only")

    def observe_main(self):
        return self.guard.main_status()


def inspect_source():
    root_directory(SCOPE, private=True)
    present = SAVED.exists() or SAVED.is_symlink()
    result = {"inspect_only": True, "start_repeated": False, "acceptance_directory_present": present,
              "current_state": strict(root_read(SCOPE / "state.json")),
              "current_gates_sha256": sha(root_read(SCOPE / "deployment-gates.json"))}
    if present:
        root_directory(SAVED, private=True)
        for name in ("generation-acceptance.json", "gates-update.intent.json", "gates-update.complete.json",
                     "main-start.intent.json", "main-start.observation.json"):
            path = SAVED / name
            if path.exists() or path.is_symlink():
                raw = root_read(path)
                result[name] = {"sha256": sha(raw), "value": strict(raw)}
    try:
        raw = root_read(LIB / "runner_guard.py", 0o644)
        need(sha(raw) == GUARD_SHA, "reviewed-guard-program-changed")
        guard = load_bytes("acceptance_inspect_guard", LIB / "runner_guard.py", raw)
        result["current_main"] = guard.LocalBackend().main_status()
    except Exception:
        result["current_main"] = {"unavailable": True}
    return result


def remote_source(action, payload):
    need(action in ("target", "source", "inspect"), "invalid-remote-action")
    raw = pathlib.Path(__file__).read_bytes()
    return ("import base64,json,sys\nsys.dont_write_bytecode=True\n" +
        "namespace={'__name__':'acceptance_remote','__file__':'reviewed-acceptance.py'}\n" +
        "exec(compile(base64.b64decode(" + repr(base64.b64encode(raw).decode()) +
        "),'reviewed-acceptance.py','exec'),namespace)\n" +
        "payload=json.loads(base64.b64decode(" + repr(base64.b64encode(canonical(payload)).decode()) + "))\n" +
        ("result=namespace['inspect_source']()\n" if action == "inspect" else
         "C=namespace['load_canary'](base64.b64decode(payload['canary_program']))\n" +
         ("result=namespace['target_check'](C)\n" if action == "target" else
          "result=namespace['accept_generation'](namespace['SourceHost'](payload,C),payload['target'])\n")) +
        "print(json.dumps(result,sort_keys=True))\n")


def remote(ssh, action, payload):
    code = remote_source(action, payload)
    compile(code, "acceptance-remote", "exec")
    try:
        result = subprocess.run(ssh + ["sudo -n python3 -I -c " + shlex.quote(code)],
                                capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("acceptance-SSH-outcome-unknown-inspect-only") from None
    need(result.returncode == 0, "acceptance-remote-refused-inspect-private-state")
    return strict(result.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--inspect", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply and not args.inspect:
        print(json.dumps({"offline": True, "production_actions": False, "history_preserved": True,
                          "target_mutations": False, "main_start_after_execution_lock_release": True}))
        return 0
    folder = pathlib.Path(__file__).resolve().parent
    channel = load_bytes("acceptance_channel", folder / "prepare_channel.py", (folder / "prepare_channel.py").read_bytes())
    if args.inspect:
        print(json.dumps(remote(channel.SSH_SOURCE, "inspect", {}), sort_keys=True))
        return 0
    raw = (folder / "recovery_generation_canary.py").read_bytes()
    load_canary(raw)
    delta = (FOLDER / DELTA_FILE).read_bytes()
    validate_delta(delta, folder)
    payload = {"canary_program": base64.b64encode(raw).decode(), "delta": base64.b64encode(delta).decode()}
    payload["target"] = remote(channel.TARGET, "target", payload)
    marker = FOLDER / "generation-acceptance-20261017.intent.json"
    need(not marker.exists() and not marker.is_symlink(), "local-acceptance-already-attempted-inspect-only")
    write_new(marker, canonical({"at": time.time(), "target": payload["target"], "program_sha256": sha(pathlib.Path(__file__).read_bytes()),
                                 "source_worker_sha256": sha(remote_source("source", payload).encode()), "no_repeat": True}))
    try:
        result = remote(channel.SSH_SOURCE, "source", payload)
    except Exception:
        write_new(marker.with_name("generation-acceptance-20261017.unknown.json"), canonical({"outcome": "unknown-inspect-only"}))
        raise
    write_new(marker.with_name("generation-acceptance-20261017.result.json"), canonical(result))
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"ok": False, "error": "generation-acceptance-refused-or-unknown-inspect-only"}), file=sys.stderr)
        raise SystemExit(1)
