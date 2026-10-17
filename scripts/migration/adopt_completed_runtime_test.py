"""Offline completion adoption checks; no production operations."""
import contextlib
import hashlib
import importlib.util
import io
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

P=pathlib.Path(__file__).with_name('adopt_completed_runtime.py')
spec=importlib.util.spec_from_file_location('completed_adoption_tested',P)
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)


class CompletedAdoptionTests(unittest.TestCase):
    def test_default_has_no_remote_or_file_read(self):
        with mock.patch.object(M.subprocess,'run',side_effect=AssertionError('remote')),mock.patch.object(M.pathlib.Path,'read_bytes',side_effect=AssertionError('read')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main([]),0)
        self.assertIn('"wait_budget_passed": false',out.getvalue())

    def test_program_pins_and_generated_sources(self):
        self.assertEqual(M.KEEPER_SHA,hashlib.sha256(P.with_name('target_keeper.py').read_bytes()).hexdigest())
        self.assertEqual(M.ROUTE_SHA,hashlib.sha256(P.with_name('rolling_app_route.py').read_bytes()).hexdigest())
        compile(M.SOURCE,'source','exec');compile(M.TARGET,'target','exec')
        self.assertIn('ADOPTION_MUST_NOT_START_CONTAINER',M.TARGET)
        self.assertIn('ADOPTION_MUST_NOT_START_ENTRY',M.TARGET)
        self.assertIn("route.load_route().inspect()",M.TARGET)
        self.assertIn("proof['budget']==original['budget']",M.TARGET)
        self.assertIn("'BUSINESS_PIDS_CHANGED'",M.TARGET)
        self.assertIn('ORIGINAL_FAILURE_HISTORY_CHANGED',M.TARGET)

    def test_uncertain_source_receipt_prevents_target_and_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            home=pathlib.Path(temporary);backup=home/'.codex/backups/sub2api-source-controller-20261016';backup.mkdir(parents=True)
            # Fake source command receipt has no secret data.
            (backup/'reconciled-runtime-finalization-20261017.source.command.json').write_text('{"stdout_b64":"e30="}')
            remote=mock.Mock(side_effect=subprocess.TimeoutExpired('fixed',55))
            with mock.patch.object(M.pathlib.Path,'home',return_value=home),mock.patch.object(M.runpy,'run_path',return_value={'SSH_SOURCE':['source'],'TARGET':['target']}),mock.patch.object(M.subprocess,'run',remote):
                with self.assertRaisesRegex(RuntimeError,'outcome-unknown'):M.main(['--apply'])
                with self.assertRaises(FileExistsError):M.main(['--apply'])
                self.assertEqual(remote.call_count,1)
            self.assertIn('unknown-inspect-only',(backup/'completed-runtime-adoption-20261017.source.command.json').read_text())


if __name__=='__main__':unittest.main()
