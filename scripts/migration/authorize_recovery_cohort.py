#!/usr/bin/env python3
"""Append a budget cohort after the old application generation has exited.

The lifetime pause history and all operation ledgers remain intact. Default is
offline. This does not pause requests or start the migration controller.
"""
import argparse
import hashlib
import json
import pathlib
import runpy
import secrets
import shlex
import subprocess

RPC_SHA = '8180879f75169f130b726536ee16957832a9d2bf9e193f93ad380ebd8264f6ec'
OWNER = '30bda011defad8149621c2630a2bd89c'
STATE_SHA = 'd11bc0b90d6118d09ec6a2a215acb9c9a1bfdd488e929b6edb85933a9cf21d0e'
LEDGER_SHA = 'abd784df64eaf0fb3d5d622f8025a221657078b731cf97d9859cee3ba8991ce3'

VALIDATION = r'''
def fresh_proof(p,now):
 need(p.get('v')==1 and p.get('run')=='20261016' and p.get('owner')==payload['owner'] and
      type(p.get('checked_at')) in (int,float) and 0<=now-p['checked_at']<=20 and
      p.get('old_generation_clean_exit') is True and p.get('old_generation_all_connections_gone') is True and
      p.get('new_generation_born_after_closed_window') is True and p.get('new_generation_idle') is True and
      p.get('new_generation_ready') is True and p.get('aws_authority') is True and
      p.get('ledger_sha256')==payload['ledger_sha'] and p.get('ledger_epoch')==14 and
      p.get('new_instance_id')!=p.get('old_instance_id') and
      type(p.get('old_instance_id')) is str and bool(p['old_instance_id']) and
      type(p.get('new_instance_id')) is str and bool(p['new_instance_id']),
      'fresh-drained-generation-proof-required')
'''

TARGET = r'''
need(sha(trusted(lib/'target_rpc.py',0o700))==payload['rpc_sha'],'target-RPC-changed')
R=load('cohort_target_rpc',lib/'target_rpc.py');cfg=R.load_config();b=R.LocalBackend(cfg);store=R.FileStore(cfg)
scope=root/'rolling-recovery'
with store.mutation_lock():
 raw=trusted(root/'rpc/state.json',0o600);ledger=store.read()
 need(sha(raw)==payload['ledger_sha'] and ledger['epoch']==14 and
      ledger['pg']['authority']==ledger['redis']['authority']=='AWS' and
      ledger['pg'].get('promotion_intent') is None and
      (not ledger['pg'].get('arm') or ledger['pg']['arm']['status']=='CANCELLED') and
      all(op['phase']=='COMPLETE' for op in ledger['operations'].values()) and
      all(op.get('cancelled') is True for op in ledger['operations'].values()
          if op['request']['action']=='app.quiesce'),'original-recovered-target-ledger-required')
 receipt_raw=trusted(scope/'controller-adoption.json',0o600);adopt=json.loads(receipt_raw)
 rolling_raw=trusted(scope/'state.json',0o600);rolling=json.loads(rolling_raw)
 closed_raw=trusted(scope/'source-controller-closed.json',0o600);closed=json.loads(closed_raw)
 need(adopt['owner']==payload['owner'] and adopt['run']=='20261016' and
      adopt['rolling_complete_sha256']==sha(rolling_raw) and
      adopt['source_closed_sha256']==sha(closed_raw) and
      adopt['ledger_raw_sha256']==sha(raw) and
      adopt['controller_config_raw_sha256']==sha(trusted(root/'rpc/config.json',0o600)) and
      adopt['new_container_id']==cfg['app']['container_id'] and
      adopt['new_app_config_sha256']==cfg['app']['config_sha256'] and
      cfg['app']['control_port']==18093 and
      rolling['phase']=='COMPLETE' and rolling['intent'] is None and
      rolling['adoption_receipt']['old_exited_cleanly'] is True,
      'complete-controller-adoption-required')
 old=b.docker_json('container',adopt['old_container_id']);new=b.resource('app')
 need(old['State']['Running'] is False and old['State']['Pid']==0 and
      old['State']['Status']=='exited' and old['State']['ExitCode']==0 and
      old['State']['OOMKilled'] is False and old['HostConfig']['RestartPolicy']['Name']=='no',
      'old-generation-not-clean-retired')
 import datetime
 born=datetime.datetime.fromisoformat(new['State']['StartedAt'].replace('Z','+00:00')).timestamp()
 need(closed['window'] is None and closed['controller_stopped'] is True and
      closed['pg']==closed['redis']=='AWS' and born>closed['checked_at'],
      'new-generation-not-born-after-closed-window')
 read=b.app_session()
 def status(name):
  row=read(name+'.status');need(row['http_status']==200,'control-read-failed');return row['body']
 end=time.monotonic()+20
 while True:
  started=time.time();before=status('ingress');rt=status('runtime');after=status('ingress')
  if rt.get('active_requests')==rt.get('active_hijacked_connections')==0 and before==after==rt.get('ingress') and before.get('queued_requests')==0:break
  need(time.monotonic()<end,'new-generation-not-observed-idle');time.sleep(.2)
 need(rt['instance_id']==adopt['new_instance_id'] and rt['instance_id']!=adopt['old_instance_id'] and
      rt['ready'] is True and rt['serving'] is True and rt['draining'] is False and
      rt['background_workers_enabled'] is False and before['paused'] is False and
      R.REQUIRED_WRITERS<=rt['tasks'].keys() and
      all(row.get('failed')==row.get('dropped')==0 for row in rt['tasks'].values()),
      'new-generation-not-clean-and-ready')
 need(b.endpoint_state()=={'v':1,'postgres':'source','redis':'source'},'persistent-endpoints-changed')
 pg,redis=status('postgres'),status('redis')
 need(pg['endpoint']=='source' and pg['paused'] is False and
      redis['address']=='127.0.0.1:16379' and redis['paused'] is False and
      redis['pubsub_cache_ready'] is True and status('writers')['held'] is False,
      'new-generation-controls-not-open-on-AWS')
 actual=b.pg_sample();replica=b.redis_inspect()
 need(actual['in_recovery'] is True and actual['system_identifier']=='7626048206782038050' and
      actual['source_timeline']==1 and replica['role']=='slave' and
      replica['run_id']==R.REDIS_TARGET_RUN_ID,'actual-target-replicas-changed')
 H=load('cohort_route_parser',lib/'rolling_app_host.py')
 nat=H.parse_nat(command(['iptables-save','-t','nat']),payload['rule'])
 need(nat['output'] and nat['output'][0]==payload['rule'] and
      sum(row==payload['rule'] for row in nat['output'])==1,'new-app-route-not-unique-and-first')
 need(trusted(root/'rpc/state.json',0o600)==raw,'target-ledger-raced')
 proof={'v':1,'run':'20261016','owner':payload['owner'],'checked_at':started,
   'old_generation_clean_exit':True,'old_generation_all_connections_gone':True,
   'new_generation_born_after_closed_window':True,'new_generation_idle':True,
   'new_generation_ready':True,'aws_authority':True,'ledger_sha256':sha(raw),'ledger_epoch':14,
   'old_instance_id':adopt['old_instance_id'],'new_instance_id':rt['instance_id'],
   'old_container_id':adopt['old_container_id'],'new_container_id':new['Id'],
   'adoption_sha256':sha(receipt_raw),'rolling_complete_sha256':sha(rolling_raw),
   'runtime':rt,'ingress_before':before,'ingress_after':after,'new_started_at':new['State']['StartedAt']}
 fresh_proof(proof,time.time());print(json.dumps(proof,sort_keys=True))
'''

SOURCE = r'''
scope=root/'runner';saved=scope/'recovery-cohort-20261017'
M=load('cohort_source_service',lib/'runner_service.py')
G=load('cohort_guard',lib/'runner_guard.py')
with M.execution_lock():
 b=M.B.RunnerBackend();state=b.store.read('state');raw=trusted(scope/'state.json',0o600)
 need(sha(raw)==payload['state_sha'] and state.get('v')==1 and state.get('run')=='20261016' and
      state.get('owner')==payload['owner'] and state['attempt']==3 and state['phase']=='SOURCE_DATA' and
      state['pg']==state['redis']=='AWS' and state['window'] is None and
      state['pause_seconds']==14.656846920028329 and state.get('deadline_breached') is False,
      'exact-recovered-source-boundary-required')
 need(sha(trusted(lib/'target_rpc.py',0o644))==payload['rpc_sha'],'source-RPC-mirror-changed')
 need(G.LocalBackend().main_status()['quiescent'] and not M.selftest_armed(payload['owner']),
      'source-main-not-quiescent')
 need(not (scope/'guard-trip.json').exists() and not (scope/'guard-fence.json').exists(),
      'guard-trip-or-fence-present')
 guard=b.source_ops._guard()
 need(guard['tripped'] is False and guard['recovering'] is False,'source-guard-not-stable')
 M.verify_entry();M.read_gates(payload['owner']);b.source_ops._assert_monitors_retired()
 need(not any(op['request']['action'] in ('pg_stop','redis_failover','redis_role_persist')
              for op in b.source_ops._ledger()['operations'].values()),
      'source-data-stop-already-intended')
 for kind,pid in [('app',0),('postgres',972839),('redis',972827)]:
  row=b.source_ops._resource(kind)
  need(row['State']['Pid']==pid and row['State']['Running']==(kind!='app') and
       row['State']['OOMKilled'] is False,'source-business-resource-changed')
 fresh_proof(payload['proof'],time.time())
 need(payload['proof']['old_instance_id']==state['quiet_instance_id'],
      'old-generation-does-not-match-prior-cohort')
 frozen={name:trusted(scope/name,0o600) for name in ('transport-state.json','source-operations.json')}
 need(not saved.exists() and not saved.is_symlink(),'cohort-already-attempted-inspect-only')
 saved.mkdir(mode=0o700);sync(scope)
 write_new(saved/'previous-state.json',raw)
 proofraw=canonical(payload['proof']);write_new(saved/'drain-proof.json',proofraw)
 identifier=payload['cohort_id'];need(re.fullmatch('[0-9a-f]{32}',identifier),'invalid-cohort-id')
 directory=scope/'budget-cohorts';M.S.trusted_parent(directory,private=True)
 record={'v':1,'run':'20261016','owner':payload['owner'],'id':identifier,
    'previous_state':state,'previous_state_sha256':M.E.digest(state),
    'proof_sha256':sha(proofraw)}
 recordraw=canonical(record);write_new(directory/(identifier+'.json'),recordraw)
 updated=json.loads(json.dumps(state));updated['budget_cohort']={'id':identifier,'record_sha256':sha(recordraw)}
 oldbudget=M.E.budget_accounting(state);budget=M.E.budget_accounting(updated)
 need(budget['total_pause_seconds']==oldbudget['total_pause_seconds'] and
      budget['max_cohort_pause_seconds']==oldbudget['max_cohort_pause_seconds'] and
      budget['cohort_pause_seconds']==0,'lifetime-budget-not-preserved')
 with b.store.lock():
  need(trusted(scope/'state.json',0o600)==raw,'source-state-CAS-changed')
  fresh_proof(payload['proof'],time.time());b.store._write('state',updated)
 need(all(trusted(scope/name,0o600)==value for name,value in frozen.items()),'operation-ledger-changed')
 result={'ok':True,'cohort_id':identifier,'budget':budget,'quiet_instance_id_preserved':True,
   'lifetime_pause_seconds_preserved':True,'operation_ledgers_preserved':True,
   'request_pause':False,'main_started':False,'at':time.time()}
 write_new(saved/'verification.json',canonical(result));print(json.dumps(result,sort_keys=True))
'''


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    if not p.parse_args(argv).apply:
        print(json.dumps({'offline': True, 'lifetime_history_preserved': True,
                          'requires_retired_old_generation': True, 'main_started': False}))
        return
    folder = pathlib.Path(__file__).parent
    need_sha = hashlib.sha256((folder/'target_rpc.py').read_bytes()).hexdigest()
    if need_sha != RPC_SHA:
        raise RuntimeError('reviewed-RPC-changed')
    c = runpy.run_path(str(folder/'prepare_channel.py'))
    prior = runpy.run_path(str(folder/'prepare_retry.py'))
    common = runpy.run_path(str(folder/'recover_source_guard.py'))['COMMON']
    core = runpy.run_path(str(folder/'rolling_app_recovery.py'))
    payload = {'owner': OWNER, 'state_sha': STATE_SHA, 'ledger_sha': LEDGER_SHA,
               'rpc_sha': RPC_SHA, 'cohort_id': secrets.token_hex(16), 'rule': core['REDIRECT']}
    for label, script, host in [('target', TARGET, c['TARGET']), ('source', SOURCE, c['SSH_SOURCE'])]:
        path = prior['FOLDER']/('recovery-cohort-'+label+'-20261017.json')
        if path.exists() or path.with_suffix('.intent.json').exists():
            raise RuntimeError('existing-cohort-result-inspect-only')
        code = common + VALIDATION + script
        compile(code, label, 'exec')
        prior['private_json'](path.with_suffix('.intent.json'), {'script_sha256': hashlib.sha256(code.encode()).hexdigest(),
                                                              'inspect_before_retry': True})
        r = subprocess.run(host+['sudo python3 -I -c '+shlex.quote(code)], input=json.dumps(payload).encode(),
                           capture_output=True, timeout=45)
        result = prior['private_result'](path, r)
        if label == 'target':
            payload['proof'] = result
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
