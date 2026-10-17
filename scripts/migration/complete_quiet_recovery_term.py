#!/usr/bin/env python3
"""Authorize one independent root TERM resolution after a complete quiet proof.

This does not prove the original signal was never sent. Its UNKNOWN intent and
all original bytes remain intact. Default is offline; --apply is one attempt;
--inspect only reads. A draining process is observed without another signal.
The existing core subsequently reconciles the original intent by inspection.
"""
import argparse
import base64
import hashlib
import json
import pathlib
import runpy
import secrets
import shlex
import subprocess

CID = "cd5c29cf86b2595fffc41fc068e024d3f790f69bb8bdaaacc63eaba1ac9bcd82"
NEW_CID = "17e3bfacbd442beff52427e003e58f4970e613203356b8bb8bde0e98b9229bce"
NEW_INSTANCE = "r32ufr8h00vvbf"
INTENT_ID = "6d4713c17a33b7a5f3a4d1ee95ac461f"
LABEL = "quiet-term-resolution-20261017"
HOST_SHA = "a8194c260f88dd695fd02cd1b6f6f6dbe066ec9e7cbeee83cb72f12130e656ce"
CORE_SHA = "14b21455e754109b71e649e69a4adf3fd2e251e54f490ebf187a20c0442bc39f"
LEDGER_SHA = "abd784df64eaf0fb3d5d622f8025a221657078b731cf97d9859cee3ba8991ce3"
STATE_SHA = "bcfab87f4d51e2f24aa0b20c9380475fc828d25d9ce7e86ae5ec21f8076fc2a6"
PROCESS = {"pid": 257639, "start_ticks": 2596510, "started_at": "2026-10-16T16:09:03.001202869Z"}

HELPERS = r'''
def runtime_clients(proc,cid,name):
 active=[];shims=[]
 for entry in pathlib.Path(proc).iterdir():
  if not entry.name.isdecimal():continue
  try:raw=(entry/'cmdline').read_bytes()
  except (FileNotFoundError,ProcessLookupError):continue
  if not raw:continue
  argv=[os.fsdecode(part) for part in raw.rstrip(b'\0').split(b'\0')]
  binary=pathlib.PurePosixPath(argv[0]).name
  targeted=cid in '\0'.join(argv) or any(part in (name,'/'+name) for part in argv) or any(
    re.fullmatch(r'[0-9a-f]{4,63}',part) and cid.startswith(part) for part in argv[1:])
  if not targeted:continue
  row={'pid':int(entry.name),'binary':binary,'argv_sha256':sha(raw)}
  if binary.startswith(('containerd-shim','docker-containerd-shim')):
   # A standing runtime shim owns the existing process; it is not a pending
   # Docker kill/stop client. Never discount an explicit operation subcommand.
   if not any(part in ('kill','stop','delete','start','restart','pause','unpause') for part in argv[1:]):
    shims.append(row);continue
  if binary in ('docker','docker.io','docker-runc','runc','ctr','nerdctl') or binary.startswith(('containerd-shim','docker-containerd-shim')):
   if any(part in ('kill','stop','restart','start','rm','delete','pause','unpause','update') for part in argv[1:]):active.append(row)
 return {'active':active,'standing_shims':shims}

def strict_old(row,h,running=True):
 s=row.get('State',{})
 need(row.get('Id')==payload['cid'] and row.get('Image')==C.IMAGE and
      h.r.sha(row['Config'])==state['old_identity']['config_sha256'] and
      row.get('HostConfig',{}).get('RestartPolicy',{}).get('Name')=='no' and
      all(s.get(key) is False for key in ('OOMKilled','Paused','Restarting','Dead')),
      'old-container-identity-or-state-changed')
 if running:
  need(s.get('Running') is True and s.get('Status')=='running' and h._process(row)==payload['old_process'],
       'pinned-old-process-changed')
 else:
  need(s.get('Running') is False and s.get('Status')=='exited' and s.get('Pid')==0 and
       type(s.get('ExitCode')) is int and s['ExitCode']==0 and
       s.get('StartedAt')==payload['old_process']['started_at'],'old-clean-exit-unproven')

def complete_quiet(o,required=True):
 old=o['old'];work=old.get('work',{})
 quiet=(old.get('state')=='running' and old.get('ready') is True and old.get('serving') is True and
      old.get('draining') is False and old.get('process')==payload['old_process'] and
      old.get('tcp_scope')=='all-accepted-including-idle' and set(work)==set(C.QUIET_FIELDS) and
      all(type(work[key]) is int and work[key]==0 for key in C.QUIET_FIELDS))
 if required:need(quiet,'complete-old-quiet-proof-required')
 return quiet

def shutdown_markers(stdout,stderr):
 raw=(stdout+b'\n'+stderr).lower()
 return {marker.decode():raw.count(marker) for marker in (
  b'shutting down server',b'migration mode: waiting for http requests',
  b'migration request drain complete',b'migration http shutdown failed',b'server forced to shutdown')}
'''

TARGET = r'''
scope=root/'rolling-recovery';saved=scope/payload['label']
need(sha(trusted(lib/'rolling_app_host.py',0o700))==payload['host_sha'] and
     sha(trusted(lib/'rolling_app_recovery.py',0o700))==payload['core_sha'],'reviewed-rolling-program-changed')
H=load('quiet_resolution_host',lib/'rolling_app_host.py');C,h,cfg=H.load_host()
store=C.FileStore(scope/'state.json',uid=0)
counter=0
def checkpoint_cas():
 need(trusted(scope/'state.json',0o600)==state_raw and trusted(root/'rpc/state.json',0o600)==ledger_raw,
      'original-checkpoints-CAS-changed')
def pinned_read(path):
 return H.private_read(path,uid=0,limit=H.MAX_LOG) if path.parent==scope/'history' else trusted(path,0o600)
def unchanged():
 checkpoint_cas()
 need(sha(trusted(lib/'rolling_app_host.py',0o700))==payload['host_sha'] and
      sha(trusted(lib/'rolling_app_recovery.py',0o700))==payload['core_sha'],'reviewed-programs-raced')
 need(all(pinned_read(path)==raw for path,raw in frozen.items()),'pinned-files-or-history-changed')
def new_route_history():
 h._safety()
 new=h._app(h._docker('container',payload['new_cid']),new=True)
 C.Recovery(h,None,cfg['owner'])._new(state,new,running=True)
 need(new.get('instance_id')==payload['new_instance'],'new-generation-restarted')
 expected=C.copy.deepcopy(state['nat_before']);expected['output'].insert(0,C.REDIRECT)
 need(h._nat()==expected and C.Recovery._history(h.inspect_history())==state['history'],
      'exact-NAT-or-history-changed')
 unchanged();return new
def read_old_runtime():
 value=h.read_backend.app_control('runtime.status')
 need(value.get('http_status')==200 and type(value.get('body')) is dict,'old-runtime-unavailable-inspect-only')
 return value['body']
def sample():
 global counter
 counter+=1;prefix='sample-%02d'%counter
 row=h._docker('container',payload['cid'])
 if row['State']['Running'] is False:
  strict_old(row,h,running=False);new=new_route_history()
  proof={'mode':'exited','new':new,'old':row['State'],'at':time.time(),'tick':time.monotonic()}
 else:
  strict_old(row,h);runtime=read_old_runtime()
  if runtime.get('draining') is True:
   new=new_route_history();proof={'mode':'draining','new':new,'old_runtime':runtime,'at':time.time(),'tick':time.monotonic()}
  else:
   clients=runtime_clients(pathlib.Path('/proc'),payload['cid'],H.OLD_NAME)
   need(not clients['active'],'old-runtime-operation-in-flight')
   try:logs=subprocess.run(['/usr/bin/docker','logs','--timestamps',payload['cid']],capture_output=True,timeout=5,env=H.SAFE_ENV)
   except subprocess.TimeoutExpired as error:
    if payload['mode']=='apply':
     write_new(saved/(prefix+'.logs.stdout'),error.stdout or b'');write_new(saved/(prefix+'.logs.stderr'),error.stderr or b'')
    raise
   if payload['mode']=='apply':
    write_new(saved/(prefix+'.logs.stdout'),logs.stdout);write_new(saved/(prefix+'.logs.stderr'),logs.stderr)
   need(logs.returncode==0 and len(logs.stdout)<=H.MAX_LOG and len(logs.stderr)<=H.MAX_LOG,'old-logs-unavailable')
   markers=shutdown_markers(logs.stdout,logs.stderr)
   # The complete observation follows log retrieval. No network observation
   # may occur between the durable signal intent and the one TERM command.
   o=C.Recovery(h,None,cfg['owner'])._observe(state)
   C.Recovery(h,None,cfg['owner'])._new(state,o['new'],running=True)
   need(o['new'].get('container_id')==payload['new_cid'] and o['new'].get('instance_id')==payload['new_instance'],
        'new-generation-changed')
   quiet=complete_quiet(o,required=payload['mode']=='apply')
   runtime_idle=(runtime.get('ready') is True and runtime.get('serving') is True and runtime.get('draining') is False and
        runtime.get('active_requests')==runtime.get('active_hijacked_connections')==0 and
        runtime.get('ingress',{}).get('queued_requests')==0)
   if payload['mode']=='apply':need(runtime_idle,'old-runtime-not-idle-ready')
   proof={'mode':'draining' if any(markers.values()) else ('quiet' if quiet and runtime_idle else 'busy'),'observation':o,'old_runtime':runtime,
          'clients':clients,'shutdown_markers':markers,'logs_stdout_sha256':sha(logs.stdout),
          'logs_stderr_sha256':sha(logs.stderr),'at':time.time(),'tick':time.monotonic()}
 unchanged()
 if payload['mode']=='apply':write_new(saved/(prefix+'.json'),canonical(proof))
 return proof
def wait_exit():
 end=time.monotonic()+30
 while True:
  proof=sample()
  if proof['mode']=='exited':return {'exited_cleanly':True,'pending':False}
  if time.monotonic()>=end:return {'exited_cleanly':False,'pending':True}
  time.sleep(.5)
def finish(signal_sent,observed):
 unchanged()
 result={'ok':observed['exited_cleanly'],'signal_sent_by_resolution':signal_sent,
  'single_signal_maximum':True,'pending':observed['pending'],'original_intent_id':payload['intent_id'],
  'resolution_id':payload.get('resolution_id'),'original_state_raw_sha256':sha(state_raw),
  'target_ledger_raw_sha256':sha(ledger_raw),'original_state_preserved':True,'original_ledger_preserved':True,
  'reconciled_original_intent':False,'existing_core_inspection_required':True,'checked_at':time.time()}
 write_new(saved/'verification.json',canonical(result));print(json.dumps(result,sort_keys=True))
with store.locked():
 state=store.read();state_raw=trusted(scope/'state.json',0o600);ledger_raw=trusted(root/'rpc/state.json',0o600)
 need(sha(ledger_raw)==payload['ledger_sha'],'original-target-ledger-changed')
 frozen={scope/name:trusted(scope/name,0o600) for name in ('host-config.json','new-process.json',
    'source-controller-closed.json','source-recovery-receipt.json')}
 frozen.update({root/'rpc/config.json':trusted(root/'rpc/config.json',0o600)})
 frozen.update({scope/'history'/name:pinned_read(scope/'history'/name) for name in (
    'old-runtime.json','old-process.json','dockerlogs.stdout','dockerlogs.stderr','logs-manifest.json',
    'source-recovery-receipt.json','receipt.json')})
 if payload['mode']=='inspect':
  proof=sample();files={}
  if saved.exists():
   for name in ('authorization.json','signal.intent.json','docker-term.result.json','verification.json','failure.traceback.txt'):
    path=saved/name
    if path.exists() or path.is_symlink():files[name]={'raw_sha256':sha(trusted(path,0o600))}
  print(json.dumps({'inspect_only':True,'signal_sent':False,'old_mode':proof['mode'],'phase':state.get('phase'),
    'original_intent':state.get('intent'),'resolution_files':files,'state_raw_sha256':sha(state_raw)},sort_keys=True))
 else:
  need(sha(state_raw)==payload['state_sha'],'original-rolling-state-changed')
  need(state.get('v')==1 and state.get('owner')==cfg['owner'] and state.get('phase')=='TERM_INTENT' and
       state.get('intent')=={'id':payload['intent_id'],'kind':'term','status':'UNKNOWN'} and
       state.get('old_identity',{}).get('container_id')==payload['cid'] and
       state.get('new_identity',{}).get('container_id')==payload['new_cid'], 'exact-owned-unknown-term-required')
  need(not saved.exists() and not saved.is_symlink(),'quiet-resolution-already-attempted-inspect-only')
  saved.mkdir(mode=0o700);sync(scope)
  try:
   write_new(saved/'rolling-state-before.json',state_raw);write_new(saved/'target-ledger-before.json',ledger_raw)
   write_new(saved/'pinned-files-before.json',canonical({str(p):sha(raw) for p,raw in frozen.items()}))
   authorization={'v':1,'owner':cfg['owner'],'run':'20261016','kind':'independent-root-quiet-TERM-resolution',
    'resolution_id':payload['resolution_id'],'original_intent_id':payload['intent_id'],
    'original_state_raw_sha256':sha(state_raw),'original_ledger_raw_sha256':sha(ledger_raw),
    'old_container_id':payload['cid'],'old_process':payload['old_process'],'new_container_id':payload['new_cid'],
    'new_instance_id':payload['new_instance'],'host_sha256':payload['host_sha'],'core_sha256':payload['core_sha'],
    'does_not_prove_original_signal_never_sent':True,'maximum_new_TERM_signals':1,'at':time.time()}
   write_new(saved/'authorization.json',canonical(authorization))
   samples=[];seen=set();boot=None
   for index in range(3):
    proof=sample();samples.append(proof)
    if proof['mode']!='quiet':break
    o=proof['observation'];need(o['sample_id'] not in seen and (boot is None or boot==o['boot_id']),'quiet-sample-not-fresh-or-same-boot')
    seen.add(o['sample_id']);boot=o['boot_id']
    if index<2:time.sleep(1)
   if samples[-1]['mode']!='quiet':finish(False,wait_exit())
   else:
    need(len(samples)==3 and samples[-1]['tick']-samples[0]['tick']>=2,'quiet-proof-shorter-than-two-seconds')
    write_new(saved/'three-complete-quiet-samples.json',canonical(samples))
    last=sample()
    if last['mode']!='quiet':finish(False,wait_exit())
    else:
     need(last['observation']['boot_id']==boot and last['observation']['sample_id'] not in seen,'final-sample-not-fresh')
     # Complete every process/network/history/health check before intent.
     strict_old(h._docker('container',payload['cid']),h)
     rt=read_old_runtime();clients=runtime_clients(pathlib.Path('/proc'),payload['cid'],H.OLD_NAME)
     need(not clients['active'],'old-runtime-operation-in-flight')
     unchanged()
     if rt.get('draining') is True:finish(False,wait_exit())
     else:
      need(rt.get('ready') is True and rt.get('serving') is True and rt.get('draining') is False and
           rt.get('active_requests')==rt.get('active_hijacked_connections')==0 and
           rt.get('ingress',{}).get('queued_requests')==0,'final-old-runtime-not-idle')
      argv=['/usr/bin/docker','kill','--signal','TERM',payload['cid']]
      write_new(saved/'signal.intent.json',canonical({**authorization,'status':'INTENT','at':time.time(),
       'quiet_proof_sha256':sha(canonical(samples)),'final_sample_sha256':sha(canonical(last)),
       'final_old_runtime':rt,'final_runtime_clients':clients}))
      # Only a final byte CAS is allowed after this fsynced intent. A failed
      # CAS leaves the independent attempt inspect-only without issuing TERM.
      checkpoint_cas()
      try:result=subprocess.run(argv,capture_output=True,timeout=5,env=H.SAFE_ENV)
      except subprocess.TimeoutExpired as error:
       write_new(saved/'docker-term.stdout',error.stdout or b'');write_new(saved/'docker-term.stderr',error.stderr or b'');raise
      write_new(saved/'docker-term.stdout',result.stdout);write_new(saved/'docker-term.stderr',result.stderr)
      write_new(saved/'docker-term.result.json',canonical({'returncode':result.returncode}))
      need(result.returncode==0,'TERM-outcome-unknown-inspect-private-diagnostics')
      finish(True,wait_exit())
  except Exception:
   write_new(saved/'failure.traceback.txt',__import__('traceback').format_exc().encode());raise
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--inspect", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply and not args.inspect:
        print(json.dumps({"offline": True, "original_intent_replayed": False, "original_state_changed": False,
            "container_id": CID, "original_intent_id": INTENT_ID, "three_full_quiet_samples_minimum_seconds": 2,
            "draining_is_observed_without_another_signal": True, "maximum_new_TERM_signals": 1}))
        return 0
    folder = pathlib.Path(__file__).parent
    for name, digest in (("rolling_app_host.py", HOST_SHA), ("rolling_app_recovery.py", CORE_SHA)):
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("reviewed-rolling-program-changed")
    prior = runpy.run_path(str(folder / "prepare_retry.py"))
    channel = runpy.run_path(str(folder / "prepare_channel.py"))
    common = runpy.run_path(str(folder / "recover_source_guard.py"))["COMMON"]
    script = common + HELPERS + TARGET
    compile(script, "complete-quiet-recovery-term", "exec")
    payload = {"mode": "apply" if args.apply else "inspect", "cid": CID, "new_cid": NEW_CID,
        "new_instance": NEW_INSTANCE, "intent_id": INTENT_ID, "label": LABEL, "host_sha": HOST_SHA,
        "core_sha": CORE_SHA, "ledger_sha": LEDGER_SHA, "state_sha": STATE_SHA,
        "old_process": PROCESS, "resolution_id": secrets.token_hex(16)}
    path = prior["FOLDER"] / (LABEL + ".json")
    marker = path.with_suffix(".intent.json")
    if args.apply:
        if any(p.exists() or p.is_symlink() for p in (path, marker)):
            raise RuntimeError("existing-resolution-inspect-only")
        prior["private_json"](marker, {"script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "container_id": CID, "original_intent_id": INTENT_ID, "resolution_id": payload["resolution_id"], "no_repeat": True})
    try:
        result = subprocess.run(channel["TARGET"] + ["sudo python3 -I -c " + shlex.quote(script)],
            input=json.dumps(payload).encode(), capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        if args.apply:
            prior["private_json"](path, {"ok": False, "error_type": type(error).__name__, "inspect_before_retry": True,
                "stdout_b64": base64.b64encode(getattr(error, "stdout", None) or b"").decode(),
                "stderr_b64": base64.b64encode(getattr(error, "stderr", None) or b"").decode()})
        raise RuntimeError("resolution-outcome-unknown-inspect-only") from None
    if args.inspect:
        if result.returncode != 0:
            raise RuntimeError("resolution-inspection-unavailable")
        print(json.dumps(json.loads(result.stdout), sort_keys=True))
        return 0
    prior["private_json"](path.with_suffix(".private-diagnostics.json"), {"returncode": result.returncode,
        "stdout_b64": base64.b64encode(result.stdout).decode(), "stderr_b64": base64.b64encode(result.stderr).decode()})
    value = prior["private_result"](path, result)
    print(json.dumps(value, sort_keys=True))
    return 0 if value.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
