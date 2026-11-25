"""Exercise trigger decisions through the CLI without network or model calls."""
import copy
import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_alignment.__main__ import run_cli
from tools.codex_alignment.evidence import atomic_json


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / 'config.json'
        self.config = {'state_dir': str(self.root), 'snapshot_file': str(self.root / 'snapshot.json'),
                       'repository': 'test/repository', 'proxy_url': 'socks5h://127.0.0.1:1'}
        self.snapshot = {'image': 'image-a', 'binary_sha256': 'binary-a', 'generation': 'settings-a'}
        self.now = datetime.datetime(2026, 10, 20, 2, tzinfo=datetime.timezone.utc).timestamp()
        for target in ('tools.codex_alignment.egress.configure',
                       'tools.codex_alignment.__main__.save_document'):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        box = patch('tools.codex_alignment.__main__.outbox')
        box.start().return_value.flush.return_value = []
        self.addCleanup(box.stop)
        runner = patch('tools.codex_alignment.__main__.Runner.run', side_effect=self.report)
        self.runner = runner.start()
        self.addCleanup(runner.stop)
        self.network_mocks = []
        for target in ('tools.codex_alignment.releases.urlopen', 'urllib.request.urlopen'):
            patcher = patch(target, side_effect=AssertionError('unexpected_network_call'))
            self.network_mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        self.tick(0)  # Establish a completed daily run with the installed trigger policy.
        self.runner.reset_mock()

    def report(self, reason, generation):
        return {'run': 'synthetic-run', 'status': 'pass', 'findings': [], 'scenarios': [],
                'trigger': reason, 'generation': generation, 'production': copy.deepcopy(self.snapshot)}

    def tick(self, seconds, command='tick'):
        atomic_json(self.config_path, self.config)
        atomic_json(self.root / 'snapshot.json', self.snapshot)
        with patch('tools.codex_alignment.__main__.time.time', return_value=self.now + seconds), \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(run_cli(['--config', str(self.config_path), command]), 0)

    def assert_no_network_calls(self):
        for mock in self.network_mocks:
            mock.assert_not_called()

    def assert_idle_until_daily_backstop(self):
        self.runner.assert_not_called()
        state = json.loads((self.root / 'schedule.json').read_text())
        self.assertIsNone(state['pending'])
        self.tick(23 * 3600 - 1)  # Next day, 08:59:59 Asia/Shanghai.
        self.runner.assert_not_called()
        self.tick(23 * 3600)
        self.runner.assert_called_once()
        self.assert_no_network_calls()

    def test_admin_changes_do_not_trigger_or_delay_daily_backstop(self):
        for index, change in enumerate([
            {'generation': 'empty-map-to-null'},
            {'generation': 'account-proxy-settings', 'proxy_id': 2},
            {'generation': 'group-global-settings', 'account_available': False},
        ], 1):
            self.snapshot.update(change)
            self.tick(index * 600)
            self.tick(index * 600 + 301)
        self.assert_idle_until_daily_backstop()

    def test_image_and_binary_changes_still_trigger_after_quiet_window(self):
        for index, key in enumerate(['image', 'binary_sha256'], 1):
            self.runner.reset_mock()
            self.snapshot[key] += '-deployed'
            self.tick(index * 600)
            self.tick(index * 600 + 299)
            self.runner.assert_not_called()
            self.tick(index * 600 + 300)
            self.runner.assert_called_once()

    def test_repository_and_ci_changes_do_not_trigger_or_poll_github(self):
        repo_path = self.root / 'repository.json'
        self.assertFalse(repo_path.exists())
        self.tick(60)
        self.tick(361)
        self.assertFalse(repo_path.exists())
        self.runner.assert_not_called()
        self.assert_no_network_calls()

        for elapsed, data in [
            (400, {'head': 'commit-b', 'conclusion': None}),
            (500, {'head': 'commit-c', 'conclusion': 'success'}),
            (600, {'head': 'commit-d', 'conclusion': 'failure'}),
        ]:
            atomic_json(repo_path, data)
            stale = self.now + elapsed - 3600
            os.utime(repo_path, (stale, stale))
            self.tick(elapsed)
        self.tick(901)
        self.assert_idle_until_daily_backstop()

    def test_deployment_and_config_bursts_coalesce_to_latest_state(self):
        self.snapshot['image'] = 'image-b'
        self.tick(60)
        self.snapshot['binary_sha256'] = 'binary-b'
        self.tick(180)
        self.config['watchdog_seconds'] = 1800
        self.tick(240)
        self.tick(539)
        self.runner.assert_not_called()
        self.tick(540)
        self.runner.assert_called_once()
        self.tick(900)
        self.runner.assert_called_once()

    def test_monitor_rule_changes_still_trigger(self):
        self.config['watchdog_seconds'] = 1800
        self.tick(60)
        self.runner.assert_not_called()
        self.tick(360)
        self.runner.assert_called_once()

    def test_manual_acceptance_can_check_admin_changes_immediately(self):
        self.snapshot['generation'] = 'new-admin-settings'
        self.tick(60, command='acceptance')
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0], 'acceptance')


if __name__ == '__main__':
    unittest.main()
