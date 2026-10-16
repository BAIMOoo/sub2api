#!/usr/bin/env python3
"""Fixed Tencent API entry lifecycle and TLS/idle-WebSocket acceptance.

Default is offline. Only explicit install/start modes touch the Tencent unit.
Verify runs from AWS to Tencent IP with domain SNI/Host and never sends a model
message. Real SSE and billing acceptance belong to model_canary.py's TLS lane.
"""
import argparse
import base64
import hashlib
import http.client
import json
import os
import pathlib
import runpy
import socket
import ssl
import stat
import struct
import subprocess
import sys
import time

RUN = "20261016"
DOMAIN, TARGET_IP = "api.b4im.com", "43.165.177.136"
ENTRY = pathlib.Path("/home/ubuntu/sub2api-migration-20261016/entry")
CERT_DIR = ENTRY / "data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/api.b4im.com"
UNIT_NAME = "sub2api-tencent-entry.service"
UNIT_PATH = pathlib.Path("/etc/systemd/system") / UNIT_NAME
CADDY_SHA = "9681d054b6e5137d53f6d027188b349e0603fd12ea6334bd0d49da6755432f1b"
CONFIG_SHA = "077de5dac0181a7319c724e9131727bca6b0357e86762442f27308b8eaf56f45"
UNIT_SHA = "0ea178533c2522b39136eb17da18471896201b15930a4b8e35a38c4788e6e893"
CERT_HASHES = {
    "api.b4im.com.crt": "3bc3fa3eb21690d6d5ab8dffd15f82e42e2db09a181a379c062e9fa5795d6d4f",
    "api.b4im.com.key": "c686fa6ae9e6b691ea9c9f6934509dc1ee61195795e12068fdd22a181a6a1e90",
    "api.b4im.com.json": "34e8e6ce462bf405898e69ee93ef2d0bff51f4cf7fad9092dae982fead76147f",
}
LEAF_SHA = "d85ae437e821b8ad794a19e001019d21fcbd8ca6b54cfa658385652fd33c4d09"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
if __name__ != "_entry_remote":
    STAGE = runpy.run_path(str(pathlib.Path(__file__).with_name("stage_target_entry.py")))
    CONFIG, UNIT = STAGE["CONFIG"], STAGE["UNIT"]


class Refusal(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise Refusal(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def trusted_file(path, owner, mode):
    need(not any(item.is_symlink() for item in [path, *path.parents]), "ENTRY_SYMLINK_REFUSED")
    for parent in path.parents:
        info = parent.stat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, 1000) and not info.st_mode & 0o022,
             "ENTRY_PARENT_UNTRUSTED")
    info = path.stat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == owner and info.st_nlink == 1 and
         stat.S_IMODE(info.st_mode) == mode, "ENTRY_FILE_UNTRUSTED")
    return path.read_bytes()


def command(argv, timeout=5):
    try:
        result = subprocess.run(argv, input=b"", capture_output=True, timeout=timeout,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("ENTRY_COMMAND_UNKNOWN_INSPECT_NO_RESTART") from None
    need(result.returncode == 0, "ENTRY_COMMAND_FAILED_NO_SECRET_OUTPUT")
    return result.stdout


def staged():
    need(sha(CONFIG.encode()) == CONFIG_SHA and sha(UNIT.encode()) == UNIT_SHA, "ENTRY_DEFINITION_CHANGED")
    expected = [(ENTRY / "caddy", CADDY_SHA, 0o755),
                (ENTRY / "Caddyfile", CONFIG_SHA, 0o600),
                (ENTRY / UNIT_NAME, UNIT_SHA, 0o600)]
    expected.extend((CERT_DIR / name, digest, 0o600) for name, digest in CERT_HASHES.items())
    for path, digest, mode in expected:
        need(sha(trusted_file(path, 1000, mode)) == digest, "ENTRY_STAGED_HASH_CHANGED")
    # Fixed certificate and key must still form a pair and cover the hostname.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(CERT_DIR / "api.b4im.com.crt"), str(CERT_DIR / "api.b4im.com.key"))
    command(["/usr/bin/openssl", "x509", "-in", str(CERT_DIR / "api.b4im.com.crt"),
             "-noout", "-checkhost", DOMAIN])
    command(["/usr/bin/openssl", "x509", "-in", str(CERT_DIR / "api.b4im.com.crt"),
             "-noout", "-checkend", "604800"])
    return {"caddy_sha256": CADDY_SHA, "config_sha256": sha(CONFIG.encode()),
            "unit_sha256": sha(UNIT.encode()), "certificate_sha256": CERT_HASHES["api.b4im.com.crt"]}


def unit_status():
    names = {"LoadState", "ActiveState", "SubState", "MainPID", "Job", "FragmentPath", "DropInPaths", "NeedDaemonReload"}
    raw = command(["/usr/bin/systemctl", "show", UNIT_NAME, "--property=" + ",".join(sorted(names))])
    result = {}
    for line in raw.decode().splitlines():
        key, value = line.split("=", 1)
        need(key not in result, "ENTRY_UNIT_STATUS_DUPLICATE")
        result[key] = value
    need(set(result) == names and result["MainPID"].isdigit(), "ENTRY_UNIT_STATUS_INVALID")
    need(not result["DropInPaths"] and not result["Job"], "ENTRY_UNIT_OVERRIDE_OR_JOB")
    return result


def installed(status):
    need(status["LoadState"] == "loaded" and status["FragmentPath"] == str(UNIT_PATH) and
         status["NeedDaemonReload"] == "no", "ENTRY_UNIT_NOT_LOADED_EXACTLY")
    need(trusted_file(UNIT_PATH, 0, 0o644) == UNIT.encode(), "ENTRY_INSTALLED_UNIT_CHANGED")


def remote_inspect():
    return {"v": 1, "run": RUN, "staged": staged(), "unit": unit_status(),
            "source_actions": False, "dns_changed": False}


def remote_install():
    need(sys.platform == "linux" and os.geteuid() == 0, "ENTRY_ROOT_LINUX_REQUIRED")
    proof = staged()
    status = unit_status()
    need(status["MainPID"] == "0" and status["ActiveState"] == "inactive", "ENTRY_INSTALL_REQUIRES_INACTIVE")
    if UNIT_PATH.exists() or UNIT_PATH.is_symlink():
        need(trusted_file(UNIT_PATH, 0, 0o644) == UNIT.encode(), "ENTRY_EXISTING_UNIT_DIFFERS")
    else:
        need(status["LoadState"] == "not-found" and not status["FragmentPath"], "ENTRY_FOREIGN_UNIT_EXISTS")
        need(not any(item.is_symlink() for item in UNIT_PATH.parents), "ENTRY_UNIT_PARENT_SYMLINK")
        fd = os.open(UNIT_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(UNIT.encode()); handle.flush(); os.fsync(handle.fileno())
        directory = os.open(UNIT_PATH.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    command(["/usr/bin/systemctl", "daemon-reload"])
    status = unit_status(); installed(status)
    need(status["MainPID"] == "0" and status["ActiveState"] == "inactive", "ENTRY_INSTALL_UNEXPECTEDLY_ACTIVE")
    return {"v": 1, "run": RUN, "installed": True, "started": False, "enabled": False,
            "proof": proof, "source_actions": False, "dns_changed": False}


def upstream_health():
    conn = http.client.HTTPConnection("127.0.0.1", 8082, timeout=3)
    try:
        conn.request("GET", "/health", headers={"Connection": "close"})
        response = conn.getresponse(); body = response.read(65537)
        need(response.status == 200 and len(body) <= 65536, "ENTRY_UPSTREAM_NOT_READY")
    finally:
        conn.close()


def require_public_ports_free():
    raw = command(["/usr/bin/ss", "-H", "-lntu"])
    for line in raw.decode().splitlines():
        fields = line.split()
        need(len(fields) >= 6, "ENTRY_LISTENER_STATUS_INVALID")
        need(fields[4].rsplit(":", 1)[-1] not in ("80", "443"), "ENTRY_PORT_ALREADY_IN_USE")


def remote_start():
    need(sys.platform == "linux" and os.geteuid() == 0, "ENTRY_ROOT_LINUX_REQUIRED")
    proof = staged(); status = unit_status(); installed(status)
    upstream_health()
    if status["ActiveState"] == "active" and status["SubState"] == "running" and int(status["MainPID"]) > 0:
        return {"v": 1, "run": RUN, "started": True, "already_running": True, "proof": proof}
    need(status["MainPID"] == "0" and status["ActiveState"] == "inactive", "ENTRY_START_STATE_UNCERTAIN")
    require_public_ports_free()
    # No restart/reload/enable command exists. A lost start reply is inspected.
    command(["/usr/bin/systemctl", "start", UNIT_NAME], timeout=8)
    status = unit_status(); installed(status)
    need(status["ActiveState"] == "active" and status["SubState"] == "running" and int(status["MainPID"]) > 0,
         "ENTRY_START_NOT_CONFIRMED_INSPECT")
    return {"v": 1, "run": RUN, "started": True, "proof": proof, "source_actions": False, "dns_changed": False}


def tls_socket():
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    raw = socket.create_connection((TARGET_IP, 443), timeout=4)
    sock = None
    try:
        sock = context.wrap_socket(raw, server_hostname=DOMAIN)
        need(sha(sock.getpeercert(binary_form=True)) == LEAF_SHA, "ENTRY_TLS_LEAF_CHANGED")
        return sock
    except BaseException:
        (sock if sock is not None else raw).close()
        raise


class EntryConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__(DOMAIN, 443, timeout=4)

    def connect(self):
        self.sock = tls_socket()


def tls_health():
    conn = EntryConnection()
    try:
        conn.request("GET", "/health", headers={"Connection": "close"})
        response = conn.getresponse(); body = response.read(65537)
        need(response.status == 200 and len(body) <= 65536, "ENTRY_TLS_HEALTH_FAILED")
        return {"http_status": 200, "server_name": DOMAIN, "target_ip": TARGET_IP,
                "full_chain_and_hostname_verified": True, "leaf_der_sha256": LEAF_SHA}
    finally:
        conn.close()


def ws_send(sock, opcode, payload):
    need(opcode in (8, 9, 10) and len(payload) <= 125, "ENTRY_WS_CONTROL_ONLY")
    mask = os.urandom(4)
    sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask +
                 bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))


def exact_read(handle, count):
    value = handle.read(count)
    need(len(value) == count, "ENTRY_WS_FRAME_TRUNCATED")
    return value


def ws_control(sock, handle):
    for _ in range(16):
        a, b = exact_read(handle, 2)
        opcode, size = a & 15, b & 127
        need(a & 0x80 and not a & 0x70 and not b & 0x80 and size <= 125 and opcode in (8, 9, 10),
             "ENTRY_WS_UNEXPECTED_FRAME")
        value = exact_read(handle, size)
        if opcode == 9:
            ws_send(sock, 10, value)
        else:
            return opcode, value
    raise Refusal("ENTRY_WS_TOO_MANY_CONTROL_FRAMES")


def verify_websocket(token):
    need(type(token) is str and 16 <= len(token) <= 128 and token.isascii() and
         not any(char in token for char in "\r\n\x00"), "ENTRY_TOKEN_INVALID")
    sock = tls_socket(); handle = sock.makefile("rb")
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        request = ("GET /v1/responses HTTP/1.1\r\nHost: " + DOMAIN +
                   "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key +
                   "\r\nAuthorization: Bearer " + token + "\r\nUser-Agent: sub2api-entry-verification/20261016\r\n\r\n")
        sock.sendall(request.encode("ascii"))
        need(handle.readline(8193).startswith(b"HTTP/1.1 101 "), "ENTRY_WS_UPGRADE_FAILED")
        headers, size = {}, 0
        while True:
            line = handle.readline(8193); size += len(line)
            need(line and len(line) <= 8192 and size <= 16384, "ENTRY_WS_HEADERS_INVALID")
            if line == b"\r\n":
                break
            name, value = line.decode("ascii").split(":", 1)
            name = name.lower()
            need(name not in headers, "ENTRY_WS_HEADER_DUPLICATE")
            headers[name] = value.strip()
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        need(headers.get("sec-websocket-accept") == accept and headers.get("upgrade", "").lower() == "websocket" and
             "upgrade" in [value.strip().lower() for value in headers.get("connection", "").split(",")], "ENTRY_WS_ACCEPT_INVALID")
        ping = b"sub2api-entry-20261016"
        ws_send(sock, 9, ping)
        need(ws_control(sock, handle) == (10, ping), "ENTRY_WS_PONG_FAILED")
        ws_send(sock, 8, struct.pack("!H", 1000))
        opcode, payload = ws_control(sock, handle)
        need(opcode == 8 and len(payload) >= 2 and struct.unpack("!H", payload[:2])[0] == 1000,
             "ENTRY_WS_CLOSE_FAILED")
        return {"upgrade_status": 101, "pong_verified": True, "normal_close_code": 1000,
                "model_messages_sent": 0, "tls_verified": True}
    finally:
        handle.close(); sock.close()


def remote_verify(token):
    return {"v": 1, "run": RUN, "health": tls_health(), "websocket": verify_websocket(token),
            "model_request_sent": False, "source_actions": False, "dns_changed": False, "checked_at": time.time()}


def remote_source(mode, token=None):
    need(mode in ("inspect", "install", "start", "verify"), "ENTRY_UNKNOWN_MODE")
    need(sha(CONFIG.encode()) == CONFIG_SHA and sha(UNIT.encode()) == UNIT_SHA, "ENTRY_DEFINITION_CHANGED")
    source = pathlib.Path(__file__).read_text()
    call = "remote_verify(" + repr(token) + ")" if mode == "verify" else "remote_" + mode + "()"
    return ("__name__='_entry_remote'\nCONFIG=" + repr(CONFIG) + "\nUNIT=" + repr(UNIT) + "\n" + source +
            "\ntry:\n result=" + call + "\n print(json.dumps(result,sort_keys=True),flush=True)\n" +
            "except Exception as exc:\n print(json.dumps({'ok':False,'error':str(exc) if isinstance(exc,Refusal) else 'ENTRY_RESULT_UNKNOWN_NO_SECRET_OUTPUT'}),flush=True)\n raise SystemExit(1)\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--apply", choices=("install", "start", "verify"))
    args = parser.parse_args(argv)
    if not args.inspect and not args.apply:
        print(json.dumps({"offline": True, "network_access": False, "source_actions": False,
                          "dns_changed": False, "modes": ["inspect", "install", "start", "verify"],
                          "model_messages_sent": 0, "caddy_sha256": CADDY_SHA}))
        return 0
    selected = "inspect" if args.inspect else args.apply
    token = None
    if selected == "verify":
        canary = runpy.run_path(str(pathlib.Path(__file__).with_name("model_canary.py")))
        token = canary["load_token"]()
    ssh = STAGE["CHANNEL"]["SSH_SOURCE" if selected == "verify" else "TARGET"]
    remote = "python3 -" if selected in ("verify", "inspect") else "sudo -n python3 -"
    try:
        result = subprocess.run(ssh + [remote], input=remote_source(selected, token).encode(),
                                capture_output=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("ENTRY_SSH_RESULT_UNKNOWN_INSPECT_NO_RESTART") from None
    need(result.returncode == 0, "ENTRY_REMOTE_FAILED_NO_SECRET_OUTPUT")
    report = json.loads(result.stdout)
    need(not token or token not in json.dumps(report), "ENTRY_SECRET_IN_REPORT_REFUSED")
    directory = pathlib.Path.home() / ".codex/backups" / ("sub2api-entry-" + selected + "-" + str(time.time_ns()))
    directory.mkdir(mode=0o700, parents=True)
    path = directory / "report.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(report, handle, sort_keys=True); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    print(json.dumps({**report, "local_report": str(path), "report_sha256": sha(path.read_bytes())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refusal) else "ENTRY_RESULT_UNKNOWN_NO_SECRET_OUTPUT"}), file=sys.stderr)
        raise SystemExit(1)
