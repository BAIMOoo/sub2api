#!/usr/bin/env python3
"""Local continuation fault tests; no remote resources or real Docker calls."""
import contextlib
import copy
import io
import json
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from scripts.migration import complete_quiet_recovery_term_test as BASE
from scripts.migration import continue_quiet_recovery_term as M

OLD, HELPERS, TARGET = M.assemble(pathlib.Path(M.__file__).with_name('complete_quiet_recovery_term.py').read_bytes())


class AssemblyTests(unittest.TestCase):
    def test_offline_has_no_io_and_frozen_dependency_cannot_be_changed(self):
        with mock.patch.object(M.subprocess, 'run') as run, mock.patch.object(M.pathlib.Path, 'read_bytes') as read:
            with contextlib.redirect_stdout(io.StringIO()) as out:self.assertEqual(M.main([]), 0)
        run.assert_not_called(); read.assert_not_called(); self.assertTrue(json.loads(out.getvalue())['offline'])
        with self.assertRaisesRegex(RuntimeError, 'frozen-predecessor-helper-changed'):M.assemble(b'changed')
        self.assertEqual(len(M.MANIFEST), 13)
        self.assertEqual(M.MANIFEST['authorization.json'], 'fd7fcfd7c7d8d63747e86856deaf364b3f139edf9ec85a59e3d1e4e74d892013')
        self.assertEqual(M.MANIFEST['failure.traceback.txt'], 'c3d112cd5543014df53a1dacc28263379e0a618590dceaf643406ce36901cb7a')
        self.assertNotEqual(M.LABEL, M.PREDECESSOR_LABEL)

    def test_no_user_supplied_directory_or_retry_switch(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):M.main(['--label', 'another'])
            with self.assertRaises(SystemExit):M.main(['--retry'])


class ContinuationTests(unittest.TestCase):
    exit_old = BASE.ProgramTests.exit_old
    unchanged = BASE.ProgramTests.unchanged
    reset_attempt = BASE.ProgramTests.reset_attempt
    assert_no_signal_intent = BASE.ProgramTests.assert_no_signal_intent

    def setUp(self):
        BASE.ProgramTests.setUp(self)
        # Reproduce the predecessor's third-sample abort with the actual frozen
        # predecessor program, then pin every byte as the production map does.
        self.observation_patch = lambda n, o: o['old']['work'].update(ops_pending=1) if n == 3 else None
        with self.assertRaisesRegex(RuntimeError, 'complete-old-quiet-proof-required'):
            BASE.ProgramTests.execute(self)
        self.previous = self.saved
        self.previous_bytes = {p.name: p.read_bytes() for p in self.previous.iterdir()}
        self.assertEqual(set(self.previous_bytes), set(M.MANIFEST)); self.assertEqual(len(self.previous_bytes), 13)
        for name, raw in self.previous_bytes.items():self.immutable[self.previous/name] = raw
        self.saved = self.scope/M.LABEL; self.payload['label'] = M.LABEL
        self.observation_patch = None; self.observations = 0; self.runtime_reads = 0; self.clock = 100
        self.calls.clear(); self.events.clear(); self.term_started = False
        exec(M.CONTINUATION_HELPERS, self.env)
        self.env.update(PREDECESSOR_LABEL=M.PREDECESSOR_LABEL, PREDECESSOR_HELPER_SHA=M.PREDECESSOR_SHA,
                        PREDECESSOR_MANIFEST={name: BASE.sha(raw) for name, raw in self.previous_bytes.items()})
        original_run = self.env['subprocess'].run
        self.term_observation_count = None
        def run(argv, **kwargs):
            if argv[:2] != ['/usr/bin/docker', 'kill']:return original_run(argv, **kwargs)
            self.term_observation_count = self.observations
            actual = self.observations
            # The inherited Docker fixture checks a four-sample quiet sequence.
            # Continuation may have additional proved-busy samples before it.
            self.observations = 4
            try:return original_run(argv, **kwargs)
            finally:self.observations = actual
        self.env['subprocess'] = types.SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:exec(TARGET, self.env)
        return json.loads(out.getvalue())

    def test_success_binds_exact_predecessor_and_preserves_all_bytes(self):
        result = self.execute()
        self.assertTrue(result['ok']); self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.term_observation_count, 4)
        receipt = json.loads((self.saved/'authorization.json').read_bytes())
        self.assertEqual(receipt['kind'], 'fixed-preflight-abort-TERM-continuation')
        self.assertEqual(receipt['predecessor_manifest'], self.env['PREDECESSOR_MANIFEST'])
        self.assertEqual(receipt['predecessor_helper_sha256'], M.PREDECESSOR_SHA)
        index = self.events.index('durable:signal.intent.json')
        self.assertEqual(self.events[index+1:index+4], ['read:rolling-recovery/state.json', 'read:rpc/state.json', 'TERM'])
        self.unchanged()
        with self.assertRaisesRegex(RuntimeError, 'already-attempted-inspect-only'):self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_each_predecessor_file_is_exactly_pinned_before_new_attempt(self):
        for name in self.previous_bytes:
            with self.subTest(name=name):
                path = self.previous/name; raw = path.read_bytes(); path.write_bytes(raw+b'changed')
                with self.assertRaisesRegex(RuntimeError, 'predecessor-exact-manifest-changed'):self.execute()
                self.assertFalse(self.saved.exists()); self.assertEqual(self.calls, []); path.write_bytes(raw)
        self.unchanged()

    def test_signal_intent_result_or_unexpected_predecessor_entry_refuses(self):
        for name in ('signal.intent.json', 'docker-term.stdout', 'docker-term.stderr', 'docker-term.result.json', 'verification.json', 'unexpected'):
            with self.subTest(name=name):
                path = self.previous/name; path.write_bytes(b'cannot discount')
                with self.assertRaisesRegex(RuntimeError, 'predecessor-file-set-changed-or-signal-evidence'):self.execute()
                self.assertFalse(self.saved.exists()); self.assertEqual(self.calls, []); path.unlink()
        self.unchanged()

    def test_missing_or_symlink_predecessor_entry_refuses(self):
        path = self.previous/'sample-02.json'; raw = path.read_bytes(); path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'predecessor-file-set-changed'):self.execute()
        path.symlink_to(self.previous/'sample-01.json')
        # H.private_read in the actual host rejects symlinks. Inject that trust
        # failure here, as the shared in-memory host intentionally skips modes.
        original = self.env['load']('host', None).private_read
        def reject_symlink(path, **kwargs):
            BASE.need(not path.is_symlink(), 'UNTRUSTED_FILE'); return original(path, **kwargs)
        self.env['load']('host', None).private_read = reject_symlink
        with self.assertRaisesRegex(RuntimeError, 'UNTRUSTED_FILE'):self.execute()
        path.unlink(); path.write_bytes(raw); self.assertFalse(self.saved.exists()); self.unchanged()

    def test_predecessor_3mib_logs_use_host64mib_reader(self):
        path = self.previous/'sample-03.logs.stdout'; raw = b'x'*(3*1024*1024+1)
        path.write_bytes(raw); self.previous_bytes[path.name] = raw; self.immutable[path] = raw
        self.env['PREDECESSOR_MANIFEST'][path.name] = BASE.sha(raw)
        self.assertTrue(self.execute()['ok']); self.unchanged()

    def test_transient_ops_pending_resets_then_three_fresh_quiet_samples_succeed(self):
        self.observation_patch = lambda n, o: o['old']['work'].update(ops_pending=1) if n == 3 else None
        self.assertTrue(self.execute()['ok'])
        self.assertEqual(self.term_observation_count, 7)
        samples = json.loads((self.saved/'three-complete-quiet-samples.json').read_bytes())
        self.assertEqual([p['observation']['sample_id'] for p in samples], ['4', '5', '6'])
        self.assertGreaterEqual(samples[-1]['tick']-samples[0]['tick'], 2)
        self.assertEqual(json.loads((self.saved/'sample-03.json').read_bytes())['mode'], 'busy')
        self.assertEqual(len(self.calls), 1); self.unchanged()

    def test_final_sample_busy_resets_and_final_runtime_busy_resets_without_intent(self):
        for source in ('sample', 'runtime'):
            with self.subTest(source=source):
                if source == 'sample':
                    self.observation_patch = lambda n, o: o['old']['work'].update(ops_pending=1) if n == 4 else None
                else:
                    self.runtime_patch = lambda n, rt: rt['ingress'].update(queued_requests=1) if n == 5 else None
                self.assertTrue(self.execute()['ok']); self.assertGreater(self.term_observation_count, 4)
                self.assertEqual(len(self.calls), 1)
                self.reset_attempt(); self.calls.clear(); self.row['State'].update(Running=True, Status='running', Pid=BASE.M.PROCESS['pid'])
                self.observation_patch = None; self.runtime_patch = None
        self.unchanged()

    def test_permanent_positive_work_is_bounded_pending_and_never_signals(self):
        self.old['work']['ops_pending'] = 1
        result = self.execute()
        self.assertTrue(result['pending']); self.assertFalse(result['ok']); self.assertEqual(self.clock, 130)
        self.assert_no_signal_intent(); self.unchanged()
        with self.assertRaisesRegex(RuntimeError, 'already-attempted-inspect-only'):self.execute()

    def test_busy_classification_does_not_mask_unhealthy_identity_or_invalid_counters(self):
        for kind in ('process', 'tcp', 'ready', 'negative', 'bool'):
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.old); self.old['work']['ops_pending'] = 1
                if kind == 'process':self.old['process']['start_ticks'] += 1
                elif kind == 'tcp':self.old['tcp_scope'] = 'http-only'
                elif kind == 'ready':self.old['ready'] = False
                elif kind == 'negative':self.old['work']['ops_pending'] = -1
                else:self.old['work']['ops_pending'] = True
                with self.assertRaisesRegex(RuntimeError, 'busy-old-proof-invalid'):self.execute()
                self.assert_no_signal_intent(); self.old = original; self.reset_attempt()
        self.unchanged()

    def test_runtime_unhealthy_or_invalid_counts_refuse_instead_of_wait(self):
        for key, value in [('ready', False), ('active_requests', -1), ('active_hijacked_connections', True)]:
            with self.subTest(field=key):
                original = copy.deepcopy(self.runtime); self.old['work']['ops_pending'] = 1; self.runtime[key] = value
                with self.assertRaisesRegex(RuntimeError, 'old-runtime-not-healthy-nonnegative'):self.execute()
                self.assert_no_signal_intent(); self.runtime = original; self.reset_attempt()
        self.unchanged()

    def test_get_failure_refuses_before_intent_and_preserves_trace(self):
        def fail(n, rt):
            if n == 3:raise TimeoutError('private continuation GET diagnostic')
        self.runtime_patch = fail
        with self.assertRaisesRegex(TimeoutError, 'continuation GET'):self.execute()
        self.assert_no_signal_intent()
        self.assertIn(b'private continuation GET', (self.saved/'failure.traceback.txt').read_bytes()); self.unchanged()

    def test_predecessor_change_during_quiet_proof_is_rejected_before_signal(self):
        path = self.previous/'sample-01.logs.stderr'
        def change(n, observation):
            if n == 4:path.write_bytes(b'predecessor raced')
        self.observation_patch = change
        with self.assertRaisesRegex(RuntimeError, 'predecessor-exact-manifest-changed'):self.execute()
        self.assert_no_signal_intent(); self.unchanged(except_paths=(path,))

    def test_state_pin_and_ledger_pin_must_still_match(self):
        for name in ('state_sha', 'ledger_sha'):
            previous = self.payload[name]; self.payload[name] = '0'*64
            with self.assertRaises(RuntimeError):self.execute()
            self.assertFalse(self.saved.exists()); self.assertEqual(self.calls, []); self.payload[name] = previous
        self.unchanged()

    def test_lost_term_reply_before_or_after_effect_never_replays(self):
        for outcome in ('unknown-before', 'unknown-after'):
            with self.subTest(outcome=outcome):
                self.mode = outcome
                with self.assertRaises(subprocess.TimeoutExpired):self.execute()
                with self.assertRaisesRegex(RuntimeError, 'already-attempted-inspect-only'):self.execute()
                self.assertEqual(len(self.calls), 1)
                self.assertIn(b'private-', (self.saved/'docker-term.stderr').read_bytes())
                self.reset_attempt(); self.calls.clear(); self.row['State'].update(Running=True, Status='running', Pid=BASE.M.PROCESS['pid'])
        self.unchanged()

    def test_intent_fsync_or_post_intent_cas_failure_prevents_signal(self):
        def fsync(path):
            if path.name == 'signal.intent.json':raise OSError('continuation fsync failure')
        self.after_write = fsync
        with self.assertRaises(OSError):self.execute()
        self.assertTrue((self.saved/'signal.intent.json').exists()); self.assertEqual(self.calls, [])
        self.reset_attempt()
        def race(path):
            if path.name == 'signal.intent.json':(self.rpc/'state.json').write_bytes(b'concurrent external ledger')
        self.after_write = race
        with self.assertRaisesRegex(RuntimeError, 'original-checkpoints-CAS-changed'):self.execute()
        self.assertEqual(self.calls, []); self.unchanged(except_paths=(self.rpc/'state.json',))

    def test_already_draining_waits_read_only_and_inspect_busy_has_no_writes(self):
        self.runtime['draining'] = True; self.mode = 'drain-exit'
        self.assertTrue(self.execute()['ok']); self.assert_no_signal_intent(); self.unchanged()
        self.reset_attempt(); self.row['State'].update(Running=True, Status='running', Pid=BASE.M.PROCESS['pid'])
        self.runtime['draining'] = False; self.old['work']['ops_pending'] = 1; self.payload['mode'] = 'inspect'
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        result = self.execute()
        self.assertEqual(result['old_mode'], 'busy'); self.assertTrue(result['inspect_only']); self.assertFalse(self.saved.exists())
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before); self.unchanged()


class LocalAttemptTests(unittest.TestCase):
    def test_local_ssh_timeout_cannot_replay_and_retains_private_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            def private_json(path, value):
                with path.open('xb') as out:out.write(BASE.canonical(value))
            actual_run_path = M.runpy.run_path
            def run_path(path):
                if path.endswith('prepare_retry.py'):return {'FOLDER': folder, 'private_json': private_json}
                if path.endswith('prepare_channel.py'):return {'TARGET': ['ssh', 'fake']}
                return actual_run_path(path)
            timeout = subprocess.TimeoutExpired(['ssh'], 120, output=b'private output', stderr=b'private SSH diagnostic')
            with mock.patch.object(M.runpy, 'run_path', side_effect=run_path), mock.patch.object(M.subprocess, 'run', side_effect=timeout) as run:
                with self.assertRaisesRegex(RuntimeError, 'outcome-unknown-inspect-only'):M.main(['--apply'])
                with self.assertRaisesRegex(RuntimeError, 'existing-continuation-inspect-only'):M.main(['--apply'])
            self.assertEqual(run.call_count, 1)
            result = json.loads((folder/(M.LABEL+'.json')).read_bytes())
            self.assertTrue(result['inspect_before_retry']); self.assertIn('stderr_b64', result)


if __name__ == '__main__':
    unittest.main()
