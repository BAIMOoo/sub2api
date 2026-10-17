"""Offline tests for the read-only installed-runtime adoption continuation."""
import contextlib
import importlib.util
import io
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

P=pathlib.Path(__file__).with_name('activate_installed_keeper.py')
spec=importlib.util.spec_from_file_location('installed_adoption_tested',P)
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)


class InstalledAdoptionTests(unittest.TestCase):
    def test_default_offline(self):
        with mock.patch.object(M.runpy,'run_path',side_effect=AssertionError('read')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]),0)
        self.assertIn('"offline": true',out.getvalue())

    def test_source_compiles_and_adoption_forbids_all_business_effects(self):
        compile(M.SOURCE,'remote','exec')
        for gate in ('NO_CONTAINER_START_DURING_ADOPTION','NO_ENTRY_START_DURING_ADOPTION',
                     'NO_CONTROL_EFFECT_DURING_ADOPTION','INSTALLED_COMPLETION_PROOF_CHANGED',
                     'FAILURE_RECORD_CHANGED','BUSINESS_PIDS_CHANGED'):
            self.assertIn(gate,M.SOURCE)
        self.assertIn("if e.code!='OPERATION_BUSY'",M.SOURCE)
        self.assertIn('range(30)',M.SOURCE)
        self.assertIn('route.load_route().inspect()',M.SOURCE)

    def test_unknown_command_is_recorded_and_cannot_be_repeated(self):
        actual=M.runpy.run_path
        def modules(path):
            if str(path).endswith('prepare_channel.py'):return {'TARGET':['never-ssh']}
            return actual(path)
        with tempfile.TemporaryDirectory() as temporary:
            home=pathlib.Path(temporary);backup=home/'.codex/backups/sub2api-source-controller-20261016';backup.mkdir(parents=True)
            remote=mock.Mock(side_effect=subprocess.TimeoutExpired('fixed',55,output=b'partial'))
            with mock.patch.object(M.pathlib.Path,'home',return_value=home),mock.patch.object(M.runpy,'run_path',side_effect=modules),mock.patch.object(M.subprocess,'run',remote):
                with self.assertRaisesRegex(RuntimeError,'unknown-outcome'):M.main(['--apply'])
                with self.assertRaises(FileExistsError):M.main(['--apply'])
                remote.assert_called_once()
            self.assertIn('cGFydGlhbA==',(backup/'installed-keeper-adoption-20261017.command.json').read_text())


if __name__=='__main__':unittest.main()
