#!/usr/bin/env python3
"""Import reviewed controller programs to AWS; main remains inactive."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import runpy
import shlex
import subprocess

OWNER = "30bda011defad8149621c2630a2bd89c"
PROGRAMS = ("install_source_runner.py", "durable_runner.py", "runner_backend.py", "runner_guard.py",
            "runner_service.py", "source_operations.py", "target_rpc.py")
STAGE = "/home/ubuntu/.local/share/sub2api-migration-20261016/runner-staging"
LIBEXEC = "/usr/local/libexec/sub2api-migration-20261016"


def execute(argv, data=None, timeout=90):
    result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        # Both remote bootstrap and installer use only fixed, nonsecret errors.
        # Store bounded private diagnostic output; never print an SSH/process dump.
        folder = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / "last-failure.json"
        raw = json.dumps({"exit_code": result.returncode, "stdout": result.stdout.decode(errors="replace")[:4096],
                          "stderr": result.stderr.decode(errors="replace")[:4096]}).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        raise RuntimeError("source-controller-install-incomplete-see-private-evidence")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"offline": True, "main_started": False, "route_or_data_changed": False}))
        return
    root = pathlib.Path(__file__).parent
    payload = {name: (root / name).read_bytes() for name in PROGRAMS}
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in payload.items()}
    approval = {"v": 1, "run": "20261016", "owner": OWNER, "initial_target_epoch": 0, "programs": hashes,
                "known_hosts_sha256": "aa6428edb5171b3bc24fe84bd0e0943ecd25be6887d3ee40da180594c2abe8b2",
                "source_replica_state_sha256": "281fd7778d33489fb33ee3140d56f843585e674331e73c3135d21117b1a78d9b"}
    channel = runpy.run_path(str(root / "prepare_channel.py"))
    stage_script = f'''
import base64,hashlib,json,os,pathlib,sys
p=pathlib.Path({STAGE!r})
for item in [p,*p.parents]:
 if item.is_symlink(): raise SystemExit('stage-symlink-refused')
p.mkdir(mode=0o700,exist_ok=True)
for name,value in json.load(sys.stdin).items():
 raw=base64.b64decode(value['raw'],validate=True)
 if hashlib.sha256(raw).hexdigest()!=value['sha256']:raise SystemExit('stage-byte-hash-failed')
 path=p/name
 if path.is_symlink():raise SystemExit('stage-file-symlink')
 if path.exists() and path.read_bytes()!=raw:raise SystemExit('existing-stage-differs-review-required')
 if not path.exists():
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
 path.chmod(0o600)
print(json.dumps({{'staged':len({PROGRAMS!r})}}))
'''
    packed = {name: {"raw": base64.b64encode(raw).decode(), "sha256": hashes[name]} for name, raw in payload.items()}
    execute(channel["SSH_SOURCE"] + ["python3 -c " + shlex.quote(stage_script)], json.dumps(packed).encode())
    bootstrap = f'''
import hashlib,os,pathlib,stat,sys
stage=pathlib.Path({STAGE!r});lib=pathlib.Path({LIBEXEC!r});expected={hashes!r}
if os.geteuid()!=0:raise SystemExit('root-required')
for part in reversed([lib,*lib.parents]):
 if not part.exists() and not part.is_symlink():part.mkdir(mode=0o700 if part==lib else 0o755)
 st=part.lstat()
 if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or st.st_mode&0o022:raise SystemExit('untrusted-root-program-directory')
for name,digest in expected.items():
 src=stage/name
 for parent in [src.parent,*src.parent.parents]:
  st=parent.lstat()
  if not stat.S_ISDIR(st.st_mode) or st.st_uid not in (0,1000) or st.st_mode&0o022:raise SystemExit('untrusted-stage-parent')
 fd=os.open(src,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,'rb') as inp:
  st=os.fstat(inp.fileno());raw=inp.read(2**20)
 if not stat.S_ISREG(st.st_mode) or st.st_uid!=1000 or st.st_nlink!=1 or stat.S_IMODE(st.st_mode)!=0o600 or hashlib.sha256(raw).hexdigest()!=digest:raise SystemExit('staged-program-hash-failed')
 dst=lib/name
 if dst.exists() or dst.is_symlink():
  st=dst.lstat()
  if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_nlink!=1 or stat.S_IMODE(st.st_mode)!=0o644 or dst.read_bytes()!=raw:raise SystemExit('installed-program-differs-no-overwrite')
 else:
  fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
  with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
fd=os.open(lib,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
os.execv('/usr/bin/python3',['/usr/bin/python3','-I',str(lib/'install_source_runner.py'),'install-inert'])
'''
    result = json.loads(execute(channel["SSH_SOURCE"] + ["sudo /usr/bin/python3 -I -c " + shlex.quote(bootstrap)],
                                json.dumps(approval).encode()))
    folder = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
    folder.mkdir(mode=0o700, exist_ok=True)
    evidence = {"approval": approval, "result": result}
    fd = os.open(folder / "install-result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(evidence, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
