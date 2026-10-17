#!/usr/bin/env python3
"""Offline tests; no SSH, Docker, socket, or production operations."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

spec = importlib.util.spec_from_file_location('backup_tencent_final', pathlib.Path(__file__).with_name('backup_tencent_final.py'))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def archive(extra=None, missing=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as tar:
        names = ['app/endpoints.json', 'keeper/arm.json', 'rolling-recovery/app/data/config.yaml']
        if missing:
            names.pop()
        for name in names:
            item = tarfile.TarInfo(name)
            item.mode = 0o600
            item.size = 2
            tar.addfile(item, io.BytesIO(b'{}'))
        if extra:
            item = tarfile.TarInfo(extra)
            item.mode = 0o600
            tar.addfile(item, io.BytesIO())
    return output.getvalue()


def result(stdout=b'', stderr=b'', code=0, failure=None):
    return {'stdout': stdout, 'stderr': stderr, 'returncode': code, 'failure': failure}


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = pathlib.Path(self.temp.name).resolve()
        self.folder = self.parent / 'backup'

    def remote(self, *replies):
        return mock.Mock(side_effect=list(replies))

    def success(self):
        return self.remote(result(b'PGDMP-current-primary'),
                           result(json.dumps({'toc_verified': True, 'toc_sha256': 'a' * 64}).encode()),
                           result(archive()))

    def test_default_is_offline(self):
        with mock.patch.object(B.runpy, 'run_path', side_effect=AssertionError('no channel')), \
                mock.patch.object(B, 'backup', side_effect=AssertionError('no backup')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            B.main([])
        value = json.loads(output.getvalue())
        self.assertTrue(value['offline'])
        self.assertFalse(value['redis_live_keys_included'])
        self.assertFalse(value['remote_mutations'])

    def test_success_encrypts_all_payloads_and_verifies_persisted_bytes(self):
        remote = self.success()
        report = B.backup(self.folder, ['never-ssh'], remote=remote)
        self.assertEqual(report['status'], 'complete')
        self.assertFalse(report['redis_live_keys_included'])
        self.assertFalse(report['cross_service_atomic'])
        self.assertEqual([call.args[1] for call in remote.call_args_list], [B.DUMP, B.VERIFY_DUMP, B.FILES])
        self.assertEqual(remote.call_args_list[1].kwargs['input'], b'PGDMP-current-primary')
        key = (self.folder / 'backup.key').read_bytes()
        blob = (self.folder / 'postgres.dump.aesgcm').read_bytes()
        plain = AESGCM(key).decrypt(blob[:12], blob[12:], (B.RUN + '/postgres.dump').encode())
        self.assertEqual(plain, b'PGDMP-current-primary')
        self.assertEqual(report['files']['postgres.dump']['ciphertext_sha256'], hashlib.sha256(blob).hexdigest())
        self.assertEqual(json.loads((self.folder / 'verification.json').read_bytes()), report)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        for path in self.folder.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(b'PGDMP-current-primary', path.read_bytes())
        self.assertFalse((self.folder / 'failure.json').exists())

    def test_existing_scope_is_never_overwritten(self):
        self.folder.mkdir(mode=0o700)
        marker = self.folder / 'evidence'
        marker.write_bytes(b'old')
        remote = self.success()
        with self.assertRaises(FileExistsError):
            B.backup(self.folder, [], remote=remote)
        remote.assert_not_called()
        self.assertEqual(marker.read_bytes(), b'old')

    def test_rejects_symlink_parent(self):
        link = self.parent / 'link'
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'DIRECTORY_UNTRUSTED'):
            B.private_folder(link / 'child')
        self.assertFalse((self.parent / 'child').exists())

    def test_rejects_writable_parent(self):
        self.parent.chmod(0o777)
        try:
            with self.assertRaisesRegex(RuntimeError, 'DIRECTORY_UNTRUSTED'):
                B.private_folder(self.folder)
        finally:
            self.parent.chmod(0o700)

    def test_exclusive_file_write_preserves_original(self):
        path = self.parent / 'evidence'
        B.write(path, b'original')
        with self.assertRaises(FileExistsError):
            B.write(path, b'replacement')
        self.assertEqual(path.read_bytes(), b'original')

    def test_failed_command_encrypts_stderr_and_preserves_failure_receipt(self):
        secret = b'private SQL password=secret'
        remote = self.remote(result(b'partial database', secret, code=7))
        with self.assertRaisesRegex(RuntimeError, '^backup-failed-inspect-private-receipt$'):
            B.backup(self.folder, [], remote=remote)
        failure = json.loads((self.folder / 'failure.json').read_bytes())
        self.assertEqual(failure['failed_stage'], 'postgres.dump')
        self.assertEqual(failure['status'], 'failed')
        receipt = json.loads((self.folder / 'postgres.dump.command.json').read_bytes())
        self.assertEqual(receipt['returncode'], 7)
        self.assertEqual(receipt['stdout_sha256'], hashlib.sha256(b'partial database').hexdigest())
        for path in self.folder.iterdir():
            self.assertNotIn(secret, path.read_bytes())
        blob = (self.folder / 'postgres.dump.stderr.aesgcm').read_bytes()
        key = (self.folder / 'backup.key').read_bytes()
        self.assertEqual(AESGCM(key).decrypt(blob[:12], blob[12:],
                         (B.RUN + '/postgres.dump.stderr').encode()), secret)
        self.assertFalse((self.folder / 'verification.json').exists())
        remote.assert_called_once()

    def test_timeout_is_failure_even_with_zero_exit_code(self):
        with self.assertRaisesRegex(RuntimeError, 'backup-failed'):
            B.backup(self.folder, [], remote=self.remote(result(b'PGDMP', failure='COMMAND_TIMEOUT')))
        receipt = json.loads((self.folder / 'postgres.dump.command.json').read_bytes())
        self.assertEqual(receipt['failure'], 'COMMAND_TIMEOUT')

    def test_invalid_dump_header_stops_before_restore_or_files(self):
        remote = self.remote(result(b'not a dump'))
        with self.assertRaisesRegex(RuntimeError, 'backup-failed'):
            B.backup(self.folder, [], remote=remote)
        remote.assert_called_once()
        self.assertFalse((self.folder / 'postgres.dump.aesgcm').exists())
        self.assertEqual(json.loads((self.folder / 'failure.json').read_bytes())['failed_stage'], 'postgres.dump')

    def test_restore_verification_failure_stops(self):
        remote = self.remote(result(b'PGDMP-dump'), result(b'{}'))
        with self.assertRaisesRegex(RuntimeError, 'backup-failed'):
            B.backup(self.folder, [], remote=remote)
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(json.loads((self.folder / 'failure.json').read_bytes())['failed_stage'], 'postgres.toc')

    def test_corrupt_persisted_ciphertext_does_not_pass(self):
        original = B.write
        def corrupt(path, raw):
            original(path, b'corrupt' if path.name == 'postgres.dump.aesgcm' else raw)
        with mock.patch.object(B, 'write', side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, 'backup-failed'):
                B.backup(self.folder, [], remote=self.success())
        self.assertTrue((self.folder / 'failure.json').exists())
        self.assertFalse((self.folder / 'verification.json').exists())

    def test_archive_rejects_traversal_absolute_duplicate_and_missing(self):
        for name in ('../outside', '/absolute', 'app/endpoints.json'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                B.validate_archive(archive(name))
        with self.assertRaisesRegex(RuntimeError, 'REQUIRED_FILE_MISSING'):
            B.validate_archive(archive(missing=True))

    def test_remote_scripts_compile_and_bind_current_identity(self):
        for name in ('CHECK', 'DUMP', 'VERIFY_DUMP', 'FILES'):
            compile(getattr(B, name), name, 'exec')
        self.assertIn(B.RPC_SHA, B.CHECK)
        self.assertIn('cfg[\'pg\'][\'container_id\']', B.VERIFY_DUMP)
        self.assertIn('signature(before)==signature(after)==signature(current)', B.FILES)
        self.assertNotIn('assert ', B.CHECK + B.DUMP + B.VERIFY_DUMP + B.FILES)


class PgReadContentionTest(unittest.TestCase):
    class Refusal(Exception):
        def __init__(self, code):
            self.code = code
            super().__init__(code)

    class Clock:
        def __init__(self):
            self.now = 0.0
            self.sleeps = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    def setUp(self):
        namespace = {'require': B.require}
        exec(B.PG_READ, namespace)
        self.read = namespace['read_expected_primary']
        self.clock = self.Clock()
        self.rpc = types.SimpleNamespace(Refusal=self.Refusal)
        self.backend = mock.Mock()
        self.backend.pg_sample.return_value = {'in_recovery': False, 'system_identifier': '7626048206782038050'}

    def locks(self, failures):
        failures = iter(failures)
        @contextlib.contextmanager
        def acquire():
            error = next(failures, None)
            if error:
                raise error
            yield
        self.backend.pg_locks.side_effect = acquire

    def test_transient_busy_retries_only_lock_and_read(self):
        self.locks([self.Refusal('OPERATION_BUSY'), self.Refusal('OPERATION_BUSY')])
        value = self.read(self.backend, self.rpc, self.clock)
        self.assertFalse(value['in_recovery'])
        self.assertEqual(self.backend.pg_locks.call_count, 3)
        self.backend.pg_sample.assert_called_once()
        self.assertEqual(self.clock.sleeps, [0.05, 0.05])
        self.assertEqual(set(call[0] for call in self.backend.mock_calls), {'pg_locks', 'pg_sample'})

    def test_permanent_busy_has_deadline_and_attempt_bound(self):
        self.locks([self.Refusal('OPERATION_BUSY') for _ in range(50)])
        with self.assertRaisesRegex(self.Refusal, 'OPERATION_BUSY'):
            self.read(self.backend, self.rpc, self.clock)
        self.assertLessEqual(self.backend.pg_locks.call_count, 41)
        self.assertLessEqual(sum(self.clock.sleeps), 2.0)
        self.backend.pg_sample.assert_not_called()

    def test_nonbusy_refusal_is_immediate_fail_closed(self):
        self.locks([self.Refusal('PG_AUTHORITY_CHANGED')])
        with self.assertRaisesRegex(self.Refusal, 'PG_AUTHORITY_CHANGED'):
            self.read(self.backend, self.rpc, self.clock)
        self.backend.pg_locks.assert_called_once()
        self.assertEqual(self.clock.sleeps, [])

    def test_untyped_busy_message_is_not_retried(self):
        self.locks([RuntimeError('OPERATION_BUSY')])
        with self.assertRaisesRegex(RuntimeError, 'OPERATION_BUSY'):
            self.read(self.backend, self.rpc, self.clock)
        self.backend.pg_locks.assert_called_once()
        self.assertEqual(self.clock.sleeps, [])

    def test_failed_primary_identity_is_not_retried(self):
        self.locks([])
        self.backend.pg_sample.return_value = {'in_recovery': True, 'system_identifier': '7626048206782038050'}
        with self.assertRaisesRegex(RuntimeError, 'PG_NOT_EXPECTED_PRIMARY'):
            self.read(self.backend, self.rpc, self.clock)
        self.backend.pg_sample.assert_called_once()
        self.assertEqual(self.clock.sleeps, [])

    def test_sole_read_body_busy_can_retry_without_mutation(self):
        self.locks([])
        self.backend.pg_sample.side_effect = [self.Refusal('OPERATION_BUSY'),
                                             {'in_recovery': False, 'system_identifier': '7626048206782038050'}]
        self.assertFalse(self.read(self.backend, self.rpc, self.clock)['in_recovery'])
        self.assertEqual(self.backend.pg_sample.call_count, 2)
        self.assertEqual(self.clock.sleeps, [0.05])


class CaptureTest(unittest.TestCase):
    def local(self, code, **kwargs):
        return B.run_remote([sys.executable, '-c', code], 'unused', **kwargs)

    def test_captures_both_streams_and_input(self):
        output = self.local('import sys;data=sys.stdin.buffer.read();sys.stdout.buffer.write(data);sys.stderr.write("warning")', input=b'dump' * 100000)
        self.assertEqual(output['returncode'], 0)
        self.assertIsNone(output['failure'])
        self.assertEqual(output['stdout'], b'dump' * 100000)
        self.assertEqual(output['stderr'], b'warning')

    def test_capture_limit_terminates_process(self):
        with mock.patch.object(B, 'MAX_CAPTURE', 20):
            output = self.local('import sys;sys.stdout.buffer.write(b"x"*1000000);sys.stdout.flush()')
        self.assertEqual(output['failure'], 'COMMAND_CAPTURE_LIMIT')
        self.assertLessEqual(len(output['stdout']), 20)

    def test_timeout_terminates_process(self):
        output = self.local('import time;time.sleep(5)', timeout=0.05)
        self.assertEqual(output['failure'], 'COMMAND_TIMEOUT')
        self.assertNotEqual(output['returncode'], 0)


if __name__ == '__main__':
    unittest.main()
