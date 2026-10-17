#!/usr/bin/env python3
"""Encrypt a final, read-only archive of the stopped AWS app's data directory.

Run with the bundled Python containing cryptography. No remote files change.
The fixed local destination is exclusive; an incomplete archive is inspected.
"""
import argparse
import hashlib
import io
import json
import os
import pathlib
import runpy
import shlex
import subprocess
import tarfile

REMOTE = r'''
import hashlib,io,json,pathlib,stat,subprocess,sys,tarfile
root=pathlib.Path('/var/lib/sub2api-migration-20261016')
manifest=json.loads((root/'runner/source-manifest.json').read_bytes())
cid=manifest['containers']['app']['id']
row=json.loads(subprocess.check_output(['/usr/bin/docker','inspect',cid]))[0]
s=row['State']
assert row['Id']==cid and s['Running'] is False and s['Status']=='exited' and s['Pid']==0
assert s['ExitCode']==0 and s['OOMKilled'] is False and row['HostConfig']['RestartPolicy']['Name']=='no'
mounts=[m for m in row['Mounts'] if m['Destination']=='/app/data']
assert len(mounts)==1
data=pathlib.Path(mounts[0]['Source']);assert data.is_dir() and not data.is_symlink()
entries=sorted(data.rglob('*'));assert all(not p.is_symlink() for p in entries)
files=[p for p in entries if not p.is_dir()]
assert len(files)<=512
report={'container_id':cid,'files':{},'source_app_stopped_cleanly':True}
buf=io.BytesIO();total=0
with tarfile.open(fileobj=buf,mode='w:gz',compresslevel=1) as tar:
 for p in files:
  a=p.lstat();assert stat.S_ISREG(a.st_mode) and a.st_nlink==1 and a.st_size<=128*1024**2
  raw=p.read_bytes();b=p.lstat()
  assert (a.st_ino,a.st_size,a.st_mtime_ns,a.st_ctime_ns)==(b.st_ino,b.st_size,b.st_mtime_ns,b.st_ctime_ns)
  total+=len(raw);assert total<=256*1024**2
  rel=p.relative_to(data).as_posix();info=tarfile.TarInfo('app-data/'+rel);info.size=len(raw);info.mode=0o600
  tar.addfile(info,io.BytesIO(raw));report['files'][rel]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
 raw=json.dumps(report,sort_keys=True).encode();info=tarfile.TarInfo('manifest.json');info.size=len(raw);info.mode=0o600
 tar.addfile(info,io.BytesIO(raw))
sys.stdout.buffer.write(buf.getvalue())
'''


def write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    if not parser.parse_args().apply:
        print(json.dumps({"offline": True, "remote_changes": False, "archive_encrypted": True}))
        return
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    folder = pathlib.Path.home() / ".codex/backups/sub2api-retired-source-final-20261017"
    folder.mkdir(mode=0o700)
    key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    write(folder / "archive.key", key)
    channel = runpy.run_path(str(pathlib.Path(__file__).with_name("prepare_channel.py")))
    result = subprocess.run(channel["SSH_SOURCE"] + ["sudo -n python3 -I -c " + shlex.quote(REMOTE)],
                            capture_output=True, timeout=120)
    if result.returncode:
        write(folder / "failure.json", json.dumps({"returncode": result.returncode}).encode())
        raise RuntimeError("archive-source-check-failed-inspect-private-directory")
    raw = result.stdout
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
    required = {"config.yaml", ".installed", "model_pricing.json", "model_pricing.sha256"}
    assert required <= manifest["files"].keys(), "required-files-missing"
    aad = b"sub2api-retired-source-final-20261017"
    encrypted = AESGCM(key).encrypt(nonce, raw, aad)
    write(folder / "app-data.tar.gz.aesgcm", nonce + encrypted)
    stored = (folder / "app-data.tar.gz.aesgcm").read_bytes()
    verified = AESGCM((folder / "archive.key").read_bytes()).decrypt(stored[:12], stored[12:], aad)
    assert verified == raw, "archive-decrypt-verification-failed"
    report = {"ok": True, "source_app_stopped_cleanly": True, "file_count": len(manifest["files"]),
              "plaintext_bytes": len(raw), "plaintext_sha256": hashlib.sha256(raw).hexdigest(),
              "ciphertext_sha256": hashlib.sha256(stored).hexdigest(), "decrypt_verified": True,
              "required_files": {k: manifest["files"][k] for k in sorted(required)},
              "archive_directory": str(folder), "remote_changes": False}
    write(folder / "verification.json", json.dumps(report, sort_keys=True).encode() + b"\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
