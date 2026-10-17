#!/usr/bin/env python3
"""Fixed attempt-5 continuation, reusing the reviewed once-only recovery tool.

The original executed tool and all historical receipts remain byte-for-byte
intact. Only the explicit closed boundary and supplemental engine change differ.
Default is offline; --install only CAS-installs the reviewed engine, --apply
uses the derived once-only continuation, and --inspect never repeats a start.
"""
import argparse
import base64
import hashlib
import json
import pathlib
import shlex
import subprocess
import sys
import types

BASE_SHA = "6246e7ee0b776518419868e7ad322867ebbcb652d2756d0995db26e01b9068ff"
OLD_ENGINE = "18f0184ce061b4de57146a1f1024ab576622adc521dc16f0a9f0080fb76229d4"
NEW_ENGINE = "5419ce229ed41bd98b9b4143d15f89817236c29b94071f7c8fb54dfefa86fb14"
SCOPE_NAME = "resume-attested-attempt5-20261017"


def need(value, code):
    if not value:
        raise RuntimeError(code)


def derive(raw):
    need(hashlib.sha256(raw).hexdigest() == BASE_SHA, "reviewed-recovery-base-changed")
    text = raw.decode()
    replacements = [
        (OLD_ENGINE, NEW_ENGINE, 1),
        ("18.332221914082766", "23.854798037558794", 1),
        ("f3f1df620d449b751e544ebb34111b1b807f8d4cba171b65214b0644fcbf6be8", "1d1049731538fd7e3e37b1ee71e8e237ed09df5af7b61c14024aaf6a735d5adb", 1),
        ("4bb9a93b3b64f8d91b67165f10ec36d7e2c7b61321de25ae1fb2014ed79835bd", "8d5d191afb753a5879daba45178177a37fcdf210e863c529e78f6b073cf299a6", 1),
        ("7e627ac9d821d566a05d6d03123c7e8c659b1e09e395e86a178b3173b216442d", "7bb983984ae9056622d21752647e79c66390c74001b594fd6607e5b225f77bf8", 1),
        ("c631d3ff7aa10cb0fdf306566d77d3fb84dea6b9bb585922e599bf55721d95fb", "6a8f9664e6b453cf8c3b7f2fc74c9aaab40e8a887da5539399d4ab249073ad4d", 1),
        ("10b4ebf48d69833af52b7d9e6c17b4ed", "3acbfa1034067aab0675e2c2a095af86", 1),
        ("9f3d68ecaf85192bd9e9456c387f3b1e", "eeceb7d8975c689d59705aaeacb073e0", 1),
        ('state.get("attempt") == 4', 'state.get("attempt") == 5', 1),
        ('current.get("attempt", 0) > 4', 'current.get("attempt", 0) > 5', 1),
        ("== 17", "== 20", 5),
        ('"ledger_epoch": 17, "public_route_proven"', '"ledger_epoch": 20, "public_route_proven"', 1),
        ('restored["result"]["ingress"]["epoch"] == 2', 'restored["result"]["ingress"]["epoch"] == 4', 1),
        ('"restored_ingress_epoch": 2', '"restored_ingress_epoch": 4', 1),
        ('ingress_epoch=2', 'ingress_epoch=4', 1),
        ('need(not failed.get("instance_id") and not failed.get("substeps"), "attempt4-quiesce-progressed")',
         'need(failed.get("instance_id") == INSTANCE and set(failed.get("substeps", {})) == {"ingress", "writers", "redis", "postgres"} and all(row.get("phase") == "COMPLETE" for row in failed["substeps"].values()), "attempt5-quiesce-not-complete")', 1),
        ('program = pathlib.Path(__file__).read_bytes()', 'program = DERIVED_PROGRAM', 1),
    ]
    for before, after, count in replacements:
        need(text.count(before) == count, "reviewed-continuation-anchor-changed")
        text = text.replace(before, after)
    need(text.count("resume-idle-attempt4-20261017") == 4, "reviewed-scope-anchor-changed")
    text = text.replace("resume-idle-attempt4-20261017", SCOPE_NAME)
    result = text.encode()
    compile(result, "reviewed-attempt5-continuation", "exec")
    return result


def load_derived(raw):
    result = types.ModuleType("attested_recovery_derived")
    result.__file__ = __file__
    program = derive(raw)
    exec(compile(program, __file__, "exec"), result.__dict__)
    result.DERIVED_PROGRAM = program
    return result


INSTALL = r'''
import base64,hashlib,importlib.util,json,os,pathlib,sys,time
sys.dont_write_bytecode=True
p=json.load(sys.stdin);lib=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016')
def need(value,code):
 if not value:raise RuntimeError(code)
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);sys.modules[name]=m;s.loader.exec_module(m);return m
need(hashlib.sha256((lib/'runner_service.py').read_bytes()).hexdigest()=='7318557010904f289e05b8974f0ff6e23cffdcb3f372f82763824c9fc0eaa5a1','source-service-changed')
M=load('attested_upgrade_service',lib/'runner_service.py');G=load('attested_upgrade_guard',lib/'runner_guard.py')
scope=M.SCOPE;saved=scope/'attestation-order-upgrade-20261017'
with M.execution_lock():
 b=M.B.RunnerBackend();g=G.LocalBackend()
 need(g.main_status()['quiescent'] and not M.selftest_armed(p['owner']),'source-main-not-closed')
 guard=b.source_ops._guard();need(guard['healthy'] is True and guard['tripped'] is False and guard['recovering'] is False,'guard-not-stable')
 M.verify_entry();M.read_gates(p['owner'])
 frozen={name:M.S.private_bytes(scope/name) for name in p['pins']}
 need(all(hashlib.sha256(raw).hexdigest()==p['pins'][name] for name,raw in frozen.items()),'source-boundary-changed')
 state=b.store.read('state');need(state['attempt']==5 and state['phase']=='SOURCE_DATA' and state['pg']==state['redis']=='AWS' and state['window'] is None,'source-boundary-not-closed')
 need(not any(row['request']['action'] in ('pg_stop','redis_failover','redis_role_persist') for row in b.source_ops._ledger()['operations'].values()),'irreversible-operation-present')
 for kind,pid in [('app',0),('postgres',972839),('redis',972827)]:
  row=b.source_ops._resource(kind);need(row['State']['Pid']==pid and row['State']['Running']==(kind!='app'),'source-resource-changed')
 path=lib/'durable_runner.py';old=M.S.private_bytes(path,mode=0o644);new=base64.b64decode(p['program'],validate=True)
 need(hashlib.sha256(old).hexdigest()==p['old'] and hashlib.sha256(new).hexdigest()==p['new'],'engine-CAS-changed')
 compile(new,str(path),'exec')
 need(not saved.exists() and not saved.is_symlink(),'upgrade-already-attempted-inspect-only')
 saved.mkdir(mode=0o700);M.S.fsync_directory(scope)
 def write(path,raw,mode=0o600):
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
  with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
  M.S.fsync_directory(path.parent)
 write(saved/'previous-engine.py',old);write(saved/'upgrade.intent.json',M.B.canonical({'old':p['old'],'new':p['new'],'at':time.time(),'test_sha256':p['test_sha256'],'no_repeat':True}))
 pending=lib/'durable_runner.attested-20261017.pending';write(pending,new,0o644)
 need(M.S.private_bytes(path,mode=0o644)==old and all(M.S.private_bytes(scope/name)==raw for name,raw in frozen.items()),'upgrade-final-CAS-changed')
 os.replace(pending,path);M.S.fsync_directory(lib)
 need(M.S.private_bytes(path,mode=0o644)==new and all(M.S.private_bytes(scope/name)==raw for name,raw in frozen.items()),'upgrade-outcome-uncertain')
 result={'ok':True,'engine_sha256':p['new'],'runner_state_and_ledgers_preserved':True,'services_restarted':False,'at':time.time()}
 write(saved/'verification.json',M.B.canonical(result));print(json.dumps(result,sort_keys=True))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    for flag in ("--install", "--apply", "--inspect"):
        modes.add_argument(flag, action="store_true")
    args = parser.parse_args(argv)
    folder = pathlib.Path(__file__).resolve().parent
    derived = load_derived((folder / "resume_idle_recovery.py").read_bytes())
    if not args.install:
        return derived.main(["--apply"] if args.apply else ["--inspect"] if args.inspect else [])
    support = derived.load_support((folder / "accept_recovery_generation.py").read_bytes())
    channel = support.load_bytes("attested_upgrade_channel", folder / "prepare_channel.py", (folder / "prepare_channel.py").read_bytes())
    program = (folder / "durable_runner.py").read_bytes()
    need(hashlib.sha256(program).hexdigest() == NEW_ENGINE, "reviewed-engine-changed")
    need(hashlib.sha256((folder / "durable_runner_test.py").read_bytes()).hexdigest() ==
         "56a648f093d2781fd1ae206de51fee8bf2bddbc17ae1eb488c86e43ccb670e03", "reviewed-engine-tests-changed")
    marker = support.FOLDER / "attestation-order-upgrade-20261017.intent.json"
    need(not marker.exists() and not marker.is_symlink(), "upgrade-already-attempted-inspect-only")
    payload = {"owner": derived.OWNER, "old": OLD_ENGINE, "new": NEW_ENGINE, "pins": derived.SOURCE_FILES,
               "test_sha256": "56a648f093d2781fd1ae206de51fee8bf2bddbc17ae1eb488c86e43ccb670e03",
               "program": base64.b64encode(program).decode()}
    support.write_new(marker, support.canonical({"old": OLD_ENGINE, "new": NEW_ENGINE, "no_repeat": True,
        "installer_sha256": hashlib.sha256(INSTALL.encode()).hexdigest(), "test_sha256": payload["test_sha256"]}))
    command_receipt = marker.with_name("attestation-order-upgrade-20261017.command.json")
    try:
        result = subprocess.run(channel.SSH_SOURCE + ["sudo -n python3 -I -c " + shlex.quote(INSTALL)],
                                input=json.dumps(payload).encode(), capture_output=True, timeout=35)
    except (OSError, subprocess.TimeoutExpired) as error:
        support.write_new(command_receipt, support.canonical({"outcome": "unknown-inspect-only",
            "error_type": type(error).__name__, "no_repeat": True,
            "stdout_b64": base64.b64encode(getattr(error, "stdout", None) or b"").decode(),
            "stderr_b64": base64.b64encode(getattr(error, "stderr", None) or b"").decode()}))
        raise RuntimeError("upgrade-outcome-uncertain-inspect-private-receipt") from None
    support.write_new(command_receipt, support.canonical({
        "returncode": result.returncode, "stdout_b64": base64.b64encode(result.stdout).decode(),
        "stderr_b64": base64.b64encode(result.stderr).decode(), "no_repeat": True}))
    need(result.returncode == 0, "upgrade-outcome-uncertain-inspect-private-receipt")
    print(result.stdout.decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
