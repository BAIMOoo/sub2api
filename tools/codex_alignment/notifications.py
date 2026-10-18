"""Durable, per-channel delivery with stable incident keys and recovery."""
import base64
from email.message import EmailMessage
import hashlib
import json
from pathlib import Path
import re
import secrets
import shlex
import smtplib
import ssl
import time
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .evidence import atomic_json, digest


def read_env(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError('notification_file_permissions')
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ValueError('invalid_notification_file')
        parsed = shlex.split(value)
        if len(parsed) != 1:
            raise ValueError('invalid_notification_value')
        result[key] = parsed[0]
    return result


def email_send(cfg, text, event_id, proxy_url=None):
    msg = EmailMessage()
    msg['From'], msg['To'] = cfg['SMTP_FROM'], cfg['SMTP_TO']
    msg['Subject'] = 'Sub2API Codex 请求对齐检测'
    msg['Message-ID'] = '<alignment-' + event_id + '@sub2api.local>'
    msg.set_content(text)
    msg.add_attachment(text, subtype='markdown', charset='utf-8', filename='Sub2API-检测报告.md')
    # Credentials can only go to the explicitly configured QQ implicit-TLS server.
    if cfg['SMTP_HOST'] != 'smtp.qq.com' or str(cfg['SMTP_PORT']) != '465':
        raise ValueError('qq_smtp_tls_required')
    class SMTP(smtplib.SMTP_SSL):
        def _get_socket(self, host, port, timeout):
            if proxy_url is None:
                return super()._get_socket(host, port, timeout)
            from .capture import socks_connect
            sock = socks_connect(proxy_url, host, port, timeout)
            try:
                return self.context.wrap_socket(sock, server_hostname=host)
            except BaseException:
                sock.close()
                raise
    with SMTP(cfg['SMTP_HOST'], 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(cfg['SMTP_USERNAME'], cfg['SMTP_PASSWORD'])
        if smtp.send_message(msg):
            raise ValueError('smtp_recipient_refused')


def wechat_api(cfg, endpoint, payload):
    base = cfg['base_url'].rstrip('/')
    if base != 'https://ilinkai.weixin.qq.com':
        raise ValueError('unexpected_wechat_endpoint')
    req = Request(base + '/ilink/bot/' + endpoint,
                  data=json.dumps({**payload, 'base_info': {'channel_version': '2.2.0'}}).encode(), headers={
        'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token',
        'Authorization': 'Bearer ' + cfg['token'], 'iLink-App-Id': 'bot',
        'iLink-App-ClientVersion': str((2 << 16) | (2 << 8)),
        'X-WECHAT-UIN': base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read(65536))


def wechat_send(cfg, text, event_id):
    """Send one encrypted Markdown attachment using the iLink file protocol."""
    raw = text.encode('utf-8')
    filekey, key = secrets.token_hex(16), secrets.token_bytes(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    # AES-128-ECB with PKCS#7 is required by the WeChat media protocol.
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    upload = wechat_api(cfg, 'getuploadurl', {
        'filekey': filekey, 'media_type': 3, 'to_user_id': cfg['chat_id'],
        'rawsize': len(raw), 'rawfilemd5': hashlib.md5(raw, usedforsecurity=False).hexdigest(),
        'filesize': len(encrypted), 'no_need_thumb': True, 'aeskey': key.hex()})
    if upload.get('ret', 0) != 0 or upload.get('errcode', 0) != 0:
        raise ValueError('wechat_upload_authorization_refused')
    url = upload.get('upload_full_url')
    if not url and upload.get('upload_param'):
        url = 'https://novac2c.cdn.weixin.qq.com/c2c/upload?' + urlencode({
            'encrypted_query_param': upload['upload_param'], 'filekey': filekey})
    target = urlsplit(url or '')
    if (target.scheme != 'https' or target.hostname != 'novac2c.cdn.weixin.qq.com'
            or target.port not in (None, 443) or target.path != '/c2c/upload'
            or target.username or target.password or target.fragment):
        raise ValueError('unexpected_wechat_upload_endpoint')
    # Only ciphertext goes to the CDN. Bot authorization stays on the API host.
    req = Request(url, data=encrypted, headers={'Content-Type': 'application/octet-stream'})
    with urlopen(req, timeout=60) as response:
        download = response.headers.get('x-encrypted-param')
        if response.status != 200 or not download:
            raise ValueError('wechat_file_upload_failed')
    run = re.search(r'^- 运行编号：([A-Za-z0-9_-]{1,80})$', text, re.MULTILINE)
    suffix = run.group(1) if run else re.sub(r'[^A-Za-z0-9_-]', '', event_id)[:40]
    item = {'type': 4, 'file_item': {
        'file_name': 'Sub2API-请求对齐报告-' + suffix + '.md', 'len': str(len(raw)),
        'media': {'encrypt_query_param': download,
                  'aes_key': base64.b64encode(key.hex().encode('ascii')).decode('ascii'),
                  'encrypt_type': 1}}}
    message = {'from_user_id': '', 'to_user_id': cfg['chat_id'], 'client_id': 'alignment-' + event_id,
               'message_type': 2, 'message_state': 2,
               'item_list': [item]}
    if cfg.get('context_token'):
        message['context_token'] = cfg['context_token']
    for attempt in range(2):
        result = wechat_api(cfg, 'sendmessage', {'msg': message})
        if result.get('ret', 0) == 0 and result.get('errcode', 0) == 0:
            return
        if attempt == 0 and (result.get('errcode') == -14 or result.get('ret') == -14) and 'context_token' in message:
            message.pop('context_token')
            continue
        raise ValueError('wechat_delivery_refused')


class Outbox:
    def __init__(self, directory, senders):
        self.root, self.senders = Path(directory), senders
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / 'state.json'
        self.state = json.loads(path.read_text()) if path.exists() else {'current': None, 'last_notice': 0}

    def observe(self, status, findings, text, now=None, force=False):
        now = time.time() if now is None else now
        key = digest({'status': status, 'findings': findings})
        changed = key != self.state['current']
        periodic = status != 'pass' and now - self.state['last_notice'] >= 86400
        recovery = status == 'pass' and self.state.get('status') not in (None, 'pass')
        if force or recovery or status != 'pass' and (changed or periodic):
            event_id = digest({'key': key, 'at': now})[:32]
            atomic_json(self.root / (event_id + '.json'), {'id': event_id, 'text': text, 'created': now,
                                                         'delivered': [], 'attempts': 0, 'next_attempt': 0})
            self.state['last_notice'] = now
        self.state.update(current=key, status=status)
        atomic_json(self.root / 'state.json', self.state)

    def flush(self, now=None):
        now = time.time() if now is None else now
        pending = []
        for path in sorted(self.root.glob('*.json')):
            if path.name == 'state.json':
                continue
            item = json.loads(path.read_text())
            if now < item['next_attempt']:
                pending.append(item['id'])
                continue
            for channel, sender in self.senders.items():
                if channel in item['delivered']:
                    continue
                try:
                    # Legacy partial text notices are now sent as one complete
                    # document. Already delivered channels remain untouched.
                    sender(item['text'], item['id'])
                except Exception:
                    pass  # No provider error body or credential-bearing traceback.
                else:
                    item['delivered'].append(channel)
                    atomic_json(path, item)
            if set(item['delivered']) >= {'email', 'wechat'}:
                path.unlink()
            else:
                item['attempts'] += 1
                item['next_attempt'] = now + min(3600, 60 * 2 ** min(item['attempts'], 6))
                atomic_json(path, item)
                pending.append(item['id'])
        return pending
