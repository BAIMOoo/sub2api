#!/usr/bin/env python3
"""Review-first, isolated new-TCP bridge rehearsal; dry-run unless --apply.

Never changes production Caddy, its unit, Docker services, or production ports.
Remote watchdogs clean only this run's exact NAT rule, test unit and labelled
test containers, even when the initiating SSH/local process disappears.
"""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import pathlib
import pwd
import re
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

SOURCE_PUBLISHED = 28081
CADDY_LISTENER = 28080
BRIDGE_LISTENER = 18081
TARGET_PUBLISHED = 8082
CONTAINER_LISTENER = 8080
PROTECTED_PORTS = frozenset({80, 443, 8081, 8787})
LABEL = "sub2api.migration.bridge.run"
SCOPE_PREFIX = "/var/tmp/sub2api-bridge-rehearsal-"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def safety_invariants():
    if PROTECTED_PORTS.intersection({SOURCE_PUBLISHED, CADDY_LISTENER, BRIDGE_LISTENER, TARGET_PUBLISHED, CONTAINER_LISTENER}):
        raise RuntimeError("refuse a production port in rehearsal constants")
    if (SOURCE_PUBLISHED, CADDY_LISTENER, BRIDGE_LISTENER, TARGET_PUBLISHED) != (28081, 28080, 18081, 8082):
        raise RuntimeError("refuse changed rehearsal ports without a new reviewed script")


def command(argv, *, timeout=20, check=True, data=None, env=None, cwd=None):
    result = subprocess.run(argv, input=data, text=True, capture_output=True,
                            timeout=timeout, env=env, cwd=cwd)
    if check and result.returncode:
        raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {result.stderr.strip()[:1000]}")
    return result


def json_write(path, value):
    temporary = path.with_suffix(".pending")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run_names(run_id, role):
    if not re.fullmatch(r"[0-9a-f]{16}", run_id) or role not in {"source", "target"}:
        raise ValueError("invalid isolated run identity")
    return {
        "container": f"sub2api-bridge-{role}-{run_id}",
        "caddy_unit": f"sub2api-bridge-caddy-{run_id}.service",
        "watchdog_unit": f"sub2api-bridge-watchdog-{role}-{run_id}.service",
        "comment": f"sub2api-bridge-{run_id}",
    }


def scope_state(scope):
    scope = pathlib.Path(scope)
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("remote operations require Linux root through sudo -n")
    if not re.fullmatch(re.escape(SCOPE_PREFIX) + r"[0-9a-f]{16}", str(scope)):
        raise RuntimeError("refuse a non-rehearsal scope")
    if scope.is_symlink() or scope.resolve() != scope or scope.stat().st_uid != 0:
        raise RuntimeError("refuse an unowned or redirected rehearsal scope")
    state = json.loads((scope / "state.json").read_text())
    if str(scope) != SCOPE_PREFIX + state["run_id"]:
        raise RuntimeError("scope and run identity differ")
    run_names(state["run_id"], state["role"])
    return scope, state


@contextlib.contextmanager
def resources(scope, *, allow_cleanup=False):
    scope, _ = scope_state(scope)
    with (scope / "resources.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _, state = scope_state(scope)
        if not allow_cleanup and (scope / "cleanup.started").exists():
            raise RuntimeError("watchdog cleanup has begun; refuse new resources")
        yield scope, state


def unit_property(unit, name):
    return command(["systemctl", "show", unit, "--property=" + name, "--value"], check=False, timeout=5).stdout.strip()


def production_snapshot(run_id):
    ids = command(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"], timeout=8)
    containers = {}
    if ids.stdout.split():
        inspected = json.loads(command(["docker", "inspect", *ids.stdout.split()], timeout=8).stdout)
        for item in inspected:
            if (item.get("Config", {}).get("Labels") or {}).get(LABEL) == run_id:
                continue
            status = item["State"]
            containers[item["Name"].lstrip("/")] = {
                "id": item["Id"], "running": status["Running"],
                "health": status.get("Health", {}).get("Status", "not-configured"),
            }
    return {"containers": containers,
            "caddy_main_pid": unit_property("caddy.service", "MainPID") or "0",
            "caddy_active": unit_property("caddy.service", "ActiveState")}


def unchanged(before, after):
    return (before["caddy_main_pid"] == after["caddy_main_pid"]
            and before["caddy_active"] == after["caddy_active"]
            and all(after["containers"].get(name) == value
                    for name, value in before["containers"].items()))


def nat_rule(uid, run_id):
    if not isinstance(uid, int) or uid <= 0:
        raise ValueError("the test upstream owner must be the non-root caddy UID")
    return ["-p", "tcp", "-d", "127.0.0.1/32", "--dport", str(SOURCE_PUBLISHED),
            "-m", "owner", "--uid-owner", str(uid),
            "-m", "conntrack", "--ctstate", "NEW", "-m", "comment", "--comment",
            run_names(run_id, "source")["comment"], "-j", "REDIRECT", "--to-ports", str(BRIDGE_LISTENER)]


def iptables(action, rule, *, check=True):
    if action not in {"-I", "-C", "-D"}:
        raise ValueError("only exact insertion, inspection or deletion is allowed")
    argv = ["iptables", "-w", "5", "-t", "nat", action, "OUTPUT"]
    if action == "-I":
        argv.append("1")
    return command(argv + rule, check=check, timeout=10)


def ensure_rule_first(rule, run_id):
    lines = command(["iptables", "-w", "5", "-t", "nat", "-S", "OUTPUT"]).stdout.splitlines()
    rules = [line for line in lines if line.startswith("-A OUTPUT ")]
    if not rules or run_names(run_id, "source")["comment"] not in rules[0]:
        raise RuntimeError("the exact rehearsal REDIRECT is not before Docker OUTPUT rules")
    if iptables("-C", rule, check=False).returncode != 0:
        raise RuntimeError("the exact rehearsal rule cannot be verified")
    return rules[0]


def set_redirect(scope, enabled):
    with resources(scope) as (scope, state):
        if state["role"] != "source":
            raise RuntimeError("NAT is source-only")
        rule = nat_rule(state["caddy_uid"], state["run_id"])
        present = iptables("-C", rule, check=False)
        if present.returncode not in {0, 1}:
            raise RuntimeError("cannot inspect the exact rehearsal NAT rule")
        if enabled:
            if present.returncode == 0:
                raise RuntimeError("unique run rule already exists; refuse duplicate insertion")
            iptables("-I", rule)
            evidence = ensure_rule_first(rule, state["run_id"])
        else:
            if present.returncode == 0:
                iptables("-D", rule)
            evidence = "exact rule absent"
        state["rule_installed"] = enabled
        json_write(scope / "state.json", state)
        return evidence


def container_info(name):
    result = command(["docker", "container", "inspect", name], check=False, timeout=8)
    if result.returncode:
        # A missing name is benign; daemon/permission errors are not.
        message = result.stderr.strip().lower()
        expected = ("no such container: " + name.lower(), "no such object: " + name.lower())
        if result.returncode == 1 and result.stdout.strip() == "[]" and any(message.endswith(item) for item in expected):
            return None
        raise RuntimeError("cannot inspect the exact rehearsal container")
    return json.loads(result.stdout)[0]


def owned_container(state):
    name = run_names(state["run_id"], state["role"])["container"]
    item = container_info(name)
    if item is not None and (item.get("Config", {}).get("Labels") or {}).get(LABEL) != state["run_id"]:
        raise RuntimeError("container name belongs to another owner; refuse mutation")
    return item


def check_port_free(port):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def start_container(scope):
    with resources(scope) as (scope, state):
        if owned_container(state) is not None:
            raise RuntimeError("rehearsal container already exists; refuse replacement")
        port = SOURCE_PUBLISHED if state["role"] == "source" else TARGET_PUBLISHED
        check_port_free(port)
        image_id = command(["docker", "image", "inspect", "--format", "{{.Id}}", state["image"]]).stdout.strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise RuntimeError("explicit existing test image cannot be pinned")
        name = run_names(state["run_id"], state["role"])["container"]
        result = command([
            "docker", "run", "-d", "--pull=never", "--name", name,
            "--label", LABEL + "=" + state["run_id"], "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=65534:65534", "--pids-limit=64",
            "--memory=64m", "--cpus=0.5", "--no-healthcheck", "--restart=no",
            "--publish", f"127.0.0.1:{port}:{CONTAINER_LISTENER}",
            "--mount", f"type=bind,src={scope}/bridge_probe,dst=/bridge_probe,readonly",
            "--entrypoint=/bridge_probe", image_id,
            "--listen=0.0.0.0:8080", "--identity=" + state["role"] + "-" + state["run_id"],
        ])
        state["test_container_id"] = result.stdout.strip()
        json_write(scope / "state.json", state)


def caddy_config():
    # Disabling only THIS test upstream's keepalive proves new-TCP steering.
    # Production Caddy's reused upstream connections remain a separate gate.
    return """{
    admin off
    auto_https off
}
http://127.0.0.1:28080 {
    reverse_proxy 127.0.0.1:28081 {
        flush_interval -1
        transport http {
            keepalive off
        }
    }
}
"""


def start_caddy(scope):
    with resources(scope) as (scope, state):
        check_port_free(CADDY_LISTENER)
        if not re.fullmatch(r"v?2\.6\.2(?:\s.*)?", command(["caddy", "version"]).stdout.strip()):
            raise RuntimeError("rehearsal was reviewed for Caddy 2.6.2 only")
        config = scope / "Caddyfile"
        config.write_text(caddy_config())
        config.chmod(0o644)
        account = pwd.getpwnam("caddy")
        for name in ("caddy-home", "caddy-config", "caddy-data"):
            directory = scope / name
            directory.mkdir(mode=0o700, exist_ok=False)
            os.chown(directory, account.pw_uid, account.pw_gid)
        command(["caddy", "validate", "--adapter=caddyfile", "--config", str(config)])
        names = run_names(state["run_id"], "source")
        command(["systemd-run", "--quiet", "--collect", "--unit=" + names["caddy_unit"],
                 "--description=Sub2API isolated bridge Caddy " + state["run_id"],
                 "--property=User=caddy", "--property=Group=caddy",
                 "--property=NoNewPrivileges=yes", "--property=KillMode=control-group",
                 "--setenv=HOME=" + str(scope / "caddy-home"),
                 "--setenv=XDG_CONFIG_HOME=" + str(scope / "caddy-config"),
                 "--setenv=XDG_DATA_HOME=" + str(scope / "caddy-data"),
                 "--property=RuntimeMaxSec=" + str(state["timeout"]),
                 "--property=TimeoutStopSec=10", "/usr/bin/caddy", "run", "--adapter=caddyfile", "--config", str(config)])
        state["test_caddy_pid"] = unit_property(names["caddy_unit"], "MainPID")
        json_write(scope / "state.json", state)


def verify_test_caddy(scope):
    with resources(scope) as (scope, state):
        unit = run_names(state["run_id"], "source")["caddy_unit"]
        pid = unit_property(unit, "MainPID")
        if not pid.isdigit() or pid == "0" or pid == state["baseline"]["caddy_main_pid"]:
            raise RuntimeError("isolated Caddy PID is unavailable or collides with production")
        if unit_property(unit, "User") != "caddy":
            raise RuntimeError("isolated Caddy does not use the production caddy UID")
        status = pathlib.Path("/proc") / pid / "status"
        uid_line = next(line for line in status.read_text().splitlines() if line.startswith("Uid:"))
        if any(int(value) != state["caddy_uid"] for value in uid_line.split()[1:]):
            raise RuntimeError("isolated Caddy process UID differs")
        command_line = (pathlib.Path("/proc") / pid / "cmdline").read_bytes().split(b"\0")
        if str(scope / "Caddyfile").encode() not in command_line:
            raise RuntimeError("isolated Caddy PID does not point to this run's config")
        state["test_caddy_pid"] = pid
        json_write(scope / "state.json", state)
        return {"pid": pid, "uid": state["caddy_uid"], "unit": unit, "config": str(scope / "Caddyfile")}


def cleanup(scope):
    with resources(scope, allow_cleanup=True) as (scope, state):
        (scope / "cleanup.started").touch(mode=0o600, exist_ok=True)
        names = run_names(state["run_id"], state["role"])
        errors = []
        if state["role"] == "source":
            try:
                rule = nat_rule(state["caddy_uid"], state["run_id"])
                present = iptables("-C", rule, check=False)
                if present.returncode == 0:
                    iptables("-D", rule)
                elif present.returncode != 1:
                    raise RuntimeError("cannot inspect the exact rehearsal NAT rule")
                if iptables("-C", rule, check=False).returncode != 1:
                    raise RuntimeError("exact rehearsal NAT rule is not confirmed absent")
            except Exception as exc:
                errors.append("NAT cleanup: " + str(exc))
            try:
                description = unit_property(names["caddy_unit"], "Description")
                pid = unit_property(names["caddy_unit"], "MainPID") or "0"
                if description == "Sub2API isolated bridge Caddy " + state["run_id"]:
                    if pid != "0" and pid == state["baseline"]["caddy_main_pid"]:
                        raise RuntimeError("test PID collides with production Caddy; refuse stop")
                    command(["systemctl", "stop", names["caddy_unit"]], timeout=15)
                elif pid != "0":
                    raise RuntimeError("test unit identity differs; refuse stop")
            except Exception as exc:
                errors.append("Caddy cleanup: " + str(exc))
        try:
            item = owned_container(state)
            if item is not None:
                # Exact immutable ID plus ownership label; never name-pattern kill.
                command(["docker", "rm", "--force", item["Id"]], timeout=8)
        except Exception as exc:
            errors.append("container cleanup: " + str(exc))
        try:
            after = production_snapshot(state["run_id"])
            preserved = unchanged(state["baseline"], after)
            if not preserved:
                errors.append("production IDs/health or Caddy MainPID/state changed")
        except Exception as exc:
            after, preserved = {}, False
            errors.append("post-snapshot: " + str(exc))
        report = {"run_id": state["run_id"], "role": state["role"], "before": state["baseline"],
                  "after": after, "production_unchanged": preserved,
                  "errors": errors, "cleaned_at": time.time()}
        json_write(scope / "cleanup.json", report)
        return report


def identity(port, expected):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        conn.request("GET", "/identity", headers={"Connection": "close"})
        response = conn.getresponse()
        body = response.read(4097)
        if response.status != 200 or len(body) > 4096 or json.loads(body).get("identity") != expected:
            raise RuntimeError("isolated identity check failed at port " + str(port))
        return {"identity": expected, "port": port, "at": time.monotonic()}
    finally:
        conn.close()


def wait_identity(port, expected):
    expires = time.monotonic() + 6
    while True:
        try:
            return identity(port, expected)
        except Exception:
            if time.monotonic() >= expires:
                raise
            time.sleep(0.1)


def sse_client(expected, count, interval_ms, ready=None):
    conn = http.client.HTTPConnection("127.0.0.1", CADDY_LISTENER, timeout=10)
    records, first, done = [], None, False
    try:
        conn.request("GET", f"/sse?count={count}&interval_ms={interval_ms}", headers={"Connection": "close"})
        response = conn.getresponse()
        if response.status != 200 or response.getheader("X-Probe-Identity") != expected:
            raise RuntimeError("SSE handshake identity differs")
        event, data = "", []
        while True:
            line = response.readline(8193)
            if not line:
                break
            if len(line) > 8192:
                raise RuntimeError("unexpected oversized test SSE line")
            line = line.rstrip(b"\r\n")
            if line.startswith(b"event: "):
                event = line[7:].decode()
            elif line.startswith(b"data: "):
                data.append(line[6:])
            elif not line and data:
                item = json.loads(b"\n".join(data))
                if item.get("identity") != expected:
                    raise RuntimeError("SSE changed backend mid-stream")
                if event == "probe":
                    if item.get("sequence") != len(records):
                        raise RuntimeError("SSE sequence missing or duplicated")
                    records.append(item["sequence"])
                    if first is None:
                        first = time.monotonic()
                        if ready:
                            ready.set()
                elif event == "probe_done":
                    done = item.get("count") == count
                else:
                    raise RuntimeError("unknown isolated SSE event")
                event, data = "", []
        if records != list(range(count)) or not done:
            raise RuntimeError("SSE did not end completely")
        return {"identity": expected, "frames": len(records), "complete": done,
                "first_at": first, "finished_at": time.monotonic()}
    finally:
        conn.close()


def ws_send(sock, opcode, payload):
    if len(payload) > 65535:
        raise RuntimeError("test websocket payload too large")
    mask = os.urandom(4)
    header = bytes([0x80 | opcode, 0x80 | len(payload)]) if len(payload) < 126 else bytes([0x80 | opcode, 0xFE]) + struct.pack("!H", len(payload))
    sock.sendall(header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))


def exact_read(handle, size):
    payload = handle.read(size)
    if len(payload) != size:
        raise RuntimeError("WebSocket ended before the frame completed")
    return payload


def ws_read(sock, handle):
    while True:
        a, b = exact_read(handle, 2)
        if not a & 0x80 or a & 0x70 or b & 0x80:
            raise RuntimeError("unexpected fragmented, compressed or masked server test frame")
        size = b & 0x7F
        if size == 126:
            size = struct.unpack("!H", exact_read(handle, 2))[0]
        elif size == 127:
            size = struct.unpack("!Q", exact_read(handle, 8))[0]
        if size > 65536:
            raise RuntimeError("server test frame exceeded limit")
        payload = exact_read(handle, size)
        opcode = a & 0x0F
        if opcode == 9:
            ws_send(sock, 10, payload)
            continue
        if opcode == 10:
            continue
        return opcode, payload


def ws_client(expected, count, interval_ms, ready=None):
    sock = socket.create_connection(("127.0.0.1", CADDY_LISTENER), timeout=10)
    handle = sock.makefile("rb")
    first = None
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{CADDY_LISTENER}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n")
        sock.sendall(request.encode("ascii"))
        if not handle.readline(8192).startswith(b"HTTP/1.1 101 "):
            raise RuntimeError("standard WebSocket upgrade failed")
        headers = {}
        total = 0
        while True:
            line = handle.readline(8192)
            total += len(line)
            if not line or total > 16384:
                raise RuntimeError("invalid WebSocket upgrade headers")
            if line == b"\r\n":
                break
            name, value = line.decode("ascii").split(":", 1)
            headers[name.lower()] = value.strip()
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != accept or headers.get("x-probe-identity") != expected:
            raise RuntimeError("WebSocket handshake or backend identity differs")
        for sequence in range(count):
            payload = json.dumps({"sequence": sequence, "expected": expected}, separators=(",", ":")).encode()
            ws_send(sock, 1, payload)
            opcode, echoed = ws_read(sock, handle)
            if opcode != 1 or echoed != payload:
                raise RuntimeError("standard WebSocket text echo changed or ended")
            if first is None:
                first = time.monotonic()
                if ready:
                    ready.set()
            if sequence != count - 1:
                time.sleep(interval_ms / 1000)
        ws_send(sock, 8, struct.pack("!H", 1000))
        opcode, payload = ws_read(sock, handle)
        if opcode != 8 or len(payload) < 2 or struct.unpack("!H", payload[:2])[0] != 1000:
            raise RuntimeError("WebSocket did not complete its normal close handshake")
        return {"identity": expected, "echoes": count, "close_code": 1000,
                "first_at": first, "finished_at": time.monotonic()}
    finally:
        handle.close()
        sock.close()


def exercise(scope):
    scope, state = scope_state(scope)
    if state["role"] != "source":
        raise RuntimeError("exercise is source-only")
    report = {"run_id": state["run_id"], "success": False, "production_changed": False}
    streams, threads, ready = {}, [], [threading.Event(), threading.Event()]
    source, target = "source-" + state["run_id"], "target-" + state["run_id"]
    try:
        start_container(scope)
        wait_identity(SOURCE_PUBLISHED, source)
        identity(BRIDGE_LISTENER, target)
        start_caddy(scope)
        report["before_redirect"] = wait_identity(CADDY_LISTENER, source)
        report["test_caddy"] = verify_test_caddy(scope)
        def stream_task(name, function, event):
            try:
                streams[name] = function(source, 24, 250, event)
            except Exception as exc:
                streams[name] = {"error": str(exc)}
                event.set()
        for name, function, event in zip(("old_sse", "old_ws"), (sse_client, ws_client), ready):
            thread = threading.Thread(target=stream_task, args=(name, function, event), daemon=True)
            thread.start()
            threads.append(thread)
        if not all(event.wait(6) for event in ready) or any("error" in value for value in streams.values()):
            raise RuntimeError("old test streams were not both established")
        report["exact_nat_rule"] = set_redirect(scope, True)
        report["redirect_at"] = time.monotonic()
        report["new_http_target"] = identity(CADDY_LISTENER, target)
        report["new_ws_target"] = ws_client(target, 2, 10)
        report["new_sse_target"] = sse_client(target, 2, 10)
        for thread in threads:
            thread.join(10)
        report.update(streams)
        for name in ("old_sse", "old_ws"):
            value = streams.get(name, {})
            if "error" in value or not (value.get("first_at", float("inf")) < report["redirect_at"] < value.get("finished_at", 0)):
                raise RuntimeError("old " + name + " did not complete across NAT insertion")
        # No active old test stream remains. Stopping this owned test backend
        # also removes Docker's publication, but must not remove our REDIRECT.
        with resources(scope) as (_, current):
            item = owned_container(current)
            if item is None:
                raise RuntimeError("old test container vanished")
            command(["docker", "stop", "--time=5", item["Id"]])
        ensure_rule_first(nat_rule(state["caddy_uid"], state["run_id"]), state["run_id"])
        report["old_container_stopped_rule_still_works"] = identity(CADDY_LISTENER, target)
        with resources(scope) as (_, current):
            item = owned_container(current)
            command(["docker", "start", item["Id"]])
        wait_identity(SOURCE_PUBLISHED, source)
        set_redirect(scope, False)
        report["rule_removed_http_source"] = identity(CADDY_LISTENER, source)
        report["rule_removed_ws_source"] = ws_client(source, 2, 10)
        report["rule_removed_sse_source"] = sse_client(source, 2, 10)
        report["success"] = True
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["cleanup"] = cleanup(scope)
        report["production_changed"] = not report["cleanup"]["production_unchanged"]
        report["success"] = report["success"] and not report["cleanup"]["errors"]
        json_write(scope / "exercise.json", report)
    return report


def watchdog(scope):
    _, state = scope_state(scope)
    def stop(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while time.time() < state["expires_at"]:
            time.sleep(0.5)
    finally:
        cleanup(scope)


def bootstrap(payload):
    # Invoked through an encoded, fixed bootstrap call under sudo. Only creates
    # a fresh root-owned run scope and its watchdog, never source/target probes.
    run_id, role = payload["run_id"], payload["role"]
    names = run_names(run_id, role)
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("bootstrap requires Linux root")
    scope = pathlib.Path(SCOPE_PREFIX + run_id)
    scope.mkdir(mode=0o755, exist_ok=False)
    (scope / "bridge_rehearsal.py").write_bytes(base64.b64decode(payload["script"]))
    (scope / "bridge_probe").write_bytes(base64.b64decode(payload["binary"]))
    (scope / "bridge_rehearsal.py").chmod(0o555)
    (scope / "bridge_probe").chmod(0o555)
    state = {"run_id": run_id, "role": role, "image": payload["image"], "timeout": payload["timeout"],
             "expires_at": time.time() + payload["timeout"], "rule_installed": False,
             "baseline": production_snapshot(run_id)}
    if role == "source":
        state["caddy_uid"] = pwd.getpwnam("caddy").pw_uid
        if state["caddy_uid"] <= 0:
            raise RuntimeError("caddy must have a non-root UID")
        if command(["docker", "version", "--format", "{{.Server.Version}}"] ).stdout.strip() != "28.2.2":
            raise RuntimeError("source Docker differs from the reviewed 28.2.2")
        version = command(["iptables", "--version"]).stdout
        if "v1.8.10" not in version or "nf_tables" not in version:
            raise RuntimeError("source iptables differs from the reviewed nft backend")
        if state["baseline"]["caddy_active"] != "active" or state["baseline"]["caddy_main_pid"] == "0":
            raise RuntimeError("production Caddy is not active; refuse rehearsal")
        for port in (SOURCE_PUBLISHED, CADDY_LISTENER):
            check_port_free(port)
    else:
        check_port_free(TARGET_PUBLISHED)
    json_write(scope / "state.json", state)
    stop_command = shlex.join(["/usr/bin/python3", str(scope / "bridge_rehearsal.py"), "--remote=cleanup", "--scope=" + str(scope)])
    command(["systemd-run", "--quiet", "--collect", "--unit=" + names["watchdog_unit"],
             "--description=Sub2API isolated bridge watchdog " + role + " " + run_id,
             "--property=RuntimeMaxSec=" + str(payload["timeout"] + 5),
             "--property=TimeoutStopSec=120", "--property=ExecStopPost=" + stop_command,
             "/usr/bin/python3", str(scope / "bridge_rehearsal.py"), "--remote=watchdog", "--scope=" + str(scope)])
    if unit_property(names["watchdog_unit"], "ActiveState") != "active":
        raise RuntimeError("independent cleanup watchdog is not active")
    return {"scope": str(scope), "watchdog_unit": names["watchdog_unit"], "baseline": state["baseline"]}


def stop_watchdog(scope):
    scope, state = scope_state(scope)
    name = run_names(state["run_id"], state["role"])["watchdog_unit"]
    expected = "Sub2API isolated bridge watchdog " + state["role"] + " " + state["run_id"]
    if unit_property(name, "Description") == expected:
        command(["systemctl", "stop", name], timeout=130)
    return json.loads((scope / "cleanup.json").read_text())


def remote_call(ssh, script_path, mode, scope, *, timeout=60):
    argv = ["sudo", "-n", "python3", script_path, "--remote=" + mode, "--scope=" + scope]
    result = command(ssh + [shlex.join(argv)], timeout=timeout)
    return json.loads(result.stdout)


def source_ssh(host):
    if not host or host.startswith("-") or any(character.isspace() for character in host):
        raise ValueError("invalid source SSH host")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes", host]


def target_ssh():
    return ["ssh", "-o", "ProxyCommand=nc -X connect -x 127.0.0.1:7897 %h %p",
            "-o", "ProxyJump=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
            "-i", str(pathlib.Path.home() / ".ssh/tokyo.pem"), "ubuntu@43.165.177.136"]


def apply(args):
    if not args.source_image or not args.target_image:
        raise RuntimeError("--apply requires explicitly approved existing --source-image and --target-image")
    run_id = uuid.uuid4().hex[:16]
    report = {"run_id": run_id, "success": False, "scope": SCOPE_PREFIX + run_id}
    script_path = pathlib.Path(__file__).resolve()
    repo = script_path.parents[2]
    hosts = {"source": source_ssh(args.source_host), "target": target_ssh()}
    bootstrapped = []
    try:
        with tempfile.TemporaryDirectory(prefix="sub2api-bridge-build-") as directory:
            for role in ("target", "source"):
                ssh = hosts[role]
                architecture = command(ssh + ["uname -m"], timeout=20).stdout.strip()
                arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture)
                if arch is None:
                    raise RuntimeError("unsupported static probe architecture")
                binary = pathlib.Path(directory) / ("bridge_probe_" + arch)
                if not binary.exists():
                    environment = dict(os.environ, GOOS="linux", GOARCH=arch, CGO_ENABLED="0")
                    command(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(binary),
                             str(script_path.with_name("bridge_probe.go"))], env=environment, cwd=repo / "backend", timeout=180)
                payload = {"run_id": run_id, "role": role, "timeout": args.timeout,
                           "image": args.source_image if role == "source" else args.target_image,
                           "script": base64.b64encode(script_path.read_bytes()).decode(),
                           "binary": base64.b64encode(binary.read_bytes()).decode()}
                # Source is the reviewed script itself, executed in an isolated
                # Python namespace. Payload stays on stdin, not shell arguments.
                launcher = "import base64,json; p=json.load(__import__('sys').stdin); n={'__name__':'bootstrap_module'}; exec(compile(base64.b64decode(p['script']),'bridge_rehearsal.py','exec'),n); print(json.dumps(n['bootstrap'](p)))"
                output = command(ssh + [shlex.join(["sudo", "-n", "python3", "-c", launcher])], data=json.dumps(payload), timeout=60)
                report[role + "_bootstrap"] = json.loads(output.stdout)
                bootstrapped.append(role)
                if role == "target":
                    report["target_start"] = remote_call(ssh, report["scope"] + "/bridge_rehearsal.py", "start-target", report["scope"])
            report["exercise"] = remote_call(hosts["source"], report["scope"] + "/bridge_rehearsal.py", "exercise", report["scope"], timeout=90)
            report["success"] = report["exercise"]["success"]
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        for role in reversed(bootstrapped):
            try:
                report[role + "_cleanup"] = remote_call(hosts[role], report["scope"] + "/bridge_rehearsal.py", "cleanup", report["scope"], timeout=150)
                report[role + "_watchdog_stopped"] = remote_call(hosts[role], report["scope"] + "/bridge_rehearsal.py", "stop-watchdog", report["scope"], timeout=150)
                if report[role + "_cleanup"]["errors"]:
                    report["success"] = False
            except Exception as exc:
                report[role + "_cleanup_error"] = str(exc)
                report["success"] = False
        destination = pathlib.Path(args.report or ("bridge-rehearsal-" + run_id + ".json")).resolve()
        json_write(destination, report)
        print(json.dumps({"report": str(destination), "success": report["success"], "run_id": run_id}))
    return 0 if report["success"] else 1


def main():
    safety_invariants()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--source-host", default="sub2api")
    parser.add_argument("--source-image")
    parser.add_argument("--target-image")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--report")
    parser.add_argument("--remote", choices=["watchdog", "start-target", "exercise", "cleanup", "stop-watchdog"], help=argparse.SUPPRESS)
    parser.add_argument("--scope", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 120 <= args.timeout <= 600:
        parser.error("--timeout must be between 120 and 600 seconds")
    if args.remote:
        scope, state = scope_state(args.scope)
        if args.remote == "watchdog":
            watchdog(scope)
            return 0
        if args.remote == "start-target":
            if state["role"] != "target":
                raise RuntimeError("start-target is target-only")
            start_container(scope)
            output = wait_identity(TARGET_PUBLISHED, "target-" + state["run_id"])
        elif args.remote == "exercise":
            output = exercise(scope)
        elif args.remote == "cleanup":
            output = cleanup(scope)
        else:
            output = stop_watchdog(scope)
        print(json.dumps(output))
        return 0
    if not args.apply:
        print(json.dumps({"mode": "dry-run", "remote_commands_executed": False,
                          "source": {"test_caddy": "127.0.0.1:28080", "test_docker": "127.0.0.1:28081"},
                          "target_test_docker": "127.0.0.1:8082", "existing_channel": "127.0.0.1:18081",
                          "rule": "OUTPUT first; tcp dst 127.0.0.1:28081; owner caddy UID; ctstate NEW; unique comment; REDIRECT :18081",
                          "production_ports": "never modified", "apply_requires": "review, explicit existing image IDs, free test ports"}, indent=2))
        return 0
    return apply(args)


if __name__ == "__main__":
    raise SystemExit(main())
