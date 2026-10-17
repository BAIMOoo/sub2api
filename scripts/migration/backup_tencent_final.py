#!/usr/bin/env python3
"""Read-only, locally encrypted backup of the migrated primary and runtime evidence.

The default is offline. Redis live keys are explicitly excluded: redis-cli --rdb
uses replication and may trigger a server-side snapshot. Existing RDB/AOF files
are not represented as a current Redis backup. PostgreSQL and file archives are
captured at separate times; this is not a cross-service atomic restore point.
"""
import argparse
import hashlib
import io
import json
import os
import pathlib
import runpy
import selectors
import shlex
import stat
import subprocess
import tarfile
import time

RUN = 'sub2api-tencent-final-20261017'
ROOT = '/var/lib/sub2api-migration-20261016'
RPC_SHA = '85d980acff46125de0db0a2734d33b632f1cfb919577be871f8c56faff2db75a'
MAX_CAPTURE = 1024 * 1024 ** 2
MAX_STDERR = 1024 * 1024
PG_READ = r'''
def read_expected_primary(backend, rpc, clock):
 deadline=clock.monotonic()+2.0
 for attempt in range(41):
  try:
   with backend.pg_locks():
    sample=backend.pg_sample()
    require(sample['in_recovery'] is False and sample['system_identifier']=='7626048206782038050','PG_NOT_EXPECTED_PRIMARY')
    return sample
  except rpc.Refusal as error:
   if error.code!='OPERATION_BUSY' or attempt==40:raise
   remaining=deadline-clock.monotonic()
   if remaining<=0:raise
   clock.sleep(min(0.05,remaining))
'''
CHECK = r'''
import importlib.util,pathlib,json,sys,os,stat,hashlib,time
sys.dont_write_bytecode=True
def require(ok,code):
 if not ok:raise RuntimeError(code)
path=pathlib.Path('/usr/local/libexec/sub2api-migration-20261016/target_rpc.py')
for parent in reversed(path.parents):
 info=parent.lstat()
 require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022,'RPC_DIRECTORY_UNTRUSTED')
fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
with os.fdopen(fd,'rb') as handle:
 info=os.fstat(handle.fileno());raw=handle.read(2*1024**2)
 require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o700 and info.st_size==len(raw),'RPC_FILE_UNTRUSTED')
require(hashlib.sha256(raw).hexdigest()==__RPC_SHA__,'RPC_PIN_CHANGED')
# Execute the exact bytes just checked rather than reopening an imported path.
import types
r=types.ModuleType('backup_rpc');r.__file__=str(path);sys.modules[r.__name__]=r
exec(compile(raw,str(path),'exec'),r.__dict__)
cfg=r.load_config();b=r.LocalBackend(cfg);ledger=r.FileStore(cfg).read()
require(ledger['pg']['authority']==ledger['redis']['authority']=='TENCENT','PRIMARY_AUTHORITY_CHANGED')
require(b.endpoint_state()=={'v':1,'postgres':'target','redis':'target'},'ENDPOINTS_CHANGED')
b.resource('pg');b.resource('app')
__PG_READ__
sample=read_expected_primary(b,r,time)
require(b.redis_inspect()['role']=='master','REDIS_NOT_PRIMARY')
require(b.app_control('runtime.status')['body']['ready'] is True,'APP_NOT_READY')
'''.replace('__RPC_SHA__', repr(RPC_SHA)).replace('__PG_READ__', PG_READ)
DUMP = CHECK + r'''
import subprocess
result=subprocess.run(['/usr/bin/docker','exec','--user','postgres','-i',cfg['pg']['container_id'],'pg_dump','-w','-h','/tmp','-p','25432','-U','sub2api','-d','sub2api','-Fc','--no-owner','--no-acl','-Z','3'],stdout=sys.stdout.buffer,stderr=subprocess.PIPE,timeout=240)
if result.stderr:sys.stderr.buffer.write(result.stderr)
require(result.returncode==0,'PRIMARY_DUMP_FAILED')
'''
VERIFY_DUMP = CHECK + r'''
import subprocess
result=subprocess.run(['/usr/bin/docker','exec','--user','postgres','-i',cfg['pg']['container_id'],'pg_restore','-l'],input=sys.stdin.buffer.read(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
if result.stderr:sys.stderr.buffer.write(result.stderr)
require(result.returncode==0 and b'TABLE DATA' in result.stdout,'DUMP_TOC_VERIFY_FAILED')
print(json.dumps({'toc_verified':True,'toc_sha256':hashlib.sha256(result.stdout).hexdigest()}))
'''
FILES = CHECK + r'''
import io,tarfile
root=pathlib.Path('/var/lib/sub2api-migration-20261016');files=[];total=0
def file_uid(path):
 relative=path.relative_to(root).parts
 return 1000 if relative[:3]==('rolling-recovery','app','data') or relative[:2]==('app','data') else 0
for part in ('rolling-recovery/app/data','app','keeper','rpc'):
 folder=root/part
 r.trusted_directory(folder,uid=file_uid(folder))
 for path in sorted(folder.rglob('*')):
  info=path.lstat()
  require(not stat.S_ISLNK(info.st_mode),'BACKUP_SYMLINK')
  if stat.S_ISDIR(info.st_mode):
   require(info.st_uid==file_uid(path) and not info.st_mode&0o022,'BACKUP_DIRECTORY_UNTRUSTED');continue
  require(stat.S_ISREG(info.st_mode) and info.st_uid==file_uid(path) and info.st_nlink==1 and not info.st_mode&0o022,'BACKUP_FILE_UNTRUSTED')
  require(info.st_size<=128*1024**2,'BACKUP_FILE_TOO_LARGE')
  total+=info.st_size;require(total<=512*1024**2,'BACKUP_ARCHIVE_TOO_LARGE')
  files.append(path)
require(len(files)<1024,'BACKUP_TOO_MANY_FILES')
buf=io.BytesIO()
with tarfile.open(fileobj=buf,mode='w:gz',compresslevel=1) as tar:
 for path in files:
  r.trusted_directory(path.parent,uid=file_uid(path))
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
  with os.fdopen(fd,'rb') as handle:
   before=os.fstat(handle.fileno());raw=handle.read(128*1024**2+1);after=os.fstat(handle.fileno())
  current=path.lstat()
  signature=lambda item:(item.st_dev,item.st_ino,item.st_size,item.st_mtime_ns,item.st_ctime_ns,item.st_mode,item.st_uid,item.st_nlink)
  require(stat.S_ISREG(before.st_mode) and before.st_uid==file_uid(path) and before.st_nlink==1 and not before.st_mode&0o022,'BACKUP_FILE_UNTRUSTED')
  require(signature(before)==signature(after)==signature(current) and len(raw)==before.st_size,'BACKUP_FILE_CHANGED')
  info=tarfile.TarInfo(path.relative_to(root).as_posix());info.size=len(raw);info.mode=0o600
  tar.addfile(info,io.BytesIO(raw))
sys.stdout.buffer.write(buf.getvalue())
'''


def require(ok, code):
    if not ok:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fsync_directory(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_folder(folder):
    folder = pathlib.Path(folder).absolute()
    for parent in reversed(folder.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.getuid()) and
                not info.st_mode & 0o022, 'BACKUP_DIRECTORY_UNTRUSTED')
    folder.mkdir(mode=0o700)  # Existing backup scopes are evidence, never overwritten.
    info = folder.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
            stat.S_IMODE(info.st_mode) == 0o700, 'BACKUP_DIRECTORY_UNTRUSTED')
    fsync_directory(folder.parent)
    return folder


def write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    fsync_directory(path.parent)


def run_remote(target, script, *, input=None, timeout=300):
    """Bound both captured streams in RAM; never spill database data to disk."""
    # Upload input via a separate SSH stdin writer so large dumps cannot deadlock
    # against stderr. All child streams are closed and the process reaped.
    import threading
    process = subprocess.Popen(target + ['sudo -n python3 -I -c ' + shlex.quote(script)],
                               stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    writer = None
    if input is not None:
        def feed():
            try:
                process.stdin.write(input)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
    outputs = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + timeout
    failure = None
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        while selector.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                failure = 'COMMAND_TIMEOUT'
                break
            for entry, _ in selector.select(min(left, 1)):
                chunk = os.read(entry.fd, 65536)
                if not chunk:
                    selector.unregister(entry.fileobj)
                    continue
                stream = outputs[entry.data]
                limit = MAX_CAPTURE if entry.data == 'stdout' else MAX_STDERR
                if len(stream) + len(chunk) > limit:
                    failure = 'COMMAND_CAPTURE_LIMIT'
                    break
                stream.extend(chunk)
            if failure:
                break
        if failure:
            process.kill()
        try:
            code = process.wait(timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            failure = 'COMMAND_TIMEOUT'
            process.kill()
            code = process.wait()
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
        if writer:
            writer.join(timeout=5)
    return {'returncode': code, 'stdout': bytes(outputs['stdout']),
            'stderr': bytes(outputs['stderr']), 'failure': failure}


def validate_archive(raw):
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        members = archive.getmembers()
        names = [item.name for item in members]
        require(len(names) == len(set(names)) and len(names) < 1024, 'ARCHIVE_DUPLICATE_OR_LIMIT')
        require({'app/endpoints.json', 'keeper/arm.json',
                 'rolling-recovery/app/data/config.yaml'} <= set(names), 'ARCHIVE_REQUIRED_FILE_MISSING')
        total = 0
        for item in members:
            path = pathlib.PurePosixPath(item.name)
            require(item.isfile() and not path.is_absolute() and '..' not in path.parts and
                    item.mode == 0o600 and item.size <= 128 * 1024 ** 2, 'ARCHIVE_MEMBER_UNTRUSTED')
            total += item.size
            require(total <= 512 * 1024 ** 2, 'ARCHIVE_CONTENT_LIMIT')
            handle = archive.extractfile(item)
            with handle:
                require(len(handle.read(item.size + 1)) == item.size, 'ARCHIVE_CONTENT_TRUNCATED')


def backup(folder, target, *, remote=run_remote):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    folder = private_folder(folder)
    key = AESGCM.generate_key(bit_length=256)
    write(folder / 'backup.key', key)
    report = {'v': 1, 'encrypted': True, 'remote_mutations': False, 'files': {},
              'redis_live_keys_included': False, 'cross_service_atomic': False,
              'limitations': ['Redis live keys are not backed up; existing RDB/AOF is not a current snapshot.',
                              'PostgreSQL and runtime files are captured separately.',
                              'pg_restore -l validates the archive directory, not a full restore.'],
              'started_at': time.time()}
    stage = 'start'

    def encrypt(name, raw):
        nonce = os.urandom(12)
        aad = (RUN + '/' + name).encode()
        cipher = AESGCM(key).encrypt(nonce, raw, aad)
        blob = nonce + cipher
        write(folder / (name + '.aesgcm'), blob)
        # Verify persisted bytes, catching a bad write as well as bad encryption.
        stored = (folder / (name + '.aesgcm')).read_bytes()
        require(sha(AESGCM(key).decrypt(stored[:12], stored[12:], aad)) == sha(raw),
                'DECRYPT_VERIFY_FAILED')
        return {'bytes': len(raw), 'plaintext_sha256': sha(raw), 'ciphertext_sha256': sha(stored),
                'aad': aad.decode(), 'decrypt_verified': True}

    def command(name, script, **kwargs):
        result = remote(target, script, **kwargs)
        receipt = {key: result[key] for key in ('returncode', 'failure')}
        receipt.update(stdout_bytes=len(result['stdout']), stdout_sha256=sha(result['stdout']),
                       stderr_bytes=len(result['stderr']), stderr_sha256=sha(result['stderr']))
        if result['stderr']:
            # Diagnostics can contain SQL/configuration; keep them encrypted too.
            receipt['stderr_artifact'] = encrypt(name + '.stderr', result['stderr'])
        write(folder / (name + '.command.json'), json.dumps(receipt, sort_keys=True).encode())
        require(result['returncode'] == 0 and result['failure'] is None, 'REMOTE_COMMAND_FAILED')
        return result['stdout']

    try:
        for name, script in [('postgres.dump', DUMP), ('runtime.tar.gz', FILES)]:
            stage = name
            raw = command(name, script)
            require(0 < len(raw) <= MAX_CAPTURE, 'BACKUP_SIZE_INVALID')
            if name == 'postgres.dump':
                require(raw.startswith(b'PGDMP'), 'DUMP_HEADER_INVALID')
                stage = 'postgres.toc'
                verification = json.loads(command('postgres.toc', VERIFY_DUMP, input=raw, timeout=120))
                require(verification.get('toc_verified') is True, 'DUMP_TOC_VERIFY_FAILED')
            else:
                validate_archive(raw)
            report['files'][name] = encrypt(name, raw)
            report['files'][name]['archive_structure_verified'] = True
            if name == 'postgres.dump':
                report['files'][name]['toc_sha256'] = verification['toc_sha256']
        report.update(status='complete', completed_at=time.time())
        write(folder / 'verification.json', json.dumps(report, sort_keys=True).encode())
        return report
    except BaseException as error:
        report.update(status='failed', failed_stage=stage, error_type=type(error).__name__,
                      completed_at=time.time())
        write(folder / 'failure.json', json.dumps(report, sort_keys=True).encode())
        raise RuntimeError('backup-failed-inspect-private-receipt') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    if not parser.parse_args(argv).apply:
        print(json.dumps({'offline': True, 'remote_mutations': False, 'encrypted': True,
                          'redis_live_keys_included': False, 'cross_service_atomic': False}))
        return
    channel = runpy.run_path(str(pathlib.Path(__file__).with_name('prepare_channel.py')))
    folder = pathlib.Path.home() / '.codex/backups' / RUN
    print(json.dumps(backup(folder, channel['TARGET']), sort_keys=True))


if __name__ == '__main__':
    main()
