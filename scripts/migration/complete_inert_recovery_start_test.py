#!/usr/bin/env python3
"""Local resolution fault injection; no Docker, SSH or production resources."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import runpy
import subprocess
import tempfile
import types
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location('inert_resolution_tested', pathlib.Path(__file__).with_name('complete_inert_recovery_start.py'))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()+b'\n'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def need(value, code):
    if not value:
        raise RuntimeError(code)


def created():
    return {'Id': M.CID, 'Image': 'sha256:'+'a'*64, 'Config': {'Env': ['PRIVATE_VALUE=preserved-only-in-private-archive']},
            'RestartCount': 0, 'State': {'Status': 'created', 'Running': False, 'Pid': 0,
            'StartedAt': '0001-01-01T00:00:00Z', 'FinishedAt': '0001-01-01T00:00:00Z',
            'Error': '', 'ExitCode': 0, 'OOMKilled': False, 'Paused': False, 'Restarting': False, 'Dead': False}}


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.env = {'payload': {'cid': M.CID}, 'need': need, 'sha': sha, 're': re, 'os': os, 'pathlib': pathlib}
        exec(M.HELPERS, self.env)

    def test_only_created_never_started_generation_is_eligible(self):
        row = created()
        self.env['never_started'](row)
        for field, value in [('Status', 'running'), ('Running', True), ('Pid', 12), ('Pid', False),
                             ('StartedAt', '2026-10-17T00:00:00Z'), ('FinishedAt', '2026-10-17T00:00:00Z'),
                             ('Error', 'start failed'), ('ExitCode', 1), ('OOMKilled', True),
                             ('Paused', True), ('Restarting', True), ('Dead', True)]:
            with self.subTest(field=field):
                changed = copy.deepcopy(row); changed['State'][field] = value
                with self.assertRaisesRegex(RuntimeError, 'not-proven-never-started'):
                    self.env['never_started'](changed)
        for value in [1, True]:
            changed = copy.deepcopy(row); changed['RestartCount'] = value
            with self.assertRaises(RuntimeError):
                self.env['never_started'](changed)

    def test_runtime_clients_detect_full_short_named_and_shim_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = pathlib.Path(directory)
            commands = [('/usr/bin/docker', 'start', M.CID),
                        ('docker', 'start', M.CID[:12]), ('docker', 'start', 'fixed-candidate-name'),
                        ('containerd-shim-runc-v2', '--id', M.CID),
                        ('runc', '--bundle=/run/docker/'+M.CID, 'create'),
                        ('docker', 'inspect', 'f'*64), ('python3', '-c', 'unrelated text '+M.CID)]
            for index, argv in enumerate(commands, 100):
                folder = proc/str(index); folder.mkdir()
                (folder/'cmdline').write_bytes(b'\0'.join(part.encode() for part in argv)+b'\0')
            found = self.env['runtime_clients'](proc, M.CID, 'fixed-candidate-name')
            self.assertEqual({item['pid'] for item in found}, set(range(100, 105)))
            self.assertTrue(all('argv_sha256' in item and 'argv' not in item for item in found))

    def test_default_is_offline_and_assembled_remote_compiles(self):
        with mock.patch.object(M.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]), 0)
        run.assert_not_called()
        self.assertTrue(json.loads(out.getvalue())['offline'])
        common = runpy.run_path(str(pathlib.Path(__file__).with_name('recover_source_guard.py')))['COMMON']
        compile(common+M.HELPERS+M.TARGET, 'inert-start-resolution', 'exec')


class ProgramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.scope = self.root/'rolling-recovery'; self.scope.mkdir()
        self.lib = self.root/'lib'; self.lib.mkdir()
        self.rpc = self.root/'rpc'; self.rpc.mkdir()
        self.proc = self.root/'proc'; self.proc.mkdir()
        (self.lib/'rolling_app_host.py').write_bytes(b'host')
        (self.lib/'rolling_app_recovery.py').write_bytes(b'core')
        self.owner = '3'*32
        self.row = created()
        self.state = {'v': 1, 'owner': self.owner, 'phase': 'START_INTENT',
                      'intent': {'id': M.INTENT_ID, 'kind': 'start', 'status': 'UNKNOWN'},
                      'new_identity': {'container_id': M.CID, 'image_id': self.row['Image'],
                                       'config_sha256': sha(canonical(self.row['Config']))}}
        self.state_raw = canonical(self.state)
        self.ledger_raw = canonical({'epoch': 14, 'immutable': 'ledger'})
        self.pin = self.scope/'new-process.json'
        (self.scope/'state.json').write_bytes(self.state_raw)
        (self.rpc/'state.json').write_bytes(self.ledger_raw)
        (self.scope/'app-control.token').write_bytes(b'private-unchanged')
        self.payload = {'cid': M.CID, 'intent_id': M.INTENT_ID, 'label': M.LABEL,
                        'host_sha': sha(b'host'), 'core_sha': sha(b'core'), 'ledger_sha': sha(self.ledger_raw)}
        self.locked = False; self.calls = []; self.observations = 0; self.mode = 'success'
        self.changed_on_sample = None
        fixture = self

        class Store:
            def __init__(self, path, uid):
                fixture.assertEqual(path, fixture.scope/'state.json')

            @contextlib.contextmanager
            def locked(self):
                fixture.assertFalse(fixture.locked); fixture.locked = True
                try:yield
                finally:fixture.locked = False

            def read(self):
                fixture.assertTrue(fixture.locked)
                return json.loads((fixture.scope/'state.json').read_bytes())

        class Recovery:
            def __init__(self, *args):
                pass

            def _observe(self, state):
                fixture.assertTrue(fixture.locked)
                fixture.observations += 1
                if fixture.changed_on_sample == fixture.observations:
                    fixture.row['State']['StartedAt'] = '2026-10-17T01:00:00Z'
                return {'old': {'state': 'running'}, 'new': {'state': fixture.row['State']['Status']}}

            def _new(self, state, candidate):
                fixture.assertEqual(state['new_identity']['container_id'], M.CID)

        def write_new(path, raw, mode=0o600):
            with path.open('xb') as out:out.write(raw)

        backend = types.SimpleNamespace(r=types.SimpleNamespace(sha=lambda obj: sha(canonical(obj))),
            _docker=lambda kind, identity: copy.deepcopy(self.row),
            _process=lambda row: {'pid': row['State']['Pid'], 'start_ticks': 99, 'started_at': row['State']['StartedAt']},
            _safety=lambda: self.assertTrue(self.locked))
        core = types.SimpleNamespace(FileStore=Store, Recovery=Recovery, NEW_NAME='fixed-candidate', canonical=lambda value: canonical(value).rstrip(b'\n'))
        host = types.SimpleNamespace(load_host=lambda: (core, backend, {'owner': self.owner}),
                                     SAFE_ENV={'PATH': '/usr/bin'}, write_new=write_new)

        def start(argv, **kwargs):
            self.assertTrue(self.locked)
            self.assertEqual(self.observations, 4)
            self.assertEqual(argv, ['/usr/bin/docker', 'start', M.CID])
            self.assertTrue((self.scope/M.LABEL/'intent.json').is_file())
            self.assertFalse(self.pin.exists())
            self.calls.append(argv)
            if self.mode == 'unknown-before':
                raise subprocess.TimeoutExpired(argv, 20, output=b'', stderr=b'original-before-error')
            self.row['State'].update(Status='running', Running=True, Pid=123, StartedAt='2026-10-17T01:00:00Z')
            if self.mode == 'unknown-after':
                raise subprocess.TimeoutExpired(argv, 20, output=M.CID.encode(), stderr=b'original-after-error')
            if self.mode == 'nonzero':
                return types.SimpleNamespace(returncode=1, stdout=b'', stderr=b'original-docker-start-rejection')
            return types.SimpleNamespace(returncode=0, stdout=M.CID.encode()+b'\n', stderr=b'')

        self.env = {'root': self.root, 'lib': self.lib, 'payload': self.payload, 'need': need,
                    'sha': sha, 'trusted': lambda path, mode: path.read_bytes(), 'canonical': canonical,
                    'write_new': write_new, 'sync': lambda path: None,
                    'load': lambda name, path: host, 'os': os, 'pathlib': pathlib, 'json': json, 're': re,
                    'time': types.SimpleNamespace(time=__import__('time').time, sleep=lambda seconds: None),
                    'subprocess': types.SimpleNamespace(run=start, TimeoutExpired=subprocess.TimeoutExpired)}
        exec(M.HELPERS, self.env)
        scan = self.env['runtime_clients']
        self.env['runtime_clients'] = lambda proc, cid, name: scan(self.proc, cid, name)

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            exec(M.TARGET, self.env)
        return json.loads(out.getvalue())

    def unchanged(self):
        self.assertEqual((self.scope/'state.json').read_bytes(), self.state_raw)
        self.assertEqual((self.rpc/'state.json').read_bytes(), self.ledger_raw)
        self.assertEqual((self.scope/'app-control.token').read_bytes(), b'private-unchanged')
        self.assertFalse(self.locked)

    def test_success_is_one_new_authorization_and_preserves_original_unknown_and_ledgers(self):
        result = self.execute()
        self.assertTrue(result['single_start_accepted'])
        self.assertFalse(result['reconciled_original_intent'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads(self.pin.read_bytes()), {'pid': 123, 'start_ticks': 99, 'started_at': '2026-10-17T01:00:00Z'})
        self.assertEqual((self.scope/M.LABEL/'rolling-state-before.json').read_bytes(), self.state_raw)
        self.unchanged()
        with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_unknown_before_start_cannot_be_repeated_even_when_still_created(self):
        self.mode = 'unknown-before'
        with self.assertRaises(subprocess.TimeoutExpired):self.execute()
        self.assertEqual(self.row['State']['Status'], 'created')
        with self.assertRaisesRegex(RuntimeError, 'already-attempted-inspect-only'):self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.scope/M.LABEL/'docker-start.stderr').read_bytes(), b'original-before-error')
        self.assertIn(b'TimeoutExpired', (self.scope/M.LABEL/'failure.traceback.txt').read_bytes())
        self.assertFalse(self.pin.exists()); self.unchanged()

    def test_unknown_after_start_keeps_evidence_and_never_resends_start(self):
        self.mode = 'unknown-after'
        with self.assertRaises(subprocess.TimeoutExpired):self.execute()
        self.assertEqual(self.row['State']['Status'], 'running')
        with self.assertRaisesRegex(RuntimeError, 'already-attempted-inspect-only'):self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.scope/M.LABEL/'docker-start.stderr').read_bytes(), b'original-after-error')
        self.assertFalse(self.pin.exists()); self.unchanged()

    def test_prior_started_or_existing_process_receipt_refuses_without_new_intent(self):
        self.row['State']['StartedAt'] = '2026-10-17T01:00:00Z'
        with self.assertRaisesRegex(RuntimeError, 'not-proven-never-started'):self.execute()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.scope/M.LABEL).exists()); self.unchanged()
        self.row['State']['StartedAt'] = '0001-01-01T00:00:00Z'
        self.pin.write_bytes(b'old process receipt')
        with self.assertRaisesRegex(RuntimeError, 'receipt-already-present'):self.execute()
        self.assertEqual(self.calls, [])

    def test_active_docker_client_prevents_new_resolution_intent(self):
        entry = self.proc/'400'; entry.mkdir()
        (entry/'cmdline').write_bytes(b'/usr/bin/docker\0start\0'+M.CID.encode()+b'\0')
        with self.assertRaisesRegex(RuntimeError, 'operation-in-flight'):self.execute()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.scope/M.LABEL).exists()); self.unchanged()

    def test_post_intent_changed_candidate_refuses_start_and_keeps_new_intent(self):
        self.changed_on_sample = 4
        with self.assertRaisesRegex(RuntimeError, 'not-proven-never-started'):self.execute()
        self.assertEqual(self.calls, [])
        self.assertTrue((self.scope/M.LABEL/'intent.json').is_file())
        self.assertTrue((self.scope/M.LABEL/'failure.traceback.txt').is_file()); self.unchanged()

    def test_nonzero_start_retains_original_docker_diagnostic_privately(self):
        self.mode = 'nonzero'
        with self.assertRaisesRegex(RuntimeError, 'inspect-private-stderr'):self.execute()
        self.assertEqual((self.scope/M.LABEL/'docker-start.stderr').read_bytes(), b'original-docker-start-rejection')
        self.assertFalse((self.scope/M.LABEL/'verification.json').exists())
        self.unchanged()


if __name__ == '__main__':
    unittest.main()
