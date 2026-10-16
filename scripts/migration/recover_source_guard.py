#!/usr/bin/env python3
"""Recover only the pinned AWS guard trip; offline unless --apply is explicit.

This is a one-attempt source recovery, not a replay of the two-host upgrade.
An uncertain phase is retained for inspection and is never automatically retried.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import stat
import subprocess
import time

RUN = "20261016"
OWNER = "30bda011defad8149621c2630a2bd89c"
SOURCE_PIDS = {"app": 2260633, "postgres": 972839, "redis": 972827}
TARGET_PIDS = {"app": 257639, "pg": 180813, "redis": 133623}
TARGET_CONFIG_SHA = "9eb33e0f44ac2e6b8c9e743a2bd7593548967aa2eca2233548391a1581df2169"
PINS = {
    "guard-trip.json": "a915bb07f40825a61149463a9ae3b66ec15abbed881bf1e1ed76ea06fe5dfde4",
    "state.json": "4847490cfac0418664168d4032f3d47a1eec05ba871d1deeb12351b2c17ec339",
    "guard-config.json": "84e16b40d83b7f58ab9233b6eb12cb33b06b655ae165bde159711138a7941973",
    "source-manifest.json": "1dd72b9b0b03870ad59c12bdb71cda1571d8c3c2708b0dd258e152c3c8764844",
    "controller-selftest.json": "18495a6ddddf2c6f6a9ffce756511fa832b324c170279b9c3b92f1eb51c66c64",
}
ROLE_COMMENT_SHA = "12a9c08c6ccb61822bf2130243f040e02d0046c09f116d45b5e76de7cd9ed007"
STATE = {"attempt": 0, "owner": OWNER, "pause_seconds": 0.0, "pg": "AWS",
         "phase": "ENTRY_PREPARATION", "redis": "AWS", "route_fenced": False,
         "run": RUN, "v": 1, "window": None}

COMMON = r'''
import base64,hashlib,importlib.util,json,os,pathlib,re,stat,subprocess,sys,time
sys.dont_write_bytecode=True
payload=json.load(sys.stdin)
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016')
root=pathlib.Path('/var/lib/sub2api-migration-20261016')
sha=lambda raw:hashlib.sha256(raw).hexdigest()
def need(value,code):
 if not value:raise RuntimeError(code)
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec)
 sys.modules[name]=m;spec.loader.exec_module(m);return m
def trusted(path,mode):
 for part in [path.parent,*path.parent.parents]:
  s=part.lstat();need(stat.S_ISDIR(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022,'untrusted-parent')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  s=os.fstat(fd);need(stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==mode and s.st_size<=2097152,'untrusted-file')
  return os.read(fd,2097153)
 finally:os.close(fd)
def command(argv,data=None,timeout=3):
 r=subprocess.run(argv,input=data,capture_output=True,timeout=timeout,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','LC_ALL':'C'})
 need(r.returncode==0 and len(r.stdout)<=2097152,'bounded-command-failed');return r.stdout
def sync(path):
 fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
def write_new(path,raw,mode=0o600):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
 sync(path.parent)
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()+b'\n'
def atomic(path,raw,mode=0o600):
 tmp=path.with_name('.guard-recovery-'+path.name);write_new(tmp,raw,mode);os.replace(tmp,path);sync(path.parent)
'''

# This script uses direct reads only. In particular, it never calls RPC.handle,
# whose operation admission can write a ledger even for an apparent inspection.
TARGET_READ = r'''
prog=lib/'target_rpc.py';cfgpath=root/'rpc/config.json'
need(sha(trusted(prog,0o700))==payload['new']['target_rpc.py'],'target-program-changed')
need(sha(trusted(cfgpath,0o600))==payload['target_config_sha'],'target-config-changed')
M=load('recovery_target_rpc',prog);cfg=M.load_config();b=M.LocalBackend(cfg);ledger=M.FileStore(cfg).read()
need(ledger['epoch']==2 and ledger['pg']['authority']==ledger['redis']['authority']=='AWS' and not ledger['pg'].get('arm') and not ledger['pg'].get('promotion_intent') and all(op['phase']=='COMPLETE' for op in ledger['operations'].values()),'target-authority-or-ledger-changed')
need(b.endpoint_state()=={'v':1,'postgres':'source','redis':'source'},'target-endpoints-changed')
pids={k:b.resource(k)['State']['Pid'] for k in payload['target_pids']}
need(pids==payload['target_pids'],'target-pids-changed')
pg=b.pg_sample();need(pg['system_identifier']=='7626048206782038050' and pg['in_recovery'] is True and pg['source_timeline']==cfg['pg']['source_timeline'],'target-pg-changed')
need(b.redis_inspect()['role']=='slave','target-redis-changed')
query=b"SET statement_timeout='1s'; SELECT json_build_object('slot',current_setting('primary_slot_name'),'receivers',(SELECT json_agg(json_build_object('status',status,'timeline',received_tli,'slot',slot_name)) FROM pg_stat_wal_receiver));"
receiver=M.strict_json(b.command(['/usr/bin/docker','exec','--user','postgres','-i',cfg['pg']['container_id'],'psql','-X','-q','-A','-t','-w','-v','ON_ERROR_STOP=1','-h','/tmp','-p','25432','-U','sub2api','-d','sub2api'],query),16384)
need(receiver['slot']=='sub2api_mig_20261016' and len(receiver['receivers'] or [])==1 and receiver['receivers'][0]=={'status':'streaming','timeline':cfg['pg']['source_timeline'],'slot':'sub2api_mig_20261016'},'target-receiver-not-streaming')
controls={}
for name in ('runtime','postgres','redis','background'):
 r=b.app_control(name+'.status');need(r['http_status']==200,'target-control-unavailable');controls[name]=r['body']
rt,p,r,bg=(controls[k] for k in ('runtime','postgres','redis','background'))
need(rt.get('ready') is True and rt.get('serving') is True and rt.get('draining') is False and rt.get('background_workers_enabled') is False and p.get('endpoint')=='source' and p.get('paused') is False and r.get('address')=='127.0.0.1:16379' and r.get('paused') is False and r.get('pubsub_cache_ready') is True and bg.get('activated') is False and bg.get('activating') is False and bg.get('started')==[] and not bg.get('failures'),'target-not-ready-with-background-off')
entry=dict(line.split('=',1) for line in command(['/usr/bin/systemctl','show','sub2api-tencent-entry.service','--property=MainPID,ActiveState,Job']).decode().splitlines())
need(entry=={'MainPID':'259990','ActiveState':'active','Job':''},'target-entry-changed')
print(json.dumps({'ok':True,'checked_at':time.time(),'pids':pids,'ledger_sha256':sha(M.canonical(ledger)),'system_identifier':pg['system_identifier'],'timeline':pg['source_timeline'],'receiver':receiver,'background_off':True,'config_sha256':payload['target_config_sha']},sort_keys=True))
'''

SOURCE_RECOVERY = r'''
scope=root/'runner';saved=scope/'guard-trip-recovery-20261017'
old,new=payload['old'],payload['new'];pins=payload['pins']
need(set(old)==set(new)=={'target_rpc.py','durable_runner.py','runner_service.py','source_operations.py','runner_guard.py'},'program-set-changed')
programs={name:base64.b64decode(payload['programs'][name],validate=True) for name in new}
for name,raw in programs.items():
 need(sha(raw)==new[name],'new-program-digest-changed');compile(raw,name,'exec')
oldraw={name:trusted(lib/name,0o644) for name in old}
need(all(sha(oldraw[name])==old[name] for name in old),'source-not-exact-old-programs')
S=load('recovery_source_operations',lib/'source_operations.py')
G=load('recovery_old_guard',lib/'runner_guard.py');backend=G.LocalBackend();source=backend.source
need(source.m['owner']==payload['owner'],'source-owner-changed')
need(backend.config['role_owner_comment_sha256']==payload['role_comment_sha'],'role-comment-config-changed')
def fixed_files():
 raw={name:trusted(scope/name,0o600) for name in pins}
 need(all(sha(raw[name])==pins[name] for name in pins),'pinned-source-file-changed')
 need(json.loads(raw['state.json'])==payload['state'],'not-exact-initial-state')
 trip=json.loads(raw['guard-trip.json'])
 need(trip['reason']=='GUARD_SAMPLE_TIMEOUT' and trip['owner']==payload['owner'] and trip['run']=='20261016' and trip['boot_id']==backend.boot_id(),'unexpected-trip')
 need(json.loads(raw['controller-selftest.json'])=={'v':1,'run':'20261016','owner':payload['owner'],'armed':False},'selftest-armed')
 closed=json.loads(trusted(scope/'passed-selftest-20261017/closure-verification.json',0o600))
 need(closed.get('ok') is True and closed.get('selftest_disarmed') is True,'prior-selftest-not-closed')
 need(not (scope/'guard-fence.json').exists() and not (scope/'guard-fence.json').is_symlink(),'fence-already-present')
 need(backend.main_status()['quiescent'],'main-not-quiescent')
 main=dict(line.split('=',1) for line in command(['/usr/bin/systemctl','show',G.MAIN_UNIT,'--property=MainPID,ActiveState,Job']).decode().splitlines())
 need(main=={'MainPID':'0','ActiveState':'inactive','Job':''},'main-not-inactive')
 nat=command(['/usr/sbin/iptables-save','-t','nat'])
 need(b'sub2api-live-migration-20261016' not in nat,'entry-bridge-present')
 need(not (scope/'entry-bridge/state.json').exists() and not (scope/'entry-bridge/state.json').is_symlink(),'bridge-already-attempted')
 return raw,nat
def resources():
 result={}
 for kind,pid in payload['source_pids'].items():
  info=source._running(kind);need(info['State']['Pid']==pid,'source-original-pid-changed')
  result[kind]={'id':info['Id'],'pid':pid,'image':info['Image']}
 backend._resource();return result
def stopped_guard():
 row=dict(line.split('=',1) for line in command(['/usr/bin/systemctl','show',G.GUARD_UNIT,'--property=MainPID,ActiveState,Job,ControlGroup']).decode().splitlines())
 need(row['MainPID']=='0' and row['ActiveState']=='inactive' and not row['Job'] and row['ControlGroup'] in ('','/system.slice/'+G.GUARD_UNIT),'guard-not-stopped')
 path=pathlib.Path('/sys/fs/cgroup/system.slice')/G.GUARD_UNIT
 if path.exists():need(dict(line.split() for line in (path/'cgroup.events').read_text().splitlines()).get('populated')=='0','guard-cgroup-not-empty')
 return row
QUERY=b"""SET statement_timeout='1s'; SET lock_timeout='200ms';
SELECT json_build_object(
'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
'timeline',(SELECT timeline_id FROM pg_control_checkpoint()),'in_recovery',pg_is_in_recovery(),
'data_directory',current_setting('data_directory'),'wal_cap_bytes',pg_size_bytes(current_setting('max_slot_wal_keep_size')),
'role',(SELECT json_build_object('name',rolname,'comment',shobj_description(oid,'pg_authid'),'replication',rolreplication,'login',rolcanlogin,'superuser',rolsuper,'createdb',rolcreatedb,'createrole',rolcreaterole,'bypassrls',rolbypassrls) FROM pg_roles WHERE rolname='sub2api_mig_20261016_replica'),
'slots',COALESCE((SELECT json_agg(json_build_object('name',slot_name,'type',slot_type,'temporary',temporary,'active',active,'pid',active_pid,'wal_status',wal_status,'restart_lsn',restart_lsn::text,'retained_bytes',pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn)::bigint)) FROM pg_replication_slots WHERE slot_name='sub2api_mig_20261016'),'[]'::json),
'senders',COALESCE((SELECT json_agg(json_build_object('pid',pid,'user',usename,'application',application_name,'address',host(client_addr),'state',state)) FROM pg_stat_replication),'[]'::json),
'orphan_guard_sql',(SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND backend_type<>'walsender' AND (query LIKE '%sub2api_mig_20261016%' OR query LIKE '%GUARD_OWNERSHIP_CHANGED%' OR query LIKE '%pg_terminate_backend%' OR query LIKE '%pg_drop_replication_slot%')));
"""
def sql(query):
 with S.pg_sampling_lock():
  return command(['/usr/bin/docker','exec','-i',source.m['containers']['postgres']['id'],'psql','-X','-q','-A','-t','-w','-v','ON_ERROR_STOP=1','-U','sub2api','-d','sub2api'],query,timeout=2)
def snapshot(login):
 stopped_guard();resources();fixed_files()
 top=command(['/usr/bin/docker','top',source.m['containers']['postgres']['id'],'-eo','pid,comm'])
 need(not any(line.split()[-1:] == ['psql'] for line in top.decode().splitlines()[1:]),'lingering-container-psql')
 facts=S.strict_json(sql(QUERY).strip());backend._cluster(facts);role=backend._role(facts)
 need(role['login'] is login,'unexpected-role-login')
 slots=backend._slots(facts);need(len(slots)==1,'existing-slot-required-no-recreation')
 slot=slots[0]
 need(slot['active'] is True and slot['wal_status']=='reserved' and type(slot['pid']) is int and slot['pid']>0 and S.match(slot['restart_lsn'],S.LSN),'existing-slot-not-active-reserved')
 need(type(slot['retained_bytes']) is int and 0<=slot['retained_bytes']<G.RETAIN_LIMIT and type(facts['wal_cap_bytes']) is int and 0<=facts['wal_cap_bytes']<=G.WAL_CAP,'wal-not-bounded')
 st=os.statvfs(backend.config['pg_data_host_path']);need(st.f_bavail*st.f_frsize>=G.FREE_MIN,'disk-too-low')
 wanted={'pid':slot['pid'],'user':G.ROLE,'application':G.APPLICATION,'address':G.ADDRESS,'state':'streaming'}
 need(facts['senders']==[wanted] and facts['orphan_guard_sql']==0,'sender-changed-or-lingering-guard-sql')
 return facts
def stable_key(facts):
 return {'system_identifier':facts['system_identifier'],'timeline':facts['timeline'],'role':facts['role'],'slot':{k:facts['slots'][0][k] for k in ('name','type','temporary','active','pid','wal_status')},'senders':facts['senders']}
raw,nat=fixed_files();before=resources()
need(time.time()-payload['target_before']['checked_at']>=0 and time.time()-payload['target_before']['checked_at']<30 and payload['target_before']['ok'] is True,'target-proof-stale')
need(not saved.exists() and not saved.is_symlink(),'source-recovery-already-attempted')
saved.mkdir(mode=0o700);sync(scope)
stage='created'
def mark(value):
 global stage
 stage=value;atomic(saved/'progress.json',canonical({'stage':stage,'checked_at':time.time(),'owner':payload['owner']}))
try:
 write_new(saved/'target-before.json',canonical(payload['target_before']))
 mark('stop-old-guard-intent')
 command(['/usr/bin/systemctl','stop',G.GUARD_UNIT],timeout=5)
 stopped_guard();mark('old-guard-stopped')
 # Killing docker's host client does not prove the container psql/backend ended.
 # Wait for both, without terminating any session or reissuing fence SQL.
 deadline=time.monotonic()+4
 while True:
  try:first=snapshot(False);break
  except Exception:
   if time.monotonic()>=deadline:raise
   time.sleep(.2)
 time.sleep(.25);second=snapshot(False)
 need(stable_key(first)==stable_key(second),'slot-role-sender-not-stable')
 raw,nat=fixed_files();status=trusted(scope/'guard-status.json',0o600)
 status_value=json.loads(status)
 need(status_value.get('owner')==payload['owner'] and status_value.get('tripped') is True and status_value.get('pg_wal_protected') is True and status_value.get('recovering') is False,'stopped-status-unexpected')
 for name,data in raw.items():write_new(saved/name,data)
 write_new(saved/'guard-status.json',status);write_new(saved/'fence-absence.json',canonical({'missing':True}))
 write_new(saved/'nat-before.txt',nat)
 for name,data in oldraw.items():write_new(saved/('old-'+name),data)
 write_new(saved/'two-stable-snapshots.json',canonical({'first':first,'second':second,'resources':before}))
 need(trusted(scope/'guard-status.json',0o600)==status,'stopped-status-raced')
 stopped_guard();mark('restore-owned-login-intent')
 pg=source.m['postgres'];pid=second['slots'][0]['pid']
 # Only verified numeric/hex values are inserted; SQL names remain literals.
 need(re.fullmatch('[0-9]{1,20}',pg['system_identifier']) and type(pg['timeline']) is int and re.fullmatch('[0-9a-f]{64}',payload['role_comment_sha']),'invalid-sql-pin')
 restore=("SET statement_timeout='1s'; SET lock_timeout='200ms'; BEGIN; DO $recover$ BEGIN IF "
 "(SELECT system_identifier::text FROM pg_control_system()) <> '"+pg['system_identifier']+"' OR (SELECT timeline_id FROM pg_control_checkpoint()) <> "+str(pg['timeline'])+" OR pg_is_in_recovery() OR current_setting('data_directory') <> '/var/lib/postgresql/data' OR "
 "NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sub2api_mig_20261016_replica' AND NOT rolcanlogin AND rolreplication AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolbypassrls AND encode(sha256(convert_to(coalesce(shobj_description(oid,'pg_authid'),''),'UTF8')),'hex')='"+payload['role_comment_sha']+"') OR "
 "NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='sub2api_mig_20261016' AND slot_type='physical' AND NOT temporary AND active AND active_pid="+str(pid)+" AND wal_status='reserved') OR "
 "NOT EXISTS (SELECT 1 FROM pg_stat_replication WHERE pid="+str(pid)+" AND usename='sub2api_mig_20261016_replica' AND application_name='sub2api-pg-replica-20261016' AND host(client_addr)='172.18.0.1' AND state='streaming') "
 "THEN RAISE EXCEPTION 'RECOVERY_OWNERSHIP_CHANGED'; END IF; ALTER ROLE sub2api_mig_20261016_replica LOGIN; END $recover$; COMMIT;")
 sql(restore.encode());mark('owned-login-restored')
 restored=snapshot(True)
 expected=stable_key(second);expected['role']['login']=True
 need(stable_key(restored)==expected,'post-login-slot-or-sender-changed')
 write_new(saved/'login-verification.json',canonical(restored))
 # CAS-retire the original trip only after durable archive and exact restoration.
 need(trusted(scope/'guard-trip.json',0o600)==raw['guard-trip.json'] and trusted(saved/'guard-trip.json',0o600)==raw['guard-trip.json'],'trip-cas-failed')
 need(trusted(scope/'guard-status.json',0o600)==status,'status-cas-failed');stopped_guard()
 os.rename(scope/'guard-trip.json',saved/'retired-guard-trip.json');sync(scope);sync(saved)
 mark('trip-retired')
 for name,data in programs.items():
  need(trusted(lib/name,0o644)==oldraw[name],'old-program-cas-failed')
  atomic(lib/name,data,0o644)
 mark('all-five-source-programs-installed')
 need(all(sha(trusted(lib/name,0o644))==new[name] for name in new),'new-program-verification-failed')
 need(sha(trusted(scope/'state.json',0o600))==pins['state.json'] and resources()==before,'source-changed-before-new-guard')
 need(not (scope/'guard-trip.json').exists() and not (scope/'guard-fence.json').exists(),'trip-or-fence-reappeared')
 mark('start-fresh-guard-intent');command(['/usr/bin/systemctl','start',G.GUARD_UNIT],timeout=5);started_at=time.time()
 deadline=time.monotonic()+6
 while True:
  fresh=json.loads(trusted(scope/'guard-status.json',0o600));now=time.time()
  if fresh.get('owner')==payload['owner'] and fresh.get('boot_id')==backend.boot_id() and fresh.get('healthy') is True and fresh.get('tripped') is False and fresh.get('recovering') is False and fresh.get('pg_wal_protected') is True and fresh.get('sample',{}).get('mode')=='RUNNING_BOUNDED' and started_at<=fresh.get('checked_at',0)<=now and now-fresh['checked_at']<1:break
  need(time.monotonic()<deadline,'fresh-guard-not-healthy');time.sleep(.1)
 need(sha(trusted(scope/'state.json',0o600))==pins['state.json'] and resources()==before,'source-changed-after-new-guard')
 need(not (scope/'guard-trip.json').exists() and not (scope/'guard-fence.json').exists(),'new-guard-tripped')
 final=S.strict_json(sql(QUERY).strip());backend._cluster(final);backend._role(final);backend._slots(final)
 need(stable_key(final)==expected and final['orphan_guard_sql']==0,'final-role-slot-or-sender-changed')
 result={'ok':True,'checked_at':time.time(),'source_pids_unchanged':True,'state_unchanged':True,'guard_fresh_healthy':True,'programs':new,'archive':str(saved),'phase':'SOURCE_RECOVERED_TARGET_POSTCHECK_REQUIRED'}
 write_new(saved/'verification.json',canonical({'result':result,'guard':fresh,'pg':final}));mark('source-recovered-target-postcheck-required')
 print(json.dumps(result,sort_keys=True))
except BaseException as error:
 # Never restart the old tripped guard or erase evidence on any failure.
 write_new(saved/'failure.json',canonical({'stage':stage,'checked_at':time.time(),'exception_type':type(error).__name__,'inspection_required':True}))
 raise
'''


def reviewed_payload():
    folder = pathlib.Path(__file__).resolve().parent
    reviewed = runpy.run_path(str(folder / "upgrade_control_verified.py"))
    old, new = reviewed["OLD"], reviewed["NEW"]
    programs = {name: (folder / name).read_bytes() for name in new}
    if not all(hashlib.sha256(raw).hexdigest() == new[name] for name, raw in programs.items()):
        raise RuntimeError("reviewed-local-program-digest-changed")
    return {"old": old, "new": new, "programs": {name: base64.b64encode(raw).decode() for name, raw in programs.items()},
            "owner": OWNER, "pins": PINS, "state": STATE, "role_comment_sha": ROLE_COMMENT_SHA,
            "source_pids": SOURCE_PIDS, "target_pids": TARGET_PIDS, "target_config_sha": TARGET_CONFIG_SHA}


def journal_write(folder, name, value):
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode() + b"\n"
    fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def remote(argv, code, payload, folder, label):
    journal_write(folder, label + "-intent.json", {"checked_at": time.time(), "code_sha256": hashlib.sha256(code.encode()).hexdigest()})
    try:
        result = subprocess.run(argv + ["sudo -n python3 -I -c " + shlex.quote(code)],
                                input=json.dumps(payload).encode(), capture_output=True, timeout=55)
    except BaseException as error:
        journal_write(folder, label + "-uncertain.json", {"exception_type": type(error).__name__, "inspect_before_any_retry": True})
        raise
    journal_write(folder, label + "-response.json", {"exit": result.returncode, "stdout": result.stdout.decode(errors="replace"),
                                                   "stderr": result.stderr.decode(errors="replace")[-8192:]})
    if result.returncode:
        raise RuntimeError(label + "-failed-inspect-private-evidence-no-retry")
    value = json.loads(result.stdout)
    if value.get("ok") is not True:
        raise RuntimeError(label + "-verification-failed")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = reviewed_payload()
    for label, script in (("source", SOURCE_RECOVERY), ("target-read-only", TARGET_READ)):
        compile(COMMON + script, "guard-recovery-" + label, "exec")
    if not args.apply:
        print(json.dumps({"offline": True, "source_only_recovery": True, "target_read_only": True,
                          "reviewed_programs": payload["new"], "source_pids": SOURCE_PIDS,
                          "no_automatic_retry": True, "customer_route_changes": False}, sort_keys=True))
        return
    parent = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
    if any(p.is_symlink() for p in [parent, *parent.parents]):
        raise RuntimeError("local-evidence-symlink-refused")
    # mkdir and O_EXCL intent precede every SSH call, including target reads.
    folder = parent / "guard-trip-recovery-20261017"
    folder.mkdir(mode=0o700)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if stat.S_IMODE(folder.stat().st_mode) != 0o700:
        raise RuntimeError("local-evidence-not-private")
    journal_write(folder, "intent.json", {"created_at": time.time(), "source_only": True,
                  "programs": payload["new"], "pins": PINS, "no_automatic_retry": True})
    channel = runpy.run_path(str(pathlib.Path(__file__).parent / "prepare_channel.py"))
    before = remote(channel["TARGET"], COMMON + TARGET_READ, payload, folder, "target-before")
    payload["target_before"] = before
    source = remote(channel["SSH_SOURCE"], COMMON + SOURCE_RECOVERY, payload, folder, "source-recovery")
    after = remote(channel["TARGET"], COMMON + TARGET_READ, payload, folder, "target-after")
    if any(before[key] != after[key] for key in ("pids", "ledger_sha256", "system_identifier", "timeline", "receiver", "config_sha256")):
        raise RuntimeError("target-postcheck-changed-inspect-evidence")
    journal_write(folder, "complete.json", {"ok": True, "source": source, "target": after})
    print(json.dumps({"ok": True, "source_guard_recovered": True, "target_unchanged": True,
                      "evidence": str(folder), "customer_route_changes": False}, sort_keys=True))


if __name__ == "__main__":
    main()
