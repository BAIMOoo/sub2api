#!/usr/bin/env python3
"""Archive a passed unrouted r2 selftest and restore its initial checkpoint."""
import argparse
import json
import os
import pathlib
import runpy
import shlex
import subprocess

REMOTE = r'''
import importlib.util,json,math,pathlib,subprocess,sys,time
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016');spec=importlib.util.spec_from_file_location('runner_service',lib/'runner_service.py');M=importlib.util.module_from_spec(spec);sys.modules['runner_service']=M;spec.loader.exec_module(M)
B,E,S=M.B,M.E,M.S
attempt=M.SELFTEST_ATTEMPT
E.need(type(attempt) is int and attempt==1000001,'SELFTEST_R2_INSTALLED_ATTEMPT_REQUIRED')
def finite(value):return type(value) in (int,float) and math.isfinite(value)
with M.execution_lock():
 b=B.RunnerBackend();scope=B.SCOPE;state=b.store.read('state');owner=b.config['owner']
 initial=S.strict_json(S.private_bytes(scope/'selftest-initial-production-state-r2.json'))
 report=S.strict_json(S.private_bytes(scope/'controller-selftest-result-r2.json'))
 armed=S.strict_json(S.private_bytes(scope/'controller-selftest.json'))
 E.need(armed=={'v':1,'run':'20261016','owner':owner,'armed':True} and armed.get('armed') is True,'SELFTEST_R2_MUST_REMAIN_ARMED')
 E.need(report.get('v')==1 and report.get('run')=='20261016' and report.get('owner')==owner and report.get('selftest_round')=='r2' and report.get('selftest_attempt')==attempt and report.get('ok') is True and report.get('window_closed') is True and report.get('checkpoint_reset') is False and report.get('source_unchanged') is True and report.get('source_pids_unchanged') is True and type(report.get('main_started_pid')) is int and report['main_started_pid']>1 and report['health']['status']==200 and report['health'].get('body_ok') is True and finite(report['health']['elapsed']) and 0<=report['health']['elapsed']<=10 and report['state']==state,'PASSED_SELFTEST_R2_REQUIRED')
 E.need(state.get('attempt')==attempt and state['window'] is None and state['pg']==state['redis']=='AWS' and state['route_fenced'] is False and finite(state.get('pause_seconds')) and 0<=state['pause_seconds']<=9.5 and not state.get('deadline_breached') and finite(state.get('selftest_paused_at')) and 0<state['selftest_paused_at']<=time.time(),'UNROUTED_SELFTEST_R2_ONLY')
 E.need(initial=={'v':1,'run':'20261016','owner':owner,'phase':'ENTRY_PREPARATION','attempt':0,'pause_seconds':0.0,'window':None,'pg':'AWS','redis':'AWS','route_fenced':False},'SAVED_INITIAL_STATE_CHANGED')
 unit=subprocess.check_output(['systemctl','show','sub2api-migration-runner-20261016.service','--property=MainPID,ActiveState,Job']).decode()
 E.need('MainPID=0\n' in unit and ('ActiveState=inactive\n' in unit or 'ActiveState=failed\n' in unit) and 'Job=\n' in unit,'SELFTEST_MAIN_NOT_STOPPED')
 check=subprocess.run(['/usr/sbin/iptables','-w','1','-t','nat','-C','OUTPUT']+M.RULE,capture_output=True,timeout=2)
 E.need(check.returncode==1,'SELFTEST_CUSTOMER_BRIDGE_MUST_BE_ABSENT')
 guard=b.source_ops._guard();E.need(guard.get('recovering') is False,'SELFTEST_RECOVERY_STILL_RUNNING')
 target=b.rpc(None,'status');runtime=b.control(None,'runtime.status');ingress=b.control(None,'ingress.status');writers=b.control(None,'writers.status');pg=b.control(None,'postgres.status');redis=b.control(None,'redis.status')
 E.need(target.get('pg_authority')==target.get('redis_authority')=='AWS' and not target.get('pg_arm') and not target.get('pg_promotion_intent') and target['pg']['in_recovery'] is True and target['redis']['role']=='slave' and target['persistent_endpoints']=={'v':1,'postgres':'source','redis':'source'},'SELFTEST_AUTHORITIES_CHANGED')
 E.need(runtime.get('ready') is True and runtime.get('background_workers_enabled') is False and ingress.get('paused') is False and writers.get('held') is False and pg.get('paused') is False and pg.get('endpoint')=='source' and redis.get('paused') is False and redis.get('address')=='127.0.0.1:16379' and redis.get('pubsub_cache_ready') is True,'SELFTEST_RUNTIME_NOT_RESTORED')
 expected_pids={'app':2260633,'postgres':972839,'redis':972827}
 E.need(report.get('source_pids')==expected_pids,'SELFTEST_REPORT_SOURCE_PID_CHANGED')
 for kind,pid in expected_pids.items():
  actual=b.source_ops._running(kind);E.need(actual['State']['Pid']==pid,'SELFTEST_SOURCE_PID_CHANGED')
 archive=scope/'passed-selftest-20261017-r2';E.need(not archive.exists() and not archive.is_symlink(),'SELFTEST_R2_ALREADY_CLOSED');archive.mkdir(mode=0o700);S.fsync_directory(scope)
 names=('state.json','controller-selftest.json','controller-selftest-result-r2.json','selftest-initial-production-state-r2.json','transport-state.json','source-attestations.json')
 for name in names:S.atomic_private(archive/name,S.private_bytes(scope/name))
 # Customer traffic never used the target: only the isolated rehearsal's
 # checkpoint is restored. Transport, RPC and audit ledgers remain untouched.
 S.atomic_private(scope/'controller-selftest.json',S.canonical({'v':1,'run':'20261016','owner':owner,'armed':False})+b'\n')
 b.store.save_state(initial)
 if 'ActiveState=failed\n' in unit:subprocess.run(['systemctl','reset-failed','sub2api-migration-runner-20261016.service'],check=True,capture_output=True,timeout=3)
 result={'ok':True,'selftest_round':'r2','selftest_attempt':attempt,'selftest_disarmed':True,'initial_unrouted_checkpoint_restored':True,'production_pause_seconds':0.0,'selftest_pause_seconds':state['pause_seconds'],'health_wait_seconds':report['health']['elapsed'],'transport_and_rpc_ledgers_preserved':True,'source_pids_unchanged':True,'customer_route_changed':False}
 S.atomic_private(archive/'closure-verification.json',S.canonical(result)+b'\n')
 print(json.dumps(result,sort_keys=True))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"offline": True, "requires_passed_unrouted_selftest": True,
                          "production_budget_reset": False, "audit_ledgers_deleted": False,
                          "selftest_round": "r2", "selftest_attempt": 1000001}))
        return
    path = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016/selftest-closure-20261017-r2.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w+b") as out:
        out.write(json.dumps({"selftest_round": "r2", "attempted": True}).encode())
        out.flush(); os.fsync(out.fileno())
        directory = os.open(path.parent, os.O_RDONLY); os.fsync(directory); os.close(directory)
        channel = runpy.run_path(str(pathlib.Path(__file__).with_name("prepare_channel.py")))
        result = subprocess.run(channel["SSH_SOURCE"] + ["sudo python3 -I -c " + shlex.quote(REMOTE)],
                                capture_output=True, timeout=35)
        out.seek(0); out.truncate()
        out.write(result.stdout if result.returncode == 0 else json.dumps({"exit": result.returncode,
            "stderr": result.stderr.decode(errors="replace")[-4096:]}).encode())
        out.flush(); os.fsync(out.fileno())
    assert result.returncode == 0, "closure-incomplete-inspect-private-evidence"
    print(result.stdout.decode())


if __name__ == "__main__":
    main()
