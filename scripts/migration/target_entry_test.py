"""Offline target entry boundary tests; no SSH, TLS or business request."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import struct
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('target_entry', pathlib.Path(__file__).with_name('target_entry.py'))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
TOKEN = 'PRIVATE_ENTRY_TOKEN_NOT_FOR_OUTPUT'


def status(active=False):
    return {'LoadState': 'loaded', 'ActiveState': 'active' if active else 'inactive',
            'SubState': 'running' if active else 'dead', 'MainPID': '123' if active else '0',
            'Job': '', 'FragmentPath': str(M.UNIT_PATH), 'DropInPaths': '', 'NeedDaemonReload': 'no'}


class EntryTests(unittest.TestCase):
    def test_default_is_offline_and_never_reads_token_or_calls_network(self):
        with mock.patch.object(M.subprocess, 'run', side_effect=AssertionError('process')), \
             mock.patch.object(M.socket, 'create_connection', side_effect=AssertionError('network')), \
             mock.patch.object(M.runpy, 'run_path', side_effect=AssertionError('token')), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
        report = json.loads(out.getvalue())
        self.assertTrue(report['offline'])
        self.assertFalse(report['network_access'])
        self.assertEqual(report['model_messages_sent'], 0)

    def test_generated_remote_code_compiles_and_token_only_in_stdin(self):
        for mode in ('inspect', 'install', 'start', 'verify'):
            source = M.remote_source(mode, TOKEN if mode == 'verify' else None)
            compile(source, '<fixed-entry>', 'exec')
            self.assertIn("__name__='_entry_remote'", source)
            self.assertNotIn('restart", UNIT_NAME', source)
            self.assertNotIn('reload", UNIT_NAME', source)
        with self.assertRaises(M.Refusal):
            M.remote_source('restart')

    def test_config_preserves_api_only_upstream_and_flush(self):
        self.assertEqual(M.sha(M.CONFIG.encode()), '077de5dac0181a7319c724e9131727bca6b0357e86762442f27308b8eaf56f45')
        self.assertIn('admin off', M.CONFIG)
        self.assertIn('reverse_proxy 127.0.0.1:8082', M.CONFIG)
        self.assertIn('flush_interval -1', M.CONFIG)
        self.assertNotIn('codex', M.CONFIG)

    def test_staged_artifact_change_refuses_before_certificate_or_unit_command(self):
        with mock.patch.object(M, 'trusted_file', return_value=b'changed'), \
             mock.patch.object(M, 'command') as call:
            with self.assertRaisesRegex(M.Refusal, 'ENTRY_STAGED_HASH_CHANGED'):
                M.staged()
        call.assert_not_called()

    def test_changed_imported_staging_definition_is_never_adopted(self):
        for field in ('CONFIG', 'UNIT'):
            with mock.patch.object(M, field, 'foreign'), mock.patch.object(M, 'trusted_file') as read:
                with self.assertRaisesRegex(M.Refusal, 'ENTRY_DEFINITION_CHANGED'):
                    M.staged()
                with self.assertRaisesRegex(M.Refusal, 'ENTRY_DEFINITION_CHANGED'):
                    M.remote_source('install')
                read.assert_not_called()

    def test_unit_status_rejects_override_or_pending_job(self):
        for field, value in [('DropInPaths', '/foreign'), ('Job', '12'), ('MainPID', 'bad')]:
            props = status(); props[field] = value
            raw = '\n'.join(key+'='+value for key, value in props.items()).encode()
            with mock.patch.object(M, 'command', return_value=raw), self.assertRaises(M.Refusal):
                M.unit_status()

    def test_inert_install_writes_exact_unit_without_start_or_enable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory).resolve() / M.UNIT_NAME
            missing = {**status(), 'LoadState': 'not-found', 'FragmentPath': ''}
            ready = {**status(), 'FragmentPath': str(path)}
            with mock.patch.object(M, 'UNIT_PATH', path), \
                 mock.patch.object(M.sys, 'platform', 'linux'), mock.patch.object(M.os, 'geteuid', return_value=0), \
                 mock.patch.object(M, 'staged', return_value={'pinned': True}), \
                 mock.patch.object(M, 'unit_status', side_effect=[missing, ready]), \
                 mock.patch.object(M, 'trusted_file', side_effect=lambda path, *args: path.read_bytes()), \
                 mock.patch.object(M, 'command') as command:
                report = M.remote_install()
            self.assertEqual(path.read_bytes(), M.UNIT.encode())
            self.assertFalse(report['started'])
            self.assertFalse(report['enabled'])
            command.assert_called_once_with(['/usr/bin/systemctl', 'daemon-reload'])

    def test_existing_different_unit_is_never_overwritten_or_reloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory).resolve() / M.UNIT_NAME
            path.write_bytes(b'foreign')
            with mock.patch.object(M, 'UNIT_PATH', path), \
                 mock.patch.object(M.sys, 'platform', 'linux'), mock.patch.object(M.os, 'geteuid', return_value=0), \
                 mock.patch.object(M, 'staged', return_value={}), mock.patch.object(M, 'unit_status', return_value=status()), \
                 mock.patch.object(M, 'trusted_file', return_value=b'foreign'), mock.patch.object(M, 'command') as command:
                with self.assertRaisesRegex(M.Refusal, 'ENTRY_EXISTING_UNIT_DIFFERS'):
                    M.remote_install()
            self.assertEqual(path.read_bytes(), b'foreign')
            command.assert_not_called()

    def test_start_checks_upstream_and_free_ports_before_only_fixed_start(self):
        with mock.patch.object(M.sys, 'platform', 'linux'), mock.patch.object(M.os, 'geteuid', return_value=0), \
             mock.patch.object(M, 'staged', return_value={}), mock.patch.object(M, 'installed'), \
             mock.patch.object(M, 'unit_status', side_effect=[status(), status(True)]), \
             mock.patch.object(M, 'upstream_health') as health, mock.patch.object(M, 'require_public_ports_free') as ports, \
             mock.patch.object(M, 'command') as command:
            report = M.remote_start()
        health.assert_called_once(); ports.assert_called_once()
        command.assert_called_once_with(['/usr/bin/systemctl', 'start', M.UNIT_NAME], timeout=8)
        self.assertTrue(report['started'])

    def test_live_entry_is_inspected_without_second_start(self):
        with mock.patch.object(M.sys, 'platform', 'linux'), mock.patch.object(M.os, 'geteuid', return_value=0), \
             mock.patch.object(M, 'staged', return_value={}), mock.patch.object(M, 'installed'), \
             mock.patch.object(M, 'unit_status', return_value=status(True)), \
             mock.patch.object(M, 'upstream_health'), mock.patch.object(M, 'require_public_ports_free') as ports, \
             mock.patch.object(M, 'command') as command:
            self.assertTrue(M.remote_start()['already_running'])
        ports.assert_not_called(); command.assert_not_called()

    def test_failed_upstream_prevents_listener_start(self):
        with mock.patch.object(M.sys, 'platform', 'linux'), mock.patch.object(M.os, 'geteuid', return_value=0), \
             mock.patch.object(M, 'staged', return_value={}), mock.patch.object(M, 'installed'), \
             mock.patch.object(M, 'unit_status', return_value=status()), \
             mock.patch.object(M, 'upstream_health', side_effect=M.Refusal('not-ready')), \
             mock.patch.object(M, 'command') as command:
            with self.assertRaises(M.Refusal):
                M.remote_start()
        command.assert_not_called()

    def test_any_tcp_udp_ipv4_ipv6_public_listener_blocks_start(self):
        for address in ('0.0.0.0:80', '127.0.0.1:443', '[::]:443', '*:80'):
            raw = ('tcp LISTEN 0 4096 '+address+' *:*\n').encode()
            with mock.patch.object(M, 'command', return_value=raw), self.assertRaises(M.Refusal):
                M.require_public_ports_free()
        with mock.patch.object(M, 'command', return_value=b'tcp LISTEN 0 4096 127.0.0.1:8082 *:*\n'):
            M.require_public_ports_free()

    def test_tls_uses_fixed_ip_full_default_verification_hostname_and_leaf_pin(self):
        sock = mock.Mock(); sock.getpeercert.return_value = b'leaf'
        context = mock.Mock(); context.wrap_socket.return_value = sock
        raw = mock.Mock()
        with mock.patch.object(M.ssl, 'create_default_context', return_value=context), \
             mock.patch.object(M.socket, 'create_connection', return_value=raw) as connect, \
             mock.patch.object(M, 'LEAF_SHA', M.sha(b'leaf')):
            self.assertIs(M.tls_socket(), sock)
        connect.assert_called_once_with((M.TARGET_IP, 443), timeout=4)
        context.wrap_socket.assert_called_once_with(raw, server_hostname=M.DOMAIN)
        context.set_alpn_protocols.assert_called_once_with(['http/1.1'])
        sock.close.assert_not_called()
        with mock.patch.object(M.ssl, 'create_default_context', return_value=context), \
             mock.patch.object(M.socket, 'create_connection', return_value=raw), \
             self.assertRaisesRegex(M.Refusal, 'ENTRY_TLS_LEAF_CHANGED'):
            M.tls_socket()
        sock.close.assert_called_once()

    def websocket_fixture(self, accept=True, pong=True):
        key = base64.b64encode(b'k'*16).decode()
        digest = base64.b64encode(hashlib.sha1((key+M.WS_GUID).encode()).digest()) if accept else b'foreign'
        handshake = (b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+digest+b'\r\n\r\n')
        ping = b'sub2api-entry-20261016' if pong else b'foreign'
        incoming = handshake+bytes([0x8a, len(ping)])+ping+b'\x88\x02'+struct.pack('!H', 1000)
        sock = mock.Mock(); sock.makefile.return_value = io.BytesIO(incoming)
        return sock

    def test_websocket_verifies_upgrade_ping_pong_and_normal_close_without_model_message(self):
        sock = self.websocket_fixture()
        with mock.patch.object(M, 'tls_socket', return_value=sock), mock.patch.object(M.os, 'urandom', side_effect=lambda n: b'k'*n):
            result = M.verify_websocket(TOKEN)
        writes = [call.args[0] for call in sock.sendall.call_args_list]
        self.assertIn(('Host: '+M.DOMAIN).encode(), writes[0])
        self.assertIn(('Authorization: Bearer '+TOKEN).encode(), writes[0])
        self.assertEqual([value[0] & 15 for value in writes[1:]], [9, 8])
        self.assertNotIn(b'response.create', b''.join(writes))
        self.assertEqual(result['model_messages_sent'], 0)
        self.assertEqual(result['normal_close_code'], 1000)
        self.assertNotIn(TOKEN, json.dumps(result))
        sock.close.assert_called_once()

    def test_websocket_bad_accept_or_pong_fails_without_model_frame(self):
        for accept, pong in ((False, True), (True, False)):
            sock = self.websocket_fixture(accept, pong)
            with mock.patch.object(M, 'tls_socket', return_value=sock), \
                 mock.patch.object(M.os, 'urandom', side_effect=lambda n: b'k'*n), self.assertRaises(M.Refusal):
                M.verify_websocket(TOKEN)
            self.assertNotIn(b'response.create', b''.join(call.args[0] for call in sock.sendall.call_args_list))
            sock.close.assert_called_once()

    def test_websocket_header_injection_and_data_frame_are_forbidden(self):
        with mock.patch.object(M, 'tls_socket') as connect:
            with self.assertRaises(M.Refusal):
                M.verify_websocket(TOKEN+'\r\nX-Injected: yes')
            connect.assert_not_called()
        with self.assertRaises(M.Refusal):
            M.ws_send(mock.Mock(), 1, b'no-model-message')
        with self.assertRaises(M.Refusal):
            M.ws_control(mock.Mock(), io.BytesIO(b'\x81\x04text'))


if __name__ == '__main__':
    unittest.main()
