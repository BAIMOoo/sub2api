import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import runpy
import tempfile
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('tested_source_reseed', pathlib.Path(__file__).with_name('source_reseed_ready.py'))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def need(ok, code):
    if not ok:
        raise RuntimeError(code)


class ProofTest(unittest.TestCase):
    def setUp(self):
        self.sha = lambda raw: hashlib.sha256(raw).hexdigest()
        self.v = {'need': need, 'sha': self.sha, 'time': time}
        exec(R.VALIDATION, self.v)
        self.comment = 'owned migration role'
        self.facts = {'system_identifier': '7626048206782038050', 'timeline': 1,
                      'in_recovery': False, 'data_directory': '/var/lib/postgresql/data',
                      'wal_cap_bytes': 512*1024**2,
                      'role': {'name': 'sub2api_mig_20261016_replica', 'login': False,
                               'replication': True, 'superuser': False, 'createdb': False,
                               'createrole': False, 'bypassrls': False, 'comment': self.comment},
                      'slots': []}
        self.slot = {'name': 'sub2api_mig_20261016', 'type': 'physical', 'temporary': False,
                     'active': False, 'wal_status': 'reserved', 'restart_lsn': '8/D0000000',
                     'retained_bytes': 32}

    def validate(self, facts, login=False, slot=False):
        self.v['database_proof'](facts, login, slot, self.sha(self.comment.encode()))

    def test_slot_and_login_are_independently_proven(self):
        self.validate(self.facts)
        self.facts['slots'] = [self.slot]
        self.validate(self.facts, slot=True)
        with self.assertRaisesRegex(RuntimeError, 'owned-replication-role'):
            self.validate(self.facts, login=True, slot=True)
        self.facts['role']['login'] = True
        self.validate(self.facts, login=True, slot=True)

    def test_foreign_cluster_role_or_slot_is_rejected(self):
        for path, value in [(('system_identifier',), 'other'), (('timeline',), 2),
                            (('in_recovery',), True), (('wal_cap_bytes',), -1),
                            (('role', 'comment'), 'other owner'), (('role', 'superuser'), True),
                            (('role', 'replication'), False)]:
            with self.subTest(path=path):
                facts = copy.deepcopy(self.facts)
                if len(path) == 1:
                    facts[path[0]] = value
                else:
                    facts[path[0]][path[1]] = value
                with self.assertRaises(RuntimeError):
                    self.validate(facts)

    def test_unreserved_or_unbounded_slot_is_rejected(self):
        for field, value in [('name', 'foreign'), ('type', 'logical'), ('temporary', True),
                             ('active', True), ('wal_status', 'lost'), ('restart_lsn', None),
                             ('retained_bytes', 384*1024**2), ('retained_bytes', -1)]:
            with self.subTest(field=field):
                facts = copy.deepcopy(self.facts)
                facts['slots'] = [dict(self.slot, **{field: value})]
                with self.assertRaises(RuntimeError):
                    self.validate(facts, slot=True)

    def test_guard_freshness_mode_trip_and_boot_are_required(self):
        status = {'owner': R.OWNER, 'run': '20261016', 'boot_id': 'boot', 'checked_at': 100,
                  'healthy': True, 'pg_wal_protected': True, 'tripped': False,
                  'recovering': False, 'sample': {'mode': 'RUNNING_BOUNDED'}}
        self.v['guard_proof'](status, 100.5, R.OWNER, 'boot', 'RUNNING_BOUNDED')
        for field, value in [('checked_at', 98), ('checked_at', 101), ('boot_id', 'old'),
                             ('healthy', False), ('tripped', True), ('recovering', True),
                             ('sample', {'mode': 'OWNED_REPLICA_FENCED'})]:
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError, 'fresh-owned-guard'):
                    self.v['guard_proof'](dict(status, **{field: value}), 100.5, R.OWNER, 'boot', 'RUNNING_BOUNDED')

    def test_target_proof_rejects_stale_unstopped_or_changed_ledger(self):
        proof = {'ok': True, 'owner': R.OWNER, 'checked_at': 100,
                 'ledger_sha256': R.TARGET_LEDGER_SHA, 'epoch': 14,
                 'pg_id': 'ac9f494b2fd0ec87257a9a7308a6ad7b270051d1dc4da33b33fea9af2885418c',
                 'pg_clean_stopped': True, 'source_endpoints': True}
        self.v['fresh_target'](proof, 101, R.OWNER, R.TARGET_LEDGER_SHA)
        for field, value in [('checked_at', 69), ('checked_at', 102), ('pg_clean_stopped', False),
                             ('ledger_sha256', 'changed'), ('epoch', 15), ('source_endpoints', False)]:
            with self.subTest(field=field):
                with self.assertRaises(RuntimeError):
                    self.v['fresh_target'](dict(proof, **{field: value}), 101, R.OWNER, R.TARGET_LEDGER_SHA)

    def test_source_history_is_never_reset_or_rebased(self):
        state = {'v': 1, 'run': '20261016', 'owner': R.OWNER, 'attempt': 3,
                 'phase': 'SOURCE_DATA', 'pg': 'AWS', 'redis': 'AWS', 'window': None,
                 'deadline_breached': False, 'route_fenced': True,
                 'pause_seconds': 14.656846920028329,
                 'budget_cohort': {'id': '7b54d1b6384a4d859436856bbbce4dcb',
                                  'record_sha256': 'd7339281e53fc4fb84cd9ea92b960860390baa157402b0b8da830512b2098b7e'}}
        self.v['source_boundary'](state, R.OWNER)
        for field, value in [('pause_seconds', 0), ('attempt', 4), ('window', {}),
                             ('pg', 'TENCENT'), ('budget_cohort', {})]:
            with self.subTest(field=field):
                with self.assertRaises(RuntimeError):
                    self.v['source_boundary'](dict(state, **{field: value}), R.OWNER)


class IntentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.saved = pathlib.Path(self.temp.name)
        self.v = {'time': time, 'sha': lambda raw: hashlib.sha256(raw).hexdigest(),
                  'need': need, 'canonical': lambda value: json.dumps(value).encode()}

        def write_new(path, value):
            with path.open('xb') as out:
                out.write(value)

        self.v['write_new'] = write_new
        exec(R.VALIDATION, self.v)

    def test_unknown_mutation_is_not_replayed(self):
        invoke = mock.Mock(side_effect=RuntimeError('unknown'))
        inspect = mock.Mock()
        step = self.v['exclusive_sql_step']
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            step(self.saved, 'slot', b'create', invoke, inspect, mock.Mock())
        with self.assertRaises(FileExistsError):
            step(self.saved, 'slot', b'create', invoke, inspect, mock.Mock())
        invoke.assert_called_once_with(b'create')
        inspect.assert_not_called()
        self.assertFalse((self.saved/'slot.verified.json').exists())

    def test_failed_readback_keeps_intent_and_blocks_second_mutation(self):
        invoke = mock.Mock()
        inspect = mock.Mock(return_value={'login': False})
        verify = mock.Mock(side_effect=RuntimeError('readback mismatch'))
        step = self.v['exclusive_sql_step']
        with self.assertRaisesRegex(RuntimeError, 'readback mismatch'):
            step(self.saved, 'login', b'login', invoke, inspect, verify)
        with self.assertRaises(FileExistsError):
            step(self.saved, 'login', b'login', invoke, inspect, verify)
        invoke.assert_called_once()
        inspect.assert_called_once()
        self.assertFalse((self.saved/'login.verified.json').exists())

    def test_slot_receipt_precedes_independent_login_intent(self):
        events = []

        def login(query):
            self.assertTrue((self.saved/'slot.verified.json').is_file())
            events.append('login')

        self.v['exclusive_sql_step'](self.saved, 'slot', b'create', lambda q: events.append('slot'),
                                     lambda: {'slot': 'reserved'}, lambda f: need(f['slot']=='reserved', 'bad'))
        self.v['exclusive_sql_step'](self.saved, 'login', b'login', login,
                                     lambda: {'login': True}, lambda f: need(f['login'], 'bad'))
        self.assertEqual(events, ['slot', 'login'])
        self.assertTrue((self.saved/'login.verified.json').is_file())


class CoordinatorTest(unittest.TestCase):
    def test_default_is_offline_and_never_runs_ssh(self):
        with mock.patch.object(R.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()) as out:
            R.main([])
        run.assert_not_called()
        self.assertTrue(json.loads(out.getvalue())['offline'])

    def test_remote_scripts_compile_with_shared_common(self):
        common = runpy.run_path(str(pathlib.Path(__file__).with_name('recover_source_guard.py')))['COMMON']
        for script in (R.TARGET_READ, R.SOURCE, R.TARGET_START):
            compile(common+R.VALIDATION+script, 'remote-source-reseed-review', 'exec')

    def test_source_proof_is_immediately_followed_by_backup(self):
        payload, calls = {}, []

        def remote(label, code, host, timeout):
            calls.append((label, host))
            if label == 'target-prepared':
                return {'ok': True}
            if label == 'source-ready':
                self.assertEqual(payload['target'], {'ok': True})
                return {'checked_at': 100}
            self.assertEqual(payload['source'], {'checked_at': 100})
            return {'ok': True, 'backup': True}

        result = R.coordinate(remote, payload, 'source-host', 'target-host', clock=lambda: 101)
        self.assertTrue(result['backup'])
        self.assertEqual(calls, [('target-prepared', 'target-host'), ('source-ready', 'source-host'),
                                 ('target-backup', 'target-host')])

    def test_stale_or_future_source_proof_never_starts_backup(self):
        for now in (116, 99):
            calls = []

            def remote(label, code, host, timeout):
                calls.append(label)
                return {'checked_at': 100}

            with self.subTest(now=now), self.assertRaisesRegex(RuntimeError, 'source-proof-aged'):
                R.coordinate(remote, {}, 'source', 'target', clock=lambda: now)
            self.assertEqual(calls, ['target-prepared', 'source-ready'])


if __name__ == '__main__':
    unittest.main()
