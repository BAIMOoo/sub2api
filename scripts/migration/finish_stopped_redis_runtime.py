#!/usr/bin/env python3
"""Adopt Tencent using exact evidence of the already stopped legacy Redis.

This continuation sends no further source signal or Redis command. It preserves
Docker's exit 127, the pending signal operation, and the failed wait objective.
"""
import hashlib
import pathlib
import types

BASE_SHA = '6f389c1c3eaf8bd96f1b5c096c403e53b61cb25838a612407bfd0c92723af227'
PREVIOUS_KEEPER = '544a9a9b8625bf7e3ec00f85b0cdda2804d554d79bd63abd22c3dc49c288786e'
KEEPER_SHA = '7baf987354fcca8a6117b61f292d1dc8dceef07c15999a0ea5cd923ac4dd59bf'
KEEPER_TEST_SHA = 'd21093e82f47a82e5ea0e8e0d2e545387bddbd9e3aefccfc47316b421873b83d'

OBSERVE_STOPPED = r'''
 engine=M.E.DataHandover(state,b)
 prior=scope/'completed-runtime-finalization-20261017'
 M.E.need(M.S.private_bytes(prior/'state.json')==M.S.private_bytes(scope/'state.json'),'PRIOR_STATE_CHANGED')
 ack=M.S.private_bytes(prior/'redis-shutdown.acknowledged.json')
 M.E.need(M.B.sha(ack)=='d3d04d6c1629590847db0c77200e1795b3b3d27a7a4fc37183d77e97ba5b7b18','SHUTDOWN_ACK_CHANGED')
 intent=M.S.private_bytes(prior/'redis-shutdown.intent.json')
 M.E.need(M.B.sha(intent)=='97ac17f2c0ac564b0614e56aeebd1da788237415a18d43a8d252e7dbd751d88a','SHUTDOWN_INTENT_CHANGED')
 stopped=b.source_ops._resource('redis')
 M.E.need(stopped['Id']=='e2f41a97ee0a36a9a5bd3e36b8f65874411419b3dae3867097323b3e730c6835' and stopped['State']['Running'] is False and stopped['State']['Status']=='exited' and stopped['State']['Pid']==0 and stopped['State']['ExitCode']==127 and stopped['State']['OOMKilled'] is False and stopped['HostConfig']['RestartPolicy']['Name']=='no','EXACT_SOURCE_REDIS_EXIT_REQUIRED')
 import subprocess
 logs=subprocess.run(['/usr/bin/docker','logs','--since','2026-10-17T04:48:00Z',stopped['Id']],capture_output=True,check=True,timeout=5)
 M.E.need(M.B.sha(logs.stdout)=='739b00e22555c6a01e4af138dcd1e3b0cf0b021eede6bff2d98f6cb1a5d41e74' and M.B.sha(logs.stderr)=='9383ce5789fec964a23935052fa1a9f06850125a518043ef4d4a4b4d096bf510','EXACT_REDIS_SHUTDOWN_LOGS_REQUIRED')
 M.E.need(b'User requested shutdown' in logs.stdout and b'DB saved on disk' in logs.stdout and b'Redis is now ready to exit, bye bye' in logs.stdout,'REDIS_CLEAN_CHILD_SHUTDOWN_REQUIRED')
 write(saved/'redis-child-shutdown.log',logs.stdout);write(saved/'redis-shell-exit.log',logs.stderr)
 shell_exit={'container_id':stopped['Id'],'container_exit_code':127,'shutdown_ack_sha256':M.B.sha(ack),'shutdown_log_sha256':M.B.sha(logs.stdout),'wrapper_log_sha256':M.B.sha(logs.stderr),'prior_op_id':'1cbf783b8b944a2814839cfa9c51946f','prior_op_phase':'pending'}
'''


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise RuntimeError('reviewed-finalizer-anchor-changed')
    return text.replace(before, after)


def derive(raw):
    if hashlib.sha256(raw).hexdigest() != BASE_SHA:
        raise RuntimeError('reviewed-finalizer-base-changed')
    module=types.ModuleType('stopped_redis_runtime_finalizer');module.__file__=__file__
    text=raw.decode().replace('recovered-runtime-finalization-20261017','stopped-redis-runtime-finalization-20261017')
    exec(compile(text,__file__,'exec'),module.__dict__)
    source=replace_once(module.SOURCE,' engine=M.E.DataHandover(state,b);engine.retire_source_redis()',OBSERVE_STOPPED)
    source=replace_once(source,"row['exit_code']==0 and row['oom_killed'] is False", "row['exit_code']==(127 if name=='redis' else 0) and row['oom_killed'] is False")
    source=replace_once(source,
        "M.E.need(len(rows)==1 and rows[0]['phase']=='complete' and signal in rows[0]['steps'],'SOURCE_OWNED_RETIREMENT_REQUIRED')",
        "M.E.need(len(rows)==1 and rows[0]['phase']==('pending' if action=='redis_role_persist' else 'complete') and signal in rows[0]['steps'],'SOURCE_OWNED_RETIREMENT_REQUIRED')\n  if action=='redis_role_persist':M.E.need(ops['operations'].get(shell_exit['prior_op_id'])==rows[0] and rows[0]['steps']=={'sigterm':'accepted'} and rows[0]['request']=={'action':'redis_role_persist','arguments':{}},'EXACT_PRIOR_SIGNAL_OPERATION_REQUIRED')")
    source=replace_once(source,"'deadline_breached':True}","'deadline_breached':True,'source_redis_shell_exit':shell_exit}")
    source=replace_once(source,"'source_tombstones':{'operations':ops,'deployment_fence_raw_sha256':M.B.sha(fence)}", "'source_tombstones':{'operations':ops,'deployment_fence_raw_sha256':M.B.sha(fence),'source_redis_shell_exit':shell_exit}")
    target_original=module.derive_target
    def target(raw):
        text=target_original(raw).replace(PREVIOUS_KEEPER,KEEPER_SHA)
        return replace_once(text,'K.validate_arm(arm,cfg,R,R.sha(raw));',"R.require(proof['verification']['source_redis_shell_exit']==proof['source_tombstones']['source_redis_shell_exit']==K.SOURCE_REDIS_SHELL_EXIT,'EXACT_SHELL_RETIREMENT_PROOF_REQUIRED')\narm['complete']['source_redis_shell_exit']=proof['verification']['source_redis_shell_exit']\nK.validate_arm(arm,cfg,R,R.sha(raw));")
    module.SOURCE=source;module.derive_target=target
    module.KEEPER_SHA=KEEPER_SHA;module.KEEPER_TEST_SHA=KEEPER_TEST_SHA
    compile(source,'stopped-source','exec')
    compile(target(pathlib.Path(__file__).with_name('finalize_runtime.py').read_bytes()),'stopped-target','exec')
    return module


def main(argv=None):
    return derive(pathlib.Path(__file__).with_name('finalize_recovered_runtime.py').read_bytes()).main(argv)


if __name__=='__main__':raise SystemExit(main())
