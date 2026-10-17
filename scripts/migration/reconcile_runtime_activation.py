#!/usr/bin/env python3
"""Continue only the exact accepted activation after OPERATION_BUSY."""
import hashlib
import pathlib
import types

BASE_SHA = 'ad9ff3258388625b0e70e5fd8c278f7c8a1deb3ff02f72651aaa4368d6caebb7'


def derive():
    folder=pathlib.Path(__file__).resolve().parent
    raw=(folder/'finish_stopped_redis_runtime.py').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=BASE_SHA:raise RuntimeError('reviewed-continuation-changed')
    text=raw.decode().replace('stopped-redis-runtime-finalization-20261017','reconciled-runtime-finalization-20261017')
    module=types.ModuleType('reconciled_activation');module.__file__=__file__
    exec(compile(text,__file__,'exec'),module.__dict__)
    result=module.derive((folder/'finalize_recovered_runtime.py').read_bytes())
    anchor=" activation=b.control('final:background.activate','background.activate')"
    replacement=r'''
 # Replay this immutable RPC identity only. TargetRPC either executes the
 # not-started effect once or observes an already started effect; never POSTs
 # an effect_started operation again. Retry solely a lock-busy response.
 original=b.store.read('transport')['operations']['0235e60404de4ea5932903852fcc1b29']
 M.E.need(original['phase']=='unknown' and original['request_sha256']=='de4ceef0603a6c67a919ab60a205f48d3dab152631d7ea73679ae1d42ea5a17b' and original['response']['result']['reason']=='OPERATION_BUSY','EXACT_LOCK_BUSY_ACTIVATION_REQUIRED')
 for attempt in range(25):
  try:
   activation=b.control('final:background.activate','background.activate');break
  except M.E.Unknown:
   current=b.store.read('transport')['operations']['0235e60404de4ea5932903852fcc1b29']
   M.E.need(current['request']==original['request'] and current.get('response',{}).get('result',{}).get('reason')=='OPERATION_BUSY','ACTIVATION_RESULT_REQUIRES_INSPECTION')
   if attempt==24:raise
   time.sleep(.15)
'''
    result.SOURCE=module.replace_once(result.SOURCE,anchor,replacement)
    compile(result.SOURCE,'reconciled-source','exec')
    return result


def main(argv=None):return derive().main(argv)


if __name__=='__main__':raise SystemExit(main())
