#!/usr/bin/env python3
"""One fixed continuation of a proved pre-signal abort; offline by default.

The predecessor directory and original UNKNOWN checkpoints remain byte-for-byte
unchanged. Only positive work counters may wait/reset the quiet proof. Every
other failed prerequisite refuses. One durable signal intent permits one TERM.
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

PREDECESSOR_SHA = '26bf5e008e0fe8c6a777302b9558fc6955b199adb721ee299b6e4b6e70566211'
PREDECESSOR_LABEL = 'quiet-term-resolution-20261017'
LABEL = 'quiet-term-preflight-abort-continuation-20261017'
MANIFEST = {
    'authorization.json': 'fd7fcfd7c7d8d63747e86856deaf364b3f139edf9ec85a59e3d1e4e74d892013',
    'failure.traceback.txt': 'c3d112cd5543014df53a1dacc28263379e0a618590dceaf643406ce36901cb7a',
    'pinned-files-before.json': '5d83764cf3a6b9f97646b92eaaa7ba54367401404bc245a046122f784066b797',
    'rolling-state-before.json': 'bcfab87f4d51e2f24aa0b20c9380475fc828d25d9ce7e86ae5ec21f8076fc2a6',
    'target-ledger-before.json': 'abd784df64eaf0fb3d5d622f8025a221657078b731cf97d9859cee3ba8991ce3',
    'sample-01.json': '70a63a644f9a59acd82d31c10f48312880755ef711df07b97bede3ff589052b6',
    'sample-02.json': '10c6b32e3f89629ee8f61896e49e2b5d65c2f954c876468621a84eb570f3cbb2',
    **{'sample-%02d.logs.stdout' % n: '2176f9acf5aa5ad4010d14c136cc68d2b6ff8bf33603f236b96a877c89a38a16' for n in (1, 2, 3)},
    **{'sample-%02d.logs.stderr' % n: '2e797966df5e0bcd067d171bcc184adca6d5ea9d4e9ea9d99b92181fd90505fa' for n in (1, 2, 3)},
}

CONTINUATION_HELPERS = r'''
def verify_predecessor(scope):
 previous=scope/PREDECESSOR_LABEL
 need(previous.is_dir() and not previous.is_symlink(),'predecessor-directory-invalid')
 need({path.name for path in previous.iterdir()}==set(PREDECESSOR_MANIFEST),'predecessor-file-set-changed-or-signal-evidence')
 raw={name:H.private_read(previous/name,uid=0,limit=H.MAX_LOG) for name in PREDECESSOR_MANIFEST}
 need(all(sha(value)==PREDECESSOR_MANIFEST[name] for name,value in raw.items()),'predecessor-exact-manifest-changed')
 need(raw['rolling-state-before.json']==state_raw and raw['target-ledger-before.json']==ledger_raw,
      'predecessor-checkpoints-do-not-match')
 authorization=json.loads(raw['authorization.json'])
 need(authorization.get('kind')=='independent-root-quiet-TERM-resolution' and authorization.get('owner')==cfg['owner'] and
      authorization.get('original_intent_id')==payload['intent_id'] and authorization.get('old_container_id')==payload['cid'] and
      authorization.get('old_process')==payload['old_process'] and authorization.get('new_container_id')==payload['new_cid'] and
      authorization.get('new_instance_id')==payload['new_instance'] and authorization.get('maximum_new_TERM_signals')==1 and
      authorization.get('does_not_prove_original_signal_never_sent') is True,'predecessor-authorization-invalid')
 need(raw['failure.traceback.txt'].rstrip().endswith(b'RuntimeError: complete-old-quiet-proof-required'),
      'predecessor-not-exact-quiet-preflight-abort')
 return raw
def predecessor_unchanged():
 need(verify_predecessor(scope)==previous_raw,'predecessor-bytes-raced')
def pure_busy(proof):
 old=proof['observation']['old'];work=old.get('work',{});runtime=proof['old_runtime']
 need(old.get('state')=='running' and old.get('ready') is True and old.get('serving') is True and
      old.get('draining') is False and old.get('process')==payload['old_process'] and
      old.get('tcp_scope')=='all-accepted-including-idle' and set(work)==set(C.QUIET_FIELDS) and
      all(type(work[key]) is int and work[key]>=0 for key in C.QUIET_FIELDS),'busy-old-proof-invalid')
 runtime_counts(runtime)
 need(any(work.values()) or any(runtime[key]>0 for key in ('active_requests','active_hijacked_connections')) or
      runtime['ingress']['queued_requests']>0,'busy-without-positive-work')
def runtime_counts(runtime):
 need(runtime.get('ready') is True and runtime.get('serving') is True and runtime.get('draining') is False and
      all(type(runtime.get(key)) is int and runtime[key]>=0 for key in ('active_requests','active_hijacked_connections')) and
      type(runtime.get('ingress',{}).get('queued_requests')) is int and runtime['ingress']['queued_requests']>=0,
      'old-runtime-not-healthy-nonnegative')
 return runtime['active_requests']+runtime['active_hijacked_connections']+runtime['ingress']['queued_requests']
'''

APPLY_CYCLE = r'''
   samples=[];seen=set();boot=None;deadline=time.monotonic()+30
   while True:
    proof=sample()
    if proof['mode'] in ('draining','exited'):
     finish(False,wait_exit());break
    if proof['mode']=='busy':
     pure_busy(proof);samples=[];seen=set();boot=None
    else:
     o=proof['observation']
     need(o['sample_id'] not in seen and (boot is None or boot==o['boot_id']),'quiet-sample-not-fresh-or-same-boot')
     samples.append(proof);seen.add(o['sample_id']);boot=o['boot_id']
     if len(samples)>=3 and samples[-1]['tick']-samples[0]['tick']>=2:
      last=sample()
      if last['mode'] in ('draining','exited'):
       finish(False,wait_exit());break
      if last['mode']=='busy':
       pure_busy(last);samples=[];seen=set();boot=None
      else:
       need(last['observation']['boot_id']==boot and last['observation']['sample_id'] not in seen,'final-sample-not-fresh')
       strict_old(h._docker('container',payload['cid']),h)
       rt=read_old_runtime();clients=runtime_clients(pathlib.Path('/proc'),payload['cid'],H.OLD_NAME)
       need(not clients['active'],'old-runtime-operation-in-flight')
       unchanged()
       if rt.get('draining') is True:
        finish(False,wait_exit());break
       if runtime_counts(rt):samples=[];seen=set();boot=None
       else:
        write_new(saved/'three-complete-quiet-samples.json',canonical(samples))
        argv=['/usr/bin/docker','kill','--signal','TERM',payload['cid']]
        write_new(saved/'signal.intent.json',canonical({**authorization,'status':'INTENT','at':time.time(),
         'quiet_proof_sha256':sha(canonical(samples)),'final_sample_sha256':sha(canonical(last)),
         'final_old_runtime':rt,'final_runtime_clients':clients}))
        checkpoint_cas()
        try:result=subprocess.run(argv,capture_output=True,timeout=5,env=H.SAFE_ENV)
        except subprocess.TimeoutExpired as error:
         write_new(saved/'docker-term.stdout',error.stdout or b'');write_new(saved/'docker-term.stderr',error.stderr or b'');raise
        write_new(saved/'docker-term.stdout',result.stdout);write_new(saved/'docker-term.stderr',result.stderr)
        write_new(saved/'docker-term.result.json',canonical({'returncode':result.returncode}))
        need(result.returncode==0,'TERM-outcome-unknown-inspect-private-diagnostics')
        finish(True,wait_exit());break
    if time.monotonic()>=deadline:
     finish(False,{'exited_cleanly':False,'pending':True});break
    time.sleep(min(1,deadline-time.monotonic()))
'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError('frozen-helper-assembly-anchor-changed')
    return text.replace(old, new, 1)


def assemble(raw):
    if hashlib.sha256(raw).hexdigest() != PREDECESSOR_SHA:
        raise RuntimeError('frozen-predecessor-helper-changed')
    namespace = {'__name__': 'frozen_predecessor', '__file__': str(pathlib.Path(__file__).with_name('complete_quiet_recovery_term.py'))}
    exec(compile(raw, namespace['__file__'], 'exec'), namespace)
    target = namespace['TARGET']
    target = replace_once(target, ' checkpoint_cas()\n need(sha(', ' checkpoint_cas()\n predecessor_unchanged()\n need(sha(')
    target = replace_once(target, "quiet=complete_quiet(o,required=payload['mode']=='apply')", 'quiet=complete_quiet(o,required=False)')
    target = replace_once(target, "   if payload['mode']=='apply':need(runtime_idle,'old-runtime-not-idle-ready')\n", '')
    anchor = " state=store.read();state_raw=trusted(scope/'state.json',0o600);ledger_raw=trusted(root/'rpc/state.json',0o600)\n"
    target = replace_once(target, anchor, anchor+' previous_raw=verify_predecessor(scope)\n')
    target = replace_once(target, "'kind':'independent-root-quiet-TERM-resolution',", "'kind':'fixed-preflight-abort-TERM-continuation',\n    'predecessor_helper_sha256':PREDECESSOR_HELPER_SHA,'predecessor_manifest':PREDECESSOR_MANIFEST,")
    start = target.index('   samples=[];seen=set();boot=None\n')
    end = target.index('  except Exception:\n', start)
    target = target[:start]+APPLY_CYCLE+target[end:]
    helpers = namespace['HELPERS']+'\nPREDECESSOR_LABEL='+repr(PREDECESSOR_LABEL)+'\nPREDECESSOR_HELPER_SHA='+repr(PREDECESSOR_SHA)+'\nPREDECESSOR_MANIFEST='+repr(MANIFEST)+'\n'+CONTINUATION_HELPERS
    compile(helpers+target, 'fixed-preflight-abort-TERM-continuation', 'exec')
    return namespace, helpers, target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--apply', action='store_true'); modes.add_argument('--inspect', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply and not args.inspect:
        print(json.dumps({'offline': True, 'predecessor_directory_preserved': True,
                          'maximum_new_TERM_signals': 1, 'busy_wait_seconds': 30}))
        return 0
    folder = pathlib.Path(__file__).parent
    old, helpers, target = assemble((folder/'complete_quiet_recovery_term.py').read_bytes())
    for name, digest in (('rolling_app_host.py', old['HOST_SHA']), ('rolling_app_recovery.py', old['CORE_SHA'])):
        if hashlib.sha256((folder/name).read_bytes()).hexdigest() != digest:raise RuntimeError('reviewed-rolling-program-changed')
    prior = runpy.run_path(str(folder/'prepare_retry.py'))
    channel = runpy.run_path(str(folder/'prepare_channel.py'))
    common = runpy.run_path(str(folder/'recover_source_guard.py'))['COMMON']
    script = common+helpers+target
    payload = {'mode': 'apply' if args.apply else 'inspect', 'cid': old['CID'], 'new_cid': old['NEW_CID'],
               'new_instance': old['NEW_INSTANCE'], 'intent_id': old['INTENT_ID'], 'label': LABEL,
               'host_sha': old['HOST_SHA'], 'core_sha': old['CORE_SHA'], 'ledger_sha': old['LEDGER_SHA'],
               'state_sha': old['STATE_SHA'], 'old_process': old['PROCESS'], 'resolution_id': secrets.token_hex(16)}
    path = prior['FOLDER']/(LABEL+'.json'); marker = path.with_suffix('.intent.json')
    if args.apply:
        if any(p.exists() or p.is_symlink() for p in (path, marker)):raise RuntimeError('existing-continuation-inspect-only')
        prior['private_json'](marker, {'script_sha256': hashlib.sha256(script.encode()).hexdigest(),
                'predecessor_helper_sha256': PREDECESSOR_SHA, 'predecessor_manifest': MANIFEST,
                'container_id': old['CID'], 'resolution_id': payload['resolution_id'], 'no_repeat': True})
    try:
        result = subprocess.run(channel['TARGET']+['sudo python3 -I -c '+shlex.quote(script)],
                                input=json.dumps(payload).encode(), capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        if args.apply:
            prior['private_json'](path, {'ok': False, 'error_type': type(error).__name__, 'inspect_before_retry': True,
                    'stdout_b64': base64.b64encode(getattr(error, 'stdout', None) or b'').decode(),
                    'stderr_b64': base64.b64encode(getattr(error, 'stderr', None) or b'').decode()})
        raise RuntimeError('continuation-outcome-unknown-inspect-only') from None
    if args.inspect:
        if result.returncode != 0:raise RuntimeError('continuation-inspection-unavailable')
        print(json.dumps(json.loads(result.stdout), sort_keys=True)); return 0
    prior['private_json'](path.with_suffix('.private-diagnostics.json'), {'returncode': result.returncode,
            'stdout_b64': base64.b64encode(result.stdout).decode(), 'stderr_b64': base64.b64encode(result.stderr).decode()})
    value = prior['private_result'](path, result)
    print(json.dumps(value, sort_keys=True)); return 0 if value.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
