#!/usr/bin/env python3
"""Archive and fence only the four obsolete AWS Compose entry files.

Existing containers, Docker volumes, AWS Caddy and Codex Relay are untouched.
Default is offline. Root retains byte-exact originals for deliberate recovery.
"""
import argparse
import json
import pathlib
import runpy
import shlex
import subprocess

NAMES = ("docker-compose.yml", "docker-compose.dev.yml", "docker-compose.local.yml", "docker-compose.standalone.yml")
FENCE = b"# AWS Sub2API deployment retired by migration 20261016.\n# Originals: /var/lib/sub2api-migration-20261016/deployment-archive\nsub2api_migration_fenced: true\n"


def script():
    return f'''
import hashlib,json,os,pathlib,stat,subprocess
root=pathlib.Path('/var/lib/sub2api-migration-20261016/deployment-archive')
deploy=pathlib.Path('/home/ubuntu/sub2api/deploy')
names={NAMES!r};fence={FENCE!r}
def need(ok,code):
 if not ok:raise SystemExit(code)
need(os.geteuid()==0,'root-required')
for parent in [root,*root.parents]:
 need(not parent.is_symlink(),'symlink-refused')
root.mkdir(mode=0o700,exist_ok=True)
need(root.stat().st_uid==0 and stat.S_IMODE(root.stat().st_mode)==0o700,'archive-not-private')
actual=sorted(p.name for p in deploy.glob('*compose*.y*ml'))
need(actual==sorted(names),'deployment-file-inventory-changed')
report={{}}
for name in names:
 path=deploy/name;archive=root/name
 info=path.lstat()
 need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<2**20,'manifest-not-regular')
 raw=path.read_bytes()
 if archive.exists() or archive.is_symlink():
  ai=archive.lstat()
  need(stat.S_ISREG(ai.st_mode) and ai.st_uid==0 and ai.st_nlink==1 and stat.S_IMODE(ai.st_mode)==0o600,'archive-not-trusted')
  original=archive.read_bytes()
  need(raw in (original,fence),'manifest-changed-after-archive')
 else:
  need(raw!=fence,'original-archive-missing')
  original=raw
  fd=os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
  fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
 if raw!=fence:
  temp=deploy/('.migration-fence-'+name)
  fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o444)
  with os.fdopen(fd,'wb') as out:out.write(fence);out.flush();os.fsync(out.fileno())
  os.replace(temp,path)
 need(path.read_bytes()==fence and path.stat().st_uid==0 and stat.S_IMODE(path.stat().st_mode)==0o444,'fence-readback-failed')
 probe=subprocess.run(['/usr/bin/docker','compose','-f',str(path),'config','--services'],capture_output=True,timeout=5)
 need(probe.returncode!=0 and not probe.stdout.strip(),'compose-fence-not-effective')
 report[name]={{'original_sha256':hashlib.sha256(original).hexdigest(),'fence_sha256':hashlib.sha256(fence).hexdigest(),'compose_rejected':True}}
fd=os.open(deploy,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
result={{'v':1,'run':'20261016','source_deployment_paths_fenced':True,'files':report,'containers_changed':False,'relay_changed':False}}
raw=json.dumps(result,sort_keys=True).encode()+b'\\n'
path=root/'verification.json'
if path.exists():need(path.read_bytes()==raw,'archive-evidence-changed')
else:
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
print(json.dumps(result,sort_keys=True))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"offline": True, "files": NAMES, "containers_changed": False}))
        return
    channel = runpy.run_path(str(pathlib.Path(__file__).with_name("prepare_channel.py")))
    result = subprocess.run(channel["SSH_SOURCE"] + ["sudo python3 -c " + shlex.quote(script())], capture_output=True, timeout=40)
    if result.returncode:
        raise RuntimeError("deployment-fence-incomplete-inspect-fixed-archive-before-retry")
    evidence = json.loads(result.stdout)
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
