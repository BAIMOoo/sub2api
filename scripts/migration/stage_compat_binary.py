#!/usr/bin/env python3
"""Build the production-baseline compatibility binary and stage an inert image.

Default is offline. --apply builds locally and copies a static linux/amd64
binary into the already verified original runtime image on Tencent. It never
starts an application listener, connects to data services, or changes a route.
"""
import argparse
import base64
import gzip
import hashlib
import json
import os
import pathlib
import runpy
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE = "c69fdbeb0fa26543cc5bf115d2e5048426041e84"
BASE_IMAGE = "sub2api:migration-20261016-7f83a7aa3937"
TARGET_IMAGE_ID = "sha256:c9e9c907d12aad50738b5a8e58a826fce5e65b9f27111e3a684c6eb4b46bf70f"
TARGET_ROOT = "/home/ubuntu/sub2api-migration-20261016/artifacts"
LOCAL_ROOT = pathlib.Path.home()/".local/share/sub2api-migration-20261016/build"


def command(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, timeout=600, **kwargs)
    if result.returncode:
        if LOCAL_ROOT.is_dir() and not LOCAL_ROOT.is_symlink():
            stderr = result.stderr.encode() if isinstance(result.stderr, str) else result.stderr
            private_file(LOCAL_ROOT/"last-command.stderr", stderr or b"")
        raise RuntimeError("build-or-stage-failed-review-private-diagnostics")
    return result.stdout


def private_file(path, content):
    if path.is_symlink():
        raise RuntimeError("refuse-symlink-build-file")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(content)
        out.flush()
        os.fsync(out.fileno())


def source_fingerprints():
    changed = command(["git", "ls-files", "-m", "-o", "--exclude-standard", "--", "backend"], cwd=ROOT).decode().splitlines()
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in changed if (ROOT/name).is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"dry_run": True, "baseline": BASELINE, "version": "0.2.14", "local_compile": True, "stage_only": True, "starts_application": False}))
        return
    if command(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip() != BASELINE:
        raise RuntimeError("compat-worktree-must-use-exact-production-baseline")
    if not (ROOT/"backend/internal/web/dist/index.html").is_file():
        raise RuntimeError("embedded-frontend-build-is-required")
    if any(p.is_symlink() for p in [LOCAL_ROOT, *LOCAL_ROOT.parents]):
        raise RuntimeError("refuse-symlink-build-directory")
    LOCAL_ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    LOCAL_ROOT.chmod(0o700)
    binary = LOCAL_ROOT/"sub2api-compat"
    if binary.is_symlink():
        raise RuntimeError("refuse-symlink-build-binary")
    sources = source_fingerprints()
    environment = dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH="amd64")
    flags = "-s -w -X main.Version=0.2.14 -X main.Commit=c69fdbeb-migration-20261016 -X main.BuildType=release"
    command(["go", "build", "-tags", "embed", "-ldflags", flags, "-o", str(binary), "./cmd/server"], cwd=ROOT/"backend", env=environment)
    if sources != source_fingerprints():
        raise RuntimeError("sources-changed-during-build-rebuild-after-freeze")
    binary.chmod(0o700)
    raw = binary.read_bytes()
    if raw[:6] != b"\x7fELF\x02\x01" or int.from_bytes(raw[18:20], "little") != 62:
        raise RuntimeError("binary-is-not-linux-amd64-elf")
    digest = hashlib.sha256(raw).hexdigest()
    tag = "sub2api:migration-compat-20261016-"+digest[:12]
    manifest = {"baseline": BASELINE, "version": "0.2.14", "binary_sha256": digest, "binary_bytes": len(raw), "image": tag, "source_files": sources}
    private_file(LOCAL_ROOT/(digest+".json"), (json.dumps(manifest, sort_keys=True)+"\n").encode())
    payload = {"sha256": digest, "size": len(raw), "image": tag, "compressed": base64.b64encode(gzip.compress(raw, compresslevel=6)).decode(), "manifest": manifest}
    remote = r'''
import base64,gzip,hashlib,json,os,pathlib,subprocess
p=PAYLOAD
def run(argv):
    r=subprocess.run(argv,capture_output=True,text=True,timeout=180)
    if r.returncode:
        directory=pathlib.Path(TARGET_ROOT)/p["sha256"]
        if directory.is_dir():
            log=directory/"last-command.stderr";log.write_text(r.stderr);log.chmod(0o600)
        raise RuntimeError("target-artifact-command-failed")
    return r.stdout.strip()
base=json.loads(run(["docker","image","inspect",BASE_IMAGE]))[0]
if base["Id"]!=EXPECTED_ID or base["Architecture"]!="amd64" or base["Os"]!="linux": raise RuntimeError("target-original-image-drift")
root=pathlib.Path(TARGET_ROOT)
if any(f.is_symlink() for f in [root,*root.parents]): raise RuntimeError("artifact-directory-symlink")
root.mkdir(mode=0o700,exist_ok=True);root.chmod(0o700)
directory=root/p["sha256"]
if directory.is_symlink(): raise RuntimeError("artifact-directory-symlink")
directory.mkdir(mode=0o700,exist_ok=True)
raw=gzip.decompress(base64.b64decode(p["compressed"],validate=True))
if len(raw)!=p["size"] or hashlib.sha256(raw).hexdigest()!=p["sha256"]: raise RuntimeError("binary-transfer-mismatch")
dockerfile="FROM "+BASE_IMAGE+"\nCOPY sub2api /app/sub2api\nLABEL io.sub2api.migration.baseline="+p["manifest"]["baseline"]+" io.sub2api.migration.binary="+p["sha256"]+"\n"
for name,data,mode in [("sub2api",raw,0o755),("manifest.json",(json.dumps(p["manifest"],sort_keys=True)+"\n").encode(),0o600),("Dockerfile",dockerfile.encode(),0o600)]:
    path=directory/name
    if path.is_symlink() or (path.exists() and path.read_bytes()!=data): raise RuntimeError("previous-artifact-differs")
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,mode)
    with os.fdopen(fd,"wb") as f: f.write(data);f.flush();os.fsync(f.fileno())
    path.chmod(mode)
run(["docker","build","--pull=false","--network=none","--tag",p["image"],str(directory)])
actual=run(["docker","run","--rm","--network=none","--entrypoint","sha256sum",p["image"],"/app/sub2api"]).split()[0]
if actual!=p["sha256"]: raise RuntimeError("image-binary-mismatch")
version=run(["docker","run","--rm","--network=none",p["image"],"/app/sub2api","--version"])
print(json.dumps({"staged":True,"image":p["image"],"binary_sha256":actual,"version":version,"starts_application_listener":False}))
'''
    script = "PAYLOAD="+repr(payload)+"\nBASE_IMAGE="+repr(BASE_IMAGE)+"\nEXPECTED_ID="+repr(TARGET_IMAGE_ID)+"\nTARGET_ROOT="+repr(TARGET_ROOT)+"\n"+remote
    channel = runpy.run_path(str(pathlib.Path(__file__).with_name("prepare_channel.py")))
    output = command(channel["TARGET"]+["python3 -"], input=script.encode())
    private_file(LOCAL_ROOT/(digest+"-stage-result.json"), output)
    print(output.decode(), end="")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, RuntimeError) else "artifact-operation-failed-no-secret-output"}), file=sys.stderr)
        raise SystemExit(1)
