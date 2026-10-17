"""Offline fault checks for the one-shot stopped-Redis continuation."""
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

P=pathlib.Path(__file__).with_name('finish_stopped_redis_runtime.py')
spec=importlib.util.spec_from_file_location('stopped_redis_tested',P)
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)


class StoppedRedisTests(unittest.TestCase):
    def module(self):
        return M.derive(P.with_name('finalize_recovered_runtime.py').read_bytes())

    def test_default_reports_failure_without_remote_command(self):
        module=self.module()
        with mock.patch.object(module.subprocess,'run',side_effect=AssertionError('remote')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(module.main([]),0)
        self.assertFalse(json.loads(out.getvalue())['wait_budget_passed'])

    def test_frozen_base_and_exact_keeper_are_bound(self):
        raw=P.with_name('finalize_recovered_runtime.py').read_bytes()
        with self.assertRaisesRegex(RuntimeError,'base-changed'):M.derive(raw+b'\n')
        # This executed helper stays pinned to its historical keeper. A later
        # route compatibility upgrade must not silently make it runnable again.
        self.assertEqual(M.KEEPER_SHA,'7baf987354fcca8a6117b61f292d1dc8dceef07c15999a0ea5cd923ac4dd59bf')
        self.assertEqual(M.KEEPER_TEST_SHA,hashlib.sha256(P.with_name('target_keeper_test.py').read_bytes()).hexdigest())
        with self.assertRaisesRegex(RuntimeError,'anchor-changed'):M.replace_once('duplicate duplicate','duplicate','x')

    def test_no_retirement_command_or_rewriting_of_failed_history(self):
        module=self.module();source=module.SOURCE
        compile(source,'source','exec')
        self.assertNotIn('engine.retire_source_redis()',source)
        self.assertNotIn("._redis('SHUTDOWN'",source)
        self.assertNotIn('docker kill',source)
        self.assertIn("stopped['State']['ExitCode']==127",source)
        self.assertIn("('pending' if action=='redis_role_persist' else 'complete')",source)
        self.assertIn("M.E.budget_accounting(state)==before_budget",source)
        self.assertIn("'EXACT_PRIOR_SIGNAL_OPERATION_REQUIRED'",source)
        target=module.derive_target(P.with_name('finalize_runtime.py').read_bytes())
        self.assertIn('K.SOURCE_REDIS_SHELL_EXIT',target)
        self.assertIn('ADOPTION_CHANGED_BUSINESS_PID',target)
        self.assertIn('FRESH_COMPLETE_PROOF_REQUIRED',target)

    def test_unknown_source_stops_and_repeat_cannot_send_another_command(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as temporary:
            home=pathlib.Path(temporary);backup=home/'.codex/backups/sub2api-source-controller-20261016';backup.mkdir(parents=True)
            unknown=subprocess.TimeoutExpired('fixed',55,output=b'partial',stderr=b'detail')
            run=mock.Mock(side_effect=unknown)
            with mock.patch.object(module.pathlib.Path,'home',return_value=home),mock.patch.object(module.runpy,'run_path',return_value={'SSH_SOURCE':['ssh','source'],'TARGET':['ssh','target']}),mock.patch.object(module.subprocess,'run',run):
                with mock.patch.object(module,'KEEPER_SHA',hashlib.sha256(P.with_name('target_keeper.py').read_bytes()).hexdigest()):
                    with self.assertRaisesRegex(RuntimeError,'uncertain-inspect'):module.main(['--apply'])
                    with self.assertRaises(FileExistsError):module.main(['--apply'])
                    self.assertEqual(run.call_count,1)
            receipt=json.loads((backup/'stopped-redis-runtime-finalization-20261017.source.command.json').read_bytes())
            self.assertEqual(receipt['outcome'],'unknown-inspect-only')
            self.assertTrue(receipt['no_repeat'])


if __name__=='__main__':unittest.main()
