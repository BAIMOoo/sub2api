#!/usr/bin/env python3
"""CAS-upgrade reviewed read batching and timer parsing before customer routing."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import subprocess

OLD = {
    "target_rpc.py": "0d5903493bf8e99393b3488f2ae1ac81771097032165d670183763a3ac305802",
    "runner_backend.py": "22df63bb5f7b9c123ab3acfac4a6c1843e48864ed30b1b14299bb8e083e0a84e",
    "source_operations.py": "29132c710bf701c84f2d5da396114322bb7ae884c43b4f462717a94a4ea729cf",
}
NEW = {
    "target_rpc.py": "dffaf4274a6d92f7d745a7ae5c1be268a7cb4e890820cbdbf4c80029073ec947",
    "runner_backend.py": "7904f16e12ef9893b910961c2d51d5156207a7463c8c82950ceb40f64b46638d",
    "source_operations.py": "8c96ae9234701022ef84914b1856fcc1ea9b76fcf0401a711a2dab83b2073818",
}
COMMON = r'''
import base64,hashlib,importlib.util,json,os,pathlib,stat,subprocess,sys,time
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016')
root=pathlib.Path('/var/lib/sub2api-migration-20261016')
payload=json.load(sys.stdin);old=payload['old'];new=payload['new']
sha=lambda raw:hashlib.sha256(raw).hexdigest()
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
def trusted(path,mode):
 s=path.lstat();assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==mode
def atomic(path,raw,mode):
 tmp=path.with_name('.read-upgrade-'+path.name)
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
 os.replace(tmp,path);fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
def archive(scope,paths):
 folder=scope/'read-upgrade-20261017';assert not folder.exists();folder.mkdir(mode=0o700)
 for path in paths:
  fd=os.open(folder/path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as out:out.write(path.read_bytes());out.flush();os.fsync(out.fileno())
 fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd);return folder
def bytes_for(name):
 raw=base64.b64decode(payload['programs'][name],validate=True);assert sha(raw)==new[name];return raw
'''
TARGET = r'''
prog=lib/'target_rpc.py';cfgpath=root/'rpc/config.json'
trusted(prog,0o700);trusted(cfgpath,0o600)
assert sha(prog.read_bytes())==old['target_rpc.py']
assert sha(cfgpath.read_bytes())=='98f37d9476e8317d06b85893593222d7a1808c2c8a7a481d372b463a6e056ce7'
raw=bytes_for('target_rpc.py');M=load('target_rpc',prog);cfg=M.load_config();b=M.LocalBackend(cfg)
ledger=M.FileStore(cfg).read();before={k:b.resource(k)['State']['Pid'] for k in ('app','pg','redis')}
assert ledger['epoch']==0 and ledger['pg']['authority']==ledger['redis']['authority']=='AWS' and not ledger['pg'].get('arm') and not ledger['pg'].get('promotion_intent')
assert b.pg_sample()['in_recovery'] is True and b.redis_inspect()['role']=='slave'
saved=archive(root/'rpc',[prog,cfgpath])
timer='sub2api-migration-target-guard-20261016.timer';service='sub2api-migration-target-guard-20261016.service'
assert subprocess.run(['systemctl','is-active','--quiet',timer]).returncode==0
subprocess.run(['systemctl','stop',timer],check=True,capture_output=True,timeout=5)
try:
 for _ in range(40):
  if subprocess.run(['systemctl','is-active','--quiet',service]).returncode!=0:break
  time.sleep(.1)
 else:raise AssertionError('guard-still-running')
 atomic(prog,raw,0o700);cfg['program_sha256']=new['target_rpc.py'];atomic(cfgpath,M.canonical(cfg)+b'\n',0o600)
 N=load('target_rpc_new',prog);fresh=N.load_config();after=N.LocalBackend(fresh)
 assert N.FileStore(fresh).read()==ledger
 assert {k:after.resource(k)['State']['Pid'] for k in before}==before
 assert after.pg_sample()['in_recovery'] is True and after.redis_inspect()['role']=='slave'
 result={'updated':'target','program_sha256':sha(prog.read_bytes()),'config_raw_sha256':sha(cfgpath.read_bytes()),'config_canonical_sha256':sha(N.canonical(fresh)),'pids_unchanged':True,'ledger_unchanged':True}
 atomic(saved/'verification.json',N.canonical(result)+b'\n',0o600)
finally:subprocess.run(['systemctl','start',timer],check=True,capture_output=True,timeout=5)
print(json.dumps(result,sort_keys=True))
'''
SOURCE = r'''
scope=root/'runner';statepath=scope/'state.json';state=json.loads(statepath.read_bytes())
assert state['phase']=='ENTRY_PREPARATION' and state['attempt']==0 and state['window'] is None and state['route_fenced'] is False and state['pg']==state['redis']=='AWS'
unit=subprocess.check_output(['systemctl','show','sub2api-migration-runner-20261016.service','--property=MainPID,ActiveState']).decode()
assert 'MainPID=0\n' in unit and 'ActiveState=inactive\n' in unit
guard=json.loads((scope/'guard-status.json').read_bytes());assert guard['healthy'] is True and guard['tripped'] is False
for name in new:
 trusted(lib/name,0o644);assert sha((lib/name).read_bytes())==old[name];bytes_for(name)
saved=archive(scope,[lib/name for name in new])
for name in new:atomic(lib/name,bytes_for(name),0o644)
assert json.loads(statepath.read_bytes())==state
result={'updated':'source','programs':{name:sha((lib/name).read_bytes()) for name in new},'main_inactive':True,'state_unchanged':True,'guard_not_restarted':True}
atomic(saved/'verification.json',json.dumps(result,sort_keys=True).encode()+b'\n',0o600)
print(json.dumps(result,sort_keys=True))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"offline": True, "new": NEW, "customer_route_changes": False}))
        return
    root = pathlib.Path(__file__).parent
    programs = {name: (root / name).read_bytes() for name in NEW}
    assert all(hashlib.sha256(raw).hexdigest() == NEW[name] for name, raw in programs.items())
    channel = runpy.run_path(str(root / "prepare_channel.py"))
    payload = json.dumps({"old": OLD, "new": NEW,
                          "programs": {name: base64.b64encode(raw).decode() for name, raw in programs.items()}}).encode()
    folder = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
    for label, remote, argv in (("target", TARGET, channel["TARGET"]), ("source", SOURCE, channel["SSH_SOURCE"])):
        path = folder / (label + "-read-upgrade-20261017.json")
        assert not path.exists(), "existing-upgrade-evidence-inspect-before-resuming"
        result = subprocess.run(argv + ["sudo python3 -I -c " + shlex.quote(COMMON + remote)],
                                input=payload, capture_output=True, timeout=40)
        evidence = result.stdout if result.returncode == 0 else json.dumps({"exit": result.returncode,
            "stdout": result.stdout.decode(errors="replace")[:4096], "stderr": result.stderr.decode(errors="replace")[:4096]}).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(evidence); out.flush(); os.fsync(out.fileno())
        assert result.returncode == 0, "upgrade-incomplete-inspect-private-evidence"
        print(result.stdout.decode())


if __name__ == "__main__":
    main()
