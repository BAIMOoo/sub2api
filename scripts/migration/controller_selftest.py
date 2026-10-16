#!/usr/bin/env python3
"""Run the independent r2 AWS recovery rehearsal before customer routing.

The independent installed services perform pause and recovery. This client only
arms that fixed rehearsal, observes a queued health request and records proof.
It never resets checkpoints, changes DNS, signals containers or promotes data.
"""
import argparse
import json
import os
import pathlib
import runpy
import shlex
import subprocess


REMOTE = r'''
import concurrent.futures,http.client,importlib.util,json,math,pathlib,subprocess,sys,time
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016')
spec=importlib.util.spec_from_file_location('runner_service',lib/'runner_service.py')
M=importlib.util.module_from_spec(spec);sys.modules['runner_service']=M;spec.loader.exec_module(M)
B,E,S=M.B,M.E,M.S
backend=B.RunnerBackend();initial=backend.store.read('state');owner=backend.config['owner'];scope=B.SCOPE
attempt=M.SELFTEST_ATTEMPT
E.need(type(attempt) is int and attempt==1000001,'SELFTEST_R2_INSTALLED_ATTEMPT_REQUIRED')
def finite(value):return type(value) in (int,float) and math.isfinite(value)
def source_pids():return {kind:backend.source_ops._running(kind)['State']['Pid'] for kind in ('app','postgres','redis')}
expected_pids={'app':2260633,'postgres':972839,'redis':972827}
def main_status():
 raw=subprocess.check_output(['/usr/bin/systemctl','show','sub2api-migration-runner-20261016.service','--property=MainPID,ActiveState,SubState,Result,Job'],timeout=3).decode()
 pairs=[line.split('=',1) for line in raw.splitlines()];value=dict(pairs)
 E.need(len(value)==len(pairs) and set(value)=={'MainPID','ActiveState','SubState','Result','Job'} and value['MainPID'].isdigit(),'SELFTEST_MAIN_STATUS_INVALID')
 return value
backup=scope/'selftest-initial-production-state-r2.json';report_path=scope/'controller-selftest-result-r2.json'
with M.execution_lock():
 E.need(initial=={'v':1,'run':'20261016','owner':owner,'phase':'ENTRY_PREPARATION','attempt':0,'pause_seconds':0.0,'window':None,'pg':'AWS','redis':'AWS','route_fenced':False},'SELFTEST_INITIAL_STATE_REQUIRED')
 armed=S.strict_json(S.private_bytes(scope/'controller-selftest.json'))
 E.need(armed=={'v':1,'run':'20261016','owner':owner,'armed':False} and armed.get('armed') is False,'SELFTEST_DISARMED_CONFIGURATION_REQUIRED')
 E.need(all(not path.exists() and not path.is_symlink() for path in (backup,report_path)),'SELFTEST_R2_ALREADY_ATTEMPTED')
 main=main_status();E.need(main['MainPID']=='0' and main['ActiveState']=='inactive' and not main['Job'],'SELFTEST_MAIN_MUST_BE_INACTIVE')
 check=subprocess.run(['/usr/sbin/iptables','-w','1','-t','nat','-C','OUTPUT']+M.RULE,capture_output=True,timeout=2)
 E.need(check.returncode==1,'SELFTEST_CUSTOMER_BRIDGE_MUST_BE_ABSENT')
 before=backend.rpc(None,'status')
 E.need(before.get('pg_authority')==before.get('redis_authority')=='AWS' and not before.get('pg_arm') and not before.get('pg_promotion_intent') and before.get('pg',{}).get('in_recovery') is True and before.get('redis',{}).get('role')=='slave','SELFTEST_INITIAL_AUTHORITY_REQUIRED')
 before_source=backend.source(None,'inspect');before_pids=source_pids()
 E.need(all(before_source['resources'][k].get('running') is True for k in ('app','postgres','redis')) and before_pids==expected_pids,'SOURCE_NOT_LIVE_OR_ORIGINAL')
 backend.source_ops._guard()
 E.need(backend.store.read('state')==initial,'SELFTEST_INITIAL_CHECKPOINT_CHANGED')
 S.atomic_private(backup,S.canonical(initial)+b'\n')
 S.atomic_private(scope/'controller-selftest.json',S.canonical({'v':1,'run':'20261016','owner':owner,'armed':True})+b'\n')
start=time.monotonic()
subprocess.run(['/usr/bin/systemctl','start','--no-block','sub2api-migration-runner-20261016.service'],check=True,capture_output=True,timeout=3)
def queued_health():
 started=time.monotonic();connection=http.client.HTTPConnection('127.0.0.1',18081,timeout=15)
 try:
  connection.request('GET','/health',headers={'Connection':'close'});reply=connection.getresponse();body=reply.read(65537)
  return {'status':reply.status,'body_ok':json.loads(body).get('status')=='ok','elapsed':time.monotonic()-started}
 finally:connection.close()
health=None;closed=None;health_error=None;main_started_pid=None
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
 for _ in range(250):
  state=backend.store.read('state')
  if state.get('window') is not None and health is None:
   observed_main=main_status()
   if int(observed_main['MainPID'])>1:main_started_pid=int(observed_main['MainPID'])
   # The saved window precedes ingress.pause. Wait for actual ingress closed.
   ingress=backend.control(None,'ingress.status')
   if ingress.get('paused') is True:health=pool.submit(queued_health)
  if state.get('attempt')==attempt and state.get('window') is None:
   closed=state;break
  if time.monotonic()-start>20:break
  time.sleep(.05)
 try:result=health.result(timeout=16) if health is not None else None
 except Exception:
  result=None;health_error='QUEUED_HEALTH_DID_NOT_COMPLETE'
after=backend.rpc(None,'status');runtime=backend.control(None,'runtime.status');source=backend.source(None,'inspect')
unit=main_status();after_pids=source_pids()
report={'v':1,'run':'20261016','owner':owner,'selftest_round':'r2','selftest_attempt':attempt,'ok':False,'window_closed':closed is not None,'state':closed,'health':result,'health_error':health_error,'elapsed':time.monotonic()-start,'target_authorities':{k:after.get(k) for k in ('pg_authority','redis_authority','pg_arm','pg_promotion_intent')},'runtime_ready':runtime.get('ready'),'background_workers_enabled':runtime.get('background_workers_enabled'),'source_unchanged':source['resources']==before_source['resources'],'source_pids':after_pids,'source_pids_unchanged':after_pids==before_pids==expected_pids,'main_started_pid':main_started_pid,'main_unit':unit,'checkpoint_reset':False,'pause_acceptance_seconds':9.5}
S.atomic_private(report_path,S.canonical(report)+b'\n')
E.need(closed is not None and finite(closed.get('selftest_paused_at')) and 0<closed['selftest_paused_at']<=time.time() and closed.get('pg')==closed.get('redis')=='AWS' and closed.get('route_fenced') is False and result is not None and result['status']==200 and result['body_ok'] is True and finite(result['elapsed']) and 0<=result['elapsed']<=10 and finite(closed.get('pause_seconds')) and 0<=closed['pause_seconds']<=9.5 and not closed.get('deadline_breached') and type(main_started_pid) is int and main_started_pid>1 and unit['MainPID']=='0' and unit['ActiveState'] in ('inactive','failed') and not unit['Job'],'SELFTEST_WAIT_OR_RECOVERY_FAILED')
E.need(after.get('pg_authority')==after.get('redis_authority')=='AWS' and not after.get('pg_arm') and not after.get('pg_promotion_intent') and after.get('pg',{}).get('in_recovery') is True and after.get('redis',{}).get('role')=='slave' and runtime.get('ready') is True and runtime.get('background_workers_enabled') is False and report['source_unchanged'] and report['source_pids_unchanged'],'SELFTEST_AUTHORITY_OR_SOURCE_CHANGED')
report['ok']=True
S.atomic_private(report_path,S.canonical(report)+b'\n')
print(json.dumps(report,sort_keys=True))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"offline": True, "authority_actions": False, "checkpoint_reset": False,
                          "selftest_round": "r2", "selftest_attempt": 1000001,
                          "pause_acceptance_seconds": 9.5}))
        return
    folder = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / "controller-selftest-result-r2.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w+b") as out:
        out.write(json.dumps({"selftest_round": "r2", "attempted": True}).encode())
        out.flush(); os.fsync(out.fileno())
        directory = os.open(folder, os.O_RDONLY); os.fsync(directory); os.close(directory)
        channel = runpy.run_path(str(pathlib.Path(__file__).with_name("prepare_channel.py")))
        result = subprocess.run(channel["SSH_SOURCE"] + ["sudo python3 -I -c " + shlex.quote(REMOTE)],
                                capture_output=True, timeout=55)
        out.seek(0); out.truncate()
        out.write(result.stdout if result.returncode == 0 else json.dumps({"exit_code": result.returncode,
            "stdout": result.stdout.decode(errors="replace")[:4096], "stderr": result.stderr.decode(errors="replace")[:4096]}).encode())
        out.flush(); os.fsync(out.fileno())
    if result.returncode:
        raise RuntimeError("controller-selftest-incomplete-inspect-private-result-do-not-repeat")
    print(result.stdout.decode())


if __name__ == "__main__":
    main()
