#!/usr/bin/env python3
"""Stage the exact source Caddy and API-only certificate store on Tencent.

Default offline dry-run. Does not install/enable/start a unit, bind a port,
reload source Caddy, change business routing, or edit DNS.
"""
import argparse
import gzip
import hashlib
import json
import pathlib
import runpy
import shlex
import subprocess

CHANNEL=runpy.run_path(str(pathlib.Path(__file__).with_name('prepare_channel.py')))
ROOT='/home/ubuntu/sub2api-migration-20261016'
ENTRY=ROOT+'/entry'
ACME='certificates/acme-v02.api.letsencrypt.org-directory/api.b4im.com'
CONFIG='''{
    admin off
}
api.b4im.com {
    reverse_proxy 127.0.0.1:8082 {
        flush_interval -1
    }
}
'''
UNIT=f'''[Unit]
Description=Sub2API Tencent API-only entry
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory={ENTRY}
Environment=HOME={ENTRY}
Environment=XDG_DATA_HOME={ENTRY}/data
Environment=XDG_CONFIG_HOME={ENTRY}/config
ExecStart={ENTRY}/caddy run --config {ENTRY}/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=2
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={ENTRY}
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
'''


def run(command,**kwargs):

 try:
  p=subprocess.run(command,capture_output=True,timeout=300,**kwargs)
 except subprocess.TimeoutExpired:
  raise RuntimeError("entry staging timeout; inspect retained artifacts before retry") from None
 if p.returncode:raise RuntimeError('entry staging command failed; no source changes')
 return p.stdout


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');args=p.parse_args()
 if not args.apply:
  print(json.dumps({'apply':False,'actions':['read source Caddy executable and verify SHA','stage immutable executable, API-only ACME certificate files, config and inactive unit','validate staged config'],'source_mutations':False,'listeners_started':False,'dns_changed':False}));return
 source=CHANNEL['SSH_SOURCE']
 expected=run(source+['sha256sum /usr/bin/caddy'],text=True).split()[0]
 packed=run(source+['gzip -c /usr/bin/caddy'])
 raw=gzip.decompress(packed)
 if hashlib.sha256(raw).hexdigest()!=expected:raise RuntimeError('source binary changed during copy')
 script=f'''
import gzip,hashlib,json,os,pathlib,subprocess,sys
root=pathlib.Path({ROOT!r});entry=pathlib.Path({ENTRY!r})
for p in [root,*root.parents,entry]:
 if p.is_symlink():raise SystemExit('symlink entry path refused')
if not root.is_dir():raise SystemExit('preparation directory missing')
entry.mkdir(mode=0o700,exist_ok=True);entry.chmod(0o700)
def write(path,data,mode):
 if path.is_symlink():raise SystemExit('symlink file refused')
 if path.exists():
  if path.read_bytes()!=data:raise SystemExit('existing staged artifact differs; no overwrite')
 else:
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
  with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
 path.chmod(mode)
packed=sys.stdin.buffer.read(128*1024*1024)
raw=gzip.decompress(packed)
if hashlib.sha256(raw).hexdigest()!={expected!r}:raise SystemExit('binary transfer digest mismatch')
write(entry/'caddy',raw,0o755)
certstore=entry/'data/caddy'/{ACME!r}
if any(p.is_symlink() for p in [certstore,*certstore.parents]):raise SystemExit('symlink certificate store refused')
certstore.mkdir(mode=0o700,parents=True,exist_ok=True)
for name in ['api.b4im.com.crt','api.b4im.com.key','api.b4im.com.json']:
 src=root/'certs-initial'/name
 if src.is_symlink() or not src.is_file():raise SystemExit('initial certificate missing')
 write(certstore/name,src.read_bytes(),0o600)
for d in [entry/'data',entry/'data/caddy',entry/'data/caddy/certificates',certstore.parent,certstore,entry/'config']:
 d.mkdir(mode=0o700,parents=True,exist_ok=True);d.chmod(0o700)
write(entry/'Caddyfile',{CONFIG.encode()!r},0o600)
write(entry/'sub2api-tencent-entry.service',{UNIT.encode()!r},0o600)
env=os.environ.copy();env.update(HOME=str(entry),XDG_DATA_HOME=str(entry/'data'),XDG_CONFIG_HOME=str(entry/'config'))
p=subprocess.run([str(entry/'caddy'),'validate','--config',str(entry/'Caddyfile'),'--adapter','caddyfile'],env=env,capture_output=True,text=True,timeout=30)
if p.returncode:raise SystemExit('staged Caddy validation failed; no listener started')
version=subprocess.check_output([str(entry/'caddy'),'version'],text=True).strip()
report={{'caddy_version':version,'caddy_sha256':{expected!r},'validated':True,'unit_installed':False,'listeners_started':False,'source_changed':False}}
write(entry/'staging-verification.json',(json.dumps(report,indent=2)+'\\n').encode(),0o600)
print(json.dumps(report))
'''
 print(run(CHANNEL['TARGET']+['python3 -c '+shlex.quote(script)],input=packed).decode(),end='')

if __name__=='__main__':main()
