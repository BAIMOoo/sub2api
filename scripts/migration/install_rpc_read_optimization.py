#!/usr/bin/env python3
"""CAS install the reviewed RPC read optimization; no service or data mutation."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import subprocess

OLD_RPC = "8180879f75169f130b726536ee16957832a9d2bf9e193f93ad380ebd8264f6ec"
NEW_RPC = "85d980acff46125de0db0a2734d33b632f1cfb919577be871f8c56faff2db75a"
TEST_SHA = "6358be82d6a99af44459000c26b4257aa9a4e92d7145ab51b710f6532aff6b3b"
OLD_CONFIG = "ca7d126ee925221fce6bf88d42eccd11641116330a1cedfb1cb7323c50c3b032"
NEW_CONFIG = "8f47473b2a430b4f0b2f9b993cc75eed6c4e405abe8beb678a11e25636a2d40d"
STATE_SHA = "866a4d665f45b671d385d67028b0b46ffa881142bd0a6c63c0989fe7a224ff56"
LEDGER_SHA = "5c0e2528b2d135bc61d248ef9fde41f9c064e5dbdfc768ef79c8df8f254b53a8"

COMMON = r'''
import base64,hashlib,importlib.util,json,os,pathlib,sys
sys.dont_write_bytecode=True
p=json.load(sys.stdin);lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016');root=pathlib.Path('/var/lib/sub2api-migration-20261016')
def need(value,code):
 if not value:raise RuntimeError(code)
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);sys.modules[name]=m;s.loader.exec_module(m);return m
R=load('optimization_previous_rpc',lib/'target_rpc.py')
def raw(path,mode=0o600):return R.private_read(path,mode=mode)
def sha(value):return hashlib.sha256(value).hexdigest()
def write(path,value,mode=0o600):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as out:out.write(value);out.flush();os.fsync(out.fileno())
 R.fsync_directory(path.parent)
program=base64.b64decode(p['program'],validate=True)
need(sha(program)==p['new'],'new-program-digest-mismatch');compile(program,str(lib/'target_rpc.py'),'exec')
'''

TARGET = COMMON + r'''
cfg=R.load_config();store=R.FileStore(cfg);backend=R.LocalBackend(cfg)
saved=root/'rpc/read-optimization-20261017';path=lib/'target_rpc.py';config=root/'rpc/config.json'
with store.mutation_lock():
 with R.file_lock(root/'rpc/ledger.lock'):
  old=raw(path,0o700);oldcfg=raw(config);ledger=raw(root/'rpc/state.json');state=R.strict_json(ledger,8*1024**2)
  need(sha(old)==p['old'] and sha(oldcfg)==p['old_config'] and sha(ledger)==p['ledger'],'target-exact-boundary-changed')
  need(state['epoch']==24 and state['pg']['authority']==state['redis']['authority']=='AWS' and state['pg']['arm']['status']=='CANCELLED' and not state['pg']['promotion_intent'],'target-authority-not-closed')
  need(all(x['phase']=='COMPLETE' for x in state['operations'].values()),'target-operation-pending')
  need(backend.endpoint_state()=={'v':1,'postgres':'source','redis':'source'},'target-endpoints-changed')
  before={k:backend.resource(k)['State']['Pid'] for k in ('app','pg','redis')}
  runtime=backend.app_control('runtime.status')['body']
  need(runtime['instance_id']=='r32ufr8h00vvbf' and runtime['ready'] is True and runtime['background_workers_enabled'] is False,'target-generation-not-ready')
  need(not saved.exists() and not saved.is_symlink(),'target-upgrade-already-attempted-inspect-only')
  saved.mkdir(mode=0o700);R.fsync_directory(saved.parent)
  newcfg=R.canonical({**cfg,'program_sha256':p['new']})+b'\n'
  need(sha(newcfg)==p['new_config'],'target-new-config-digest-mismatch')
  for name,value in [('previous-program.py',old),('previous-config.json',oldcfg),('previous-ledger.json',ledger)]:write(saved/name,value)
  write(saved/'intent.json',R.canonical({k:v for k,v in p.items() if k!='program'}))
  pending=lib/'target_rpc.read-optimization.pending';cp=root/'rpc/config.read-optimization.pending'
  write(pending,program,0o700);write(cp,newcfg)
  need(raw(path,0o700)==old and raw(config)==oldcfg and raw(root/'rpc/state.json')==ledger,'target-final-CAS-changed')
  os.replace(pending,path);R.fsync_directory(lib);os.replace(cp,config);R.fsync_directory(config.parent)
  N=load('optimization_new_rpc',path);new=N.load_config();nb=N.LocalBackend(new)
  need(raw(path,0o700)==program and raw(config)==newcfg and raw(root/'rpc/state.json')==ledger,'target-upgrade-outcome-uncertain')
  need(before=={k:nb.resource(k)['State']['Pid'] for k in before},'target-business-process-changed')
  result={'v':1,'run':'20261016','owner':cfg['owner'],'old_program_sha256':p['old'],'new_program_sha256':p['new'],'old_config_sha256':p['old_config'],'new_config_sha256':p['new_config'],'ledger_sha256':p['ledger'],'business_processes_unchanged':True}
  write(saved/'verification.json',N.canonical(result));print(json.dumps(result,sort_keys=True))
'''

SOURCE = COMMON + r'''
M=load('optimization_service',lib/'runner_service.py');G=load('optimization_guard',lib/'runner_guard.py')
with M.execution_lock():
 b=M.B.RunnerBackend();g=G.LocalBackend();scope=root/'runner';saved=scope/'rpc-read-optimization-20261017'
 need(g.main_status()['quiescent'] and not M.selftest_armed(b.config['owner']),'main-not-closed')
 guard=b.source_ops._guard();need(guard['healthy'] is True and guard['tripped'] is False and guard['recovering'] is False,'guard-not-stable')
 M.verify_entry();M.read_gates(b.config['owner'])
 pins={'state.json':p['state'],'transport-state.json':'a8c7730e88f85946fc113eb28f82d9382788def742c75e1ed658e90395ba6112','source-operations.json':'7e086ba2e50e496381e3c9efd8952847dcefddff71100dcb1480282f5ef9427a'}
 frozen={name:raw(scope/name) for name in pins};need(all(sha(value)==pins[name] for name,value in frozen.items()),'source-exact-boundary-changed')
 state=R.strict_json(frozen['state.json'],8*1024**2)
 need(state['attempt']==6 and state['phase']=='SOURCE_DATA' and state['pg']==state['redis']=='AWS' and state['window'] is None and state['deadline_breached'] is False,'source-not-closed')
 need(not any(x['request']['action'] in ('pg_stop','redis_failover','redis_role_persist') for x in b.source_ops._ledger()['operations'].values()),'source-irreversible-operation-present')
 for kind,pid in [('app',0),('postgres',972839),('redis',972827)]:
  row=b.source_ops._resource(kind);need(row['State']['Pid']==pid and row['State']['Running']==(kind!='app'),'source-resource-changed')
 path=lib/'target_rpc.py';old=raw(path,0o644);need(sha(old)==p['old'],'source-program-CAS-changed')
 need(not saved.exists() and not saved.is_symlink(),'source-upgrade-already-attempted-inspect-only')
 saved.mkdir(mode=0o700);R.fsync_directory(scope);write(saved/'previous-program.py',old)
 write(saved/'intent.json',R.canonical({k:v for k,v in p.items() if k!='program'}))
 pending=lib/'target_rpc.read-optimization.pending';write(pending,program,0o644)
 need(raw(path,0o644)==old and all(raw(scope/name)==value for name,value in frozen.items()),'source-final-CAS-changed')
 os.replace(pending,path);R.fsync_directory(lib)
 need(raw(path,0o644)==program and all(raw(scope/name)==value for name,value in frozen.items()),'source-upgrade-outcome-uncertain')
 result={'ok':True,'new_program_sha256':p['new'],'state_and_ledgers_preserved':True,'services_restarted':False}
 write(saved/'verification.json',R.canonical(result));print(json.dumps(result,sort_keys=True))
'''


def need(value, code):
    if not value:
        raise RuntimeError(code)


def write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(value); out.flush(); os.fsync(out.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({'offline': True, 'services_restarted': False, 'state_and_ledgers_preserved': True}))
        return 0
    folder = pathlib.Path(__file__).resolve().parent
    program = (folder / 'target_rpc.py').read_bytes()
    need(hashlib.sha256(program).hexdigest() == NEW_RPC and hashlib.sha256((folder / 'target_rpc_test.py').read_bytes()).hexdigest() == TEST_SHA, 'reviewed-program-or-tests-changed')
    channel = runpy.run_path(str(folder / 'prepare_channel.py'))
    backup = pathlib.Path.home() / '.codex/backups/sub2api-source-controller-20261016'
    marker = backup / 'rpc-read-optimization-20261017.intent.json'
    payload = {'old': OLD_RPC, 'new': NEW_RPC, 'old_config': OLD_CONFIG, 'new_config': NEW_CONFIG,
               'state': STATE_SHA, 'ledger': LEDGER_SHA, 'test_sha256': TEST_SHA,
               'program': base64.b64encode(program).decode()}
    write_new(marker, json.dumps({k:v for k,v in payload.items() if k != 'program'}, sort_keys=True).encode())
    for name, ssh, script in [('target', channel['TARGET'], TARGET), ('source', channel['SSH_SOURCE'], SOURCE)]:
        receipt = backup / ('rpc-read-optimization-20261017.' + name + '.command.json')
        try:
            result = subprocess.run(ssh + ['sudo -n python3 -I -c ' + shlex.quote(script)],
                                    input=json.dumps(payload).encode(), capture_output=True, timeout=35)
        except (OSError, subprocess.TimeoutExpired) as error:
            write_new(receipt, json.dumps({'outcome':'unknown-inspect-only','error_type':type(error).__name__,
                'stdout_b64':base64.b64encode(getattr(error,'stdout',None) or b'').decode(),
                'stderr_b64':base64.b64encode(getattr(error,'stderr',None) or b'').decode(),'no_repeat':True}).encode())
            raise RuntimeError('upgrade-outcome-uncertain-inspect-private-receipt') from None
        write_new(receipt, json.dumps({'returncode':result.returncode,'stdout_b64':base64.b64encode(result.stdout).decode(),
                                     'stderr_b64':base64.b64encode(result.stderr).decode(),'no_repeat':True}).encode())
        need(result.returncode == 0, 'upgrade-outcome-uncertain-inspect-private-receipt')
        print(name, result.stdout.decode())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
