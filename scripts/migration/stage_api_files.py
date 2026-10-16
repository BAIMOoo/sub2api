#!/usr/bin/env python3
"""Copy whitelisted initial application files and API-only TLS material.

Default dry-run; never starts services or overwrites a previous staging copy.
Logs are left on the source for a later, explicit archival step. Final file
authority/freeze is still required before an application/data cutover.
"""

import argparse
import json
import pathlib
import runpy

CHANNEL = runpy.run_path(str(pathlib.Path(__file__).with_name("prepare_channel.py")))
ROOT = "/home/ubuntu/sub2api-migration-20261016"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print("Stages only config.yaml, .installed, model pricing files, and api.b4im.com certificate/key. No live files, Caddy, application or DNS changes. Use --apply.")
        return
    # The source payload is carried in process memory and SSH stdin, never a
    # shell argument, console output or local plaintext staging directory.
    payload = CHANNEL["run"](CHANNEL["SSH_SOURCE"]+["sudo python3 -"], r'''
import base64,hashlib,json,pathlib,subprocess
data=json.loads(subprocess.check_output(["docker","inspect","sub2api"],text=True))[0]
mount=[m for m in data["Mounts"] if m["Destination"]=="/app/data"]
if len(mount)!=1: raise SystemExit("unexpected app data mount")
app=pathlib.Path(mount[0]["Source"])
cert=pathlib.Path("/var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/api.b4im.com")
files={}
for group,root,names in [
    ("app-initial",app,["config.yaml",".installed","model_pricing.json","model_pricing.sha256"]),
    ("certs-initial",cert,["api.b4im.com.crt","api.b4im.com.key","api.b4im.com.json"])]:
    for name in names:
        path=root/name
        if path.is_symlink() or not path.is_file(): raise SystemExit("unexpected staged file type")
        before=path.stat()
        if before.st_size>2**20: raise SystemExit("unexpectedly large staged file")
        raw=path.read_bytes()
        after=path.stat()
        if (before.st_ino,before.st_mtime_ns,before.st_size)!=(after.st_ino,after.st_mtime_ns,after.st_size):
            raise SystemExit("source file changed during initial snapshot")
        files[group+"/"+name]={"sha256":hashlib.sha256(raw).hexdigest(),"size":len(raw),"base64":base64.b64encode(raw).decode()}
print(json.dumps(files))
''')
    files = json.loads(payload)
    allowed = {
        "app-initial/config.yaml", "app-initial/.installed",
        "app-initial/model_pricing.json", "app-initial/model_pricing.sha256",
        "certs-initial/api.b4im.com.crt", "certs-initial/api.b4im.com.key",
        "certs-initial/api.b4im.com.json",
    }
    if set(files) != allowed:
        raise RuntimeError("unexpected source file list")
    result = CHANNEL["remote"](CHANNEL["TARGET"], f'''
import base64,hashlib,json,os,pathlib
root=pathlib.Path({ROOT!r})
if root.is_symlink() or not root.is_dir(): raise SystemExit("target preparation directory missing")
files=json.loads({payload!r})
report={{}}
for rel,entry in files.items():
    parent,name=rel.split("/")
    path=root/parent/name
    directory=path.parent
    if directory.is_symlink(): raise SystemExit("refuse symlink staging directory")
    directory.mkdir(mode=0o700,exist_ok=True)
    directory.chmod(0o700)
    raw=base64.b64decode(entry["base64"],validate=True)
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=entry["sha256"] or len(raw)!=entry["size"]: raise SystemExit("transfer integrity mismatch")
    if path.is_symlink(): raise SystemExit("refuse symlink staged file")
    if path.exists():
        if path.read_bytes()!=raw: raise SystemExit("previous staging file differs; no overwrite")
    else:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"wb") as out:
            out.write(raw);out.flush();os.fsync(out.fileno())
    path.chmod(0o600)
    report[rel]={{"sha256":digest,"size":len(raw),"mode":"0600"}}
for dirname in ["app-initial","certs-initial"]:
    fd=os.open(root/dirname,os.O_RDONLY);os.fsync(fd);os.close(fd)
manifest=root/"initial-api-files-verification.json"
manifest.write_text(json.dumps(report,indent=2)+"\\n");manifest.chmod(0o600)
print(json.dumps({{"files_verified":len(report),"manifest":str(manifest),"production_changed":False}}))
''')
    print(result, end="")


if __name__ == "__main__":
    main()
