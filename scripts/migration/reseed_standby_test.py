import contextlib
import importlib.util
import json
import pathlib
import tempfile
import time
import types
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('tested_reseed', pathlib.Path(__file__).with_name('reseed_standby.py'))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


class DataMovementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.old, self.new = self.root/'volume', self.root/'new'
        for path, value in ((self.old, b'old'), (self.new, b'new')):
            path.mkdir()
            (path/'pg_tblspc').mkdir()
            (path/'pg_wal').mkdir()
            (path/'pg_wal'/'segment').write_bytes(value+b'-wal')
            for name in ('PG_VERSION', 'base', '.sub2api-seed-verified'):
                (path/name).write_bytes(value+b'-'+name.encode())
        self.archive = self.root/'retained'

    def payloads(self):
        return sorted(p.read_bytes() for p in self.root.rglob('*')
                      if p.is_file() and p.parent.name != 'moves')

    def test_preserves_volume_inode_and_every_payload(self):
        before, inode = self.payloads(), self.old.stat().st_ino
        R.move_data(self.old, self.new, self.archive)
        self.assertEqual(self.payloads(), before)
        self.assertEqual(self.old.stat().st_ino, inode)
        self.assertEqual((self.old/'base').read_bytes(), b'new-base')
        self.assertEqual((self.archive/'base').read_bytes(), b'old-base')
        self.assertFalse(list(self.new.iterdir()))

    def test_each_interruption_preserves_data_and_refuses_replay(self):
        for cutoff in (1, 3, 5, 8):
            with self.subTest(cutoff=cutoff):
                if cutoff != 1:
                    self.tearDown()
                    self.setUp()
                before, inode = self.payloads(), self.old.stat().st_ino
                rename = R.os.rename
                count = 0

                def interrupted(src, dst):
                    nonlocal count
                    rename(src, dst)
                    count += 1
                    if count == cutoff:
                        raise OSError('injected after durable rename')

                with mock.patch.object(R.os, 'rename', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        R.move_data(self.old, self.new, self.archive)
                self.assertEqual(self.payloads(), before)
                self.assertEqual(self.old.stat().st_ino, inode)
                with self.assertRaisesRegex(RuntimeError, 'OLD_ARCHIVE_ALREADY_EXISTS'):
                    R.move_data(self.old, self.new, self.archive)

    def test_marker_is_removed_first_and_installed_last(self):
        moved, rename = [], R.os.rename

        def observe(src, dst):
            moved.append((src.parent.name, src.name))
            rename(src, dst)

        with mock.patch.object(R.os, 'rename', side_effect=observe):
            R.move_data(self.old, self.new, self.archive)
        self.assertEqual(moved[0], ('volume', '.sub2api-seed-verified'))
        self.assertEqual(moved[-1], ('new', '.sub2api-seed-verified'))

    def test_external_tablespace_is_rejected_before_any_move(self):
        (self.old/'pg_tblspc'/'123').symlink_to(self.root)
        with self.assertRaisesRegex(RuntimeError, 'EXTERNAL_WAL_OR_TABLESPACE'):
            R.move_data(self.old, self.new, self.archive)
        self.assertFalse(self.archive.exists())
        self.assertEqual((self.old/'base').read_bytes(), b'old-base')


class BackupIntentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scope = pathlib.Path(self.temp.name)/'scope'
        self.scope.mkdir()
        self.old = pathlib.Path(self.temp.name)/'old'
        self.old.mkdir()
        self.proof = {'owner': R.OWNER, 'healthy': True, 'role_login': True,
                      'slot': 'sub2api_mig_20261016', 'checked_at': time.time()}
        self.module = types.SimpleNamespace(trusted_directory=lambda *a, **k: None,
                                           strict_json=json.loads,
                                           private_read=lambda p: pathlib.Path(p).read_bytes())
        self.backend = types.SimpleNamespace(pg_locks=contextlib.nullcontext)
        self.store = types.SimpleNamespace(mutation_lock=contextlib.nullcontext)
        self.patches = [mock.patch.object(R, 'SCOPE', self.scope),
                        mock.patch.object(R, 'load', return_value=(self.module, {}, self.backend, self.store)),
                        mock.patch.object(R, 'plan_and_stopped', return_value=({}, None, self.old)),
                        mock.patch('shutil.disk_usage', return_value=types.SimpleNamespace(free=8*R.GIB))]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def save_proof(self):
        (self.scope/'source-ready.json').write_text(json.dumps(self.proof))

    def test_stale_source_proof_never_creates_container(self):
        self.proof['checked_at'] -= 31
        self.save_proof()
        with mock.patch.object(R, 'command') as cmd:
            with self.assertRaisesRegex(RuntimeError, 'FRESH_SOURCE_READY'):
                R.backup()
        cmd.assert_not_called()

    def test_unknown_create_or_start_is_never_replayed(self):
        for failing_call in (1, 2):
            with self.subTest(failing_call=failing_call):
                if failing_call == 2:
                    self.tearDown()
                    self.setUp()
                self.save_proof()
                calls = []

                def command(argv, timeout=8):
                    calls.append(argv)
                    if len(calls) == failing_call:
                        raise RuntimeError('COMMAND_UNKNOWN_INSPECT_ONLY')
                    return b'owned-helper-id\n'

                with mock.patch.object(R, 'command', side_effect=command):
                    with self.assertRaisesRegex(RuntimeError, 'COMMAND_UNKNOWN'):
                        R.backup()
                    count = len(calls)
                    with self.assertRaisesRegex(RuntimeError, 'BACKUP_ALREADY_ATTEMPTED'):
                        R.backup()
                self.assertEqual(len(calls), count)
                self.assertTrue((self.scope/'backup.intent.json').exists())


class RecoveryConnectionTest(unittest.TestCase):
    def auto(self, extra=''):
        conn = ("user=sub2api_mig_20261016_replica host=127.0.0.1 port=15432 "
                "passfile='/run/sub2api-migration/replication.pgpass' "
                "application_name='sub2api-pg-replica-20261016' sslmode=disable" + extra)
        return "primary_conninfo = '" + conn.replace("'", "''") + "'\nprimary_slot_name = 'sub2api_mig_20261016'\n"

    def test_accepts_pg_basebackup_double_quoted_conninfo(self):
        R.validate_recovery_connection(self.auto())

    def test_rejects_overrides_duplicates_or_partial_matches(self):
        for value in [self.auto(' hostaddr=10.0.0.1'), self.auto(' host=127.0.0.1'),
                      self.auto().replace('port=15432', 'port=154320'),
                      self.auto().replace('replication.pgpass', 'replication.pgpass.wrong'),
                      self.auto() + "primary_slot_name = 'other'\n",
                      self.auto() + " primary_slot_name = 'other' # override\n",
                      self.auto() + "\tprimary_conninfo = 'host=10.0.0.1'\n"]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, 'RECOVERY_CONNECTION_SETTINGS_CHANGED'):
                    R.validate_recovery_connection(value)


if __name__ == '__main__':
    unittest.main()
