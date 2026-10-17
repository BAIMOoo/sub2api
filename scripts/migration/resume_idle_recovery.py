#!/usr/bin/env python3
"""Resume the exact recovered attempt 4 once, after a stable drained cohort.

Default is offline. An ambiguous write/start is inspect-only, never retried.
The same source process waits for idle, appends the cohort, starts the existing
controller once and observes it without another model request in between.
"""
import argparse
import base64
import hashlib
import json
import pathlib
import secrets
import shlex
import subprocess
import sys
import time

RUN, OWNER = "20261016", "30bda011defad8149621c2630a2bd89c"
ROOT = pathlib.Path("/var/lib/sub2api-migration-20261016")
LIB = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016")
SCOPE = ROOT / "runner"
SAVED = SCOPE / "resume-idle-attempt4-20261017"
INSTANCE = "r32ufr8h00vvbf"
LIFETIME = 18.332221914082766
TARGET_CONFIG_SHA = "ca7d126ee925221fce6bf88d42eccd11641116330a1cedfb1cb7323c50c3b032"
TARGET_LEDGER_SHA = "c631d3ff7aa10cb0fdf306566d77d3fb84dea6b9bb585922e599bf55721d95fb"
FAILED_OP = "10b4ebf48d69833af52b7d9e6c17b4ed"
SOURCE_FILES = {
    "state.json": "f3f1df620d449b751e544ebb34111b1b807f8d4cba171b65214b0644fcbf6be8",
    "transport-state.json": "4bb9a93b3b64f8d91b67165f10ec36d7e2c7b61321de25ae1fb2014ed79835bd",
    "source-operations.json": "7e627ac9d821d566a05d6d03123c7e8c659b1e09e395e86a178b3173b216442d",
    "deployment-gates.json": "d3373916a00217ff47894e61dd118e3eacf920ff3140b4c3914eb4d8885f7a73",
    "runner-config.json": "e05c4037c6fe8dbc4c1786861f659bdb6e5b4beb2a99cc0091d923f3596f3549",
    "guard-config.json": "84e16b40d83b7f58ab9233b6eb12cb33b06b655ae165bde159711138a7941973",
    "source-manifest.json": "1dd72b9b0b03870ad59c12bdb71cda1571d8c3c2708b0dd258e152c3c8764844",
}
EVIDENCE = {
    "accepted-canary.json": (ROOT / "recovery-generation-canary-v2-20261017/target.json",
                             "a06ad5ad1c53f99a62414cc87bd91150ea6c3a164369f3ac855dfc6d243d5dd5"),
    "prior-acceptance.json": (SCOPE / "generation-acceptance-20261017/generation-acceptance.json",
                              "ec6b73694be3e528a0600d7b40783fc83ab1cefcee0af818deed5f2e650d936c"),
    "prior-start-observation.json": (SCOPE / "generation-acceptance-20261017/main-start.observation.json",
                                     "7ef82ee055ccf9bd95283e502ac1e0e9c2dae84edcc47c8f98b8143a7d6c0259"),
}
PROGRAMS = {
    "runner_service.py": "7318557010904f289e05b8974f0ff6e23cffdcb3f372f82763824c9fc0eaa5a1",
    "runner_guard.py": "72cb81b27bcf8c4f316511ca0460f44af453ab51276d53cda82ba55e2cee8d05",
    "durable_runner.py": "18f0184ce061b4de57146a1f1024ab576622adc521dc16f0a9f0080fb76229d4",
    "runner_backend.py": "8d1f7e6064e51c2392e977603e47ba2684db5c36096bc3e944041b1d76218bbd",
    "source_operations.py": "28ff80ddfa89e71e4f91d217ddeb4d1ca90aee3028fe6523af0dd1ff2f15fbb6",
    "target_rpc.py": "8180879f75169f130b726536ee16957832a9d2bf9e193f93ad380ebd8264f6ec",
    "recovery_generation_canary.py": "2acb804b7353fd1f8a38d113744e5732a9498ffcc4444c9bc4f477171bfeba36",
    "accept_recovery_generation.py": "7efd8fe8b6e2a40e2449c681cda7925d8cb8b4ad988a5ccd4f91214c3fad8926",
    "rolling_app_host.py": "a8194c260f88dd695fd02cd1b6f6f6dbe066ec9e7cbeee83cb72f12130e656ce",
    "rolling_app_recovery.py": "14b21455e754109b71e649e69a4adf3fd2e251e54f490ebf187a20c0442bc39f",
}


def need(value, code):
    if not value:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load_support(raw):
    need(sha(raw) == PROGRAMS["accept_recovery_generation.py"], "reviewed-support-changed")
    namespace = {"__name__": "resume_reviewed_support", "__file__": str(LIB / "accept_recovery_generation.py")}
    exec(compile(raw, namespace["__file__"], "exec"), namespace)
    return type("Support", (), namespace)


def target_check(support, raw):
    """Reuse the complete reviewed adopted-generation check at fixed epoch 17."""
    need(sha(raw) == PROGRAMS["recovery_generation_canary.py"], "reviewed-target-proof-changed")
    text = raw.decode()
    for before, after in [('ledger.get("epoch") == 14', 'ledger.get("epoch") == 17'),
                          ('"ledger_epoch": 14, "public_route_proven"', '"ledger_epoch": 17, "public_route_proven"')]:
        need(text.count(before) == 1, "reviewed-epoch-anchor-changed")
        text = text.replace(before, after)
    canary = support.load_bytes("resume_target_preflight", LIB / "recovery_generation_canary.py", text.encode())
    ledger_raw = support.root_read(ROOT / "rpc/state.json")
    config_raw = support.root_read(ROOT / "rpc/config.json")
    need(sha(ledger_raw) == TARGET_LEDGER_SHA and sha(config_raw) == TARGET_CONFIG_SHA, "target-exact-pins-changed")
    ledger = support.strict(ledger_raw)
    need(ledger.get("epoch") == 17 and all(op.get("phase") == "COMPLETE" for op in ledger["operations"].values()) and
         all(op.get("cancelled") is True for op in ledger["operations"].values() if op["request"]["action"] == "app.quiesce"),
         "target-operations-not-fully-recovered")
    failed = ledger["operations"][FAILED_OP]
    need(not failed.get("instance_id") and not failed.get("substeps"), "attempt4-quiesce-progressed")
    restore = max((op for op in ledger["operations"].values() if op["request"]["action"] == "app.restore"),
                  key=lambda op: op["accepted_at"])
    restored = restore["response"]
    need(restored["code"] == "OK" and restored["epoch"] == 17 and
         restored["result"]["app"]["body"]["instance_id"] == INSTANCE and
         type(restored["result"]["ingress"]["epoch"]) is int and restored["result"]["ingress"]["epoch"] == 2,
         "latest-restore-identity-or-ingress-epoch-changed")
    proof = canary.target_preflight()
    need(proof["instance_id"] == INSTANCE and proof["ledger_epoch"] == 17 and
         proof["target_ledger_sha256"] == TARGET_LEDGER_SHA and proof["controller_config_sha256"] == TARGET_CONFIG_SHA and
         support.root_read(ROOT / "rpc/state.json") == ledger_raw and support.root_read(ROOT / "rpc/config.json") == config_raw,
         "target-identity-or-raw-CAS-changed")
    return {**proof, "restored_ingress_epoch": 2, "restore_op_id": restore["request"]["op_id"]}


def drained(before, runtime, after, required, ingress_epoch=2):
    """Idle moderation workers are readers; detached accepted work must finish."""
    need(runtime.get("instance_id") == INSTANCE and runtime.get("ready") is True and runtime.get("serving") is True and
         runtime.get("draining") is False and runtime.get("background_workers_enabled") is False,
         "current-generation-not-ready")
    tasks = runtime.get("tasks")
    need(type(tasks) is dict and set(required) <= tasks.keys(), "writer-coverage-missing")
    for name, row in tasks.items():
        need(type(row) is dict and all(type(row.get(k)) is int and row[k] >= 0 for k in ("active", "pending", "failed", "dropped")),
             "writer-counter-invalid")
        need(row["failed"] == row["dropped"] == 0, "writer-failure-or-drop")
    return (type(runtime.get("active_requests")) is int and runtime["active_requests"] == 0 and
            type(runtime.get("active_hijacked_connections")) is int and runtime["active_hijacked_connections"] == 0 and
            before == runtime.get("ingress") == after and before.get("paused") is False and
            type(before.get("epoch")) is int and before["epoch"] == ingress_epoch and
            type(before.get("queued_requests")) is int and before["queued_requests"] == 0 and
            all(row["pending"] == 0 and (row["active"] == 0 or name == "content_moderation") for name, row in tasks.items()))


def wait_drained(host, deadline, stable_seconds=2):
    stable = None
    while True:
        need(host.monotonic() < deadline, "stable-drain-timeout-no-start")
        before = host.observe("ingress.status")
        runtime = host.observe("runtime.status")
        after = host.observe("ingress.status")
        now = host.monotonic()
        if drained(before, runtime, after, host.required, host.target["restored_ingress_epoch"]):
            stable = now if stable is None else stable
            if now - stable >= stable_seconds:
                return {"v": 1, "run": RUN, "owner": OWNER, "checked_at": host.now(), "stable_seconds": now - stable,
                        "instance_id": INSTANCE, "runtime": runtime, "ingress_before": before, "ingress_after": after,
                        "target": host.target, "same_generation_cohort": True, "accepted_detached_work_finished": True}
        else:
            stable = None
        host.sleep(.15)


def source_state(support, raw, engine):
    state = support.strict(raw)
    need(state.get("v") == 1 and state.get("run") == RUN and state.get("owner") == OWNER and
         state.get("attempt") == 4 and state.get("phase") == "SOURCE_DATA" and state.get("route_fenced") is True and
         state.get("pg") == state.get("redis") == "AWS" and state.get("window") is None and
         state.get("pause_seconds") == LIFETIME and state.get("deadline_breached") is False and
         state.get("budget_accounting_unknown", False) is False and
         state.get("budget_cohort", {}).get("id") == "9f3d68ecaf85192bd9e9456c387f3b1e", "exact-attempt4-boundary-required")
    need(engine.budget_accounting(state)["max_cohort_pause_seconds"] <= 10, "prior-budget-breached")
    return state


def accepted_evidence(support, frozen):
    need(all(sha(frozen[name]) == pin for name, (_, pin) in EVIDENCE.items()), "prior-evidence-raw-pins-changed")
    canary, receipt, observation = (support.strict(frozen[name]) for name in EVIDENCE)
    need(canary.get("phase") == "COMPLETE" and canary.get("ok") is True and
         all(canary[key]["instance_id"] == INSTANCE for key in ("target_preflight", "target_postflight")) and
         receipt.get("run") == RUN and receipt.get("owner") == OWNER and receipt.get("accepted") is True and
         receipt["target"]["instance_id"] == INSTANCE and
         receipt["live_evidence"]["canary_sha256"] == sha(frozen["accepted-canary.json"]) and
         receipt["gates_after_sha256"] == sha(frozen["deployment-gates.json"]) and
         observation.get("start_attempted_once") is True and observation.get("start_outcome") == "acknowledged",
         "accepted-generation-canary-or-gates-unbound")


def resume(host):
    started = host.monotonic()
    deadline = started + 50
    with host.execution_lock():
        need(not host.exists(), "resume-already-attempted-inspect-only")
        frozen = host.snapshot()
        state = source_state(host.support, frozen["state.json"], host.engine)
        proof = wait_drained(host, min(deadline - 12, started + 35))
        host.recheck(frozen)
        need(0 <= host.now() - host.target["checked_at"] <= 50, "target-proof-expired")
        host.create()
        for name, raw in frozen.items():
            host.write(name, raw)
        proofraw = host.support.canonical(proof)
        host.write("drain-proof.json", proofraw)
        identifier = host.cohort_id
        record = {"v": 1, "run": RUN, "owner": OWNER, "id": identifier, "previous_state": state,
                  "previous_state_sha256": host.engine.digest(state), "proof_sha256": sha(proofraw)}
        recordraw = host.support.canonical(record)
        host.write_record(identifier, recordraw)
        updated = {**state, "budget_cohort": {"id": identifier, "record_sha256": sha(recordraw)}}
        before, after = host.engine.budget_accounting(state), host.engine.budget_accounting(updated)
        need(after["total_pause_seconds"] == before["total_pause_seconds"] and
             after["max_cohort_pause_seconds"] == before["max_cohort_pause_seconds"] and
             after["cohort_pause_seconds"] == 0, "budget-history-not-preserved")
        with host.store_lock():
            host.recheck(frozen)
            host.save_state(updated)
        expected = {**frozen, "state.json": host.read_state_raw()}
        need(host.support.strict(expected["state.json"]) == updated, "state-persistence-uncertain-inspect-only")
        # Stay in this uninterrupted process. Any newly accepted work restarts
        # the stable interval; no fresh model request is needed to authorize it.
        final = wait_drained(host, min(deadline - 8, host.monotonic() + 8))
        host.recheck(expected)
        need(0 <= host.now() - host.target["checked_at"] <= 50, "target-proof-expired-before-start")
        host.write("final-drain-proof.json", host.support.canonical(final))
        baseline = host.observe_main()
        host.write("main-start.intent.json", host.support.canonical({"v": 1, "run": RUN, "owner": OWNER,
            "at": host.now(), "unit": host.main_unit, "cohort_id": identifier, "budget": after,
            "state_sha256": sha(expected["state.json"]), "baseline": baseline, "no_repeat": True}))
    outcome = "acknowledged"
    try:
        host.start_main()
    except Exception:
        outcome = "unknown-inspect-only"
    # Draining may consume 35 seconds. Give the launched controller its own
    # uninterrupted observation budget; terminal proof returns immediately.
    deadline = host.monotonic() + 45
    observed_new = False
    terminal_candidate = None
    result = {"ok": False, "start_attempted_once": True, "start_outcome": outcome, "cohort_id": identifier}
    while host.monotonic() < deadline:
        current = host.support.strict(host.read_state_raw())
        main = host.observe_main()
        observed_new = observed_new or (bool(main.get("InvocationID")) and main["InvocationID"] != baseline.get("InvocationID")) or current.get("attempt", 0) > 4
        budget = host.engine.budget_accounting(current)
        result.update(current_state=current, main=main, budget=budget)
        candidate = (main.get("InvocationID"), main.get("Result"), main.get("ExecMainStatus"), host.engine.digest(current))
        terminal = observed_new and main["quiescent"]
        if terminal and terminal_candidate == candidate:
            result["ok"] = (current.get("phase") == "COMPLETE" and current.get("pg") == current.get("redis") == "TENCENT" and
                            current.get("window") is None and current.get("deadline_breached") is False and
                            current.get("budget_accounting_unknown", False) is False and budget["max_cohort_pause_seconds"] <= 10 and
                            main.get("Result") == "success" and main.get("ExecMainStatus") == "0")
            result["observation"] = "complete" if result["ok"] else "stopped-inspect-only"
            break
        terminal_candidate = candidate if terminal else None
        host.sleep(.25)
    else:
        result["observation"] = "bounded-observation-ended-inspect-only"
    host.write("main-start.observation.json", host.support.canonical(result))
    return result


class SourceHost:
    now, monotonic, sleep = staticmethod(time.time), staticmethod(time.monotonic), staticmethod(time.sleep)

    def __init__(self, support, target, cohort_id):
        need(support.os.geteuid() == 0 and sys.platform == "linux", "source-linux-root-required")
        self.support, self.target, self.cohort_id = support, target, cohort_id
        for name in ("runner_service.py", "runner_guard.py", "durable_runner.py", "runner_backend.py", "source_operations.py", "target_rpc.py"):
            need(sha(support.root_read(LIB / name, 0o644)) == PROGRAMS[name], "source-program-pin-changed")
        self.service = support.load_bytes("resume_source_service", LIB / "runner_service.py", support.root_read(LIB / "runner_service.py", 0o644))
        self.guard_module = support.load_bytes("resume_source_guard", LIB / "runner_guard.py", support.root_read(LIB / "runner_guard.py", 0o644))
        self.backend, self.guard = self.service.B.RunnerBackend(), self.guard_module.LocalBackend()
        self.engine, self.main_unit = self.service.E, self.guard_module.MAIN_UNIT
        self.required = self.service.B.rpc_module.REQUIRED_WRITERS
        need(self.backend.config["owner"] == OWNER, "source-owner-changed")

    def execution_lock(self): return self.service.execution_lock()
    def exists(self): return SAVED.exists() or SAVED.is_symlink()
    def check_boundary(self): return self.support.SourceHost.check_boundary(self)
    def observe(self, name): return self.backend.observe_control(name)
    def store_lock(self): return self.backend.store.lock()
    def save_state(self, state): self.backend.store._write("state", state)
    def read_state_raw(self): return self.support.root_read(SCOPE / "state.json")

    def snapshot(self):
        self.check_boundary()
        self.service.read_gates(OWNER)
        raw = {name: self.support.root_read(SCOPE / name) for name in SOURCE_FILES}
        need(all(sha(raw[name]) == expected for name, expected in SOURCE_FILES.items()), "source-exact-pins-changed")
        need(self.support.strict(raw["transport-state.json"])["target_epoch"] == 17, "source-transport-epoch-changed")
        raw.update({name: self.support.root_read(path) for name, (path, _) in EVIDENCE.items()})
        accepted_evidence(self.support, raw)
        return raw

    def recheck(self, frozen):
        self.check_boundary()
        self.service.read_gates(OWNER)
        need(all(self.support.root_read(EVIDENCE[name][0] if name in EVIDENCE else SCOPE / name) == raw
                 for name, raw in frozen.items()), "source-CAS-changed")

    def create(self):
        self.support.root_directory(SCOPE, private=True)
        SAVED.mkdir(mode=0o700)
        self.support.sync(SCOPE)

    def write(self, name, raw): self.support.write_new(SAVED / name, raw)

    def write_record(self, identifier, raw):
        need(self.support.re.fullmatch(r"[0-9a-f]{32}", identifier), "invalid-cohort-id")
        directory = SCOPE / "budget-cohorts"
        self.service.S.trusted_parent(directory, private=True)
        self.support.write_new(directory / (identifier + ".json"), raw)

    def start_main(self):
        result = subprocess.run(["/usr/bin/systemctl", "start", "--no-block", self.main_unit], input=b"", capture_output=True, timeout=3)
        need(result.returncode == 0, "start-outcome-unknown-inspect-only")

    def observe_main(self):
        result = self.guard.main_status()
        raw = self.guard.command(["/usr/bin/systemctl", "show", self.main_unit,
                                  "--property=InvocationID,Result,ExecMainStatus"], timeout=.2)
        fields = dict(row.split("=", 1) for row in raw.decode().splitlines())
        need(set(fields) == {"InvocationID", "Result", "ExecMainStatus"}, "main-result-fields-changed")
        return {**result, **fields}


def inspect_source(support):
    result = {"inspect_only": True, "start_repeated": False, "current_state": support.strict(support.root_read(SCOPE / "state.json"))}
    if SAVED.exists() or SAVED.is_symlink():
        support.root_directory(SAVED, private=True)
        for name in ("drain-proof.json", "final-drain-proof.json", "main-start.intent.json", "main-start.observation.json"):
            path = SAVED / name
            if path.exists() or path.is_symlink():
                result[name] = support.strict(support.root_read(path))
    return result


def remote_code(action, payload, program):
    need(action in ("target", "source", "inspect"), "remote-action-invalid")
    code = ("import base64,json,sys\nsys.dont_write_bytecode=True\n" +
        "n={'__name__':'resume_remote','__file__':'reviewed-resume.py'}\n" +
        "exec(compile(base64.b64decode(" + repr(base64.b64encode(program).decode()) + "),'reviewed-resume.py','exec'),n)\n" +
        "p=json.loads(base64.b64decode(" + repr(base64.b64encode(json.dumps(payload).encode()).decode()) + "))\n" +
        "s=n['load_support'](base64.b64decode(p['support']))\n")
    if action == "target":
        code += "result=n['target_check'](s,base64.b64decode(p['canary']))\n"
    elif action == "source":
        code += "result=n['resume'](n['SourceHost'](s,p['target'],p['cohort_id']))\n"
    else:
        code += "result=n['inspect_source'](s)\n"
    return code + "print(json.dumps(result,sort_keys=True))\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--inspect", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply and not args.inspect:
        print(json.dumps({"offline": True, "one_start_only": True, "observe_in_same_process": True}))
        return 0
    folder = pathlib.Path(__file__).resolve().parent
    raw = (folder / "accept_recovery_generation.py").read_bytes()
    support = load_support(raw)
    channel = support.load_bytes("resume_channel", folder / "prepare_channel.py", (folder / "prepare_channel.py").read_bytes())
    program = pathlib.Path(__file__).read_bytes()
    payload = {"support": base64.b64encode(raw).decode()}
    def remote(ssh, action):
        code = remote_code(action, payload, program)
        compile(code, "resume-remote", "exec")
        receipt = support.FOLDER / "resume-idle-attempt4-20261017.source-command.json"
        try:
            result = subprocess.run(ssh + ["sudo -n python3 -I -c " + shlex.quote(code)], capture_output=True,
                                    timeout=105 if action == "source" else 58)
        except (OSError, subprocess.TimeoutExpired) as error:
            if action == "source":
                support.write_new(receipt, support.canonical({"at": time.time(), "outcome": "unknown-inspect-only",
                    "error_type": type(error).__name__, "no_repeat": True,
                    "stdout_b64": base64.b64encode(getattr(error, "stdout", None) or b"").decode(),
                    "stderr_b64": base64.b64encode(getattr(error, "stderr", None) or b"").decode()}))
            raise RuntimeError("resume-SSH-outcome-unknown-inspect-only") from None
        if action == "source":
            support.write_new(receipt, support.canonical({"at": time.time(), "returncode": result.returncode, "no_repeat": True,
                "stdout_b64": base64.b64encode(result.stdout).decode(), "stderr_b64": base64.b64encode(result.stderr).decode()}))
        need(result.returncode == 0, "resume-remote-refused-or-unknown-inspect-only")
        return support.strict(result.stdout)
    if args.inspect:
        print(json.dumps(remote(channel.SSH_SOURCE, "inspect"), sort_keys=True))
        return 0
    for name, expected in PROGRAMS.items():
        need(sha((folder / name).read_bytes()) == expected, "reviewed-local-program-changed")
    marker = support.FOLDER / "resume-idle-attempt4-20261017.intent.json"
    need(not marker.exists() and not marker.is_symlink(), "local-resume-already-attempted-inspect-only")
    payload["canary"] = base64.b64encode((folder / "recovery_generation_canary.py").read_bytes()).decode()
    payload["target"] = remote(channel.TARGET, "target")
    # Source only needs the returned proof. Avoid carrying the target-only
    # program in its SSH command and crossing Linux's per-argument size cap.
    del payload["canary"]
    payload["cohort_id"] = secrets.token_hex(16)
    support.write_new(marker, support.canonical({"at": time.time(), "program_sha256": sha(program), "no_repeat": True,
        "target": payload["target"], "cohort_id": payload["cohort_id"]}))
    result = remote(channel.SSH_SOURCE, "source")
    support.write_new(marker.with_name("resume-idle-attempt4-20261017.result.json"), support.canonical(result))
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"ok": False, "error": "resume-refused-or-unknown-inspect-only"}), file=sys.stderr)
        raise SystemExit(1)
