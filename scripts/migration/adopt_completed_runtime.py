#!/usr/bin/env python3
"""CAS repair of the unstarted keeper's route compatibility, then adopt live PIDs.

Original programs, arm and source proof are retained. No source authority or
business container is started, stopped, signalled or recreated.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import subprocess

KEEPER_SHA='05c5f2e681713d8a890c022577406ce92993a0d86ca91ffb15898956fe278352'
ROUTE_SHA='e958f4a975db00b7b5a55facaa43f99e9511949aa29727a7d6523682c798c8f1'
OLD_KEEPER='7baf987354fcca8a6117b61f292d1dc8dceef07c15999a0ea5cd923ac4dd59bf'
OLD_ROUTE='c3d2b62bf07472f071a1c586c600a133b79a6b36464aa6e1edf77a306b0dc284'

SOURCE=r'''
import importlib.util,pathlib,sys,json,time
sys.dont_write_bytecode=True
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016')
s=importlib.util.spec_from_file_location('runner_service',lib/'runner_service.py');M=importlib.util.module_from_spec(s);sys.modules['runner_service']=M;s.loader.exec_module(M)
old=json.load(sys.stdin)
with M.execution_lock():
 b=M.B.RunnerBackend();state=b.store.read('state');budget=M.E.budget_accounting(state)
 M.E.need(M.B.sha(state)=='28dc19f71e085a8e65de2d6e65b705f18afcf7b70678a3ec9082a65506543b25' and state==old['state'] and budget==old['budget'] and state['phase']=='COMPLETE_WITH_WAIT_BREACH' and state['deadline_breached'] is True and state['window'] is None,'COMPLETED_SOURCE_STATE_CHANGED')
 guard=b.source_ops._guard();M.E.need(guard['healthy'] is True and guard['tripped'] is False and guard['recovering'] is False,'GUARD_NOT_STABLE')
 observed=b.source(None,'inspect');original=old['source_inspect']
 M.E.need(observed['resources']==original['resources'] and observed['operations']==original['operations'] and observed['app_connections']==original['app_connections'],'RETIRED_SOURCE_CHANGED')
 runtime=b.control(None,'runtime.status');background=b.control(None,'background.status')
 M.E.need(runtime['ready'] is True and runtime['background_workers_enabled'] is True and background['activated'] is True and not background['activating'] and not background['pending'] and not background.get('failures'),'BACKGROUND_NOT_READY')
 proof={**old,'verified_at':time.time(),'source_inspect':observed}
 print(json.dumps(proof,sort_keys=True))
'''

TARGET=r'''
import base64,hashlib,importlib.util,json,os,pathlib,subprocess,sys,time
sys.dont_write_bytecode=True
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016');root=pathlib.Path('/var/lib/sub2api-migration-20261016');scope=root/'keeper'
p=json.load(sys.stdin)
def load(name,path,raw=None):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);sys.modules[name]=m
 if raw is None:s.loader.exec_module(m)
 else:exec(compile(raw,str(path),'exec'),m.__dict__)
 return m
R=load('adoption_rpc',lib/'target_rpc.py');R.require(R.sha(R.private_read(lib/'target_rpc.py',mode=0o700))=='85d980acff46125de0db0a2734d33b632f1cfb919577be871f8c56faff2db75a','RPC_CHANGED')
cfg=R.load_config();proof=p['proof']
R.require(0<=time.time()-proof['verified_at']<=30 and proof['owner']==cfg['owner'] and proof['state']['phase']=='COMPLETE_WITH_WAIT_BREACH' and proof['state']['deadline_breached'] is True,'FRESH_FAILED_WAIT_COMPLETION_REQUIRED')
old_program=R.private_read(lib/'target_keeper.py',mode=0o700);old_route=R.private_read(lib/'rolling_app_route.py',mode=0o700);old_arm=R.private_read(scope/'arm.json');old_proof=R.private_read(scope/'source-complete-proof.json')
R.require(R.sha(old_program)==p['old_keeper'] and R.sha(old_route)==p['old_route'] and R.sha(old_arm)=='60aa8dd8f71b12faa780fbc32e58015d21b729a46588a1b6b4374cadaafb2700' and R.sha(old_proof)=='3c7e388a1e242a2336d0ddfe985b320a89648dc9d24c1f2f42f921d504e02986','PARTIAL_ADOPTION_CHANGED')
original=R.strict_json(old_proof,65536)
R.require(proof['state']==original['state'] and proof['budget']==original['budget'] and proof['verification']==original['verification'] and proof['source_tombstones']==original['source_tombstones'] and proof['source_inspect']['resources']==original['source_inspect']['resources'],'ORIGINAL_FAILURE_HISTORY_CHANGED')
R.require(not (scope/'state.json').exists() and not (scope/'state.json').is_symlink() and not pathlib.Path('/etc/systemd/system/sub2api-tencent-keeper-20261016.service').exists(),'KEEPER_MAY_HAVE_RUN_INSPECT_ONLY')
R.require(not list((root/'rolling-recovery/route-keeper').glob('intent-*.json')),'EXISTING_ROUTE_INTENT_NEEDS_SEPARATE_UPGRADE')
program=base64.b64decode(p['keeper'],validate=True);route=base64.b64decode(p['route'],validate=True)
R.require(R.sha(program)==p['keeper_sha'] and R.sha(route)==p['route_sha'],'REVIEWED_PROGRAMS_CHANGED')
K=load('completed_keeper',lib/'target_keeper.py',program)
R.require(K.ROUTE_SHA==R.sha(route),'ROUTE_PIN_MISMATCH')
arm=R.strict_json(old_arm,65536);arm['program_sha256']=R.sha(program);arm['armed_at']=time.time()
arm['complete'].update(verified_at=proof['verified_at'],source_inspect_sha256=R.sha(proof['source_inspect']),source_tombstones_sha256=R.sha(proof['source_tombstones']))
K.validate_arm(arm,cfg,R,R.sha(program))
class AdoptOnly(K.backend_class(R)):
 def start(self,kind):raise K.Refusal('ADOPTION_MUST_NOT_START_CONTAINER')
 def start_entry(self):raise K.Refusal('ADOPTION_MUST_NOT_START_ENTRY')
 def ensure_route(self):
  route=load('adoption_route',lib/'rolling_app_route.py');result=route.load_route().inspect()
  K.need(result['ok'] is True and result['status']=='present','EXISTING_ROUTE_REQUIRED')
  return result
b=AdoptOnly(cfg);before={k:b.inspect(k)['State']['Pid'] for k in ('pg','redis','app')};entry=b.entry()['MainPID']
R.require(before=={'pg':509035,'redis':133623,'app':534792} and int(entry)>0,'LIVE_ORIGINAL_PIDS_REQUIRED')
b.persistent(arm);b.primary(arm);b.master(arm)
runtime=b.control('runtime.status');bg=b.control('background.status')
R.require(runtime['ready'] is True and runtime['background_workers_enabled'] is True and bg['activated'] is True and not bg['activating'] and not bg['pending'] and not bg.get('failures'),'BACKGROUND_NOT_READY')
saved=scope/'route-compatibility-20261017';R.require(not saved.exists() and not saved.is_symlink(),'UPGRADE_ALREADY_ATTEMPTED_INSPECT_ONLY');saved.mkdir(mode=0o700);R.fsync_directory(scope)
def write(path,raw,mode=0o600):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
 R.fsync_directory(path.parent)
for name,raw in [('previous-keeper.py',old_program),('previous-route.py',old_route),('previous-arm.json',old_arm),('original-source-proof.json',old_proof),('fresh-source-proof.json',R.canonical(proof))]:write(saved/name,raw)
write(saved/'intent.json',R.canonical({'old_keeper':p['old_keeper'],'old_route':p['old_route'],'new_keeper':p['keeper_sha'],'new_route':p['route_sha'],'business_pids':before,'no_repeat':True}))
for path,raw,mode in [(lib/'rolling_app_route.py',route,0o700),(lib/'target_keeper.py',program,0o700),(scope/'arm.json',R.canonical(arm)+b'\n',0o600)]:
 temporary=path.with_name(path.name+'.route-compatibility.pending');write(temporary,raw,mode)
 expected=old_route if path.name=='rolling_app_route.py' else old_program if path.name=='target_keeper.py' else old_arm
 R.require(R.private_read(path,mode=mode)==expected,'UPGRADE_CAS_CHANGED');os.replace(temporary,path);R.fsync_directory(path.parent)
keeper=K.Keeper(R,cfg,b,K.Store(R,cfg),R.sha(program));result=keeper.step()
R.require(result=={'ready':True,'adopted':True} and not keeper.state['starts'],'ADOPT_RUNNING_ONLY')
unit=pathlib.Path('/etc/systemd/system/sub2api-tencent-keeper-20261016.service');write(unit,K.unit().encode(),0o644)
subprocess.run(['systemctl','daemon-reload'],capture_output=True,check=True,timeout=5)
subprocess.run(['systemctl','enable','--now',unit.name],capture_output=True,check=True,timeout=8)
time.sleep(1)
R.require(subprocess.run(['systemctl','is-active','--quiet',unit.name]).returncode==0 and subprocess.run(['systemctl','is-enabled','--quiet',unit.name]).returncode==0,'KEEPER_NOT_ACTIVE_ENABLED')
R.require(subprocess.run(['systemctl','is-enabled','--quiet',K.ENTRY_UNIT]).returncode!=0,'ENTRY_MUST_BE_KEEPER_ORDERED')
R.require(before=={k:b.inspect(k)['State']['Pid'] for k in before} and b.entry()['MainPID']==entry,'BUSINESS_PIDS_CHANGED')
result={'keeper_active':True,'keeper_enabled':True,'adopted_running_originals':True,'business_pids_unchanged':True,'pids':before,'wait_budget_passed':False,'budget':proof['budget'],'source_redis_container_exit_code':127,'prior_source_redis_operation_phase':'pending','cold_boot_tested':False,'new_keeper_sha256':R.sha(program),'new_route_sha256':R.sha(route)}
write(saved/'verification.json',R.canonical(result));print(json.dumps(result,sort_keys=True))
'''


def write(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true')
    if not parser.parse_args(argv).apply:
        print(json.dumps({'offline':True,'adopt_running_pids_only':True,'wait_budget_passed':False}));return 0
    folder=pathlib.Path(__file__).resolve().parent;keeper=(folder/'target_keeper.py').read_bytes();route=(folder/'rolling_app_route.py').read_bytes()
    if hashlib.sha256(keeper).hexdigest()!=KEEPER_SHA or hashlib.sha256(route).hexdigest()!=ROUTE_SHA:raise RuntimeError('reviewed-program-changed')
    backup=pathlib.Path.home()/'.codex/backups/sub2api-source-controller-20261016'
    write(backup/'completed-runtime-adoption-20261017.intent.json',json.dumps({'no_repeat':True,'keeper_sha':KEEPER_SHA,'route_sha':ROUTE_SHA}).encode())
    original=json.loads(base64.b64decode(json.loads((backup/'reconciled-runtime-finalization-20261017.source.command.json').read_bytes())['stdout_b64']))
    channel=runpy.run_path(str(folder/'prepare_channel.py'));proof=None
    for side,script in [('source',SOURCE),('target',TARGET)]:
        ssh=channel['SSH_SOURCE' if side=='source' else 'TARGET'];payload=original if side=='source' else {'proof':proof,'keeper':base64.b64encode(keeper).decode(),'route':base64.b64encode(route).decode(),'keeper_sha':KEEPER_SHA,'route_sha':ROUTE_SHA,'old_keeper':OLD_KEEPER,'old_route':OLD_ROUTE}
        receipt=backup/('completed-runtime-adoption-20261017.'+side+'.command.json')
        try:r=subprocess.run(ssh+['sudo -n python3 -I -c '+shlex.quote(script)],input=json.dumps(payload).encode(),capture_output=True,timeout=55)
        except (OSError,subprocess.TimeoutExpired) as e:
            write(receipt,json.dumps({'outcome':'unknown-inspect-only','no_repeat':True,'error_type':type(e).__name__,
                'stdout_b64':base64.b64encode(getattr(e,'stdout',None) or b'').decode(),
                'stderr_b64':base64.b64encode(getattr(e,'stderr',None) or b'').decode()}).encode());raise RuntimeError('outcome-unknown-inspect-only') from None
        write(receipt,json.dumps({'returncode':r.returncode,'stdout_b64':base64.b64encode(r.stdout).decode(),'stderr_b64':base64.b64encode(r.stderr).decode(),'no_repeat':True}).encode())
        if r.returncode:raise RuntimeError('adoption-incomplete-inspect-private-receipt')
        value=json.loads(r.stdout)
        if side=='source':proof=value
        else:write(backup/'completed-runtime-adoption-20261017.result.json',json.dumps(value,sort_keys=True).encode());print(json.dumps(value,sort_keys=True))
    return 0


if __name__=='__main__':raise SystemExit(main())
