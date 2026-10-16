#!/usr/bin/env python3
"""Resolve one fixed UNKNOWN start only after proving it never took effect.

Default is offline. --apply creates an independent, non-replayable resolution
intent and sends at most one start to the fixed immutable candidate ID. The old
rolling intent and all ledgers remain byte-for-byte unchanged. Root subsequently
reconciles that original intent through the existing core's inspection path.
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

CID = "17e3bfacbd442beff52427e003e58f4970e613203356b8bb8bde0e98b9229bce"
INTENT_ID = "d48c476b63579a1ec38d0142430c96de"
LABEL = "inert-start-resolution-20261017"
HOST_SHA = "6b268f762c16e7f2274a069cc0d6ba059b2024d0be4633dd05806de26daf7217"
CORE_SHA = "14b21455e754109b71e649e69a4adf3fd2e251e54f490ebf187a20c0442bc39f"
LEDGER_SHA = "abd784df64eaf0fb3d5d622f8025a221657078b731cf97d9859cee3ba8991ce3"

HELPERS = r'''
def never_started(row):
 s=row.get('State',{})
 zero=lambda value:type(value) is str and re.fullmatch(r'0001-01-01T00:00:00(?:\.0+)?Z',value) is not None
 need(row.get('Id')==payload['cid'] and type(row.get('RestartCount')) is int and row['RestartCount']==0 and
      s.get('Status')=='created' and s.get('Running') is False and type(s.get('Pid')) is int and s['Pid']==0 and
      zero(s.get('StartedAt')) and zero(s.get('FinishedAt')) and s.get('Error')=='' and
      type(s.get('ExitCode')) is int and s['ExitCode']==0 and
      all(s.get(k) is False for k in ('Paused','Restarting','Dead','OOMKilled')),
      'candidate-not-proven-never-started')
 return {key:s[key] for key in ('Status','Running','Pid','StartedAt','FinishedAt','Error','ExitCode',
                              'Paused','Restarting','Dead','OOMKilled')}
def runtime_clients(proc,cid,name):
 found=[]
 for entry in pathlib.Path(proc).iterdir():
  if not entry.name.isdecimal():continue
  try:raw=(entry/'cmdline').read_bytes()
  except (FileNotFoundError,ProcessLookupError):continue
  if not raw:continue
  argv=[os.fsdecode(part) for part in raw.rstrip(b'\0').split(b'\0')]
  binary=pathlib.PurePosixPath(argv[0]).name
  if not (binary in ('docker','docker.io','docker-runc','runc','ctr','nerdctl') or
          binary.startswith(('containerd-shim','docker-containerd-shim'))):continue
  joined='\0'.join(argv)
  # Docker accepts names and abbreviated IDs; runtime shims commonly receive
  # --id=<full ID> or a bundle path containing it. Never expose these argv.
  targeted=cid in joined or name in joined or any(
    re.fullmatch(r'[0-9a-f]{4,63}',part) and cid.startswith(part) for part in argv[1:])
  if targeted:found.append({'pid':int(entry.name),'binary':binary,'argv_sha256':sha(raw)})
 return found
'''

TARGET = r'''
scope=root/'rolling-recovery';saved=scope/payload['label'];pin=scope/'new-process.json'
need(sha(trusted(lib/'rolling_app_host.py',0o700))==payload['host_sha'] and
     sha(trusted(lib/'rolling_app_recovery.py',0o700))==payload['core_sha'],'reviewed-rolling-program-changed')
H=load('inert_resolution_host',lib/'rolling_app_host.py');C,h,cfg=H.load_host()
store=C.FileStore(scope/'state.json',uid=0)
def original_boundary(state):
 need(state.get('v')==1 and state.get('owner')==cfg['owner'] and state.get('phase')=='START_INTENT' and
      state.get('intent')=={'id':payload['intent_id'],'kind':'start','status':'UNKNOWN'} and
      state.get('new_identity',{}).get('container_id')==payload['cid'],
      'exact-owned-unknown-start-required')
def observe_inert(state):
 original_boundary(state)
 need(not pin.exists() and not pin.is_symlink(),'new-process-receipt-already-present')
 o=C.Recovery(h,None,cfg['owner'])._observe(state)
 C.Recovery(h,None,cfg['owner'])._new(state,o['new'])
 need(o['old']['state']=='running' and o['new']['state']=='created','old-not-running-or-new-not-created')
 row=h._docker('container',payload['cid']);lifecycle=never_started(row)
 need(h.r.sha(row['Config'])==state['new_identity']['config_sha256'] and
      row['Image']==state['new_identity']['image_id'],'inert-container-identity-changed')
 need(not runtime_clients(pathlib.Path('/proc'),payload['cid'],C.NEW_NAME),'candidate-runtime-operation-in-flight')
 return row,{'container_id':row['Id'],'config_sha256':h.r.sha(row['Config']),
             'image_id':row['Image'],'restart_count':row['RestartCount'],'lifecycle':lifecycle}
with store.locked():
 state=store.read();original_boundary(state)
 state_raw=trusted(scope/'state.json',0o600);ledger_raw=trusted(root/'rpc/state.json',0o600)
 need(sha(ledger_raw)==payload['ledger_sha'],'original-target-ledger-changed')
 need(not saved.exists() and not saved.is_symlink(),'inert-resolution-already-attempted-inspect-only')
 samples=[];baseline=None
 for i in range(3):
  row,proof=observe_inert(state)
  need(trusted(scope/'state.json',0o600)==state_raw and trusted(root/'rpc/state.json',0o600)==ledger_raw,
       'original-checkpoints-changed')
  need(baseline is None or canonical(proof)==canonical(baseline),'inert-candidate-not-stable')
  baseline=proof;samples.append({'checked_at':time.time(),'proof':proof})
  if i<2:time.sleep(.2)
 need(not saved.exists() and not saved.is_symlink(),'inert-resolution-already-attempted-inspect-only')
 saved.mkdir(mode=0o700);sync(scope)
 try:
  write_new(saved/'rolling-state-before.json',state_raw)
  write_new(saved/'target-ledger-before.json',ledger_raw)
  write_new(saved/'candidate-before.json',canonical(row))
  write_new(saved/'stable-never-started-proof.json',canonical(samples))
  intent={'v':1,'owner':cfg['owner'],'run':'20261016','kind':'root-inert-start-resolution',
          'original_intent_id':payload['intent_id'],'container_id':payload['cid'],
          'original_state_raw_sha256':sha(state_raw),'original_ledger_raw_sha256':sha(ledger_raw),
          'resolution_id':__import__('secrets').token_hex(16),'at':time.time(),'status':'INTENT'}
  write_new(saved/'intent.json',canonical(intent))
  # This authority is the independent root resolution, never a status rewrite
  # or replay of the original UNKNOWN intent. Recheck after fsync, before start.
  _,last=observe_inert(state)
  need(canonical(last)==canonical(baseline) and trusted(scope/'state.json',0o600)==state_raw and
       trusted(root/'rpc/state.json',0o600)==ledger_raw,'resolution-pre-start-CAS-changed')
  argv=['/usr/bin/docker','start',payload['cid']]
  try:
   result=subprocess.run(argv,capture_output=True,timeout=20,env=H.SAFE_ENV)
  except subprocess.TimeoutExpired as error:
   write_new(saved/'docker-start.stdout',error.stdout or b'')
   write_new(saved/'docker-start.stderr',error.stderr or b'')
   raise
  write_new(saved/'docker-start.stdout',result.stdout)
  write_new(saved/'docker-start.stderr',result.stderr)
  write_new(saved/'docker-start.result.json',canonical({'returncode':result.returncode}))
  need(result.returncode==0,'docker-start-outcome-unknown-inspect-private-stderr')
  started=h._docker('container',payload['cid'])
  need(started['Id']==payload['cid'] and started['Image']==row['Image'] and started['Config']==row['Config'] and
       started['State']['Running'] is True and started['State']['Status']=='running',
       'started-process-unconfirmed-inspect-only')
  process=h._process(started);need(type(process['pid']) is int and process['pid']>1,'started-PID-unconfirmed')
  H.write_new(pin,C.canonical(process)+b'\n')
  checked=h._docker('container',payload['cid'])
  need(h._process(checked)==process and checked['State']['Running'] is True,'new-process-changed-after-receipt')
  h._safety()
  need(trusted(scope/'state.json',0o600)==state_raw and trusted(root/'rpc/state.json',0o600)==ledger_raw,
       'resolution-changed-original-checkpoints')
  result={'ok':True,'container_id':payload['cid'],'resolution_id':intent['resolution_id'],
          'original_intent_id':payload['intent_id'],'single_start_accepted':True,
          'new_process_receipt_raw_sha256':sha(trusted(pin,0o600)),
          'rolling_state_raw_sha256':sha(state_raw),'target_ledger_raw_sha256':sha(ledger_raw),
          'original_state_preserved':True,'original_ledger_preserved':True,
          'reconciled_original_intent':False,'routes_changed':False,'checked_at':time.time()}
  write_new(saved/'verification.json',canonical(result));print(json.dumps(result,sort_keys=True))
 except Exception:
  # Keep the exact original traceback privately, including the first cause.
  write_new(saved/'failure.traceback.txt',__import__('traceback').format_exc().encode())
  raise
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({'offline': True, 'container_id': CID, 'original_intent_id': INTENT_ID,
                          'original_intent_replayed': False, 'rolling_state_changed': False,
                          'requires_three_stable_never_started_samples': True}))
        return 0
    folder = pathlib.Path(__file__).parent
    for name, digest in [('rolling_app_host.py', HOST_SHA), ('rolling_app_recovery.py', CORE_SHA)]:
        if hashlib.sha256((folder/name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('reviewed-rolling-program-changed')
    prior = runpy.run_path(str(folder/'prepare_retry.py'))
    channel = runpy.run_path(str(folder/'prepare_channel.py'))
    common = runpy.run_path(str(folder/'recover_source_guard.py'))['COMMON']
    script = common+HELPERS+TARGET
    compile(script, 'complete-inert-recovery-start', 'exec')
    payload = {'cid': CID, 'intent_id': INTENT_ID, 'label': LABEL, 'host_sha': HOST_SHA,
               'core_sha': CORE_SHA, 'ledger_sha': LEDGER_SHA}
    path = prior['FOLDER']/(LABEL+'.json')
    intent_path = path.with_suffix('.intent.json')
    if path.exists() or path.is_symlink() or intent_path.exists() or intent_path.is_symlink():
        raise RuntimeError('existing-resolution-inspect-only')
    prior['private_json'](intent_path, {'script_sha256': hashlib.sha256(script.encode()).hexdigest(),
                         'container_id': CID, 'original_intent_id': INTENT_ID, 'inspect_before_retry': True})
    try:
        result = subprocess.run(channel['TARGET']+['sudo python3 -I -c '+shlex.quote(script)],
                                input=json.dumps(payload).encode(), capture_output=True, timeout=55)
    except (OSError, subprocess.TimeoutExpired) as error:
        prior['private_json'](path, {'ok': False, 'error_type': type(error).__name__, 'inspect_before_retry': True,
                             'stdout_b64': base64.b64encode(getattr(error, 'stdout', None) or b'').decode(),
                             'stderr_b64': base64.b64encode(getattr(error, 'stderr', None) or b'').decode()})
        raise RuntimeError('resolution-outcome-unknown-inspect-private-evidence') from None
    # private_result's small failure summary is supplemented by complete raw
    # stdout/stderr, so the original reason remains available without UI leaks.
    prior['private_json'](path.with_suffix('.private-diagnostics.json'), {
                         'returncode': result.returncode,
                         'stdout_b64': base64.b64encode(result.stdout).decode(),
                         'stderr_b64': base64.b64encode(result.stderr).decode()})
    value = prior['private_result'](path, result)
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
