#!/usr/bin/env python3
"""Offline safety/failure checks for the single-setting AWS exit correction."""
import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('independent_recharge_link',
    pathlib.Path(__file__).with_name('independent_recharge_link.py'))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def primary():
    return {'system_identifier': M.SYSTEM_ID, 'pg_wal_timeline': 2, 'primary': True,
            'pg_authority': 'TENCENT', 'redis_authority': 'TENCENT',
            'persistent_endpoints': {'v': 1, 'postgres': 'target', 'redis': 'target'},
            'app_ready': True, 'background_ready': True, 'instance_id': 'same-instance'}


def row():
    return {'id': 41, 'key': M.KEY, 'value': M.OLD_PREFIX + '/recharge?private=a%2Fb#section',
            'xmin': '100', 'updated_at': '2026-10-17T12:00:00+00:00'}


class FakeBackend:
    def __init__(self, folder):
        self.folder, self.current, self.proof = folder, row(), primary()
        self.reads = self.writes = self.preflights = 0
        self.before_intent_race = self.cas_race = self.lost_response = self.no_write = False
        self.readback_unavailable = self.bad_write_response = self.after_runtime_change = False

    def preflight(self):
        self.preflights += 1
        result = copy.deepcopy(self.proof)
        if self.after_runtime_change and self.writes:
            result['instance_id'] = 'different-instance'
        return result

    def read(self):
        self.reads += 1
        if self.before_intent_race and self.reads == 2:
            self.current['xmin'] = '101'
        if self.readback_unavailable and self.writes:
            raise RuntimeError('private-secret-never-report')
        return copy.deepcopy(self.current)

    def update_once(self, before, replacement):
        report = json.loads((self.folder / 'report.json').read_text())
        intent = json.loads((self.folder / 'intent.json').read_text())
        assert report['mutation_attempted'] and report['write_intent_durable']
        assert intent['expected'] == before and intent['replacement'] == replacement
        assert json.loads((self.folder / 'before.json').read_text()) == before
        self.writes += 1
        if self.cas_race:
            self.current['xmin'] = '102'
            self.current['value'] = 'https://administrator.example/new-value'
            raise RuntimeError('private-secret-never-report')
        if not self.no_write:
            self.current.update(value=replacement, xmin='103', updated_at='2026-10-17T13:00:00+00:00')
        if self.lost_response:
            raise RuntimeError('private-secret-never-report')
        changed = copy.deepcopy(self.current)
        if self.bad_write_response:
            changed['xmin'] = '104'
        return changed


class RechargeLinkTests(unittest.TestCase):
    def execute(self, configure=None):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            folder = pathlib.Path(temporary).resolve()
            folder.chmod(0o700)
            backend = FakeBackend(folder)
            if configure:
                configure(backend)
            result = M.transaction(backend, folder)
            saved = {p.name: json.loads(p.read_text()) for p in folder.iterdir()}
            for p in folder.iterdir():
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('private-secret-never-report', json.dumps(saved))
            return result, backend, saved

    def test_default_offline_without_network_or_library_reads(self):
        with mock.patch.object(M.subprocess, 'run', side_effect=AssertionError('network')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(M.main([]), 0)
        self.assertTrue(json.loads(output.getvalue())['offline'])

    def test_suffix_is_exactly_preserved_including_empty_delimiters(self):
        for suffix in ('', '/', '/pay', '?', '#', '/?#', '?path=%2F&x=a+b#片段',
                       '/https://13.231.143.136:8081?q=13.231.143.136'):
            self.assertEqual(M.transform(M.OLD_PREFIX + suffix), M.NEW_PREFIX + suffix)

    def test_foreign_userinfo_ports_scheme_and_control_characters_refused(self):
        for value in ('https://13.231.143.136:8081/', 'http://other.example:8081/',
                      'http://13.231.143.136/', 'http://13.231.143.136:80810/',
                      'http://user@13.231.143.136:8081/', M.OLD_PREFIX + '@evil.example/',
                      M.OLD_PREFIX + '\n/pay', M.OLD_PREFIX + '/ pay', None):
            with self.assertRaises(RuntimeError):
                M.transform(value)

    def test_one_update_backup_durable_intent_and_sanitized_report(self):
        result, backend, saved = self.execute()
        self.assertTrue(result['ok'])
        self.assertEqual(backend.writes, 1)
        self.assertEqual(set(saved), {'report.json', 'before.json', 'intent.json', 'after.json'})
        self.assertEqual(saved['before.json'], row())
        self.assertEqual(saved['after.json']['value'], M.transform(row()['value']))
        self.assertNotIn('private=a', json.dumps(result))
        self.assertFalse(result['app_restart_performed'])
        self.assertFalse(result['notification_sent'])

    def test_wrong_primary_timeline_authority_endpoints_fail_before_reads(self):
        for change in ({'system_identifier': 'wrong'}, {'pg_wal_timeline': 1}, {'primary': False},
                       {'pg_authority': 'AWS'}, {'redis_authority': 'AWS'}, {'app_ready': False},
                       {'persistent_endpoints': {'v': 1, 'postgres': 'source', 'redis': 'target'}}):
            result, backend, saved = self.execute(lambda b: b.proof.update(change))
            self.assertFalse(result['ok'])
            self.assertEqual((backend.reads, backend.writes), (0, 0))
            self.assertEqual(set(saved), {'report.json'})

    def test_already_new_url_refuses_without_intent(self):
        result, backend, saved = self.execute(lambda b: b.current.update(value=M.NEW_PREFIX))
        self.assertFalse(result['ok'])
        self.assertEqual(backend.writes, 0)
        self.assertNotIn('intent.json', saved)

    def test_admin_change_before_intent_is_not_overwritten(self):
        result, backend, saved = self.execute(lambda b: setattr(b, 'before_intent_race', True))
        self.assertFalse(result['ok'])
        self.assertEqual(backend.writes, 0)
        self.assertNotIn('intent.json', saved)

    def test_cas_race_retains_administrative_change_without_retry(self):
        result, backend, _ = self.execute(lambda b: setattr(b, 'cas_race', True))
        self.assertFalse(result['ok'])
        self.assertEqual(backend.writes, 1)
        self.assertEqual(backend.current['value'], 'https://administrator.example/new-value')

    def test_lost_response_reconciles_one_write_with_readback(self):
        result, backend, _ = self.execute(lambda b: setattr(b, 'lost_response', True))
        self.assertTrue(result['ok'])
        self.assertTrue(result['write_response_unavailable'])
        self.assertEqual(backend.writes, 1)

    def test_lost_response_without_commit_is_not_retried(self):
        def configure(b):
            b.lost_response = b.no_write = True
        result, backend, _ = self.execute(configure)
        self.assertFalse(result['ok'])
        self.assertEqual(backend.writes, 1)

    def test_readback_failure_response_mismatch_and_runtime_change_refuse_success(self):
        for field in ('readback_unavailable', 'bad_write_response', 'after_runtime_change'):
            result, backend, _ = self.execute(lambda b: setattr(b, field, True))
            self.assertFalse(result['ok'])
            self.assertEqual(backend.writes, 1)

    def test_sql_uses_copy_not_value_literals_and_atomic_identity_cas(self):
        before = row()
        before['value'] += "&quote='\\."
        sql = M.update_sql(before, M.transform(before['value']))
        self.assertNotIn(before['value'], sql)
        self.assertNotIn(M.transform(before['value']), sql)
        self.assertIn('COPY recharge_expected FROM STDIN;', sql)
        self.assertIn('s.xmin::text=e.xmin_text', sql)
        self.assertIn('s.value=convert_from', sql)
        self.assertIn('affected <> 1', sql)
        self.assertIn("timeline_id FROM pg_control_checkpoint()) <> 2", sql)
        self.assertIn(M.SYSTEM_ID, sql)
        self.assertIn("log_statement='none'", sql)
        self.assertTrue(sql.endswith('COMMIT;\n'))

    def test_unscoped_update_or_injected_identity_refused(self):
        for change in ({'key': 'other-setting'}, {'id': '41;SELECT secret'}, {'xmin': "1';DROP TABLE x;--"}):
            before = row()
            before.update(change)
            with self.assertRaises(RuntimeError):
                M.update_sql(before, M.transform(before['value']))
        with self.assertRaises(RuntimeError):
            M.update_sql(row(), 'https://other.example/')

    def test_repeat_refuses_without_overwriting_receipts(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            folder = pathlib.Path(temporary).resolve()
            folder.chmod(0o700)
            backend = FakeBackend(folder)
            self.assertTrue(M.transaction(backend, folder)['ok'])
            original = {p.name: p.read_bytes() for p in folder.iterdir()}
            with self.assertRaisesRegex(RuntimeError, 'existing-evidence'):
                M.transaction(backend, folder)
            self.assertEqual(original, {p.name: p.read_bytes() for p in folder.iterdir()})
            self.assertEqual(backend.writes, 1)

    def test_permissive_directory_and_symlink_refused(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            folder = pathlib.Path(temporary).resolve()
            folder.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'journal-directory-not-private'):
                M.transaction(FakeBackend(folder), folder)
            folder.chmod(0o700)
            actual = folder / 'actual'
            actual.mkdir(mode=0o700)
            link = folder / 'link'
            link.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'untrusted-journal-directory'):
                M.transaction(FakeBackend(link), link)


if __name__ == '__main__':
    unittest.main()
