#!/usr/bin/env python3
"""Continue the recorded source Redis shell-wrapper retirement, once."""
import hashlib
import pathlib
import types

BASE_SHA='6f389c1c3eaf8bd96f1b5c096c403e53b61cb25838a612407bfd0c92723af227'

SUPPLEMENT = r'''
 prior=scope/'recovered-runtime-finalization-20261017'
 M.E.need(prior.is_dir() and not prior.is_symlink() and M.S.private_bytes(prior/'state.json')==M.S.private_bytes(scope/'state.json'),'PREVIOUS_FINALIZATION_NOT_BOUND')
 row=b.source_ops._resource('redis');ops=b.source_ops._ledger();op=ops['operations'].get('1cbf783b8b944a2814839cfa9c51946f',{})
 M.E.need(op.get('phase')=='pending' and op.get('request')=={'action':'redis_role_persist','arguments':{}} and op.get('steps')=={'sigterm':'accepted'},'PRIOR_REDIS_RETIREMENT_NOT_PENDING')
 M.E.need(row['State']['Running'] is True and row['State']['Pid']==972827 and row['HostConfig']['RestartPolicy']['Name']=='no','SOURCE_REDIS_PROCESS_CHANGED')
 for kind in ('app','postgres'):b.source_ops._stopped(b.source_ops._resource(kind))
 pair=b.source_ops._redis_pair();source,target=pair['source'],pair['target']
 M.E.need(source['role']=='slave' and source['run_id']=='08e111c3724844873b08ee96322628d6a2724ee3' and source['master_host']=='172.18.0.1' and source['master_port']=='26379' and source['master_link_status']=='up' and target['role']=='master' and target['run_id']=='39d29826637d6bcf89b9630612f7fd0107260394' and not b.source_ops._redis_clients()['unknown_clients'],'SOURCE_REPLICA_NOT_SAFE_TO_RETIRE')
 write(saved/'redis-shutdown.intent.json',M.B.canonical({'container_id':row['Id'],'source_run_id':source['run_id'],'target_run_id':target['run_id'],'prior_op_id':'1cbf783b8b944a2814839cfa9c51946f','command':'SHUTDOWN SAVE','no_repeat':True}))
 try:
  reply=b.source_ops._redis('SHUTDOWN','SAVE')
 except Exception as error:
  write(saved/'redis-shutdown.unknown.json',M.B.canonical({'error_type':type(error).__name__,'inspect_only':True}));raise
 write(saved/'redis-shutdown.acknowledged.json',M.B.canonical({'command_returned':True,'reply_sha256':M.B.sha(reply),'prior_signal_not_repeated':True}))
 for _ in range(40):
  stopped=b.source_ops._resource('redis')
  if not stopped['State']['Running']:
   b.source_ops._stopped(stopped);break
  time.sleep(.1)
 else:raise RuntimeError('SOURCE_REDIS_EXIT_PENDING_INSPECT_ONLY')
'''


def derive(raw):
    if hashlib.sha256(raw).hexdigest()!=BASE_SHA:raise RuntimeError('reviewed-finalization-base-changed')
    text=raw.decode()
    if text.count('recovered-runtime-finalization-20261017')!=4:raise RuntimeError('scope-anchor-changed')
    text=text.replace('recovered-runtime-finalization-20261017','completed-runtime-finalization-20261017')
    anchor=" engine=M.E.DataHandover(state,b);engine.retire_source_redis()"
    if text.count(anchor)!=1:raise RuntimeError('retirement-anchor-changed')
    text=text.replace(anchor,SUPPLEMENT+anchor)
    anchor="'source_tombstones':{'operations':ops,'deployment_fence_raw_sha256':M.B.sha(fence)}"
    if text.count(anchor)!=1:raise RuntimeError('supplement-proof-anchor-changed')
    text=text.replace(anchor,"'source_tombstones':{'operations':ops,'deployment_fence_raw_sha256':M.B.sha(fence),'redis_shutdown_receipt_sha256':M.B.sha(M.S.private_bytes(saved/'redis-shutdown.acknowledged.json'))}")
    module=types.ModuleType('completed_runtime_finalizer');module.__file__=__file__
    exec(compile(text,__file__,'exec'),module.__dict__)
    return module


def main(argv=None):
    module=derive(pathlib.Path(__file__).with_name('finalize_recovered_runtime.py').read_bytes())
    return module.main(argv)


if __name__=='__main__':raise SystemExit(main())
