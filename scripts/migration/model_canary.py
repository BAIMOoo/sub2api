#!/usr/bin/env python3
"""One bounded real Responses SSE request plus exact-request billing evidence.

Default execution is offline. --apply source|target is explicitly billable.
Each lane has a durable no-repeat journal; --inspect reads it without a call.
"""
from __future__ import annotations

import argparse
import decimal
import hashlib
import http.client
import json
import os
import pathlib
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import uuid

MODEL = "gpt-6.1-sol"
PORTS = {"source": 8081, "target": 18081}
TARGET_IP = "43.165.177.136"
TARGET_DOMAIN = "api.b4im.com"
TARGET_CERT_SHA256 = "d85ae437e821b8ad794a19e001019d21fcbd8ca6b54cfa658385652fd33c4d09"
SOURCE_PG_ID = "c99ec20ad4468b29698083cdc6827c853141717f0cf0a7a98bfed32e788414ee"
SOURCE_PG_IMAGE = "sha256:ff4a089dadda5e50a5f09604b2e2bc16f97d5befb6b05c328fc21159da96c602"
SOURCE_SYSTEM_ID = "7626048206782038050"
SCOPE = pathlib.Path("/var/lib/sub2api-migration-20261016/model-canary")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "sub2api"]
MAX_FRAME = 1024 * 1024
MAX_STREAM = 2 * MAX_FRAME


class Refusal(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def no_symlinks(path):
    need(not any(p.is_symlink() for p in [path, *path.parents]), "symlink-path-refused")


def private_write(path, value, *, replace=False):
    no_symlinks(path)
    raw = canonical(value) + b"\n"
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp") if replace else path
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    if replace:
        os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def request_body():
    return {"model": MODEL, "instructions": "Respond briefly to the user's request.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with exactly MIGRATION_OK."}]}],
        "stream": True, "store": False, "reasoning": {"effort": "low"}, "max_output_tokens": 128}


def valid_uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


class TargetTLSConnection(http.client.HTTPSConnection):
    """Connect to the fixed target IP while verifying the real domain/SNI."""
    def __init__(self, timeout):
        super().__init__(TARGET_DOMAIN, 443, timeout=timeout, context=ssl.create_default_context())
        self.evidence = None

    def connect(self):
        need(self._context.check_hostname and self._context.verify_mode == ssl.CERT_REQUIRED, "target-tls-verification-disabled")
        raw = socket.create_connection((TARGET_IP, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=TARGET_DOMAIN)
        except BaseException:
            raw.close()
            raise
        need(self.sock.getpeername()[0] == TARGET_IP, "target-tls-peer-address-mismatch")
        self.evidence = {"peer_ip": TARGET_IP, "port": 443, "sni_host": TARGET_DOMAIN,
            "certificate_chain_and_hostname_verified": True, "tls_version": self.sock.version(),
            "leaf_der_sha256": hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()}
        need(self.evidence["leaf_der_sha256"] == TARGET_CERT_SHA256, "target-tls-leaf-certificate-pin-mismatch")


def consume_sse(response, stats, started, *, monotonic=time.monotonic):
    event, data, frame_bytes = "", [], 0
    digest = hashlib.sha256()
    completed = False
    stats.update(event_count=0, text_delta_count=0, text_characters=0, stream_bytes=0,
                 terminal_event=None, eof_after_terminal=False, first_text_delta_seconds=None)

    def dispatch():
        nonlocal completed
        if not data:
            return
        payload = "\n".join(data)
        if payload == "[DONE]":
            need(completed, "done-before-completed-response")
            return
        try:
            obj = json.loads(payload)
        except ValueError:
            raise Refusal("invalid-sse-json") from None
        need(isinstance(obj, dict) and isinstance(obj.get("type"), str), "invalid-sse-object")
        kind = obj["type"]
        need(not event or event == kind, "sse-event-type-mismatch")
        need(kind not in {"error", "response.failed", "response.incomplete"} and not obj.get("error"), "model-stream-error-event")
        stats["event_count"] += 1
        need(stats["event_count"] <= 4096, "too-many-sse-events")
        if isinstance(obj.get("response"), dict):
            rid = obj["response"].get("id")
            if rid:
                need(isinstance(rid, str) and re.fullmatch(r"resp_[A-Za-z0-9_-]{1,160}", rid), "invalid-model-response-id")
                need(stats.get("response_id", rid) == rid, "model-response-id-changed")
                stats["response_id"] = rid
        if kind == "response.output_text.delta":
            need(not completed and isinstance(obj.get("delta"), str), "invalid-or-late-text-delta")
            if obj["delta"]:
                if stats["first_text_delta_seconds"] is None:
                    stats["first_text_delta_seconds"] = monotonic() - started
                stats["text_delta_count"] += 1
                stats["text_characters"] += len(obj["delta"])
        if kind == "response.completed":
            need(not completed, "duplicate-completed-response")
            r = obj.get("response", {})
            need(r.get("status") == "completed" and not r.get("error"), "response-not-completed")
            usage = r.get("usage")
            need(isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                for k in ["input_tokens", "output_tokens", "total_tokens"]), "completed-usage-missing")
            need(usage["input_tokens"] + usage["output_tokens"] == usage["total_tokens"], "usage-total-mismatch")
            stats["usage"] = {k: usage[k] for k in ["input_tokens", "output_tokens", "total_tokens"]}
            stats["terminal_event"] = kind
            stats["completed_seconds"] = monotonic() - started
            completed = True

    while True:
        need(monotonic() - started <= 120, "stream-total-deadline-exceeded")
        line = response.readline(MAX_FRAME + 1)
        if not line:
            need(not data, "sse-frame-truncated-at-eof")
            break
        need(len(line) <= MAX_FRAME, "sse-line-too-large")
        digest.update(line); stats["stream_bytes"] += len(line); frame_bytes += len(line)
        need(stats["stream_bytes"] <= MAX_STREAM and frame_bytes <= MAX_FRAME, "sse-stream-or-frame-too-large")
        try:
            text = line.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise Refusal("sse-not-utf8") from None
        if not text:
            dispatch(); event, data, frame_bytes = "", [], 0
        elif text.startswith("event:"):
            need(not event, "duplicate-sse-event-field")
            event = text[6:].lstrip(" ")
        elif text.startswith("data:"):
            data.append(text[5:].lstrip(" "))
        elif not text.startswith((":", "id:", "retry:")):
            raise Refusal("unexpected-sse-field")
    need(completed and stats["text_delta_count"] > 0 and stats.get("response_id"), "stream-ended-without-text-and-completion")
    need(stats["usage"]["output_tokens"] > 0, "completed-response-has-no-output-tokens")
    stats.update(eof_after_terminal=True, total_seconds=monotonic() - started, stream_sha256=digest.hexdigest())


def model_request(lane, token, request_id, supplied_client_id, stats):
    connection = TargetTLSConnection(timeout=90) if lane == "target" else http.client.HTTPConnection("127.0.0.1", PORTS[lane], timeout=90)
    started = time.monotonic()
    try:
        connection.request("POST", "/v1/responses", body=canonical(request_body()), headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "text/event-stream",
            "X-Request-ID": request_id, "X-Client-Request-ID": supplied_client_id,
            "User-Agent": "sub2api-migration-model-canary/20261016", "Connection": "close"})
        response = connection.getresponse()
        stats["transport"] = connection.evidence if lane == "target" else {"loopback_http_port": PORTS[lane]}
        stats["http_status"] = response.status
        effective = response.getheader("X-Client-Request-ID")
        need(valid_uuid(effective), "server-client-request-id-missing-or-invalid")
        stats["effective_client_request_id"] = effective
        need(response.getheader("X-Request-ID") == request_id, "server-request-id-not-echoed")
        need(response.status == 200 and response.getheader("Content-Type", "").split(";")[0].strip() == "text/event-stream",
             "model-http-status-or-content-type")
        consume_sse(response, stats, started)
    finally:
        connection.close()


def run(argv, *, data=None, timeout=5):
    try:
        result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("canary-command-result-unavailable") from None
    need(result.returncode == 0, "canary-command-failed-no-secret-output")
    return result.stdout


def verify_source_pg():
    c = json.loads(run(["docker", "inspect", SOURCE_PG_ID]))[0]
    need(c["Id"] == SOURCE_PG_ID and c["Name"] == "/sub2api-postgres" and c["Image"] == SOURCE_PG_IMAGE and
         c["State"]["Running"] is True, "source-pg-identity-or-state-changed")


def sql(query):
    verify_source_pg()
    raw = run(["docker", "exec", "-i", SOURCE_PG_ID, "psql", "-X", "-q", "-A", "-t", "-w",
        "-v", "ON_ERROR_STOP=1", "-U", "sub2api", "-d", "sub2api"],
        data=("BEGIN READ ONLY; SET LOCAL statement_timeout='2s'; " + query + "; COMMIT;\n").encode())
    try:
        return json.loads(raw)
    except ValueError:
        raise Refusal("canary-db-json-invalid") from None


def key_preflight(token):
    need(isinstance(token, str) and 16 <= len(token) <= 128 and not any(c in token for c in "\r\n\x00"), "invalid-private-token")
    value = sql("SELECT json_build_object('api_key_id',k.id,'active',k.status='active' AND u.status='active',"
        "'quota_enabled',k.quota>0,'quota_used',k.quota_used::text,'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),"
        "'primary',NOT pg_is_in_recovery()) FROM api_keys k JOIN users u ON u.id=k.user_id "
        "WHERE k.deleted_at IS NULL AND u.deleted_at IS NULL AND k.key='" + token.replace("'", "''") + "'")
    need(isinstance(value, dict) and value.get("active") is True and value.get("primary") is True and
         value.get("system_identifier") == SOURCE_SYSTEM_ID and type(value.get("api_key_id")) is int, "canary-key-or-authority-preflight-failed")
    return value


def exact_billing(request_id, api_key_id):
    need(re.fullmatch(r"client:[0-9a-f-]{36}", request_id) and type(api_key_id) is int and api_key_id > 0, "billing-lookup-not-exact")
    return sql("SELECT json_build_object('usage_count',(SELECT count(*) FROM usage_logs WHERE request_id='" + request_id +
        "' AND api_key_id=" + str(api_key_id) + "),'dedup_count',(SELECT count(*) FROM usage_billing_dedup WHERE request_id='" + request_id +
        "' AND api_key_id=" + str(api_key_id) + "),'rows',COALESCE((SELECT json_agg(json_build_object('usage_log_id',ul.id,"
        "'model',ul.model,'requested_model',ul.requested_model,'stream',ul.stream,'input_tokens',ul.input_tokens,'output_tokens',ul.output_tokens,"
        "'cache_read_tokens',ul.cache_read_tokens,'cache_creation_tokens',ul.cache_creation_tokens,'total_cost',ul.total_cost::text,"
        "'actual_cost',ul.actual_cost::text,'rate_multiplier',ul.rate_multiplier::text,'billing_type',ul.billing_type,"
        "'ledger_count',(SELECT count(*) FROM billing_usage_entries b WHERE b.usage_log_id=ul.id),"
        "'ledger_delta',(SELECT COALESCE(sum(delta_usd),0)::text FROM billing_usage_entries b WHERE b.usage_log_id=ul.id),"
        "'ledger_all_applied',(SELECT COALESCE(bool_and(applied),false) FROM billing_usage_entries b WHERE b.usage_log_id=ul.id))) "
        "FROM usage_logs ul WHERE ul.request_id='" + request_id + "' AND ul.api_key_id=" + str(api_key_id) +
        "),'[]'::json),'quota_used',(SELECT quota_used::text FROM api_keys WHERE id=" + str(api_key_id) + "))")


def validate_billing(value, usage, preflight):
    need(value.get("usage_count") == 1 and value.get("dedup_count") == 1 and len(value.get("rows", [])) == 1, "exact-billing-not-yet-unique-and-committed")
    row = value["rows"][0]
    need((row.get("requested_model") or row.get("model")) == MODEL and row.get("stream") is True, "billing-model-or-stream-mismatch")
    need(row["input_tokens"] + row["cache_read_tokens"] + row["cache_creation_tokens"] == usage["input_tokens"] and
         row["output_tokens"] == usage["output_tokens"], "billing-token-count-mismatch")
    actual, total, multiplier = [decimal.Decimal(row[k]) for k in ["actual_cost", "total_cost", "rate_multiplier"]]
    need(actual > 0 and total > 0 and multiplier > 0 and abs(actual - total * multiplier) <= decimal.Decimal("0.0000000002"), "billing-cost-nonpositive-or-multiplier-mismatch")
    need(row["ledger_count"] in {0, 1}, "duplicate-request-billing-ledger")
    if row["ledger_count"]:
        need(row["ledger_all_applied"] and abs(decimal.Decimal(row["ledger_delta"])) == actual, "request-ledger-cost-mismatch")
    return {"request_id": "client:" + preflight["effective_client_request_id"], "usage_row_count": 1, "billing_dedup_row_count": 1,
        "tokens_match_stream": True, "row": row, "quantized_charge_usd": str(actual.quantize(decimal.Decimal("0.00000001"), rounding=decimal.ROUND_HALF_UP)),
        "key_quota_enabled": preflight["quota_enabled"],
        "shared_key_quota_delta_usd": str(decimal.Decimal(value["quota_used"]) - decimal.Decimal(preflight["quota_used"])),
        "shared_counter_delta_is_not_canary_charge": True, "per_request_charge_ledger_proven": row["ledger_count"] == 1,
        "balance_delta_attribution_proven": row["ledger_count"] == 1 and row["billing_type"] == 0,
        "billing_atomicity_evidence": "exact dedup row commits in same transaction as normal balance/subscription/quota effects"}


def health(lane):
    evidence = {"bridge_or_source_loopback_port": PORTS[lane]}
    connection = http.client.HTTPConnection("127.0.0.1", PORTS[lane], timeout=3)
    try:
        connection.request("GET", "/health", headers={"Connection": "close"})
        response = connection.getresponse(); response.read(MAX_FRAME + 1)
        need(response.status == 200, "canary-route-health-not-ready")
        evidence["bridge_or_source_health_http_status"] = response.status
    finally:
        connection.close()
    if lane == "target":
        connection = TargetTLSConnection(timeout=3)
        try:
            connection.request("GET", "/health", headers={"Connection": "close"})
            response = connection.getresponse(); response.read(MAX_FRAME + 1)
            need(response.status == 200, "canary-target-tls-health-not-ready")
            evidence.update(tls_health_http_status=response.status, tls=connection.evidence)
        finally:
            connection.close()
    return evidence


def journal(lane):
    need(lane in PORTS, "invalid-canary-lane")
    no_symlinks(SCOPE)
    return SCOPE / (lane + ".json")


def remote_apply(payload):
    lane = payload["lane"]; path = journal(lane); token = payload["token"]
    need(os.geteuid() == 0 and sys.platform == "linux", "remote-linux-root-required")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    need(valid_uuid(payload["client_request_id"]) and re.fullmatch(r"migration-canary-(?:source|target)-[0-9a-f]{32}", payload["request_id"]), "invalid-canary-request-identity")
    SCOPE.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = SCOPE.stat(); need(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700, "canary-journal-directory-not-private-root")
    report = {"v": 1, "lane": lane, "ok": False, "request_id": payload["request_id"],
        "supplied_client_request_id": payload["client_request_id"], "model": MODEL, "max_output_tokens": 128,
        "request_attempted": False, "no_automatic_retry": True, "production_routes_changed": False,
        "stream": {}, "journal_path": str(path), "started_unix_seconds": time.time()}
    try:
        private_write(path, report)
    except FileExistsError:
        raise Refusal("canary-lane-already-reserved-inspect-existing-evidence-no-repeat") from None
    try:
        preflight = key_preflight(token); report["preflight_health"] = health(lane)
        report.update(request_attempted=True, post_intent_durable=True)
        private_write(path, report, replace=True)
        stream_error = None
        try:
            model_request(lane, token, report["request_id"], payload["client_request_id"], report["stream"])
        except Exception as exc:
            stream_error = str(exc) if isinstance(exc, Refusal) else "model-stream-result-unavailable-no-retry"
            report["stream_error"] = stream_error
        private_write(path, report, replace=True)
        effective = report["stream"].get("effective_client_request_id")
        if effective:
            preflight["effective_client_request_id"] = effective
            deadline = time.monotonic() + 30
            while True:
                value = exact_billing("client:" + effective, preflight["api_key_id"])
                report["observed_usage_row_count"] = value.get("usage_count")
                report["observed_billing_dedup_row_count"] = value.get("dedup_count")
                need(value.get("usage_count", 0) <= 1 and value.get("dedup_count", 0) <= 1, "duplicate-exact-request-billing")
                if value.get("usage_count") == value.get("dedup_count") == 1:
                    if not stream_error:
                        report["billing"] = validate_billing(value, report["stream"]["usage"], preflight)
                    break
                need(time.monotonic() < deadline, "exact-request-billing-did-not-settle")
                time.sleep(0.25)
        need(not stream_error, stream_error or "model-stream-failed")
        need(report.get("billing"), "billing-evidence-missing")
        report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc) if isinstance(exc, Refusal) else "canary-failed-no-secret-output"
    report["finished_unix_seconds"] = time.time()
    need(token not in canonical(report).decode(), "secret-in-evidence-refused")
    private_write(path, report, replace=True)
    return report


def remote_inspect(lane):
    path = journal(lane); no_symlinks(path); info = path.stat()
    need(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600 and stat.S_ISREG(info.st_mode), "canary-journal-not-private-root-file")
    return json.loads(path.read_bytes())


def remote_source(payload, *, inspect=False):
    source = pathlib.Path(__file__).read_text().replace("from __future__ import annotations\n", "")
    call = "remote_inspect(" + repr(payload["lane"]) + ")" if inspect else "remote_apply(" + repr(payload) + ")"
    return "__name__='model_canary_remote'\n" + source + "\ntry:\n result=" + call + "\n print(json.dumps(result,sort_keys=True),flush=True)\nexcept Exception as exc:\n print(json.dumps({'ok':False,'error':str(exc) if isinstance(exc,Refusal) else 'canary-remote-failed-no-secret-output'}),flush=True)\n"


def load_token():
    try:
        import tomllib
    except ImportError:
        raise Refusal("python-3.11-required-for-private-toml-parser") from None
    path = pathlib.Path.home() / ".codex/config.toml"; no_symlinks(path)
    info = path.stat(); need(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, "local-config-must-be-private")
    value = tomllib.loads(path.read_text())
    need(value.get("model_provider") == "custom", "custom-provider-not-configured")
    token = value.get("model_providers", {}).get("custom", {}).get("experimental_bearer_token")
    need(isinstance(token, str) and token, "inline-private-custom-token-missing")
    return token


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", choices=sorted(PORTS)); mode.add_argument("--inspect", choices=sorted(PORTS))
    args = parser.parse_args(argv)
    if not args.apply and not args.inspect:
        print(json.dumps({"dry_run": True, "network_access": False, "billable_request_sent": False,
            "explicit_apply_lanes": PORTS, "model": MODEL, "max_output_tokens": 128,
            "target_model_transport": {"address": TARGET_IP, "port": 443, "sni_host": TARGET_DOMAIN, "certificate_verification": True},
            "source_pg_pinned": SOURCE_PG_ID, "durable_no_repeat_journal": str(SCOPE),
            "target_requires_owner_app_ready_notice": True}))
        return 0
    lane = args.apply or args.inspect
    payload = {"lane": lane}
    if args.apply:
        payload.update(token=load_token(), request_id="migration-canary-" + lane + "-" + uuid.uuid4().hex,
                       client_request_id=str(uuid.uuid4()))
    local = pathlib.Path.home() / ".codex/backups" / ("sub2api-model-canary-" + lane + "-" + uuid.uuid4().hex[:16])
    no_symlinks(local); local.mkdir(mode=0o700, parents=True)
    private_write(local / "request.json", {k: v for k, v in payload.items() if k != "token"})
    result = subprocess.run(SSH + ["sudo -n python3 -"], input=remote_source(payload, inspect=bool(args.inspect)).encode(),
                            capture_output=True, timeout=200)
    need(result.returncode == 0, "canary-ssh-result-unavailable-inspect-journal-no-repeat")
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise Refusal("canary-ssh-json-invalid-inspect-journal-no-repeat") from None
    need(not payload.get("token") or payload["token"] not in canonical(report).decode(), "secret-in-evidence-refused")
    private_write(local / "report.json", report)
    print(json.dumps({**report, "local_report": str(local / "report.json"), "report_sha256": hashlib.sha256(canonical(report)).hexdigest()}, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refusal) else "canary-operation-result-unavailable-no-secret-output"}), file=sys.stderr)
        raise SystemExit(1)
