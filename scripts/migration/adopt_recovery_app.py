#!/usr/bin/env python3
"""Adopt the cleanly rolled application without replaying any controller work.

The default is offline. --apply requires the independently reviewed RPC digest
and attempts this fixed adoption once. The source execution lock remains held
through target adoption, source mirror installation and both read-only checks.
Every mutation has an exclusive durable intent. An uncertain result is retained
for inspection; neither this coordinator nor its remote programs retry effects.
No request pause, restore, quiesce, data routing, signal or service restart exists.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import runpy
import selectors
import shlex
import stat
import subprocess
import time

OWNER = "30bda011defad8149621c2630a2bd89c"
OLD_RPC_SHA = "4681f2e7203623b39d8f861811cb00cace10dd17f61bc447a3821196587acb9c"
SOURCE_STATE_SHA = "d11bc0b90d6118d09ec6a2a215acb9c9a1bfdd488e929b6edb85933a9cf21d0e"
TARGET_CONFIG_SHA = "3aed65e1a5c69093d4c3b72f616974fffab75b18dc31cffe0a42765e02dc0152"
TARGET_LEDGER_SHA = "abd784df64eaf0fb3d5d622f8025a221657078b731cf97d9859cee3ba8991ce3"
HOST_SHA = "a8194c260f88dd695fd02cd1b6f6f6dbe066ec9e7cbeee83cb72f12130e656ce"
CORE_SHA = "14b21455e754109b71e649e69a4adf3fd2e251e54f490ebf187a20c0442bc39f"
SOURCE_PROGRAMS = {
    "runner_guard.py": "72cb81b27bcf8c4f316511ca0460f44af453ab51276d53cda82ba55e2cee8d05",
    "runner_backend.py": "8d1f7e6064e51c2392e977603e47ba2684db5c36096bc3e944041b1d76218bbd",
    "runner_service.py": "7318557010904f289e05b8974f0ff6e23cffdcb3f372f82763824c9fc0eaa5a1",
    "source_operations.py": "28ff80ddfa89e71e4f91d217ddeb4d1ca90aee3028fe6523af0dd1ff2f15fbb6",
    "durable_runner.py": "18f0184ce061b4de57146a1f1024ab576622adc521dc16f0a9f0080fb76229d4",
}
LABEL = "adopt-recovery-app-20261017"
RETIRED_NAME = "sub2api-migration-retired-app-20261017"

VALIDATION = r'''
def same_lifecycle(before,after):
 fields=('Running','Pid','StartedAt','Status','ExitCode','OOMKilled','Paused','Restarting','Dead')
 return (type(before) is dict and type(after) is dict and
         all(key in before and key in after for key in fields) and
         canonical({key:before[key] for key in fields})==canonical({key:after[key] for key in fields}))
def boundary(state):
 need(state.get('v')==1 and state.get('run')=='20261016' and state.get('owner')==payload['owner'] and
      state.get('attempt')==3 and state.get('phase')=='SOURCE_DATA' and
      state.get('pg')==state.get('redis')=='AWS' and state.get('window') is None and
      state.get('deadline_breached') is False and state.get('route_fenced') is True and
      state.get('pause_seconds')==14.656846920028329 and
      type(state.get('quiet_instance_id')) is str and state['quiet_instance_id'],
      'exact-recovered-source-boundary-required')
def fresh_source(proof):
 need(proof.get('ok') is True and proof.get('owner')==payload['owner'] and
      type(proof.get('checked_at')) in (int,float) and 0<=time.time()-proof['checked_at']<=20 and
      proof.get('state_raw_sha256')==payload['source_state_sha'] and proof.get('main_quiescent') is True and
      proof.get('guard_healthy') is True and proof.get('no_fence') is True and proof.get('lock_held') is True,
      'fresh-locked-source-proof-required')
def healthy_controls(backend):
 controls={}
 for name in ('runtime','ingress','writers','postgres','redis','background'):
  reply=backend.app_control(name+'.status')
  need(reply['http_status']==200,'fresh-private-controls-required');controls[name]=reply['body']
 rt,p,r,bg,ing,w=(controls[k] for k in ('runtime','postgres','redis','background','ingress','writers'))
 need(rt.get('ready') is True and rt.get('serving') is True and rt.get('draining') is False and
      rt.get('background_workers_enabled') is False and ing.get('paused') is False and w.get('held') is False and
      p.get('endpoint')=='source' and p.get('paused') is False and
      r.get('address')=='127.0.0.1:16379' and r.get('paused') is False and r.get('pubsub_cache_ready') is True and
      bg.get('activated') is False and bg.get('activating') is False and not bg.get('started') and not bg.get('failures'),
      'healthy-source-connected-app-required')
 tasks=rt.get('tasks',{})
 need(backend_module.REQUIRED_WRITERS<=tasks.keys() and all(type(row) is dict and
      all(type(row.get(k,0)) is int and row.get(k,0)==0 for k in ('failed','dropped')) for row in tasks.values()),
      'fresh-writer-failure-or-coverage-gap')
 after=backend.app_control('runtime.status')
 need(after['http_status']==200 and after['body'].get('instance_id')==rt.get('instance_id') and
      after['body'].get('ready') is True and after['body'].get('serving') is True and
      after['body'].get('draining') is False and after['body'].get('background_workers_enabled') is False,
      'app-process-changed-during-controls')
 tasks=after['body'].get('tasks',{})
 need(backend_module.REQUIRED_WRITERS<=tasks.keys() and all(type(row) is dict and
      all(type(row.get(k,0)) is int and row.get(k,0)==0 for k in ('failed','dropped')) for row in tasks.values()),
      'fresh-writer-failure-or-coverage-gap')
 return after['body']
'''

SOURCE = r'''
scope=root/'runner';saved=scope/payload['label'];program=lib/'target_rpc.py'
for name,digest in payload['source_programs'].items():need(sha(trusted(lib/name,0o644))==digest,'source-program-changed')
need(sha(trusted(program,0o644))==payload['old_rpc_sha'],'source-rpc-mirror-changed')
M=load('adoption_source_service',lib/'runner_service.py')
G=load('adoption_source_guard',lib/'runner_guard.py');g=G.LocalBackend();b=M.B.RunnerBackend()
raw=base64.b64decode(payload['rpc_program'],validate=True)
need(sha(raw)==payload['new_rpc_sha'],'reviewed-rpc-digest-changed');compile(raw,str(program),'exec')
def emit(value):
 print(json.dumps(value,sort_keys=True),flush=True)
def receive(expected):
 line=sys.stdin.buffer.readline(65537)
 need(line and len(line)<=65536,'coordinator-lost-inspect-only')
 value=json.loads(line);need(type(value) is dict and value.get('command')==expected,'coordinator-command-invalid')
 return value
def source_check():
 state=b.store.read('state');boundary(state)
 need(sha(trusted(scope/'state.json',0o600))==payload['source_state_sha'],'source-checkpoint-changed')
 need(g.main_status()['quiescent'],'source-main-not-quiescent')
 for name in ('guard-trip.json','guard-fence.json'):
  need(not (scope/name).exists() and not (scope/name).is_symlink(),'new-source-trip-or-fence')
 guard=b.source_ops._guard()
 need(guard.get('healthy') is True and guard.get('tripped') is False and guard.get('recovering') is False and
      guard.get('pg_wal_protected') is True and guard.get('boot_id')==g.boot_id() and
      0<=time.time()-guard['checked_at']<=1 and guard.get('sample',{}).get('mode')=='RUNNING_BOUNDED',
      'source-guard-not-fresh-healthy')
 M.verify_entry();b.source_ops._assert_monitors_retired()
 need(not M.selftest_armed(payload['owner']),'source-selftest-armed')
 transport=b.store.read('transport');need(transport['target_epoch']==14,'source-transport-epoch-changed')
 for kind,pid in (('app',0),('postgres',972839),('redis',972827)):
  row=b.source_ops._resource(kind);s=row['State']
  need(s['Pid']==pid and s['Running']==(kind!='app') and s['OOMKilled'] is False,'source-resource-changed')
  if kind=='app':need(s['ExitCode']==0 and row['HostConfig']['RestartPolicy']['Name']=='no','source-app-not-retired')
 return state,guard
with M.execution_lock():
 state,guard=source_check()
 frozen={name:trusted(scope/name,0o600) for name in
  ('state.json','transport-state.json','source-operations.json','runner-config.json','guard-config.json','source-manifest.json')}
 need(not saved.exists() and not saved.is_symlink(),'source-adoption-already-attempted-inspect-only')
 saved.mkdir(mode=0o700);sync(scope)
 write_new(saved/'intent.json',canonical({'owner':payload['owner'],'new_rpc_sha256':payload['new_rpc_sha'],
  'at':time.time(),'source_files':{k:sha(v) for k,v in frozen.items()}}))
 for name,value in frozen.items():write_new(saved/name,value)
 write_new(saved/'old-target_rpc.py',trusted(program,0o644))
 proof={'ok':True,'owner':payload['owner'],'checked_at':time.time(),'state_raw_sha256':sha(frozen['state.json']),
  'quiet_instance_id':state['quiet_instance_id'],'main_quiescent':True,'guard_healthy':True,'no_fence':True,
  'lock_held':True,'source_files':{k:sha(v) for k,v in frozen.items()},'guard':guard}
 write_new(saved/'source-fresh-proof.json',canonical(proof));emit(proof)
 instruction=receive('install-source')
 target=instruction.get('target',{})
 need(target.get('ok') is True and target.get('new_rpc_sha256')==payload['new_rpc_sha'] and
      target.get('ledger_raw_sha256')==payload['target_ledger_sha'] and target.get('epoch')==14,
      'target-adoption-not-verified')
 source_check()
 need(all(trusted(scope/name,0o600)==value for name,value in frozen.items()),'source-ledger-CAS-changed')
 need(sha(trusted(program,0o644))==payload['old_rpc_sha'],'source-rpc-CAS-changed')
 write_new(saved/'install-mirror.intent.json',canonical({'at':time.time(),'new_rpc_sha256':payload['new_rpc_sha']}))
 atomic(program,raw,0o644)
 # The running holder imported the old mirror for preflight. Select the new
 # reviewed parser explicitly; only the fixed GET wire observer is called.
 M.B.rpc_module=load('adoption_source_new_rpc',program)
 observer=M.B.RunnerBackend()
 statuses={name:observer.observe_control(name+'.status') for name in
  ('runtime','ingress','writers','postgres','redis','background')}
 need(statuses['runtime'].get('instance_id')==target.get('new_instance_id') and
      statuses['runtime'].get('ready') is True and statuses['runtime'].get('serving') is True and
      statuses['ingress'].get('paused') is False and statuses['writers'].get('held') is False and
      statuses['postgres'].get('endpoint')=='source' and statuses['redis'].get('address')=='127.0.0.1:16379' and
      statuses['background'].get('activated') is False,'new-wire-observer-failed')
 source_check()
 need(all(trusted(scope/name,0o600)==value for name,value in frozen.items()),'source-ledgers-changed-by-observation')
 result={'ok':True,'owner':payload['owner'],'source_mirror_sha256':sha(trusted(program,0o644)),
  'state_raw_sha256':sha(frozen['state.json']),'source_ledgers_preserved':True,'epoch':14,
  'new_instance_id':target['new_instance_id'],'read_only_wire_verified':True,'lock_held':True,'checked_at':time.time()}
 write_new(saved/'source-verified.json',canonical(result));emit(result)
 receive('release')
 source_check()
 need(all(trusted(scope/name,0o600)==value for name,value in frozen.items()),'source-ledgers-changed-before-release')
 write_new(saved/'complete.json',canonical({'ok':True,'at':time.time(),'no_pause':True,'no_cohort_change':True}))
 emit({'ok':True,'released':True,'source_ledgers_preserved':True})
'''

TARGET_COMMON = r'''
scope=root/'rolling-recovery';saved=scope/payload['label'];cfgpath=root/'rpc/config.json';program=lib/'target_rpc.py'
need(sha(trusted(lib/'rolling_app_host.py',0o700))==payload['host_sha'] and
     sha(trusted(lib/'rolling_app_recovery.py',0o700))==payload['core_sha'],'rolling-programs-changed')
H=load('adoption_host',lib/'rolling_app_host.py');C=load('adoption_core',lib/'rolling_app_recovery.py')
def target_check(R,cfg,rolling_raw,oldcfg,new_instance):
 global backend_module
 backend_module=R;b=R.LocalBackend(cfg)
 ledgerraw=trusted(root/'rpc/state.json',0o600);ledger=R.strict_json(ledgerraw,8*1024*1024)
 need(sha(ledgerraw)==payload['target_ledger_sha'] and ledger['epoch']==14 and
      ledger['pg']['authority']==ledger['redis']['authority']=='AWS' and not ledger['pg'].get('promotion_intent') and
      (ledger['pg'].get('arm') is None or ledger['pg']['arm'].get('status')=='CANCELLED') and
      all(op.get('phase')=='COMPLETE' or (op['request']['action']=='app.quiesce' and op.get('cancelled') is True)
          for op in ledger['operations'].values()) and
      all(op.get('cancelled') is True for op in ledger['operations'].values() if op['request']['action']=='app.quiesce'),
      'target-original-ledger-or-authority-changed')
 need(trusted(scope/'state.json',0o600)==rolling_raw,'rolling-complete-changed')
 rolling=R.strict_json(rolling_raw,2*1024*1024)
 need(rolling['phase']=='COMPLETE' and rolling['intent'] is None and rolling['quiet']['samples']>=3 and
      [x['kind'] for x in rolling['completed_intents']]==['archive','prepare','start','route','term'],
      'rolling-drain-not-complete')
 old=b.docker_json('container',C.OLD_ID);new=b.resource('app')
 need(old['Name']=='/'+payload['retired_name'] and old['Id']==C.OLD_ID and
      R.sha(old['Config'])==oldcfg['app']['config_sha256'] and old['Image']==cfg['app']['image_id'] and
      old['State']['Status']=='exited' and old['State']['Running'] is False and old['State']['Pid']==0 and
      old['State']['ExitCode']==0 and old['State']['OOMKilled'] is False and
      old['HostConfig']['RestartPolicy']['Name']=='no','old-app-not-clean-retired')
 need(new['Name']=='/'+R.APP_CONTAINER and new['Id']==rolling['new_identity']['container_id'] and
      cfg['app']['image_id']==oldcfg['app']['image_id'] and cfg['app']['rootfs_diffids']==oldcfg['app']['rootfs_diffids'],
      'adopted-container-or-image-changed')
 expected=R.strict_json(trusted(scope/'new-process.json',0o600),4096)
 need(expected=={'pid':new['State']['Pid'],'start_ticks':H.process_start_ticks(pathlib.Path('/proc',str(new['State']['Pid']),'stat').read_text()),
      'started_at':new['State']['StartedAt']},'new-app-process-restarted')
 mounts=sorted([{'source':x['Source'],'destination':x['Destination'],'rw':x['RW']} for x in new['Mounts']],key=lambda x:x['destination'])
 need(mounts==C.MOUNTS,'adopted-mounts-changed')
 nat=C.copy.deepcopy(rolling['nat_before']);nat['output']=[C.REDIRECT]+nat['output']
 need(H.parse_nat(command(['/usr/sbin/iptables-save','-t','nat']),C.REDIRECT)==nat,'adopted-entry-rule-changed')
 need(b.endpoint_state()==C.ENDPOINTS,'target-endpoints-changed')
 with b.pg_locks():
  manifest=b.pg_manifest();pg=b.pg_sample()
 need(manifest.get('authority_mode')=='STANDBY' and not manifest.get('promotion_intent') and
      pg['in_recovery'] is True and pg['system_identifier']==cfg['pg']['system_identifier'] and
      pg['source_timeline']==cfg['pg']['source_timeline'],'target-PG-not-standby')
 redis=b.redis_inspect();need(redis['role']=='slave' and redis['run_id']==R.REDIS_TARGET_RUN_ID and
      redis['failover_state']=='no-failover','target-Redis-not-standby')
 runtime=healthy_controls(b);need(runtime['instance_id']==new_instance,'adopted-runtime-restarted')
 need(trusted(root/'rpc/state.json',0o600)==ledgerraw,'read-only-check-mutated-target-ledger')
 return b,ledger,runtime
'''

TARGET_ADOPT = TARGET_COMMON + r'''
fresh_source(payload['source'])
need(sha(trusted(program,0o700))==payload['old_rpc_sha'] and
     sha(trusted(cfgpath,0o600))==payload['target_config_sha'],'target-old-program-or-config-changed')
R=load('adoption_old_rpc',program);cfg=R.load_config();store=R.FileStore(cfg)
raw=base64.b64decode(payload['rpc_program'],validate=True)
need(sha(raw)==payload['new_rpc_sha'],'reviewed-target-program-changed');compile(raw,str(program),'exec')
with store.mutation_lock():
 core,h,hostcfg=H.load_host()
 rolling_raw=trusted(scope/'state.json',0o600);rolling=R.strict_json(rolling_raw,2*1024*1024)
 need(rolling.get('phase')=='COMPLETE' and rolling.get('intent') is None,'rolling-COMPLETE-required')
 rec=core.Recovery(h,None,payload['owner']);o=rec._observe(rolling);rec._new(rolling,o['new'],running=True)
 need(o['old']['state']=='exited' and o['old']['exit_code']==0 and o['old']['oom_killed'] is False,
      'rolling-old-clean-exit-required')
 history=R.strict_json(trusted(scope/'history/old-runtime.json',0o600),2*1024*1024)
 need(history['instance_id']==payload['source']['quiet_instance_id'],'historical-source-instance-changed')
 ledgerraw=trusted(root/'rpc/state.json',0o600);ledger=store.read()
 need(sha(ledgerraw)==payload['target_ledger_sha'] and ledger['epoch']==14,'target-ledger-CAS-changed')
 oldcfg=trusted(cfgpath,0o600);oldrpc=trusted(program,0o700);oldtoken=trusted(root/'rpc/app-control.token',0o600)
 token=trusted(C.TOKEN_FILE,0o600);need(R.sha(token.strip())==rolling['new_identity']['control_token_sha256'],
      'new-private-token-changed')
 names=command(['/usr/bin/docker','container','ls','-a','--format','{{.Names}}']).decode().splitlines()
 need(names.count(R.APP_CONTAINER)==1 and names.count(C.NEW_NAME)==1 and payload['retired_name'] not in names,
      'adoption-names-not-exclusive')
 need(not saved.exists() and not saved.is_symlink() and not (scope/'controller-adoption.json').exists() and
      not (scope/'controller-adoption.json').is_symlink() and not (scope/'controller-ledger-before.json').exists() and
      not (scope/'controller-ledger-before.json').is_symlink(),'target-adoption-already-attempted-inspect-only')
 saved.mkdir(mode=0o700);sync(scope)
 write_new(saved/'intent.json',canonical({'owner':payload['owner'],'at':time.time(),'old_config_raw_sha256':sha(oldcfg),
  'target_ledger_raw_sha256':sha(ledgerraw),'rolling_complete_raw_sha256':sha(rolling_raw),'new_rpc_sha256':payload['new_rpc_sha']}))
 for name,value in {'old-config.json':oldcfg,'old-app-control.token':oldtoken,'old-target_rpc.py':oldrpc,
    'target-ledger-before.json':ledgerraw,'rolling-complete-before.json':rolling_raw,
    'source-fresh-proof.json':canonical(payload['source']),
    'source-controller-closed.json':trusted(scope/'source-controller-closed.json',0o600)}.items():write_new(saved/name,value)
 write_new(scope/'controller-ledger-before.json',ledgerraw)
 updated=R.strict_json(oldcfg,65536);updated['app'].update(container_id=rolling['new_identity']['container_id'],
  config_sha256=rolling['new_identity']['config_sha256'],control_port=18093);updated['program_sha256']=payload['new_rpc_sha']
 newcfg=canonical(updated);R.validate_config(updated)
 receipt={'v':1,'run':'20261016','owner':payload['owner'],'old_instance_id':history['instance_id'],
  'new_instance_id':o['new']['instance_id'],'old_container_id':C.OLD_ID,'new_container_id':updated['app']['container_id'],
  'new_app_config_sha256':updated['app']['config_sha256'],'new_control_port':18093,
  'new_control_token_sha256':sha(token.strip()),'controller_config_raw_sha256':sha(newcfg),
  'ledger_raw_sha256':sha(ledgerraw),'ledger_epoch':14,'operation_floor':14,
  'source_closed_sha256':sha(trusted(scope/'source-controller-closed.json',0o600)),
  'rolling_complete_sha256':sha(rolling_raw),'old_clean_exit':{'container_id':C.OLD_ID,'exit_code':0,
  'oom_killed':False,'pid':0,'restart_policy':'no'}}
 write_new(saved/'new-config.json',newcfg);write_new(saved/'new-target_rpc.py',raw)
 write_new(saved/'new-app-control.token',token);write_new(saved/'adoption-receipt.json',canonical(receipt))
 fresh_source(payload['source'])
 need(trusted(cfgpath,0o600)==oldcfg and trusted(program,0o700)==oldrpc and
      trusted(root/'rpc/app-control.token',0o600)==oldtoken and
      trusted(root/'rpc/state.json',0o600)==ledgerraw and trusted(scope/'state.json',0o600)==rolling_raw,
      'adoption-pre-effect-CAS-changed')
 # Rename each immutable ID once, with a separate durable intent and readback.
 for label,identity,before,after in [('retire-old',C.OLD_ID,R.APP_CONTAINER,payload['retired_name']),
                                  ('adopt-new',updated['app']['container_id'],C.NEW_NAME,R.APP_CONTAINER)]:
  actual=h._docker('container',identity);need(actual['Name']=='/'+before,'rename-source-changed')
  write_new(saved/(label+'.intent.json'),canonical({'container_id':identity,'from':before,'to':after,'at':time.time()}))
  command(['/usr/bin/docker','rename',identity,after])
  changed=h._docker('container',identity)
  need(changed['Name']=='/'+after and changed['Id']==actual['Id'] and changed['Config']==actual['Config'] and
       changed['Image']==actual['Image'] and same_lifecycle(actual['State'],changed['State']),
       'rename-changed-container')
  write_new(saved/(label+'.verified.json'),canonical({'ok':True,'container_id':identity,'name':after}))
 write_new(scope/'controller-adoption.json',canonical(receipt))
 write_new(saved/'install-controller.intent.json',canonical({'at':time.time(),'new_config_raw_sha256':sha(newcfg)}))
 need(trusted(cfgpath,0o600)==oldcfg and trusted(program,0o700)==oldrpc and
      trusted(root/'rpc/app-control.token',0o600)==oldtoken,'controller-install-CAS-changed')
 atomic(root/'rpc/app-control.token',token);atomic(program,raw,0o700);atomic(cfgpath,newcfg)
 N=load('adoption_new_rpc',program);newconfig=N.load_config()
 backend,current,runtime=target_check(N,newconfig,rolling_raw,cfg,o['new']['instance_id'])
 # Validate the narrow receipt without RPC.handle, admission or any POST. The
 # synthetic entry exists only in memory, proving no production restore occurs.
 quiesces=[op for op in current['operations'].values() if op['request']['action']=='app.quiesce']
 latest=max(quiesces,key=lambda op:op['request']['expected_epoch'])
 request={'v':1,'run':'20261016','owner':payload['owner'],'op_id':__import__('secrets').token_hex(16),
          'expected_epoch':14,'action':'app.restore'}
 hypothetical=N.copy.deepcopy(current);hypothetical['operations'][request['op_id']]={'request':request}
 need(backend.app_restore_adoption(request,runtime,latest,hypothetical) is True,'narrow-adoption-proof-rejected')
 need(trusted(root/'rpc/state.json',0o600)==ledgerraw,'receipt-check-mutated-ledger')
 result={'ok':True,'new_rpc_sha256':sha(trusted(program,0o700)),'new_container_id':updated['app']['container_id'],
  'new_instance_id':runtime['instance_id'],'config_raw_sha256':sha(trusted(cfgpath,0o600)),
  'ledger_raw_sha256':sha(ledgerraw),'epoch':14,'old_clean_exit':True,'read_only_receipt_verified':True,
  'checked_at':time.time(),'owner':payload['owner']}
 write_new(saved/'target-verified.json',canonical(result));print(json.dumps(result,sort_keys=True))
'''

TARGET_VERIFY = TARGET_COMMON + r'''
need(sha(trusted(program,0o700))==payload['new_rpc_sha'],'installed-new-rpc-changed')
R=load('adoption_verify_rpc',program);cfg=R.load_config()
with R.FileStore(cfg).mutation_lock():
 rolling_raw=trusted(saved/'rolling-complete-before.json',0o600)
 oldcfg=R.strict_json(trusted(saved/'old-config.json',0o600),65536)
 need(trusted(cfgpath,0o600)==trusted(saved/'new-config.json',0o600) and
      trusted(root/'rpc/app-control.token',0o600)==trusted(saved/'new-app-control.token',0o600) and
      trusted(scope/'controller-adoption.json',0o600)==trusted(saved/'adoption-receipt.json',0o600),
      'installed-controller-artifact-changed')
 b,ledger,runtime=target_check(R,cfg,rolling_raw,oldcfg,payload['target']['new_instance_id'])
 result={'ok':True,'epoch':14,'ledger_raw_sha256':sha(trusted(root/'rpc/state.json',0o600)),
  'new_instance_id':runtime['instance_id'],'checked_at':time.time(),'source_and_target_verified':True}
 write_new(saved/'complete.json',canonical(result));print(json.dumps(result,sort_keys=True))
'''


def private_write(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def need(value, code):
    if not value:
        raise RuntimeError(code)


class SourceHolder:
    """A fixed SSH process retaining execution_lock until both hosts verify."""
    def __init__(self, argv, script, payload, directory):
        self.directory = directory
        self.errors = open(directory / "source-private-stderr", "xb", opener=lambda p, f: os.open(p, f, 0o600))
        self.process = subprocess.Popen(argv + ["sudo python3 -I -c " + shlex.quote(script)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors)
        try:
            self.send(payload)
        except Exception:
            self.close()
            raise

    def send(self, value):
        self.process.stdin.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, label, timeout=25):
        # Remote writes exactly one bounded JSON line per stage. Unbuffered OS
        # reads avoid a readline deadline being bypassed by a partial line.
        output = bytearray()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in output:
                left = deadline - time.monotonic()
                need(left > 0 and selector.select(left), "source-holder-outcome-unknown-inspect-only")
                chunk = os.read(self.process.stdout.fileno(), 65537 - len(output))
                need(chunk and len(output) + len(chunk) <= 65536, "source-holder-output-invalid-inspect-only")
                output.extend(chunk)
        need(output.count(b"\n") == 1 and output.endswith(b"\n"), "source-holder-protocol-invalid")
        value = json.loads(output)
        need(type(value) is dict and value.get("ok") is True, "source-holder-not-verified")
        private_write(self.directory / (label + ".json"), value)
        return value

    def close(self):
        # Closing stdin releases only this SSH holder. No service or business
        # process is signalled. Partial target effects remain inspect-only.
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self.errors.close()


def coordinate(holder, remote, payload, clock=time.time):
    """No effect is retried, including successful effects with a lost reply."""
    payload["source"] = holder.receive("source-prepared")
    need(0 <= clock() - payload["source"]["checked_at"] <= 20, "source-proof-aged-inspect-only")
    payload["target"] = remote("target-adopt", TARGET_ADOPT, payload)
    holder.send({"command": "install-source", "target": payload["target"]})
    source = holder.receive("source-verified")
    need(source.get("source_ledgers_preserved") is True and source.get("read_only_wire_verified") is True,
         "source-verification-incomplete")
    target = remote("target-verify", TARGET_VERIFY, payload)
    holder.send({"command": "release"})
    released = holder.receive("source-released")
    need(released.get("released") is True, "source-lock-release-unconfirmed")
    return {"ok": True, "new_instance_id": payload["target"]["new_instance_id"],
            "epoch": 14, "source_ledgers_preserved": True, "target_ledger_preserved": True,
            "target_verified": target.get("source_and_target_verified") is True,
            "request_pause": False, "cohort_changed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewed-rpc-sha256")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"offline": True, "request_pause": False, "unknown_results_replayed": False,
                          "source_execution_lock_held_across_hosts": True, "cohort_changed": False}))
        return 0
    need(type(args.reviewed_rpc_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", args.reviewed_rpc_sha256),
         "reviewed-rpc-digest-required")
    folder = pathlib.Path(__file__).parent
    raw = (folder / "target_rpc.py").read_bytes()
    need(hashlib.sha256(raw).hexdigest() == args.reviewed_rpc_sha256, "reviewed-rpc-digest-changed")
    compile(raw, "target_rpc.py", "exec")
    for filename, digest in {"rolling_app_host.py": HOST_SHA, "rolling_app_recovery.py": CORE_SHA}.items():
        need(hashlib.sha256((folder / filename).read_bytes()).hexdigest() == digest, "reviewed-rolling-program-changed")
    old = runpy.run_path(str(folder / "recover_source_guard.py"))
    channel = runpy.run_path(str(folder / "prepare_channel.py"))
    prior = runpy.run_path(str(folder / "prepare_retry.py"))
    directory = prior["FOLDER"] / LABEL
    # mkdir is exclusive; even a crash before recording an intent cannot make
    # a second invocation replay a remote operation.
    directory.mkdir(mode=0o700)
    need(stat.S_IMODE(directory.stat().st_mode) == 0o700, "private-local-archive-required")
    payload = {"owner": OWNER, "label": LABEL, "retired_name": RETIRED_NAME,
               "old_rpc_sha": OLD_RPC_SHA, "new_rpc_sha": args.reviewed_rpc_sha256,
               "rpc_program": base64.b64encode(raw).decode(), "source_state_sha": SOURCE_STATE_SHA,
               "target_config_sha": TARGET_CONFIG_SHA, "target_ledger_sha": TARGET_LEDGER_SHA,
               "host_sha": HOST_SHA, "core_sha": CORE_SHA, "source_programs": SOURCE_PROGRAMS}
    common = old["COMMON"]
    source_script = common.replace("payload=json.load(sys.stdin)", "payload=json.loads(sys.stdin.buffer.readline(2*1024*1024))") + VALIDATION + SOURCE
    compile(source_script, "adopt-source-holder", "exec")
    private_write(directory / "intent.json", {"new_rpc_sha256": args.reviewed_rpc_sha256,
                  "source_script_sha256": hashlib.sha256(source_script.encode()).hexdigest(),
                  "at": time.time(), "inspect_before_retry": True})

    def remote(label, code, data):
        script = common + VALIDATION + code
        compile(script, label, "exec")
        private_write(directory / (label + ".intent.json"), {"script_sha256": hashlib.sha256(script.encode()).hexdigest()})
        try:
            result = subprocess.run(channel["TARGET"] + ["sudo python3 -I -c " + shlex.quote(script)],
                                    input=json.dumps(data).encode(), capture_output=True, timeout=55)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("target-outcome-unknown-inspect-only") from None
        # Preserve all diagnostics privately; only a fixed summary reaches UI.
        private_write(directory / (label + ".private-result.json"), {"returncode": result.returncode,
                      "stdout_b64": base64.b64encode(result.stdout).decode(),
                      "stderr_b64": base64.b64encode(result.stderr).decode()})
        need(result.returncode == 0 and len(result.stdout) <= 65536, "target-outcome-unknown-inspect-only")
        value = json.loads(result.stdout)
        need(type(value) is dict and value.get("ok") is True, "target-verification-incomplete")
        private_write(directory / (label + ".json"), value)
        return value

    holder = None
    try:
        holder = SourceHolder(channel["SSH_SOURCE"], source_script, payload, directory)
        result = coordinate(holder, remote, payload)
        private_write(directory / "complete.json", result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        private_write(directory / "unknown.json", {"ok": False, "code": "ADOPTION_OUTCOME_UNKNOWN_INSPECT_ONLY"})
        print(json.dumps({"ok": False, "code": "ADOPTION_OUTCOME_UNKNOWN_INSPECT_ONLY"}))
        return 1
    finally:
        if holder is not None:
            holder.close()


if __name__ == "__main__":
    raise SystemExit(main())
