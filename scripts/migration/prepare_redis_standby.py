#!/usr/bin/env python3
"""Prepare a separate Redis 8.6.2 standby; offline dry-run is the default.

--inspect is read-only. --apply creates only the owned target container,
configuration and persistent volume, then starts replication through the existing
target loopback tunnel. There is no promotion or source configuration mutation.
The source must currently use the unauthenticated, diskless-sync configuration
verified by --inspect; credential-bearing configurations deliberately fail closed.
"""
from __future__ import annotations

import argparse
import inspect
import json
import pathlib
import re
import secrets
import subprocess
import sys

RUN = "sub2api-redis-standby-20261016"
CONTAINER = "sub2api-migration-redis-standby-20261016"
VOLUME = "sub2api-migration-redis-data-20261016"
IMAGE = "redis:migration-20261016-b95f59534a91"
CONFIG_DIGEST = "sha256:b95f59534a9112e82c68677a05afa0c69dd4d75c9fdc7114b98594a28ad79f13"
TARGET_SCOPE = "/home/ubuntu/sub2api-migration-20261016/redis-standby"
PORT = 26379
SOURCE_CHANNEL_PORT = 16379
ROLE_SWAP_ANNOUNCE_IP = "172.18.0.1"
RUNTIME_CONFIG = "/data/migration.conf"
MIB = 1024**2
GIB = 1024**3
MEMORY_LIMIT = 256*MIB
LABEL = "io.sub2api.migration.owner"
SOURCE = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", "-oStrictHostKeyChecking=yes", "sub2api"]
TARGET = ["ssh", "-oProxyCommand=nc -X connect -x 127.0.0.1:7897 %h %p", "-oProxyJump=none",
          "-oBatchMode=yes", "-oConnectTimeout=10", "-oStrictHostKeyChecking=yes",
          "-i", str(pathlib.Path.home()/".ssh/tokyo.pem"), "-oIdentitiesOnly=yes", "ubuntu@43.165.177.136"]


class Refusal(RuntimeError):
    """A bounded, credential-free reason to refuse this operation."""


def require(condition, code):
    if not condition:
        raise Refusal(code)


def remote(ssh, script, *, timeout=30):
    result = subprocess.run(ssh+["sudo -n python3 -"], input=script, text=True,
                            capture_output=True, timeout=timeout)
    # Do not publish raw subprocess stderr or remote exception details: future
    # Docker/Redis configuration errors could contain credentials.
    require(result.returncode == 0, "remote-operation-failed-review-protected-server-state")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise Refusal("remote-operation-returned-invalid-public-json") from None


REMOTE_COMMON = r'''
import hashlib,json,os,pathlib,socket,stat,subprocess,time
def need(ok, code):
    if not ok: raise RuntimeError(code)
def info(text):
    return dict(line.split(":",1) for line in text.splitlines() if line and not line.startswith("#") and ":" in line)
def redis(port, *parts):
    # A fixed loopback RESP connection deliberately avoids REDISCLI_AUTH,
    # process argv credentials and accidental environment authentication.
    with socket.create_connection(("127.0.0.1",port),timeout=4) as s:
        encoded=[str(p).encode() for p in parts]
        s.sendall(("*%d\r\n"%len(encoded)).encode()+b"".join(("$%d\r\n"%len(p)).encode()+p+b"\r\n" for p in encoded))
        f=s.makefile("rb")
        def read():
            line=f.readline(4096);need(line.endswith(b"\r\n"),"redis-response-truncated")
            kind,payload=line[:1],line[1:-2]
            if kind==b"+": return payload.decode()
            if kind==b":": return int(payload)
            if kind==b"-": raise RuntimeError("redis-refused-unauthenticated-inspection")
            if kind==b"$":
                n=int(payload);need(0<=n<=8*1024*1024,"redis-bulk-size")
                body=f.read(n);need(len(body)==n and f.read(2)==b"\r\n","redis-bulk-truncated")
                return body.decode()
            if kind==b"*":
                n=int(payload);need(0<=n<=10000,"redis-array-size");return [read() for _ in range(n)]
            raise RuntimeError("redis-response-type")
        try: return read()
        finally: f.close()
def disk(path):
    v=os.statvfs(path);return {"free_bytes":v.f_bavail*v.f_frsize,"total_bytes":v.f_blocks*v.f_frsize}
def available_memory():
    values=dict(line.split(":",1) for line in pathlib.Path("/proc/meminfo").read_text().splitlines())
    return int(values["MemAvailable"].split()[0])*1024
def config_digest(image):
    # Docker 29 containerd stores use manifest IDs; older stores use Config IDs.
    if image["Id"]==EXPECTED_CONFIG: return image["Id"]
    descriptor=json.loads(subprocess.check_output(["ctr","-n","moby","content","get",image["Id"]],text=True))
    return descriptor["config"]["digest"]
def no_symlinks(path):
    need(not any(p.is_symlink() for p in [path,*path.parents]),"path-symlink")
'''


def source_inspection_script():
    # Redis's original container exposes only its Docker-network address, so
    # execute its own redis-cli with the stale REDISCLI_AUTH variable cleared.
    return "EXPECTED_CONFIG="+repr(CONFIG_DIGEST)+"\n"+REMOTE_COMMON+r'''
def cli(*args):
    return subprocess.check_output(["docker","exec","-e","REDISCLI_AUTH=","sub2api-redis","redis-cli","--raw",*args],text=True,stderr=subprocess.DEVNULL)
container=json.loads(subprocess.check_output(["docker","inspect","sub2api-redis"],text=True))[0]
image=json.loads(subprocess.check_output(["docker","image","inspect",container["Image"]],text=True))[0]
sections={s:info(cli("INFO",s)) for s in ["server","memory","persistence","replication","keyspace"]}
need("redis_version" in sections["server"] and "role" in sections["replication"],"source-auth-or-info-unavailable")
names=["dir","dbfilename","appendonly","appendfsync","save","repl-diskless-sync","repl-diskless-load","repl-backlog-size","repl-timeout","maxmemory","maxmemory-policy","replica-read-only"]
rows=cli("CONFIG","GET",*names).splitlines();settings=dict(zip(rows[::2],rows[1::2]))
mounts=[m for m in container["Mounts"] if m["Destination"]=="/data" and m["RW"]]
need(len(mounts)==1 and mounts[0]["Type"]=="volume","source-persistent-volume-ambiguous")
networks=list(container["NetworkSettings"]["Networks"].values());need(len(networks)==1,"source-network-ambiguous")
def selected(section, keys): return {k:sections[section][k] for k in keys if k in sections[section]}
result={"server":selected("server",["redis_version","run_id","arch_bits"]),
"memory":selected("memory",["used_memory","used_memory_rss","used_memory_peak","maxmemory","maxmemory_policy"]),
"persistence":selected("persistence",["loading","rdb_bgsave_in_progress","rdb_last_bgsave_status","aof_enabled","aof_rewrite_in_progress","aof_last_bgrewrite_status","aof_last_write_status"]),
"replication":selected("replication",["role","master_replid","master_repl_offset","connected_slaves","repl_backlog_active","master_failover_state"]),
"keyspace":sections["keyspace"],"config":settings,"image_config_digest":config_digest(image),"rootfs_layers":image["RootFS"]["Layers"],
"redis_uid":int(subprocess.check_output(["docker","exec","sub2api-redis","id","-u","redis"],text=True)),
"redis_gid":int(subprocess.check_output(["docker","exec","sub2api-redis","id","-g","redis"],text=True)),
"disk":disk(mounts[0]["Source"]),"available_memory_bytes":available_memory(),
"data_bytes":int(subprocess.check_output(["du","-sb",mounts[0]["Source"]],text=True).split()[0]),
"docker_network":{"redis_ip":networks[0]["IPAddress"],"bridge_gateway":networks[0]["Gateway"]}}
print(json.dumps(result))
'''


def target_inspection_script():
    constants = {"EXPECTED_CONFIG": CONFIG_DIGEST, "IMAGE": IMAGE, "SCOPE": TARGET_SCOPE,
                 "CONTAINER": CONTAINER, "VOLUME": VOLUME, "PORT": PORT, "CHANNEL": SOURCE_CHANNEL_PORT}
    return "\n".join(k+"="+repr(v) for k, v in constants.items())+"\n"+REMOTE_COMMON+r'''
image=json.loads(subprocess.check_output(["docker","image","inspect",IMAGE],text=True))[0]
repl=info(redis(CHANNEL,"INFO","replication"));server=info(redis(CHANNEL,"INFO","server"))
def exists(kind, name): return subprocess.run(["docker",kind,"inspect",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
s=socket.socket();port_free=True
try: s.bind(("127.0.0.1",PORT))
except OSError: port_free=False
finally: s.close()
scope=pathlib.Path(SCOPE);no_symlinks(scope)
print(json.dumps({"machine_id_sha256":hashlib.sha256(pathlib.Path("/etc/machine-id").read_bytes()).hexdigest(),
"image_config_digest":config_digest(image),"rootfs_layers":image["RootFS"]["Layers"],"architecture":image["Architecture"],
"source_channel":{"run_id":server.get("run_id"),"redis_version":server.get("redis_version"),"role":repl.get("role"),"master_replid":repl.get("master_replid")},
"disk":disk(scope.parent),"available_memory_bytes":available_memory(),"scope_exists":scope.exists(),
"container_exists":exists("container",CONTAINER),"volume_exists":exists("volume",VOLUME),"standby_port_free":port_free}))
'''


def validate_inspection(source, target):
    require(source["server"]["redis_version"] == "8.6.2", "source-redis-version-changed")
    require(source["replication"]["role"] == "master", "source-is-not-primary")
    require(source["replication"]["master_failover_state"] == "no-failover" and source["replication"]["connected_slaves"] == "0",
            "source-failover-or-other-replication-active")
    require(source["replication"]["repl_backlog_active"] in {"0", "1"}, "source-backlog-state-invalid")
    require(target["source_channel"]["role"] == "master", "source-channel-is-not-primary")
    require(source["server"]["run_id"] == target["source_channel"]["run_id"], "source-channel-identity-mismatch")
    require(source["replication"]["master_replid"] == target["source_channel"]["master_replid"], "source-channel-replication-identity-mismatch")
    require(source["image_config_digest"] == target["image_config_digest"] == CONFIG_DIGEST, "redis-image-config-mismatch")
    require(source["rootfs_layers"] == target["rootfs_layers"] and len(source["rootfs_layers"]) == 7, "redis-image-layers-mismatch")
    require(target["architecture"] == "amd64", "target-architecture-mismatch")
    require(source["docker_network"]["bridge_gateway"] == ROLE_SWAP_ANNOUNCE_IP,
            "source-redis-bridge-gateway-changed-review-role-swap-route")
    require(all(isinstance(source[k],int) and 0 < source[k] <= 65535 for k in ["redis_uid","redis_gid"]),
            "source-redis-user-identity-invalid")
    p = source["persistence"]
    require(p["loading"] == "0" and p["rdb_bgsave_in_progress"] == "0" and p["aof_rewrite_in_progress"] == "0", "source-persistence-work-active")
    require(p["rdb_last_bgsave_status"] == p["aof_last_bgrewrite_status"] == p["aof_last_write_status"] == "ok", "source-persistence-not-healthy")
    settings = source["config"]
    require(settings["dir"] == "/data" and settings["dbfilename"] == "dump.rdb", "source-data-path-changed")
    require(settings["appendonly"] == "no" and p["aof_enabled"] == "0", "source-persistence-policy-changed")
    require(settings["maxmemory"] == "0" and settings["maxmemory-policy"] == "noeviction", "source-memory-policy-changed")
    require(settings["repl-diskless-sync"] == "yes" and settings["repl-diskless-load"] == "disabled", "source-replication-disk-policy-changed")
    require(source["disk"]["free_bytes"] >= 2*GIB and target["disk"]["free_bytes"] >= 2*GIB, "insufficient-replication-disk-reserve")
    peak = max(int(source["memory"]["used_memory_peak"]), int(source["memory"]["used_memory_rss"]))
    require(peak*4 < MEMORY_LIMIT, "redis-standby-memory-headroom-insufficient")
    require(source["available_memory_bytes"] >= max(256*MIB, peak*4), "source-fork-memory-headroom-insufficient")
    require(target["available_memory_bytes"] >= 512*MIB, "target-memory-reserve-insufficient")
    require(not any(target[k] for k in ["scope_exists", "container_exists", "volume_exists"]), "target-standby-resources-already-exist")
    require(target["standby_port_free"], "target-standby-port-in-use")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", source["server"]["run_id"])), "source-run-id-invalid")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", source["replication"]["master_replid"])), "source-replid-invalid")
    return {"inspection_passed": True, "source": source, "target": target}


def render_config(source):
    settings = source["config"]
    schedule = settings["save"]
    require(bool(re.fullmatch(r"\d+ \d+(?: \d+ \d+)*", schedule)), "source-snapshot-schedule-invalid")
    require(settings["appendfsync"] in {"always", "everysec", "no"}, "source-appendfsync-invalid")
    require(settings["repl-timeout"].isdigit() and int(settings["repl-timeout"]) >= 10, "source-replication-timeout-invalid")
    return "\n".join([
        "# Owned migration standby; contains no credential material.", "bind 127.0.0.1", "protected-mode yes",
        f"port {PORT}", "daemonize no", "dir /data", "dbfilename dump.rdb", "save "+schedule,
        "appendonly no", "appendfsync "+settings["appendfsync"], "maxmemory 0", "maxmemory-policy noeviction",
        f"replicaof 127.0.0.1 {SOURCE_CHANNEL_PORT}", "replica-read-only yes", "replica-serve-stale-data no",
        "repl-diskless-sync yes", "repl-diskless-load disabled", "repl-timeout "+settings["repl-timeout"],
        "replica-announce-ip "+ROLE_SWAP_ANNOUNCE_IP, f"replica-announce-port {PORT}", "repl-backlog-size 16mb", "logfile \"\"", "",
    ])


def source_epoch_allowed(server, replication, run_id, initial_repl_id, initial_backlog_active):
    # Redis rotates replid when creating a missing backlog for its first new
    # replica, without restarting or changing the primary's role. Process run_id
    # is the stable source identity. This exception applies only to that initial
    # absent->active backlog transition; an initially active epoch must not drift.
    observed = replication.get("master_replid", "")
    identity_ok = (server.get("run_id") == run_id and replication.get("role") == "master"
                   and replication.get("master_failover_state") == "no-failover"
                   and len(observed) == 40 and all(c in "0123456789abcdef" for c in observed))
    return identity_ok and (observed == initial_repl_id or
                           (initial_backlog_active == "0" and replication.get("repl_backlog_active") == "1"))


def container_command(owner):
    require(bool(re.fullmatch(r"[0-9a-f]{32}", owner)), "standby-owner-invalid")
    return ["docker", "create", "--name", CONTAINER, "--label", LABEL+"="+owner,
            "--label", "io.sub2api.migration.run="+RUN, "--network", "host",
            "--memory", str(MEMORY_LIMIT), "--memory-swap", str(MEMORY_LIMIT), "--cpus", "0.5",
            "--pids-limit", "64", "--restart", "no", "--security-opt", "no-new-privileges:true", "--cap-drop", "NET_RAW",
            "--mount", "type=volume,src="+VOLUME+",dst=/data,volume-nocopy",
            # Keep the official entrypoint's privilege dropping and permission
            # checks. Initial setup owns /data and config explicitly because
            # newer entrypoints skip unknown data files. Redis needs both writable
            # because CONFIG REWRITE replaces this file with an atomic rename.
            IMAGE, "sh", "-ceu", startup_shell()]


def startup_shell():
    # The exact official image appends its default --loadmodule arguments on
    # every boot. CONFIG REWRITE also persists those loadmodule statements;
    # without removing that generated duplication a second boot aborts. Only
    # parameter-free modules in the image's auto-loaded directory are accepted.
    # Run before the original entrypoint drops privileges, then delegate all
    # normal startup/permission/module behavior back to that entrypoint.
    return r'''set -eu
config=/data/migration.conf
if grep -Eq '^[[:space:]]*loadmodule[[:space:]]' "$config"; then
    temporary="$config.startup.$$"
    trap 'rm -f "$temporary"' EXIT HUP INT TERM
    awk '
        $1 == "loadmodule" {
            if (NF != 2 || $2 !~ /^\/usr\/local\/lib\/redis\/modules\/+[^\/]+\.so$/) exit 81
            next
        }
        { print }
    ' "$config" > "$temporary"
    chown redis:redis "$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$config"
    sync
    trap - EXIT HUP INT TERM
fi
exec /usr/local/bin/docker-entrypoint.sh redis-server "$config"
'''


def apply_script(inspection, owner):
    config = render_config(inspection["source"])
    expected = inspection["source"]
    constants = {"EXPECTED_CONFIG": CONFIG_DIGEST, "IMAGE": IMAGE, "SCOPE": TARGET_SCOPE, "CONTAINER": CONTAINER,
                 "VOLUME": VOLUME, "OWNER": owner, "LABEL": LABEL, "RUN": RUN, "CONFIG": config,
                 "CREATE": container_command(owner), "PORT": PORT, "CHANNEL": SOURCE_CHANNEL_PORT,
                 "RUNTIME_CONFIG": RUNTIME_CONFIG, "ANNOUNCE_IP": ROLE_SWAP_ANNOUNCE_IP,
                 "EXPECTED_CMD": ["sh", "-ceu", startup_shell()],
                 "REDIS_UID": expected["redis_uid"], "REDIS_GID": expected["redis_gid"],
                 "RUN_ID": expected["server"]["run_id"], "REPL_ID": expected["replication"]["master_replid"],
                 "INITIAL_BACKLOG_ACTIVE": expected["replication"]["repl_backlog_active"],
                 "MACHINE_ID": inspection["target"]["machine_id_sha256"]}
    return "\n".join(k+"="+repr(v) for k, v in constants.items())+"\n"+REMOTE_COMMON+"\n"+inspect.getsource(source_epoch_allowed)+r'''
need(hashlib.sha256(pathlib.Path("/etc/machine-id").read_bytes()).hexdigest()==MACHINE_ID,"target-identity-changed")
image=json.loads(subprocess.check_output(["docker","image","inspect",IMAGE],text=True))[0]
need(config_digest(image)==EXPECTED_CONFIG,"target-image-changed")
server=info(redis(CHANNEL,"INFO","server"));repl=info(redis(CHANNEL,"INFO","replication"))
need(server.get("run_id")==RUN_ID and repl.get("master_replid")==REPL_ID and repl.get("role")=="master","source-identity-changed")
scope=pathlib.Path(SCOPE);no_symlinks(scope);need(not scope.exists(),"target-scope-already-exists")
for kind,name in [("container",CONTAINER),("volume",VOLUME)]:
    need(subprocess.run(["docker",kind,"inspect",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0,"target-resource-already-exists")
need(disk(scope.parent)["free_bytes"]>=2*1024**3,"target-disk-reserve-lost")
need(available_memory()>=512*1024**2,"target-memory-reserve-lost")
probe=socket.socket()
try: probe.bind(("127.0.0.1",PORT))
finally: probe.close()
scope.mkdir(mode=0o700)
conf=scope/"initial-redis.conf"
fd=os.open(conf,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,"w") as out: out.write(CONFIG);out.flush();os.fsync(out.fileno())
# Audit input is kept privately on the host. The actual runtime config is in
# the owned data volume, so successful role changes can survive a restart.
os.chmod(conf,0o600)
state=scope/"ownership.json"
state.write_text(json.dumps({"owner":OWNER,"run":RUN,"source_run_id":RUN_ID,"source_replid":REPL_ID,"container":CONTAINER,"volume":VOLUME})+"\n");state.chmod(0o600)
subprocess.run(["docker","volume","create","--label",LABEL+"="+OWNER,"--label","io.sub2api.migration.run="+RUN,VOLUME],check=True,stdout=subprocess.DEVNULL)
volume=json.loads(subprocess.check_output(["docker","volume","inspect",VOLUME],text=True))[0]
need(volume.get("Labels",{}).get(LABEL)==OWNER and volume.get("Labels",{}).get("io.sub2api.migration.run")==RUN,"target-volume-ownership-mismatch")
need(volume.get("Driver")=="local" and volume.get("Scope")=="local","target-volume-driver-or-scope-unreviewed")
data=pathlib.Path(volume["Mountpoint"]);no_symlinks(data)
need(data.is_dir() and not any(data.iterdir()),"target-volume-not-empty")
runtime_conf=data/"migration.conf"
fd=os.open(runtime_conf,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
with os.fdopen(fd,"w") as out: out.write(CONFIG);out.flush();os.fsync(out.fileno())
os.chmod(runtime_conf,0o644)
os.chown(runtime_conf,REDIS_UID,REDIS_GID)
os.chown(data,REDIS_UID,REDIS_GID)
subprocess.run(CREATE,check=True,stdout=subprocess.DEVNULL)
created=json.loads(subprocess.check_output(["docker","inspect",CONTAINER],text=True))[0]
need(created["Config"]["Labels"].get(LABEL)==OWNER and created["Image"]==image["Id"],"target-created-container-image-or-owner-changed")
need(created["Config"]["Cmd"]==EXPECTED_CMD,"target-runtime-config-command-changed")
subprocess.run(["docker","start",CONTAINER],check=True,stdout=subprocess.DEVNULL)
deadline=time.monotonic()+90
active_replid=None
while True:
    need(time.monotonic()<deadline,"target-initial-sync-timeout-resources-retained")
    before=info(redis(CHANNEL,"INFO","replication"))
    source_server=info(redis(CHANNEL,"INFO","server"))
    need(source_epoch_allowed(source_server,before,RUN_ID,REPL_ID,INITIAL_BACKLOG_ACTIVE),"source-role-process-or-active-epoch-changed")
    if before.get("repl_backlog_active")=="1":
        if active_replid is None: active_replid=before["master_replid"]
        need(active_replid==before["master_replid"],"source-active-replication-epoch-changed")
    try:
        target=info(redis(PORT,"INFO","replication"));persist=info(redis(PORT,"INFO","persistence"))
        healthy=(target.get("role")=="slave" and target.get("master_host")=="127.0.0.1" and target.get("master_port")==str(CHANNEL)
                 and target.get("master_link_status")=="up" and target.get("master_sync_in_progress")=="0"
                 and active_replid is not None and target.get("master_replid")==active_replid and int(target.get("slave_repl_offset","-1"))>=int(before["master_repl_offset"])
                 and persist.get("loading")=="0")
        if healthy: break
    except (OSError,RuntimeError): pass
    time.sleep(.5)
after=info(redis(CHANNEL,"INFO","replication"));server=info(redis(CHANNEL,"INFO","server"))
need(source_epoch_allowed(server,after,RUN_ID,REPL_ID,INITIAL_BACKLOG_ACTIVE) and after.get("master_replid")==active_replid,"source-role-or-identity-changed")
rows=redis(PORT,"CONFIG","GET","replica-read-only","bind","port","dir","replica-announce-ip","replica-announce-port");settings=dict(zip(rows[::2],rows[1::2]))
need(settings=={"replica-read-only":"yes","bind":"127.0.0.1","port":str(PORT),"dir":"/data","replica-announce-ip":ANNOUNCE_IP,"replica-announce-port":str(PORT)},"target-isolation-or-readonly-setting-changed")
need(info(redis(PORT,"INFO","server")).get("config_file")==RUNTIME_CONFIG,"target-runtime-config-path-changed")
# This changes only the owned target's persisted configuration, not its role.
# It proves Redis can replace the initialized writable /data config.
need(redis(PORT,"CONFIG","REWRITE")=="OK","target-runtime-config-not-rewriteable")
need(not runtime_conf.is_symlink() and runtime_conf.is_file() and runtime_conf.stat().st_uid==REDIS_UID and data.stat().st_uid==REDIS_UID,"target-runtime-config-not-owned-by-redis")
container=json.loads(subprocess.check_output(["docker","inspect",CONTAINER],text=True))[0]
need(container["Config"]["Labels"].get(LABEL)==OWNER and container["HostConfig"]["NetworkMode"]=="host","target-resource-ownership-changed")
result={"prepared":True,"standby_role":"slave","replication_link":"up","source_remains_primary":True,"target_port":PORT,
        "source_channel_port":CHANNEL,"caught_up_to_sampled_source_offset":int(before["master_repl_offset"]),
        "standby_offset":int(target["slave_repl_offset"]),"container":CONTAINER,"volume":VOLUME,
        "source_configuration_changed":False,"production_traffic_changed":False,"promoted":False}
result.update(runtime_config=RUNTIME_CONFIG,runtime_config_rewrite_verified=True,replica_announce_address=ANNOUNCE_IP+":"+str(PORT))
result.update(source_replid=active_replid,initial_source_replid=REPL_ID,first_backlog_replid_rotated=active_replid!=REPL_ID)
evidence=scope/"verification.json";evidence.write_text(json.dumps(result,sort_keys=True)+"\n");evidence.chmod(0o600)
print(json.dumps(result))
'''


def dry_run_plan():
    return {"mode": "offline-dry-run", "remote_operations": 0, "image": IMAGE, "image_config_digest": CONFIG_DIGEST,
            "source_redis_version": "8.6.2", "target_port": "127.0.0.1:"+str(PORT),
            "replication_source": "127.0.0.1:"+str(SOURCE_CHANNEL_PORT), "container": CONTAINER, "volume": VOLUME,
            "persistence": "separate persistent volume; preserve source RDB schedule and disabled AOF",
            "runtime_config": RUNTIME_CONFIG+" is writable by Redis and retained in the owned volume; CONFIG REWRITE is verified",
            "target_container_memory_limit_bytes": MEMORY_LIMIT,
            "inspect": "--inspect performs read-only host, image, persistence, memory, disk and channel checks",
            "apply": "--apply --expected-source-run-id RUN_ID creates only target standby resources and starts replication",
            "role_swap_network_prerequisite": {
                "required_later": "source Docker bridge 172.18.0.1:26379 -> encrypted SSH forward -> target 127.0.0.1:26379",
                "ssh_key_change": "existing source-held key must gain exactly permitopen=127.0.0.1:26379",
                "source_listener_restriction": "allow only the inspected source Redis container IP; reject other bridge clients",
                "announced_address": ROLE_SWAP_ANNOUNCE_IP+":"+str(PORT),
                "before_final_failover": "verify this exact restricted source bridge listener reaches the owned Tencent Redis; FAILOVER is forbidden until that proof exists",
                "prepared_here": False},
            "promotion_included": False, "source_configuration_changes": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--inspect", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-source-run-id")
    args = parser.parse_args(argv)
    if not args.inspect and not args.apply:
        print(json.dumps(dry_run_plan(), indent=2))
        return 0
    if args.apply:
        require(bool(args.expected_source_run_id and re.fullmatch(r"[0-9a-f]{40}", args.expected_source_run_id)), "apply-requires-reviewed-source-run-id")
    source, target = remote(SOURCE, source_inspection_script()), remote(TARGET, target_inspection_script())
    inspection = validate_inspection(source, target)
    if args.inspect:
        print(json.dumps(inspection, indent=2))
        return 0
    require(args.expected_source_run_id == source["server"]["run_id"], "reviewed-source-run-id-changed")
    result = remote(TARGET, apply_script(inspection, secrets.token_hex(16)), timeout=115)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Refusal, subprocess.TimeoutExpired) as error:
        print(json.dumps({"refused": True, "reason": str(error) if isinstance(error, Refusal) else "remote-operation-timeout-resources-may-be-retained"}), file=sys.stderr)
        raise SystemExit(1)
    except (KeyError, TypeError, ValueError, OSError):
        print(json.dumps({"refused": True, "reason": "inspection-or-host-state-incomplete"}), file=sys.stderr)
        raise SystemExit(1)
