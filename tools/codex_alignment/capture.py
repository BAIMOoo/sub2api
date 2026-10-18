"""Loopback-only inspection proxy with mandatory SOCKS egress and bounded I/O.

HTTP inspection deliberately uses a custom CA. Its TLS is never described as
the native default TLS fingerprint. hello_only captures that separately and
stops before an HTTP request can be sent.
"""
from contextlib import contextmanager
import datetime
import http.client
import ipaddress
import json
from pathlib import Path
import select
import socket
import socketserver
import ssl
import struct
import threading
import time
from urllib.parse import unquote, urlsplit

from .evidence import MAX_BODY, atomic_json, decode_body, http_evidence
from .protocol import completion

ALLOWED_GET = {'/backend-api/codex/models', '/backend-api/plugins/featured',
               '/backend-api/wham/settings/user', '/backend-api/wham/accounts/check'}
ALLOWED_POST = {'/backend-api/codex/responses', '/backend-api/codex/responses/compact'}


def exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        piece = sock.recv(size - len(chunks))
        if not piece:
            raise OSError('unexpected_eof')
        chunks.extend(piece)
    return bytes(chunks)


def socks_connect(proxy_url, host, port, timeout=45):
    proxy = urlsplit(proxy_url)
    if proxy.scheme not in {'socks5', 'socks5h'} or not proxy.hostname or not proxy.port:
        raise ValueError('fixed_socks_proxy_required')
    sock = socket.create_connection((proxy.hostname, proxy.port), timeout=timeout)
    try:
        auth = proxy.username is not None
        sock.sendall(b'\x05\x01' + (b'\x02' if auth else b'\x00'))
        if exact(sock, 2) != (b'\x05\x02' if auth else b'\x05\x00'):
            raise OSError('socks_auth_method_refused')
        if auth:
            user, password = unquote(proxy.username).encode(), unquote(proxy.password or '').encode()
            if not 0 < len(user) <= 255 or len(password) > 255:
                raise ValueError('invalid_socks_auth')
            sock.sendall(b'\x01' + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if exact(sock, 2) != b'\x01\x00':
                raise OSError('socks_auth_refused')
        encoded = host.encode('idna')
        if len(encoded) > 255 or not 0 < port < 65536:
            raise ValueError('invalid_socks_target')
        # DNS always resolves at the selected exit, including socks5 URLs.
        sock.sendall(b'\x05\x01\x00\x03' + bytes([len(encoded)]) + encoded + struct.pack('!H', port))
        header = exact(sock, 4)
        if header[:3] != b'\x05\x00\x00':
            raise OSError('socks_connect_refused')
        lengths = {1: 4, 4: 16}
        size = exact(sock, 1)[0] if header[3] == 3 else lengths.get(header[3])
        if size is None:
            raise OSError('socks_invalid_reply')
        exact(sock, size + 2)
        return sock
    except BaseException:
        sock.close()
        raise


class Cursor:
    def __init__(self, data):
        self.data = data

    def take(self, n):
        if n < 0 or len(self.data) < n:
            raise ValueError('invalid_client_hello')
        value, self.data = self.data[:n], self.data[n:]
        return value

    def number(self, n):
        return int.from_bytes(self.take(n), 'big')

    def vector(self, n):
        return self.take(self.number(n))


def grease(n):
    return -1 if n & 0x0f0f == 0x0a0a else n


def words(raw):
    if len(raw) % 2:
        raise ValueError('invalid_tls_vector')
    return [grease(int.from_bytes(raw[i:i+2], 'big')) for i in range(0, len(raw), 2)]


def parse_hello(raw):
    c = Cursor(raw)
    if c.number(1) != 1 or c.number(3) != len(raw) - 4:
        raise ValueError('not_client_hello')
    legacy = c.number(2)
    c.take(32)
    c.vector(1)
    ciphers = words(c.vector(2))
    compression = list(c.vector(1))
    ext = Cursor(c.vector(2))
    if c.data:
        raise ValueError('invalid_client_hello_tail')
    order, details = [], {}
    while ext.data:
        kind = grease(ext.number(2))
        v = Cursor(ext.vector(2))
        order.append(kind)
        key = str(kind)
        if kind in (10, 13):
            details[key] = words(v.vector(2))
        elif kind in (11, 45):
            details[key] = list(v.vector(1))
        elif kind == 43:
            details[key] = words(v.vector(1))
        elif kind == 16:
            a = Cursor(v.vector(2))
            details[key] = []
            while a.data:
                protocol = a.vector(1)
                if protocol not in (b'h2', b'http/1.1'):
                    raise ValueError('unknown_alpn')
                details[key].append(protocol.decode('ascii'))
        elif kind == 51:
            a = Cursor(v.vector(2))
            details[key] = []
            while a.data:
                details[key].append(grease(a.number(2)))
                a.vector(2)  # Never retain key exchange material.
    return {'evidence': 'native_default_tls_offer_local', 'legacy_version': legacy,
            'cipher_suites': ciphers, 'compression_methods': compression,
            'extension_order': order, 'extensions': details}


def certificate(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    cert, key_path = directory / 'inspection-ca.pem', directory / 'inspection-key.pem'
    leaf_path = directory / 'inspection-server.pem'
    if cert.exists() and key_path.exists() and leaf_path.exists():
        return cert, key_path, leaf_path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Local alignment inspection only')])
    now = datetime.datetime.now(datetime.timezone.utc)
    value = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
             .not_valid_after(now + datetime.timedelta(days=365))
             .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
             .sign(key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'chatgpt.com')]))
            .issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('chatgpt.com')]), critical=False)
            .sign(key, hashes.SHA256()))
    key_path.touch(mode=0o600)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    cert.write_bytes(value.public_bytes(serialization.Encoding.PEM))
    leaf_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    return cert, key_path, leaf_path


def read_head(stream):
    lines, count = [], 0
    while True:
        line = stream.readline(65537)
        count += len(line)
        if not line or count > 65536 or not line.endswith(b'\r\n'):
            raise ValueError('invalid_http_head')
        if line == b'\r\n':
            break
        lines.append(line[:-2].decode('latin1'))
    if not lines:
        raise ValueError('empty_http_head')
    parts = lines[0].split(' ')
    if len(parts) != 3 or parts[2] != 'HTTP/1.1':
        raise ValueError('http11_required')
    pairs = []
    for line in lines[1:]:
        key, sep, value = line.partition(':')
        if not sep or not key or key[0].isspace():
            raise ValueError('invalid_http_header')
        pairs.append((key, value.strip()))
    return parts[0], parts[1], pairs


class Capture:
    def __init__(self, proxy_url, secret, output, cert_dir, max_requests=64, hello_only=False, budget=None):
        self.proxy_url, self.secret, self.output = proxy_url, secret, Path(output)
        self.cert, key, leaf = certificate(cert_dir)
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(leaf, key)
        self.tls.set_alpn_protocols(['http/1.1'])
        self.max_requests, self.hello_only = max_requests, hello_only
        self.lock, self.requests, self.hellos, self.errors = threading.Lock(), [], [], []
        self.count = 0
        self.budget = budget
        self.websocket_refusals = 0
        self.auth_tunnels = 0
        self.refusals = []
        self.connection_closures = []
        self.decode_failures = []
        self.sockets = set()

    def reserve(self):
        with self.lock:
            if self.count >= self.max_requests:
                raise ValueError('native_request_budget_exhausted')
            if self.budget is not None:
                self.budget.reserve()
            self.count += 1
            return self.count

    def handle(self, conn):
        conn.settimeout(120)
        with self.lock:
            self.sockets.add(conn)
        try:
            # Unbuffered prevents CONNECT parsing from swallowing TLS bytes.
            stream = conn.makefile('rb', buffering=0)
            method, target, _ = read_head(stream)
            if method == 'CONNECT' and target in {'auth.openai.com:443', 'auth0.openai.com:443'}:
                # OAuth refresh remains end-to-end TLS. Never inspect or persist
                # credentials. Count bounded tunnels separately from HTTP probes.
                with self.lock:
                    if self.auth_tunnels >= 2:
                        raise ValueError('auth_tunnel_budget_exhausted')
                    self.auth_tunnels += 1
                with socks_connect(self.proxy_url, target.split(':')[0], 443) as upstream:
                    conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    deadline, total = time.monotonic() + 60, 0
                    while time.monotonic() < deadline:
                        ready, _, _ = select.select([conn, upstream], [], [], min(5, max(0, deadline - time.monotonic())))
                        for source in ready:
                            chunk = source.recv(65536)
                            if not chunk:
                                return
                            total += len(chunk)
                            if total > 2 << 20:
                                raise ValueError('auth_tunnel_byte_limit')
                            (upstream if source is conn else conn).sendall(chunk)
                return
            if method != 'CONNECT' or target.lower() != 'chatgpt.com:443':
                raise ValueError('destination_not_allowed')
            conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            if self.hello_only:
                handshake = bytearray()
                while len(handshake) < 131072:
                    head = exact(conn, 5)
                    if head[0] != 22:
                        raise ValueError('not_tls_handshake')
                    size = int.from_bytes(head[3:], 'big')
                    if size > 18432:
                        raise ValueError('tls_record_too_large')
                    handshake.extend(exact(conn, size))
                    if len(handshake) >= 4 and len(handshake) >= 4 + int.from_bytes(handshake[1:4], 'big'):
                        hello = parse_hello(bytes(handshake))
                        with self.lock:
                            self.hellos.append(hello)
                            atomic_json(self.output / 'native-default-tls.json', hello)
                        return
                raise ValueError('client_hello_too_large')
            with self.tls.wrap_socket(conn, server_side=True) as client:
                with self.lock:
                    self.sockets.discard(conn)
                    self.sockets.add(client)
                stream = client.makefile('rb', buffering=0)
                method, path, pairs = read_head(stream)
                clean_path = urlsplit(path).path
                allowed = method == 'POST' and clean_path in ALLOWED_POST or method == 'GET' and clean_path in ALLOWED_GET
                headers = {k.lower(): v for k, v in pairs}
                if method == 'GET' and clean_path == '/backend-api/codex/responses' and headers.get('upgrade', '').lower() == 'websocket':
                    # Official built-in providers no longer honor the legacy WS
                    # feature flags. Force their documented HTTP fallback by
                    # refusing the local upgrade; no WS reaches the provider.
                    with self.lock:
                        self.websocket_refusals += 1
                    client.sendall(b'HTTP/1.1 426 Upgrade Required\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    return
                if not allowed or not path.startswith('/') or headers.get('host') != 'chatgpt.com':
                    with self.lock:
                        self.refusals.append({'method': method if method in {'GET', 'POST', 'PUT', 'CONNECT'} else 'other',
                                              'known_path': clean_path if clean_path in ALLOWED_GET | ALLOWED_POST else 'other',
                                              'path_shape': '/'.join(part if len(part) <= 24 and part.replace('-', '').replace('_', '').isalpha() else '<segment>'
                                                                     for part in clean_path.split('/')),
                                              'origin_form': path.startswith('/'),
                                              'host_has_default_port': headers.get('host') == 'chatgpt.com:443',
                                              'host_exact': headers.get('host') == 'chatgpt.com'})
                    client.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    raise ValueError('request_not_allowed')
                if any(k in headers for k in ('transfer-encoding', 'upgrade', 'expect')):
                    raise ValueError('unsupported_request_framing')
                if len([k for k, _ in pairs if k.lower() == 'content-length']) > 1:
                    raise ValueError('ambiguous_content_length')
                length = int(headers.get('content-length', '0'))
                if not 0 <= length <= MAX_BODY:
                    raise ValueError('request_body_too_large')
                body = bytearray()
                while len(body) < length:
                    piece = stream.read(length - len(body))
                    if not piece:
                        raise ValueError('incomplete_request_body')
                    body.extend(piece)
                raw = bytes(body)
                try:
                    decoded = decode_body(pairs, raw)
                except Exception:
                    import zstandard
                    try:
                        frame = zstandard.get_frame_parameters(raw)
                        self.decode_failures.append({'window_size': frame.window_size,
                                                     'content_size': frame.content_size, 'dict_id': frame.dict_id})
                    except zstandard.ZstdError:
                        self.decode_failures.append({'valid_zstd_frame': False})
                    raise
                number = self.reserve()
                record = http_evidence(self.secret, method, clean_path, pairs, decoded, len(raw),
                                       'native_http_custom_ca_inspection')
                atomic_json(self.output / ('native-%03d-http.json' % number), record)
                # Raw synthetic payloads live in memory only for exact replay.
                with self.lock:
                    self.requests.append({'method': method, 'path': clean_path, 'pairs': pairs,
                                          'body': decoded, 'evidence': record, 'status': None})
                    entry = self.requests[-1]
                with socks_connect(self.proxy_url, 'chatgpt.com', 443) as upstream:
                    ctx = ssl.create_default_context()
                    ctx.set_alpn_protocols(['http/1.1'])
                    with ctx.wrap_socket(upstream, server_hostname='chatgpt.com') as tls:
                        wire = ('%s %s HTTP/1.1\r\n' % (method, path)).encode('ascii')
                        wire += b''.join((k + ': ' + v + '\r\n').encode('latin1') for k, v in pairs
                                         if k.lower() not in {'connection', 'proxy-connection', 'proxy-authorization'})
                        tls.sendall(wire + b'Connection: close\r\n\r\n' + raw)
                        response = http.client.HTTPResponse(tls)
                        response.begin()
                        entry['status'] = response.status
                        client.sendall(('HTTP/1.1 %d Response\r\n' % response.status).encode())
                        for k, v in response.getheaders():
                            if k.lower() not in {'connection', 'transfer-encoding', 'content-length'}:
                                client.sendall((k + ': ' + v + '\r\n').encode('latin1'))
                        # Explicit framing is essential: rustls correctly rejects
                        # a close-delimited body when the peer closes TCP without
                        # TLS close_notify. Never use TLS EOF as HTTP completion.
                        client.sendall(b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
                        received, response_body = 0, bytearray()
                        while True:
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            received += len(chunk)
                            if received > 8 << 20:
                                raise ValueError('response_too_large')
                            response_body.extend(chunk)
                            client.sendall(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
                        client.sendall(b'0\r\n\r\n')
                        entry['response'] = completion(response.status, response.getheader('Content-Type', ''),
                                                       bytes(response_body), clean_path,
                                                       response.getheader('Content-Encoding', ''))
                        atomic_json(self.output / ('native-%03d-response.json' % number), entry['response'])
                        response.close()
        except Exception as exc:
            # Codex can close a warmup connection or stop reading immediately
            # after the SSE terminal event, before TLS/chunked close framing.
            # This never excuses a missing/failed model completion.
            closure = isinstance(exc, (ssl.SSLEOFError, BrokenPipeError, ConnectionResetError))
            if closure and 'entry' in locals():
                if entry['method'] == 'GET':
                    with self.lock:
                        self.connection_closures.append('metadata_connection_closed')
                    return
                if 'response_body' in locals():
                    finished = completion(entry['status'], response.getheader('Content-Type', ''),
                                          bytes(response_body), clean_path,
                                          response.getheader('Content-Encoding', ''))
                    if finished['complete']:
                        entry['response'] = finished
                        atomic_json(self.output / ('native-%03d-response.json' % number), finished)
                        with self.lock:
                            self.connection_closures.append('closed_after_terminal_event')
                        return
            elif closure and locals().get('method') == 'CONNECT' and locals().get('target') == 'chatgpt.com:443':
                with self.lock:
                    self.connection_closures.append('closed_before_http_request')
                return
            # Do not log exception strings: TLS/HTTP libraries may embed inputs.
            with self.lock:
                safe_codes = {'destination_not_allowed', 'request_not_allowed', 'unsupported_request_framing',
                              'auth_tunnel_budget_exhausted', 'auth_tunnel_byte_limit',
                              'ambiguous_content_length', 'request_body_too_large', 'incomplete_request_body',
                              'native_request_budget_exhausted', 'native_run_request_budget_exhausted',
                              'invalid_http_head', 'http11_required', 'invalid_http_header', 'empty_http_head',
                              'unsupported_content_encoding', 'body_too_large', 'response_too_large'}
                self.errors.append(str(exc) if type(exc) is ValueError and str(exc) in safe_codes else type(exc).__name__)
        finally:
            with self.lock:
                self.sockets.discard(conn)
                if 'client' in locals():
                    self.sockets.discard(client)
            conn.close()

    @contextmanager
    def serve(self):
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner.handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        server = Server(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield 'http://127.0.0.1:%d' % server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()
            with self.lock:
                for conn in self.sockets:
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            thread.join(timeout=5)


class Budget:
    def __init__(self, limit):
        self.limit, self.count, self.lock = limit, 0, threading.Lock()

    def reserve(self):
        with self.lock:
            if self.count >= self.limit:
                raise ValueError('native_run_request_budget_exhausted')
            self.count += 1
