import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_alignment.__main__ import run_cli
from tools.codex_alignment.notifications import Outbox, email_send, wechat_send
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
    def test_wechat_sends_one_encrypted_markdown_file_with_full_unicode_content(self):
        import base64
        import hashlib
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        text = ('完整中文报告🙂\n' * 550) + '- 运行编号：20261018T093357Z-example\n'
        cfg = {'base_url': 'https://ilinkai.weixin.qq.com', 'token': 'PRIVATE_TOKEN',
               'chat_id': 'test-user', 'context_token': 'old-context'}
        requests, messages = [], []
        class Response:
            status = 200
            headers = {'x-encrypted-param': 'download-parameter'}
            def __init__(self, body): self.body = body
            def read(self, *_): return json.dumps(self.body).encode()
            def __enter__(self): return self
            def __exit__(self, *_): pass
        def send(request, **kwargs):
            requests.append(request)
            if request.full_url.endswith('/getuploadurl'):
                return Response({'ret': 0, 'upload_param': 'upload+parameter/='})
            if '/c2c/upload?' in request.full_url:
                return Response({})
            message = json.loads(request.data)['msg']
            messages.append(message)
            return Response({'ret': -14} if 'context_token' in message else {'ret': 0})
        with patch('tools.codex_alignment.notifications.urlopen', side_effect=send):
            wechat_send(cfg, text, 'stable-event')
        self.assertEqual(len(requests), 4)  # Authorize, upload, stale-context send, retry.
        auth = json.loads(requests[0].data)
        self.assertEqual(auth['media_type'], 3)
        self.assertEqual(auth['rawsize'], len(text.encode()))
        self.assertEqual(auth['rawfilemd5'], hashlib.md5(text.encode()).hexdigest())
        self.assertEqual(requests[1].get_method(), 'POST')
        self.assertIsNone(requests[1].get_header('Authorization'))
        self.assertNotIn(text.encode(), requests[1].data)
        self.assertEqual(len(requests[1].data), auth['filesize'])
        self.assertEqual(messages[0]['client_id'], messages[1]['client_id'])
        self.assertEqual(messages[0]['item_list'], messages[1]['item_list'])
        self.assertEqual(len(messages[-1]['item_list']), 1)
        item = messages[-1]['item_list'][0]
        self.assertEqual(item['type'], 4)
        self.assertNotIn('text_item', item)
        self.assertEqual(item['file_item']['file_name'], 'Sub2API-请求对齐报告-20261018T093357Z-example.md')
        self.assertEqual(int(item['file_item']['len']), len(text.encode()))
        media = item['file_item']['media']
        self.assertEqual(media['encrypt_query_param'], 'download-parameter')
        key = bytes.fromhex(base64.b64decode(media['aes_key']).decode())
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(requests[1].data) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        self.assertEqual((unpadder.update(padded) + unpadder.finalize()).decode(), text)

    def test_file_delivery_failure_retries_only_wechat_with_same_full_document(self):
        text = '完整报告内容\n' * 800
        with tempfile.TemporaryDirectory() as tmp:
            email, wechat, attempts = [], [], []
            def wx(body, event):
                attempts.append((body, event))
                if len(attempts) == 1:
                    raise OSError('PRIVATE_ERROR')
                wechat.append(body)
            box = Outbox(tmp, {'email': lambda body, event: email.append(body), 'wechat': wx})
            box.observe('drift', ['changed'], text, now=100)
            self.assertEqual(len(box.flush(now=100)), 1)
            self.assertEqual(wechat, [])
            restarted = Outbox(tmp, box.senders)
            self.assertEqual(restarted.flush(now=1000), [])
            self.assertEqual(email, [text])
            self.assertEqual(wechat, [text])
            self.assertEqual(attempts[0], attempts[1])
            self.assertNotIn('PRIVATE_ERROR', (Path(tmp) / 'state.json').read_text())

    def test_legacy_partial_text_notice_becomes_complete_file_without_resending_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = '旧队列里的完整报告\n' * 500
            item = {'id': 'legacy', 'text': text, 'delivered': ['email'], 'attempts': 1, 'next_attempt': 0,
                    'wechat_parts': ['old part one', 'old part two'], 'wechat_next': 1}
            (Path(tmp) / 'legacy.json').write_text(json.dumps(item))
            sent = []
            box = Outbox(tmp, {'email': lambda *_: self.fail('email already delivered'),
                               'wechat': lambda body, _: sent.append(body)})
            self.assertEqual(box.flush(now=1000), [])
            self.assertEqual(sent, [text])

    def test_unexpected_cdn_url_is_rejected_before_upload(self):
        cfg = {'base_url': 'https://ilinkai.weixin.qq.com', 'token': 'PRIVATE_TOKEN', 'chat_id': 'test-user'}
        for url in ['https://untrusted.example/c2c/upload', 'http://novac2c.cdn.weixin.qq.com/c2c/upload',
                    'https://novac2c.cdn.weixin.qq.com:444/c2c/upload',
                    'https://user:password@novac2c.cdn.weixin.qq.com/c2c/upload']:
            with self.subTest(url=url), \
                 patch('tools.codex_alignment.notifications.wechat_api', return_value={'upload_full_url': url}), \
                 patch('tools.codex_alignment.notifications.urlopen') as upload:
                with self.assertRaises(ValueError):
                    wechat_send(cfg, '中文全文', 'event')
                upload.assert_not_called()

    def test_missing_cdn_receipt_does_not_send_file_or_fall_back_to_long_text(self):
        cfg = {'base_url': 'https://ilinkai.weixin.qq.com', 'token': 'PRIVATE_TOKEN', 'chat_id': 'test-user'}
        class Response:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *_): pass
        with patch('tools.codex_alignment.notifications.wechat_api', return_value={'upload_param': 'test'}) as api, \
             patch('tools.codex_alignment.notifications.urlopen', return_value=Response()):
            with self.assertRaisesRegex(ValueError, 'wechat_file_upload_failed'):
                wechat_send(cfg, '中文全文', 'event')
            self.assertEqual(api.call_count, 1)

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
