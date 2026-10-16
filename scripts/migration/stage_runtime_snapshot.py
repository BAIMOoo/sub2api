#!/usr/bin/env python3
"""Stage the running source app's actual environment privately, without starting it."""
import argparse
import hashlib
import json
import pathlib
import runpy
import subprocess
import sys

DIRECTORY = "/home/ubuntu/sub2api-migration-20261016/app-runtime"
EXPECTED_IMAGE = "sha256:7f83a7aa3937e47473df037a341e48104318f6ad7fa6c5de00504a12cbe87742"


def remote(ssh, script):
    result = subprocess.run(ssh+["python3 -"], input=script, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("runtime-snapshot-failed-no-secret-output")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"dry_run": True, "actual_environment": True, "starts_application": False, "changes_source": False}))
        return
    channel = runpy.run_path(str(pathlib.Path(__file__).with_name("prepare_channel.py")))
    raw = remote(channel["SSH_SOURCE"], '''
import json,subprocess
i=json.loads(subprocess.check_output(["docker","inspect","sub2api"],text=True))[0]
if not i["State"]["Running"] or i["Image"]!='''+repr(EXPECTED_IMAGE)+''': raise RuntimeError("source-identity-drift")
print(json.dumps({"container_id":i["Id"],"image_id":i["Image"],"environment":i["Config"]["Env"],"command":i["Config"]["Cmd"],"entrypoint":i["Config"]["Entrypoint"]},sort_keys=True))
''')
    payload = json.loads(raw)
    if not isinstance(payload["environment"], list) or any(not isinstance(x, str) or "=" not in x for x in payload["environment"]):
        raise RuntimeError("invalid-source-environment")
    canonical = json.dumps(payload, sort_keys=True)+"\n"
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    # Payload travels only in process memory and SSH stdin. Docker Compose .env
    # is deliberately not used: runtime Redis authentication differs from it.
    result = remote(channel["TARGET"], "PAYLOAD="+repr(canonical)+"\nDIRECTORY="+repr(DIRECTORY)+"\n"+r'''
import hashlib,json,os,pathlib
p=pathlib.Path(DIRECTORY)
if any(x.is_symlink() for x in [p,*p.parents]): raise RuntimeError("snapshot-symlink")
p.mkdir(mode=0o700,exist_ok=True);p.chmod(0o700)
f=p/"source-runtime.json"
if f.is_symlink() or (f.exists() and f.read_text()!=PAYLOAD): raise RuntimeError("existing-snapshot-differs")
fd=os.open(f,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,"w") as out: out.write(PAYLOAD);out.flush();os.fsync(out.fileno())
f.chmod(0o600)
fd=os.open(p,os.O_RDONLY);os.fsync(fd);os.close(fd)
print(json.dumps({"staged":True,"path":str(f),"sha256":hashlib.sha256(PAYLOAD.encode()).hexdigest(),"mode":"0600","source_modified":False,"application_started":False}))
''')
    report = json.loads(result)
    if report["sha256"] != digest:
        raise RuntimeError("runtime-snapshot-transfer-mismatch")
    print(json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"ok": False, "error": "runtime-snapshot-failed-no-secret-output"}), file=sys.stderr)
        raise SystemExit(1)
