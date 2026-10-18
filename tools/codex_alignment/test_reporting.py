import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_alignment.__main__ import run_cli
from tools.codex_alignment.notifications import Outbox, email_send, wechat_parts
from tools.codex_alignment.reporting import document, notification_signature, save_document


def sample():
    return {'run': '20261018T093357Z-example', 'status': 'drift', 'started_at': 1792316037,
            'finished_at': 1792316141, 'elapsed_seconds': 104, 'trigger': 'acceptance',
            'baseline': {'version': '0.158.0'}, 'latest_stable': 'rust-v0.163.0',
            'repository': {'head': 'abcdef', 'conclusion': 'success', 'run_id': 123},
            'production': {'source': 'old+observer', 'health_ok': True},
            'coverage_gaps': {'standard': 'no_supported_standard_model'},
            'boundaries': ['tls_is_separate_measurement', 'native_same_transaction_direct_http_unverified'],
            'native_upstream_attempts': 4, 'findings': ['v0_163_0-lite-tool:drift'],
            'scenarios': [{'name': 'v0_163_0-lite-tool', 'status': 'drift', 'model': 'test-model',
                           'cli_exit': 0, 'answer_verified': True, 'capture_errors': [],
                           'replays': [{'status': 'drift', 'result': {'http_status': 200, 'complete': True},
                                        'body_diff': ['$.input[8].call_id.$hmac:value', '$.input[9].id:missing'],
                                        'header_contract_diff': ['$.user-agent.$hmac:value'],
                                        'header_order_equal': True, 'compression_equal': True,
                                        'tls_diff': [], 'identity_failures': []}]},
                          {'name': 'compat-chat', 'status': 'pass', 'violations': [],
                           'result': {'http_status': 200, 'complete': True}}]}


class ReportingTests(unittest.TestCase):
    def test_report_explains_drift_counts_sources_and_limits_without_wire_values(self):
        report = sample()
        report['private_extra'] = 'PRIVATE_CANARY'
        report['scenarios'][0]['replays'][0]['raw_body'] = 'PRIVATE_CANARY'
        text = document(report)
        for expected in ['2026-10-18 17:33:57', '生产请求重放：1/1', 'call_id', 'User-Agent',
                         '标准模式', 'WebSocket', '分别测量', '不能仅凭本次差异',
                         'old+observer', 'abcdef', '完整成功响应', '工具调用编号']:
            self.assertIn(expected, text)
        self.assertNotIn('PRIVATE_CANARY', text)

    def test_drift_does_not_hide_partial_failure_or_missing_response_evidence(self):
        report = sample()
        report['findings'].append('incomplete:run_deadline')
        report['scenarios'][0]['replays'][0]['provider_verified'] = False
        report['scenarios'][0]['capture_errors'] = ['PRIVATE_PROVIDER_ERROR']
        text = document(report)
        self.assertIn('生产请求重放：0/1', text)
        self.assertIn('达到最长运行时间', text)
        self.assertIn('同时存在检测不完整', text)
        self.assertNotIn('本轮已执行的问答、工具等场景均完成', text)
        self.assertNotIn('PRIVATE_PROVIDER_ERROR', text)

    def test_unknown_differences_and_more_than_twelve_findings_are_preserved(self):
        report = sample()
        report['scenarios'][0]['replays'][0]['body_diff'] = ['$.future_field_' + str(i) + ':missing' for i in range(25)]
        report['findings'] += ['future_problem_' + str(i) for i in range(25)]
        text = document(report)
        for i in range(25):
            self.assertIn('$.future_field_' + str(i) + ':missing', text)
            self.assertIn('future_problem_' + str(i), text)
        empty = document({'status': 'unverified', 'scenarios': [], 'findings': ['incomplete:native_login_required']})
        self.assertIn('尚未登录', empty)
        self.assertNotIn('本轮已执行的问答、工具等场景均完成', empty)

    def test_document_persistence_and_signature_ignore_run_time_but_detect_new_failures(self):
        report = sample()
        with tempfile.TemporaryDirectory() as tmp:
            text = save_document(tmp, report)
            self.assertEqual((Path(tmp) / 'latest.md').read_text(), text)
            self.assertEqual((Path(tmp) / 'runs' / report['run'] / 'report.md').read_text(), text)
            self.assertEqual((Path(tmp) / 'latest.md').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                save_document(tmp, {**report, 'run': '../outside'})
        changed = copy.deepcopy(report)
        changed['elapsed_seconds'] += 1
        self.assertEqual(notification_signature(report), notification_signature(changed))
        changed['scenarios'][0]['replays'][0]['provider_verified'] = False
        self.assertNotEqual(notification_signature(report), notification_signature(changed))


class DeliveryTests(unittest.TestCase):
    def test_unicode_document_splits_losslessly_with_numbered_size_bounded_parts(self):
        text = ('这是一份中文报告🙂\n' * 550) + '不能遗漏的结尾。'
        parts = wechat_parts(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(p.partition('\n')[2] for p in parts), text)
        for i, part in enumerate(parts, 1):
            self.assertTrue(part.startswith('检测报告（' + str(i) + '/' + str(len(parts)) + '）'))
            self.assertLessEqual(len(part.encode('utf-8')), 3000)

    def test_partial_wechat_failure_retries_only_unsent_parts_after_process_restart(self):
        text = '完整报告内容\n' * 800
        with tempfile.TemporaryDirectory() as tmp:
            email, wechat, failed = [], [], []
            def wx(body, event):
                if event.endswith('-2') and not failed:
                    failed.append(event)
                    raise OSError('PRIVATE_ERROR')
                wechat.append((body, event))
            box = Outbox(tmp, {'email': lambda body, event: email.append(body), 'wechat': wx})
            box.observe('drift', ['changed'], text, now=100)
            self.assertEqual(len(box.flush(now=100)), 1)
            self.assertEqual(len(wechat), 1)
            restarted = Outbox(tmp, box.senders)
            self.assertEqual(restarted.flush(now=1000), [])
            self.assertEqual(email, [text])
            self.assertEqual([body for body, _ in wechat], wechat_parts(text))
            self.assertEqual(len(set(event for _, event in wechat)), len(wechat))
            self.assertEqual(wechat[1][1], failed[0])
            self.assertNotIn('PRIVATE_ERROR', (Path(tmp) / 'state.json').read_text())

    def test_legacy_outbox_document_is_upgraded_without_resending_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = '旧队列里的完整报告\n' * 500
            item = {'id': 'legacy', 'text': text, 'delivered': ['email'], 'attempts': 1, 'next_attempt': 0}
            (Path(tmp) / 'legacy.json').write_text(json.dumps(item))
            sent = []
            box = Outbox(tmp, {'email': lambda *_: self.fail('email already delivered'),
                               'wechat': lambda body, _: sent.append(body)})
            self.assertEqual(box.flush(now=1000), [])
            self.assertEqual(sent, wechat_parts(text))

    def test_email_contains_full_readable_body_and_downloadable_document(self):
        messages = []
        cfg = {'SMTP_FROM': 'from@example.test', 'SMTP_TO': 'to@example.test', 'SMTP_HOST': 'smtp.qq.com',
               'SMTP_PORT': '465', 'SMTP_USERNAME': 'from@example.test', 'SMTP_PASSWORD': 'PRIVATE_PASSWORD'}
        with patch('smtplib.SMTP_SSL.__init__', return_value=None), \
             patch('smtplib.SMTP_SSL.__enter__', lambda self: self), \
             patch('smtplib.SMTP_SSL.__exit__', return_value=False), \
             patch('smtplib.SMTP_SSL.login'), \
             patch('smtplib.SMTP_SSL.send_message', side_effect=lambda msg: messages.append(msg) or {}):
            email_send(cfg, '完整中文报告\n包括最后一项。', 'event')
        msg = messages[0]
        self.assertEqual(msg.get_body(preferencelist=('plain',)).get_content().strip(), '完整中文报告\n包括最后一项。')
        attachment = list(msg.iter_attachments())[0]
        self.assertEqual(attachment.get_content().strip(), '完整中文报告\n包括最后一项。')
        self.assertNotIn('PRIVATE_PASSWORD', msg.as_string())

    def test_resend_uses_existing_report_without_provider_calls_or_schedule_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = sample()
            raw = json.dumps(report)
            (root / 'latest.json').write_text(raw)
            config = root / 'config.json'
            config.write_text(json.dumps({'proxy_url': 'socks5h://127.0.0.1:1', 'state_dir': tmp}))
            config.chmod(0o600)
            sent = []
            box = Outbox(root / 'outbox', {name: lambda text, event: sent.append(text)
                                          for name in ('email', 'wechat')})
            with patch('tools.codex_alignment.egress.configure'), \
                 patch('tools.codex_alignment.__main__.outbox', return_value=box), \
                 patch('tools.codex_alignment.__main__.Runner.run', side_effect=AssertionError('no live calls')):
                self.assertEqual(run_cli(['--config', str(config), 'notify-latest']), 0)
            self.assertEqual((root / 'latest.json').read_text(), raw)
            self.assertFalse((root / 'schedule.json').exists())
            self.assertIn('报告补发，没有重新调用模型', sent[0])
            self.assertIn('2026-10-18 17:33:57', sent[0])


if __name__ == '__main__':
    unittest.main()
