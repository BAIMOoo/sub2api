#!/usr/bin/env python3
"""Replace the reviewed inert RPC program/config together before its first call."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import subprocess

EXPECTED = "0d5903493bf8e99393b3488f2ae1ac81771097032165d670183763a3ac305802"
REMOTE = r'''
import base64,hashlib,importlib.util,json,os,pathlib,subprocess,sys,time
root=pathlib.Path('/var/lib/sub2api-migration-20261016/rpc');prog=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016/target_rpc.py');cfgpath=root/'config.json'
oldsha='e8d6f8a5cd668c55cd14ef6d22a55c55f47105a15916904675ed69218d647ef9'
oldcfgsha='77b65ab52793e36ec2a9509d94afc4bc9c7bd127acb97539c4e761a881c62777'
newsha='0d5903493bf8e99393b3488f2ae1ac81771097032165d670183763a3ac305802'
sha=lambda v:hashlib.sha256(v).hexdigest()
payload=json.load(sys.stdin);new=base64.b64decode(payload['program'],validate=True);assert sha(new)==newsha
assert sha(prog.read_bytes())==oldsha and sha(cfgpath.read_bytes())==oldcfgsha
spec=importlib.util.spec_from_file_location('target_rpc',prog);M=importlib.util.module_from_spec(spec);sys.modules['target_rpc']=M;spec.loader.exec_module(M)
cfg=M.load_config();assert sha(M.canonical(cfg))=='841f7e06a9ae0d29b0a0f0ec49dbb6755628db64f31b15238a87bbf34616d6d0'
back=M.LocalBackend(cfg);before={k:back.resource(k)['State']['Pid'] for k in ('app','pg','redis')};ledger=M.FileStore(cfg).read()
assert ledger['epoch']==0 and ledger['operations']=={} and ledger['pg']['authority']==ledger['redis']['authority']=='AWS' and not ledger['pg'].get('arm') and not ledger['pg'].get('promotion_intent')
assert back.pg_sample()['in_recovery'] is True and back.redis_inspect()['role']=='slave'
archive=root/'rpc-upgrade-20261016';assert not archive.exists();archive.mkdir(mode=0o700)
for path in (prog,cfgpath):
 fd=os.open(archive/path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as out:out.write(path.read_bytes());out.flush();os.fsync(out.fileno())
fd=os.open(archive,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
timer='sub2api-migration-target-guard-20261016.timer';service='sub2api-migration-target-guard-20261016.service'
assert subprocess.run(['systemctl','is-active','--quiet',timer]).returncode==0
subprocess.run(['systemctl','stop',timer],check=True,capture_output=True,timeout=5)
try:
 for i in range(30):
  if subprocess.run(['systemctl','is-active','--quiet',service]).returncode!=0:break
  time.sleep(.1)
 else:raise AssertionError('guard-still-running')
 def replace(path,raw,mode):
  tmp=path.with_name('.rpc-upgrade-'+path.name)
  fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
  with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
  os.replace(tmp,path);fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
 replace(prog,new,0o700)
 cfg['program_sha256']=newsha;replace(cfgpath,M.canonical(cfg)+b'\n',0o600)
 spec=importlib.util.spec_from_file_location('target_rpc_new',prog);N=importlib.util.module_from_spec(spec);sys.modules['target_rpc_new']=N;spec.loader.exec_module(N)
 fresh=N.load_config();b=N.LocalBackend(fresh);after={k:b.resource(k)['State']['Pid'] for k in ('app','pg','redis')};assert after==before
 current=N.FileStore(fresh).read();assert current['epoch']==0 and current['operations']=={}
 assert b.pg_sample()['in_recovery'] is True and b.redis_inspect()['role']=='slave'
 result={'upgraded':True,'program_sha256':sha(prog.read_bytes()),'config_raw_sha256':sha(cfgpath.read_bytes()),'config_canonical_sha256':sha(N.canonical(fresh)),'epoch':0,'operations':0,'resource_pids_unchanged':True,'data_roles_unchanged':True}
 fd=os.open(archive/'verification.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as out:json.dump(result,out,sort_keys=True);out.flush();os.fsync(out.fileno())
finally:subprocess.run(['systemctl','start',timer],check=True,capture_output=True,timeout=5)
print(json.dumps(result,sort_keys=True))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"offline": True, "expected_program_sha256": EXPECTED}))
        return
    root = pathlib.Path(__file__).parent
    raw = (root / "target_rpc.py").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED, "unreviewed-program"
    channel = runpy.run_path(str(root / "prepare_channel.py"))
    result = subprocess.run(channel["TARGET"] + ["sudo python3 -I -c " + shlex.quote(REMOTE)],
                            input=json.dumps({"program": base64.b64encode(raw).decode()}).encode(),
                            capture_output=True, timeout=50)
    path = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016/target-rpc-upgrade-v2.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(result.stdout if result.returncode == 0 else json.dumps({"exit": result.returncode,
            "stdout": result.stdout.decode()[:4096], "stderr": result.stderr.decode()[:4096]}).encode())
        out.flush(); os.fsync(out.fileno())
    assert result.returncode == 0, "target-rpc-upgrade-incomplete-inspect-private-result"
    print(result.stdout.decode())


if __name__ == "__main__":
    main()
