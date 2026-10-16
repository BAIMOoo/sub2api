#!/usr/bin/env python3
"""Once-only control-script upgrade at the recovered, unused cohort boundary.

No application restart, request pause, budget reset, DNS or data handover.
Unknown transport outcomes require inspection; an intent is never replayed.
"""
import argparse
import base64
import hashlib
import json
import pathlib
import runpy
import shlex
import subprocess

OLD = {
    "target_rpc.py": "60cea6e567c8d1990c4bad86205a4c351727499b5a20b48ba8dccd2a1a56c463",
    "runner_backend.py": "afb43d190c7e80ed2e9759c88ce5d0b6cefbbf7755eeb1b04f53a0f7de131dd1",
    "runner_service.py": "fb07110ea461a9872a90626ea435b7c22c18a0a04bca40523279aed55d3f0c63",
}
NEW = {
    "target_rpc.py": "4681f2e7203623b39d8f861811cb00cace10dd17f61bc447a3821196587acb9c",
    "runner_backend.py": "8d1f7e6064e51c2392e977603e47ba2684db5c36096bc3e944041b1d76218bbd",
    "runner_service.py": "7318557010904f289e05b8974f0ff6e23cffdcb3f372f82763824c9fc0eaa5a1",
}
DEPENDENCIES = {
    "durable_runner.py": "18f0184ce061b4de57146a1f1024ab576622adc521dc16f0a9f0080fb76229d4",
    "runner_guard.py": "e95be2c5e3bb01c842708c026c000b617aeca0ac26a049260a7d0010ddb8f200",
    "source_operations.py": "b6cfab63741c0f2dfb4d5c36445f9d14d62c845e3c2c6e1b0bd849eddbf3ec97",
}
TARGET_CONFIG_SHA = "f7e50f436ca8cbbb9542e9a7106da64275f5791a484ac7c85a1f25c2c1254ae1"
COHORT = {"id": "deb5f0b6ba505ed70b769d7311e929fe",
          "record_sha256": "bc4b2a900a8bf1c0d789c6c0b909128f637d9707bb5031bf0eadd0377deddebe"}

VALIDATION = r'''
def need(value,code):
 if not value:raise RuntimeError(code)
def previous_state(state):
 need(state.get('v')==1 and state.get('run')=='20261016' and state.get('owner')==payload['owner'] and
      state.get('phase')=='SOURCE_DATA' and state.get('attempt')==1 and state.get('route_fenced') is True and
      state.get('pg')==state.get('redis')=='AWS' and state.get('window') is None and
      state.get('deadline_breached') is False and state.get('pause_seconds')==payload['prior_pause'] and
      state.get('budget_cohort')==payload['cohort'],'unused-exact-cohort-required')
'''

SOURCE_PREFLIGHT = r'''
for name,value in {**payload['old'],**payload['dependencies']}.items():
 need(sha(trusted(lib/name,0o644))==value,'source-program-changed')
M=load('admission_preflight',lib/'runner_service.py')
with M.execution_lock():
 b,state=source_idle(M);budget=M.E.budget_accounting(state)
 need(budget['cohort_pause_seconds']==0,'cohort-budget-already-consumed')
 print(json.dumps({'ok':True,'state_raw_sha256':sha(trusted(scope/'state.json',0o600)),
                   'target_epoch':b.store.read('transport')['target_epoch'],'budget':budget},sort_keys=True))
'''

TARGET_UPGRADE = r'''
path=lib/'target_rpc.py';cfgpath=root/'rpc/config.json';ledgerpath=root/'rpc/state.json'
need(sha(trusted(path,0o700))==payload['old']['target_rpc.py'],'target-program-changed')
need(sha(trusted(cfgpath,0o600))==payload['target_config_sha'],'target-config-changed')
M=load('admission_target_old',path);cfg=M.load_config();b=M.LocalBackend(cfg);store=M.FileStore(cfg)
raw=base64.b64decode(payload['programs']['target_rpc.py'],validate=True)
need(sha(raw)==payload['new']['target_rpc.py'],'target-new-digest');compile(raw,str(path),'exec')
with store.mutation_lock():
 ledger=store.read();ledgerraw=trusted(ledgerpath,0o600)
 need(ledger['pg']['authority']==ledger['redis']['authority']=='AWS' and not ledger['pg'].get('promotion_intent') and
      ledger['pg'].get('arm',{}).get('status')=='CANCELLED' and
      all(op['phase']=='COMPLETE' for op in ledger['operations'].values()) and
      all(op.get('cancelled') is True for op in ledger['operations'].values() if op['request']['action']=='app.quiesce'),
      'target-not-recovered')
 need(b.endpoint_state()=={'v':1,'postgres':'source','redis':'source'},'target-endpoints-changed')
 rt=b.app_control('runtime.status')['body']
 need(rt.get('instance_id')=='r23hcagq4x9cyq' and rt['ready'] is True and rt['ingress']['paused'] is False and
      rt.get('background_workers_enabled') is False,'target-not-ready')
 before={k:b.resource(k)['State']['Pid'] for k in ('pg','redis','app')}
 need(before=={'pg':180813,'redis':133623,'app':257639},'target-pids-changed')
 saved=archive(root/'rpc',[(path,0o700),(cfgpath,0o600),(ledgerpath,0o600)])
 need(trusted(ledgerpath,0o600)==ledgerraw,'target-ledger-CAS-changed')
 replace(path,raw,0o700)
 updated_cfg=dict(cfg);updated_cfg['program_sha256']=payload['new']['target_rpc.py']
 replace(cfgpath,canonical(updated_cfg)+b'\n',0o600)
 need(before=={k:b.resource(k)['State']['Pid'] for k in before} and trusted(ledgerpath,0o600)==ledgerraw,
      'target-resources-or-ledger-changed')
 result={'ok':True,'program_sha256':sha(trusted(path,0o700)),'config_raw_sha256':sha(trusted(cfgpath,0o600)),
         'ledger_raw_sha256':sha(ledgerraw),'epoch':ledger['epoch'],'business_pids_unchanged':True}
 write_new(saved/'verification.json',canonical(result)+b'\n');print(json.dumps(result,sort_keys=True))
'''

SOURCE_UPGRADE = r'''
for name,value in payload['dependencies'].items():need(sha(trusted(lib/name,0o644))==value,'source-dependency-changed')
for name in payload['old']:need(sha(trusted(lib/name,0o644))==payload['old'][name],'source-old-program-changed')
M=load('admission_service_old',lib/'runner_service.py')
with M.execution_lock():
 b,state=source_idle(M)
 need(sha(trusted(scope/'state.json',0o600))==payload['state_raw_sha256'],'source-checkpoint-changed')
 budget=M.E.budget_accounting(state)
 need(budget['cohort_pause_seconds']==0 and budget['total_pause_seconds']==payload['prior_pause'],
      'cohort-budget-already-consumed')
 paths=[scope/name for name in ('state.json','transport-state.json','source-operations.json')]
 before={str(p):trusted(p,0o600) for p in paths}
 programs={name:base64.b64decode(raw,validate=True) for name,raw in payload['programs'].items()}
 for name,raw in programs.items():need(sha(raw)==payload['new'][name],'source-new-digest');compile(raw,name,'exec')
 saved=archive(scope,[(lib/name,0o644) for name in payload['old']]+[(p,0o600) for p in paths])
 need(all(trusted(p,0o600)==before[str(p)] for p in paths),'source-ledger-CAS-changed')
 for name,raw in programs.items():replace(lib/name,raw,0o644)
 # Guard code did not change. It launches recovery as a fresh process, which
 # now loads this revision; the healthy, already-running guard stays running.
 guard=b.source_ops._guard();need(not guard['recovering'] and not guard['tripped'],'guard-not-stable')
 need(all(trusted(p,0o600)==before[str(p)] for p in paths),'source-ledgers-changed')
 result={'ok':True,'new_programs':payload['new'],'state_raw_sha256':sha(before[str(scope/'state.json')]),
         'source_ledgers_preserved':True,'cohort_preserved':True,'main_started':False,'guard_restarted':False}
 write_new(saved/'verification.json',canonical(result)+b'\n');print(json.dumps(result,sort_keys=True))
'''

SOURCE_PROBE = r'''
for name,value in {**payload['new'],**payload['dependencies']}.items():
 need(sha(trusted(lib/name,0o644))==value,'new-source-program-changed')
M=load('admission_probe',lib/'runner_service.py')
with M.execution_lock():
 b,state=source_idle(M);before=trusted(scope/'transport-state.json',0o600)
 samples=[]
 for _ in range(3):
  started=time.monotonic();rt=b.observe_control('runtime.status')
  need(rt.get('instance_id')=='r23hcagq4x9cyq' and rt.get('ready') is True and
       rt.get('serving') is True and rt.get('draining') is False and rt.get('ingress',{}).get('paused') is False,
       'fresh-observer-not-ready')
  samples.append({'seconds':time.monotonic()-started,'active_requests':rt['active_requests']})
 need(trusted(scope/'transport-state.json',0o600)==before and
      sha(trusted(scope/'state.json',0o600))==payload['state_raw_sha256'],'observer-wrote-source-state')
 print(json.dumps({'ok':True,'fresh_observations':samples,'transport_unchanged':True,'main_started':False},sort_keys=True))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"offline": True, "main_started": False, "request_pauses": False,
                          "cohort_preserved": True, "reviewed_programs": NEW}))
        return
    folder = pathlib.Path(__file__).parent
    prior = runpy.run_path(str(folder / "prepare_retry.py"))
    channel = runpy.run_path(str(folder / "prepare_channel.py"))
    programs = {name: (folder / name).read_bytes() for name in NEW}
    if set(OLD) != set(NEW) or any(hashlib.sha256(raw).hexdigest() != NEW[name] for name, raw in programs.items()):
        raise RuntimeError("reviewed-program-changed")
    payload = {"old": OLD, "new": NEW, "dependencies": DEPENDENCIES, "cohort": COHORT,
               "owner": prior["OWNER"], "prior_pause": prior["PRIOR_PAUSE"], "target_config_sha": TARGET_CONFIG_SHA,
               "programs": {name: base64.b64encode(raw).decode() for name, raw in programs.items()}}
    common = prior["COMMON"].replace("retry-upgrade-20261017", "admission-upgrade-20261017")

    def remote(label, code, host):
        path = prior["FOLDER"] / ("admission-" + label + "-20261017.json")
        script = common + VALIDATION + code
        compile(script, label, "exec")
        # O_EXCL intent survives loss of the SSH reply and prevents replay.
        if path.exists() or path.is_symlink():
            raise RuntimeError("prior-result-exists-inspect-only")
        prior["private_json"](path.with_suffix(".intent.json"),
            {"script_sha256": hashlib.sha256(script.encode()).hexdigest(), "inspect_before_any_retry": True})
        try:
            result = subprocess.run(host + ["sudo python3 -I -c " + shlex.quote(script)],
                                    input=json.dumps(payload).encode(), capture_output=True, timeout=45)
        except BaseException as error:
            prior["private_json"](path.with_suffix(".uncertain.json"),
                {"exception": type(error).__name__, "inspect_before_any_retry": True})
            raise
        return prior["private_result"](path, result)

    preflight = remote("preflight", SOURCE_PREFLIGHT, channel["SSH_SOURCE"])
    payload["state_raw_sha256"] = preflight["state_raw_sha256"]
    target = remote("target-upgrade", TARGET_UPGRADE, channel["TARGET"])
    source = remote("source-upgrade", SOURCE_UPGRADE, channel["SSH_SOURCE"])
    probe = remote("probe", SOURCE_PROBE, channel["SSH_SOURCE"])
    print(json.dumps({"target": target, "source": source, "probe": probe}, sort_keys=True))


if __name__ == "__main__":
    main()
