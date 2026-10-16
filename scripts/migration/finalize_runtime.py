#!/usr/bin/env python3
"""Transfer fresh root COMPLETE proof and adopt the fixed Tencent runtime."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import subprocess

KEEPER_SHA = "03f628c9847226a9dcbce79e7852aae564caa658213305fb21b39d807ee2727c"
SOURCE = r'''
import importlib.util,json,pathlib,sys,time
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016')
spec=importlib.util.spec_from_file_location('runner_service',lib/'runner_service.py');M=importlib.util.module_from_spec(spec);sys.modules['runner_service']=M;spec.loader.exec_module(M)
b=M.B.RunnerBackend();state=b.store.read('state')
budget=M.E.budget_accounting(state)
M.E.need(state['phase']=='COMPLETE' and state['pg']==state['redis']=='TENCENT' and state['window'] is None and not state.get('deadline_breached') and not state.get('budget_accounting_unknown') and budget['max_cohort_pause_seconds']<=10,'FINAL_COMPLETE_REQUIRED')
verified=M.Service(b,state).verify_complete();observed=b.source(None,'inspect');ops=b.source_ops._ledger()
for name,row in observed['resources'].items():
 M.E.need(row['id']==b.source_ops.m['containers'][name]['id'] and row['running'] is False and row['status']=='exited' and row['restart']=='no' and row['exit_code']==0 and row['oom_killed'] is False,'SOURCE_RETIREMENT_REQUIRED')
for action,signal in (('app_stop','sigterm'),('pg_stop','sigint'),('redis_role_persist','sigterm')):
 rows=[x for x in ops['operations'].values() if x['request']['action']==action]
 M.E.need(len(rows)==1 and rows[0]['phase']=='complete' and signal in rows[0]['steps'],'SOURCE_OWNED_RETIREMENT_REQUIRED')
fence=M.S.SCOPE.parent/'deployment-archive/verification.json';fence_raw=M.S.private_bytes(fence)
proof={'v':1,'run':'20261016','owner':b.config['owner'],'verified_at':time.time(),'state':state,'budget':budget,'verification':verified,'source_machine_id_sha256':b.config['machine_id_sha256'],'source_inspect':observed,'source_tombstones':{'operations':ops,'deployment_fence_raw_sha256':M.B.sha(fence_raw)}}
M.S.atomic_private(M.S.SCOPE/'runtime-complete-proof.json',M.S.canonical(proof)+b'\n')
print(json.dumps(proof,sort_keys=True))
'''
TARGET = r'''
import base64,hashlib,importlib.util,json,os,pathlib,stat,subprocess,sys,time
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016');prefix=pathlib.Path('/var/lib/sub2api-migration-20261016');scope=prefix/'keeper';program=lib/'target_keeper.py'
payload=json.load(sys.stdin);raw=base64.b64decode(payload['program'],validate=True);assert hashlib.sha256(raw).hexdigest()=='03f628c9847226a9dcbce79e7852aae564caa658213305fb21b39d807ee2727c'
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
R=load('target_rpc',lib/'target_rpc.py');cfg=R.load_config();proof=payload['proof'];now=time.time()
R.require(proof['v']==1 and proof['run']=='20261016' and proof['owner']==cfg['owner'] and 0<=now-proof['verified_at']<=30,'FRESH_COMPLETE_PROOF_REQUIRED')
R.trusted_directory(lib);R.trusted_directory(prefix)
R.require(R.sha(R.private_read(lib/'rolling_app_route.py',mode=0o700,limit=2*1024*1024))=='c3d2b62bf07472f071a1c586c600a133b79a6b36464aa6e1edf77a306b0dc284','REVIEWED_ROUTE_PROGRAM_REQUIRED')
R.trusted_directory(prefix/'rolling-recovery/route-keeper',private=True)
R.require(not scope.exists() and not scope.is_symlink(),'KEEPER_ALREADY_PRESENT_INSPECT_ONLY')
R.require(not program.exists() and not program.is_symlink(),'KEEPER_PROGRAM_ALREADY_PRESENT')
scope.mkdir(mode=0o700);R.fsync_directory(prefix)
def write_new(path,value,mode):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as out:out.write(value);out.flush();os.fsync(out.fileno())
 R.fsync_directory(path.parent)
write_new(program,raw,0o700);K=load('target_keeper',program)
class AdoptBackend(K.backend_class(R)):
 def start(self,kind):raise K.Refusal('ADOPTION_MUST_NOT_START_CONTAINERS')
 def start_entry(self):raise K.Refusal('ADOPTION_MUST_NOT_START_ENTRY')
b=AdoptBackend(cfg)
ledger=R.FileStore(cfg).read();endpoints=b.endpoint_state()
R.require(endpoints=={'v':1,'postgres':'target','redis':'target'} and ledger['pg']['authority']==ledger['redis']['authority']=='TENCENT','TARGET_AUTHORITIES_REQUIRED')
before={k:b.inspect(k)['State']['Pid'] for k in ('pg','redis','app')};entry_before=b.entry()['MainPID']
R.require(all(pid>0 for pid in before.values()),'ADOPT_RUNNING_ORIGINALS_ONLY')
operations=[(key,value) for key,value in ledger['operations'].items() if value['request']['action']=='redis.persist-primary' and value.get('phase')=='COMPLETE' and value.get('response',{}).get('ok') is True and value.get('response',{}).get('result',{}).get('persisted') is True]
R.require(len(operations)==1 and ledger['redis'].get('op_id')==operations[0][0],'REDIS_PRIMARY_PERSISTENCE_REQUIRED')
redis_raw,_=b.redis_config();source=proof['source_inspect'];state=proof['state']
arm={'v':1,'run':'20261016','owner':cfg['owner'],'machine_id_sha256':cfg['machine_id_sha256'],'rpc_config_sha256':R.sha(cfg),'program_sha256':R.sha(raw),'armed_at':time.time(),
 'complete':{'phase':state['phase'],'pg':state['pg'],'redis':state['redis'],'window':state['window'],'verified_at':proof['verified_at'],'state_sha256':R.sha(state),'verification_sha256':R.sha(proof['verification']),'source_machine_id_sha256':proof['source_machine_id_sha256'],'source_inspect_sha256':R.sha(source),'source_tombstones_sha256':R.sha(proof['source_tombstones']),'source_resources':source['resources']},
 'pg_intent_sha256':R.sha(ledger['pg']['promotion_intent']),'redis_persist_op_id':operations[0][0],'redis_normalized_config_sha256':K.normalized_redis(redis_raw,R.sha)}
K.validate_arm(arm,cfg,R,R.sha(raw));b.persistent(arm);b.primary(arm);b.master(arm)
runtime=b.control('runtime.status');background=b.control('background.status')
R.require(runtime.get('ready') is True and runtime.get('background_workers_enabled') is True and background.get('activated') is True and background.get('activating') is False and not background.get('pending') and not background.get('failures'),'BACKGROUND_MUST_ALREADY_BE_COMPLETE')
write_new(scope/'source-complete-proof.json',R.canonical(proof)+b'\n',0o600)
write_new(scope/'arm.json',R.canonical(arm)+b'\n',0o600)
keeper=K.Keeper(R,cfg,b,K.Store(R,cfg),R.sha(raw));adopt=keeper.step();R.require(adopt=={'ready':True,'adopted':True},'ADOPT_ONLY_REQUIRED')
R.require(not keeper.state['starts'],'NO_SERVICE_START_ALLOWED_DURING_ADOPTION')
unit=pathlib.Path('/etc/systemd/system/sub2api-tencent-keeper-20261016.service');write_new(unit,K.unit().encode(),0o644)
subprocess.run(['systemctl','daemon-reload'],check=True,capture_output=True,timeout=5)
subprocess.run(['systemctl','enable','--now',unit.name],check=True,capture_output=True,timeout=8)
time.sleep(1)
R.require(subprocess.run(['systemctl','is-active','--quiet',unit.name]).returncode==0,'KEEPER_NOT_ACTIVE')
R.require(subprocess.run(['systemctl','is-enabled','--quiet',unit.name]).returncode==0,'KEEPER_NOT_ENABLED')
R.require(subprocess.run(['systemctl','is-enabled','--quiet',K.ENTRY_UNIT]).returncode!=0,'ENTRY_MUST_REMAIN_KEEPER_ORDERED')
after={k:b.inspect(k)['State']['Pid'] for k in before};R.require(before==after and b.entry()['MainPID']==entry_before,'ADOPTION_CHANGED_BUSINESS_PID')
result={'keeper_active':True,'keeper_enabled':True,'adopted_running_originals':True,'business_pids_unchanged':True,'pids':after,'entry_pid':entry_before,'arm_sha256':R.sha(arm),'source_proof_sha256':R.sha(proof),'budget':proof['budget'],'cold_boot_tested':False}
write_new(scope/'adoption-verification.json',R.canonical(result)+b'\n',0o600)
print(json.dumps(result,sort_keys=True))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"offline": True, "requires_fresh_root_complete": True,
                          "container_starts_during_adoption": False, "keeper_sha256": KEEPER_SHA}))
        return
    root = pathlib.Path(__file__).parent
    raw = (root / "target_keeper.py").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == KEEPER_SHA, "unreviewed-keeper"
    channel = runpy.run_path(str(root / "prepare_channel.py"))
    folder = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
    result_path = folder / "runtime-adoption-20261017.json"
    assert not result_path.exists(), "existing-adoption-evidence-reconcile-do-not-repeat"
    source = subprocess.run(channel["SSH_SOURCE"] + ["sudo python3 -I -c " + shlex.quote(SOURCE)],
                            capture_output=True, timeout=30)
    assert source.returncode == 0, "complete-proof-unavailable-no-target-write"
    proof = json.loads(source.stdout)
    payload = json.dumps({"proof": proof, "program": base64.b64encode(raw).decode()}).encode()
    result = subprocess.run(channel["TARGET"] + ["sudo python3 -I -c " + shlex.quote(TARGET)],
                            input=payload, capture_output=True, timeout=40)
    evidence = result.stdout if result.returncode == 0 else json.dumps({"exit": result.returncode,
        "stderr": result.stderr.decode(errors="replace")[-4096:]}).encode()
    fd = os.open(result_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(evidence); out.flush(); os.fsync(out.fileno())
    assert result.returncode == 0, "adoption-incomplete-inspect-private-evidence-do-not-repeat"
    print(result.stdout.decode())


if __name__ == "__main__":
    main()
