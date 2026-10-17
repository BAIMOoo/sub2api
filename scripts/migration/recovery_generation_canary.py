#!/usr/bin/env python3
"""One recovery-generation Tencent TLS/SSE canary billed by the AWS primary.

Default is offline. --apply is explicitly billable and never repeats an
existing journal. A local coordinator reads Tencent identity/role evidence;
the AWS root worker reuses the unmodified model_canary SQL/TLS/SSE/accounting
implementation in a new journal scope. A second Tencent proof is required
before final acceptance. --inspect reads the AWS journal without a request.
No endpoint, container, controller history, Caddy or DNS is changed.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import time
import types
import uuid

RUN, OWNER = "20261016", "30bda011defad8149621c2630a2bd89c"
BASE_SHA256 = "a8458fb80ca426e9ad203584578be6df3d1d16a36c1bd3b7a71e5bdd99d1e20f"
RPC_SHA256 = "8180879f75169f130b726536ee16957832a9d2bf9e193f93ad380ebd8264f6ec"
ROOT = pathlib.Path("/var/lib/sub2api-migration-20261016")
LIB = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016")
SCOPE = ROOT / "recovery-generation-canary-v2-20261017"
JOURNAL = "target.json"
APP_ID = "17e3bfacbd442beff52427e003e58f4970e613203356b8bb8bde0e98b9229bce"
OLD_APP_ID = "cd5c29cf86b2595fffc41fc068e024d3f790f69bb8bdaaacc63eaba1ac9bcd82"
APP_IMAGE = "sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a"
PG_ID = "ac9f494b2fd0ec87257a9a7308a6ad7b270051d1dc4da33b33fea9af2885418c"
PG_IMAGE = "sha256:9a5296338b4957a7082ad925c9f31346f6f1cb6b7cbe7bd2971430432488a395"
SYSTEM_ID = "7626048206782038050"
TARGET_IP, DOMAIN = "43.165.177.136", "api.b4im.com"
ENDPOINTS = {"v": 1, "postgres": "source", "redis": "source"}
PROOF_MAX_AGE = 30


class Refusal(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def private_root_dir(path, private=False):
    path = pathlib.Path(path)
    for parent in [path, *path.parents]:
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "untrusted-root-directory")
    need(not private or stat.S_IMODE(path.stat().st_mode) == 0o700, "journal-directory-not-private")


def root_read(path, mode=0o600, limit=2 * 1024 ** 2):
    path = pathlib.Path(path)
    private_root_dir(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "untrusted-root-file")
        raw = b""
        while len(raw) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        need(len(raw) <= limit, "root-file-too-large")
        return raw
    finally:
        os.close(fd)


def reused_base(raw):
    need(sha(raw) == BASE_SHA256, "reviewed-model-canary-changed")
    module = types.ModuleType("recovery_reused_model_canary")
    module.__file__ = "reviewed-model-canary.py"
    exec(compile(raw, "reviewed-model-canary", "exec"), module.__dict__)
    need(module.TARGET_IP == TARGET_IP and module.TARGET_DOMAIN == DOMAIN and module.SOURCE_SYSTEM_ID == SYSTEM_ID,
         "reused-canary-pins-changed")
    return module


def load_base():
    return reused_base(pathlib.Path(__file__).with_name("model_canary.py").read_bytes())


def load_target():
    path = LIB / "target_rpc.py"
    need(sha(root_read(path, mode=0o700)) == RPC_SHA256, "reviewed-target-rpc-changed")
    spec = importlib.util.spec_from_file_location("recovery_canary_target_rpc", path)
    rpc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rpc)
    cfg = rpc.load_config()
    need(cfg["owner"] == OWNER and cfg["app"]["container_id"] == APP_ID and cfg["app"]["image_id"] == APP_IMAGE and
         cfg["app"]["control_port"] == 18093 and cfg["pg"]["container_id"] == PG_ID and
         cfg["pg"]["image_id"] == PG_IMAGE and cfg["pg"]["system_identifier"] == SYSTEM_ID, "accepted-resource-pins-changed")
    return rpc, cfg


def runtime_proof(runtime, instance, required):
    need(runtime.get("instance_id") == instance and runtime.get("ready") is True and runtime.get("serving") is True and
         runtime.get("draining") is False and runtime.get("background_workers_enabled") is False,
         "recovery-generation-not-ready")
    tasks = runtime.get("tasks")
    need(type(tasks) is dict and set(required) <= tasks.keys() and
         all(type(row) is dict and all(type(row.get(key)) is int and row[key] == 0 for key in ("failed", "dropped"))
             for row in tasks.values()), "recovery-generation-writer-failure-or-coverage-gap")


def target_preflight():
    need(os.geteuid() == 0 and sys.platform == "linux", "tencent-linux-root-required")
    r, cfg = load_target()
    b, store = r.LocalBackend(cfg), r.FileStore(cfg)
    ledger_raw = root_read(ROOT / "rpc/state.json")
    ledger = store.read()
    arm = ledger["pg"].get("arm")
    need(ledger == r.strict_json(ledger_raw, 2 * 1024 ** 2) and ledger.get("epoch") == 14 and
         ledger["owner"] == OWNER and ledger["pg"]["authority"] == ledger["redis"]["authority"] == "AWS" and
         not ledger["pg"].get("promotion_intent") and (arm is None or type(arm) is dict and arm.get("status") == "CANCELLED") and
         b.endpoint_state() == ENDPOINTS, "aws-authority-or-endpoints-unconfirmed")
    need(not any(op["phase"] != "COMPLETE" and not (op["request"]["action"] == "app.quiesce" and op.get("cancelled") is True)
                 and r.TargetRPC.is_mutation(op["request"]) for op in ledger["operations"].values()), "migration-mutation-pending")
    scope = ROOT / "rolling-recovery"
    receipt_raw, rolling_raw = root_read(scope / "controller-adoption.json"), root_read(scope / "state.json")
    receipt, rolling = r.strict_json(receipt_raw, 65536), r.strict_json(rolling_raw, 2 * 1024 ** 2)
    need(receipt.get("v") == 1 and receipt.get("run") == RUN and receipt.get("owner") == OWNER and
         receipt.get("old_container_id") == OLD_APP_ID and receipt.get("new_container_id") == APP_ID and
         receipt.get("new_app_config_sha256") == cfg["app"]["config_sha256"] and receipt.get("new_control_port") == 18093 and
         receipt.get("controller_config_raw_sha256") == sha(root_read(ROOT / "rpc/config.json")) and
         receipt.get("rolling_complete_sha256") == sha(rolling_raw) and
         receipt.get("new_control_token_sha256") == sha(b._app_token()), "recovery-adoption-changed")
    need(rolling.get("owner") == OWNER and rolling.get("phase") == "COMPLETE" and rolling.get("intent") is None and
         rolling.get("new_identity", {}).get("container_id") == APP_ID and
         rolling.get("new_identity", {}).get("config_sha256") == cfg["app"]["config_sha256"] and
         rolling.get("adoption_receipt", {}).get("old_exited_cleanly") is True, "rolling-not-complete")
    old, app = b.docker_json("container", OLD_APP_ID), b.resource("app")
    need(old["Id"] == OLD_APP_ID and old["Image"] == APP_IMAGE and old["State"]["Running"] is False and
         old["State"]["Status"] == "exited" and old["State"]["ExitCode"] == old["State"]["Pid"] == 0 and
         old["State"]["OOMKilled"] is False and old["HostConfig"]["RestartPolicy"]["Name"] == "no",
         "old-generation-not-retired")
    instance = receipt.get("new_instance_id")
    need(isinstance(instance, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", instance), "adopted-instance-missing")
    def control(name):
        value = b.app_control(name)
        need(value.get("http_status") == 200 and type(value.get("body")) is dict, "app-control-unavailable")
        return value["body"]
    runtime_proof(control("runtime.status"), instance, r.REQUIRED_WRITERS)
    controls = {name: control(name + ".status") for name in ("postgres", "redis", "writers", "ingress", "background")}
    pg, redis, writers, ingress, background = (controls[key] for key in ("postgres", "redis", "writers", "ingress", "background"))
    need(pg.get("endpoint") == "source" and pg.get("paused") is False and redis.get("address") == "127.0.0.1:16379" and
         redis.get("paused") is False and redis.get("pubsub_cache_ready") is True and writers.get("held") is False and
         ingress.get("paused") is False and background.get("activated") is False and background.get("activating") is False and
         not background.get("started") and not background.get("failures"), "recovery-controls-or-background-changed")
    with b.pg_locks():
        manifest, sample = b.pg_manifest(), b.pg_sample()
        r.TargetRPC(cfg, b, store)._pg_validate(sample, primary=False)
        need(manifest["authority_mode"] == "STANDBY" and not manifest.get("promotion_intent"), "target-pg-not-standby")
    b.resource("redis")
    redis_pair = b.redis_preroute_pair()
    source, target = redis_pair["source"], redis_pair["target"]
    need(source.get("role") == "master" and source.get("run_id") == r.REDIS_SOURCE_RUN_ID and
         target.get("role") == "slave" and target.get("run_id") == r.REDIS_TARGET_RUN_ID and
         target.get("master_host") == "127.0.0.1" and target.get("master_port") == "16379" and
         target.get("master_link_status") == "up" and target.get("master_sync_in_progress") == "0",
         "target-redis-not-following-aws")
    # Reuse the reviewed normalizer to prove the public entry still owns exactly
    # the first scoped rule; no Caddy or route mutation occurs.
    hcfg = r.strict_json(root_read(scope / "host-config.json"), 65536)
    modules = {}
    for name, key in (("rolling_app_host", "host_sha256"), ("rolling_app_recovery", "core_sha256")):
        path = LIB / (name + ".py")
        need(sha(root_read(path, 0o700)) == hcfg[key], "route-check-program-changed")
        spec = importlib.util.spec_from_file_location("canary_" + name, path)
        modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modules[name])
    h, core = modules["rolling_app_host"], modules["rolling_app_recovery"]
    nat = h.parse_nat(b.command(["/usr/sbin/iptables-save", "-t", "nat"]), core.REDIRECT)
    comment = core.REDIRECT[core.REDIRECT.index("--comment") + 1]
    owned = [row for row in nat["output"] + nat["other"]
             if any(row[i] == "--comment" and row[i + 1] == comment for i in range(len(row) - 1))]
    need(rolling["adoption_receipt"].get("redirect") == core.REDIRECT and nat["output"] and
         nat["output"][0] == core.REDIRECT and owned == [core.REDIRECT],
         "public-route-not-adopted-generation")
    runtime_proof(control("runtime.status"), instance, r.REQUIRED_WRITERS)
    need(b.endpoint_state() == ENDPOINTS and root_read(ROOT / "rpc/state.json") == ledger_raw, "persisted-endpoints-or-ledger-changed")
    return {"v": 1, "run": RUN, "owner": OWNER, "app_container_id": APP_ID, "app_image_id": APP_IMAGE,
            "app_config_sha256": cfg["app"]["config_sha256"], "app_pid": app["State"]["Pid"], "instance_id": instance,
            "control_port": 18093, "ready": True, "writer_failures": 0, "writer_dropped": 0,
            "background_workers_enabled": False, "persistent_endpoints": ENDPOINTS, "pg_authority": "AWS", "redis_authority": "AWS",
            "pg_container_id": PG_ID, "pg_image_id": PG_IMAGE, "system_identifier": SYSTEM_ID, "pg_in_recovery": True,
            "redis_run_id": r.REDIS_TARGET_RUN_ID, "redis_role": "slave", "redis_master_link_up": True,
            "adoption_sha256": sha(receipt_raw), "controller_config_sha256": receipt["controller_config_raw_sha256"],
            "target_ledger_sha256": sha(ledger_raw), "ledger_epoch": 14, "public_route_proven": True, "checked_at": time.time()}


def validate_target_proof(proof, fresh=True):
    need(type(proof) is dict and proof.get("v") == 1 and proof.get("run") == RUN and proof.get("owner") == OWNER and
         proof.get("app_container_id") == APP_ID and proof.get("app_image_id") == APP_IMAGE and proof.get("control_port") == 18093 and
         isinstance(proof.get("instance_id"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", proof["instance_id"]) and
         type(proof.get("app_pid")) is int and proof["app_pid"] > 0 and proof.get("ready") is True and
         proof.get("writer_failures") == proof.get("writer_dropped") == 0 and proof.get("background_workers_enabled") is False and
         proof.get("persistent_endpoints") == ENDPOINTS and proof.get("pg_authority") == proof.get("redis_authority") == "AWS" and
         proof.get("pg_container_id") == PG_ID and proof.get("pg_image_id") == PG_IMAGE and proof.get("system_identifier") == SYSTEM_ID and
         proof.get("pg_in_recovery") is True and proof.get("redis_role") == "slave" and proof.get("redis_master_link_up") is True and
         proof.get("public_route_proven") is True and all(isinstance(proof.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", proof[key])
             for key in ("app_config_sha256", "adoption_sha256", "controller_config_sha256", "target_ledger_sha256")) and
         proof.get("ledger_epoch") == 14, "target-proof-invalid")
    need(type(proof.get("checked_at")) in (int, float) and (not fresh or -5 <= time.time() - proof["checked_at"] <= PROOF_MAX_AGE),
         "target-proof-stale")


def same_generation(before, after):
    validate_target_proof(before, fresh=False)
    validate_target_proof(after)
    need({k: v for k, v in before.items() if k != "checked_at"} == {k: v for k, v in after.items() if k != "checked_at"},
         "target-generation-or-role-changed")


def source_sql_adapter(base):
    """Preserve the reviewed AWS-local SQL implementation and re-prove primary."""
    source_sql = base.sql
    def authority():
        value = source_sql("SELECT json_build_object('primary',NOT pg_is_in_recovery(),"
                           "'system_identifier',(SELECT system_identifier::text FROM pg_control_system()))")
        need(value.get("primary") is True and value.get("system_identifier") == SYSTEM_ID, "aws-source-not-original-primary")
        return {"primary": True, "system_identifier": SYSTEM_ID, "postgres_container_id": base.SOURCE_PG_ID}
    def checked(query):
        authority()
        return source_sql(query)
    base.sql = checked
    return authority


def source_cohort(proof):
    """Bind this request to the append-only cohort authorized for this adoption."""
    state = json.loads(root_read(ROOT / "runner/state.json"))
    need(state.get("v") == 1 and state.get("run") == RUN and state.get("owner") == OWNER and state.get("window") is None and
         state.get("pg") == state.get("redis") == "AWS", "source-cohort-authority-changed")
    cohort = state.get("budget_cohort", {})
    identifier = cohort.get("id")
    need(isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier), "source-cohort-missing")
    record_raw = root_read(ROOT / "runner/budget-cohorts" / (identifier + ".json"))
    record = json.loads(record_raw)
    drain_raw = root_read(ROOT / "runner/recovery-cohort-20261017/drain-proof.json")
    drain = json.loads(drain_raw)
    need(sha(record_raw) == cohort.get("record_sha256") and record.get("v") == 1 and record.get("run") == RUN and
         record.get("owner") == OWNER and record.get("id") == identifier and record.get("proof_sha256") == sha(drain_raw) and
         drain.get("owner") == OWNER and drain.get("new_instance_id") == proof["instance_id"] and
         drain.get("new_container_id") == APP_ID and drain.get("adoption_sha256") == proof["adoption_sha256"] and
         drain.get("ledger_sha256") == proof["target_ledger_sha256"] and drain.get("ledger_epoch") == 14,
         "source-cohort-adoption-or-generation-changed")
    return {"id": identifier, "record_sha256": sha(record_raw), "proof_sha256": sha(drain_raw),
            "adoption_sha256": proof["adoption_sha256"], "instance_id": proof["instance_id"]}


def safe_error(error, base):
    if isinstance(error, (Refusal, base.Refusal)) and re.fullmatch(r"[a-z][a-z0-9-]{1,100}", str(error)):
        return str(error)
    return "recovery-canary-check-failed-no-secret-output"


def aws_root():
    need(os.geteuid() == 0 and sys.platform == "linux", "aws-linux-root-required")


def remote_apply(payload, base):
    aws_root()
    need(set(payload) == {"token", "request_id", "client_request_id", "target_preflight"} and
         base.valid_uuid(payload["client_request_id"]) and
         re.fullmatch(r"migration-canary-recovery-generation-[0-9a-f]{32}", payload["request_id"]), "invalid-recovery-request")
    token = payload["token"]
    need(isinstance(token, str) and 16 <= len(token) <= 128 and not any(c in token for c in "\r\n\x00"), "invalid-private-token")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    private_root_dir(SCOPE.parent, private=True)
    if not SCOPE.exists() and not SCOPE.is_symlink():
        SCOPE.mkdir(mode=0o700)
        fd = os.open(SCOPE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    private_root_dir(SCOPE, private=True)
    path = SCOPE / JOURNAL
    report = {"v": 1, "run": RUN, "lane": "recovery-generation", "phase": "RESERVED", "ok": False,
              "request_id": payload["request_id"], "supplied_client_request_id": payload["client_request_id"],
              "model": base.MODEL, "max_output_tokens": 128, "request_attempted": False, "no_automatic_retry": True,
              "production_routes_changed": False, "stream": {}, "target_preflight": payload["target_preflight"],
              "journal_path": str(path), "started_unix_seconds": time.time()}
    try:
        base.private_write(path, report)
    except FileExistsError:
        raise Refusal("recovery-lane-reserved-inspect-evidence-no-repeat") from None
    try:
        validate_target_proof(payload["target_preflight"])
        authority = source_sql_adapter(base)
        report["source_preflight"] = authority()
        report["source_cohort"] = source_cohort(payload["target_preflight"])
        key = base.key_preflight(token)
        validate_target_proof(payload["target_preflight"])
        report.update(phase="POST_INTENT", request_attempted=True, post_intent_durable=True)
        base.private_write(path, report, replace=True)
        stream_error = None
        try:
            base.model_request("target", token, payload["request_id"], payload["client_request_id"], report["stream"])
        except Exception as error:
            stream_error = safe_error(error, base)
            report["stream_error"] = stream_error
        report["phase"] = "POST_RESULT"
        base.private_write(path, report, replace=True)
        effective = report["stream"].get("effective_client_request_id")
        if effective:
            need(base.valid_uuid(effective), "invalid-effective-request-id")
            key["effective_client_request_id"] = effective
            deadline = time.monotonic() + 30
            while True:
                value = base.exact_billing("client:" + effective, key["api_key_id"])
                report["observed_usage_row_count"], report["observed_billing_dedup_row_count"] = value.get("usage_count"), value.get("dedup_count")
                need(value.get("usage_count", 0) <= 1 and value.get("dedup_count", 0) <= 1, "duplicate-exact-request-billing")
                if value.get("usage_count") == value.get("dedup_count") == 1:
                    if not stream_error:
                        report["billing"] = base.validate_billing(value, report["stream"]["usage"], key)
                    break
                need(time.monotonic() < deadline, "exact-request-billing-did-not-settle")
                time.sleep(0.25)
        need(not stream_error, stream_error or "model-stream-failed")
        need(report.get("billing"), "billing-evidence-missing")
        report["source_postflight"] = authority()
        need(source_cohort(payload["target_preflight"]) == report["source_cohort"], "source-cohort-changed-during-canary")
        report.update(phase="BILLING_VERIFIED", awaiting_target_postflight=True)
    except Exception as error:
        report["error"] = safe_error(error, base)
    report["worker_finished_at"] = time.time()
    need(token not in canonical(report).decode(), "secret-in-evidence-refused")
    base.private_write(path, report, replace=True)
    return report


def remote_inspect():
    aws_root()
    private_root_dir(SCOPE, private=True)
    return json.loads(root_read(SCOPE / JOURNAL))


def remote_finalize(payload, base):
    aws_root()
    need(type(payload) is dict and set(payload) == {"request_id", "target_postflight"}, "invalid-finalization")
    report = remote_inspect()
    need(report["request_id"] == payload["request_id"] and report.get("phase") == "BILLING_VERIFIED" and
         report.get("awaiting_target_postflight") is True and not report.get("error") and report.get("billing"),
         "existing-canary-not-ready-for-acceptance")
    same_generation(report["target_preflight"], payload["target_postflight"])
    need(source_cohort(payload["target_postflight"]) == report.get("source_cohort"), "source-cohort-changed-before-acceptance")
    authority = source_sql_adapter(base)
    report["source_finalflight"] = authority()
    report.update(target_postflight=payload["target_postflight"], awaiting_target_postflight=False,
                  phase="COMPLETE", ok=True, finished_unix_seconds=time.time())
    base.private_write(SCOPE / JOURNAL, report, replace=True)
    return report


def remote_source(action, payload=None):
    need(action in ("target", "apply", "inspect", "finalize"), "invalid-remote-action")
    source = pathlib.Path(__file__).read_text()
    if action in ("apply", "finalize"):
        raw = pathlib.Path(__file__).with_name("model_canary.py").read_bytes()
        need(sha(raw) == BASE_SHA256, "reviewed-model-canary-changed")
        base_expr = "reused_base(base64.b64decode(" + repr(base64.b64encode(raw).decode()) + "))"
        call = ("remote_apply" if action == "apply" else "remote_finalize") + "(" + repr(payload) + "," + base_expr + ")"
    else:
        call = "target_preflight()" if action == "target" else "remote_inspect()"
    return "__name__='recovery_generation_canary_remote'\n__file__='recovery_generation_canary.py'\n" + source + \
           "\ntry:\n result=" + call + "\n print(json.dumps(result,sort_keys=True),flush=True)\nexcept Exception as error:\n" + \
           " print(json.dumps({'ok':False,'error':str(error) if isinstance(error,Refusal) else 'recovery-remote-proof-or-outcome-unavailable'}),flush=True)\n"


def target_ssh():
    spec = importlib.util.spec_from_file_location("recovery_existing_channel", pathlib.Path(__file__).with_name("prepare_channel.py"))
    channel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(channel)
    need(channel.TARGET_IP == TARGET_IP and channel.TARGET[-1] == "ubuntu@" + TARGET_IP and
         "StrictHostKeyChecking=yes" in channel.TARGET, "reviewed-tencent-ssh-route-changed")
    return channel.TARGET


def remote(ssh, action, payload=None):
    try:
        result = subprocess.run(ssh + ["sudo -n python3 -I -"], input=remote_source(action, payload).encode(),
                                capture_output=True, timeout=200 if action == "apply" else 45)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("recovery-ssh-outcome-unknown-inspect-journal-no-repeat") from None
    need(result.returncode == 0, "recovery-ssh-result-unavailable-inspect-journal-no-repeat")
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise Refusal("recovery-ssh-json-invalid-inspect-journal-no-repeat") from None
    need(type(value) is dict and (not payload or not payload.get("token") or payload["token"] not in canonical(value).decode()),
         "secret-or-invalid-evidence-refused")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--inspect", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply and not args.inspect:
        print(json.dumps({"offline": True, "network_access": False, "billable_request_sent": False,
                          "lane": "recovery-generation", "target_ip": TARGET_IP, "tls_port": 443, "sni_host": DOMAIN,
                          "new_app_container_id": APP_ID, "control_port": 18093, "billing_authority": "AWS",
                          "model": "gpt-6.1-sol", "max_output_tokens": 128, "durable_no_repeat_journal": str(SCOPE / JOURNAL)}))
        return 0
    base = load_base()
    local = pathlib.Path.home() / ".codex/backups" / ("sub2api-recovery-generation-canary-" + uuid.uuid4().hex[:16])
    base.no_symlinks(local)
    local.mkdir(mode=0o700, parents=True)
    if args.inspect:
        report = remote(base.SSH, "inspect")
    else:
        proof = remote(target_ssh(), "target")
        validate_target_proof(proof)
        payload = {"token": base.load_token(), "request_id": "migration-canary-recovery-generation-" + uuid.uuid4().hex,
                   "client_request_id": str(uuid.uuid4()), "target_preflight": proof}
        base.private_write(local / "request.json", {k: v for k, v in payload.items() if k != "token"})
        report = remote(base.SSH, "apply", payload)
        base.private_write(local / "worker-report.json", report)
        if report.get("phase") == "BILLING_VERIFIED" and report.get("awaiting_target_postflight") is True and not report.get("error"):
            after = remote(target_ssh(), "target")
            same_generation(proof, after)
            base.private_write(local / "target-postflight.json", after)
            report = remote(base.SSH, "finalize", {"request_id": payload["request_id"], "target_postflight": after})
    base.private_write(local / "report.json", report)
    print(json.dumps({**report, "local_report": str(local / "report.json"), "report_sha256": sha(canonical(report))}, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"ok": False, "error": "recovery-canary-outcome-unknown-inspect-journal-no-repeat"}), file=sys.stderr)
        raise SystemExit(1)
