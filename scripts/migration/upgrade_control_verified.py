#!/usr/bin/env python3
"""CAS-install the final reviewed controller before the second unrouted rehearsal."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import runpy
import shlex
import subprocess

OLD = {
    "target_rpc.py": "30faa6c7d50cf72800bbc102af2550d1e01b187b4b5894d2b18ab2d9dd0406b1",
    "durable_runner.py": "b721cb9a9dc7de8ada54c0795b94c08b7cd9fac4ac37f5bbf0f9a01ced5bba7a",
    "runner_service.py": "f84d86611bae5baec488f857d47283b5c7443095fd2798ecadb0706f5096bfa5",
    "source_operations.py": "f6c90b5d5069fde8a1cdce9ad3f6d750327bb64b935cc77473d7f8491ea4e86b",
    "runner_guard.py": "9e5598884aeadfe631c2934aaca155ddbdac7ef5def993138daa46201782eaae",
}
# Populate only with independently reviewed, frozen program digests.
NEW = {
    "target_rpc.py": "2b4fa88abb3a35c3ba1349ed7370d624cb5c3ebdd2fcbc121fd9704c4a3e196c",
    "durable_runner.py": "ed820505a9120786f04c4584ffe63ffa0e87d2fda02229edec6cc620b06f78e4",
    "runner_service.py": "b4c46158b4374e8be2f8c444eadb35833e0a29d3f1a78558cb52df0c8d90e89a",
    "source_operations.py": "b6cfab63741c0f2dfb4d5c36445f9d14d62c845e3c2c6e1b0bd849eddbf3ec97",
    "runner_guard.py": "704b171cc50feccf28f873320437886e8ea15fc7b76f9a8dbf36ed964dfd9345",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"offline": True, "reviewed_programs": NEW, "customer_route_changes": False}))
        return
    assert set(NEW) == set(OLD), "final-independent-review-required"
    root = pathlib.Path(__file__).parent
    prior = runpy.run_path(str(root / "upgrade_control_reads.py"))
    common = prior["COMMON"].replace("read-upgrade-20261017", "verified-upgrade-20261017")
    target = prior["TARGET"].replace(
        "98f37d9476e8317d06b85893593222d7a1808c2c8a7a481d372b463a6e056ce7",
        "ce201b7c2b6845ab2281b31d3e692f1b5de5fcb01b9c9d54394b45ae4e2192ab")
    target = target.replace("ledger['epoch']==0", "ledger['epoch']==2")
    target = target.replace("saved=archive(root/'rpc',[prog,cfgpath])", """assert all(op['phase']=='COMPLETE' for op in ledger['operations'].values())
assert b.endpoint_state()=={'v':1,'postgres':'source','redis':'source'}
saved=archive(root/'rpc',[prog,cfgpath])""")
    source = prior["SOURCE"].replace("saved=archive(scope,[lib/name for name in new])", """closed=json.loads((scope/'passed-selftest-20261017/closure-verification.json').read_bytes())
assert closed['ok'] is True and closed['selftest_disarmed'] is True
assert json.loads((scope/'controller-selftest.json').read_bytes())['armed'] is False
saved=archive(scope,[lib/name for name in new])""")
    source = source.replace("print(json.dumps(result,sort_keys=True))", """subprocess.run(['systemctl','restart','sub2api-migration-recovery-20261016.service'],check=True,capture_output=True,timeout=5)
restarted_at=time.time()
for _ in range(50):
 fresh=json.loads((scope/'guard-status.json').read_bytes())
 if fresh['healthy'] is True and not fresh['tripped'] and not fresh['recovering'] and restarted_at<=fresh['checked_at']<=time.time() and time.time()-fresh['checked_at']<1:
  break
 time.sleep(.1)
else:raise AssertionError('restarted-guard-not-fresh-and-healthy')
assert json.loads(statepath.read_bytes())==state
result['guard_not_restarted']=False;result['guard_restarted_healthy']=True
atomic(saved/'guard-restart-verification.json',json.dumps(result,sort_keys=True).encode()+b'\\n',0o600)
print(json.dumps(result,sort_keys=True))""")
    compile(common + target, "verified-target-upgrade", "exec")
    compile(common + source, "verified-source-upgrade", "exec")
    programs = {name: (root / name).read_bytes() for name in NEW}
    assert all(hashlib.sha256(raw).hexdigest() == NEW[name] for name, raw in programs.items())
    channel = runpy.run_path(str(root / "prepare_channel.py"))
    payload = json.dumps({"old": OLD, "new": NEW,
                          "programs": {name: base64.b64encode(raw).decode() for name, raw in programs.items()}}).encode()
    folder = pathlib.Path.home() / ".codex/backups/sub2api-source-controller-20261016"
    for label, remote, argv in (("target", target, channel["TARGET"]), ("source", source, channel["SSH_SOURCE"])):
        path = folder / (label + "-verified-upgrade-20261017.json")
        assert not path.exists(), "existing-upgrade-evidence-reconcile-do-not-repeat"
        result = subprocess.run(argv + ["sudo python3 -I -c " + shlex.quote(common + remote)],
                                input=payload, capture_output=True, timeout=45)
        evidence = result.stdout if result.returncode == 0 else json.dumps({"exit": result.returncode,
            "stdout": result.stdout.decode(errors="replace")[-4096:],
            "stderr": result.stderr.decode(errors="replace")[-4096:]}).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(evidence); out.flush(); os.fsync(out.fileno())
        assert result.returncode == 0, "verified-upgrade-incomplete-inspect-private-evidence"
        print(result.stdout.decode())


if __name__ == "__main__":
    main()
