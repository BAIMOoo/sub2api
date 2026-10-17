#!/usr/bin/env python3
"""Finish recovered Tencent runtime while preserving the failed wait objective.

This fixed continuation never clears deadline_breached, retries a handover or
reopens AWS authority. Runtime completion and the wait failure stay distinct.
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

BASE_SHA = 'f93ab49e33ddcdb7c6bd1e6e4c1a3f61bc02f857ddf9ebb6d16c7a6e6f9eb091'
OLD_KEEPER = '03f628c9847226a9dcbce79e7852aae564caa658213305fb21b39d807ee2727c'
KEEPER_SHA = '544a9a9b8625bf7e3ec00f85b0cdda2804d554d79bd63abd22c3dc49c288786e'
KEEPER_TEST_SHA = '2a0a76fa91aa804e945f0b7c9adc780be9b46c7d5e34bbd451889da1ea7768ac'

SOURCE = r'''
import importlib.util,json,os,pathlib,sys,time
sys.dont_write_bytecode=True
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016');root=pathlib.Path('/var/lib/sub2api-migration-20261016');scope=root/'runner'
spec=importlib.util.spec_from_file_location('runner_service',lib/'runner_service.py');M=importlib.util.module_from_spec(spec);sys.modules['runner_service']=M;spec.loader.exec_module(M)
def write(path,raw):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
 M.S.fsync_directory(path.parent)
with M.execution_lock():
 b=M.B.RunnerBackend();state=b.store.read('state');budget=M.E.budget_accounting(state)
 M.E.need(M.B.sha(M.S.private_bytes(scope/'state.json'))=='666f5173074ba8ab17be41fc5c8b70904dc1c3b72518ebad57d53f1ba3c4524b','RECOVERED_EXACT_STATE_REQUIRED')
 M.E.need(state['phase']=='DATA_TENCENT' and state['attempt']==7 and state['pg']==state['redis']=='TENCENT' and state['window'] is None and state['deadline_breached'] is True and not state.get('budget_accounting_unknown') and budget['max_cohort_pause_seconds']>10,'RECOVERED_FAILED_WAIT_REQUIRED')
 M.verify_entry();M.read_gates(b.config['owner'])
 spec=importlib.util.spec_from_file_location('recovered_finalization_guard',lib/'runner_guard.py');G=importlib.util.module_from_spec(spec);spec.loader.exec_module(G)
 M.E.need(G.LocalBackend().main_status()['quiescent'],'MAIN_STILL_RUNNING')
 guard=b.source_ops._guard();M.E.need(guard['healthy'] is True and guard['recovering'] is False and guard['tripped'] is False,'RECOVERY_STILL_RUNNING')
 observed=b.rpc(None,'status');pg=b.control(None,'postgres.status');redis=b.control(None,'redis.status');runtime=b.control(None,'runtime.status');ingress=b.control(None,'ingress.status')
 M.E.need(observed['pg_authority']==observed['redis_authority']=='TENCENT' and observed['pg']['in_recovery'] is False and observed['redis']['role']=='master' and pg['endpoint']=='target' and pg['paused'] is False and redis['address']=='127.0.0.1:26379' and redis['paused'] is False and redis['pubsub_cache_ready'] is True and runtime['ready'] is True and ingress['paused'] is False,'LIVE_TENCENT_SERVICE_REQUIRED')
 saved=scope/'recovered-runtime-finalization-20261017';M.E.need(not saved.exists() and not saved.is_symlink(),'FINALIZATION_ALREADY_ATTEMPTED_INSPECT_ONLY')
 saved.mkdir(mode=0o700);M.S.fsync_directory(scope)
 for name in ('state.json','transport-state.json','source-operations.json','source-attestations.json'):write(saved/name,M.S.private_bytes(scope/name))
 write(saved/'intent.json',M.B.canonical({'run':'20261016','owner':b.config['owner'],'budget':budget,'wait_budget_passed':False,'no_repeat':True}))
 engine=M.E.DataHandover(state,b);engine.retire_source_redis()
 source=b.source(None,'inspect');ops=b.source_ops._ledger()
 for name,row in source['resources'].items():
  M.E.need(row['id']==b.source_ops.m['containers'][name]['id'] and row['running'] is False and row['status']=='exited' and row['restart']=='no' and row['exit_code']==0 and row['oom_killed'] is False,'SOURCE_RETIREMENT_REQUIRED')
 for action,signal in (('app_stop','sigterm'),('pg_stop','sigint'),('redis_role_persist','sigterm')):
  rows=[x for x in ops['operations'].values() if x['request']['action']==action]
  M.E.need(len(rows)==1 and rows[0]['phase']=='complete' and signal in rows[0]['steps'],'SOURCE_OWNED_RETIREMENT_REQUIRED')
 activation=b.control('final:background.activate','background.activate')
 M.E.need(activation.get('activated') is True and activation.get('activating') is False and not activation.get('pending') and not activation.get('failures'),'BACKGROUND_ACTIVATION_INCOMPLETE')
 runtime=b.control(None,'runtime.status');background=b.control(None,'background.status');pg=b.control(None,'postgres.status');redis=b.control(None,'redis.status');ingress=b.control(None,'ingress.status');observed=b.rpc(None,'status')
 M.E.need(observed['pg_authority']==observed['redis_authority']=='TENCENT' and observed['pg']['in_recovery'] is False and observed['redis']['role']=='master' and M.Service.ready(runtime) and runtime['background_workers_enabled'] is True and background['activated'] is True and not background['activating'] and not background['pending'] and not background.get('failures') and pg['endpoint']=='target' and pg['paused'] is False and redis['address']=='127.0.0.1:26379' and redis['paused'] is False and redis['pubsub_cache_ready'] is True and ingress['paused'] is False,'FINAL_FRESH_RUNTIME_VERIFICATION_FAILED')
 M.E.need(all(row['failed']==row['dropped']==0 for row in runtime['tasks'].values()),'WRITER_FAILURE_OR_DROP')
 before_budget=M.E.budget_accounting(state)
 engine.save(phase='COMPLETE_WITH_WAIT_BREACH',runtime_completed_at=time.time())
 M.E.need(state['deadline_breached'] is True and M.E.budget_accounting(state)==before_budget,'FAILURE_HISTORY_CHANGED')
 verified={'phase':state['phase'],'runtime_complete':True,'pg':'TENCENT','redis':'TENCENT','wait_budget_passed':False,'budget':budget,'deadline_breached':True}
 fence=M.S.private_bytes(root/'deployment-archive/verification.json')
 proof={'v':1,'run':'20261016','owner':b.config['owner'],'verified_at':time.time(),'state':state,'budget':budget,'verification':verified,'source_machine_id_sha256':b.config['machine_id_sha256'],'source_inspect':source,'source_tombstones':{'operations':ops,'deployment_fence_raw_sha256':M.B.sha(fence)}}
 write(saved/'verification.json',M.B.canonical(proof));print(json.dumps(proof,sort_keys=True))
'''


def need(value, code):
    if not value:
        raise RuntimeError(code)


def derive_target(raw):
    need(hashlib.sha256(raw).hexdigest()==BASE_SHA,'reviewed-finalizer-base-changed')
    namespace={'__name__':'reviewed_finalizer_base','__file__':str(pathlib.Path(__file__).with_name('finalize_runtime.py'))}
    exec(compile(raw,namespace['__file__'],'exec'),namespace)
    text=namespace['TARGET']
    need(text.count(OLD_KEEPER)==1,'reviewed-keeper-pin-anchor-changed')
    text=text.replace(OLD_KEEPER,KEEPER_SHA)
    anchor="K.validate_arm(arm,cfg,R,R.sha(raw));b.persistent(arm);b.primary(arm);b.master(arm)"
    need(text.count(anchor)==1,'reviewed-keeper-proof-anchor-changed')
    text=text.replace(anchor,"R.require(state['phase']=='COMPLETE_WITH_WAIT_BREACH' and state['deadline_breached'] is True and proof['verification']['wait_budget_passed'] is False and proof['budget']['max_cohort_pause_seconds']>10,'FAILED_WAIT_EVIDENCE_REQUIRED')\narm['complete']['wait_budget_breach']={'deadline_breached':True,'max_cohort_pause_seconds':proof['budget']['max_cohort_pause_seconds'],'user_limit_seconds':10}\n"+anchor)
    compile(text,'recovered-runtime-target','exec')
    return text


def write_new(path, raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({'offline':True,'runtime_completion_only':True,'wait_budget_passed':False,'failure_history_preserved':True}));return 0
    folder=pathlib.Path(__file__).resolve().parent
    target=derive_target((folder/'finalize_runtime.py').read_bytes());program=(folder/'target_keeper.py').read_bytes()
    need(hashlib.sha256(program).hexdigest()==KEEPER_SHA and hashlib.sha256((folder/'target_keeper_test.py').read_bytes()).hexdigest()==KEEPER_TEST_SHA,'reviewed-keeper-or-tests-changed')
    backup=pathlib.Path.home()/'.codex/backups/sub2api-source-controller-20261016'
    marker=backup/'recovered-runtime-finalization-20261017.intent.json'
    write_new(marker,json.dumps({'source_sha256':hashlib.sha256(SOURCE.encode()).hexdigest(),'target_sha256':hashlib.sha256(target.encode()).hexdigest(),'keeper_sha256':KEEPER_SHA,'wait_budget_passed':False,'no_repeat':True}).encode())
    channel=runpy.run_path(str(folder/'prepare_channel.py'));proof=None
    for name,ssh,script in [('source',channel['SSH_SOURCE'],SOURCE),('target',channel['TARGET'],target)]:
        receipt=backup/('recovered-runtime-finalization-20261017.'+name+'.command.json')
        payload=None if proof is None else json.dumps({'proof':proof,'program':base64.b64encode(program).decode()}).encode()
        try:result=subprocess.run(ssh+['sudo -n python3 -I -c '+shlex.quote(script)],input=payload,capture_output=True,timeout=55)
        except (OSError,subprocess.TimeoutExpired) as error:
            write_new(receipt,json.dumps({'outcome':'unknown-inspect-only','no_repeat':True,'error_type':type(error).__name__,
                'stdout_b64':base64.b64encode(getattr(error,'stdout',None) or b'').decode(),'stderr_b64':base64.b64encode(getattr(error,'stderr',None) or b'').decode()}).encode())
            raise RuntimeError('finalization-outcome-uncertain-inspect-private-receipt') from None
        write_new(receipt,json.dumps({'returncode':result.returncode,'stdout_b64':base64.b64encode(result.stdout).decode(),'stderr_b64':base64.b64encode(result.stderr).decode(),'no_repeat':True}).encode())
        need(result.returncode==0,'finalization-incomplete-inspect-private-receipt')
        value=json.loads(result.stdout)
        if name=='source':proof=value
        else:
            value.update(wait_budget_passed=False,failure_history_preserved=True)
            write_new(backup/'recovered-runtime-finalization-20261017.result.json',json.dumps(value,sort_keys=True).encode());print(json.dumps(value,sort_keys=True))
    return 0


if __name__=='__main__':raise SystemExit(main())
