#!/usr/bin/env python3
"""Explicitly retire the investigated guard trip; retain the replica fence.

No source database restart, LOGIN, slot creation, application signal or request
pause. The default is offline. The original trip/fence and every ledger remain
archived; any uncertain result requires inspection rather than replay.
"""
import argparse
import base64
import hashlib
import json
import pathlib
import runpy
import shlex
import subprocess

OLD_SHA = 'e95be2c5e3bb01c842708c026c000b617aeca0ac26a049260a7d0010ddb8f200'
NEW_SHA = '72cb81b27bcf8c4f316511ca0460f44af453ab51276d53cda82ba55e2cee8d05'
TRIP_SHA = '15a6d71270e81014045d4db6a9ce68767806cb48c0a4ae9bf91f6480955eab80'
FENCE_SHA = '64f690b49b92e6be6434efedea2a1387e7f15d1f4e13909e15865c299a497790'

SOURCE = r'''
scope=root/'runner';saved=scope/'guard-rearm-reseed-20261017'
program=lib/'runner_guard.py'
need(sha(trusted(program,0o644))==payload['old_sha'],'old-guard-changed')
raw=base64.b64decode(payload['program'],validate=True)
need(sha(raw)==payload['new_sha'],'new-guard-changed');compile(raw,str(program),'exec')
M=load('rearm_source_service',lib/'runner_service.py')
G=load('rearm_old_guard',program);g=G.LocalBackend();b=M.B.RunnerBackend()
with M.execution_lock():
 state=b.store.read('state');before=trusted(scope/'state.json',0o600)
 need(state['owner']=='30bda011defad8149621c2630a2bd89c' and state['attempt']==3 and
      state['phase']=='SOURCE_DATA' and state['pg']==state['redis']=='AWS' and
      state['window'] is None and state.get('deadline_breached') is False and
      state['pause_seconds']==14.656846920028329 and g.main_status()['quiescent'],
      'exact-recovered-source-boundary-required')
 M.verify_entry();M.read_gates(state['owner'])
 tripraw=trusted(scope/'guard-trip.json',0o600);fenceraw=trusted(scope/'guard-fence.json',0o600)
 need(sha(tripraw)==payload['trip_sha'] and sha(fenceraw)==payload['fence_sha'],'investigated-trip-or-fence-bytes-changed')
 trip=json.loads(tripraw);fence=json.loads(fenceraw)
 need(trip['owner']==fence['owner']==state['owner'] and trip['reason']=='LOCAL_PROBE_FAILED' and
      trip['run']==fence['run']=='20261016' and trip['boot_id']==fence['boot_id']==g.boot_id() and
      fence['fenced'] is True and fence['checked_at']>=trip['checked_at'],'investigated-fence-required')
 for kind,pid in [('app',0),('postgres',972839),('redis',972827)]:
  info=b.source_ops._resource(kind);s=info['State']
  need(s['Pid']==pid and s['Running']==(kind!='app') and not s['OOMKilled'],'source-resource-changed')
  if kind=='app':need(s['ExitCode']==0 and info['HostConfig']['RestartPolicy']['Name']=='no','source-app-not-retired')
 need(not saved.exists() and not saved.is_symlink(),'rearm-already-attempted-inspect-only')
 saved.mkdir(mode=0o700);sync(scope)
 write_new(saved/'intent.json',canonical({'at':time.time(),'state_raw_sha256':sha(before),
    'old_sha':payload['old_sha'],'new_sha':payload['new_sha'],'keep_fence':True}))
 command(['systemctl','stop',G.GUARD_UNIT],timeout=5)
 def stopped():
  v=dict(line.split('=',1) for line in command(['systemctl','show',G.GUARD_UNIT,
    '--property=MainPID,Job,ActiveState,ControlGroup']).decode().splitlines())
  need(v['MainPID']=='0' and not v['Job'] and v['ActiveState']=='inactive','old-guard-still-running')
  cg=pathlib.Path('/sys/fs/cgroup/system.slice')/G.GUARD_UNIT
  if cg.exists():need(dict(line.split() for line in (cg/'cgroup.events').read_text().splitlines()).get('populated')=='0','old-guard-cgroup-not-empty')
 stopped()
 # A killed host docker client need not kill its in-container psql. Every old
 # guard SQL has a statement deadline; wait for its actual process to vanish.
 end=time.monotonic()+5
 while True:
  top=command(['docker','top',b.source_ops.m['containers']['postgres']['id'],'-eo','pid,comm'])
  if not any(line.split()[-1:]==['psql'] for line in top.decode().splitlines()[1:]):break
  need(time.monotonic()<end,'old-container-psql-still-present');time.sleep(.1)
 sample=G.S.strict_json(g._sql(G.PG_SAMPLE).strip());g._cluster(sample);role=g._role(sample)
 need(role['login'] is False and g._slots(sample)==[],'fenced-role-or-slot-changed')
 need(g.sample()['mode']=='OWNED_REPLICA_FENCED','owned-fence-not-valid')
 files=['state.json','transport-state.json','source-operations.json','guard-trip.json',
        'guard-fence.json','guard-status.json','guard-config.json','source-manifest.json']
 frozen={name:trusted(scope/name,0o600) for name in files}
 need(frozen['state.json']==before and frozen['guard-trip.json']==tripraw and frozen['guard-fence.json']==fenceraw,
      'source-state-or-fence-CAS-changed')
 for name,value in frozen.items():write_new(saved/name,value)
 write_new(saved/'old-runner_guard.py',trusted(program,0o644),0o644)
 write_new(saved/'fenced-sample.json',canonical(sample))
 need(sha(trusted(program,0o644))==payload['old_sha'] and
      trusted(scope/'guard-trip.json',0o600)==tripraw and
      trusted(scope/'guard-fence.json',0o600)==fenceraw,'pre-install-CAS-changed')
 atomic(program,raw,0o644)
 need(trusted(scope/'guard-trip.json',0o600)==tripraw and
      trusted(scope/'guard-fence.json',0o600)==fenceraw,'trip-or-fence-CAS-changed')
 os.rename(scope/'guard-trip.json',saved/'retired-guard-trip.json');sync(scope);sync(saved)
 command(['systemctl','start',G.GUARD_UNIT],timeout=5)
 start=time.time();end=time.monotonic()+8;accepted=[];last_checked=0
 while time.monotonic()<end:
  status=json.loads(trusted(scope/'guard-status.json',0o600))
  checked=status.get('checked_at',0)
  if checked>=start and checked>last_checked and 0<=time.time()-checked<=1 and status.get('owner')==state['owner'] and status.get('run')=='20261016' and status.get('boot_id')==g.boot_id() and status.get('healthy') is True and status.get('pg_wal_protected') is True and not status.get('tripped') and not status.get('recovering') and status.get('sample',{}).get('mode')=='OWNED_REPLICA_FENCED':
   accepted.append(status)
   last_checked=checked
   if len(accepted)>=3:break
  time.sleep(.2)
 need(len(accepted)>=3,'new-guard-not-healthy-inspect-only')
 unit=dict(line.split('=',1) for line in command(['systemctl','show',G.GUARD_UNIT,
    '--property=MainPID,ActiveState,SubState,Job']).decode().splitlines())
 need(unit['ActiveState']=='active' and unit['SubState']=='running' and unit['MainPID'].isdigit() and
      int(unit['MainPID'])>1 and not unit['Job'] and sha(trusted(program,0o644))==payload['new_sha'],
      'new-guard-process-or-program-not-proven')
 need(all(trusted(scope/name,0o600)==frozen[name] for name in ('state.json','transport-state.json','source-operations.json','guard-fence.json')),
      'ledgers-or-retained-fence-changed')
 need(not (scope/'guard-trip.json').exists() and g.main_status()['quiescent'],'new-trip-or-main-started')
 result={'ok':True,'guard_sha256':sha(trusted(program,0o644)),'new_guard_healthy':True,
         'mode':'OWNED_REPLICA_FENCED','old_trip_archived':True,'fence_retained':True,
         'state_raw_sha256':sha(before),'replication_rearmed':False,'ledgers_preserved':True,
         'source_database_restarted':False,'request_pause':False,'at':time.time()}
 write_new(saved/'verification.json',canonical(result));print(json.dumps(result,sort_keys=True))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({'offline': True, 'fence_retained': True, 'replication_rearmed': False,
                          'old_guard_sha256': OLD_SHA, 'new_guard_sha256': NEW_SHA}))
        return
    folder = pathlib.Path(__file__).parent
    old = runpy.run_path(str(folder/'recover_source_guard.py'))
    channel = runpy.run_path(str(folder/'prepare_channel.py'))
    raw = (folder/'runner_guard.py').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == NEW_SHA, 'reviewed-guard-changed'
    payload = {'old_sha': OLD_SHA, 'new_sha': NEW_SHA, 'trip_sha': TRIP_SHA,
               'fence_sha': FENCE_SHA, 'program': base64.b64encode(raw).decode()}
    script = old['COMMON'] + SOURCE
    compile(script, 'guard-rearm-reseed', 'exec')
    prior = runpy.run_path(str(folder/'prepare_retry.py'))
    path = prior['FOLDER']/'guard-rearm-reseed-20261017.json'
    assert not path.exists() and not path.with_suffix('.intent.json').exists(), 'existing-rearm-inspect-only'
    prior['private_json'](path.with_suffix('.intent.json'), {'script_sha256': hashlib.sha256(script.encode()).hexdigest(),
                                                          'inspect_before_retry': True})
    result = subprocess.run(channel['SSH_SOURCE']+['sudo python3 -I -c '+shlex.quote(script)],
                            input=json.dumps(payload).encode(), capture_output=True, timeout=35)
    value = prior['private_result'](path, result)
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    main()
