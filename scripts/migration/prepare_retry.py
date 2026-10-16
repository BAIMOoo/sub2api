#!/usr/bin/env python3
"""CAS-upgrade an aborted, recovered migration and prove a fresh request cohort.

The default is offline. This never starts the main runner or pauses requests.
Existing operation ledgers and the global pause history are preserved.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import secrets
import shlex
import subprocess

OLD = {
    "target_rpc.py": "2b4fa88abb3a35c3ba1349ed7370d624cb5c3ebdd2fcbc121fd9704c4a3e196c",
    "durable_runner.py": "ed820505a9120786f04c4584ffe63ffa0e87d2fda02229edec6cc620b06f78e4",
    "runner_service.py": "b4c46158b4374e8be2f8c444eadb35833e0a29d3f1a78558cb52df0c8d90e89a",
    "runner_guard.py": "704b171cc50feccf28f873320437886e8ea15fc7b76f9a8dbf36ed964dfd9345",
}
# Frozen only after independent review and targeted checks.
NEW = {
    "target_rpc.py": "60cea6e567c8d1990c4bad86205a4c351727499b5a20b48ba8dccd2a1a56c463",
    "durable_runner.py": "18f0184ce061b4de57146a1f1024ab576622adc521dc16f0a9f0080fb76229d4",
    "runner_service.py": "fb07110ea461a9872a90626ea435b7c22c18a0a04bca40523279aed55d3f0c63",
    "runner_guard.py": "e95be2c5e3bb01c842708c026c000b617aeca0ac26a049260a7d0010ddb8f200",
}
OWNER = "30bda011defad8149621c2630a2bd89c"
PRIOR_PAUSE = 7.082038551568985
TARGET_CONFIG_SHA = "9eb33e0f44ac2e6b8c9e743a2bd7593548967aa2eca2233548391a1581df2169"
DEPENDENCIES = {"runner_backend.py": "afb43d190c7e80ed2e9759c88ce5d0b6cefbbf7755eeb1b04f53a0f7de131dd1",
                "source_operations.py": "b6cfab63741c0f2dfb4d5c36445f9d14d62c845e3c2c6e1b0bd849eddbf3ec97"}
FOLDER = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"

VALIDATION = r'''
def need(value, code):
 if not value:raise RuntimeError(code)
def previous_state(state):
 need(state.get('v')==1 and state.get('run')=='20261016' and state.get('owner')==payload['owner'] and
      state.get('phase')=='SOURCE_DATA' and state.get('attempt')==1 and state.get('route_fenced') is True and
      state.get('pg')==state.get('redis')=='AWS' and state.get('window') is None and
      state.get('deadline_breached') is False and state.get('pause_seconds')==payload['prior_pause'] and
      'budget_cohort' not in state,'exact-recovered-attempt-required')
def drain_proof(proof, now):
 need(proof.get('v')==1 and proof.get('run')=='20261016' and proof.get('owner')==payload['owner'] and
      type(proof.get('checked_at')) in (int,float) and 0<=now-proof['checked_at']<=20,
      'fresh-target-drain-proof-required')
 rt=proof['runtime'];ing=rt.get('ingress',{})
 need(rt.get('instance_id')=='r23hcagq4x9cyq' and rt.get('ready') is True and rt.get('serving') is True and
      rt.get('draining') is False and rt.get('background_workers_enabled') is False and
      all(type(rt.get(k)) is int and rt[k]==0 for k in ('active_requests','active_hijacked_connections')) and
      ing.get('paused') is False and type(ing.get('queued_requests')) is int and ing['queued_requests']==0 and
      type(ing.get('epoch')) is int and ing['epoch']==proof['restore_ingress_epoch'] and
      proof['ingress_before']==proof['ingress_after']==ing,
      'previous-request-cohort-not-atomically-drained')
 need(proof.get('pg_authority')==proof.get('redis_authority')=='AWS' and
      proof.get('pg_in_recovery') is True and proof.get('redis_role')=='slave' and
      proof.get('arm_cancelled') is True and proof.get('promotion_intent') is None and
      proof.get('all_operations_complete') is True and proof.get('all_quiesces_cancelled') is True and
      proof.get('endpoints')=={'v':1,'postgres':'source','redis':'source'} and
      proof.get('app_pid')==257639 and proof.get('pg_pid')==180813 and proof.get('redis_pid')==133623,
      'target-not-at-reversible-abort-boundary')
'''

COMMON = r'''
import base64,hashlib,importlib.util,json,os,pathlib,stat,subprocess,sys,time
sys.dont_write_bytecode=True
payload=json.load(sys.stdin)
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016')
root=pathlib.Path('/var/lib/sub2api-migration-20261016');scope=root/'runner'
sha=lambda raw:hashlib.sha256(raw).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec)
 sys.modules[name]=m;spec.loader.exec_module(m);return m
def trusted(path,mode):
 for part in [path.parent,*path.parent.parents]:
  s=part.lstat();need(stat.S_ISDIR(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022,'untrusted-parent')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  s=os.fstat(fd);need(stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==mode and s.st_size<8388608,'untrusted-file')
  return os.read(fd,8388608)
 finally:os.close(fd)
def sync(path):
 fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
def write_new(path,raw,mode=0o600):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
 sync(path.parent)
def replace(path,raw,mode):
 temp=path.with_name('.retry-upgrade-'+path.name);write_new(temp,raw,mode);os.replace(temp,path);sync(path.parent)
def command(argv,timeout=3):
 r=subprocess.run(argv,capture_output=True,timeout=timeout,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
 need(r.returncode==0,'bounded-command-failed');return r.stdout
def archive(parent,names):
 saved=parent/'retry-upgrade-20261017'
 need(not saved.exists() and not saved.is_symlink(),'upgrade-already-attempted-inspect-only')
 saved.mkdir(mode=0o700);sync(parent)
 for path,mode in names:write_new(saved/path.name,trusted(path,mode),mode)
 return saved
def source_idle(M):
 b=M.B.RunnerBackend();state=b.store.read('state');previous_state(state)
 b.source_ops._assert_monitors_retired()
 need(not (scope/'guard-trip.json').exists() and not (scope/'guard-fence.json').exists(),'guard-fence-present')
 G=load('retry_guard',lib/'runner_guard.py');g=G.LocalBackend()
 need(g.main_status()['quiescent'],'main-cgroup-not-empty')
 guard=b.source_ops._guard()
 need(guard.get('recovering') is False and guard.get('tripped') is False,'guard-recovering-or-tripped')
 need(not M.selftest_armed(payload['owner']),'selftest-must-be-disarmed')
 M.verify_entry()
 for kind,pid in (('app',0),('postgres',972839),('redis',972827)):
  row=b.source_ops._resource(kind);s=row['State']
  expected_restart='no' if kind=='app' else 'unless-stopped'
  need(row['HostConfig']['RestartPolicy']['Name']==expected_restart and s['Pid']==pid and
       s['Running']==(kind!='app') and s.get('OOMKilled') is False,'source-resource-changed')
  if kind=='app':need(s['Status']=='exited' and s['ExitCode']==0,'source-app-not-cleanstopped')
 operations=b.source_ops._ledger()['operations'].values()
 need(not any(x['request']['action'] in ('pg_stop','redis_failover','redis_role_persist') for x in operations),'irreversible-operation-already-intended')
 app=[x for x in operations if x['request']['action']=='app_stop']
 need(len(app)==1 and app[0]['phase']=='complete' and 'sigterm' in app[0]['steps'],'owned-app-stop-required')
 return b,state
'''

TARGET_UPGRADE = r'''
path=lib/'target_rpc.py';cfgpath=root/'rpc/config.json'
need(sha(trusted(path,0o700))==payload['old']['target_rpc.py'],'target-program-changed')
need(sha(trusted(cfgpath,0o600))==payload['target_config_sha'],'target-config-changed')
M=load('retry_target_old',path);cfg=M.load_config();b=M.LocalBackend(cfg);store=M.FileStore(cfg)
raw=base64.b64decode(payload['programs']['target_rpc.py'],validate=True)
need(sha(raw)==payload['new']['target_rpc.py'],'target-new-digest');compile(raw,str(path),'exec')
with store.mutation_lock():
 ledger=store.read()
 need(ledger['pg']['authority']==ledger['redis']['authority']=='AWS' and not ledger['pg'].get('promotion_intent') and
      (not ledger['pg'].get('arm') or ledger['pg']['arm']['status']=='CANCELLED') and
      all(op['phase']=='COMPLETE' for op in ledger['operations'].values()),'target-not-recovered')
 need(b.endpoint_state()=={'v':1,'postgres':'source','redis':'source'},'target-endpoints-changed')
 rt=b.app_control('runtime.status')['body'];need(rt['ready'] is True and rt['ingress']['paused'] is False,'target-not-ready')
 before={k:b.resource(k)['State']['Pid'] for k in ('pg','redis','app')}
 need(before=={'pg':180813,'redis':133623,'app':257639},'target-pids-changed')
 saved=archive(root/'rpc',[(path,0o700),(cfgpath,0o600),(root/'rpc/state.json',0o600)])
 replace(path,raw,0o700)
 updated_cfg=dict(cfg);updated_cfg['program_sha256']=payload['new']['target_rpc.py']
 cfgraw=canonical(updated_cfg)+b'\n';replace(cfgpath,cfgraw,0o600)
 need(before=={k:b.resource(k)['State']['Pid'] for k in before},'target-pid-changed-during-upgrade')
 result={'ok':True,'program_sha256':sha(trusted(path,0o700)),'config_raw_sha256':sha(trusted(cfgpath,0o600)),'ledger_raw_sha256':sha(trusted(root/'rpc/state.json',0o600)),'epoch':ledger['epoch'],'business_pids_unchanged':True}
 write_new(saved/'verification.json',canonical(result)+b'\n')
 print(json.dumps(result,sort_keys=True))
'''

SOURCE_UPGRADE = r'''
for name,value in payload['dependencies'].items():need(sha(trusted(lib/name,0o644))==value,'source-dependency-changed')
for name in payload['old']:need(sha(trusted(lib/name,0o644))==payload['old'][name],'source-old-program-changed')
M=load('retry_service_old',lib/'runner_service.py')
with M.execution_lock():
 b,state=source_idle(M);rawstate=trusted(scope/'state.json',0o600)
 programs={name:base64.b64decode(raw,validate=True) for name,raw in payload['programs'].items()}
 for name,raw in programs.items():need(sha(raw)==payload['new'][name],'source-new-digest');compile(raw,name,'exec')
 saved=archive(scope,[(lib/name,0o644) for name in payload['old']]+[(scope/'state.json',0o600),(scope/'transport-state.json',0o600),(scope/'source-operations.json',0o600)])
 for name,raw in programs.items():replace(lib/name,raw,0o644)
 command(['systemctl','restart','sub2api-migration-recovery-20261016.service'],5)
 restarted=time.time()
 for _ in range(50):
  try:guard=json.loads(trusted(scope/'guard-status.json',0o600))
  except RuntimeError:time.sleep(.1);continue
  if guard['healthy'] is True and not guard['tripped'] and not guard['recovering'] and restarted<=guard['checked_at']<=time.time() and time.time()-guard['checked_at']<1:break
  time.sleep(.1)
 else:raise RuntimeError('new-guard-not-fresh-and-healthy')
 need(trusted(scope/'state.json',0o600)==rawstate,'source-state-changed-during-upgrade')
 result={'ok':True,'new_programs':payload['new'],'state_raw_sha256':sha(rawstate),'guard_restarted_healthy':True,'main_started':False}
 write_new(saved/'verification.json',canonical(result)+b'\n');print(json.dumps(result,sort_keys=True))
'''

SOURCE_RECONCILE_APP = r'''
for name,value in {**payload['old'],**payload['dependencies']}.items():
 need(sha(trusted(lib/name,0o644))==value,'source-old-program-changed')
M=load('retry_service_reconcile',lib/'runner_service.py')
with M.execution_lock():
 b=M.B.RunnerBackend();state=b.store.read('state');previous_state(state)
 rawstate=trusted(scope/'state.json',0o600)
 G=load('retry_reconcile_guard',lib/'runner_guard.py');need(G.LocalBackend().main_status()['quiescent'],'main-not-quiescent')
 guard=b.source_ops._guard();need(not guard['recovering'] and not guard['tripped'],'guard-not-stable')
 b.source_ops._assert_monitors_retired();M.verify_entry()
 before={}
 for kind,pid in (('app',0),('postgres',972839),('redis',972827)):
  row=b.source_ops._resource(kind);s=row['State'];before[kind]=(s['Pid'],s['Running'],row['HostConfig']['RestartPolicy']['Name'])
  need(s['Pid']==pid and s['Running']==(kind!='app') and s.get('OOMKilled') is False,'source-resource-changed')
  if kind=='app':need(s['Status']=='exited' and s['ExitCode']==0 and row['HostConfig']['RestartPolicy']['Name']=='no','app-not-cleanstopped')
 ledger=b.source_ops._ledger();op_id=M.B.operation_id(payload['owner'],'source','source:app_stop')
 op=ledger['operations'].get(op_id)
 need(op is not None and op['request']=={'action':'app_stop','arguments':{}} and op['phase']=='pending' and
      op.get('steps')=={'sigterm':'accepted'},'exact-owned-pending-app-stop-required')
 need(not any(x['request']['action'] in ('pg_stop','redis_failover','redis_role_persist') for x in ledger['operations'].values()),'irreversible-operation-already-intended')
 transport=b.store.read('transport');need(transport['sources']['source:app_stop']['op_id']==op_id,'transport-app-stop-id-changed')
 saved=scope/'app-stop-reconcile-20261017';saved.mkdir(mode=0o700);sync(scope)
 for name in ('state.json','source-operations.json','transport-state.json'):write_new(saved/name,trusted(scope/name,0o600))
 # SourceOperations.app_stop checks the same owned step while holding its
 # ledger lock. Its stopped branch records completion without a signal.
 result=b.source('source:app_stop','app_stop')
 need(result.get('phase')=='complete' and result.get('steps')=={'sigterm':'accepted'},'app-stop-reconcile-incomplete')
 after={}
 for kind in before:
  row=b.source_ops._resource(kind);s=row['State'];after[kind]=(s['Pid'],s['Running'],row['HostConfig']['RestartPolicy']['Name'])
 need(before==after and trusted(scope/'state.json',0o600)==rawstate,'resources-or-checkpoint-changed')
 verified={'ok':True,'app_stop_op_id':op_id,'phase':'complete','no_new_signal':True,'resource_states_unchanged':True,'state_raw_sha256':sha(rawstate)}
 write_new(saved/'verification.json',canonical(verified)+b'\n');print(json.dumps(verified,sort_keys=True))
'''

TARGET_DRAIN = r'''
need(sha(trusted(lib/'target_rpc.py',0o700))==payload['new']['target_rpc.py'],'target-program-changed')
need(sha(trusted(root/'rpc/config.json',0o600))==payload['target_config_sha'],'target-config-changed')
M=load('retry_target_new',lib/'target_rpc.py');cfg=M.load_config();b=M.LocalBackend(cfg);store=M.FileStore(cfg)
with store.mutation_lock():
 ledger=store.read();ops=ledger['operations']
 restores=[(key,op) for key,op in ops.items() if op['request']['action']=='app.restore' and op['phase']=='COMPLETE' and op.get('response',{}).get('ok') is True]
 need(restores,'completed-restore-missing');restore_id,restore=max(restores,key=lambda row:row[1]['request']['expected_epoch'])
 epoch=restore['response']['result']['ingress']['epoch']
 read=b.app_session()
 def status(name):
  row=read(name+'.status');need(row['http_status']==200,'control-read-failed');return row['body']
 for _ in range(120):
  sampled_started=time.time()
  before=status('ingress');runtime=status('runtime');after=status('ingress')
  ing=runtime.get('ingress',{})
  if runtime.get('active_requests')==runtime.get('active_hijacked_connections')==0 and ing.get('queued_requests')==0 and before==after==ing:break
  time.sleep(.25)
 else:raise RuntimeError('no-drained-cohort-observed')
 pg=b.pg_sample();redis=b.redis_inspect()
 proof={'v':1,'run':'20261016','owner':cfg['owner'],'checked_at':sampled_started,'ledger_sha256':sha(M.canonical(ledger)),
  'ledger_epoch':ledger['epoch'],'runtime':runtime,'ingress_before':before,'ingress_after':after,
  'restore_op_id':restore_id,'restore_ingress_epoch':epoch,'pg_authority':ledger['pg']['authority'],
  'redis_authority':ledger['redis']['authority'],'pg_in_recovery':pg['in_recovery'],'redis_role':redis['role'],
  'arm_cancelled':ledger['pg'].get('arm',{}).get('status')=='CANCELLED','promotion_intent':ledger['pg'].get('promotion_intent'),
  'all_operations_complete':all(op['phase']=='COMPLETE' for op in ops.values()),
  'all_quiesces_cancelled':all(op.get('cancelled') is True for op in ops.values() if op['request']['action']=='app.quiesce'),
  'endpoints':b.endpoint_state(),'app_pid':b.resource('app')['State']['Pid'],
  'pg_pid':b.resource('pg')['State']['Pid'],'redis_pid':b.resource('redis')['State']['Pid']}
 drain_proof(proof,time.time());print(json.dumps(proof,sort_keys=True))
'''

SOURCE_AUTHORIZE = r'''
for name,value in payload['dependencies'].items():need(sha(trusted(lib/name,0o644))==value,'source-dependency-changed')
for name in payload['new']:need(sha(trusted(lib/name,0o644))==payload['new'][name],'source-new-program-changed')
M=load('retry_service_new',lib/'runner_service.py')
with M.execution_lock():
 b,state=source_idle(M);proof=payload['proof'];drain_proof(proof,time.time())
 need(proof['runtime']['instance_id']==state['quiet_instance_id'],'prior-instance-changed')
 oldraw=trusted(scope/'state.json',0o600)
 need(sha(oldraw)==payload['state_raw_sha256'],'source-checkpoint-CAS-mismatch')
 cid=payload['cohort_id'];need(len(cid)==32 and all(c in '0123456789abcdef' for c in cid),'invalid-cohort-id')
 directory=scope/'budget-cohorts'
 if not directory.exists():directory.mkdir(mode=0o700);sync(scope)
 s=directory.lstat();need(stat.S_ISDIR(s.st_mode) and s.st_uid==0 and stat.S_IMODE(s.st_mode)==0o700,'untrusted-cohort-directory')
 archive_dir=scope/('request-cohort-'+cid);need(not archive_dir.exists() and not archive_dir.is_symlink(),'cohort-already-attempted-inspect-only')
 archive_dir.mkdir(mode=0o700);sync(scope)
 proofraw=canonical(proof)+b'\n';write_new(archive_dir/'drain-proof.json',proofraw)
 write_new(archive_dir/'previous-state.json',oldraw)
 record={'v':1,'run':'20261016','owner':payload['owner'],'id':cid,'previous_state':state,
         'previous_state_sha256':sha(canonical(state)),'proof_sha256':sha(proofraw)}
 recordraw=canonical(record)+b'\n';write_new(directory/(cid+'.json'),recordraw)
 newstate=json.loads(json.dumps(state));newstate['budget_cohort']={'id':cid,'record_sha256':sha(recordraw)}
 accounting=M.E.budget_accounting(newstate)
 need(accounting['total_pause_seconds']==payload['prior_pause'] and accounting['cohort_pause_seconds']==0 and accounting['max_cohort_pause_seconds']==payload['prior_pause'],'cohort-accounting-not-preserved')
 # State reference is the sole activation point. A record left by a crash is
 # inert until the exact old checkpoint is CAS-replaced under the backend lock.
 with b.store.lock():
  need(trusted(scope/'state.json',0o600)==oldraw,'source-checkpoint-CAS-mismatch')
  drain_proof(proof,time.time());b.store._write('state',newstate)
 result={'ok':True,'cohort_id':cid,'record_raw_sha256':sha(recordraw),'proof_raw_sha256':sha(proofraw),
         'previous_global_pause_seconds':payload['prior_pause'],'budget':accounting,'main_started':False,
         'transport_and_rpc_ledgers_preserved':True,'previous_requests_ended_or_cancelled':True}
 write_new(archive_dir/'authorization-verification.json',canonical(result)+b'\n');print(json.dumps(result,sort_keys=True))
'''


def private_result(path, result):
    data = result.stdout if result.returncode == 0 else json.dumps({"exit": result.returncode,
        "stdout": result.stdout.decode(errors="replace")[-4096:],
        "stderr": result.stderr.decode(errors="replace")[-4096:]}).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data); out.flush(); os.fsync(out.fileno())
    if result.returncode:
        raise RuntimeError("retry-preparation-incomplete-inspect-private-evidence")
    return json.loads(result.stdout)


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(json.dumps(value, sort_keys=True).encode()+b"\n"); out.flush(); os.fsync(out.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"offline": True, "main_started": False, "request_pauses": False,
                          "global_pause_history_reset": False, "reviewed_programs": NEW}))
        return
    if set(NEW) != set(OLD):
        raise RuntimeError("independent-review-and-frozen-digests-required")
    folder = pathlib.Path(__file__).parent
    programs = {name: (folder / name).read_bytes() for name in NEW}
    if any(hashlib.sha256(raw).hexdigest() != NEW[name] for name, raw in programs.items()):
        raise RuntimeError("reviewed-program-changed")
    channel = runpy.run_path(str(folder / "prepare_channel.py"))
    payload = {"old": OLD, "new": NEW, "owner": OWNER, "prior_pause": PRIOR_PAUSE,
               "dependencies": DEPENDENCIES,
               "target_config_sha": TARGET_CONFIG_SHA,
               "programs": {name: base64.b64encode(raw).decode() for name, raw in programs.items()}}
    def remote(label, code, host, timeout=45):
        path = FOLDER / ("retry-" + label + "-20261017.json")
        if path.exists() or path.is_symlink():
            raise RuntimeError("prior-attempt-exists-inspect-and-reconcile")
        script = COMMON + VALIDATION + code
        compile(script, label, "exec")
        private_json(path.with_suffix(".intent.json"), {"script_sha256": hashlib.sha256(script.encode()).hexdigest(),
                     "cohort_id": payload.get("cohort_id"), "inspect_before_any_retry": True})
        try:
            result = subprocess.run(host + ["sudo python3 -I -c " + shlex.quote(script)],
                                    input=json.dumps(payload).encode(), capture_output=True, timeout=timeout)
        except BaseException as error:
            private_json(path.with_suffix(".uncertain.json"), {"exception": type(error).__name__,
                         "inspect_before_any_retry": True})
            raise
        return private_result(path, result)
    remote("app-reconcile", SOURCE_RECONCILE_APP, channel["SSH_SOURCE"])
    target = remote("target-upgrade", TARGET_UPGRADE, channel["TARGET"])
    payload["target_config_sha"] = target["config_raw_sha256"]
    source = remote("source-upgrade", SOURCE_UPGRADE, channel["SSH_SOURCE"])
    payload["state_raw_sha256"] = source["state_raw_sha256"]
    payload["proof"] = remote("target-drain", TARGET_DRAIN, channel["TARGET"], 55)
    payload["cohort_id"] = secrets.token_hex(16)
    result = remote("cohort-authorize", SOURCE_AUTHORIZE, channel["SSH_SOURCE"])
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
