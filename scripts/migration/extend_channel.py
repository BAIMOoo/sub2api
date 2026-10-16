#!/usr/bin/env python3
"""Extend only the existing migration SSH channel. Offline dry-run by default.

--inspect is read-only. Reviewed --apply backs up and extends the owned key/unit;
--apply --restore restores only matching owned changes. No business service,
container, database, Caddy, sshd or DNS configuration is changed.
"""
import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import pathlib
import pwd
import re
import secrets
import shlex
import socket
import stat
import struct
import subprocess
import sys
import time

RUN = "sub2api-channel-extension-20261016"
SOURCE_IP = "13.231.143.136"
TARGET_IP = "43.165.177.136"
BASE = "/home/ubuntu/.local/share/sub2api-migration-20261016"
TARGET_BASE = "/home/ubuntu/sub2api-migration-20261016"
UNIT = "sub2api-migration-channel-20261016.service"
UNIT_PATH = "/etc/systemd/system/"+UNIT
KEY_COMMENT = "sub2api-live-migration-20261016"
BRIDGE = "172.18.0.1"
REDIS_IP = "172.18.0.2"
ADDED = ["172.18.0.1:26379:127.0.0.1:26379", "127.0.0.1:18092:127.0.0.1:18092", "127.0.0.1:25432:127.0.0.1:25432"]
ADDED_OPEN = ["127.0.0.1:26379", "127.0.0.1:18092", "127.0.0.1:25432"]
SOURCE = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", "-oStrictHostKeyChecking=yes", "sub2api"]
TARGET = ["ssh", "-oProxyCommand=nc -X connect -x 127.0.0.1:7897 %h %p", "-oProxyJump=none",
          "-oBatchMode=yes", "-oConnectTimeout=10", "-oStrictHostKeyChecking=yes",
          "-i", str(pathlib.Path.home()/".ssh/tokyo.pem"), "-oIdentitiesOnly=yes", "ubuntu@"+TARGET_IP]


class Refusal(RuntimeError):
    pass


def need(ok, code):
    if not ok:
        raise Refusal(code)


def digest(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def safe_path(path):
    path = pathlib.Path(path)
    need(not any(p.is_symlink() for p in [path, *path.parents]), "symlink-path-refused")
    return path


def write_private(path, data, *, mode=0o600, uid=None, gid=None):
    path = safe_path(path)
    temporary = path.parent/("."+path.name+"."+secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data.encode() if isinstance(data, str) else data)
            out.flush(); os.fsync(out.fileno())
            os.fchmod(out.fileno(), mode)
            if uid is not None:
                os.fchown(out.fileno(), uid, gid)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def read_private(path):
    path = safe_path(path)
    need(path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == os.geteuid(), "invalid-private-state")
    return path.read_text()


def original_unit(pg_ip):
    ipaddress.IPv4Address(pg_ip)
    command = ["/usr/bin/ssh", "-F", "/dev/null", "-NT", "-i", BASE+"/tunnel_ed25519",
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile="+BASE+"/known_hosts", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=8",
        "-L", "127.0.0.1:18081:127.0.0.1:8082", "-R", "127.0.0.1:15432:"+pg_ip+":5432",
        "-R", "127.0.0.1:16379:"+REDIS_IP+":6379", "ubuntu@"+TARGET_IP]
    return '''[Unit]
Description=Sub2API migration-only encrypted loopback channel
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
ExecStart='''+" ".join(command)+'''
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
'''


def extended_unit(pg_ip):
    old = original_unit(pg_ip)
    destination = " ubuntu@"+TARGET_IP+"\n"
    need(old.count(destination) == 1, "unit-destination-ambiguous")
    body = old.replace(destination, " "+" ".join("-L "+forward for forward in ADDED)+destination)
    return body.replace("ExecStart=", "ExecStartPre=+/usr/bin/python3 "+BASE+"/channel-extension/firewall.py\nExecStart=", 1)


def key_line(public_key, *, extended=False):
    parts = public_key.split()
    need(len(parts) == 3 and parts[0] == "ssh-ed25519" and parts[2] == KEY_COMMENT, "unexpected-migration-public-key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise Refusal("invalid-migration-public-key") from None
    need(len(blob) == 51 and blob[:19] == b"\0\0\0\x0bssh-ed25519\0\0\0\x20", "invalid-ed25519-key-blob")
    options = [f'from="{SOURCE_IP}"', "restrict", "port-forwarding", 'command="/bin/false"', 'permitopen="127.0.0.1:8082"',
               'permitlisten="127.0.0.1:15432"', 'permitlisten="127.0.0.1:16379"']
    if extended:
        options += ['permitopen="'+address+'"' for address in ADDED_OPEN]
    return ",".join(options)+" "+public_key


def replace_key(contents, public_key, *, restore=False):
    old = key_line(public_key, extended=restore)
    new = key_line(public_key, extended=not restore)
    blob = public_key.split()[1]
    lines = contents.splitlines(keepends=True)
    matching = [i for i, line in enumerate(lines) if re.search(r"(?:^|\s)ssh-ed25519\s+"+re.escape(blob)+r"(?:\s|$)", line)]
    need(len(matching) == 1, "migration-key-missing-or-duplicated")
    i = matching[0]
    body = lines[i].rstrip("\r\n")
    if body == new:
        return contents  # Safe recovery of an unknown successful result.
    need(body == old, "migration-key-options-drifted-refuse-overwrite")
    lines[i] = new+lines[i][len(body):]
    return "".join(lines)


def firewall_rules(interface):
    need(bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", interface)), "invalid-bridge-interface")
    allow = ["-i", interface, "-s", REDIS_IP+"/32", "-d", BRIDGE+"/32", "-p", "tcp", "--dport", "26379",
             "-m", "comment", "--comment", RUN+":redis-allow", "-j", "ACCEPT"]
    deny = ["-d", BRIDGE+"/32", "-p", "tcp", "--dport", "26379", "-m", "comment", "--comment", RUN+":redis-deny",
            "-j", "REJECT", "--reject-with", "tcp-reset"]
    return [allow, deny]


def command(argv, *, timeout=12, allow_failure=False):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    need(allow_failure or result.returncode == 0, "remote-command-failed-private-state-retained")
    return result


def listeners():
    rows = command(["ss", "-H", "-lnt"]).stdout.splitlines()
    answer = []
    for row in rows:
        local = row.split()[3]
        host, port = local.rsplit(":", 1)
        answer.append([host.strip("[]"), int(port)])
    return answer


def loopback_exact(rows, port):
    return [row for row in rows if row[1] == port] == [["127.0.0.1", port]]


def tcp_probe(port, payload):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
            sock.sendall(payload)
            return sock.recv(512)
    except OSError:
        return b""


def api_status(port):
    response = tcp_probe(port, b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
    line = response.split(b"\r\n", 1)[0].split()
    return int(line[1]) if len(line) >= 2 and line[0].startswith(b"HTTP/") and line[1].isdigit() else None


def docker_info(name):
    result = command(["docker", "inspect", name], allow_failure=True)
    if result.returncode:
        need("no such" in result.stderr.lower() or "not found" in result.stderr.lower(), "docker-inspect-failed")
        return None
    return json.loads(result.stdout)[0]


def source_facts():
    unit = safe_path(UNIT_PATH)
    copy = safe_path(BASE+"/"+UNIT)
    public_key = safe_path(BASE+"/tunnel_ed25519.pub").read_text().strip()
    key_line(public_key)
    settings = dict(line.split("=", 1) for line in command(["systemctl", "show", UNIT, "-p", "FragmentPath", "-p", "DropInPaths", "-p", "ActiveState"]).stdout.splitlines())
    need(settings.get("FragmentPath") == UNIT_PATH and not settings.get("DropInPaths"), "channel-unit-location-or-dropins-changed")
    networks = {}
    for name in ["sub2api-postgres", "sub2api-redis"]:
        info = docker_info(name)
        need(info and info["State"]["Running"], "source-data-container-not-running")
        rows = list(info["NetworkSettings"]["Networks"].items())
        need(len(rows) == 1, "source-data-network-ambiguous")
        networks[name] = rows[0]
    network, redis = networks["sub2api-redis"]
    pg_network, pg = networks["sub2api-postgres"]
    need(redis["IPAddress"] == REDIS_IP and redis["Gateway"] == BRIDGE and pg_network == network, "source-container-route-changed")
    interfaces = json.loads(command(["ip", "-j", "address", "show"]).stdout)
    matches = [item["ifname"] for item in interfaces if any(a.get("local") == BRIDGE for a in item.get("addr_info", []))]
    need(len(matches) == 1, "source-bridge-interface-ambiguous")
    rules = firewall_rules(matches[0])
    firewall = command(["iptables", "-w", "3", "-S", "INPUT"]).stdout
    redis_text = command(["docker", "exec", "-e", "REDISCLI_AUTH=", "sub2api-redis", "redis-cli", "--raw", "INFO", "replication"]).stdout
    redis_repl = dict(line.split(":", 1) for line in redis_text.splitlines() if ":" in line)
    slots = command(["docker", "exec", "sub2api-postgres", "psql", "-X", "-A", "-t", "-w", "-v", "ON_ERROR_STOP=1", "-U", "sub2api", "-d", "sub2api", "-c", "SELECT (SELECT count(*) FROM pg_replication_slots)::text||':'||(SELECT count(*) FROM pg_stat_replication)::text;"]).stdout.strip()
    return {"machine_id": digest(pathlib.Path("/etc/machine-id").read_bytes()), "unit": unit.read_text(), "unit_sha256": digest(unit.read_bytes()),
            "stored_unit": copy.read_text(), "public_key": public_key, "pg_ip": pg["IPAddress"], "bridge_interface": matches[0],
            "channel_active": settings.get("ActiveState") == "active", "listeners": listeners(), "api_status": api_status(18081),
            "replicas_absent": redis_repl.get("role") == "master" and redis_repl.get("connected_slaves") == "0" and slots == "0:0",
            "firewall_rules": rules, "firewall_sha256": digest(firewall), "firewall_marker_absent": RUN not in firewall}


def target_facts():
    auth = safe_path("/home/ubuntu/.ssh/authorized_keys")
    need(auth.is_file() and stat.S_IMODE(auth.stat().st_mode) == 0o600 and auth.stat().st_uid == pwd.getpwnam("ubuntu").pw_uid, "authorized-keys-owner-or-mode-unexpected")
    settings = command(["sshd", "-T"]).stdout.splitlines()
    need("allowtcpforwarding yes" in settings and "disableforwarding no" in settings, "target-forwarding-policy-changed")
    rows = listeners()
    return {"machine_id": digest(pathlib.Path("/etc/machine-id").read_bytes()), "authorized_keys": auth.read_text(), "authorized_keys_sha256": digest(auth.read_bytes()),
            "listeners": rows, "api_status": api_status(8082) if any(row[1] == 8082 for row in rows) else None,
            "pg_forward_ok": tcp_probe(15432, struct.pack("!II", 8, 80877103)) == b"N",
            "redis_forward_ok": tcp_probe(16379, b"*1\r\n$4\r\nPING\r\n") == b"+PONG\r\n",
            "standby_containers_absent": all(docker_info(name) is None for name in ["sub2api-migration-pg-replica-20261016", "sub2api-migration-redis-standby-20261016"])}


def validate_before(source, target):
    need(source["unit"] == original_unit(source["pg_ip"]) and source["stored_unit"] == source["unit"], "original-unit-does-not-match-reviewed-channel")
    need(source["channel_active"] and loopback_exact(source["listeners"], 18081), "original-source-api-forward-not-listening")
    need(all(not any(row[1] == port for row in source["listeners"]) for port in [26379, 18092, 25432]), "new-source-port-already-in-use")
    need(source["replicas_absent"] and target["standby_containers_absent"], "replicas-already-prepared-do-not-restart-shared-channel")
    need(source["firewall_marker_absent"], "firewall-extension-already-present-review-state")
    need(loopback_exact(target["listeners"], 15432) and loopback_exact(target["listeners"], 16379), "original-target-reverse-listener-changed")
    need(target["pg_forward_ok"] and target["redis_forward_ok"], "original-data-forward-probe-failed")
    if target["api_status"] is not None:
        need(source["api_status"] == target["api_status"], "original-api-forward-probe-mismatch")
    transformed = replace_key(target["authorized_keys"], source["public_key"])
    need(transformed != target["authorized_keys"], "target-key-already-extended-review-state")
    return True


def public_inspection(source, target):
    return {"source": {k: v for k, v in source.items() if k not in ["unit", "stored_unit", "public_key"]},
            "target": {k: v for k, v in target.items() if k != "authorized_keys"}, "mutations": False}


def dry_run(restore=False):
    return {"offline_dry_run": True, "remote_commands": 0, "restore": restore,
            "unit": UNIT, "added_local_forwards": ADDED, "added_target_permitopen": ADDED_OPEN,
            "preserved": ["127.0.0.1:18081 -> Tencent 127.0.0.1:8082", "Tencent 127.0.0.1:15432 -> AWS PostgreSQL", "Tencent 127.0.0.1:16379 -> AWS Redis"],
            "firewall": "source INPUT permits only 172.18.0.2 on its inspected Docker bridge to 172.18.0.1:26379; every other source is REJECTed",
            "restart_effect": "restarts only the migration SSH unit and briefly interrupts all its existing forwards; refuses after replicas are prepared",
            "business_services_modified": False, "promotion_included": False}


def firewall_program(interface, owner):
    """Root-owned boot guard. Fail closed before the bridge listener can open."""
    return "INTERFACE="+repr(interface)+"\nOWNER="+repr(owner)+"\nRULES="+repr(firewall_rules(interface))+"\n"+r'''
import json,os,pathlib,socket,stat,subprocess
state=pathlib.Path("/home/ubuntu/.local/share/sub2api-migration-20261016/channel-extension/state.json")
assert os.geteuid()==0 and not any(p.is_symlink() for p in [state,*state.parents])
assert stat.S_IMODE(state.stat().st_mode)==0o600 and state.stat().st_uid==0
assert json.loads(state.read_text())["owner"]==OWNER
def run(argv,allowed=(0,)):
    result=subprocess.run(argv,text=True,capture_output=True,timeout=8)
    assert result.returncode in allowed, "owned firewall guard failed"
    return result
container=json.loads(run(["docker","inspect","sub2api-redis"]).stdout)[0]
networks=list(container["NetworkSettings"]["Networks"].values())
assert len(networks)==1 and networks[0]["IPAddress"]=="172.18.0.2" and networks[0]["Gateway"]=="172.18.0.1"
addresses=json.loads(run(["ip","-j","address","show","dev",INTERFACE]).stdout)
assert any(a.get("local")=="172.18.0.1" for item in addresses for a in item.get("addr_info",[]))
# This helper runs before SSH starts. Never reorder/delete guards while the
# listener is active, since that could create an unprotected interval.
with socket.socket() as probe:
    probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    probe.bind(("172.18.0.1",26379))
for rule in RULES:
    for attempt in range(8):
        exists=run(["iptables","-w","3","-C","INPUT",*rule],(0,1)).returncode==0
        if not exists: break
        run(["iptables","-w","3","-D","INPUT",*rule])
    else: raise RuntimeError("excessive owned firewall duplicates")
# Reject first, then prepend the sole permitted bridge client above the reject.
for rule in reversed(RULES): run(["iptables","-w","3","-I","INPUT","1",*rule])
for rule in RULES: run(["iptables","-w","3","-C","INPUT",*rule])
'''


def remote_scope(side):
    return safe_path((BASE if side == "source" else TARGET_BASE)+"/channel-extension")


def save_state(scope, state):
    write_private(scope/"state.json", json.dumps(state, sort_keys=True)+"\n")


def owned_state(scope, owner):
    state = json.loads(read_private(scope/"state.json"))
    need(state["owner"] == owner and state["run"] == RUN, "extension-state-owner-mismatch")
    return state


def wait_source_listeners(extended):
    expected = [["127.0.0.1", 18081]]+([[BRIDGE, 26379], ["127.0.0.1", 18092], ["127.0.0.1", 25432]] if extended else [])
    deadline = time.monotonic()+25
    while time.monotonic() < deadline:
        rows = listeners()
        if all([row for row in rows if row[1] == expected_row[1]] == [expected_row] for expected_row in expected):
            if extended or not any(row[1] in [26379, 18092, 25432] for row in rows):
                return rows
        time.sleep(.3)
    raise Refusal("channel-listeners-did-not-match-before-deadline")


def prepare_target(payload):
    facts = target_facts()
    expected = payload["target"]
    need(facts["machine_id"] == expected["machine_id"] and facts["authorized_keys_sha256"] == expected["authorized_keys_sha256"], "target-identity-or-keys-changed")
    need(facts["standby_containers_absent"] and facts["pg_forward_ok"] and facts["redis_forward_ok"], "target-preparation-state-changed")
    new = replace_key(facts["authorized_keys"], payload["public_key"])
    scope = remote_scope("target"); need(not scope.exists(), "target-extension-state-exists-review-or-restore")
    scope.mkdir(mode=0o700)
    auth = safe_path("/home/ubuntu/.ssh/authorized_keys")
    metadata = auth.stat()
    write_private(scope/"authorized_keys.before", facts["authorized_keys"])
    state = {"owner": payload["owner"], "run": RUN, "machine_id": facts["machine_id"], "public_key": payload["public_key"], "phase": "key-write-intended"}
    save_state(scope, state)
    write_private(auth, new, uid=metadata.st_uid, gid=metadata.st_gid)
    state["phase"] = "key-extended"; save_state(scope, state)
    return {"key_extended": True, "forced_command_preserved": True, "other_keys_preserved": True}


def prepare_source(payload):
    facts = source_facts(); expected = payload["source"]
    need(facts["machine_id"] == expected["machine_id"] and facts["public_key"] == payload["public_key"] and
         facts["unit"] == expected["unit"] and facts["stored_unit"] == expected["stored_unit"], "source-identity-unit-or-key-changed")
    need(facts["unit"] == original_unit(facts["pg_ip"]) and facts["channel_active"] and facts["replicas_absent"] and facts["firewall_marker_absent"], "source-no-longer-eligible-for-channel-restart")
    need(all(not any(row[1] == port for row in facts["listeners"]) for port in [26379, 18092, 25432]), "new-source-port-already-in-use")
    scope = remote_scope("source"); need(not scope.exists(), "source-extension-state-exists-review-or-restore")
    scope.mkdir(mode=0o700)
    unit, copy = safe_path(UNIT_PATH), safe_path(BASE+"/"+UNIT)
    new = extended_unit(facts["pg_ip"])
    state = {"owner": payload["owner"], "run": RUN, "machine_id": facts["machine_id"], "phase": "unit-write-intended", "unit_before_sha256": digest(facts["unit"]),
             "unit_after_sha256": digest(new), "bridge_interface": facts["bridge_interface"], "firewall_rules": facts["firewall_rules"],
             "unit_metadata": [unit.stat().st_uid, unit.stat().st_gid, stat.S_IMODE(unit.stat().st_mode)],
             "copy_metadata": [copy.stat().st_uid, copy.stat().st_gid, stat.S_IMODE(copy.stat().st_mode)]}
    write_private(scope/"unit.before", facts["unit"])
    write_private(scope/"stored-unit.before", facts["stored_unit"])
    write_private(scope/"firewall-input.before", command(["iptables", "-w", "3", "-S", "INPUT"]).stdout)
    save_state(scope, state)
    write_private(scope/"firewall.py", firewall_program(facts["bridge_interface"], payload["owner"]), mode=0o700)
    write_private(copy, new, mode=state["copy_metadata"][2], uid=state["copy_metadata"][0], gid=state["copy_metadata"][1])
    write_private(unit, new, mode=state["unit_metadata"][2], uid=state["unit_metadata"][0], gid=state["unit_metadata"][1])
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "restart", UNIT], timeout=40)
    rows = wait_source_listeners(True)
    for rule in facts["firewall_rules"]:
        command(["iptables", "-w", "3", "-C", "INPUT", *rule])
    # A local host client is outside the sole permitted Redis-container tuple.
    with socket.socket() as forbidden:
        forbidden.settimeout(2)
        need(forbidden.connect_ex((BRIDGE, 26379)) != 0, "bridge-listener-admits-forbidden-local-client")
    state["phase"] = "extended-listeners-verified"; save_state(scope, state)
    return {"source_extended": True, "listeners": rows, "firewall_rules_verified": True, "target_destinations_not_yet_required": [26379, 18092, 25432]}


def restore_target(payload):
    scope = remote_scope("target")
    if not scope.exists():
        return {"restored": True, "not_prepared": True}
    state = owned_state(scope, payload["owner"])
    need(state["machine_id"] == digest(pathlib.Path("/etc/machine-id").read_bytes()), "restore-target-machine-changed")
    auth = safe_path("/home/ubuntu/.ssh/authorized_keys"); metadata = auth.stat()
    # Preserve edits to all unrelated keys made after our private full backup.
    restored = replace_key(auth.read_text(), state["public_key"], restore=True)
    write_private(auth, restored, uid=metadata.st_uid, gid=metadata.st_gid)
    state["phase"] = "restored"; save_state(scope, state)
    return {"restored": True, "other_keys_preserved": True}


def restore_source(payload):
    scope = remote_scope("source")
    if not scope.exists():
        return {"restored": True, "not_prepared": True}
    state = owned_state(scope, payload["owner"])
    need(state["machine_id"] == digest(pathlib.Path("/etc/machine-id").read_bytes()), "restore-source-machine-changed")
    need(source_facts()["replicas_absent"], "replicas-now-active-refuse-preparation-channel-restore")
    unit, copy = safe_path(UNIT_PATH), safe_path(BASE+"/"+UNIT)
    for file in [unit, copy]:
        need(digest(file.read_bytes()) in [state["unit_before_sha256"], state["unit_after_sha256"]], "changed-unit-refuse-restore-firewall-retained")
    # Close the bridge listener before removing its protective rules.
    command(["systemctl", "stop", UNIT], timeout=40)
    for file, backup, metadata in [(unit, "unit.before", state["unit_metadata"]), (copy, "stored-unit.before", state["copy_metadata"])]:
        write_private(file, read_private(scope/backup), mode=metadata[2], uid=metadata[0], gid=metadata[1])
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "start", UNIT], timeout=40)
    rows = wait_source_listeners(False)
    for rule in state["firewall_rules"]:
        for attempt in range(8):
            result = command(["iptables", "-w", "3", "-C", "INPUT", *rule], allow_failure=True)
            need(result.returncode in [0, 1], "firewall-check-failed-protection-retained")
            if result.returncode == 1:
                break
            command(["iptables", "-w", "3", "-D", "INPUT", *rule])
        else:
            raise Refusal("excessive-firewall-duplicates-refuse-restore")
    state["phase"] = "restored"; save_state(scope, state)
    return {"restored": True, "listeners": rows, "private_evidence_retained": True}


def remote_main(payload):
    need(os.geteuid() == 0 and payload["side"] in ["source", "target"], "invalid-remote-side-or-user")
    side, action = payload["side"], payload["action"]
    if action == "inspect":
        return source_facts() if side == "source" else target_facts()
    parent = safe_path(BASE if side == "source" else TARGET_BASE)
    need(parent.is_dir(), "existing-migration-scope-missing")
    # Serialize unknown-result recovery with an in-flight mutation on this host.
    lock = os.open(parent/".channel-extension.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic()+60
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                need(time.monotonic() < deadline, "remote-mutation-still-running-use-state-before-retry")
                time.sleep(.25)
        if action == "prepare":
            return prepare_source(payload) if side == "source" else prepare_target(payload)
        need(action == "restore", "invalid-remote-action")
        return restore_source(payload) if side == "source" else restore_target(payload)
    finally:
        os.close(lock)


class Controller:
    def __init__(self, directory):
        self.directory = safe_path(directory)
        self.program = pathlib.Path(__file__).read_text()

    def remote(self, side, action, **payload):
        payload.update(side=side, action=action)
        ssh = SOURCE if side == "source" else TARGET
        try:
            result = subprocess.run(ssh+["sudo -n python3 -"], input="REMOTE_PAYLOAD="+repr(payload)+"\n"+self.program,
                                    text=True, capture_output=True, timeout=150)
        except subprocess.TimeoutExpired:
            raise Refusal(side+"-channel-action-timeout-review-owned-state") from None
        # Do not echo remote stderr, authorized_keys or shell exceptions.
        try:
            reply = json.loads(result.stdout)
        except ValueError:
            raise Refusal(side+"-invalid-private-reply-review-owned-state") from None
        need(result.returncode == 0 and reply.get("ok"), side+"-"+reply.get("error", "remote-operation-failed"))
        return reply["result"]

    def inspect(self):
        # The target management path uses the Mac's independent HTTP CONNECT
        # proxy and identity. It must not depend on the channel being restarted.
        target = self.remote("target", "inspect")
        source = self.remote("source", "inspect")
        return source, target

    def verify(self, source_before, target_before, *, extended):
        source, target = self.inspect()
        need(source["machine_id"] == source_before["machine_id"] and target["machine_id"] == target_before["machine_id"], "verification-machine-changed")
        need(source["channel_active"] and source["unit"] == (extended_unit(source["pg_ip"]) if extended else original_unit(source["pg_ip"])), "verification-unit-or-service-failed")
        need(loopback_exact(target["listeners"], 15432) and loopback_exact(target["listeners"], 16379) and target["pg_forward_ok"] and target["redis_forward_ok"], "verification-existing-data-forward-failed")
        need(loopback_exact(source["listeners"], 18081), "verification-api-bridge-bind-failed")
        if target_before["api_status"] is not None:
            need(target["api_status"] == source["api_status"] == target_before["api_status"], "verification-api-health-changed")
        return public_inspection(source, target)

    def restore(self, state):
        # Restore the source first; retain firewall if the listener cannot be
        # closed or an operator changed the unit. Never reopen an unguarded bind.
        source = self.remote("source", "restore", owner=state["owner"])
        target = self.remote("target", "restore", owner=state["owner"])
        proof = self.verify(state["source"], state["target"], extended=False)
        state["phase"] = "restored"; write_private(self.directory/"state.json", json.dumps(state)+"\n")
        return {"restored": True, "source": source, "target": target, "verification": proof}

    def apply(self):
        source, target = self.inspect(); validate_before(source, target)
        need(not self.directory.exists(), "local-extension-state-exists-use-inspect-or-restore")
        self.directory.mkdir(mode=0o700, parents=True)
        os.chmod(self.directory, 0o700)
        lock = os.open(self.directory/"workflow.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {"owner": secrets.token_hex(16), "run": RUN, "source": source, "target": target, "phase": "before-mutation"}
        write_private(self.directory/"unit.before", source["unit"])
        write_private(self.directory/"authorized_keys.before", target["authorized_keys"])
        write_private(self.directory/"state.json", json.dumps(state)+"\n")
        try:
            self.remote("target", "prepare", owner=state["owner"], public_key=source["public_key"], target=target)
            self.remote("source", "prepare", owner=state["owner"], public_key=source["public_key"], source=source)
            proof = self.verify(source, target, extended=True)
            state["phase"] = "verified"; write_private(self.directory/"state.json", json.dumps(state)+"\n")
            return {"extended": True, "verification": proof, "business_services_modified": False, "promotion_included": False}
        except Exception:
            try:
                self.restore(state)
            except Exception:
                raise Refusal("extension-failed-restore-incomplete-review-private-state-and-preserved-firewall") from None
            raise Refusal("extension-failed-original-channel-restored-and-verified") from None
        finally:
            os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--state-dir", default=str(pathlib.Path.home()/".local/share/sub2api-migration-20261016/channel-extension"))
    args = parser.parse_args(argv)
    need(not args.inspect or not (args.apply or args.restore), "inspect-cannot-authorize-mutation")
    if not args.inspect and not args.apply:
        print(json.dumps(dry_run(args.restore), indent=2)); return 0
    controller = Controller(pathlib.Path(args.state_dir).absolute())
    if args.inspect:
        source, target = controller.inspect()
        result = public_inspection(source, target)
        try:
            validate_before(source, target); result["eligible_for_extension"] = True
        except Refusal as error:
            result.update(eligible_for_extension=False, reason=str(error))
    elif args.restore:
        lock = os.open(safe_path(controller.directory/"workflow.lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refusal("local-controller-running-do-not-race-restore") from None
            state = json.loads(read_private(controller.directory/"state.json"))
            need(state["run"] == RUN, "local-state-run-mismatch")
            result = controller.restore(state)
        finally:
            os.close(lock)
    else:
        result = controller.apply()
    print(json.dumps(result, indent=2)); return 0


if __name__ == "__main__":
    try:
        if "REMOTE_PAYLOAD" in globals():
            print(json.dumps({"ok": True, "result": remote_main(REMOTE_PAYLOAD)}))
        else:
            raise SystemExit(main())
    except (Refusal, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, Refusal) else "operation-failed-review-private-state"
        print(json.dumps({"ok": False, "error": reason})); raise SystemExit(1)
