#!/usr/bin/env python3
"""One durable postcutover Tencent TLS model/billing canary; default offline."""
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

RUN = "20261016"
OWNER = "30bda011defad8149621c2630a2bd89c"
BASE_SHA256 = "b8d6096172abd798865aaf0263267bd66e4ebf74cfae6227caa648da4e17ed1f"
RPC_SHA256 = "8180879f75169f130b726536ee16957832a9d2bf9e193f93ad380ebd8264f6ec"
RPC_PROGRAM = pathlib.Path("/usr/local/libexec/sub2api-migration-20261016/target_rpc.py")
PREFIX = pathlib.Path("/var/lib/sub2api-migration-20261016")
SCOPE = PREFIX / "postcutover-canary"
JOURNAL = "target.json"
PG_ID = "ac9f494b2fd0ec87257a9a7308a6ad7b270051d1dc4da33b33fea9af2885418c"
PG_IMAGE = "sha256:9a5296338b4957a7082ad925c9f31346f6f1cb6b7cbe7bd2971430432488a395"
SYSTEM_ID = "7626048206782038050"
APP_ID = "17e3bfacbd442beff52427e003e58f4970e613203356b8bb8bde0e98b9229bce"
APP_IMAGE = "sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a"
TARGET_IP, DOMAIN = "43.165.177.136", "api.b4im.com"
ENDPOINTS = {"v": 1, "postgres": "target", "redis": "target"}


class Refusal(RuntimeError):
    pass


def need(condition, code):
    if not condition:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def private_root_dir(path, private=False):
    path = pathlib.Path(path)
    for parent in [path, *path.parents]:
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
             "untrusted-root-directory")
    need(not private or stat.S_IMODE(path.stat().st_mode) == 0o700, "journal-directory-not-private")


def root_read(path, mode=0o600, limit=2 * 1024**2):
    path = pathlib.Path(path)
    private_root_dir(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
             stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "untrusted-root-file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(limit + 1)
    finally:
        os.close(fd)


def reused_base(raw):
    need(hashlib.sha256(raw).hexdigest() == BASE_SHA256, "reviewed-model-canary-changed")
    module = types.ModuleType("postcutover_reused_model_canary")
    module.__file__ = str(pathlib.Path(__file__).with_name("model_canary.py"))
    exec(compile(raw, "reviewed-model-canary", "exec"), module.__dict__)
    need(module.TARGET_IP == TARGET_IP and module.TARGET_DOMAIN == DOMAIN and
         module.SOURCE_SYSTEM_ID == SYSTEM_ID, "reused-canary-pins-changed")
    return module


def load_base():
    return reused_base(pathlib.Path(__file__).with_name("model_canary.py").read_bytes())


def load_target():
    need(hashlib.sha256(root_read(RPC_PROGRAM, mode=0o700)).hexdigest() == RPC_SHA256,
         "reviewed-target-rpc-changed")
    spec = importlib.util.spec_from_file_location("postcutover_target_rpc", RPC_PROGRAM)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cfg = module.load_config()
    need(cfg["owner"] == OWNER and cfg["pg"]["container_id"] == PG_ID and
         cfg["pg"]["image_id"] == PG_IMAGE and cfg["pg"]["system_identifier"] == SYSTEM_ID and
         cfg["app"]["container_id"] == APP_ID and cfg["app"]["image_id"] == APP_IMAGE and cfg["app"]["control_port"] == 18093,
         "accepted-target-resource-pins-changed")
    return module, cfg


class TargetBackend:
    def __init__(self, rpc, cfg):
        self.r, self.cfg, self.b = rpc, cfg, rpc.LocalBackend(cfg)
        self.ledger = rpc.FileStore(cfg)

    def authority(self):
        state = self.ledger.read()
        need(state["owner"] == OWNER and state["pg"]["authority"] == state["redis"]["authority"] == "TENCENT" and
             state["pg"].get("promotion_intent") and self.b.endpoint_state() == ENDPOINTS,
             "target-authority-or-persisted-endpoints-unconfirmed")
        need(not any(op["phase"] != "COMPLETE" and self.r.TargetRPC.is_mutation(op["request"])
                     for op in state["operations"].values()), "target-migration-mutation-pending")
        pg = self.b.resource("pg")
        need(pg["Id"] == PG_ID and pg["Image"] == PG_IMAGE and pg["State"]["Running"] is True,
             "original-tencent-postgres-not-running")
        with self.b.pg_locks():
            manifest, sample = self.b.pg_manifest(), self.b.pg_sample()
            need(manifest["authority_mode"] == "PRIMARY" and
                 manifest.get("promotion_intent") == state["pg"]["promotion_intent"], "target-primary-intent-mismatch")
            self.r.TargetRPC(self.cfg, self.b, self.ledger)._pg_validate(sample, primary=True)
        need(sample["system_identifier"] == SYSTEM_ID and sample["in_recovery"] is False,
             "target-postgres-not-original-primary")
        redis = self.b.redis_inspect()
        need(redis["role"] == "master" and redis["failover_state"] == "no-failover", "target-redis-not-stable-primary")
        return {"pg_container_id": PG_ID, "pg_image_id": PG_IMAGE, "system_identifier": SYSTEM_ID,
                "postgres_port": 25432, "primary": True, "pg_authority": "TENCENT", "redis_authority": "TENCENT",
                "persistent_endpoints": ENDPOINTS, "pg_wal_timeline": sample["wal_timeline"]}

    def control(self, name):
        need(name.endswith(".status"), "read-only-controls-required")
        value = self.b.app_control(name)
        need(value.get("http_status") == 200 and type(value.get("body")) is dict,
             "target-app-status-unavailable")
        return value["body"]

    def preflight(self):
        evidence = self.authority()
        app = self.b.resource("app")
        need(app["Id"] == APP_ID and app["Image"] == APP_IMAGE, "original-tencent-app-changed")
        before = self.control("runtime.status")
        values = {name: self.control(name + ".status") for name in ("postgres", "redis", "writers", "ingress", "background")}
        fresh = self.control("runtime.status")
        need(isinstance(before.get("instance_id"), str) and before["instance_id"] and
             fresh.get("instance_id") == before["instance_id"] and fresh.get("ready") is True and
             fresh.get("serving") is True and fresh.get("draining") is False and
             fresh.get("background_workers_enabled") is True, "target-app-not-ready-with-background")
        pg, redis, writers, ingress, bg = (values[key] for key in ("postgres", "redis", "writers", "ingress", "background"))
        need(pg.get("endpoint") == "target" and pg.get("paused") is False and
             redis.get("address") == "127.0.0.1:26379" and redis.get("paused") is False and redis.get("pubsub_cache_ready") is True and
             writers.get("held") is False and ingress.get("paused") is False and
             bg.get("activated") is True and bg.get("activating") is False and
             bg.get("pending") == [] and isinstance(bg.get("started"), list) and bool(bg["started"]) and
             not bg.get("failures"), "target-app-controls-or-background-unready")
        return {**evidence, "app_container_id": APP_ID, "app_image_id": APP_IMAGE,
                "app_pid": app["State"]["Pid"], "instance_id": fresh["instance_id"],
                "app_ready": True, "background_ready": True, "background_started_count": len(bg["started"])}

    def sql(self, query):
        self.authority()  # Every billing query re-proves this exact target primary.
        raw = self.b.command(["/usr/bin/docker", "exec", "--user", "postgres", "-i", PG_ID,
            "psql", "-X", "-q", "-A", "-t", "-w", "-v", "ON_ERROR_STOP=1", "-h", "/tmp", "-p", "25432",
            "-U", "sub2api", "-d", "sub2api"],
            ("BEGIN READ ONLY; SET LOCAL statement_timeout='2s'; " + query + "; COMMIT;\n").encode())
        return self.r.strict_json(raw, 65536)


def tls_health(base):
    conn = base.TargetTLSConnection(timeout=5)
    try:
        conn.request("GET", "/health", headers={"Connection": "close"})
        response = conn.getresponse()
        need(response.status == 200 and len(response.read(base.MAX_FRAME + 1)) <= base.MAX_FRAME,
             "target-tls-health-unready")
        return {"http_status": response.status, "transport": conn.evidence}
    finally:
        conn.close()


def safe_error(error, base):
    if isinstance(error, (Refusal, base.Refusal)) and re.fullmatch(r"[a-z][a-z0-9-]{1,100}", str(error)):
        return str(error)
    return "postcutover-check-failed-no-secret-output"


def remote_apply(payload, base, backend=None):
    need(os.geteuid() == 0 and sys.platform == "linux", "tencent-linux-root-required")
    need(set(payload) == {"token", "request_id", "client_request_id"} and
         base.valid_uuid(payload["client_request_id"]) and
         re.fullmatch(r"migration-canary-postcutover-[0-9a-f]{32}", payload["request_id"]), "invalid-postcutover-request")
    token = payload["token"]
    need(isinstance(token, str) and 16 <= len(token) <= 128 and not any(c in token for c in "\r\n\x00"), "invalid-private-token")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    private_root_dir(SCOPE.parent, private=True)
    if not SCOPE.exists() and not SCOPE.is_symlink():
        SCOPE.mkdir(mode=0o700)
        directory = os.open(SCOPE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    private_root_dir(SCOPE, private=True)
    path = SCOPE / JOURNAL
    report = {"v": 1, "run": RUN, "lane": "postcutover", "ok": False, "request_id": payload["request_id"],
        "supplied_client_request_id": payload["client_request_id"], "model": base.MODEL,
        "max_output_tokens": 128, "request_attempted": False, "no_automatic_retry": True,
        "production_routes_changed": False, "stream": {}, "journal_path": str(path), "started_unix_seconds": time.time()}
    try:
        base.private_write(path, report)
    except FileExistsError:
        raise Refusal("postcutover-lane-reserved-inspect-evidence-no-repeat") from None
    try:
        if backend is None:
            rpc, cfg = load_target()
            backend = TargetBackend(rpc, cfg)
        base.sql = backend.sql  # Reuse key/exact-request SQL, always against pinned Tencent PG.
        report["target_preflight"] = backend.preflight()
        preflight = base.key_preflight(token)
        report["tls_health"] = tls_health(base)
        fresh = backend.preflight()
        need(fresh == report["target_preflight"], "target-changed-before-post")
        report.update(request_attempted=True, post_intent_durable=True)
        base.private_write(path, report, replace=True)
        stream_error = None
        try:
            base.model_request("target", token, payload["request_id"], payload["client_request_id"], report["stream"])
        except Exception as error:
            stream_error = safe_error(error, base)
            report["stream_error"] = stream_error
        base.private_write(path, report, replace=True)
        effective = report["stream"].get("effective_client_request_id")
        if effective:
            need(base.valid_uuid(effective), "invalid-effective-request-id")
            preflight["effective_client_request_id"] = effective
            deadline = time.monotonic() + 30
            while True:
                value = base.exact_billing("client:" + effective, preflight["api_key_id"])
                report["observed_usage_row_count"] = value.get("usage_count")
                report["observed_billing_dedup_row_count"] = value.get("dedup_count")
                need(value.get("usage_count", 0) <= 1 and value.get("dedup_count", 0) <= 1, "duplicate-exact-request-billing")
                if value.get("usage_count") == value.get("dedup_count") == 1:
                    if not stream_error:
                        report["billing"] = base.validate_billing(value, report["stream"]["usage"], preflight)
                    break
                need(time.monotonic() < deadline, "exact-request-billing-did-not-settle")
                time.sleep(0.25)
        need(not stream_error, stream_error or "model-stream-failed")
        need(report.get("billing"), "billing-evidence-missing")
        report["target_postflight"] = backend.preflight()
        need(report["target_postflight"] == report["target_preflight"], "target-changed-during-canary")
        report["ok"] = True
    except Exception as error:
        report["error"] = safe_error(error, base)
    report["finished_unix_seconds"] = time.time()
    need(token not in canonical(report).decode(), "secret-in-evidence-refused")
    base.private_write(path, report, replace=True)
    return report


def remote_inspect():
    need(os.geteuid() == 0 and sys.platform == "linux", "tencent-linux-root-required")
    private_root_dir(SCOPE, private=True)
    return json.loads(root_read(SCOPE / JOURNAL))


def remote_source(payload=None, inspect=False):
    source = pathlib.Path(__file__).read_text()
    if inspect:
        call = "remote_inspect()"
    else:
        raw = pathlib.Path(__file__).with_name("model_canary.py").read_bytes()
        need(hashlib.sha256(raw).hexdigest() == BASE_SHA256, "reviewed-model-canary-changed")
        call = "remote_apply(" + repr(payload) + ", reused_base(base64.b64decode(" + repr(base64.b64encode(raw).decode()) + ")))"
    return "__name__='postcutover_canary_remote'\n__file__='postcutover_canary.py'\n" + source + "\ntry:\n result=" + call + "\n print(json.dumps(result,sort_keys=True),flush=True)\nexcept Exception:\n print(json.dumps({'ok':False,'error':'postcutover-remote-refused-inspect-journal-no-repeat'}),flush=True)\n"


def target_ssh():
    spec = importlib.util.spec_from_file_location("postcutover_existing_channel", pathlib.Path(__file__).with_name("prepare_channel.py"))
    channel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(channel)
    need(channel.TARGET_IP == TARGET_IP and channel.TARGET[-1] == "ubuntu@" + TARGET_IP and
         "StrictHostKeyChecking=yes" in channel.TARGET, "reviewed-tencent-ssh-route-changed")
    return channel.TARGET


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--inspect", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply and not args.inspect:
        print(json.dumps({"offline": True, "network_access": False, "billable_request_sent": False,
            "lane": "postcutover", "target_ip": TARGET_IP, "tls_port": 443, "sni_host": DOMAIN,
            "pg_container_id": PG_ID, "pg_port": 25432, "requires_target_target_and_background_ready": True,
            "durable_no_repeat_journal": str(SCOPE / JOURNAL)}))
        return 0
    base = load_base()
    payload = {}
    if args.apply:
        payload = {"token": base.load_token(), "request_id": "migration-canary-postcutover-" + uuid.uuid4().hex,
                   "client_request_id": str(uuid.uuid4())}
    local = pathlib.Path.home() / ".codex/backups" / ("sub2api-postcutover-canary-" + uuid.uuid4().hex[:16])
    base.no_symlinks(local)
    local.mkdir(mode=0o700, parents=True)
    base.private_write(local / "request.json", {k: v for k, v in payload.items() if k != "token"})
    try:
        result = subprocess.run(target_ssh() + ["sudo -n python3 -I -"],
            input=remote_source(payload, inspect=args.inspect).encode(), capture_output=True, timeout=200)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("postcutover-ssh-outcome-unknown-inspect-journal-no-repeat") from None
    need(result.returncode == 0, "postcutover-ssh-result-unavailable-inspect-journal-no-repeat")
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise Refusal("postcutover-ssh-json-invalid-inspect-journal-no-repeat") from None
    need(type(report) is dict and (not payload.get("token") or payload["token"] not in canonical(report).decode()),
         "secret-or-invalid-evidence-refused")
    base.private_write(local / "report.json", report)
    print(json.dumps({**report, "local_report": str(local / "report.json"),
                      "report_sha256": hashlib.sha256(canonical(report)).hexdigest()}, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"ok": False, "error": "postcutover-outcome-unknown-inspect-journal-no-repeat"}), file=sys.stderr)
        raise SystemExit(1)
