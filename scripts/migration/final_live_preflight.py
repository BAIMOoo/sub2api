#!/usr/bin/env python3
"""Read-only final AWS/Tencent migration checks. Default execution is offline.

--check sends fixed inspection programs through prepare_channel's SSH routes
and saves private local evidence. It does not send model requests or change
services, gates, routes, DNS, authority, checkpoints or remote evidence.
"""
import argparse
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import subprocess
import time

FOLDER = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
REFERENCE_SHA = "44ce0dcd06ed4379616bc4746f778e30fa5c185b86054d74af8b3fbb89dd8c2f"
BACKEND_SHA = "afb43d190c7e80ed2e9759c88ce5d0b6cefbbf7755eeb1b04f53a0f7de131dd1"
RPC_SHA = "2b4fa88abb3a35c3ba1349ed7370d624cb5c3ebdd2fcbc121fd9704c4a3e196c"
CONFIG_SHA = "9eb33e0f44ac2e6b8c9e743a2bd7593548967aa2eca2233548391a1581df2169"

COMMON = r'''
import hashlib,http.client,importlib.util,json,math,os,pathlib,socket,ssl,stat,subprocess,sys,time
sys.dont_write_bytecode=True
lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016')
prefix=pathlib.Path('/var/lib/sub2api-migration-20261016')
owner='30bda011defad8149621c2630a2bd89c'
def need(ok,code):
 if not ok:raise RuntimeError(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def finite(value):return type(value) in (int,float) and math.isfinite(value)
def root_read(path,mode=0o600):
 path=pathlib.Path(path)
 for parent in path.parents:
  info=parent.lstat();need(stat.S_ISDIR(info.st_mode),'ROOT_PARENT_CHANGED')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  info=os.fstat(fd)
  need(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==mode and info.st_size<=8*1024**2,'ROOT_FILE_CHANGED')
  with os.fdopen(fd,'rb',closefd=False) as stream:return stream.read(8*1024**2+1)
 finally:os.close(fd)
def unit(name,pid=None,inactive=False):
 raw=subprocess.check_output(['/usr/bin/systemctl','show',name,'--property=MainPID,ActiveState,SubState,Job,ExecMainStartTimestampMonotonic'],timeout=3).decode()
 pairs=[line.split('=',1) for line in raw.splitlines()];value=dict(pairs)
 need(len(value)==len(pairs) and set(value)=={'MainPID','ActiveState','SubState','Job','ExecMainStartTimestampMonotonic'} and not value['Job'],'UNIT_STATUS_CHANGED')
 if inactive:need(value['MainPID']=='0' and value['ActiveState']=='inactive' and value['SubState']=='dead','RUNNER_NOT_INACTIVE')
 else:need(value['MainPID']==str(pid) and value['ActiveState']=='active' and value['SubState']=='running','PROTECTED_UNIT_CHANGED')
 return value
def business(root):
 result={}
 for name in ('config.yaml','.installed','model_pricing.json','model_pricing.sha256'):
  path=pathlib.Path(root)/name
  need(all(not p.is_symlink() for p in (path,*path.parents)),'BUSINESS_FILE_SYMLINK')
  before=path.stat();need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=1024**2,'BUSINESS_FILE_INVALID')
  raw=path.read_bytes();after=path.stat()
  need((before.st_ino,before.st_mtime_ns,before.st_size)==(after.st_ino,after.st_mtime_ns,after.st_size),'BUSINESS_FILE_CHANGED_DURING_READ')
  result[name]=sha(raw)
 return result
def load(name):
 spec=importlib.util.spec_from_file_location(name,lib/(name+'.py'))
 module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module
def lsn(value):
 need(type(value) is str and __import__('re').fullmatch(r'[0-9A-F]{1,8}/[0-9A-F]{1,8}',value),'INVALID_LSN')
 high,low=value.split('/');return (int(high,16)<<32)+int(low,16)
def summary(info):
 return {'id':info['Id'],'image_id':info['Image'],'pid':info['State']['Pid'],'started_at':info['State']['StartedAt'],
         'running':info['State']['Running'],'restart':info['HostConfig']['RestartPolicy']['Name']}
need(os.geteuid()==0,'ROOT_REQUIRED')
'''

SOURCE = r'''
for name,wanted in hashes.items():need(sha(root_read(lib/name,0o644))==wanted,'SOURCE_PROGRAM_CHANGED:'+name)
M=load('runner_service');b=M.B.RunnerBackend();S=M.S;scope=M.B.SCOPE
need(b.config['owner']==owner,'SOURCE_OWNER_CHANGED')
initial={'v':1,'run':'20261016','owner':owner,'phase':'ENTRY_PREPARATION','attempt':0,'pause_seconds':0.0,'window':None,'pg':'AWS','redis':'AWS','route_fenced':False}
def initial_state():need(b.store.read('state')==initial,'SOURCE_CHECKPOINT_NOT_INITIAL')
def no_bridge():
 result=subprocess.run(['/usr/sbin/iptables','-w','1','-t','nat','-C','OUTPUT']+M.RULE,capture_output=True,timeout=2)
 need(result.returncode==1,'CUSTOMER_BRIDGE_NOT_ABSENT')
def pids():
 resources={key:b.source_ops._running(key) for key in ('app','postgres','redis')}
 for key,info in resources.items():
  wanted=base['source']['source_resources'][key];actual=summary(info)
  need(all(actual[k]==wanted[k] for k in ('id','pid','started_at','running','restart')),'SOURCE_ORIGINAL_RESOURCE_CHANGED:'+key)
 return resources
initial_state();no_bridge();main=unit('sub2api-migration-runner-20261016.service',inactive=True)
need(json.loads(root_read(scope/'controller-selftest.json'))=={'v':1,'run':'20261016','owner':owner,'armed':False},'SELFTEST_NOT_DISARMED')
archive=scope/'passed-selftest-20261017-r2'
closed=json.loads(root_read(archive/'closure-verification.json'))
need(all(closed.get(k) is True for k in ('ok','selftest_disarmed','initial_unrouted_checkpoint_restored','transport_and_rpc_ledgers_preserved','source_pids_unchanged')) and closed.get('selftest_round')=='r2' and closed.get('selftest_attempt')==1000001 and closed.get('customer_route_changed') is False and closed.get('production_pause_seconds')==0.0,'R2_CLOSURE_REQUIRED')
need(finite(closed.get('selftest_pause_seconds')) and 0<=closed['selftest_pause_seconds']<=9.5 and finite(closed.get('health_wait_seconds')) and 0<=closed['health_wait_seconds']<=10,'R2_WAIT_LIMIT_FAILED')
report=json.loads(root_read(archive/'controller-selftest-result-r2.json'))
need(report.get('ok') is True and report.get('owner')==owner and report.get('run')=='20261016' and report.get('selftest_round')=='r2' and report.get('selftest_attempt')==1000001 and report.get('checkpoint_reset') is False and report.get('source_pids')=={'app':2260633,'postgres':972839,'redis':972827},'R2_REPORT_INVALID')
need(report['state']['pause_seconds']==closed['selftest_pause_seconds'] and report['health']['elapsed']==closed['health_wait_seconds'] and report['health']['status']==200 and report['health']['body_ok'] is True and not report['state'].get('deadline_breached'),'R2_REPORT_CLOSURE_MISMATCH')
protected_ids={'caddy.service':1284574,'codex-remote-relay.service':1307343,'sub2api-migration-channel-20261016.service':2288434}
protected={name:unit(name,pid) for name,pid in protected_ids.items()};before=pids()
deploy=pathlib.Path('/home/ubuntu/sub2api/deploy');archives=prefix/'deployment-archive'
fence=b'# AWS Sub2API deployment retired by migration 20261016.\n# Originals: /var/lib/sub2api-migration-20261016/deployment-archive\nsub2api_migration_fenced: true\n'
expected=base['source']['deployment_fences']
need(sorted(p.name for p in deploy.glob('*compose*.y*ml'))==sorted(expected),'COMPOSE_INVENTORY_CHANGED')
for name,wanted in expected.items():
 raw=root_read(deploy/name,0o444);original=root_read(archives/name)
 need(raw==fence and sha(raw)==wanted['fence_sha256'] and sha(original)==wanted['original_sha256'],'COMPOSE_FENCE_OR_ARCHIVE_CHANGED:'+name)
need(sha(root_read(archives/'verification.json'))==base['source']['deployment_verification_raw_sha256'],'DEPLOYMENT_VERIFICATION_CHANGED')
# The shared lock already exists; this read probe must not create remote files.
root_read(scope/'pg-sampling.lock')
pg=b.source_ops._pg_sample();expected_pg=b.source_ops.m['postgres'];rows=pg['owned_replication']
need(len(rows)==len(pg['replication'])==1 and rows==pg['replication'],'PG_REPLICA_NOT_UNIQUE_AND_OWNED')
replica=rows[0];need(replica['state']=='streaming','PG_REPLICA_NOT_STREAMING')
positions=[lsn(replica[k]) for k in ('replay_lsn','flush_lsn','sent_lsn')]+[lsn(pg['flushed_lsn'])]
lag=max(positions)-positions[0]
need(positions[:3]==sorted(positions[:3]) and 0<=lag<=1024**2,'PG_REPLAY_LAG_INVALID_OR_OVER_1MIB')
slots=pg['slots'];need(len(slots)==1 and slots[0]['name']==expected_pg['slot'] and slots[0]['active'] is True and slots[0]['wal_status']=='reserved' and 0<=max(positions)-lsn(slots[0]['restart_lsn'])<384*1024**2,'PG_SLOT_NOT_ACTIVE_RESERVED')
need(pg['prepared_transactions']==0 and pg['wal_cap_mb']=='512MB','PG_PREPARED_OR_WAL_CAP_CHANGED')
pair=b.source_ops._redis_pair();a,z=pair['source'],pair['target']
need(a['role']=='master' and z['role']=='slave' and a['connected_slaves']=='1' and z['master_link_status']=='up' and z['master_sync_in_progress']=='0' and z['master_host']=='127.0.0.1' and z['master_port']=='16379' and a['master_failover_state']==z['master_failover_state']=='no-failover','REDIS_NOT_HEALTHY_MASTER_REPLICA')
need(a['master_replid']==z['master_replid']=='6fbca65ab347b9efa6637b4c790feaa93c29d348' and abs(int(a['master_repl_offset'])-int(z['slave_repl_offset']))<=1024**2,'REDIS_REPLICATION_LAG_OR_REPLID_CHANGED')
mounts=[m for m in before['app']['Mounts'] if m['Destination']=='/app/data'];need(len(mounts)==1,'SOURCE_APP_DATA_AMBIGUOUS')
files=business(mounts[0]['Source'])
context=ssl.create_default_context();need(context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED,'TLS_VERIFICATION_DISABLED')
with socket.create_connection(('43.165.177.136',443),timeout=5) as raw:
 with context.wrap_socket(raw,server_hostname='api.b4im.com') as connection:
  certificate=sha(connection.getpeercert(binary_form=True));need(certificate==base['source']['tls']['leaf_der_sha256'],'TLS_CERTIFICATE_PIN_CHANGED')
  connection.sendall(b'GET /health HTTP/1.1\r\nHost: api.b4im.com\r\nConnection: close\r\n\r\n')
  response=http.client.HTTPResponse(connection);response.begin();body=response.read(65537)
  need(response.status==200 and len(body)<=65536 and json.loads(body).get('status')=='ok','TARGET_TLS_HEALTH_FAILED')
  tls={'http_status':response.status,'leaf_der_sha256':certificate,'full_chain_and_hostname_verified':True,'sni_host':'api.b4im.com','host_header':'api.b4im.com','target_ip':'43.165.177.136','port':443,'tls_version':connection.version()}
connection=http.client.HTTPConnection('127.0.0.1',18081,timeout=5)
try:
 connection.request('GET','/health',headers={'Connection':'close'});response=connection.getresponse();body=response.read(65537)
 need(response.status==200 and len(body)<=65536 and json.loads(body).get('status')=='ok','BRIDGE_HEALTH_FAILED')
finally:connection.close()
after=pids();need({k:summary(v) for k,v in before.items()}=={k:summary(v) for k,v in after.items()},'SOURCE_RESOURCE_CHANGED_DURING_PREFLIGHT')
need(protected=={name:unit(name,pid) for name,pid in protected_ids.items()},'PROTECTED_UNITS_CHANGED_DURING_PREFLIGHT')
initial_state();no_bridge();need(unit('sub2api-migration-runner-20261016.service',inactive=True)==main,'RUNNER_CHANGED_DURING_PREFLIGHT')
guard=b.source_ops._guard();need(guard.get('recovering') is False and guard.get('tripped') is False,'GUARD_RECOVERING_OR_TRIPPED')
need(not (scope/'guard-trip.json').exists() and not (scope/'guard-trip.json').is_symlink(),'GUARD_TRIP_STILL_PRESENT')
print(json.dumps({'ok':True,'remote_mutations':False,'owner':owner,'sampled_at':time.time(),'program_sha256':hashes,'state':initial,'r2_closure':closed,'guard':guard,'protected_units':protected,'source_resources':{k:summary(v) for k,v in after.items()},'business_file_sha256':files,'pg_replay_lag_bytes':lag,'redis':{key:{k:v for k,v in value.items() if k in ('role','run_id','master_replid','master_repl_offset','slave_repl_offset')} for key,value in pair.items()},'tls':tls,'bridge_health':200},sort_keys=True))
'''

TARGET = r'''
need(sha(root_read(lib/'target_rpc.py',0o700))==rpc_sha,'TARGET_RPC_CHANGED')
need(sha(root_read(prefix/'rpc/config.json'))==config_sha,'TARGET_RPC_CONFIG_CHANGED')
R=load('target_rpc');config=R.load_config();need(config['owner']==owner,'TARGET_OWNER_CHANGED');b=R.LocalBackend(config)
def resources():
 result={key:summary(b.resource(key)) for key in ('pg','redis','app')}
 need(result==base['target']['target_resources'],'TARGET_ORIGINAL_RESOURCE_CHANGED')
 return result
before=resources();entry=unit('sub2api-tencent-entry.service',259990)
ledger_raw=R.private_read(R.SCOPE/'state.json');ledger=R.strict_json(ledger_raw,8*1024**2)
need(ledger['run']=='20261016' and ledger['owner']==owner and ledger['pg']['authority']==ledger['redis']['authority']=='AWS' and not ledger['pg'].get('arm') and not ledger['pg'].get('promotion_intent'),'TARGET_AUTHORITY_CHANGED')
need(all(op.get('phase')=='COMPLETE' for op in ledger['operations'].values()),'TARGET_OPERATION_UNRESOLVED')
manifest=b.pg_manifest();pg=b.pg_sample();redis=b.redis_inspect();endpoints=b.endpoint_state()
need(manifest['authority_mode']=='STANDBY' and not manifest.get('promotion_intent') and pg['in_recovery'] is True and pg['system_identifier']==config['pg']['system_identifier']=='7626048206782038050' and pg['source_timeline']==config['pg']['source_timeline']==1 and pg['version_num']==180003 and pg['transaction_read_only']=='on' and pg['free_bytes']>=2*1024**3,'TARGET_PG_NOT_OWNED_STANDBY')
lsn(pg['replay_lsn'])
need(redis['role']=='slave' and redis['run_id']=='39d29826637d6bcf89b9630612f7fd0107260394' and redis['master_replid']=='6fbca65ab347b9efa6637b4c790feaa93c29d348' and redis['failover_state']=='no-failover','TARGET_REDIS_NOT_OWNED_REPLICA')
need(endpoints=={'v':1,'postgres':'source','redis':'source'},'TARGET_PERSISTED_ENDPOINT_CHANGED')
read=b.app_session();controls={name:read(name+'.status') for name in ('runtime','ingress','writers','postgres','redis','background')}
need(all(value.get('http_status')==200 for value in controls.values()),'TARGET_CONTROL_NOT_HEALTHY')
controls={name:value['body'] for name,value in controls.items()};runtime=controls['runtime']
need(runtime.get('ready') is True and runtime.get('serving') is True and runtime.get('background_workers_enabled') is False and runtime.get('active_requests')==runtime.get('active_hijacked_connections')==0,'TARGET_RUNTIME_NOT_READY_AND_IDLE')
need(controls['ingress'].get('paused') is False and controls['writers'].get('held') is False and controls['postgres'].get('paused') is False and controls['postgres'].get('endpoint')=='source' and controls['redis'].get('paused') is False and controls['redis'].get('address')=='127.0.0.1:16379' and controls['redis'].get('pubsub_cache_ready') is True and controls['background'].get('activated') is False and controls['background'].get('activating') is False,'TARGET_CONTROLS_NOT_SOURCE_AND_OPEN')
files=business(prefix/'app/data')
need(resources()==before and unit('sub2api-tencent-entry.service',259990)==entry,'TARGET_CHANGED_DURING_PREFLIGHT')
need(R.private_read(R.SCOPE/'state.json')==ledger_raw,'TARGET_LEDGER_CHANGED_DURING_PREFLIGHT')
print(json.dumps({'ok':True,'remote_mutations':False,'owner':owner,'sampled_at':time.time(),'rpc_sha256':rpc_sha,'config_sha256':config_sha,'target_resources':before,'entry_unit':entry,'business_file_sha256':files,'persistent_endpoints':endpoints,'authorities':{'pg':'AWS','redis':'AWS'},'controls':controls,'pg':pg,'redis':redis},sort_keys=True))
'''


def remote_scripts(base, hashes):
    constants = "\nbase=" + repr(base) + "\nhashes=" + repr(hashes)
    constants += "\nrpc_sha=" + repr(RPC_SHA) + "\nconfig_sha=" + repr(CONFIG_SHA) + "\n"
    return COMMON + constants + SOURCE, COMMON + constants + TARGET


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.check:
        print(json.dumps({"offline": True, "remote_mutations": False, "model_request_sent": False,
                          "requires": "reviewed guard installed and successful r2 closure"}))
        return
    root = pathlib.Path(__file__).parent
    frozen = runpy.run_path(str(root / "upgrade_control_verified.py"))["NEW"]
    hashes = {**frozen, "runner_backend.py": BACKEND_SHA}
    for name, expected in hashes.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("local-frozen-program-changed:" + name)
    raw = (FOLDER / "final-preflight-20261017.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != REFERENCE_SHA:
        raise RuntimeError("reviewed-preflight-reference-changed")
    original = json.loads(raw)
    base = {"source": {key: original["source"][key] for key in (
        "source_resources", "deployment_fences", "deployment_verification_raw_sha256", "tls")},
        "target": {"target_resources": original["target"]["target_resources"]}}
    source, target = remote_scripts(base, hashes)
    compile(source, "source-final-live-preflight", "exec")
    compile(target, "target-final-live-preflight", "exec")
    channel = runpy.run_path(str(root / "prepare_channel.py"))
    path = FOLDER / ("final-live-preflight-20261017-" + str(time.time_ns()) + ".json")
    if FOLDER.is_symlink() or not FOLDER.is_dir():
        raise RuntimeError("private-evidence-directory-required")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    evidence = {"ok": False, "remote_mutations": False, "model_request_sent": False,
                "reference_sha256": REFERENCE_SHA, "checked_at": time.time()}
    with os.fdopen(fd, "w") as out:
        def save():
            out.seek(0); out.truncate(); json.dump(evidence, out, sort_keys=True, indent=2)
            out.write("\n"); out.flush(); os.fsync(out.fileno())
        save()
        try:
            for name, route, script in (("target", channel["TARGET"], target),
                                        ("source", channel["SSH_SOURCE"], source)):
                result = subprocess.run(route + ["sudo python3 -I -c " + shlex.quote(script)],
                                        capture_output=True, timeout=60)
                if result.returncode:
                    evidence["failed_host"] = name
                    evidence["exit_code"] = result.returncode
                    evidence["diagnostic"] = result.stderr.decode(errors="replace")[-2048:]
                    raise RuntimeError(name + "-preflight-failed")
                evidence[name] = json.loads(result.stdout)
                if evidence[name].get("ok") is not True:
                    raise RuntimeError(name + "-preflight-result-invalid")
                save()
            if evidence["source"]["business_file_sha256"] != evidence["target"]["business_file_sha256"]:
                raise RuntimeError("source-target-business-files-differ")
            evidence["ok"] = True
        except Exception as error:
            evidence["error"] = type(error).__name__ + ":" + str(error)
            raise
        finally:
            save()
    print(json.dumps({"ok": True, "evidence": str(path), "remote_mutations": False,
                      "model_request_sent": False}))


if __name__ == "__main__":
    main()
