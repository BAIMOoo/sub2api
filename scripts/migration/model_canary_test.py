#!/usr/bin/env python3
"""Offline tests; never call a model, SQL server, Docker, SSH or private config."""
import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

PATH = pathlib.Path(__file__).with_name("model_canary.py")
SPEC = importlib.util.spec_from_file_location("model_canary", PATH)
M = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)
CLIENT = "01234567-89ab-4cde-8f01-23456789abcd"
TOKEN = "PRIVATE_CANARY_TOKEN_DO_NOT_OUTPUT"


def event(kind, **fields):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


def complete_stream():
    return (event("response.created", response={"id": "resp_migration123", "status": "in_progress"}) +
        event("response.output_text.delta", delta="MIGRATION_OK") +
        event("response.completed", response={"id": "resp_migration123", "status": "completed",
            "usage": {"input_tokens": 15, "output_tokens": 5, "total_tokens": 20}}))


def billing_value():
    return {"usage_count": 1, "dedup_count": 1, "quota_used": "1.00005000", "rows": [{
        "usage_log_id": 1, "model": M.MODEL, "requested_model": M.MODEL, "stream": True,
        "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 5, "cache_creation_tokens": 0,
        "total_cost": "0.00004", "actual_cost": "0.00005", "rate_multiplier": "1.25", "billing_type": 0,
        "ledger_count": 0, "ledger_delta": "0", "ledger_all_applied": False}]}


def preflight():
    return {"api_key_id": 123, "effective_client_request_id": CLIENT, "quota_used": "1", "quota_enabled": True}


class CanaryTests(unittest.TestCase):
    def test_default_never_reads_private_config_or_starts_process(self):
        with mock.patch.object(M, "load_token", side_effect=AssertionError("secret read")), \
             mock.patch.object(M.subprocess, "run", side_effect=AssertionError("process")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
        self.assertFalse(json.loads(out.getvalue())["billable_request_sent"])

    def test_completed_stream_requires_text_usage_and_eof(self):
        stats = {}; M.consume_sse(io.BytesIO(complete_stream()), stats, 0, monotonic=lambda: 2)
        self.assertEqual(stats["response_id"], "resp_migration123")
        self.assertEqual(stats["first_text_delta_seconds"], 2)
        self.assertTrue(stats["eof_after_terminal"])
        self.assertEqual(stats["usage"]["total_tokens"], 20)
        self.assertNotIn("MIGRATION_OK", json.dumps(stats))

    def test_disconnect_error_incomplete_and_malformed_streams_fail(self):
        samples = [event("response.output_text.delta", delta="hello"),
            complete_stream().rstrip(b"\n"), event("error", error={"message": TOKEN}),
            event("response.incomplete"), b"event: response.created\ndata: not-json\n\n",
            b"event: one\ndata: {\"type\":\"another\"}\n\n", b"data: [DONE]\n\n",
            complete_stream() + event("response.output_text.delta", delta="late"),
            complete_stream() + event("response.completed", response={}),
            b"data: " + b"x" * M.MAX_FRAME + b"\n\n"]
        for sample in samples:
            with self.subTest(sample_length=len(sample)):
                with self.assertRaises(M.Refusal) as error:
                    M.consume_sse(io.BytesIO(sample), {}, 0, monotonic=lambda: 1)
                self.assertNotIn(TOKEN, str(error.exception))

    def test_terminal_usage_and_response_ids_must_agree(self):
        raw = complete_stream()
        for sample in [raw.replace(b'"total_tokens": 20', b'"total_tokens": 21'),
                       raw.replace(b'"output_tokens": 5', b'"output_tokens": true'),
                       raw.replace(b'"status": "completed"', b'"status": "failed"'),
                       raw + event("response.created", response={"id": "resp_different"})]:
            with self.assertRaises(M.Refusal):
                M.consume_sse(io.BytesIO(sample), {}, 0, monotonic=lambda: 1)

    def test_exact_billing_checks_cached_tokens_cost_and_atomic_dedup(self):
        result = M.validate_billing(billing_value(), {"input_tokens": 15, "output_tokens": 5}, preflight())
        self.assertTrue(result["tokens_match_stream"])
        self.assertEqual(result["quantized_charge_usd"], "0.00005000")
        self.assertFalse(result["balance_delta_attribution_proven"])
        self.assertTrue(result["shared_counter_delta_is_not_canary_charge"])
        for modify in [lambda v: v.update(usage_count=2), lambda v: v.update(dedup_count=0),
            lambda v: v["rows"][0].update(input_tokens=15), lambda v: v["rows"][0].update(actual_cost="0"),
            lambda v: v["rows"][0].update(actual_cost="0.00006"), lambda v: v["rows"][0].update(ledger_count=2)]:
            value = billing_value(); modify(value)
            with self.assertRaises(M.Refusal):
                M.validate_billing(value, {"input_tokens": 15, "output_tokens": 5}, preflight())

    def test_per_request_ledger_is_claimed_only_when_present_and_matching(self):
        value = billing_value(); row = value["rows"][0]
        row.update(ledger_count=1, ledger_delta="-0.00005", ledger_all_applied=True)
        result = M.validate_billing(value, {"input_tokens": 15, "output_tokens": 5}, preflight())
        self.assertTrue(result["balance_delta_attribution_proven"])
        row["ledger_delta"] = "0.00006"
        with self.assertRaises(M.Refusal):
            M.validate_billing(value, {"input_tokens": 15, "output_tokens": 5}, preflight())

    def test_sql_is_readonly_uses_fixed_id_and_stdin_even_for_private_key(self):
        with mock.patch.object(M, "verify_source_pg"), mock.patch.object(M, "run", return_value=b'{}') as call:
            M.sql("SELECT '" + TOKEN + "'")
        args = call.call_args.args[0]; data = call.call_args.kwargs["data"]
        self.assertIn(M.SOURCE_PG_ID, args); self.assertNotIn(TOKEN, repr(args))
        self.assertTrue(data.startswith(b"BEGIN READ ONLY; SET LOCAL statement_timeout='2s';"))
        self.assertIn(TOKEN.encode(), data)
        with mock.patch.object(M, "sql") as sql:
            for bad in ["client:' OR true --", "client:../all", "local:" + CLIENT]:
                with self.assertRaises(M.Refusal):
                    M.exact_billing(bad, 1)
            sql.assert_not_called()

    def test_http_has_one_post_no_retries_and_fixed_loopback_routes(self):
        response = mock.Mock(); response.status = 200
        response.getheader.side_effect = lambda k, default=None: {
            "X-Client-Request-ID": CLIENT, "X-Request-ID": "canary", "Content-Type": "text/event-stream"}.get(k, default)
        for lane, port in M.PORTS.items():
            target = mock.patch.object(M, "TargetTLSConnection") if lane == "target" else mock.patch.object(M.http.client, "HTTPConnection")
            with target as ctor, mock.patch.object(M, "consume_sse"):
                connection = ctor.return_value; connection.getresponse.return_value = response
                connection.evidence = {"certificate_chain_and_hostname_verified": True}
                stats = {}; M.model_request(lane, TOKEN, "canary", CLIENT, stats)
                if lane == "target":
                    ctor.assert_called_once_with(timeout=90)
                    self.assertTrue(stats["transport"]["certificate_chain_and_hostname_verified"])
                else:
                    ctor.assert_called_once_with("127.0.0.1", port, timeout=90)
                connection.request.assert_called_once()
                args, kwargs = connection.request.call_args
                self.assertEqual(args, ("POST", "/v1/responses"))
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
                self.assertEqual(json.loads(kwargs["body"])["max_output_tokens"], 128)
                self.assertEqual(stats["effective_client_request_id"], CLIENT)
                connection.close.assert_called_once()

    def test_target_tls_connects_fixed_ip_but_verifies_real_domain_and_chain(self):
        with mock.patch.object(M.ssl, "create_default_context") as context, mock.patch.object(M.socket, "create_connection") as dial, \
             mock.patch.object(M, "TARGET_CERT_SHA256", M.hashlib.sha256(b"leaf-certificate").hexdigest()):
            ctx = context.return_value; ctx.check_hostname = True; ctx.verify_mode = M.ssl.CERT_REQUIRED
            tls = ctx.wrap_socket.return_value; tls.getpeername.return_value = (M.TARGET_IP, 443)
            tls.getpeercert.return_value = b"leaf-certificate"; tls.version.return_value = "TLSv1.3"
            connection = M.TargetTLSConnection(timeout=5); connection.connect()
            dial.assert_called_once_with((M.TARGET_IP, 443), 5)
            ctx.wrap_socket.assert_called_once_with(dial.return_value, server_hostname=M.TARGET_DOMAIN)
            self.assertEqual(connection.host, M.TARGET_DOMAIN)
            self.assertTrue(connection.evidence["certificate_chain_and_hostname_verified"])
            tls.getpeercert.return_value = b"wrong-leaf"
            with self.assertRaisesRegex(M.Refusal, "certificate-pin-mismatch"):
                connection.connect()
            ctx.check_hostname = False
            with self.assertRaisesRegex(M.Refusal, "verification-disabled"):
                connection.connect()

    def test_target_preflight_checks_bridge_health_and_real_caddy_tls(self):
        with mock.patch.object(M.http.client, "HTTPConnection") as bridge, mock.patch.object(M, "TargetTLSConnection") as target:
            for ctor in [bridge, target]:
                ctor.return_value.getresponse.return_value.status = 200
            target.return_value.evidence = {"peer_ip": M.TARGET_IP}
            result = M.health("target")
            bridge.assert_called_once_with("127.0.0.1", 18081, timeout=3)
            target.assert_called_once_with(timeout=3)
            self.assertEqual(result["tls_health_http_status"], 200)
            bridge.return_value.request.assert_called_once_with("GET", "/health", headers={"Connection": "close"})
            target.return_value.request.assert_called_once_with("GET", "/health", headers={"Connection": "close"})

    def test_journal_refuses_repeat_after_ambiguous_request_without_retry(self):
        # tempfile's actual owner is the local user; isolate directory owner verification
        # without replacing the filesystem calls used by durable journal writes.
        with tempfile.TemporaryDirectory() as temporary:
            scope = pathlib.Path(temporary).resolve() / "canary"
            with mock.patch.object(M, "SCOPE", scope), mock.patch.object(M.os, "geteuid", return_value=0), \
                 mock.patch.object(M.sys, "platform", "linux"), mock.patch.object(M, "key_preflight", return_value=preflight()), \
                 mock.patch.object(M, "health", return_value={}), mock.patch.object(M, "model_request") as model, \
                 mock.patch.object(M.signal, "signal"):
                real_stat = pathlib.Path.stat
                def root_stat(path, *args, **kwargs):
                    value = real_stat(path, *args, **kwargs)
                    if path == scope:
                        fields = list(value); fields[4] = 0; return M.os.stat_result(fields)
                    return value
                payload = {"lane": "source", "token": TOKEN, "request_id": "migration-canary-source-" + "a" * 32, "client_request_id": CLIENT}
                def ambiguous_post(*args):
                    persisted = json.loads((scope / "source.json").read_text())
                    self.assertTrue(persisted["request_attempted"])
                    self.assertTrue(persisted["post_intent_durable"])
                    raise OSError(TOKEN)
                model.side_effect = ambiguous_post
                with mock.patch.object(pathlib.Path, "stat", root_stat):
                    report = M.remote_apply(payload)
                    self.assertFalse(report["ok"]); self.assertTrue(report["request_attempted"])
                    self.assertNotIn(TOKEN, json.dumps(report))
                    with self.assertRaisesRegex(M.Refusal, "already-reserved"):
                        M.remote_apply(payload)
                model.assert_called_once()
                self.assertEqual((scope / "source.json").stat().st_mode & 0o777, 0o600)

    def test_apply_transports_secret_only_in_ssh_stdin_and_not_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(M.pathlib.Path, "home", return_value=pathlib.Path(temporary).resolve()), \
             mock.patch.object(M, "load_token", return_value=TOKEN), \
             mock.patch.object(M.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b'{"ok":true}', b"")) as process, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main(["--apply", "source"]), 0)
            argv = process.call_args.args[0]
            self.assertNotIn(TOKEN, repr(argv))
            self.assertIn(TOKEN.encode(), process.call_args.kwargs["input"])
            self.assertNotIn("env", process.call_args.kwargs)
            self.assertNotIn(TOKEN, out.getvalue())
            for artifact in pathlib.Path(temporary).rglob("*.json"):
                self.assertNotIn(TOKEN, artifact.read_text())
                self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)

    def test_inspect_uses_no_token_or_post_and_no_payload_mutations(self):
        with mock.patch.object(M, "load_token", side_effect=AssertionError("secret read")):
            source = M.remote_source({"lane": "source"}, inspect=True)
            self.assertTrue(source.split("\ntry:\n result=")[-1].startswith("remote_inspect('source')"))
            compile(source, "read-only-inspect-worker", "exec")

    def test_successful_worker_records_real_stream_and_exact_billing_only(self):
        # The real remote worker is separately exercised by the explicitly authorized canary.
        source = M.remote_source({"lane": "source", "token": TOKEN, "request_id": "a", "client_request_id": CLIENT})
        compile(source, "remote-canary", "exec")
        self.assertTrue(source.startswith("__name__='model_canary_remote'"))
        self.assertNotIn("users.balance", PATH.read_text())
        for mutation in ['"docker", "stop"', '"docker", "start"', '"iptables"', '"systemctl"']:
            self.assertNotIn(mutation, PATH.read_text())


if __name__ == "__main__":
    unittest.main()
