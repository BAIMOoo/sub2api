#!/usr/bin/env python3
"""Restore only the owned migration slot/LOGIN and start the isolated backup.

Offline unless --apply is supplied. The source guard remains running. Each
nontransactional operation has its own durable intent and independent readback;
an unknown result requires inspection and is never automatically replayed.
No database restart, promotion, request pause or main-runner start is included.
"""
import argparse
import hashlib
import json
import pathlib
import runpy
import shlex
import subprocess
import time

OWNER = '30bda011defad8149621c2630a2bd89c'
GUARD_SHA = '72cb81b27bcf8c4f316511ca0460f44af453ab51276d53cda82ba55e2cee8d05'
FENCE_SHA = '64f690b49b92e6be6434efedea2a1387e7f15d1f4e13909e15865c299a497790'
RESEED_SHA = '757f7c07ce6fb70ee78f5b223361ae62dbd6acd2d5e9597ee5f9455a24708200'
TARGET_LEDGER_SHA = 'abd784df64eaf0fb3d5d622f8025a221657078b731cf97d9859cee3ba8991ce3'
ROLE_COMMENT_SHA = '12a9c08c6ccb61822bf2130243f040e02d0046c09f116d45b5e76de7cd9ed007'

# This exact validation surface is shared by the remote source and local tests.
VALIDATION = r'''
def source_boundary(state, owner):
 need(state.get('v')==1 and state.get('run')=='20261016' and state.get('owner')==owner and
      state.get('attempt')==3 and state.get('phase')=='SOURCE_DATA' and
      state.get('pg')==state.get('redis')=='AWS' and state.get('window') is None and
      state.get('deadline_breached') is False and state.get('route_fenced') is True and
      state.get('pause_seconds')==14.656846920028329 and
      state.get('budget_cohort')=={'id':'7b54d1b6384a4d859436856bbbce4dcb',
       'record_sha256':'d7339281e53fc4fb84cd9ea92b960860390baa157402b0b8da830512b2098b7e'},
      'exact-source-attempt-boundary-required')
def fresh_target(proof, now, owner, ledger_sha):
 need(proof.get('ok') is True and proof.get('owner')==owner and
      type(proof.get('checked_at')) in (int,float) and 0<=now-proof['checked_at']<=30 and
      proof.get('ledger_sha256')==ledger_sha and proof.get('epoch')==14 and
      proof.get('pg_id')=='ac9f494b2fd0ec87257a9a7308a6ad7b270051d1dc4da33b33fea9af2885418c' and
      proof.get('pg_clean_stopped') is True and proof.get('source_endpoints') is True,
      'fresh-prepared-target-required')
def database_proof(facts, login, slot_present, role_comment_sha):
 need(facts.get('system_identifier')=='7626048206782038050' and facts.get('timeline')==1 and
      facts.get('in_recovery') is False and facts.get('data_directory')=='/var/lib/postgresql/data' and
      type(facts.get('wal_cap_bytes')) is int and facts['wal_cap_bytes']==512*1024**2,
      'source-cluster-or-WAL-cap-changed')
 role=facts.get('role',{})
 need(role.get('name')=='sub2api_mig_20261016_replica' and role.get('login') is login and
      role.get('replication') is True and all(role.get(k) is False for k in
      ('superuser','createdb','createrole','bypassrls')) and
      type(role.get('comment')) is str and sha(role['comment'].encode())==role_comment_sha,
      'owned-replication-role-required')
 slots=facts.get('slots')
 need(type(slots) is list and len(slots)==int(slot_present),'unexpected-owned-slot-count')
 if slot_present:
  slot=slots[0]
  need(slot.get('name')=='sub2api_mig_20261016' and slot.get('type')=='physical' and
       slot.get('temporary') is False and slot.get('active') is False and
       slot.get('wal_status')=='reserved' and type(slot.get('restart_lsn')) is str and
       __import__('re').fullmatch(r'[0-9A-F]{1,8}/[0-9A-F]{1,8}',slot['restart_lsn']) and
       type(slot.get('retained_bytes')) is int and 0<=slot['retained_bytes']<384*1024**2,
       'owned-inactive-reserved-slot-required')
def guard_proof(status, now, owner, boot, mode):
 need(status.get('owner')==owner and status.get('run')=='20261016' and
      status.get('boot_id')==boot and type(status.get('checked_at')) in (int,float) and
      0<=now-status['checked_at']<=1 and status.get('healthy') is True and
      status.get('pg_wal_protected') is True and status.get('tripped') is False and
      status.get('recovering') is False and status.get('sample',{}).get('mode')==mode,
      'fresh-owned-guard-required')
def exclusive_sql_step(saved, label, query, invoke, inspect, verify):
 # An intent survives timeout, process loss, failed readback, and a successful
 # SQL command whose receipt was not written. Nothing in this helper retries.
 write_new(saved/(label+'.intent.json'),canonical({'at':time.time(),'query_sha256':sha(query)}))
 invoke(query)
 facts=inspect()
 verify(facts)
 write_new(saved/(label+'.verified.json'),canonical({'at':time.time(),'verified':True}))
 return facts
'''

TARGET = r'''
program=lib/'reseed_standby.py'
need(sha(trusted(program,0o700))==payload['reseed_sha'],'reviewed-reseed-program-changed')
R=load('ready_reseed',program)
M,cfg,b,store=R.load()
def prepared_target():
 with store.mutation_lock(), b.pg_locks():
  p,row,path=R.plan_and_stopped(M,cfg,b,store)
  raw=trusted(root/'rpc/state.json',0o600);ledger=store.read()
  need(sha(raw)==payload['target_ledger_sha'] and ledger['epoch']==14,'target-ledger-changed')
  need(row['State']['Pid']==0 and row['HostConfig']['RestartPolicy']['Name']=='no',
       'target-PG-not-retired')
  need((R.SCOPE/'stop.signal-accepted.json').is_file() and
       (R.SCOPE/'monitor-stopped.json').is_file() and
       not (R.SCOPE/'backup.intent.json').exists(),'target-not-prepared-for-first-backup')
  fields=dict(x.split('=',1) for x in command(['systemctl','show',R.MONITOR+'.timer',
      '--property=ActiveState,Job']).decode().splitlines())
  need(fields['ActiveState']=='inactive' and not fields['Job'],'target-monitor-not-retired')
  return {'ok':True,'owner':cfg['owner'],'checked_at':time.time(),
    'ledger_sha256':sha(raw),'epoch':ledger['epoch'],'pg_id':row['Id'],
    'pg_clean_stopped':True,'source_endpoints':True,'plan_sha256':sha(trusted(R.SCOPE/'plan.json',0o600))}
'''

TARGET_READ = TARGET + r'''
print(json.dumps(prepared_target(),sort_keys=True))
'''

SOURCE = r'''
scope=root/'runner';saved=scope/'source-reseed-ready-20261017'
need(sha(trusted(lib/'runner_guard.py',0o644))==payload['guard_sha'],'guard-program-changed')
M=load('ready_service',lib/'runner_service.py')
G=load('ready_guard',lib/'runner_guard.py');g=G.LocalBackend();b=M.B.RunnerBackend()
query=G.PG_SAMPLE.replace(b"'active',active,",b"'active',active,'wal_status',wal_status,")
need(b"'wal_status',wal_status" in query,'source-slot-proof-query-changed')
def sql(value):
 with G.S.pg_sampling_lock():
  return command(['/usr/bin/docker','exec','-i',b.source_ops.m['containers']['postgres']['id'],
     'psql','-X','-q','-A','-t','-w','-v','ON_ERROR_STOP=1','-U','sub2api','-d','sub2api'],value,timeout=2)
def snapshot():
 facts=G.S.strict_json(sql(query).strip());g._cluster(facts);g._role(facts);g._slots(facts)
 stats=os.statvfs(g.config['pg_data_host_path'])
 need(stats.f_bavail*stats.f_frsize>=G.FREE_MIN,'source-disk-low')
 return facts
def check(login,slot):
 return lambda facts:database_proof(facts,login,slot,payload['role_comment_sha'])
def resources():
 result={}
 for kind,pid in [('app',0),('postgres',972839),('redis',972827)]:
  row=b.source_ops._resource(kind);s=row['State']
  need(s['Pid']==pid and s['Running']==(kind!='app') and s['OOMKilled'] is False and
       row['HostConfig']['RestartPolicy']['Name']==('no' if kind=='app' else 'unless-stopped'),
       'source-business-resource-changed')
  if kind=='app':need(s['Status']=='exited' and s['ExitCode']==0,'source-app-not-clean-retired')
  result[kind]={'id':row['Id'],'image':row['Image'],'pid':s['Pid'],'started_at':s['StartedAt']}
 return result
def healthy(mode):
 need(not (scope/'guard-trip.json').exists() and not (scope/'guard-trip.json').is_symlink(),
      'new-guard-trip-inspect-only')
 status=b.source_ops._guard()
 guard_proof(status,time.time(),payload['owner'],g.boot_id(),mode)
 need(g.main_status()['quiescent'],'main-started-during-reseed')
 return status
with M.execution_lock():
 state=b.store.read('state');source_boundary(state,payload['owner'])
 fresh_target(payload['target'],time.time(),payload['owner'],payload['target_ledger_sha'])
 need(g.main_status()['quiescent'] and not M.selftest_armed(payload['owner']),'source-not-quiescent')
 M.verify_entry();M.read_gates(payload['owner']);b.source_ops._assert_monitors_retired()
 operations=b.source_ops._ledger()['operations'].values()
 need(not any(x['request']['action'] in ('pg_stop','redis_failover','redis_role_persist')
              for x in operations),'source-data-stop-already-intended')
 app=[x for x in operations if x['request']['action']=='app_stop']
 need(len(app)==1 and app[0]['phase']=='complete' and 'sigterm' in app[0]['steps'],
      'owned-app-retirement-required')
 before=resources();healthy('OWNED_REPLICA_FENCED')
 fence=trusted(scope/'guard-fence.json',0o600)
 need(sha(fence)==payload['fence_sha'],'original-fence-changed')
 old=json.loads(fence)
 need(old['owner']==payload['owner'] and old['run']=='20261016' and old['boot_id']==g.boot_id() and
      old['fenced'] is True,'original-owned-fence-required')
 facts=snapshot();check(False,False)(facts)
 frozen={name:trusted(scope/name,0o600) for name in
   ('state.json','transport-state.json','source-operations.json','guard-config.json','source-manifest.json')}
 need(not saved.exists() and not saved.is_symlink(),'source-reseed-already-attempted-inspect-only')
 saved.mkdir(mode=0o700);sync(scope)
 write_new(saved/'intent.json',canonical({'at':time.time(),'owner':payload['owner'],
   'target':payload['target'],'guard_sha256':payload['guard_sha'],'source_files':{k:sha(v) for k,v in frozen.items()}}))
 for name,raw in frozen.items():write_new(saved/name,raw)
 write_new(saved/'original-guard-fence.json',fence)
 def unchanged():
  need(all(trusted(scope/name,0o600)==raw for name,raw in frozen.items()),'source-ledger-or-config-CAS-changed')
  need(resources()==before,'source-business-process-changed')
  need(sha(trusted(lib/'runner_guard.py',0o644))==payload['guard_sha'],'guard-program-changed')
  need(g.main_status()['quiescent'],'main-not-quiescent')
  M.verify_entry()
 # Slot creation is intentionally distinct from LOGIN. A PostgreSQL slot is
 # not rolled back together with a later failing ALTER ROLE transaction.
 invariant="""IF (SELECT system_identifier::text FROM pg_control_system()) <> '7626048206782038050'
 OR (SELECT timeline_id FROM pg_control_checkpoint()) <> 1 OR pg_is_in_recovery()
 OR current_setting('data_directory') <> '/var/lib/postgresql/data'
 OR pg_size_bytes(current_setting('max_slot_wal_keep_size')) <> 536870912
 OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sub2api_mig_20261016_replica'
 AND rolreplication AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
 AND NOT rolcreaterole AND NOT rolbypassrls
 AND encode(sha256(convert_to(coalesce(shobj_description(oid,'pg_authid'),''),'UTF8')),'hex')='"""+payload['role_comment_sha']+"""')
 THEN RAISE EXCEPTION 'OWNED_SOURCE_CHANGED'; END IF;"""
 create=("SET statement_timeout='450ms'; SET lock_timeout='100ms'; DO $owned$ BEGIN "+invariant+
   " IF EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='sub2api_mig_20261016') THEN RAISE EXCEPTION 'SLOT_EXISTS'; END IF; "
   "PERFORM pg_create_physical_replication_slot('sub2api_mig_20261016',true); END $owned$;\n").encode()
 unchanged()
 exclusive_sql_step(saved,'create-slot',create,sql,snapshot,check(False,True))
 unchanged()
 login=("SET statement_timeout='450ms'; SET lock_timeout='100ms'; DO $owned$ BEGIN "+invariant+
   " IF NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='sub2api_mig_20261016' AND slot_type='physical' AND NOT temporary AND NOT active AND wal_status='reserved' AND restart_lsn IS NOT NULL AND pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)>=0 AND pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)<402653184) THEN RAISE EXCEPTION 'OWNED_SLOT_CHANGED'; END IF; "
   "ALTER ROLE sub2api_mig_20261016_replica LOGIN; END $owned$;\n").encode()
 exclusive_sql_step(saved,'role-login',login,sql,snapshot,check(True,True))
 unchanged()
 started=time.time();end=time.monotonic()+8;accepted=[];last=0
 while time.monotonic()<end:
  try:status=healthy('RUNNING_BOUNDED')
  except (G.S.Refusal,RuntimeError) as error:
   need(not (scope/'guard-trip.json').exists(),'new-guard-trip-inspect-only')
   need(str(error) in ('fresh-owned-guard-required','RECOVERY_GUARD_NOT_PROTECTING',
        'STALE_GUARD_PROOF','STALE_GUARD_STATUS','STALE_PROOF'), 'guard-verification-failed-inspect-only')
   time.sleep(.1);continue
  checked=status['checked_at']
  if checked>=started and checked>last:
   accepted.append(status);last=checked
   if len(accepted)>=3 and checked-accepted[0]['checked_at']>=1:break
  time.sleep(.15)
 need(len(accepted)>=3 and accepted[-1]['checked_at']-accepted[0]['checked_at']>=1,
      'bounded-running-guard-not-stable-inspect-only')
 facts=snapshot();check(True,True)(facts);unchanged();healthy('RUNNING_BOUNDED')
 need(trusted(scope/'guard-fence.json',0o600)==fence,'fence-retirement-CAS-changed')
 write_new(saved/'retire-fence.intent.json',canonical({'at':time.time(),'sha256':sha(fence)}))
 need(trusted(scope/'guard-fence.json',0o600)==fence,'fence-retirement-CAS-changed')
 os.rename(scope/'guard-fence.json',saved/'retired-guard-fence.json');sync(scope);sync(saved)
 write_new(saved/'retire-fence.complete.json',canonical({'at':time.time(),'sha256':sha(fence)}))
 unchanged();facts=snapshot();check(True,True)(facts);status=healthy('RUNNING_BOUNDED')
 need(not (scope/'guard-fence.json').exists() and not (scope/'guard-fence.json').is_symlink(),
      'new-fence-inspect-only')
 slot=facts['slots'][0]
 proof={'v':1,'run':'20261016','owner':payload['owner'],'healthy':True,'role_login':True,
   'slot':'sub2api_mig_20261016','checked_at':time.time(),'system_identifier':facts['system_identifier'],
   'timeline':facts['timeline'],'guard_mode':'RUNNING_BOUNDED','guard_checked_at':status['checked_at'],
   'guard_sha256':payload['guard_sha'],'slot_restart_lsn':slot['restart_lsn'],
   'slot_wal_status':slot['wal_status'],'retained_bytes':slot['retained_bytes'],
   'source_pids_unchanged':True,'source_ledgers_preserved':True,'fence_retired_sha256':sha(fence)}
 write_new(saved/'source-ready.json',canonical(proof))
 print(json.dumps(proof,sort_keys=True))
'''

TARGET_START = TARGET + r'''
prepared_target()
proof=payload['source']
need(proof.get('v')==1 and proof.get('run')=='20261016' and proof.get('owner')==R.OWNER and
     proof.get('healthy') is True and proof.get('role_login') is True and
     proof.get('slot')=='sub2api_mig_20261016' and type(proof.get('checked_at')) in (int,float) and
     0<=time.time()-proof['checked_at']<=20 and proof.get('guard_mode')=='RUNNING_BOUNDED' and
     proof.get('guard_sha256')==payload['guard_sha'] and proof.get('slot_wal_status')=='reserved' and
     proof.get('source_pids_unchanged') is True and proof.get('source_ledgers_preserved') is True,
     'fresh-source-reseed-proof-required')
write_new(R.SCOPE/'source-ready.json',canonical(proof))
# backup() performs its own locked boundary and <=30s proof check immediately.
import contextlib,io
out=io.StringIO()
with contextlib.redirect_stdout(out):R.backup()
result=json.loads(out.getvalue());need(result['backup_started'] is True,'backup-not-started')
print(json.dumps({'ok':True,'backup':result,'source_proof_checked_at':proof['checked_at'],
                  'at':time.time()},sort_keys=True))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({'offline': True, 'guard_kept_running': True,
                          'source_restart': False, 'main_started': False,
                          'request_pause': False, 'unknown_results_replayed': False}))
        return
    folder = pathlib.Path(__file__).parent
    if hashlib.sha256((folder/'reseed_standby.py').read_bytes()).hexdigest() != RESEED_SHA:
        raise RuntimeError('reviewed-reseed-program-changed')
    common = runpy.run_path(str(folder/'recover_source_guard.py'))['COMMON']
    prior = runpy.run_path(str(folder/'prepare_retry.py'))
    channel = runpy.run_path(str(folder/'prepare_channel.py'))
    payload = {'owner': OWNER, 'guard_sha': GUARD_SHA, 'fence_sha': FENCE_SHA,
               'reseed_sha': RESEED_SHA, 'target_ledger_sha': TARGET_LEDGER_SHA,
               'role_comment_sha': ROLE_COMMENT_SHA}

    def remote(label, code, host, timeout):
        path = prior['FOLDER']/('source-reseed-'+label+'-20261017.json')
        script = common + VALIDATION + code
        compile(script, label, 'exec')
        if path.exists() or path.is_symlink():
            raise RuntimeError('prior-result-inspect-only')
        prior['private_json'](path.with_suffix('.intent.json'), {
            'script_sha256': hashlib.sha256(script.encode()).hexdigest(),
            'inspect_before_retry': True})
        try:
            result = subprocess.run(host+['sudo python3 -I -c '+shlex.quote(script)],
                                    input=json.dumps(payload).encode(), capture_output=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            prior['private_json'](path, {'ok': False, 'code': 'REMOTE_OUTCOME_UNKNOWN_INSPECT_ONLY'})
            raise RuntimeError('remote-outcome-unknown-inspect-only') from None
        return prior['private_result'](path, result)

    result = coordinate(remote, payload, channel['SSH_SOURCE'], channel['TARGET'])
    print(json.dumps(result, sort_keys=True))


def coordinate(remote, payload, source_host, target_host, clock=time.time):
    """Keep fresh source proof and target backup admission in one local run."""
    payload['target'] = remote('target-prepared', TARGET_READ, target_host, 25)
    payload['source'] = remote('source-ready', SOURCE, source_host, 40)
    if not 0 <= clock()-payload['source']['checked_at'] <= 15:
        raise RuntimeError('source-proof-aged-inspect-only')
    return remote('target-backup', TARGET_START, target_host, 25)


if __name__ == '__main__':
    main()
