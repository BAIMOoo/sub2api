#!/usr/bin/env python3
"""Isolated Linux NAT/SSE rehearsal. No arguments is entirely offline.

--run --image sha256:<immutable local image> launches a new Docker network=none
namespace, with no published ports, host network, Docker socket, writable host
mount or production endpoint. The image needs python3, iproute2, iptables and conntrack.
Only the fixed inner coordinator (PID 1, loopback-only routing) can change
NAT. It uses the current core REDIRECT constant and host NAT normalization.
The program never installs a host rule or connects to a production host.
"""
import argparse
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import os
import pathlib
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FRAMES = 60
FRAME_INTERVAL = 0.04
MARKER = "SUB2API_NAT_REHEARSAL_ISOLATED"
LABEL = "io.sub2api.isolated-nat-rehearsal"
FALLBACK_LINK_TYPES = {"tunl0": "ipip", "gre0": "gre", "gretap0": "ether", "erspan0": "ether",
                       "ip_vti0": "ipip", "ip6_vti0": "tunnel6", "sit0": "sit", "ip6tnl0": "tunnel6", "ip6gre0": "gre6"}
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C", MARKER: "1"}


class Refusal(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise Refusal(code)


def load(name):
    path = pathlib.Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("rehearsal_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guard(*, coordinator=False):
    need(sys.platform == "linux" and os.environ.get(MARKER) == "1" and pathlib.Path("/.dockerenv").is_file(),
         "ISOLATED_DOCKER_REQUIRED")
    need(not coordinator or (os.getpid() == 1 and os.getuid() == 0), "ISOLATED_ROOT_PID1_REQUIRED")
    ip = shutil.which("ip")
    need(ip is not None, "REHEARSAL_IMAGE_TOOLS_MISSING")
    state = {"addresses": json.loads(fixed_command([ip, "-j", "address", "show"])),
             "routes": json.loads(fixed_command([ip, "-j", "route", "show", "table", "all"])),
             "routes6": json.loads(fixed_command([ip, "-j", "-6", "route", "show", "table", "all"]))}
    validate_network(state)
    return state


def validate_network(state):
    """Linuxkit adds dormant fallback tunnels even to a network=none namespace."""
    addresses = state["addresses"]
    need(len({x["ifname"] for x in addresses}) == len(addresses) and any(x["ifname"] == "lo" for x in addresses),
         "LOOPBACK_ONLY_NAMESPACE_REQUIRED")
    for link in addresses:
        name = link["ifname"]
        if name == "lo":
            need(link["link_type"] == "loopback" and all(ipaddress.ip_address(x["local"]).is_loopback for x in link["addr_info"]),
                 "LOOPBACK_ONLY_NAMESPACE_REQUIRED")
        else:
            need(FALLBACK_LINK_TYPES.get(name) == link["link_type"] and link["operstate"] == "DOWN" and
                 "UP" not in link["flags"] and link["addr_info"] == [], "LOOPBACK_ONLY_NAMESPACE_REQUIRED")
    for route in state["routes"] + state["routes6"]:
        need(route.get("dev") == "lo" and route.get("type") in ("local", "broadcast") and
             route.get("table") == "local" and "gateway" not in route and ipaddress.ip_network(route["dst"]).is_loopback,
             "LOOPBACK_ONLY_ROUTING_REQUIRED")


def fixed_command(argv):
    result = subprocess.run(argv, capture_output=True, timeout=5, env=SAFE_ENV)
    need(result.returncode == 0 and len(result.stdout) < 2 * 1024 ** 2, "NAMESPACE_COMMAND_FAILED")
    return result.stdout


def scenario_rule(rule, scenario):
    need(scenario in ("warm-current", "cold-syn"), "REHEARSAL_SCENARIO_INVALID")
    rule = list(rule)
    if scenario == "cold-syn":
        if "--tcp-flags" in rule:
            i = rule.index("--tcp-flags")
            need(rule[i + 1:i + 3] == ["FIN,SYN,RST,ACK", "SYN"], "CANDIDATE_FLAGS_INVALID")
        else:
            i = rule.index("--dport") + 2
            rule[i:i] = ["--tcp-flags", "FIN,SYN,RST,ACK", "SYN"]
    return rule


def launch_argv(docker, image, directory, name, scenario="warm-current"):
    need(re.fullmatch(r"sha256:[0-9a-f]{64}", image), "IMMUTABLE_IMAGE_REQUIRED")
    need(re.fullmatch(r"sub2api-nat-rehearsal-[0-9a-f]{16}", name), "REHEARSAL_NAME_REQUIRED")
    need(scenario in ("warm-current", "cold-syn"), "REHEARSAL_SCENARIO_INVALID")
    return [docker, "run", "--rm", "--pull", "never", "--name", name, "--label", LABEL + "=" + name,
            "--network", "none", "--read-only", "--user", "0",
            "--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "--cap-add", "SETUID", "--cap-add", "SETGID",
            "--security-opt", "no-new-privileges:true", "--pids-limit", "96", "--memory", "256m",
            "--memory-swap", "256m", "--cpus", "1", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--env", MARKER + "=1", "--entrypoint", "/usr/bin/python3",
            "--mount", "type=bind,src=" + str(directory) + ",dst=/rehearsal,readonly", image,
            "-B", "/rehearsal/rolling_app_nat_rehearsal.py", "--inside", "--scenario", scenario]


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def serve(role):
    guard()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_):
            pass
        def do_GET(self):
            if self.path == "/stream":
                frames = [("data: " + role + ":" + str(i) + "\n\n").encode() for i in range(FRAMES)]
                frames.append(b"data: [DONE]\n\n")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(sum(map(len, frames))))
                self.send_header("X-Rehearsal-Backend", role)
                self.end_headers()
                for frame in frames:
                    self.wfile.write(frame)
                    self.wfile.flush()
                    time.sleep(FRAME_INTERVAL)
            else:
                raw = json.dumps({"backend": role}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Rehearsal-Backend", role)
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
    listeners = [("127.0.0.1", 8083)] if role == "new" else [
        ("127.0.0.1", 8082), ("127.0.0.2", 8082), ("127.0.0.1", 8084)]
    servers = [ThreadingHTTPServer(address, Handler) for address in listeners]
    for server in servers:
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    emit({"event": "ready", "role": role, "pid": os.getpid()})
    stop.wait()
    for server in servers:
        server.shutdown()
        server.server_close()
    return 0


def client(mode):
    guard()
    host, port = ("127.0.0.2", 8082) if mode == "other-destination" else ("127.0.0.1", 8084 if mode == "other-port" else 8082)
    connection = http.client.HTTPConnection(host, port, timeout=5)
    started = time.monotonic()
    connection.request("GET", "/stream" if mode == "stream" else "/which")
    response = connection.getresponse()
    need(response.status == 200, "REHEARSAL_HTTP_FAILED")
    first_byte = time.monotonic() - started
    backend = response.getheader("X-Rehearsal-Backend")
    local_port = connection.sock.getsockname()[1]
    if mode == "stream":
        emit({"event": "connected", "backend": backend, "local_port": local_port})
        values, times, done = [], [], False
        while True:
            line = response.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                value = line.decode().strip()[6:]
                if value == "[DONE]":
                    done = True
                else:
                    values.append(value)
                    times.append(time.monotonic())
        emit({"event": "result", "values": values, "frame_times": times, "done": done,
              "first_byte_seconds": first_byte, "max_frame_gap": max([b - a for a, b in zip(times, times[1:])] or [0])})
    elif mode == "idle":
        need(json.loads(response.read())["backend"] == backend, "IDLE_BODY_INVALID")
        emit({"event": "connected", "backend": backend, "local_port": local_port})
        need(sys.stdin.readline().strip() == "after", "IDLE_PROTOCOL_INVALID")
        connection.request("GET", "/which")
        after = connection.getresponse()
        value = json.loads(after.read())
        emit({"event": "after", "backend": value["backend"], "same_local_port": connection.sock.getsockname()[1] == local_port})
        need(sys.stdin.readline().strip() == "finish", "IDLE_PROTOCOL_INVALID")
        emit({"event": "result", "completed": True})
    else:
        need(json.loads(response.read())["backend"] == backend, "PROBE_BODY_INVALID")
        emit({"backend": backend, "uid": os.getuid(), "first_byte_seconds": first_byte})
    connection.close()
    return 0


class Child:
    def __init__(self, flag, mode, *, uid=0):
        self.role = mode
        self.process = subprocess.Popen([sys.executable, "-B", __file__, flag, mode], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, user=uid, group=uid, extra_groups=[], env=SAFE_ENV)
        self.events = queue.Queue()
        def read():
            for line in self.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    self.events.put({"invalid": True})
            self.events.put({"eof": True})
        threading.Thread(target=read, daemon=True).start()

    def event(self, kind):
        try:
            value = self.events.get(timeout=10)
        except queue.Empty:
            raise Refusal("CHILD_EVENT_TIMEOUT") from None
        if value.get("event") != kind:
            code = ""
            if value.get("eof"):
                self.process.wait(timeout=2)
                stderr = self.process.stderr.read(4096)
                try:
                    code = json.loads(stderr.strip().splitlines()[-1]).get("code", "")
                except (ValueError, IndexError, AttributeError):
                    pass
            raise Refusal("CHILD_" + self.role.replace("-", "_").upper() + "_" + kind.upper() + "_" +
                          (code if re.fullmatch(r"[A-Za-z0-9_]{1,96}", code) else "EVENT_INVALID"))
        return value

    def send(self, value):
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()

    def clean_exit(self):
        need(self.process.wait(timeout=5) == 0, "CHILD_EXIT_FAILED")


def once(mode="once", *, uid=1000):
    result = subprocess.run([sys.executable, "-B", __file__, "--client", mode], user=uid, group=uid,
                            extra_groups=[], capture_output=True, text=True, timeout=10, env=SAFE_ENV)
    need(result.returncode == 0 and len(result.stdout) < 65536, "PROBE_CHILD_FAILED")
    return json.loads(result.stdout)


def validate_evidence(e):
    need(e["ok"] is True and e["isolated"] is True and
         e["production_rules_touched"] is False and e["production_application_tested"] is False,
         "ISOLATION_EVIDENCE_INVALID")
    validate_network(e["network"])
    client_ports = set((e["stream_local_port"], e["idle_before"]["local_port"]))
    if e["scenario"] == "warm-current":
        need(e["conntrack_activated_before_clients"] is True and
             client_ports.issubset(tracked_client_ports(e["conntrack_before_route"])), "EXISTING_CONNECTIONS_NOT_TRACKED")
    elif e["scenario"] == "cold-syn":
        need(e["conntrack_activated_before_clients"] is False and e["conntrack_before_route"].strip() == "" and
             "--tcp-flags" in e["rule"] and e["rule"][e["rule"].index("--tcp-flags") + 1:e["rule"].index("--tcp-flags") + 3] ==
             ["FIN,SYN,RST,ACK", "SYN"], "UNTRACKED_SYN_PROOF_MISSING")
    else:
        need(False, "REHEARSAL_SCENARIO_INVALID")
    need(e["new_uid1000"]["backend"] == "new" and e["new_uid1000"]["uid"] == 1000 and
         e["new_uid1000"]["first_byte_seconds"] <= 10, "NEW_REQUEST_NOT_REDIRECTED")
    need(e["new_uid0"]["backend"] == e["other_destination"]["backend"] == e["other_port"]["backend"] == "old",
         "RULE_SCOPE_TOO_BROAD")
    stream = e["stream"]
    need(stream["done"] is True and stream["values"] == ["old:" + str(i) for i in range(FRAMES)] and
         stream["max_frame_gap"] < 1 and e["frames_after_rule"] >= 5, "OLD_STREAM_INTERRUPTED")
    need(e["idle_before"]["backend"] == e["idle_after"]["backend"] == "old" and
         e["idle_after"]["same_local_port"] is True and e["old_tcp_during_stream"] >= 2 and
         e["old_tcp_after_stream_before_idle_close"] >= 1 and e["old_tcp_final"] == 0,
         "ESTABLISHED_IDLE_CONNECTION_NOT_PRESERVED")
    need(e["old_sigterm_count"] == 1 and e["old_exit_code"] == 0 and e["nat_other_rules_unchanged"] is True,
         "DRAIN_OR_NAT_PROOF_FAILED")
    need(e["quiet_seconds"] >= 2 and e["quiet_samples"] >= 3, "QUIET_WINDOW_TOO_SHORT")


def tracked_client_ports(raw):
    ports = set()
    for line in raw.splitlines():
        fields = line.split()
        if "ESTABLISHED" not in fields or "[ASSURED]" not in fields or "tcp" not in fields:
            continue
        origin = {}
        for field in fields:
            if "=" not in field:
                continue
            key, value = field.split("=", 1)
            if key in origin:
                break
            origin[key] = value
        if origin.get("src") == origin.get("dst") == "127.0.0.1" and origin.get("dport") == "8082":
            need(origin.get("sport", "").isdigit(), "CONNTRACK_PORT_INVALID")
            ports.add(int(origin["sport"]))
    return ports


def cleanup_own_container(docker, name):
    """Only a failed rehearsal with the exact random name and ownership label."""
    result = subprocess.run([docker, "inspect", "--type", "container", name], capture_output=True, timeout=10)
    if result.returncode:
        return
    values = json.loads(result.stdout)
    need(len(values) == 1 and values[0].get("Name") == "/" + name and
         values[0].get("Config", {}).get("Labels", {}).get(LABEL) == name and
         re.fullmatch(r"[0-9a-f]{64}", values[0].get("Id", "")), "REHEARSAL_CLEANUP_IDENTITY_MISMATCH")
    removed = subprocess.run([docker, "rm", "--force", values[0]["Id"]], capture_output=True, timeout=10)
    need(removed.returncode == 0, "REHEARSAL_CLEANUP_FAILED")


def inside(scenario="warm-current"):
    network = guard(coordinator=True)
    m, h = load("rolling_app_recovery"), load("rolling_app_host")
    rule = scenario_rule(m.REDIRECT, scenario)
    warm = scenario == "warm-current"
    ip, iptables, save, conntrack = (shutil.which(x) for x in ("ip", "iptables", "iptables-save", "conntrack"))
    need(all((ip, iptables, save, conntrack)), "REHEARSAL_IMAGE_TOOLS_MISSING")
    fixed_command([ip, "link", "set", "lo", "up"])
    old, new = Child("--server", "old"), Child("--server", "new")
    old.event("ready")
    new.event("ready")
    old_pid = old.process.pid
    start = h.process_start_ticks(pathlib.Path("/proc", str(old_pid), "stat").read_text())
    count = lambda: h.tcp_inbound_count(old_pid, start, port=8082)
    # A preexisting unrelated rule proves preservation and owned rule ordering.
    fixed_command([iptables, "-w", "2", "-t", "nat", "-A", "OUTPUT", "-p", "tcp", "--dport", "18000"] +
                  (["-m", "conntrack", "--ctstate", "NEW"] if warm else []) + [
                   "-m", "comment", "--comment", "rehearsal-unrelated", "-j", "RETURN"])
    before = h.parse_nat(fixed_command([save, "-t", "nat"]), rule)
    stream, idle = Child("--client", "stream", uid=1000), Child("--client", "idle", uid=1000)
    stream_connected, idle_before = stream.event("connected"), idle.event("connected")
    need(stream_connected["backend"] == idle_before["backend"] == "old", "BASELINE_CLIENTS_NOT_OLD")
    ct_before = fixed_command([conntrack, "-L", "-f", "ipv4", "-p", "tcp", "--orig-src", "127.0.0.1",
                               "--orig-dst", "127.0.0.1", "--dport", "8082", "-o", "extended"]).decode()
    if warm:
        need(set((stream_connected["local_port"], idle_before["local_port"])).issubset(tracked_client_ports(ct_before)),
             "EXISTING_CONNECTIONS_NOT_TRACKED")
    else:
        need(ct_before.strip() == "", "COLD_NAMESPACE_ALREADY_TRACKING")
    rule_at = time.monotonic()
    fixed_command([iptables, "-w", "2", "-t", "nat", "-I", "OUTPUT", "1"] + rule[2:])
    after = h.parse_nat(fixed_command([save, "-t", "nat"]), rule)
    expected = {"output": [rule] + before["output"], "other": before["other"]}
    need(after == expected and after["output"].count(rule) == 1, "NAT_BASELINE_NOT_PRESERVED")
    old_during = count()
    new1000, new0, other_destination, other_port = once(), once(uid=0), once("other-destination"), once("other-port")
    idle.send("after")
    idle_after = idle.event("after")
    stream_result = stream.event("result")
    stream.clean_exit()
    old_after_stream = count()
    need(old_after_stream >= 1, "IDLE_SOCKET_WAS_NOT_RETAINED")
    idle.send("finish")
    idle.event("result")
    idle.clean_exit()
    deadline = time.monotonic() + 8
    quiet_since, quiet_samples = None, 0
    while time.monotonic() < deadline:
        if count() == 0:
            quiet_since = time.monotonic() if quiet_since is None else quiet_since
            quiet_samples += 1
            if time.monotonic() - quiet_since >= 2 and quiet_samples >= 3:
                break
        else:
            quiet_since, quiet_samples = None, 0
        time.sleep(0.05)
    need(quiet_since is not None and time.monotonic() - quiet_since >= 2 and count() == 0, "OLD_CONNECTIONS_DID_NOT_DRAIN")
    old.process.send_signal(signal.SIGTERM)
    old.clean_exit()
    evidence = {"ok": True, "isolated": True, "namespace": os.readlink("/proc/self/ns/net"),
        "network": network, "kernel": os.uname().release, "architecture": os.uname().machine,
        "rule": rule, "nat_before": before, "nat_after": after, "nat_other_rules_unchanged": after == expected,
        "scenario": scenario, "conntrack_activated_before_clients": warm, "conntrack_before_route": ct_before,
        "stream_local_port": stream_connected["local_port"],
        "new_uid1000": new1000, "new_uid0": new0, "other_destination": other_destination, "other_port": other_port,
        "stream": stream_result, "frames_after_rule": sum(t >= rule_at for t in stream_result["frame_times"]),
        "idle_before": idle_before, "idle_after": idle_after, "old_tcp_during_stream": old_during,
        "old_tcp_after_stream_before_idle_close": old_after_stream, "old_tcp_final": 0,
        "quiet_seconds": time.monotonic() - quiet_since, "quiet_samples": quiet_samples,
        "old_sigterm_count": 1, "old_exit_code": old.process.returncode,
        "production_rules_touched": False, "production_application_tested": False}
    validate_evidence(evidence)
    new.process.send_signal(signal.SIGTERM)
    new.clean_exit()
    emit(evidence)
    return 0


def run_local(image, output=None, scenario="warm-current"):
    docker = shutil.which("docker")
    need(docker is not None, "LOCAL_DOCKER_REQUIRED")
    need(re.fullmatch(r"sha256:[0-9a-f]{64}", image or ""), "IMMUTABLE_IMAGE_REQUIRED")
    inspect = subprocess.run([docker, "image", "inspect", image], capture_output=True, timeout=10)
    need(inspect.returncode == 0, "LOCAL_IMAGE_REQUIRED")
    metadata = json.loads(inspect.stdout)
    need(len(metadata) == 1 and metadata[0]["Id"] == image and metadata[0]["Os"] == "linux", "LOCAL_LINUX_IMAGE_REQUIRED")
    name = "sub2api-nat-rehearsal-" + os.urandom(8).hex()
    directory = pathlib.Path(__file__).resolve().parent
    programs = ("rolling_app_nat_rehearsal.py", "rolling_app_host.py", "rolling_app_recovery.py")
    hashes = {p: hashlib.sha256((directory / p).read_bytes()).hexdigest() for p in programs}
    argv = launch_argv(docker, image, directory, name, scenario)
    try:
        result = subprocess.run(argv, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        cleanup_own_container(docker, name)
        raise Refusal("ISOLATED_REHEARSAL_TIMEOUT") from None
    if result.returncode:
        code = ""
        try:
            code = json.loads(result.stderr.decode().strip().splitlines()[-1]).get("code", "")
        except (ValueError, IndexError, AttributeError):
            pass
        need(False, "ISOLATED_REHEARSAL_FAILED" + (":" + code if re.fullmatch(r"[A-Za-z0-9_]{1,96}", code) else ""))
    need(len(result.stdout) < 2 * 1024 ** 2, "ISOLATED_REHEARSAL_OUTPUT_TOO_LARGE")
    evidence = json.loads(result.stdout)
    validate_evidence(evidence)
    need(hashes == {p: hashlib.sha256((directory / p).read_bytes()).hexdigest() for p in programs}, "REHEARSAL_PROGRAMS_CHANGED")
    need(evidence["scenario"] == scenario and evidence["rule"] == scenario_rule(load("rolling_app_recovery").REDIRECT, scenario),
         "REHEARSAL_RULE_CHANGED")
    evidence["image_id"] = image
    evidence["program_sha256"] = hashes
    evidence["docker_argv"] = argv
    raw = json.dumps(evidence, sort_keys=True).encode() + b"\n"
    if output is not None:
        path = pathlib.Path(output).absolute()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    sys.stdout.buffer.write(raw)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--output")
    parser.add_argument("--scenario", choices=("warm-current", "cold-syn"), default="warm-current")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--server", choices=("old", "new"), help=argparse.SUPPRESS)
    parser.add_argument("--client", choices=("stream", "idle", "once", "other-destination", "other-port"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    need(sum(bool(x) for x in (args.run, args.inside, args.server, args.client)) <= 1, "ONE_MODE_REQUIRED")
    if args.inside:
        return inside(args.scenario)
    if args.server:
        return serve(args.server)
    if args.client:
        return client(args.client)
    if args.run:
        return run_local(args.image, args.output, args.scenario)
    need(args.image is None and args.output is None, "RUN_FLAG_REQUIRED")
    emit({"offline": True, "production_operations": False, "requires": "local Linux Docker image with python3/iproute2/iptables/conntrack",
          "namespace": "new Docker network=none", "published_ports": []})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "code": str(exc) if isinstance(exc, Refusal) else type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
