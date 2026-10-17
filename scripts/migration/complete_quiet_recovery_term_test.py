#!/usr/bin/env python3
"""Offline TERM resolution fault injection; no SSH or Docker is executed."""
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

FOLDER = pathlib.Path(__file__).parent

def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, FOLDER/filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

M = module('quiet_term_tested', 'complete_quiet_recovery_term.py')
C = module('quiet_term_core', 'rolling_app_recovery.py')
FIX = module('quiet_term_core_fixtures', 'rolling_app_recovery_test.py')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()+b'\n'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def need(value, code):
    if not value:
        raise RuntimeError(code)


class HelpersTests(unittest.TestCase):
    def test_default_is_offline_and_remote_program_compiles(self):
        with mock.patch.object(M.subprocess, 'run') as run, mock.patch.object(M.pathlib.Path, 'read_bytes') as read:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(M.main([]), 0)
        run.assert_not_called(); read.assert_not_called()
        self.assertTrue(json.loads(out.getvalue())['offline'])
        common = runpy.run_path(str(FOLDER/'recover_source_guard.py'))['COMMON']
        compile(common+M.HELPERS+M.TARGET, 'quiet-term-resolution', 'exec')

    def test_mutator_detection_matches_ids_names_slash_names_and_ignores_standing_shim(self):
        env = {'pathlib': pathlib, 'os': os, 're': re, 'sha': sha}
        exec(M.HELPERS, env)
        with tempfile.TemporaryDirectory() as directory:
            proc = pathlib.Path(directory)
            commands = [('docker', 'kill', M.CID), ('docker', 'stop', M.CID[:12]),
                        ('docker', 'restart', 'old-app'), ('docker', 'kill', '/old-app'),
                        ('runc', '--root=/run/'+M.CID, 'delete'),
                        ('containerd-shim-runc-v2', '--id', M.CID, 'kill'),
                        ('containerd-shim-runc-v2', '--id', M.CID),
                        ('docker', 'inspect', M.CID), ('python3', 'kill', M.CID)]
            for pid, argv in enumerate(commands, 100):
                path = proc/str(pid); path.mkdir()
                (path/'cmdline').write_bytes(b'\0'.join(part.encode() for part in argv)+b'\0')
            found = env['runtime_clients'](proc, M.CID, 'old-app')
        self.assertEqual({item['pid'] for item in found['active']}, set(range(100, 106)))
        self.assertEqual([item['pid'] for item in found['standing_shims']], [106])
        self.assertTrue(all('argv_sha256' in item and 'argv' not in item for items in found.values() for item in items))


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
        self.owner = FIX.OWNER
        self.process = copy.deepcopy(M.PROCESS)
        self.row = {'Id': M.CID, 'Image': C.IMAGE, 'Config': {'Env': ['PRIVATE=archive-only']},
                    'HostConfig': {'RestartPolicy': {'Name': 'no'}}, 'State': {
                    'Running': True, 'Status': 'running', 'Pid': self.process['pid'], 'ExitCode': 0,
                    'StartedAt': self.process['started_at'], 'OOMKilled': False, 'Paused': False,
                    'Restarting': False, 'Dead': False}}
        self.old = FIX.old_app()
        self.old.update(config_sha256=sha(canonical(self.row['Config'])), process=copy.deepcopy(self.process),
                        ready=True, serving=True, draining=False)
        self.new = FIX.new_app()
        self.new.update(container_id=M.NEW_CID, state='running', ready=True, serving=True,
                        instance_id=M.NEW_INSTANCE)
        self.history = {'failed': 1, 'dropped': 3, 'logs_sha256': 'e'*64,
                        'recovery_receipt_sha256': 'f'*64, 'archive_directory': str(C.HISTORY_DIRECTORY), 'durable': True}
        nat_before = {'output': [['-A', 'OUTPUT', '-j', 'RETURN']], 'other': {'policy': 'ACCEPT'}}
        self.nat = copy.deepcopy(nat_before); self.nat['output'].insert(0, copy.deepcopy(C.REDIRECT))
        self.state = {'v': 1, 'owner': self.owner, 'phase': 'TERM_INTENT',
                      'intent': {'id': M.INTENT_ID, 'kind': 'term', 'status': 'UNKNOWN'},
                      'old_identity': C.Recovery._identity(self.old), 'new_identity': C.Recovery._identity(self.new),
                      'nat_before': nat_before, 'history': copy.deepcopy(self.history), 'completed_intents': []}
        self.state_raw = canonical(self.state); self.ledger_raw = canonical({'epoch': 14, 'pg': 'AWS', 'redis': 'AWS'})
        (self.scope/'state.json').write_bytes(self.state_raw)
        (self.rpc/'state.json').write_bytes(self.ledger_raw)
        for name in ('host-config.json', 'new-process.json', 'source-controller-closed.json', 'source-recovery-receipt.json'):
            (self.scope/name).write_bytes(canonical({'pinned': name}))
        (self.rpc/'config.json').write_bytes(canonical({'pinned': 'rpc'}))
        history_dir = self.scope/'history'; history_dir.mkdir()
        for name in ('old-runtime.json', 'old-process.json', 'dockerlogs.stdout', 'dockerlogs.stderr',
                     'logs-manifest.json', 'source-recovery-receipt.json', 'receipt.json'):
            (history_dir/name).write_bytes(canonical({'history': name}))
        self.immutable = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.payload = {'mode': 'apply', 'cid': M.CID, 'new_cid': M.NEW_CID, 'new_instance': M.NEW_INSTANCE,
                        'intent_id': M.INTENT_ID, 'label': M.LABEL, 'resolution_id': '1'*32,
                        'host_sha': sha(b'host'), 'core_sha': sha(b'core'), 'ledger_sha': sha(self.ledger_raw),
                        'state_sha': sha(self.state_raw), 'old_process': copy.deepcopy(M.PROCESS)}
        self.saved = self.scope/M.LABEL
        self.locked = False; self.observations = 0; self.runtime_reads = 0
        self.clock = 100.0; self.calls = []; self.events = []; self.mode = 'success'
        self.observation_patch = None; self.runtime_patch = None; self.before_write = None
        self.after_write = None; self.after_read = None; self.boot = 'boot-A'; self.sample_ids = None
        self.runtime = {'ready': True, 'serving': True, 'draining': False, 'active_requests': 0,
                        'active_hijacked_connections': 0, 'ingress': {'queued_requests': 0}}
        self.log_stdout = b'normal original log\n'; self.log_stderr = b''
        self.log_timeout = False; self.term_started = False
        fixture = self

        class Store:
            def __init__(self, path, uid):
                fixture.assertEqual(path, fixture.scope/'state.json'); fixture.assertEqual(uid, 0)

            @contextlib.contextmanager
            def locked(self):
                fixture.assertFalse(fixture.locked); fixture.locked = True
                try:yield
                finally:fixture.locked = False

            def read(self):
                fixture.assertTrue(fixture.locked)
                return json.loads((fixture.scope/'state.json').read_bytes())

        def external(kind):
            self.assertTrue(self.locked)
            if (self.saved/'signal.intent.json').exists() and not self.term_started:
                self.assertEqual(kind, 'TERM', 'network/process command after durable signal intent: '+kind)
            self.events.append(kind)

        def trusted(path, mode):
            raw = path.read_bytes()
            self.events.append('read:'+str(path.relative_to(self.root)))
            if self.after_read:self.after_read(path)
            return raw

        def private_read(path, **kwargs):
            self.assertEqual(kwargs, {'uid': 0, 'limit': 64*1024*1024})
            return path.read_bytes()

        def write_new(path, raw, mode=0o600):
            self.assertEqual(mode, 0o600)
            if self.before_write:self.before_write(path)
            with path.open('xb') as out:out.write(raw)
            self.events.append('durable:'+path.name)
            if self.after_write:self.after_write(path)

        def docker(kind, cid):
            external('docker-inspect')
            self.assertEqual(kind, 'container')
            if cid == M.CID:return copy.deepcopy(self.row)
            self.assertEqual(cid, M.NEW_CID); return {'Id': M.NEW_CID}

        def old_process(row):
            external('process-read'); return copy.deepcopy(self.process)

        def app(row, new=False):
            external('new-health'); self.assertTrue(new); return copy.deepcopy(self.new)

        def safety():external('authority-health')

        def nat():external('nat'); return copy.deepcopy(self.nat)

        def history():external('history'); return copy.deepcopy(self.history)

        def observe():
            external('full-observation'); self.observations += 1
            value = {'window': None, 'quiesce_cancelled': True, 'pg_authority': 'AWS', 'redis_authority': 'AWS',
                     'endpoints': copy.deepcopy(C.ENDPOINTS), 'boot_id': self.boot,
                     'sample_id': str(self.observations) if self.sample_ids is None else self.sample_ids[self.observations-1],
                     'old': copy.deepcopy(self.old), 'new': copy.deepcopy(self.new), 'nat': copy.deepcopy(self.nat)}
            if self.observation_patch:self.observation_patch(self.observations, value)
            return value

        def runtime(name):
            external('runtime-GET'); self.runtime_reads += 1; self.assertEqual(name, 'runtime.status')
            value = copy.deepcopy(self.runtime)
            if self.runtime_patch:self.runtime_patch(self.runtime_reads, value)
            return {'http_status': 200, 'body': value}

        def run(argv, **kwargs):
            if argv[:2] == ['/usr/bin/docker', 'logs']:
                external('docker-logs')
                self.assertEqual(argv, ['/usr/bin/docker', 'logs', '--timestamps', M.CID])
                if self.log_timeout:raise subprocess.TimeoutExpired(argv, 5, output=b'partial-log', stderr=b'private-log-error')
                return types.SimpleNamespace(returncode=0, stdout=self.log_stdout, stderr=self.log_stderr)
            external('TERM'); self.term_started = True
            self.assertEqual(argv, ['/usr/bin/docker', 'kill', '--signal', 'TERM', M.CID])
            self.assertEqual(self.observations, 4)
            self.assertTrue((self.saved/'signal.intent.json').is_file())
            self.assertEqual(json.loads((self.scope/'state.json').read_bytes())['intent']['status'], 'UNKNOWN')
            self.calls.append(argv)
            if self.mode == 'unknown-before':
                raise subprocess.TimeoutExpired(argv, 5, output=b'', stderr=b'private-before-error')
            if self.mode == 'nonzero':return types.SimpleNamespace(returncode=1, stdout=b'', stderr=b'private-docker-rejection')
            self.exit_old()
            if self.mode == 'unknown-after':
                raise subprocess.TimeoutExpired(argv, 5, output=M.CID.encode(), stderr=b'private-after-error')
            return types.SimpleNamespace(returncode=0, stdout=M.CID.encode()+b'\n', stderr=b'')

        def sleep(seconds):
            self.clock += seconds
            if self.mode == 'drain-exit':self.exit_old()

        core = types.SimpleNamespace(**{name: getattr(C, name) for name in ('Recovery', 'IMAGE', 'QUIET_FIELDS', 'REDIRECT', 'copy')}, FileStore=Store)
        backend = types.SimpleNamespace(r=types.SimpleNamespace(sha=lambda value: sha(canonical(value))),
                    _docker=docker, _process=old_process, _app=app, _safety=safety, _nat=nat,
                    inspect_history=history, observe=observe, read_backend=types.SimpleNamespace(app_control=runtime))
        host = types.SimpleNamespace(load_host=lambda: (core, backend, {'owner': self.owner}), private_read=private_read,
                    SAFE_ENV={'PATH': '/usr/bin'}, MAX_LOG=64*1024*1024, OLD_NAME='sub2api-migration-app-20261016')
        self.env = {'root': self.root, 'lib': self.lib, 'payload': self.payload, 'need': need,
                    'sha': sha, 'trusted': trusted, 'canonical': canonical, 'write_new': write_new,
                    'sync': lambda path: None, 'load': lambda name, path: host,
                    'os': os, 'pathlib': pathlib, 'json': json, 're': re,
                    'time': types.SimpleNamespace(time=lambda: 1760660000+self.clock, monotonic=lambda: self.clock, sleep=sleep),
                    'subprocess': types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)}
        exec(M.HELPERS, self.env)
        scan = self.env['runtime_clients']
        def clients(proc, cid, name):
            external('proc-clients'); return scan(self.proc, cid, name)
        self.env['runtime_clients'] = clients

    def exit_old(self):
        self.row['State'].update(Running=False, Status='exited', Pid=0, ExitCode=0)

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            exec(M.TARGET, self.env)
        return json.loads(out.getvalue())

    def unchanged(self, except_paths=()):
        for path, raw in self.immutable.items():
            if path not in except_paths:self.assertEqual(path.read_bytes(), raw, str(path))
        self.assertFalse(self.locked)
        self.assertEqual(json.loads((self.scope/'state.json').read_bytes())['intent']['status'], 'UNKNOWN')

    def assert_no_signal_intent(self):
        self.assertEqual(self.calls, []); self.assertFalse((self.saved/'signal.intent.json').exists())

    def test_success_has_four_fresh_samples_and_no_external_check_after_durable_intent(self):
        result = self.execute()
        self.assertTrue(result['ok']); self.assertTrue(result['signal_sent_by_resolution'])
        self.assertFalse(result['reconciled_original_intent']); self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.saved/'rolling-state-before.json').read_bytes(), self.state_raw)
        proof = json.loads((self.saved/'three-complete-quiet-samples.json').read_bytes())
        self.assertEqual(len(proof), 3); self.assertGreaterEqual(proof[-1]['tick']-proof[0]['tick'], 2)
        index = self.events.index('durable:signal.intent.json')
        self.assertEqual(self.events[index+1:index+4], ['read:rolling-recovery/state.json', 'read:rpc/state.json', 'TERM'])
        with self.assertRaisesRegex(RuntimeError, 'already-attempted-inspect-only'):self.execute()
        self.assertEqual(len(self.calls), 1); self.unchanged()

    def test_unknown_before_term_preserves_diagnostics_and_never_resends(self):
        self.mode = 'unknown-before'
        with self.assertRaises(subprocess.TimeoutExpired):self.execute()
        self.assertTrue(self.row['State']['Running'])
        with self.assertRaisesRegex(RuntimeError, 'already-attempted-inspect-only'):self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.saved/'docker-term.stderr').read_bytes(), b'private-before-error')
        self.assertIn(b'TimeoutExpired', (self.saved/'failure.traceback.txt').read_bytes()); self.unchanged()

    def test_unknown_after_term_preserves_evidence_and_never_resends(self):
        self.mode = 'unknown-after'
        with self.assertRaises(subprocess.TimeoutExpired):self.execute()
        self.assertFalse(self.row['State']['Running'])
        with self.assertRaisesRegex(RuntimeError, 'already-attempted-inspect-only'):self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.saved/'docker-term.stderr').read_bytes(), b'private-after-error'); self.unchanged()

    def test_nonzero_term_result_is_inspect_only_with_original_private_stderr(self):
        self.mode = 'nonzero'
        with self.assertRaisesRegex(RuntimeError, 'TERM-outcome-unknown'):self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.saved/'docker-term.stderr').read_bytes(), b'private-docker-rejection')
        self.assertFalse((self.saved/'verification.json').exists()); self.unchanged()

    def test_intent_fsync_failure_cannot_issue_term(self):
        def fail(path):
            if path.name == 'signal.intent.json':raise OSError('private fsync failure')
        self.after_write = fail
        with self.assertRaisesRegex(OSError, 'fsync'):self.execute()
        self.assertTrue((self.saved/'signal.intent.json').exists()); self.assertEqual(self.calls, [])
        self.assertIn(b'fsync failure', (self.saved/'failure.traceback.txt').read_bytes()); self.unchanged()

    def test_each_complete_work_category_blocks_before_signal_intent(self):
        for key in C.QUIET_FIELDS:
            with self.subTest(work=key):
                self.old['work'][key] = 1
                with self.assertRaisesRegex(RuntimeError, 'complete-old-quiet'):self.execute()
                self.assert_no_signal_intent(); self.unchanged()
                self.old['work'][key] = 0
                self.reset_attempt()

    def test_runtime_http_websocket_queue_and_ready_fail_before_intent(self):
        cases = [('active_requests', 1), ('active_hijacked_connections', 1), ('ingress', {'queued_requests': 1}), ('ready', False)]
        for key, value in cases:
            with self.subTest(runtime=key):
                previous = self.runtime[key]; self.runtime[key] = value
                with self.assertRaisesRegex(RuntimeError, 'old-runtime-not-idle-ready'):self.execute()
                self.assert_no_signal_intent(); self.runtime[key] = previous; self.reset_attempt()
        self.unchanged()

    def test_final_runtime_get_failure_happens_before_signal_intent(self):
        def fail(index, value):
            if index == 5:raise TimeoutError('private final GET diagnostic')
        self.runtime_patch = fail
        with self.assertRaisesRegex(TimeoutError, 'private final GET'):self.execute()
        self.assertEqual(self.observations, 4); self.assert_no_signal_intent()
        self.assertIn(b'private final GET', (self.saved/'failure.traceback.txt').read_bytes()); self.unchanged()

    def test_final_busy_observation_and_reused_or_changed_boot_samples_refuse_before_intent(self):
        def busy(index, value):
            if index == 4:value['old']['work']['ops_active'] = 1
        for patch in (busy, lambda n, o: o.update(boot_id='boot-B') if n == 4 else None):
            self.observation_patch = patch
            with self.assertRaises(RuntimeError):self.execute()
            self.assert_no_signal_intent(); self.reset_attempt()
        self.observation_patch = None; self.sample_ids = ['1', '2', '3', '3']
        with self.assertRaisesRegex(RuntimeError, 'final-sample-not-fresh'):self.execute()
        self.assert_no_signal_intent(); self.unchanged()

    def test_old_process_pid_ticks_and_started_at_are_pinned(self):
        for key, value in [('pid', 257640), ('start_ticks', 2596511), ('started_at', '2026-10-17T00:00:00Z')]:
            with self.subTest(field=key):
                self.process[key] = value
                with self.assertRaisesRegex(RuntimeError, 'pinned-old-process'):self.execute()
                self.assert_no_signal_intent(); self.process = copy.deepcopy(M.PROCESS); self.reset_attempt()
        self.unchanged()

    def test_new_generation_and_writer_health_are_required(self):
        for key, value in [('instance_id', 'different'), ('ops_failed', 1), ('ops_dropped', 1),
                           ('all_writer_failures', 1), ('all_writer_drops', 1), ('serving', False)]:
            with self.subTest(field=key):
                previous = self.new[key]; self.new[key] = value
                with self.assertRaises(RuntimeError):self.execute()
                self.assert_no_signal_intent(); self.new[key] = previous; self.reset_attempt()
        self.unchanged()

    def test_authority_nat_and_history_mismatch_refuse_before_intent(self):
        for patch in [lambda n, o: o.update(pg_authority='TENCENT'),
                      lambda n, o: o.update(window={'active': True}),
                      lambda n, o: o['nat']['output'].append(['unexpected'])]:
            self.observation_patch = patch
            with self.assertRaises(RuntimeError):self.execute()
            self.assert_no_signal_intent(); self.reset_attempt()
        self.observation_patch = None; self.history['logs_sha256'] = 'a'*64
        with self.assertRaisesRegex(Exception, 'HISTORY_ARCHIVE_CHANGED'):self.execute()
        self.assert_no_signal_intent(); self.unchanged()

    def test_exact_state_ledger_and_code_pins_refuse_without_resolution_directory(self):
        for field in ('state_sha', 'ledger_sha', 'host_sha', 'core_sha'):
            with self.subTest(pin=field):
                previous = self.payload[field]; self.payload[field] = '0'*64
                with self.assertRaises(RuntimeError):self.execute()
                self.assertEqual(self.calls, []); self.assertFalse(self.saved.exists()); self.payload[field] = previous
        self.unchanged()

    def test_history_file_race_is_detected_and_archival_logs_use_64mib_reader(self):
        path = self.scope/'history/dockerlogs.stdout'
        path.write_bytes(b'x'*(2*1024*1024+1)); self.immutable[path] = path.read_bytes()
        def change(index, value):
            if index == 4:path.write_bytes(b'changed after freeze')
        self.observation_patch = change
        with self.assertRaisesRegex(RuntimeError, 'pinned-files-or-history-changed'):self.execute()
        self.assert_no_signal_intent(); self.unchanged(except_paths=(path,))

    def test_post_intent_checkpoint_cas_prevents_term_without_altering_original_unknown(self):
        path = self.rpc/'state.json'
        def race(where):
            if where.name == 'signal.intent.json':path.write_bytes(b'external ledger race')
        self.after_write = race
        with self.assertRaisesRegex(RuntimeError, 'original-checkpoints-CAS-changed'):self.execute()
        self.assertTrue((self.saved/'signal.intent.json').exists()); self.assertEqual(self.calls, [])
        self.unchanged(except_paths=(path,))

    def test_pending_runtime_mutator_blocks_and_standing_shim_is_allowed(self):
        entry = self.proc/'400'; entry.mkdir()
        (entry/'cmdline').write_bytes(b'docker\0kill\0/sub2api-migration-app-20261016\0')
        with self.assertRaisesRegex(RuntimeError, 'operation-in-flight'):self.execute()
        self.assert_no_signal_intent(); self.reset_attempt()
        (entry/'cmdline').write_bytes(b'containerd-shim-runc-v2\0--id\0'+M.CID.encode()+b'\0')
        self.assertTrue(self.execute()['ok']); self.assertEqual(len(self.calls), 1); self.unchanged()

    def test_draining_or_shutdown_markers_wait_read_only_without_new_term(self):
        self.runtime['draining'] = True; self.mode = 'drain-exit'
        result = self.execute()
        self.assertTrue(result['ok']); self.assertFalse(result['signal_sent_by_resolution']); self.assert_no_signal_intent()
        self.reset_attempt(); self.row['State'].update(Running=True, Status='running', Pid=M.PROCESS['pid'])
        self.runtime['draining'] = False; self.log_stderr = b'Shutting down server...'; self.mode = 'drain-exit'
        result = self.execute()
        self.assertTrue(result['ok']); self.assert_no_signal_intent(); self.unchanged()

    def test_draining_timeout_remains_pending_without_force_kill_and_exited_needs_no_signal(self):
        self.runtime['draining'] = True
        result = self.execute()
        self.assertTrue(result['pending']); self.assertFalse(result['ok']); self.assert_no_signal_intent()
        self.assertEqual(self.clock, 130.0); self.reset_attempt(); self.exit_old()
        result = self.execute()
        self.assertTrue(result['ok']); self.assert_no_signal_intent(); self.unchanged()

    def test_logs_timeout_keeps_private_output_before_signal_intent(self):
        self.log_timeout = True
        with self.assertRaises(subprocess.TimeoutExpired):self.execute()
        self.assert_no_signal_intent()
        self.assertEqual((self.saved/'sample-01.logs.stdout').read_bytes(), b'partial-log')
        self.assertEqual((self.saved/'sample-01.logs.stderr').read_bytes(), b'private-log-error'); self.unchanged()

    def test_inspect_is_read_only_even_after_lost_term_reply(self):
        self.mode = 'unknown-after'
        with self.assertRaises(subprocess.TimeoutExpired):self.execute()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.payload['mode'] = 'inspect'
        result = self.execute()
        self.assertTrue(result['inspect_only']); self.assertEqual(result['old_mode'], 'exited')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before); self.unchanged()

    def test_inspect_reports_busy_without_creating_attempt_or_modifying_any_file(self):
        self.payload['mode'] = 'inspect'
        for busy_source in ('complete-work', 'runtime-queue'):
            with self.subTest(busy_source=busy_source):
                self.old['work']['http'] = int(busy_source == 'complete-work')
                self.runtime['ingress']['queued_requests'] = int(busy_source == 'runtime-queue')
                before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                result = self.execute()
                self.assertTrue(result['inspect_only']); self.assertEqual(result['old_mode'], 'busy')
                self.assertFalse(self.saved.exists()); self.assertEqual(self.calls, [])
                self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        self.unchanged()

    def reset_attempt(self):
        # Test-only fresh fixture authorization; production has no removal path.
        if self.saved.exists():
            for path in self.saved.iterdir():path.unlink()
            self.saved.rmdir()
        self.observations = 0; self.runtime_reads = 0; self.clock = 100.0; self.events = []; self.term_started = False


class LocalAttemptTests(unittest.TestCase):
    def test_ssh_timeout_retains_local_marker_and_prevents_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory); written = {}
            def private_json(path, value):
                with path.open('xb') as out:out.write(canonical(value))
                written[path.name] = value
            prior = {'FOLDER': folder, 'private_json': private_json}
            original_run_path = M.runpy.run_path
            def fake_run_path(path):
                if path.endswith('prepare_retry.py'):return prior
                if path.endswith('prepare_channel.py'):return {'TARGET': ['ssh', 'fake']}
                return original_run_path(path)
            timeout = subprocess.TimeoutExpired(['ssh'], 60, output=b'private ssh stdout', stderr=b'private ssh stderr')
            with mock.patch.object(M.runpy, 'run_path', side_effect=fake_run_path), mock.patch.object(M.subprocess, 'run', side_effect=timeout) as run:
                with self.assertRaisesRegex(RuntimeError, 'outcome-unknown-inspect-only'):M.main(['--apply'])
                with self.assertRaisesRegex(RuntimeError, 'existing-resolution-inspect-only'):M.main(['--apply'])
            self.assertEqual(run.call_count, 1)
            self.assertTrue((folder/(M.LABEL+'.intent.json')).is_file())
            result = written[M.LABEL+'.json']
            self.assertTrue(result['inspect_before_retry']); self.assertIn('stderr_b64', result)


if __name__ == '__main__':
    unittest.main()
