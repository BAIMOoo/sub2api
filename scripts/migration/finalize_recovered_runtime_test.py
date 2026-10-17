"""Fault tests for post-recovery finalization; no production effects."""
import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

P=pathlib.Path(__file__).with_name('finalize_recovered_runtime.py')
spec=importlib.util.spec_from_file_location('recovered_runtime_tested',P)
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)


class FinalizationTests(unittest.TestCase):
    def test_default_is_offline_and_reports_failed_wait(self):
        with mock.patch.object(M.subprocess,'run',side_effect=AssertionError('remote')), \
             mock.patch.object(M.pathlib.Path,'read_bytes',side_effect=AssertionError('read')), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]),0)
        self.assertFalse(json.loads(out.getvalue())['wait_budget_passed'])

    def test_exact_legacy_target_is_bound_and_failure_status_is_explicit(self):
        raw=P.with_name('finalize_runtime.py').read_bytes()
        target=M.derive_target(raw);compile(target,'derived-target','exec');compile(M.SOURCE,'source','exec')
        with self.assertRaisesRegex(RuntimeError,'base-changed'):M.derive_target(raw+b'\n')
        self.assertIn("arm['complete']['wait_budget_breach']",target)
        self.assertIn("state['deadline_breached'] is True",target)
        self.assertNotIn("deadline_breached=False",M.SOURCE)

    def test_unknown_target_is_recorded_and_cannot_restart_source_or_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            home=pathlib.Path(temporary);backup=home/'.codex/backups/sub2api-source-controller-20261016'
            backup.mkdir(parents=True)
            channel={'SSH_SOURCE':['ssh','source'],'TARGET':['ssh','target']}
            unknown=subprocess.TimeoutExpired('fixed',55,output=b'partial',stderr=b'detail')
            run=mock.Mock(side_effect=[subprocess.CompletedProcess([],0,b'{"v":1}',b''),unknown])
            with mock.patch.object(M.pathlib.Path,'home',return_value=home), \
                 mock.patch.object(M,'KEEPER_SHA',hashlib.sha256(P.with_name('target_keeper.py').read_bytes()).hexdigest()), \
                 mock.patch.object(M,'KEEPER_TEST_SHA',hashlib.sha256(P.with_name('target_keeper_test.py').read_bytes()).hexdigest()), \
                 mock.patch.object(M.runpy,'run_path',return_value=channel),mock.patch.object(M.subprocess,'run',run):
                with self.assertRaisesRegex(RuntimeError,'uncertain-inspect'):M.main(['--apply'])
                self.assertEqual(run.call_count,2)
                with self.assertRaises(FileExistsError):M.main(['--apply'])
                self.assertEqual(run.call_count,2)
            receipt=json.loads((backup/'recovered-runtime-finalization-20261017.target.command.json').read_bytes())
            self.assertEqual(receipt['outcome'],'unknown-inspect-only')
            self.assertEqual(receipt['stdout_b64'],'cGFydGlhbA==')
            self.assertTrue(receipt['no_repeat'])


if __name__=='__main__':unittest.main()
