"""Local fault tests for cohort authorization; no remote or lifecycle effects."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import runpy
import subprocess
import tempfile
import types
import unittest
from unittest import mock


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


R = module('tested_recovery_cohort', 'authorize_recovery_cohort.py')
E = module('recovery_cohort_accounting', 'durable_runner.py')
COMMON = runpy.run_path(str(pathlib.Path(__file__).with_name('recover_source_guard.py')))['COMMON']
SHA = lambda raw: hashlib.sha256(raw).hexdigest()
CANONICAL = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()+b'\n'


def need(ok, code):
    if not ok:
        raise RuntimeError(code)


def proof():
    return {'v': 1, 'run': '20261016', 'owner': R.OWNER, 'checked_at': 100,
            'old_generation_clean_exit': True, 'old_generation_all_connections_gone': True,
            'new_generation_born_after_closed_window': True, 'new_generation_idle': True,
            'new_generation_ready': True, 'aws_authority': True,
            'ledger_sha256': R.LEDGER_SHA, 'ledger_epoch': 14,
            'old_instance_id': 'prior-generation', 'new_instance_id': 'new-generation'}


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.ns = {'need': need, 'payload': {'owner': R.OWNER, 'ledger_sha': R.LEDGER_SHA}}
        exec(R.VALIDATION, self.ns)

    def test_fresh_complete_generation_proof_is_accepted(self):
        self.ns['fresh_proof'](proof(), 101)

    def test_old_unretired_busy_unready_or_unconfirmed_authority_refuses(self):
        for key in ('old_generation_clean_exit', 'old_generation_all_connections_gone',
                    'new_generation_born_after_closed_window', 'new_generation_idle',
                    'new_generation_ready', 'aws_authority'):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.ns['fresh_proof'](dict(proof(), **{key: False}), 101)

    def test_stale_future_nonfinite_or_boolean_time_refuses(self):
        for checked in (79, 102, float('nan'), float('inf'), True, '100'):
            with self.subTest(checked=checked), self.assertRaises(RuntimeError):
                self.ns['fresh_proof'](dict(proof(), checked_at=checked), 101)

    def test_owner_ledger_generation_and_run_must_match(self):
        for key, value in [('owner', '0'*32), ('run', 'other'), ('ledger_sha256', '0'*64),
                           ('ledger_epoch', 15), ('new_instance_id', 'prior-generation'),
                           ('new_instance_id', ''), ('new_instance_id', None)]:
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                self.ns['fresh_proof'](dict(proof(), **{key: value}), 101)

    def test_offline_default_does_not_read_files_or_run_ssh(self):
        with mock.patch.object(R.subprocess, 'run', side_effect=AssertionError('remote')), \
                mock.patch.object(R.pathlib.Path, 'read_bytes', side_effect=AssertionError('file')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            R.main([])
        self.assertTrue(json.loads(out.getvalue())['offline'])

    def test_remote_scripts_compile_and_disable_bytecode_before_imports(self):
        for code in (R.TARGET, R.SOURCE):
            full = COMMON+R.VALIDATION+code
            compile(full, 'recovery-cohort-remote-test', 'exec')
            self.assertLess(full.index('sys.dont_write_bytecode=True'), full.index('=load('))

    def test_remote_unknown_keeps_intents_and_second_apply_never_replays(self):
        prior = runpy.run_path(str(pathlib.Path(__file__).with_name('prepare_retry.py')))
        channel = runpy.run_path(str(pathlib.Path(__file__).with_name('prepare_channel.py')))
        core = runpy.run_path(str(pathlib.Path(__file__).with_name('rolling_app_recovery.py')))
        with tempfile.TemporaryDirectory() as temporary:
            prior['FOLDER'] = pathlib.Path(temporary)
            mappings = {'prepare_retry.py': prior, 'prepare_channel.py': channel,
                        'rolling_app_recovery.py': core, 'recover_source_guard.py': {'COMMON': COMMON}}
            successful_read = subprocess.CompletedProcess([], 0, stdout=json.dumps(proof()).encode(), stderr=b'')
            run = mock.Mock(side_effect=[successful_read, subprocess.TimeoutExpired('remote-source', 45)])
            # This already-executed tool keeps its old production pin. Inject
            # the local fixture pin only to exercise transport unknown handling.
            current_pin = R.hashlib.sha256(pathlib.Path(__file__).with_name('target_rpc.py').read_bytes()).hexdigest()
            with mock.patch.object(R, 'RPC_SHA', current_pin), \
                    mock.patch.object(R.runpy, 'run_path', side_effect=lambda path: mappings[pathlib.Path(path).name]), \
                    mock.patch.object(R.subprocess, 'run', run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    R.main(['--apply'])
                self.assertEqual(run.call_count, 2)
                self.assertTrue((prior['FOLDER']/'recovery-cohort-source-20261017.intent.json').is_file())
                with self.assertRaisesRegex(RuntimeError, 'existing-cohort-result-inspect-only'):
                    R.main(['--apply'])
                self.assertEqual(run.call_count, 2)


class AuthorizationTests(unittest.TestCase):
    @contextlib.contextmanager
    def environment(self, *, stale_at_lock=False, race_at_lock=False, unknown_write=False,
                    previous_pause=7.082038551568985, irreversible=None):
        """Run actual SOURCE with real intents, hash chain, accounting and CAS.

        Only host identity reads and the store adapter are injected. Every
        write stays under a temporary directory and no lifecycle API exists.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            scope, lib = root/'runner', root/'lib'
            scope.mkdir(mode=0o700); lib.mkdir(mode=0o700)
            directory = scope/'budget-cohorts'; directory.mkdir(mode=0o700)
            old_id, new_id = 'a'*32, 'b'*32
            base = {'v': 1, 'run': '20261016', 'owner': R.OWNER, 'attempt': 1,
                    'phase': 'SOURCE_DATA', 'pg': 'AWS', 'redis': 'AWS', 'window': None,
                    'pause_seconds': previous_pause, 'route_fenced': True, 'deadline_breached': False}
            prior_record = {'v': 1, 'run': '20261016', 'owner': R.OWNER, 'id': old_id,
                            'previous_state': base, 'previous_state_sha256': E.digest(base),
                            'proof_sha256': '1'*64}
            prior_raw = CANONICAL(prior_record)
            (directory/(old_id+'.json')).write_bytes(prior_raw)
            state = dict(base, attempt=3, pause_seconds=14.656846920028329,
                         quiet_instance_id='prior-generation',
                         budget_cohort={'id': old_id, 'record_sha256': SHA(prior_raw)})
            original_raw = CANONICAL(state)
            (scope/'state.json').write_bytes(original_raw)
            frozen = {'transport-state.json': b'{"transport":"original"}\n',
                      'source-operations.json': b'{"operations":"original"}\n'}
            for name, raw in frozen.items():
                (scope/name).write_bytes(raw)
            rpc_raw = b'pinned-rpc-fixture'
            (lib/'target_rpc.py').write_bytes(rpc_raw)
            payload = {'owner': R.OWNER, 'state_sha': SHA(original_raw), 'rpc_sha': SHA(rpc_raw),
                       'ledger_sha': R.LEDGER_SHA, 'proof': proof(), 'cohort_id': new_id}
            clock = types.SimpleNamespace(value=101)
            writes, events = [], []

            def accounting(value):
                return E.budget_accounting(value, lambda ref: (directory/(ref['id']+'.json')).read_bytes())

            class Source:
                def _assert_monitors_retired(self):
                    events.append('monitors-verified')

                def _guard(self):
                    return {'healthy': True, 'tripped': False, 'recovering': False,
                            'pg_wal_protected': True}

                def _resource(self, kind):
                    return {'State': {'Pid': {'app': 0, 'postgres': 972839, 'redis': 972827}[kind],
                                     'Running': kind != 'app', 'OOMKilled': False}}

                def _ledger(self):
                    operations = {'app': {'request': {'action': 'app_stop'}, 'phase': 'complete',
                                          'steps': {'sigterm': 'accepted'}}}
                    if irreversible:
                        operations['unknown'] = {'request': {'action': irreversible}, 'phase': 'intent', 'steps': {}}
                    return {'operations': operations}

                def __getattr__(self, name):
                    raise AssertionError('unexpected lifecycle operation: '+name)

            class Store:
                locked = False

                def read(self, name):
                    need(name == 'state', 'unexpected-store-read')
                    return json.loads((scope/'state.json').read_bytes())

                @contextlib.contextmanager
                def lock(self):
                    if stale_at_lock:
                        clock.value = 121
                    if race_at_lock:
                        (scope/'state.json').write_bytes(original_raw+b' ')
                    self.locked = True
                    try:
                        yield
                    finally:
                        self.locked = False

                def _write(self, name, value):
                    need(name == 'state' and self.locked, 'unlocked-write')
                    record_raw = (directory/(new_id+'.json')).read_bytes()
                    drain_raw = (scope/'recovery-cohort-20261017'/'drain-proof.json').read_bytes()
                    record = json.loads(record_raw)
                    need(SHA(record_raw) == value['budget_cohort']['record_sha256'], 'record-not-durable')
                    need(SHA(drain_raw) == record['proof_sha256'], 'proof-not-durable')
                    need((scope/'recovery-cohort-20261017'/'previous-state.json').read_bytes() == original_raw,
                         'original-history-not-durable')
                    accounting(value)
                    writes.append(copy.deepcopy(value))
                    (scope/'state.json').write_bytes(CANONICAL(value))
                    if unknown_write:
                        raise OSError('simulated lost write result')

            backend = types.SimpleNamespace(store=Store(), source_ops=Source())
            service = types.SimpleNamespace(B=types.SimpleNamespace(RunnerBackend=lambda: backend),
                E=types.SimpleNamespace(budget_accounting=accounting, digest=E.digest),
                S=types.SimpleNamespace(trusted_parent=lambda path, **kw: need(path == directory, 'unexpected-path')),
                execution_lock=contextlib.nullcontext, selftest_armed=lambda owner: False,
                verify_entry=lambda: events.append('entry-verified'), read_gates=lambda owner: None)
            guard = types.SimpleNamespace(LocalBackend=lambda: types.SimpleNamespace(main_status=lambda: {'quiescent': True}))

            def load(name, path):
                if name == 'cohort_source_service':
                    return service
                if name == 'cohort_guard':
                    return guard
                raise AssertionError('unexpected import')

            ns = {'payload': payload, 'root': root, 'lib': lib, 'load': load, 'json': json,
                  'os': os, 're': __import__('re'), 'sha': SHA, 'need': need,
                  'time': types.SimpleNamespace(time=lambda: clock.value),
                  'trusted': lambda path, mode: path.read_bytes()}
            functions = [node for node in ast.parse(COMMON).body if isinstance(node, ast.FunctionDef) and
                         node.name in ('canonical', 'sync', 'write_new')]
            exec(compile(ast.Module(body=functions, type_ignores=[]), 'real-durable-helpers', 'exec'), ns)
            exec(R.VALIDATION, ns)
            fixture = types.SimpleNamespace(ns=ns, scope=scope, directory=directory, new_id=new_id,
                old_id=old_id, prior_raw=prior_raw, state=state, original_raw=original_raw,
                frozen=frozen, writes=writes, accounting=accounting, payload=payload, clock=clock,
                output=io.StringIO())
            with contextlib.redirect_stdout(fixture.output):
                yield fixture

    def test_real_authorizer_preserves_lifetime_history_and_both_prior_records(self):
        with self.environment() as f:
            old_budget = f.accounting(f.state)
            exec(R.SOURCE, f.ns)
            self.assertEqual(len(f.writes), 1)
            updated = f.writes[0]
            self.assertEqual({key: value for key, value in updated.items() if key != 'budget_cohort'},
                             {key: value for key, value in f.state.items() if key != 'budget_cohort'})
            current = f.accounting(updated)
            self.assertEqual(current['total_pause_seconds'], 14.656846920028329)
            self.assertEqual(current['max_cohort_pause_seconds'], old_budget['max_cohort_pause_seconds'])
            self.assertEqual(current['cohort_pause_seconds'], 0)
            self.assertEqual((f.directory/(f.old_id+'.json')).read_bytes(), f.prior_raw)
            record = json.loads((f.directory/(f.new_id+'.json')).read_bytes())
            self.assertEqual(record['previous_state'], f.state)
            self.assertEqual(record['previous_state_sha256'], E.digest(f.state))
            self.assertEqual(updated['quiet_instance_id'], 'prior-generation')
            self.assertTrue(json.loads(f.output.getvalue())['lifetime_pause_seconds_preserved'])
            for name, raw in f.frozen.items():
                self.assertEqual((f.scope/name).read_bytes(), raw)

    def test_freshness_is_checked_again_under_state_CAS_lock(self):
        with self.environment(stale_at_lock=True) as f:
            with self.assertRaisesRegex(RuntimeError, 'fresh-drained-generation-proof'):
                exec(R.SOURCE, f.ns)
            self.assertEqual(f.writes, [])
            self.assertEqual((f.scope/'state.json').read_bytes(), f.original_raw)

    def test_state_CAS_race_does_not_activate_new_reference(self):
        with self.environment(race_at_lock=True) as f:
            with self.assertRaisesRegex(RuntimeError, 'source-state-CAS-changed'):
                exec(R.SOURCE, f.ns)
            self.assertEqual(f.writes, [])
            self.assertEqual((f.scope/'state.json').read_bytes(), f.original_raw+b' ')

    def test_unknown_checkpoint_write_keeps_durable_evidence_and_never_replays(self):
        with self.environment(unknown_write=True) as f:
            with self.assertRaisesRegex(OSError, 'lost write result'):
                exec(R.SOURCE, f.ns)
            self.assertEqual(len(f.writes), 1)
            updated_raw = (f.scope/'state.json').read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'exact-recovered-source-boundary'):
                exec(R.SOURCE, f.ns)
            self.assertEqual(len(f.writes), 1)
            self.assertEqual((f.scope/'state.json').read_bytes(), updated_raw)
            self.assertTrue((f.directory/(f.new_id+'.json')).is_file())
            self.assertFalse((f.scope/'recovery-cohort-20261017'/'verification.json').exists())

    def test_stale_proof_or_same_generation_never_creates_new_record(self):
        for change in ({'checked_at': 80}, {'old_instance_id': 'different-prior-generation'},
                       {'new_generation_born_after_closed_window': False}):
            with self.subTest(change=change), self.environment() as f:
                f.payload['proof'].update(change)
                with self.assertRaises(RuntimeError):
                    exec(R.SOURCE, f.ns)
                self.assertEqual(f.writes, [])
                self.assertFalse((f.directory/(f.new_id+'.json')).exists())

    def test_previously_breached_cohort_is_not_erased_by_a_new_record(self):
        with self.environment(previous_pause=2) as f:
            self.assertGreater(f.accounting(f.state)['max_cohort_pause_seconds'], 10)
            with self.assertRaisesRegex(E.Refusal, 'budget-cohort-prior-deadline-breached'):
                exec(R.SOURCE, f.ns)
            self.assertEqual(f.writes, [])
            self.assertEqual((f.scope/'state.json').read_bytes(), f.original_raw)


if __name__ == '__main__':
    unittest.main()
