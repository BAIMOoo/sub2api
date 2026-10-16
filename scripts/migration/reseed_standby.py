#!/usr/bin/env python3
"""Re-seed the unpromoted, fenced Tencent standby; default is offline.

Run only as the installed root program. Each mutation has an exclusive intent;
an interrupted step requires inspection, never automatic replay. Source writes,
promotion, application signals and network routing are deliberately absent.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import time

ROOT = pathlib.Path('/var/lib/sub2api-migration-20261016')
SCOPE = ROOT / 'standby-reseed-20261017'
RPC = pathlib.Path('/usr/local/libexec/sub2api-migration-20261016/target_rpc.py')
RPC_SHA = '4681f2e7203623b39d8f861811cb00cace10dd17f61bc447a3821196587acb9c'
CID = 'ac9f494b2fd0ec87257a9a7308a6ad7b270051d1dc4da33b33fea9af2885418c'
# The standby runs the authority wrapper image derived from PostgreSQL 18.3.
# Reuse its actual pinned image rather than the upstream base image ID.
IMAGE = 'sha256:9a5296338b4957a7082ad925c9f31346f6f1cb6b7cbe7bd2971430432488a395'
OWNER = '30bda011defad8149621c2630a2bd89c'
SYSTEM = '7626048206782038050'
HELPER = 'sub2api-standby-reseed-backup-20261017'
MONITOR = 'sub2api-pg-authority-20261016-monitor'
DATA = '/var/lib/postgresql/data'
GIB = 1024**3


def need(ok, code):
    if not ok:
        raise RuntimeError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(value)
        out.flush()
        os.fsync(out.fileno())
    sync(path.parent)


def command(argv, timeout=8):
    try:
        r = subprocess.run(argv, input=b'', capture_output=True, timeout=timeout,
                           env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('COMMAND_UNKNOWN_INSPECT_ONLY') from None
    need(r.returncode == 0, 'COMMAND_FAILED_INSPECT_ONLY')
    return r.stdout


def load():
    need(os.geteuid() == 0, 'ROOT_REQUIRED')
    spec = importlib.util.spec_from_file_location('reseed_target_rpc', RPC)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    need(sha(m.private_read(RPC, mode=0o700)) == RPC_SHA, 'RPC_CHANGED')
    cfg = m.load_config()
    need(cfg['owner'] == OWNER and cfg['pg']['container_id'] == CID and
         cfg['pg']['image_id'] == IMAGE and cfg['pg']['system_identifier'] == SYSTEM,
         'PG_IDENTITY_CHANGED')
    m.trusted_directory(ROOT, private=True)
    return m, cfg, m.LocalBackend(cfg), m.FileStore(cfg)


def boundary(m, b, store):
    s = store.read()
    need(s['pg']['authority'] == s['redis']['authority'] == 'AWS' and
         s['pg'].get('promotion_intent') is None and
         (not s['pg'].get('arm') or s['pg']['arm']['status'] == 'CANCELLED') and
         all(x['phase'] == 'COMPLETE' for x in s['operations'].values()) and
         all(x.get('cancelled') is True for x in s['operations'].values()
             if x['request']['action'] == 'app.quiesce'), 'TARGET_NOT_RECOVERED')
    need(b.endpoint_state() == {'v': 1, 'postgres': 'source', 'redis': 'source'},
         'ENDPOINTS_CHANGED')
    rt = b.app_control('runtime.status')['body']
    need(rt['ready'] is True and rt['serving'] is True and not rt['draining'] and
         rt['ingress']['paused'] is False and rt['background_workers_enabled'] is False,
         'APP_NOT_SERVING_AWS')
    for kind, field, value in [('postgres', 'endpoint', 'source'),
                               ('redis', 'address', '127.0.0.1:16379')]:
        c = b.app_control(kind + '.status')['body']
        need(c[field] == value and c['paused'] is False, 'APP_ENDPOINT_CHANGED')
    state = b.pg_manifest()
    need(state['authority_mode'] == 'STANDBY' and not state.get('promotion_intent'),
         'STANDBY_AUTHORITY_REQUIRED')
    return s


def inspect_pg(m, cfg, b, running):
    row = b.docker_json('container', CID)
    need(row['Id'] == CID and row['Image'] == IMAGE and
         m.sha(row['Config']) == cfg['pg']['config_sha256'] and
         row['State']['Running'] is running and
         row['HostConfig']['RestartPolicy']['Name'] == 'no' and
         row['HostConfig']['NetworkMode'] == 'host', 'ORIGINAL_STANDBY_CHANGED')
    if not running:
        need(row['State']['Status'] == 'exited' and row['State']['ExitCode'] == 0 and
             row['State']['OOMKilled'] is False, 'STANDBY_NOT_CLEAN_STOPPED')
    mounts = [x for x in row['Mounts'] if x['Destination'] == DATA]
    need(len(mounts) == 1 and mounts[0]['Type'] == 'volume' and mounts[0]['RW'] is True and
         mounts[0]['Source'] == cfg['pg']['volume_path'], 'VOLUME_CHANGED')
    path = pathlib.Path(cfg['pg']['volume_path'])
    need(path.resolve(strict=True) == path and path.is_dir(), 'DATA_PATH_CHANGED')
    volume = b.docker_json('volume', mounts[0]['Name'])
    need(volume['Driver'] == 'local' and not volume.get('Options') and
         volume['Mountpoint'] == str(path), 'PLAIN_LOCAL_VOLUME_REQUIRED')
    return row, path


def prepare():
    m, cfg, b, store = load()
    need(not SCOPE.exists() and not SCOPE.is_symlink(), 'ALREADY_PREPARED_INSPECT_ONLY')
    with store.mutation_lock(), b.pg_locks():
        boundary(m, b, store)
        row, path = inspect_pg(m, cfg, b, True)
        pg = b.pg_sample()
        need(pg['in_recovery'] is True and pg['system_identifier'] == SYSTEM and
             pg['source_timeline'] == 1, 'NOT_ORIGINAL_UNPROMOTED_STANDBY')
        need(__import__('shutil').disk_usage(path).free >= 6*GIB, 'RESEED_DISK_MARGIN')
        # No other running container may retain a mount of the old directory.
        ids = command(['docker', 'ps', '-q']).decode().split()
        others = json.loads(command(['docker', 'inspect', *ids])) if ids else []
        need(all(x['Id'] == CID or not any(y['Source'] == str(path)
             for y in x['Mounts']) for x in others), 'OTHER_RUNNING_VOLUME_USER')
        SCOPE.mkdir(mode=0o700)
        sync(ROOT)
        plan = {'v': 1, 'run': '20261016', 'owner': OWNER, 'at': time.time(),
                'old_pid': row['State']['Pid'], 'container_id': CID, 'image_id': IMAGE,
                'config_sha256': m.sha(cfg), 'pg_config_sha256': cfg['pg']['config_sha256'],
                'ledger_sha256': sha(m.private_read(ROOT/'rpc/state.json')),
                'authority_sha256': sha(m.private_read(ROOT/'postgres-authority/state.json')),
                'volume_path': str(path), 'pg_before': pg,
                'old_control_sha256': sha((path/'global/pg_control').read_bytes())}
        write_new(SCOPE/'plan.json', canonical(plan))
        write_new(SCOPE/'monitor-stop.intent.json', canonical({'at': time.time()}))
        command(['systemctl', 'stop', MONITOR+'.timer'])
        # Stopping a timer does not stop an already executing oneshot. Wait;
        # never cancel it halfway through its standby safety decision.
        end = time.monotonic()+8
        while True:
            fields = dict(x.split('=', 1) for x in command(
                ['systemctl', 'show', MONITOR+'.service', '--property=MainPID,ActiveState,Job']).decode().splitlines())
            if fields['MainPID'] == '0' and not fields['Job'] and fields['ActiveState'] in ('inactive', 'failed'):
                break
            need(time.monotonic() < end, 'MONITOR_STILL_EXECUTING')
            time.sleep(.1)
        write_new(SCOPE/'monitor-stopped.json', canonical(fields))
        write_new(SCOPE/'stop.intent.json', canonical({'signal': 'SIGINT', 'id': CID}))
        command(['docker', 'kill', '--signal', 'SIGINT', CID])
        write_new(SCOPE/'stop.signal-accepted.json', canonical({'at': time.time()}))
    print(json.dumps({'prepared': True, 'standby_stop_signalled_once': True,
                      'old_pid': plan['old_pid'], 'application_signals': False}))


def plan_and_stopped(m, cfg, b, store):
    m.trusted_directory(SCOPE, private=True)
    p = m.strict_json(m.private_read(SCOPE/'plan.json'))
    need(p['container_id'] == CID and p['config_sha256'] == m.sha(cfg), 'PLAN_CHANGED')
    boundary(m, b, store)
    need(p['ledger_sha256'] == sha(m.private_read(ROOT/'rpc/state.json')) and
         p['authority_sha256'] == sha(m.private_read(ROOT/'postgres-authority/state.json')),
         'AUTHORITY_OR_LEDGER_CHANGED')
    row, path = inspect_pg(m, cfg, b, False)
    return p, row, path


def backup():
    m, cfg, b, store = load()
    with store.mutation_lock(), b.pg_locks():
        p, _, path = plan_and_stopped(m, cfg, b, store)
        # This fresh source proof is copied by the coordinating client only
        # after the guarded owned LOGIN/slot recovery has committed.
        proof = m.strict_json(m.private_read(SCOPE/'source-ready.json'))
        need(proof['owner'] == OWNER and proof['healthy'] is True and
             proof['role_login'] is True and proof['slot'] == 'sub2api_mig_20261016' and
             0 <= time.time()-proof['checked_at'] <= 30, 'FRESH_SOURCE_READY_REQUIRED')
        need(not (SCOPE/'backup.intent.json').exists(), 'BACKUP_ALREADY_ATTEMPTED_INSPECT_ONLY')
        need(__import__('shutil').disk_usage(path).free >= 6*GIB, 'RESEED_DISK_MARGIN')
        dest = SCOPE/'new-data'
        dest.mkdir(mode=0o700)
        need(dest.stat().st_dev == path.stat().st_dev, 'RESEED_MUST_USE_SAME_FILESYSTEM')
        write_new(SCOPE/'backup.intent.json', canonical({'at': time.time(), 'helper': HELPER}))
        shell = '''set -eu
umask 077
mkdir -p /run/sub2api-migration
cp /run/replication-input.pgpass /run/sub2api-migration/replication.pgpass
chmod 600 /run/sub2api-migration/replication.pgpass
chown -R postgres:postgres /run/sub2api-migration /var/lib/postgresql/data
export PGPASSFILE=/run/sub2api-migration/replication.pgpass
export PGAPPNAME=sub2api-pg-replica-20261016
export PGSSLMODE=disable PGCONNECT_TIMEOUT=10
gosu postgres pg_basebackup --host=127.0.0.1 --port=15432 --username=sub2api_mig_20261016_replica --pgdata=/var/lib/postgresql/data --format=plain --wal-method=stream --slot=sub2api_mig_20261016 --write-recovery-conf --checkpoint=spread --max-rate=8M --manifest-checksums=SHA256 --no-clean --no-password
gosu postgres pg_verifybackup --exit-on-error /var/lib/postgresql/data
test -f /var/lib/postgresql/data/standby.signal
test "$(cat /var/lib/postgresql/data/PG_VERSION)" = 18
touch /var/lib/postgresql/data/.sub2api-seed-verified
chown postgres:postgres /var/lib/postgresql/data/.sub2api-seed-verified
sync
'''
        argv = ['docker', 'create', '--name', HELPER, '--label', 'io.sub2api.migration.owner='+OWNER,
                '--label', 'io.sub2api.migration.run=sub2api-reseed-20261017', '--network', 'host',
                '--restart', 'no', '--memory', '256m', '--memory-swap', '256m', '--cpus', '0.5',
                '--pids-limit', '64', '--security-opt', 'no-new-privileges:true',
                '--tmpfs', '/run/sub2api-migration:rw,noexec,nosuid,size=1m,mode=0700',
                '--mount', 'type=bind,src='+str(dest)+',dst='+DATA,
                '--mount', 'type=bind,src='+str(ROOT/'postgres-authority/replication.pgpass')+
                ',dst=/run/replication-input.pgpass,readonly', '--entrypoint', '/bin/sh',
                IMAGE, '-ceu', shell]
        hid = command(argv).decode().strip()
        write_new(SCOPE/'backup.container.json', canonical({'id': hid, 'at': time.time()}))
        command(['docker', 'start', hid])
        write_new(SCOPE/'backup.started.json', canonical({'at': time.time(), 'id': hid}))
    print(json.dumps({'backup_started': True, 'helper_id': hid, 'production_data_authority': 'AWS'}))


def move_data(old, dest, archive):
    """Keep Docker's volume root inode; journal every child rename.

    The caller holds authority/mutation locks, has proved every mount user
    stopped, and must never start PostgreSQL unless this returns successfully.
    O_EXCL intents make a crash require inspection instead of mixing two seeds.
    """
    need(not archive.exists() and not archive.is_symlink(), 'OLD_ARCHIVE_ALREADY_EXISTS')
    need(old.stat().st_dev == dest.stat().st_dev == archive.parent.stat().st_dev,
         'DATA_MOVE_FILESYSTEM_CHANGED')
    old_inode = old.stat().st_ino
    need(not old.is_symlink() and not dest.is_symlink(), 'DATA_MOVE_SYMLINK')
    need(not (old/'pg_wal').is_symlink() and not any((old/'pg_tblspc').iterdir()) and
         not (dest/'pg_wal').is_symlink() and not any((dest/'pg_tblspc').iterdir()),
         'EXTERNAL_WAL_OR_TABLESPACE_UNSUPPORTED')
    archive.mkdir(mode=0o700)
    sync(archive.parent)
    journal = archive.parent/'moves'
    journal.mkdir(mode=0o700)
    sync(journal.parent)
    for side, src, target in [('old', old, archive), ('new', dest, old)]:
        # Remove the old marker first and install the new marker last. Only
        # the single final start is allowed; restart policy stays disabled.
        names = sorted(src.iterdir(), key=lambda p: (
            (p.name != '.sub2api-seed-verified') if side == 'old'
            else (p.name == '.sub2api-seed-verified'), p.name))
        for index, path in enumerate(names):
            info = path.lstat()
            record = {'side': side, 'name': path.name, 'inode': info.st_ino, 'device': info.st_dev}
            write_new(journal/(side+'-'+str(index)+'.intent.json'), canonical(record))
            need(path.lstat().st_ino == info.st_ino and not (target/path.name).exists(),
                 'DATA_MOVE_CAS_CHANGED')
            os.rename(path, target/path.name)
            sync(src)
            sync(target)
            write_new(journal/(side+'-'+str(index)+'.complete.json'), canonical(record))
    need(old.stat().st_ino == old_inode and not list(dest.iterdir()), 'VOLUME_ROOT_CHANGED')


def validate_recovery_connection(auto):
    # pg_basebackup quotes conninfo values, then quotes that whole string as a
    # PostgreSQL GUC. Decode both layers before comparing exact destinations.
    keys = re.findall(r'^[ \t]*(primary_slot_name|primary_conninfo)[ \t]*=', auto, re.M)
    slots = re.findall(r"^[ \t]*primary_slot_name[ \t]*=[ \t]*'([^'\r\n]*)'[ \t]*$", auto, re.M)
    lines = re.findall(r"^[ \t]*primary_conninfo[ \t]*=[ \t]*'((?:[^'\r\n]|'')*)'[ \t]*$", auto, re.M)
    need(sorted(keys) == ['primary_conninfo', 'primary_slot_name'] and
         slots == ['sub2api_mig_20261016'] and len(lines) == 1,
         'RECOVERY_CONNECTION_SETTINGS_CHANGED')
    try:
        values = [part.split('=', 1) for part in shlex.split(lines[0].replace("''", "'"))]
        need(all(len(row) == 2 for row in values), 'RECOVERY_CONNECTION_SETTINGS_CHANGED')
        conn = dict(values)
    except ValueError:
        raise RuntimeError('RECOVERY_CONNECTION_SETTINGS_CHANGED') from None
    wanted = {'user': 'sub2api_mig_20261016_replica', 'host': '127.0.0.1', 'port': '15432',
              'passfile': '/run/sub2api-migration/replication.pgpass',
              'application_name': 'sub2api-pg-replica-20261016', 'sslmode': 'disable'}
    need(len(conn) == len(values) and all(conn.get(k) == v for k, v in wanted.items()) and
         not {'password', 'hostaddr', 'service', 'servicefile'} & conn.keys(),
         'RECOVERY_CONNECTION_SETTINGS_CHANGED')


def install():
    m, cfg, b, store = load()
    with store.mutation_lock(), b.pg_locks():
        p, _, old = plan_and_stopped(m, cfg, b, store)
        hid = m.strict_json(m.private_read(SCOPE/'backup.container.json'))['id']
        helper = b.docker_json('container', hid)
        need(helper['Name'] == '/'+HELPER and helper['Image'] == IMAGE and
             helper['Config']['Labels'].get('io.sub2api.migration.owner') == OWNER and
             helper['State']['Running'] is False and helper['State']['ExitCode'] == 0 and
             helper['State']['OOMKilled'] is False, 'BACKUP_NOT_VERIFIED_AND_CLEAN_EXITED')
        dest = SCOPE/'new-data'
        need(dest.resolve(strict=True) == dest and (dest/'standby.signal').is_file() and
             (dest/'.sub2api-seed-verified').is_file() and
             (dest/'PG_VERSION').read_text().strip() == '18', 'NEW_SEED_NOT_VERIFIED')
        need(dest.stat().st_dev == old.stat().st_dev == SCOPE.stat().st_dev,
             'RESEED_FILESYSTEM_CHANGED')
        need(not (old/'postmaster.pid').exists(), 'OLD_POSTMASTER_PID_PRESENT')
        # Reject orphan namespace mounts, including containers no longer
        # reported running by Docker. Docker's local volume root stays put.
        needles = (str(old), str(dest))
        for proc in pathlib.Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                text = (proc/'mountinfo').read_text()
            except FileNotFoundError:
                continue
            need(not any(any(needle in line for needle in needles) and (' '+DATA+' ') in line
                         for line in text.splitlines()),
                 'OLD_VOLUME_NAMESPACE_STILL_MOUNTED')
        auto = (dest/'postgresql.auto.conf').read_text()
        validate_recovery_connection(auto)
        need(not (SCOPE/'install.intent.json').exists(), 'INSTALL_ALREADY_ATTEMPTED_INSPECT_ONLY')
        receipt = {'old_pid': p['old_pid'], 'old_control_sha256': p['old_control_sha256'],
                   'new_manifest_sha256': sha((dest/'backup_manifest').read_bytes()),
                   'helper_id': hid, 'old_data_retained': True, 'at': time.time(),
                   'container_id_unchanged': CID, 'volume_path_unchanged': str(old)}
        write_new(SCOPE/'install.intent.json', canonical(receipt))
        move_data(old, dest, SCOPE/'retained-old-data')
        write_new(SCOPE/'new-data.installed.json', canonical(receipt))
        command(['docker', 'start', CID])
        write_new(SCOPE/'new-standby.started.json', canonical({'at': time.time()}))
        end = time.monotonic()+30
        while True:
            try:
                pg = b.pg_sample()
                need(pg['in_recovery'] is True and pg['system_identifier'] == SYSTEM and
                     pg['source_timeline'] == 1, 'NEW_STANDBY_IDENTITY_FAILED')
                query = b"SELECT json_build_object('receivers',(SELECT json_agg(json_build_object('status',status,'slot',slot_name,'timeline',received_tli)) FROM pg_stat_wal_receiver),'read_only',current_setting('transaction_read_only'));"
                result = m.strict_json(b.command(['docker','exec','--user','postgres','-i',CID,
                    'psql','-XAtq','-v','ON_ERROR_STOP=1','-h','/tmp','-p','25432','-U','sub2api','-d','sub2api'], query))
                need(result['read_only'] == 'on' and result['receivers'] == [
                    {'status': 'streaming', 'slot': 'sub2api_mig_20261016', 'timeline': 1}],
                    'NEW_STANDBY_NOT_STREAMING')
                need(m.lsn_number(pg['replay_lsn']) >= m.lsn_number('8/D04ADEC8'),
                     'RECOVERED_DIAGNOSTICS_NOT_REPLAYED')
                break
            except (m.Refusal, m.Uncertain, RuntimeError):
                need(time.monotonic() < end, 'NEW_STANDBY_VERIFICATION_PENDING_INSPECT_ONLY')
                time.sleep(.2)
        receipt.update(new_pid=b.resource('pg')['State']['Pid'], pg_after=pg,
                       volume_root_inode=old.stat().st_ino,
                       new_control_sha256=sha((old/'global/pg_control').read_bytes()))
        write_new(SCOPE/'verification.json', canonical(receipt))
        command(['systemctl', 'start', MONITOR+'.timer'])
        write_new(SCOPE/'monitor-resumed.json', canonical({'at': time.time()}))
    print(json.dumps({'new_standby_started': True, 'streaming_verified': True, **receipt}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', nargs='?', choices=['prepare', 'backup', 'install'])
    args = parser.parse_args(argv)
    if args.step is None:
        print(json.dumps({'offline': True, 'source_mutations': False, 'promotion': False,
                          'old_data_retained': True, 'application_signals': False}))
        return
    {'prepare': prepare, 'backup': backup, 'install': install}[args.step]()


if __name__ == '__main__':
    main()
