"""Python HTTPS traffic uses the same fixed SOCKS exit as the inspected CLI.

The service's IPAddressDeny/Allow policy additionally blocks direct egress from
native child processes. DNS for remote HTTPS targets occurs at the SOCKS exit.
"""
import http.client
import ssl
from urllib.request import HTTPSHandler, HTTPHandler, ProxyHandler, build_opener, install_opener

from .capture import socks_connect

HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
         'objects.githubusercontent.com', 'ilinkai.weixin.qq.com'}


def configure(proxy_url):
    class Connection(http.client.HTTPSConnection):
        def connect(self):
            if self.host not in HOSTS or self.port != 443 or self._tunnel_host:
                raise ValueError('monitor_https_destination_not_allowed')
            sock = socks_connect(proxy_url, self.host, self.port, timeout=self.timeout)
            try:
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise

    class Handler(HTTPSHandler):
        def https_open(self, req):
            return self.do_open(Connection, req, context=ssl.create_default_context())

    class NoHTTP(HTTPHandler):
        def http_open(self, req):
            raise ValueError('plaintext_external_http_forbidden')

    install_opener(build_opener(ProxyHandler({}), Handler(), NoHTTP()))
