#!/usr/bin/env python3
"""Fixed read-only preflight and one-shot DNS entry repair; default offline.

Only DNS entry content may change. AWS forwards to the Tencent application;
all retired business containers must remain stopped and data stays Tencent.
"""
import argparse
import hashlib
import json
import pathlib
import runpy
import shlex
import subprocess
import time
import types

HELPER_SHA = '3028771b67a61e21c222fc4dba9973a85219436c8193de2e7ea8d798043d461d'
DNS_HELPER_SHA = '62abe51ea63a11f0e65d487e9134425066eb82e8ea6ee2c43d372d126ed887e0'
ROOT = pathlib.Path(__file__).parent
SOURCE_READ = r'''
import pathlib,json,subprocess,hashlib,http.client,types,os,stat
def require(ok,code):
 if not ok:raise RuntimeError(code)
def command(argv):
 p=subprocess.run(argv,capture_output=True,timeout=8)
 require(p.returncode==0,'SOURCE_READ_FAILED');return p.stdout
def private(path):
 path=pathlib.Path(path)
 require(not any(p.is_symlink() for p in (path,*path.parents)),'SOURCE_SYMLINK')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,'rb') as stream:
  info=os.fstat(stream.fileno());raw=stream.read(8*1024**2+1)
 require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and not info.st_mode&0o022 and len(raw)==info.st_size,'SOURCE_FILE_UNTRUSTED')
 return raw
state=json.loads(private('/var/lib/sub2api-migration-20261016/runner/state.json'))
require(state['phase']=='COMPLETE_WITH_WAIT_BREACH' and state['pg']==state['redis']=='TENCENT' and state['window'] is None,'SOURCE_AUTHORITY_CHANGED')
expected={'sub2api':('cba57e76f48d0e4cfb0280bcc963205dc910a0d0b4bd752e555bb2fa44b9f55e',0),
 'sub2api-postgres':('c99ec20ad4468b29698083cdc6827c853141717f0cf0a7a98bfed32e788414ee',0),
 'sub2api-redis':('e2f41a97ee0a36a9a5bd3e36b8f65874411419b3dae3867097323b3e730c6835',127)}
containers={}
for name,(identity,exit_code) in expected.items():
 row=json.loads(command(['/usr/bin/docker','container','inspect',name]))[0]
 require(row['Id']==identity and row['State']['Running'] is False and row['State']['Status']=='exited' and row['State']['ExitCode']==exit_code and row['HostConfig']['RestartPolicy']['Name']=='no','SOURCE_BUSINESS_NOT_RETIRED')
 containers[name]={'running':False,'restart_policy':'no','id':identity,'exit_code':exit_code}
path='/usr/local/libexec/sub2api-migration-20261016/activate_entry_bridge.py'
raw=private(path);require(hashlib.sha256(raw).hexdigest()==__BRIDGE_SHA__,'BRIDGE_PROGRAM_CHANGED')
m=types.ModuleType('inspected_bridge');m.__file__=path
exec(compile(raw,path,'exec'),m.__dict__)
command(m.BASE+['-C','OUTPUT']+m.RULE)
_,owned,output=m.rule_lines(command(m.BASE+['-S']))
require(len(owned)==1 and output[0]==owned[0],'BRIDGE_NOT_FIRST_UNIQUE')
units={}
for name,pid in {'caddy.service':1284574,'codex-remote-relay.service':1307343,'sub2api-migration-channel-20261016.service':2288434}.items():
 row=dict(line.split('=',1) for line in command(['/usr/bin/systemctl','show',name,'--property=ActiveState,SubState,MainPID']).decode().splitlines())
 require(row=={'ActiveState':'active','SubState':'running','MainPID':str(pid)},'SOURCE_ENTRY_PROCESS_CHANGED')
 units[name]=row
unit=private('/etc/systemd/system/sub2api-migration-channel-20261016.service').decode()
require('ubuntu@43.165.177.136' in unit and '-L 127.0.0.1:18081:127.0.0.1:8082' in unit,'CHANNEL_DESTINATION_CHANGED')
c=http.client.HTTPConnection('127.0.0.1',18081,timeout=3)
try:
 c.request('GET','/health');reply=c.getresponse();body=reply.read(65537)
 require(reply.status==200 and len(body)<=65536,'BRIDGE_NOT_HEALTHY')
finally:c.close()
print(json.dumps({'phase':state['phase'],'pg':state['pg'],'redis':state['redis'],'source_containers':containers,'units':units,'bridge_verified':True,'bridge_target':'43.165.177.136'}))
'''
TARGET_READ = r'''
runtime=b.app_control('runtime.status')['body']
pg=b.app_control('postgres.status')['body'];redis=b.app_control('redis.status')['body']
require(runtime['ready'] is True and runtime['serving'] is True and runtime['draining'] is False,'TARGET_NOT_SERVING')
require(pg['endpoint']=='target' and pg['paused'] is False and redis['address']=='127.0.0.1:26379' and redis['paused'] is False and redis['pubsub_cache_ready'] is True,'TARGET_RUNTIME_ENDPOINT_CHANGED')
print(json.dumps({'pg_authority':ledger['pg']['authority'],'redis_authority':ledger['redis']['authority'],'application_ready':True,'runtime_endpoints':{'postgres':'target','redis':'target'},'persisted_endpoints':{'postgres':'target','redis':'target'},'instance_id':runtime['instance_id']}))
'''


def require(value, code):
    if not value:
        raise RuntimeError(code)


def load_pinned(name, expected):
    path=ROOT/name;raw=path.read_bytes()
    require(not path.is_symlink() and hashlib.sha256(raw).hexdigest()==expected,'LOCAL_HELPER_CHANGED')
    module=types.ModuleType(name[:-3]);module.__file__=str(path)
    exec(compile(raw,str(path),'exec'),module.__dict__)
    return module


def preflight(base, channel, evidence):
    script=SOURCE_READ.replace('__BRIDGE_SHA__',repr(hashlib.sha256((ROOT/'activate_entry_bridge.py').read_bytes()).hexdigest()))
    def read(name, host, program):
        result=base.run_remote(host,program,timeout=45)
        require(result['returncode']==0 and result['failure'] is None and not result['stderr'],'READ_ONLY_PREFLIGHT_FAILED')
        row=json.loads(result['stdout']);base.write(evidence/name,json.dumps(row,sort_keys=True).encode())
        return row
    source=read('source.json',channel['SSH_SOURCE'],script)
    target=read('target.json',channel['TARGET'],base.CHECK+TARGET_READ)
    health=subprocess.run(['curl','--noproxy','*','--resolve','api.b4im.com:443:13.231.143.136','-fsS','--connect-timeout','5','--max-time','12','-o','/dev/null','-w','%{http_code} %{ssl_verify_result}','https://api.b4im.com/health'],capture_output=True,timeout=15)
    require(health.returncode==0 and health.stdout==b'200 0','DIRECT_AWS_ENTRY_NOT_HEALTHY')
    return {'v':1,'observed_at':time.time(),'target_address':'43.165.177.136',
        **{k:target[k] for k in ('pg_authority','redis_authority','application_ready','runtime_endpoints','persisted_endpoints')},
        'source_containers':{name:{'running':row['running'],'restart_policy':row['restart_policy']} for name,row in source['source_containers'].items()},
        'source_entry':{'address':'13.231.143.136','domain':'api.b4im.com','https_status':200,'tls_verify_result':0,
            'bridge_target':source['bridge_target'],'bridge_verified':source['bridge_verified'],
            'caddy_active':True,'relay_active':True,'channel_active':True}}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true')
    if not p.parse_args(argv).apply:
        print(json.dumps({'offline':True,'data_rollback':False,'only_mutation':'fixed A-record content: Tencent -> existing AWS-to-Tencent bridge'}));return
    base=load_pinned('backup_tencent_final.py',HELPER_SHA)
    helper=load_pinned('restore_reachable_entry.py',DNS_HELPER_SHA)
    channel=runpy.run_path(str(ROOT/'prepare_channel.py'))
    folder=base.private_folder(helper.RUN)
    evidence=base.private_folder(helper.RUN.with_name(helper.RUN.name+'-preflight'))
    base.write(evidence/'orchestrator.py',pathlib.Path(__file__).read_bytes())
    report=helper.recover(helper.Client(helper.load_private_token()),folder,lambda:preflight(base,channel,evidence))
    print(json.dumps({'ok':report['ok'],'patch_count':report['patch_count'],'outcome':report['outcome'],'data_rollback':False,'business_authority':'TENCENT','temporary_aws_entry_dependency':True,'report_sha256':helper.sha(report),'evidence':str(folder)}))
    require(report['ok'],'ENTRY_RECOVERY_INCOMPLETE_INSPECT_EVIDENCE_NO_RETRY')


if __name__=='__main__':
    main()
