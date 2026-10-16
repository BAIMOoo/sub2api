#!/usr/bin/env python3
"""Prepare the migration-only SSH channel. This never routes production traffic.

The source generates and retains its private key. The target accepts that key
only from the source IP and only for three loopback forwards. Existing sshd,
Caddy, Docker containers, databases and DNS are not reconfigured.
"""

import argparse
import json
import pathlib
import subprocess

SOURCE = "sub2api"
TARGET_IP = "43.165.177.136"
SOURCE_IP = "13.231.143.136"
SCOPE = "/home/ubuntu/.local/share/sub2api-migration-20261016"
UNIT = "sub2api-migration-channel-20261016.service"
TARGET = [
    "ssh", "-o", "ProxyCommand=nc -X connect -x 127.0.0.1:7897 %h %p",
    "-o", "ProxyJump=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=yes", "-i", str(pathlib.Path.home()/".ssh/tokyo.pem"),
    "-o", "IdentitiesOnly=yes", "ubuntu@"+TARGET_IP,
]
SSH_SOURCE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SOURCE]


def run(argv, script=None):
    result = subprocess.run(argv, input=script, text=True, capture_output=True, timeout=60)
    if result.returncode:
        # These scripts never emit private keys, passwords or environment data.
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def remote(argv, script):
    return run(argv+["python3 -"], script)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print("Creates restricted migration SSH key and supervised loopback-only forwards; no production traffic changes. Use --apply to prepare.")
        return

    host_lines = run(["ssh-keygen", "-F", TARGET_IP, "-f", str(pathlib.Path.home()/".ssh/known_hosts")])
    host_lines = "\n".join(line for line in host_lines.splitlines() if line and not line.startswith("#"))+"\n"
    if not any("ssh-ed25519 " in line for line in host_lines.splitlines()):
        raise RuntimeError("verified target Ed25519 host key is missing")

    facts = json.loads(remote(SSH_SOURCE, f'''
import json,os,pathlib,subprocess
p=pathlib.Path({SCOPE!r})
if p.is_symlink(): raise SystemExit("refuse symlink scope")
p.mkdir(mode=0o700,parents=True,exist_ok=True)
os.chmod(p,0o700)
key=p/"tunnel_ed25519"
if key.is_symlink(): raise SystemExit("refuse symlink key")
if not key.exists():
    subprocess.run(["ssh-keygen","-q","-t","ed25519","-N","","-C","sub2api-live-migration-20261016","-f",str(key)],check=True)
os.chmod(key,0o600)
hosts=p/"known_hosts"
if hosts.is_symlink(): raise SystemExit("refuse symlink hosts")
hosts.write_text({host_lines!r});os.chmod(hosts,0o600)
ips={{}}
for name in ["sub2api-postgres","sub2api-redis"]:
    data=json.loads(subprocess.check_output(["docker","inspect",name],text=True))[0]
    addresses=[n["IPAddress"] for n in data["NetworkSettings"]["Networks"].values() if n.get("IPAddress")]
    if len(addresses)!=1: raise SystemExit("ambiguous source container network")
    ips[name]=addresses[0]
print(json.dumps({{"key":key.with_suffix(".pub").read_text().strip(),"ips":ips}}))
'''))
    key = facts["key"]
    parts = key.split()
    if len(parts)!=3 or parts[0]!="ssh-ed25519" or parts[2]!="sub2api-live-migration-20261016":
        raise RuntimeError("unexpected migration public key")
    auth_line = (
        f'from="{SOURCE_IP}",restrict,port-forwarding,command="/bin/false",'
        'permitopen="127.0.0.1:8082",permitlisten="127.0.0.1:15432",'
        'permitlisten="127.0.0.1:16379" '+key
    )
    remote(TARGET, f'''
import os,pathlib,subprocess,time
settings=subprocess.check_output(["sudo","sshd","-T"],text=True)
if "allowtcpforwarding yes" not in settings: raise SystemExit("sshd forwarding is not enabled; no sshd changes made")
p=pathlib.Path.home()/".ssh";p.mkdir(mode=0o700,exist_ok=True)
f=p/"authorized_keys"
if f.is_symlink(): raise SystemExit("refuse symlink authorized_keys")
old=f.read_text() if f.exists() else ""
line={auth_line!r}
if "sub2api-live-migration-20261016" in old and line not in old.splitlines():
    raise SystemExit("existing migration key does not match; no overwrite")
if line not in old.splitlines():
    backup=p/("authorized_keys.before-sub2api-migration-"+str(time.time_ns()))
    backup.write_text(old);os.chmod(backup,0o600)
    with f.open("a") as out:
        if old and not old.endswith("\\n"):out.write("\\n")
        out.write(line+"\\n");out.flush();os.fsync(out.fileno())
    os.chmod(f,0o600)
''')
    pg_ip, redis_ip = facts["ips"]["sub2api-postgres"], facts["ips"]["sub2api-redis"]
    import ipaddress
    ipaddress.IPv4Address(pg_ip)
    ipaddress.IPv4Address(redis_ip)
    command = [
        "/usr/bin/ssh", "-F", "/dev/null", "-NT", "-i", SCOPE+"/tunnel_ed25519",
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile="+SCOPE+"/known_hosts", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=8",
        "-L", "127.0.0.1:18081:127.0.0.1:8082",
        "-R", "127.0.0.1:15432:"+pg_ip+":5432",
        "-R", "127.0.0.1:16379:"+redis_ip+":6379", "ubuntu@"+TARGET_IP,
    ]
    unit = f'''[Unit]
Description=Sub2API migration-only encrypted loopback channel
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
ExecStart={" ".join(command)}
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
'''
    remote(SSH_SOURCE, f'''
import pathlib,subprocess
p=pathlib.Path({SCOPE!r})/{UNIT!r}
target=pathlib.Path("/etc/systemd/system")/{UNIT!r}
desired={unit!r}
if target.exists() and target.read_text()!=desired:
    raise SystemExit("unit already differs; refuse implicit restart")
p.write_text(desired);p.chmod(0o600)
if not target.exists():
    subprocess.run(["sudo","install","-o","root","-g","root","-m","0644",str(p),str(target)],check=True)
    subprocess.run(["sudo","systemctl","daemon-reload"],check=True)
subprocess.run(["sudo","systemctl","enable","--now",{UNIT!r}],check=True,stdout=subprocess.DEVNULL)
''')
    print(json.dumps({"prepared":True,"unit":UNIT,"source_api_bridge":"127.0.0.1:18081","target_source_postgres":"127.0.0.1:15432","target_source_redis":"127.0.0.1:16379","production_traffic_changed":False}))


if __name__ == "__main__":
    main()
