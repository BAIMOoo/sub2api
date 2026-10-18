"""Never persist arbitrary wire strings; retain all fields for drift detection."""
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time

MAX_BODY = 2 << 20
PUBLIC_HEADERS = {'content-encoding', 'content-type', 'content-length', 'host',
                  'version', 'originator', 'x-openai-internal-codex-responses-lite', 'accept'}
SECRET_HEADERS = {'authorization', 'cookie', 'chatgpt-account-id', 'proxy-authorization'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(canonical(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def opaque(secret, value):
    raw = value.encode()
    return {'$hmac': hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), 'bytes': len(raw)}


def sanitize(secret, value):
    if isinstance(value, str):
        return opaque(secret, value)
    if isinstance(value, list):
        return [sanitize(secret, v) for v in value]
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            lower = k.lower()
            redact = lower in {'authorization', 'cookie', 'password'} or (
                'token' in lower and lower not in {'max_output_tokens', 'max_completion_tokens',
                                                  'input_tokens', 'output_tokens'})
            result[k] = '<redacted>' if redact else sanitize(secret, v)
        return result
    return value


def decode_body(headers, raw):
    encoding = dict((k.lower(), v) for k, v in headers).get('content-encoding', '')
    if encoding == 'zstd':
        import zstandard
        # Streaming decoder handles frames without a content-size declaration.
        import io
        with zstandard.ZstdDecompressor(max_window_size=16 << 20).stream_reader(io.BytesIO(raw)) as r:
            raw = r.read(MAX_BODY + 1)
    elif encoding:
        raise ValueError('unsupported_content_encoding')
    if len(raw) > MAX_BODY:
        raise ValueError('body_too_large')
    return json.loads(raw) if raw else None


def http_evidence(secret, method, path, pairs, body, wire_size, source):
    headers = {}
    plain = {k.lower(): v.strip() for k, v in pairs}
    for k, v in plain.items():
        headers[k] = '<redacted>' if k in SECRET_HEADERS else v if k in PUBLIC_HEADERS else opaque(secret, v)
    checks = {}
    formats = {}
    if isinstance(body, dict):
        cache = body.get('prompt_cache_key')
        checks['session_matches_cache'] = bool(cache) and plain.get('session-id') == cache
        meta = body.get('client_metadata')
        if isinstance(meta, dict):
            checks['metadata_session_matches'] = bool(cache) and meta.get('session_id') == cache
            checks['metadata_thread_matches'] = meta.get('thread_id') == plain.get('thread-id')
            checks['window_matches'] = meta.get('x-codex-window-id') == plain.get('x-codex-window-id')
    for key in ('session-id', 'thread-id', 'x-codex-window-id'):
        if key in plain:
            formats[key] = identity_format(plain[key])
    return {'method': method, 'path': path, 'header_order': [k for k, _ in pairs],
            'headers': headers, 'wire_body_bytes': wire_size, 'body': sanitize(secret, body),
            'identity_checks': checks, 'identity_formats': formats, 'evidence': source,
            'cold_connection': True}


def identity_format(value):
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value):
        return 'uuid'
    if re.fullmatch(r'[0-9a-f]{32}', value):
        return 'hex32'
    return 'other:' + str(len(value.encode()))


def ticket(secret, run, sample, expires):
    for value in (run, sample):
        if not re.fullmatch('[A-Za-z0-9_-]{1,80}', value):
            raise ValueError('invalid_ticket_id')
    encoded = base64.urlsafe_b64encode(canonical({'run': run, 'sample': sample, 'expires': expires})).rstrip(b'=')
    return encoded.decode() + '.' + hmac.new(secret.encode(), encoded, hashlib.sha256).hexdigest()


def differences(a, b, path='$'):
    """Report paths only: even unexpected scalar values cannot leak into alerts."""
    if type(a) is not type(b):
        return [path + ':type']
    if isinstance(a, dict):
        out = [path + '.' + k + ':missing' for k in sorted(a.keys() ^ b.keys())]
        for k in sorted(a.keys() & b.keys()):
            out.extend(differences(a[k], b[k], path + '.' + k))
        return out
    if isinstance(a, list):
        out = [path + ':length'] if len(a) != len(b) else []
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(differences(x, y, path + '[' + str(i) + ']'))
        return out
    return [] if a == b else [path + ':value']


def normalized_body(body):
    value = copy.deepcopy(body)
    if not isinstance(value, dict):
        return value
    # Only the documented per-account session rewrite is ignored. The caller
    # must independently require identity_checks and identity_formats.
    if 'prompt_cache_key' in value:
        value['prompt_cache_key'] = '<scoped-session>'
    meta = value.get('client_metadata')
    if isinstance(meta, dict):
        for k in ('session_id', 'thread_id', 'x-codex-window-id'):
            if k in meta:
                meta[k] = '<scoped-session>'
    for item in value.get('input', []) if isinstance(value.get('input'), list) else []:
        if isinstance(item, dict):
            item.pop('internal_chat_message_metadata_passthrough', None)
    return value


def compare_replay(native, gateway):
    if not native or not gateway:
        return {'status': 'unverified', 'reason': 'missing_http_evidence'}
    body = differences(normalized_body(native['body']), normalized_body(gateway['body']))
    identity = [k for k, v in gateway.get('identity_checks', {}).items() if not v]
    formats = gateway.get('identity_formats', {})
    if not formats:
        identity.append('missing_identity_formats')
    else:
        for key, value in formats.items():
            # The current observer reports UUID:<window> as other:38. Native
            # evidence must have the same shape; deeper window checks remain
            # explicitly unverified until a richer observer is installed.
            if value not in {'uuid', 'hex32'} and not (key == 'x-codex-window-id' and
                    value == native.get('identity_formats', {}).get(key) == 'other:38'):
                identity.append('unexpected_identity_format:' + key)
    headers = differences(native['headers'], gateway['headers'])
    comparable = []
    for evidence in (native, gateway):
        values = copy.deepcopy(evidence['headers'])
        # Scoped identities and compressed byte counts are deliberately dynamic.
        # Presence/order, identity formats and relationships remain checked.
        for key in ('session-id', 'thread-id', 'x-codex-window-id', 'x-client-request-id',
                    'x-codex-turn-metadata', 'content-length'):
            if key in values:
                values[key] = '<dynamic>'
        comparable.append(values)
    header_contract = differences(*comparable)
    # Device/turn values are deliberately scoped by the existing contract.
    # Keep their raw difference paths visible without treating a value rewrite
    # alone as a body regression. The opaque embedded JSON is not asserted equal.
    dynamic = ('$.client_metadata.turn_id.', '$.client_metadata.root_turn_id.',
               '$.client_metadata.x-codex-installation-id.', '$.client_metadata.x-codex-turn-metadata.')
    scoped_differences = [p for p in body if p.startswith(dynamic)]
    body = [p for p in body if not p.startswith(dynamic)]
    order_equal = native['header_order'] == gateway['header_order']
    # Differences remain visible even where they are not a body regression.
    return {'status': 'drift' if body or identity or header_contract or not order_equal else 'pass', 'body_diff': body[:100],
            'identity_failures': identity, 'header_diff': headers[:100],
            'scoped_identity_differences': scoped_differences,
            'identity_boundaries': ['opaque_turn_metadata_contents_unverified', 'window_uuid_suffix_relationship_unverified'],
            'header_contract_diff': header_contract[:100], 'header_order_equal': order_equal,
            'compression_equal': native['headers'].get('content-encoding') == gateway['headers'].get('content-encoding'),
            'wire_size_equal': native['wire_body_bytes'] == gateway['wire_body_bytes']}


def retain(root, now=None, max_bytes=512 << 20):
    """Delete only expired reports; refuse new probes if unexpired evidence fills disk."""
    import shutil
    now = time.time() if now is None else now
    root = Path(root)
    for run in root.glob('runs/*'):
        if not run.is_dir() or run.is_symlink():
            continue
        report = run / 'report.json'
        if not report.exists():
            continue  # Interrupted work requires diagnosis, not silent deletion.
        info = json.loads(report.read_text())
        days = 30 if info.get('status') == 'pass' else 90
        if now - info['finished_at'] > days * 86400:
            shutil.rmtree(run)
    # Binary caches and CLI state have separate limits. They are not evidence
    # and two official binaries alone can exceed the evidence allocation.
    total = sum(p.stat().st_size for name in ('runs', 'baselines', 'observer')
                for p in (root / name).rglob('*') if p.is_file() and not p.is_symlink())
    if total > max_bytes:
        raise ValueError('evidence_storage_full')
    return total
