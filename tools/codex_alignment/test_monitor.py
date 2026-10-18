import copy
import datetime
import io
import http.client
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tools.codex_alignment.capture import Budget, Capture, parse_hello, read_head, socks_connect
from tools.codex_alignment.protocol import completion
from tools.codex_alignment.runner import compaction_observed, gateway, native_answer_matches
from tools.codex_alignment.evidence import (atomic_json, compare_replay, decode_body, differences,
                                            http_evidence, retain, sanitize, ticket)
from tools.codex_alignment.notifications import Outbox, read_env
from tools.codex_alignment.schedule import Schedule, lock
from tools.codex_alignment.keeper_overlay import adapter
from tools.codex_alignment.evidence import digest

SECRET = 'synthetic-test-key-only-01234567890123456789'


class EvidenceTests(unittest.TestCase):
    def test_redaction_retains_unknown_structure(self):
        raw = {'instructions': 'private-canary', 'future': {'new_field': 'private-canary'},
               'access_token': 'never-store-this', 'cookie': 'cookie-value', 'max_output_tokens': 10}
        out = sanitize(SECRET, raw)
        self.assertNotIn('private-canary', json.dumps(out))
        self.assertNotIn('never-store-this', json.dumps(out))
        self.assertEqual(out['instructions'], out['future']['new_field'])
        changed = sanitize(SECRET, {**raw, 'future': {'new_field': 'different'}})
        self.assertTrue(any('future.new_field' in p for p in differences(out, changed)))
        self.assertEqual(out['max_output_tokens'], 10)

    def evidence(self):
        identity = '12345678-1234-1234-1234-123456789012'
        return http_evidence(SECRET, 'POST', '/backend-api/codex/responses',
                             [('Host', 'chatgpt.com'), ('Authorization', 'Bearer must-not-persist'),
                              ('session-id', identity), ('Content-Length', '45')],
                             {'model': 'test-model', 'prompt_cache_key': identity, 'input': []}, 45, 'test')

    def test_body_header_order_and_identity_drift_are_not_passes(self):
        baseline = self.evidence()
        self.assertEqual(compare_replay(baseline, baseline)['status'], 'pass')
        for mutate in (
            lambda e: e['body'].update(new_unknown=True),
            lambda e: e['header_order'].reverse(),
            lambda e: e['identity_checks'].update(session_matches_cache=False),
            lambda e: e['headers'].update(originator='unexpected'),
            lambda e: e['identity_formats'].update({'session-id': 'other:5'}),
        ):
            changed = copy.deepcopy(baseline)
            mutate(changed)
            self.assertEqual(compare_replay(baseline, changed)['status'], 'drift')
        self.assertEqual(compare_replay(baseline, None)['status'], 'unverified')
        self.assertNotIn('must-not-persist', json.dumps(baseline))

    def test_dynamic_compressed_size_is_visible_but_not_a_regression(self):
        baseline = self.evidence()
        changed = copy.deepcopy(baseline)
        changed['headers']['content-length'] = '99'
        changed['wire_body_bytes'] = 99
        result = compare_replay(baseline, changed)
        self.assertEqual(result['status'], 'pass')
        self.assertFalse(result['wire_size_equal'])
        self.assertTrue(result['header_diff'])

    def test_zstd_is_bounded_and_decodes_without_content_size(self):
        import zstandard
        raw = zstandard.ZstdCompressor(write_content_size=False).compress(b'{"a":1}')
        self.assertEqual(decode_body([('Content-Encoding', 'zstd')], raw), {'a': 1})
        body = {'prompt': 'x' * 100000}
        raw = zstandard.ZstdCompressor(write_content_size=False).compress(json.dumps(body).encode())
        self.assertEqual(decode_body([('Content-Encoding', 'zstd')], raw), body)
        huge = zstandard.ZstdCompressor().compress(b'x' * ((2 << 20) + 1))
        with self.assertRaises(ValueError):
            decode_body([('Content-Encoding', 'zstd')], huge)

    def test_retention_preserves_incidents_baselines_and_partial_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 100 * 86400
            atomic_json(root / 'runs/normal/report.json', {'finished_at': 0, 'status': 'pass'})
            atomic_json(root / 'runs/incident/report.json', {'finished_at': 20 * 86400, 'status': 'drift'})
            atomic_json(root / 'baselines/approved.json', {'at': 0})
            atomic_json(root / 'runs/partial/progress.json', {'at': 0})
            atomic_json(root / 'binaries/old/codex', {'irrelevant': 'x' * 10000})
            retain(root, now=now)
            self.assertFalse((root / 'runs/normal').exists())
            self.assertTrue((root / 'runs/incident/report.json').exists())
            self.assertTrue((root / 'baselines/approved.json').exists())
            self.assertTrue((root / 'runs/partial/progress.json').exists())
            self.assertLess(retain(root, now=now), 1000)
            with self.assertRaises(ValueError):
                retain(root, now=now, max_bytes=1)


class QueueTests(unittest.TestCase):
    def test_push_burst_coalesces_without_losing_changes_during_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Schedule(Path(tmp) / 'queue.json')
            now = datetime.datetime(2026, 10, 18, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
            q.observe('commit1', now)
            q.observe('commit2', now + 100)
            self.assertFalse(q.due(now + 301))
            self.assertTrue(q.due(now + 401))
            q.started('commit2', now + 401)
            q.observe('commit3', now + 402)
            q.finished('commit2')
            self.assertEqual(q.state['pending'], 'commit3')
            self.assertTrue(q.due(now + 703))

    def test_daily_backstop_and_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Schedule(Path(tmp) / 'queue.json')
            now = datetime.datetime(2026, 10, 18, 1, 0, tzinfo=datetime.timezone.utc).timestamp()
            self.assertTrue(q.due(now))
            q.started('current', now)
            q.finished('current')
            self.assertFalse(q.due(now + 60))
            self.assertTrue(q.due(now + 86400))
            first = lock(Path(tmp) / 'lock')
            self.assertIsNone(lock(Path(tmp) / 'lock'))
            first.close()

    def test_outbox_retries_only_failed_channel_and_reports_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []
            def email(text, event):
                sent.append(('email', text))
            def fail(text, event):
                raise OSError('provider secret that must not be written')
            box = Outbox(tmp, {'email': email, 'wechat': fail})
            box.observe('drift', ['body_drift'], 'drift', now=100)
            self.assertEqual(len(box.flush(now=100)), 1)
            box.observe('drift', ['body_drift'], 'drift', now=101)
            self.assertEqual(len(list(Path(tmp).glob('*.json'))), 2)  # state + one message
            box.senders['wechat'] = lambda text, event: sent.append(('wechat', text))
            self.assertEqual(box.flush(now=1000), [])
            self.assertEqual(sent, [('email', 'drift'), ('wechat', 'drift')])
            box.observe('pass', [], 'recovered', now=1001)
            box.flush(now=1001)
            self.assertEqual(sent[-2:], [('email', 'recovered'), ('wechat', 'recovered')])
            box.observe('pass', [], 'quiet', now=1002)
            box.flush(now=1002)
            self.assertEqual(len(sent), 4)

    def test_missing_channel_stays_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Outbox(tmp, {'wechat': lambda text, event: None})
            box.observe('unverified', ['login'], 'needs login', now=100)
            self.assertEqual(len(box.flush(now=100)), 1)
            self.assertNotIn('provider secret', ''.join(p.read_text() for p in Path(tmp).glob('*')))


class CaptureTests(unittest.TestCase):
    def test_transport_close_is_recovered_only_after_model_terminal_event(self):
        class Connection:
            def __init__(self, raw=b''):
                self.raw = raw
            def makefile(self, *args, **kwargs):
                return io.BytesIO(self.raw)
            def settimeout(self, _):
                pass
            def sendall(self, _):
                pass
            def close(self):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        for event, expected in [('response.completed', True), ('response.created', False)]:
            with self.subTest(event=event), tempfile.TemporaryDirectory() as tmp:
                cap = Capture('socks5h://127.0.0.1:1', SECRET, Path(tmp) / 'out', Path(tmp) / 'ca')
                client = Connection(b'POST /backend-api/codex/responses HTTP/1.1\r\nHost: chatgpt.com\r\nContent-Length: 2\r\n\r\n{}')
                class Context:
                    def wrap_socket(self, *args, **kwargs):
                        return client
                    def set_alpn_protocols(self, _):
                        pass
                class Response:
                    status = 200
                    def __init__(self, _):
                        self.sent = False
                    def begin(self):
                        pass
                    def getheader(self, name, default=''):
                        return 'text/event-stream' if name == 'Content-Type' else default
                    def getheaders(self):
                        return [('Content-Type', 'text/event-stream')]
                    def read1(self, _):
                        if self.sent:
                            raise ssl.SSLEOFError()
                        self.sent = True
                        return ('data: {"type":"' + event + '"}\n\n').encode()
                cap.tls = Context()
                with patch('tools.codex_alignment.capture.socks_connect', return_value=Connection()), \
                     patch('tools.codex_alignment.capture.ssl.create_default_context', return_value=Context()), \
                     patch('tools.codex_alignment.capture.http.client.HTTPResponse', Response):
                    cap.handle(Connection(b'CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n'))
                self.assertEqual(cap.requests[0].get('response', {}).get('complete', False), expected)
                self.assertEqual(bool(cap.errors), not expected)

    def test_tool_success_requires_reading_the_random_file_sentinel(self):
        def output(text):
            return json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}).encode()
        expected = 'ALIGNMENT_random_fixture_value'
        self.assertTrue(native_answer_matches(output(expected + '\n'), expected))
        self.assertFalse(native_answer_matches(output('OK'), expected))
        self.assertFalse(native_answer_matches(output('The tool failed'), expected))

    def test_native_replay_does_not_duplicate_content_type_case_variants(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.extend(self.headers.get_all('Content-Type'))
                self.rfile.read(int(self.headers['Content-Length']))
                body = b'{"output":[{}]}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        try:
            cfg = {'gateway_url': 'http://127.0.0.1:' + str(server.server_port),
                   'gateway_key': 'fake', 'secret': SECRET}
            result = gateway(cfg, 'test', 'basic', int(time.time() + 60), '/v1/responses', {},
                             [('content-type', 'application/json')])
            self.assertTrue(result['complete'])
        finally:
            thread.join(timeout=5)
            server.server_close()
        self.assertEqual(received, ['application/json'])

    def test_global_budget_counts_parallel_attempts_across_captures(self):
        from concurrent.futures import ThreadPoolExecutor
        budget = Budget(7)
        def attempt(_):
            try:
                budget.reserve()
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(100))), 7)
        self.assertEqual(budget.count, 7)

    def test_success_requires_terminal_response_and_rejects_late_error(self):
        def event(value):
            return b'data: ' + json.dumps(value).encode() + b'\n\n'
        good = event({'type': 'response.completed'})
        self.assertTrue(completion(200, 'text/event-stream', good, '/v1/responses')['complete'])
        for body in (b'', event({'type': 'response.created'}), good + event({'type': 'error'}),
                     event({'type': 'response.incomplete'}), b'data: not-json\n\n'):
            self.assertFalse(completion(200, 'text/event-stream', body, '/v1/responses')['complete'])
        self.assertFalse(completion(200, 'text/event-stream', b'data: [DONE]\n\n', '/v1/chat/completions')['complete'])
        chat = event({'choices': [{'finish_reason': 'stop'}]}) + b'data: [DONE]\n\n'
        self.assertTrue(completion(200, 'text/event-stream', chat, '/v1/chat/completions')['complete'])
        self.assertFalse(completion(200, 'application/json', b'{"status":"failed","output":[]}', '/v1/responses')['complete'])
        self.assertTrue(completion(200, '', good, '/backend-api/codex/responses')['complete'])
        import gzip
        self.assertTrue(completion(200, 'text/event-stream', gzip.compress(good), '/v1/responses', 'gzip')['complete'])

    def test_compaction_requires_provider_output_and_subsequent_replay(self):
        requests = [{'body': {'input': [{'type': 'compaction_trigger'}]},
                     'response': {'compaction_output': True}},
                    {'body': {'input': [{'type': 'compaction'}]}}]
        self.assertTrue(compaction_observed(requests))
        self.assertFalse(compaction_observed(requests[:1]))
        requests[0]['response']['compaction_output'] = False
        self.assertFalse(compaction_observed(requests))

    def test_inspected_response_has_explicit_completion_framing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = Capture('socks5h://127.0.0.1:1', SECRET, Path(tmp) / 'evidence', Path(tmp) / 'ca')
            client_context = ssl.create_default_context(cafile=str(cap.cert))
            upstream_context = ssl.create_default_context(cafile=str(cap.cert))
            upstream, server = socket.socketpair()
            def serve_upstream():
                with cap.tls.wrap_socket(server, server_side=True) as tls:
                    head = b''
                    while b'\r\n\r\n' not in head:
                        head += tls.recv(4096)
                    body = b'{"models":[]}'
                    tls.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' +
                                str(len(body)).encode() + b'\r\n\r\n' + body)
            thread = threading.Thread(target=serve_upstream, daemon=True)
            thread.start()
            with cap.serve() as url, patch('tools.codex_alignment.capture.socks_connect', return_value=upstream), \
                    patch('tools.codex_alignment.capture.ssl.create_default_context', return_value=upstream_context):
                from urllib.parse import urlsplit
                conn = socket.create_connection(('127.0.0.1', urlsplit(url).port), timeout=5)
                conn.sendall(b'CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n')
                self.assertIn(b'200', conn.recv(4096))
                with client_context.wrap_socket(conn, server_hostname='chatgpt.com') as tls:
                    tls.sendall(b'GET /backend-api/codex/models HTTP/1.1\r\nHost: chatgpt.com\r\n\r\n')
                    response = http.client.HTTPResponse(tls)
                    response.begin()
                    self.assertTrue(response.chunked)
                    self.assertEqual(response.read(), b'{"models":[]}')
                    response.close()
            thread.join(timeout=5)
            self.assertEqual(cap.errors, [])

    def test_hello_parser_excludes_random_and_rejects_truncation(self):
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(['http/1.1'])
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        client = ctx.wrap_bio(incoming, outgoing, server_side=False, server_hostname='chatgpt.com')
        with self.assertRaises(ssl.SSLWantReadError):
            client.do_handshake()
        raw = outgoing.read()
        hello = parse_hello(raw[5:])
        self.assertEqual(hello['extensions']['16'], ['http/1.1'])
        self.assertNotIn('random', hello)
        for end in (0, 3, 20, len(raw) - 6):
            with self.assertRaises(ValueError):
                parse_hello(raw[5:][:end])

    def test_connect_allowlist_and_socks_failure_never_fall_back(self):
        class Sock:
            def sendall(self, _):
                pass
            def recv(self, _):
                return b'\x05\xff'
            def close(self):
                pass
        with patch('tools.codex_alignment.capture.socket.create_connection', return_value=Sock()) as connect:
            with self.assertRaises(OSError):
                socks_connect('socks5h://127.0.0.1:1080', 'chatgpt.com', 443)
            self.assertEqual(connect.call_count, 1)
        with tempfile.TemporaryDirectory() as tmp:
            cap = Capture('socks5h://127.0.0.1:1', SECRET, Path(tmp) / 'evidence', Path(tmp) / 'ca')
            with cap.serve() as url, patch('tools.codex_alignment.capture.socks_connect') as connect:
                from urllib.parse import urlsplit
                with socket.create_connection(('127.0.0.1', urlsplit(url).port)) as sock:
                    sock.sendall(b'CONNECT evil.invalid:443 HTTP/1.1\r\n\r\n')
                    self.assertEqual(sock.recv(1024), b'')
                self.assertFalse(connect.called)
            self.assertEqual(cap.count, 0)

    def test_budget_is_per_http_request_and_websocket_stays_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = Capture('socks5h://127.0.0.1:1', SECRET, Path(tmp) / 'evidence', Path(tmp) / 'ca', max_requests=1)
            self.assertEqual(cap.reserve(), 1)
            with self.assertRaises(ValueError):
                cap.reserve()
            with cap.serve() as url, patch('tools.codex_alignment.capture.socks_connect') as connect:
                from urllib.parse import urlsplit
                conn = socket.create_connection(('127.0.0.1', urlsplit(url).port))
                conn.sendall(b'CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n')
                self.assertIn(b'200', conn.recv(4096))
                ctx = ssl.create_default_context(cafile=str(cap.cert))
                with ctx.wrap_socket(conn, server_hostname='chatgpt.com') as tls:
                    tls.sendall(b'GET /backend-api/codex/responses HTTP/1.1\r\nHost: chatgpt.com\r\nUpgrade: websocket\r\n\r\n')
                    self.assertIn(b'426', tls.recv(4096))
                self.assertFalse(connect.called)
            self.assertEqual(cap.websocket_refusals, 1)
            self.assertEqual(cap.errors, [])

    def test_http_parser_rejects_partial_or_ambiguous_inputs(self):
        for raw in (b'POST / HTTP/1.1\r\n', b'POST / HTTP/2\r\n\r\n',
                    b'POST / HTTP/1.1\r\n folded: yes\r\n\r\n'):
            with self.assertRaises(ValueError):
                read_head(io.BytesIO(raw))


class KeeperOverlayTests(unittest.TestCase):
    def setup_adapter(self):
        actual = {'Id': 'new-id', 'Image': 'new-image', 'Config': {'Env': [
            'SERVER_HOST=127.0.0.1', 'SERVER_PORT=8083', 'MIGRATION_RUNTIME_ADDR=127.0.0.1:18093']},
            'HostConfig': {'RestartPolicy': {'Name': 'no'}}, 'Mounts': [],
            'State': {'Status': 'running'}}
        receipt = {'app': {'container_id': 'new-id', 'image_id': 'new-image',
                           'config_sha256': digest(actual['Config']),
                           'host_config_sha256': digest(actual['HostConfig']), 'mounts_sha256': digest([]),
                           'rootfs_diffids': ['layer']}}
        class Base:
            def inspect(self, kind):
                return 'original-' + kind
            def docker_json(self, kind, identity):
                return actual if kind == 'container' else {'RootFS': {'Layers': ['layer']}}
        class RPC:
            sha = staticmethod(digest)
        return adapter(Base, RPC, receipt)(), actual

    def test_data_services_keep_original_checks_and_app_is_exactly_pinned(self):
        backend, actual = self.setup_adapter()
        self.assertEqual(backend.inspect('pg'), 'original-pg')
        self.assertEqual(backend.inspect('redis'), 'original-redis')
        self.assertEqual(backend.inspect('app')['Id'], 'new-id')
        actual['HostConfig']['RestartPolicy']['Name'] = 'always'
        with self.assertRaises(ValueError):
            backend.inspect('app')

    def test_unknown_or_oom_application_state_is_not_adopted(self):
        backend, actual = self.setup_adapter()
        actual['State']['OOMKilled'] = True
        with self.assertRaises(ValueError):
            backend.inspect('app')
        actual['State'] = {'Status': 'created'}
        with self.assertRaises(ValueError):
            backend.inspect('app')


if __name__ == '__main__':
    unittest.main()
