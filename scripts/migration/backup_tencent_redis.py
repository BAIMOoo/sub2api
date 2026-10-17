#!/usr/bin/env python3
"""One-shot Redis replication snapshot, encrypted only on the local machine.

Default offline; --check is read-only. --apply can fork a diskless replication
snapshot and writes a remote intent/receipt, without pausing or restarting Redis.
This Redis snapshot is NOT atomic with the separately captured PostgreSQL backup.
An existing local or remote scope forbids retry, including after unknown outcomes.

Mechanism checked against Redis 8.6.2 src/redis-cli.c:getRDB and
src/redis-check-rdb.c. Diskless --rdb - retains a 40-byte EOF marker on stdout;
we validate the CLI byte count and remove exactly that protocol trailer.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import sys
import time
import types

RUN = 'sub2api-tencent-redis-final-20261017'
REMOTE_SCOPE = '/var/lib/sub2api-migration-20261016/redis-final-backup-20261017'
EXPECTED_RUN_ID = '39d29826637d6bcf89b9630612f7fd0107260394'
HELPER_SHA = '3028771b67a61e21c222fc4dba9973a85219436c8193de2e7ea8d798043d461d'
MAX_RDB = 128 * 1024 ** 2
SOURCE_URLS = ['https://raw.githubusercontent.com/redis/redis/8.6.2/src/redis-cli.c',
               'https://raw.githubusercontent.com/redis/redis/8.6.2/src/redis-check-rdb.c']
SHARED = r'''
def ensure(ok, code):
 if not ok:raise RuntimeError(code)
def snapshot_safety(info,available,settings,memory_limit):
 ensure(info.get('redis_version')=='8.6.2' and info.get('run_id')==EXPECTED_RUN_ID and info.get('role')=='master','REDIS_IDENTITY_CHANGED')
 for key in ('loading','async_loading','rdb_bgsave_in_progress','aof_rewrite_in_progress','aof_rewrite_scheduled','module_fork_in_progress','current_cow_size'):
  ensure(info.get(key)=='0','REDIS_FORK_OR_LOADING_BUSY')
 ensure(info.get('rdb_last_bgsave_status')=='ok','REDIS_LAST_SAVE_FAILED')
 ensure(settings.get('repl-diskless-sync')=='yes' and settings.get('rdbchecksum')=='yes','REDIS_SNAPSHOT_CONFIGURATION_CHANGED')
 ensure(re.fullmatch(r'[0-9]+',settings.get('repl-diskless-sync-delay','')) is not None and int(settings['repl-diskless-sync-delay'])<=10,'REDIS_DISKLESS_DELAY_TOO_LONG')
 for key in ('used_memory','used_memory_rss'):
  ensure(re.fullmatch(r'[1-9][0-9]*',info.get(key,'')) is not None,'REDIS_MEMORY_UNKNOWN')
 used,rss=int(info['used_memory']),int(info['used_memory_rss'])
 ensure(used<=64*1024**2 and rss<=256*1024**2,'REDIS_MEMORY_CAPTURE_LIMIT')
 required=max(512*1024**2,4*rss+2*used)
 ensure(type(available) is int and available>=required,'REDIS_HOST_MEMORY_INSUFFICIENT')
 ensure(type(memory_limit) is int and memory_limit>=0,'REDIS_MEMORY_LIMIT_UNKNOWN')
 if memory_limit:ensure(memory_limit-rss>=2*rss+64*1024**2,'REDIS_CONTAINER_MEMORY_INSUFFICIENT')
 return {'available_bytes':available,'required_available_bytes':required,'used_memory_bytes':used,'rss_bytes':rss,'container_memory_limit_bytes':memory_limit}
def transfer_length(stderr):
 ensure(b'until EOF marker to' in stderr,'REDIS_DISKLESS_TRANSFER_NOT_CONFIRMED')
 matches=re.findall(rb'^Transfer finished with success after ([0-9]+) bytes\r?$',stderr,re.M)
 ensure(len(matches)==1,'REDIS_TRANSFER_COMPLETION_UNKNOWN')
 size=int(matches[0]);ensure(17<=size<=MAX_RDB,'REDIS_TRANSFER_SIZE_INVALID')
 return size
def canonical_rdb(raw,stderr):
 size=transfer_length(stderr)
 ensure(len(raw)==size+40 and re.fullmatch(rb'[0-9a-f]{40}',raw[-40:]) is not None,'REDIS_EOF_TRAILER_INVALID')
 payload=raw[:size]
 ensure(re.fullmatch(rb'REDIS[0-9]{4}',payload[:9]) is not None and payload[-9]==255 and payload[-8:]!=b'\x00'*8,'REDIS_RDB_HEADER_OR_CHECKSUM_DISABLED')
 return payload
'''
exec(SHARED)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def helper():
    path = pathlib.Path(__file__).with_name('backup_tencent_final.py')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        raw = handle.read(128 * 1024)
    ensure(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
           info.st_nlink == 1 and not info.st_mode & 0o022 and sha(raw) == HELPER_SHA,
           'BACKUP_HELPER_PIN_CHANGED')
    module = types.ModuleType('redis_backup_helper')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    module.MAX_CAPTURE = MAX_RDB + 40
    return module


def programs(base, operation):
    ensure(re.fullmatch(r'[0-9a-f]{32}', operation) is not None, 'OPERATION_INVALID')
    common = base.CHECK + '\nimport re,time,subprocess\n' + repr_constants() + SHARED + r'''
def current_safety():
 raw=b.redis_command('INFO');info={}
 wanted={'redis_version','run_id','role','used_memory','used_memory_rss','loading','async_loading','current_cow_size','rdb_bgsave_in_progress','aof_rewrite_in_progress','aof_rewrite_scheduled','module_fork_in_progress','rdb_last_bgsave_status','master_repl_offset'}
 for line in raw.splitlines():
  key,separator,value=line.partition(':')
  if key in wanted:
   ensure(separator and key not in info,'REDIS_INFO_DUPLICATE');info[key]=value
 values=b.redis_command('CONFIG','GET','repl-diskless-sync','repl-diskless-sync-delay','rdbchecksum').splitlines()
 ensure(len(values)==6 and len(set(values[::2]))==3,'REDIS_CONFIG_UNKNOWN')
 settings=dict(zip(values[::2],values[1::2]));available=None
 for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
  if line.startswith('MemAvailable:'):
   match=re.fullmatch(r'MemAvailable:\s+([0-9]+) kB',line);ensure(match is not None,'MEMINFO_UNKNOWN');available=int(match[1])*1024
 actual=b.resource('redis');limit=actual['HostConfig']['Memory']
 memory=snapshot_safety(info,available,settings,limit)
 return {'redis':info,'settings':settings,'memory':memory,'container_id':cfg['redis']['container_id'],'observed_at':time.time()}
'''
    preflight = common + '\nprint(json.dumps(current_safety(),sort_keys=True))\n'
    snapshot = common + '\nscope=pathlib.Path(' + repr(REMOTE_SCOPE) + ')\noperation=' + repr(operation) + r'''
before=current_safety()
r.trusted_directory(scope.parent,private=True)
scope.mkdir(mode=0o700) # Exclusive remote intent: no second snapshot after an unknown outcome.
r.fsync_directory(scope.parent)
r.trusted_directory(scope,private=True)
intent={'v':1,'operation':operation,'run_id':EXPECTED_RUN_ID,'started_at':time.time(),'before':before,'snapshot_command_once':True}
r.atomic_json(scope/'intent.json',intent)
receipt={'v':1,'operation':operation,'run_id':EXPECTED_RUN_ID,'started_at':intent['started_at'],'status':'unknown','before':before}
try:
 # Recheck after durable intent; do not wait/retry if another fork has started.
 current_safety()
 result=subprocess.run(['/usr/bin/docker','exec',cfg['redis']['container_id'],'redis-cli','-h','127.0.0.1','-p','26379','--rdb','-'],stdout=sys.stdout.buffer,stderr=subprocess.PIPE,timeout=90)
 sys.stdout.buffer.flush()
 sys.stderr.buffer.write(result.stderr);sys.stderr.buffer.flush()
 receipt['cli_returncode']=result.returncode;receipt['cli_stderr_sha256']=hashlib.sha256(result.stderr).hexdigest()
 ensure(result.returncode==0,'REDIS_SNAPSHOT_COMMAND_FAILED')
 receipt['canonical_bytes']=transfer_length(result.stderr)
 receipt['after']=current_safety()
 receipt['status']='snapshot_command_completed'
except BaseException as error:
 receipt['error_type']=type(error).__name__
 receipt['finished_at']=time.time();r.atomic_json(scope/'completion.json',receipt)
 raise
receipt['finished_at']=time.time();r.atomic_json(scope/'completion.json',receipt)
'''
    verify = common + '\nscope=pathlib.Path(' + repr(REMOTE_SCOPE) + ')\noperation=' + repr(operation) + r'''
r.trusted_directory(scope,private=True)
receipt=json.loads(r.private_read(scope/'completion.json'))
ensure(receipt['operation']==operation and receipt['run_id']==EXPECTED_RUN_ID and receipt['status']=='snapshot_command_completed' and receipt['cli_returncode']==0,'REDIS_SNAPSHOT_OUTCOME_UNKNOWN')
raw=sys.stdin.buffer.read(MAX_RDB+1)
ensure(17<=len(raw)<=MAX_RDB and len(raw)==receipt['canonical_bytes'],'REDIS_CHECK_INPUT_SIZE')
current_safety()
result=subprocess.run(['/usr/bin/docker','exec','-i',cfg['redis']['container_id'],'redis-check-rdb','/dev/stdin'],input=raw,capture_output=True,timeout=45)
if result.returncode:sys.stderr.buffer.write(result.stdout+result.stderr)
ensure(result.returncode==0 and b'Checksum OK' in result.stdout and b'RDB looks OK!' in result.stdout,'REDIS_CHECK_RDB_FAILED')
after=current_safety()
print(json.dumps({'snapshot_receipt':receipt,'rdb_sha256':hashlib.sha256(raw).hexdigest(),'checker_stdout_sha256':hashlib.sha256(result.stdout).hexdigest(),'checker_verified':True,'postcheck':after},sort_keys=True))
'''
    return preflight, snapshot, verify


def repr_constants():
    return 'EXPECTED_RUN_ID=' + repr(EXPECTED_RUN_ID) + '\nMAX_RDB=' + repr(MAX_RDB) + '\n'


def apply(folder, target, base, *, remote=None, operation=None):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    operation = operation or secrets.token_hex(16)
    preflight, snapshot, verify = programs(base, operation)
    remote = remote or base.run_remote
    folder = base.private_folder(folder)
    key = AESGCM.generate_key(bit_length=256)
    base.write(folder / 'backup.key', key)
    report = {'v':1,'run':RUN,'operation':operation,'encrypted':True,'cross_service_atomic':False,
              'remote_effects':['one diskless replication snapshot may fork Redis', 'private one-shot intent and receipt'],
              'redis_run_id':EXPECTED_RUN_ID,'remote_scope':REMOTE_SCOPE,'started_at':time.time(),
              'references':SOURCE_URLS,'files':{}}
    stage = 'preflight'

    def encrypt(name, raw):
        nonce = os.urandom(12)
        aad = (RUN + '/' + name).encode()
        blob = nonce + AESGCM(key).encrypt(nonce, raw, aad)
        base.write(folder / (name + '.aesgcm'), blob)
        stored = (folder / (name + '.aesgcm')).read_bytes()
        recovered = AESGCM(key).decrypt(stored[:12], stored[12:], aad)
        ensure(sha(recovered) == sha(raw), 'REDIS_CIPHER_VERIFY_FAILED')
        return {'bytes':len(raw),'plaintext_sha256':sha(raw),'ciphertext_sha256':sha(stored),
                'aad':aad.decode(),'decrypt_verified':True}, recovered

    def command(name, script, **kwargs):
        response = remote(target, script, **kwargs)
        receipt = {k:response[k] for k in ('returncode','failure')}
        receipt.update(stdout_bytes=len(response['stdout']),stdout_sha256=sha(response['stdout']),
                       stderr_bytes=len(response['stderr']),stderr_sha256=sha(response['stderr']))
        if response['stderr']:
            receipt['stderr_artifact'] = encrypt(name + '.stderr', response['stderr'])[0]
        base.write(folder / (name + '.command.json'), json.dumps(receipt,sort_keys=True).encode())
        if response['returncode'] or response['failure']:
            if response['stdout']:
                report['files'][name + '.unverified-partial'] = encrypt(name + '.unverified-partial',response['stdout'])[0]
            raise RuntimeError('REDIS_COMMAND_FAILED_OR_UNKNOWN')
        return response

    try:
        response = command('preflight',preflight,timeout=45)
        report['preflight'] = json.loads(response['stdout'])
        stage = 'snapshot'
        base.write(folder / 'snapshot.intent.json', json.dumps({'operation':operation,'remote_scope':REMOTE_SCOPE,'snapshot_may_have_started':True,'at':time.time()},sort_keys=True).encode())
        response = command('snapshot',snapshot,timeout=120) # Exactly one invocation; never retried.
        report['files']['snapshot-wire'] = encrypt('snapshot-wire',response['stdout'])[0]
        report['files']['snapshot-wire'].update(canonical_rdb=False,contains_protocol_trailer=True)
        raw = canonical_rdb(response['stdout'],response['stderr'])
        report['files']['redis.rdb'], recovered = encrypt('redis.rdb',raw)
        stage = 'verify'
        response = command('verify',verify,input=recovered,timeout=90)
        proof = json.loads(response['stdout'])
        ensure(proof.get('checker_verified') is True and proof.get('rdb_sha256')==sha(raw), 'REDIS_VERIFY_PROOF_INVALID')
        report['verification'] = proof
        report.update(status='complete',completed_at=time.time())
        base.write(folder / 'verification.json',json.dumps(report,sort_keys=True).encode())
        return report
    except BaseException as error:
        report.update(status='failed_or_unknown',failed_stage=stage,error_type=type(error).__name__,completed_at=time.time(),
                      retry_forbidden=True)
        base.write(folder / 'failure.json',json.dumps(report,sort_keys=True).encode())
        raise RuntimeError('redis-backup-incomplete-inspect-private-receipt-do-not-retry') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check',action='store_true')
    mode.add_argument('--apply',action='store_true')
    args = parser.parse_args(argv)
    if not (args.check or args.apply):
        print(json.dumps({'offline':True,'cross_service_atomic':False,'snapshot_on_apply_only':True,
                          'snapshot_may_fork_redis':True,'no_pause_or_restart':True}))
        return
    base = helper()
    channel = base.runpy.run_path(str(pathlib.Path(__file__).with_name('prepare_channel.py')))
    if args.check:
        script, _, _ = programs(base,'0'*32)
        result = base.run_remote(channel['TARGET'],script,timeout=45)
        ensure(result['returncode']==0 and result['failure'] is None,'REDIS_PREFLIGHT_FAILED')
        print(json.dumps({'read_only':True,'preflight':json.loads(result['stdout'])},sort_keys=True))
        return
    folder = pathlib.Path.home() / '.codex/backups' / RUN
    print(json.dumps(apply(folder,channel['TARGET'],base),sort_keys=True))


if __name__=='__main__':
    main()
