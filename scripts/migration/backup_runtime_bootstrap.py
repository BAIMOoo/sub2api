#!/usr/bin/env python3
"""Read-only, encrypted operational bootstrap material for the fixed Tencent runtime.

Default offline; --check reads only metadata. --apply copies an exact allowlist
plus three container inspect documents into local RAM and encrypts locally. No
images, database relation files, server-wide configuration or remote writes.
This complements separate PG/Redis backups; it is not an atomic restore point
or evidence of a rehearsed cold restore. New hosts need reviewed identity pins.
"""
import argparse
import hashlib
import io
import json
import os
import pathlib
import stat
import tarfile
import time
import types

RUN='sub2api-tencent-bootstrap-20261017'
HELPER_SHA='3028771b67a61e21c222fc4dba9973a85219436c8193de2e7ea8d798043d461d'
PREFIX='/var/lib/sub2api-migration-20261016'
LIB='/usr/local/libexec/sub2api-migration-20261016'
ENTRY='/home/ubuntu/sub2api-migration-20261016/entry'
CERT=ENTRY+'/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/api.b4im.com'
MAX_TOTAL=64*1024**2
# Exact reviewed paths; no recursive collection or user-selected remote paths.
FILES=[]
for name in ('install_target_control.py','prepare_live_app.py','reseed_standby.py',
             'rolling_app_host.py','rolling_app_recovery.py','rolling_app_route.py',
             'target_keeper.py','target_rpc.py'):
    FILES.append((LIB+'/'+name,0,0o700,2*1024**2))
for name in ('app-control.token','app.env','controller-adoption.json','controller-ledger-before.json',
             'host-config.json','source-controller-closed.json','source-recovery-receipt.json',
             'state.json','route-prerequisites.json','installed-inspection.json','new-process.json',
             'route-keeper/install-receipt.json'):
    FILES.append((PREFIX+'/rolling-recovery/'+name,0,0o600,4*1024**2))
for name in ('state.json','replication.pgpass'):
    FILES.append((PREFIX+'/postgres-authority/'+name,0,0o600,1024**2))
FILES.append((PREFIX+'/postgres-authority/monitor.py',0,0o700,2*1024**2))
for name in ('config.json','app-control.token','state.json'):
    FILES.append((PREFIX+'/rpc/'+name,0,0o600,8*1024**2))
for name in ('arm.json','source-complete-proof.json','state.json'):
    FILES.append((PREFIX+'/keeper/'+name,0,0o600,1024**2))
FILES.append((PREFIX+'/app/endpoints.json',0,0o644,1024))
for name in ('Caddyfile','config/caddy/autosave.json','staging-verification.json','sub2api-tencent-entry.service'):
    FILES.append((ENTRY+'/'+name,1000,0o600,1024**2))
FILES.append((ENTRY+'/caddy',1000,0o755,48*1024**2))
for extension in ('crt','key','json'):
    FILES.append((CERT+'/api.b4im.com.'+extension,1000,0o600,1024**2))
UNITS=('sub2api-tencent-entry.service','sub2api-tencent-keeper-20261016.service',
       'sub2api-migration-target-guard-20261016.service','sub2api-migration-target-guard-20261016.timer',
       'sub2api-pg-authority-20261016-monitor.service','sub2api-pg-authority-20261016-monitor.timer')
FILES.extend(('/etc/systemd/system/'+name,0,0o644,64*1024) for name in UNITS)
MASKS=tuple('/etc/systemd/system/sub2api-pg-replica-20261016-target-monitor.'+kind for kind in ('service','timer'))
VOLUME_CONFIGS={'pg':('PG_VERSION','postgresql.conf','postgresql.auto.conf','pg_hba.conf','pg_ident.conf'),
                'redis':('migration.conf',)}
VOLUME_PATHS={'pg':'/var/lib/docker/volumes/sub2api-migration-pg-replica-data-20261016/_data',
              'redis':'/var/lib/docker/volumes/sub2api-migration-redis-data-20261016/_data'}
MOUNT_COMPARE=r'''
def normalized_mounts(items):
 require(type(items) is list and all(type(item) is dict and type(item.get('Destination')) is str and item['Destination'] for item in items),'BOOTSTRAP_MOUNTS_INVALID')
 destinations=[item['Destination'] for item in items]
 require(len(destinations)==len(set(destinations)),'BOOTSTRAP_MOUNTS_DUPLICATE')
 return sorted(items,key=lambda item:item['Destination'])
'''
STABLE_READ=r'''
def stable_read(path,uid,mode,limit):
 path=pathlib.Path(path)
 r.trusted_directory(path.parent,uid=uid)
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 with os.fdopen(fd,'rb') as handle:
  before=os.fstat(handle.fileno())
  require(stat.S_ISREG(before.st_mode) and before.st_uid==uid and before.st_nlink==1 and stat.S_IMODE(before.st_mode)==mode and before.st_size<=limit,'BOOTSTRAP_FILE_UNTRUSTED')
  raw=handle.read(limit+1);after=os.fstat(handle.fileno())
 current=path.lstat()
 signature=lambda item:(item.st_dev,item.st_ino,item.st_size,item.st_mtime_ns,item.st_ctime_ns,item.st_uid,item.st_gid,item.st_mode,item.st_nlink)
 require(signature(before)==signature(after)==signature(current) and len(raw)==before.st_size,'BOOTSTRAP_FILE_CHANGED')
 return raw,{'path':str(path),'uid':uid,'gid':before.st_gid,'mode':mode,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
'''


def require(ok,code):
    if not ok:raise RuntimeError(code)


def sha(raw):return hashlib.sha256(raw).hexdigest()


def helper():
    path=pathlib.Path(__file__).with_name('backup_tencent_final.py')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as handle:
        info=os.fstat(handle.fileno());raw=handle.read(128*1024)
    require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1 and
            not info.st_mode&0o022 and sha(raw)==HELPER_SHA,'BOOTSTRAP_HELPER_PIN_CHANGED')
    module=types.ModuleType('bootstrap_backup_helper');module.__file__=str(path)
    exec(compile(raw,str(path),'exec'),module.__dict__)
    module.MAX_CAPTURE=MAX_TOTAL
    return module


def program(base,check_only=False):
    constants='\nFILES='+repr(FILES)+'\nMASKS='+repr(MASKS)+'\nUNITS='+repr(UNITS)+'\nVOLUME_CONFIGS='+repr(VOLUME_CONFIGS)+'\nVOLUME_PATHS='+repr(VOLUME_PATHS)+'\nMAX_TOTAL='+repr(MAX_TOTAL)+'\n'
    return base.CHECK+constants+STABLE_READ+MOUNT_COMPARE+'\nCHECK_ONLY='+repr(check_only)+r'''
import io,tarfile,subprocess
entries=list(FILES)
# Only named configuration files, never a database/Redis data-tree walk.
for kind,names in VOLUME_CONFIGS.items():
 require(cfg[kind]['volume_path']==VOLUME_PATHS[kind],'BOOTSTRAP_VOLUME_PATH_CHANGED')
 data=pathlib.Path(cfg[kind]['volume_path']);directory=data.lstat()
 uid=70 if kind=='pg' else 999
 require(stat.S_ISDIR(directory.st_mode) and directory.st_uid==uid and not directory.st_mode&0o022,'BOOTSTRAP_VOLUME_DIRECTORY_CHANGED')
 for name in names:
  path=data/name;mode=stat.S_IMODE(path.lstat().st_mode)
  require(mode in (0o600,0o644),'BOOTSTRAP_CONFIG_MODE_CHANGED')
  entries.append((str(path),uid,mode,1024*1024))
require(len({item[0] for item in entries})==len(entries),'BOOTSTRAP_DUPLICATE_PATH')
masked=[]
for value in MASKS:
 path=pathlib.Path(value);r.trusted_directory(path.parent)
 before=path.lstat();target=os.readlink(path);after=path.lstat()
 require(stat.S_ISLNK(before.st_mode) and before.st_uid==0 and target=='/dev/null' and
         (before.st_ino,before.st_mtime_ns,before.st_ctime_ns)==(after.st_ino,after.st_mtime_ns,after.st_ctime_ns),'BOOTSTRAP_MASK_LINK_CHANGED')
 masked.append({'path':value,'target':target,'uid':0})
metadata=[];total=0
for value,uid,mode,limit in entries:
 path=pathlib.Path(value);r.trusted_directory(path.parent,uid=uid);info=path.lstat()
 require(stat.S_ISREG(info.st_mode) and info.st_uid==uid and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==mode and info.st_size<=limit,'BOOTSTRAP_FILE_UNTRUSTED')
 total+=info.st_size;require(total<=MAX_TOTAL,'BOOTSTRAP_TOTAL_LIMIT')
 metadata.append({'path':value,'uid':uid,'mode':mode,'bytes':info.st_size})
if CHECK_ONLY:
 print(json.dumps({'read_only':True,'file_count':len(entries),'total_bytes':total,'files':metadata,'explicit_masks':masked},sort_keys=True));raise SystemExit(0)
before={kind:b.resource(kind) for kind in ('pg','redis','app')}
manifest={'v':1,'files':[],'explicit_systemd_masks':masked,'captured_at':time.time(),'cross_service_atomic':False,'no_images_or_database_relations':True}
buf=io.BytesIO()
with tarfile.open(fileobj=buf,mode='w:gz',compresslevel=1) as archive:
 def add(name,raw,mode=0o600):
  item=tarfile.TarInfo(name);item.mode=mode;item.size=len(raw);archive.addfile(item,io.BytesIO(raw))
 for value,uid,mode,limit in entries:
  raw,proof=stable_read(value,uid,mode,limit);manifest['files'].append(proof)
  add('files/'+value.lstrip('/'),raw)
 for kind,actual in before.items():add('metadata/docker-'+kind+'.inspect.json',json.dumps(actual,sort_keys=True).encode())
 unitstate=subprocess.run(['/usr/bin/systemctl','show',*UNITS,'--property=Id,UnitFileState,ActiveState,SubState,MainPID,FragmentPath,DropInPaths,NeedDaemonReload'],capture_output=True,timeout=10)
 require(unitstate.returncode==0,'BOOTSTRAP_UNIT_INSPECT_FAILED')
 add('metadata/systemd.show.txt',unitstate.stdout)
 after={kind:b.resource(kind) for kind in ('pg','redis','app')}
 require(all(before[kind]['Id']==after[kind]['Id'] and before[kind]['State']['Pid']==after[kind]['State']['Pid'] and before[kind]['Config']==after[kind]['Config'] and before[kind]['HostConfig']==after[kind]['HostConfig'] and normalized_mounts(before[kind]['Mounts'])==normalized_mounts(after[kind]['Mounts']) for kind in before),'BOOTSTRAP_RUNTIME_CHANGED')
 add('manifest.json',json.dumps(manifest,sort_keys=True).encode())
require(len(buf.getvalue())<=MAX_TOTAL,'BOOTSTRAP_ARCHIVE_LIMIT')
sys.stdout.buffer.write(buf.getvalue())
'''


def validate(raw):
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        members=archive.getmembers();names=[item.name for item in members]
        require(len(names)==len(set(names)) and len(names)<100,'BOOTSTRAP_ARCHIVE_DUPLICATE_OR_LIMIT')
        required={'manifest.json','metadata/systemd.show.txt',*(f'metadata/docker-{k}.inspect.json' for k in ('pg','redis','app'))}
        require(required<=set(names),'BOOTSTRAP_METADATA_MISSING')
        content={};total=0
        for item in members:
            path=pathlib.PurePosixPath(item.name)
            require(item.isfile() and not path.is_absolute() and '..' not in path.parts and item.mode==0o600 and item.size<=48*1024**2,'BOOTSTRAP_ARCHIVE_MEMBER_INVALID')
            total+=item.size;require(total<=MAX_TOTAL,'BOOTSTRAP_ARCHIVE_TOTAL_LIMIT')
            with archive.extractfile(item) as handle:content[item.name]=handle.read(item.size+1)
            require(len(content[item.name])==item.size,'BOOTSTRAP_ARCHIVE_TRUNCATED')
        manifest=json.loads(content['manifest.json'])
        expected={value for value,_,_,_ in FILES}
        expected.update(VOLUME_PATHS[kind]+'/'+name for kind,names in VOLUME_CONFIGS.items() for name in names)
        actual={row['path'] for row in manifest['files']}
        require(len(actual)==len(manifest['files']) and expected==actual,'BOOTSTRAP_ALLOWLIST_MISSING_OR_EXTRA')
        require(set(names)==required|{'files/'+value.lstrip('/') for value in actual},'BOOTSTRAP_UNEXPECTED_ARCHIVE_MEMBER')
        require(manifest['explicit_systemd_masks']==[{'path':p,'target':'/dev/null','uid':0} for p in MASKS],'BOOTSTRAP_MASK_PROOF_CHANGED')
        for row in manifest['files']:
            value=content['files/'+row['path'].lstrip('/')]
            require(len(value)==row['bytes'] and sha(value)==row['sha256'],'BOOTSTRAP_FILE_DIGEST_CHANGED')
        for kind in ('pg','redis','app'):
            inspect=json.loads(content['metadata/docker-'+kind+'.inspect.json'])
            require(isinstance(inspect.get('Id'),str) and len(inspect['Id'])==64 and inspect.get('State',{}).get('Running') is True,'BOOTSTRAP_CONTAINER_METADATA_INVALID')
    return {'files':len(actual),'uncompressed_bytes':total,'manifest_sha256':sha(content['manifest.json'])}


def apply(folder,target,base,*,remote=None):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    remote=remote or base.run_remote;folder=base.private_folder(folder)
    key=AESGCM.generate_key(bit_length=256);base.write(folder/'backup.key',key)
    report={'v':1,'run':RUN,'encrypted':True,'remote_mutations':False,'cross_service_atomic':False,'started_at':time.time()}
    def encrypt(name,raw):
        nonce=os.urandom(12);aad=(RUN+'/'+name).encode();blob=nonce+AESGCM(key).encrypt(nonce,raw,aad)
        base.write(folder/(name+'.aesgcm'),blob);saved=(folder/(name+'.aesgcm')).read_bytes()
        require(sha(AESGCM(key).decrypt(saved[:12],saved[12:],aad))==sha(raw),'BOOTSTRAP_DECRYPT_FAILED')
        return {'bytes':len(raw),'plaintext_sha256':sha(raw),'ciphertext_sha256':sha(saved),'aad':aad.decode(),'decrypt_verified':True}
    try:
        result=remote(target,program(base),timeout=120)
        receipt={k:result[k] for k in ('returncode','failure')};receipt.update(stdout_bytes=len(result['stdout']),stdout_sha256=sha(result['stdout']),stderr_bytes=len(result['stderr']),stderr_sha256=sha(result['stderr']))
        if result['stderr']:receipt['stderr_artifact']=encrypt('capture.stderr',result['stderr'])
        base.write(folder/'capture.command.json',json.dumps(receipt,sort_keys=True).encode())
        if result['stdout']:report['artifact']=encrypt('bootstrap.tar.gz',result['stdout'])
        require(result['returncode']==0 and result['failure'] is None,'BOOTSTRAP_CAPTURE_FAILED')
        report['archive']=validate(result['stdout']);report.update(status='complete',completed_at=time.time())
        base.write(folder/'verification.json',json.dumps(report,sort_keys=True).encode());return report
    except BaseException as error:
        report.update(status='failed',error_type=type(error).__name__,completed_at=time.time())
        base.write(folder/'failure.json',json.dumps(report,sort_keys=True).encode())
        raise RuntimeError('bootstrap-backup-incomplete-inspect-private-receipt') from None


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);mode=p.add_mutually_exclusive_group();mode.add_argument('--check',action='store_true');mode.add_argument('--apply',action='store_true');args=p.parse_args(argv)
    if not(args.check or args.apply):print(json.dumps({'offline':True,'remote_mutations':False,'fixed_files':len(FILES),'fixed_volume_config_files':6,'cross_service_atomic':False}));return
    base=helper();channel=base.runpy.run_path(str(pathlib.Path(__file__).with_name('prepare_channel.py')))
    if args.check:
        result=base.run_remote(channel['TARGET'],program(base,True),timeout=60)
        require(result['returncode']==0 and result['failure'] is None,'BOOTSTRAP_PREFLIGHT_FAILED');print(result['stdout'].decode());return
    print(json.dumps(apply(pathlib.Path.home()/'.codex/backups'/RUN,channel['TARGET'],base),sort_keys=True))


if __name__=='__main__':main()
