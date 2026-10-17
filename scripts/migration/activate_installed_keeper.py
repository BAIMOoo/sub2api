#!/usr/bin/env python3
"""Adopt already installed keeper after a read-only PG lock-busy refusal."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import subprocess

SOURCE=r'''
import importlib.util,pathlib,sys,json,os,subprocess,time
sys.dont_write_bytecode=True
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016');root=pathlib.Path('/var/lib/sub2api-migration-20261016');scope=root/'keeper';p=json.load(sys.stdin)
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);sys.modules[name]=m;s.loader.exec_module(m);return m
R=load('installed_adoption_rpc',lib/'target_rpc.py');cfg=R.load_config()
for filename,wanted in [('target_rpc.py',p['rpc_sha']),('target_keeper.py',p['keeper_sha']),('rolling_app_route.py',p['route_sha'])]:
 R.require(R.sha(R.private_read(lib/filename,mode=0o700))==wanted,'INSTALLED_PROGRAM_CHANGED')
K=load('installed_keeper',lib/'target_keeper.py');arm=K.Store(R,cfg).arm(p['keeper_sha'])
prior=scope/'route-compatibility-20261017';proof=R.strict_json(R.private_read(prior/'fresh-source-proof.json'),65536)
R.require(R.sha(R.private_read(scope/'arm.json'))=='103c57ae5eb985974d514861211980ff50aa8df52e66d9c3a51bff7f643a6614' and R.sha(R.private_read(prior/'fresh-source-proof.json'))=='e6bb98a1e2fe36930bc20a21a8409ee28e1fe53a12d389aa928d60ef1b13e746','EXACT_INSTALLED_ARM_AND_PROOF_REQUIRED')
R.require(arm['complete']['phase']=='COMPLETE_WITH_WAIT_BREACH' and arm['complete']['state_sha256']==R.sha(proof['state']) and arm['complete']['source_inspect_sha256']==R.sha(proof['source_inspect']) and arm['complete']['verification_sha256']==R.sha(proof['verification']) and arm['complete']['source_tombstones_sha256']==R.sha(proof['source_tombstones']),'INSTALLED_COMPLETION_PROOF_CHANGED')
R.require(arm['complete']['source_resources']==proof['source_inspect']['resources'] and arm['complete']['source_machine_id_sha256']==proof['source_machine_id_sha256'] and all(arm['complete'][key]==proof['state'][key] for key in ('phase','pg','redis','window')) and arm['complete']['source_redis_shell_exit']==proof['verification']['source_redis_shell_exit'],'COMPLETE_PROOF_FIELDS_CHANGED')
R.require(arm['complete']['wait_budget_breach']['max_cohort_pause_seconds']==proof['budget']['max_cohort_pause_seconds'] and proof['state']['deadline_breached'] is True and proof['verification']['source_redis_shell_exit']==K.SOURCE_REDIS_SHELL_EXIT,'FAILURE_RECORD_CHANGED')
R.require(not (scope/'state.json').exists() and not (scope/'state.json').is_symlink() and not pathlib.Path('/etc/systemd/system/sub2api-tencent-keeper-20261016.service').exists(),'KEEPER_ALREADY_STARTED_INSPECT_ONLY')
class AdoptOnly(K.backend_class(R)):
 def start(self,kind):raise K.Refusal('NO_CONTAINER_START_DURING_ADOPTION')
 def start_entry(self):raise K.Refusal('NO_ENTRY_START_DURING_ADOPTION')
 def control(self,name,epoch=0):
  K.need(name.endswith('.status'),'NO_CONTROL_EFFECT_DURING_ADOPTION');return super().control(name,epoch)
 def ensure_route(self):
  route=load('installed_adoption_route',lib/'rolling_app_route.py');result=route.load_route().inspect()
  K.need(result['ok'] is True and result['status']=='present','EXISTING_ROUTE_REQUIRED');return result
b=AdoptOnly(cfg);before={k:b.inspect(k)['State']['Pid'] for k in ('pg','redis','app')};entry=b.entry()['MainPID']
R.require(before=={'pg':509035,'redis':133623,'app':534792} and int(entry)>0,'LIVE_ORIGINAL_PIDS_REQUIRED')
saved=scope/'installed-adoption-20261017';R.require(not saved.exists() and not saved.is_symlink(),'ADOPTION_ALREADY_ATTEMPTED_INSPECT_ONLY');saved.mkdir(mode=0o700);R.fsync_directory(scope)
def write(path,raw,mode=0o600):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
 R.fsync_directory(path.parent)
write(saved/'intent.json',R.canonical({'arm_sha256':R.sha(arm),'programs':p,'pids':before,'no_repeat':True}))
keeper=K.Keeper(R,cfg,b,K.Store(R,cfg),p['keeper_sha'])
for attempt in range(30):
 try:result=keeper.step();break
 except R.Refusal as e:
  if e.code!='OPERATION_BUSY' or attempt==29:raise
  time.sleep(.15)
R.require(result=={'ready':True,'adopted':True} and not keeper.state['starts'],'ADOPT_RUNNING_ONLY')
unit=pathlib.Path('/etc/systemd/system/sub2api-tencent-keeper-20261016.service');write(unit,K.unit().encode(),0o644)
subprocess.run(['systemctl','daemon-reload'],capture_output=True,check=True,timeout=5)
subprocess.run(['systemctl','enable','--now',unit.name],capture_output=True,check=True,timeout=8)
time.sleep(1)
R.require(subprocess.run(['systemctl','is-active','--quiet',unit.name]).returncode==0 and subprocess.run(['systemctl','is-enabled','--quiet',unit.name]).returncode==0,'KEEPER_NOT_ACTIVE_ENABLED')
R.require(subprocess.run(['systemctl','is-enabled','--quiet',K.ENTRY_UNIT]).returncode!=0,'ENTRY_MUST_BE_KEEPER_ORDERED')
R.require(before=={k:b.inspect(k)['State']['Pid'] for k in before} and b.entry()['MainPID']==entry,'BUSINESS_PIDS_CHANGED')
result={'keeper_active':True,'keeper_enabled':True,'business_pids_unchanged':True,'pids':before,'wait_budget_passed':False,'budget':proof['budget'],'source_redis_container_exit_code':127,'prior_source_redis_operation_phase':'pending','cold_boot_tested':False,'read_only_busy_retries':attempt}
write(saved/'verification.json',R.canonical(result));print(json.dumps(result,sort_keys=True))
'''


def write(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true')
    if not parser.parse_args(argv).apply:print(json.dumps({'offline':True,'read_only_adoption_before_unit_enable':True}));return 0
    folder=pathlib.Path(__file__).resolve().parent;pins=runpy.run_path(str(folder/'adopt_completed_runtime.py'))
    p={'rpc_sha':'85d980acff46125de0db0a2734d33b632f1cfb919577be871f8c56faff2db75a','keeper_sha':pins['KEEPER_SHA'],'route_sha':pins['ROUTE_SHA']}
    for name,key in [('target_keeper.py','keeper_sha'),('rolling_app_route.py','route_sha')]:
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=p[key]:raise RuntimeError('reviewed-program-changed')
    backup=pathlib.Path.home()/'.codex/backups/sub2api-source-controller-20261016';write(backup/'installed-keeper-adoption-20261017.intent.json',json.dumps(p).encode())
    channel=runpy.run_path(str(folder/'prepare_channel.py'));path=backup/'installed-keeper-adoption-20261017.command.json'
    try:r=subprocess.run(channel['TARGET']+['sudo -n python3 -I -c '+shlex.quote(SOURCE)],input=json.dumps(p).encode(),capture_output=True,timeout=55)
    except (OSError,subprocess.TimeoutExpired) as e:
        write(path,json.dumps({'outcome':'unknown-inspect-only','no_repeat':True,'error_type':type(e).__name__,'stdout_b64':base64.b64encode(getattr(e,'stdout',None) or b'').decode(),'stderr_b64':base64.b64encode(getattr(e,'stderr',None) or b'').decode()}).encode());raise RuntimeError('inspect-only-unknown-outcome') from None
    write(path,json.dumps({'returncode':r.returncode,'stdout_b64':base64.b64encode(r.stdout).decode(),'stderr_b64':base64.b64encode(r.stderr).decode(),'no_repeat':True}).encode())
    if r.returncode:raise RuntimeError('incomplete-inspect-private-receipt')
    print(r.stdout.decode());return 0


if __name__=='__main__':raise SystemExit(main())
