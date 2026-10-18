"""Interactive official device login through the configured static exit."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import subprocess
import threading

from .capture import read_head, socks_connect


@contextmanager
def login_proxy(proxy_url):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.request.settimeout(60)
                stream = self.request.makefile('rb', buffering=0)
                method, target, _ = read_head(stream)
                allowed = {'auth.openai.com:443', 'auth0.openai.com:443', 'chatgpt.com:443', 'api.openai.com:443'}
                if method != 'CONNECT' or target not in allowed:
                    return
                with socks_connect(proxy_url, target.split(':')[0], 443) as upstream:
                    self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    while True:
                        ready, _, _ = select.select([self.request, upstream], [], [], 60)
                        if not ready:
                            return
                        for source in ready:
                            chunk = source.recv(65536)
                            if not chunk:
                                return
                            (upstream if source is self.request else self.request).sendall(chunk)
            except Exception:
                pass

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d' % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    cfg = json.loads(Path('/etc/sub2api-alignment/config.json').read_text())
    home = Path(cfg['native_home'])
    versions = [p for p in (Path(cfg['state_dir']) / 'binaries').iterdir() if (p / 'codex').is_file()]
    binary = max(versions, key=lambda p: tuple(int(x) for x in p.name.split('.'))) / 'codex'
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home / 'home'),
           'CODEX_HOME': str(home), 'LANG': 'C.UTF-8', 'TERM': os.environ.get('TERM', 'xterm')}
    with login_proxy(cfg['proxy_url']) as proxy:
        env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, ALL_PROXY=proxy, NO_PROXY='')
        return subprocess.call([str(binary), '-c', 'cli_auth_credentials_store="file"', 'login', '--device-auth'],
                               env=env, cwd=home)


if __name__ == '__main__':
    raise SystemExit(main())
