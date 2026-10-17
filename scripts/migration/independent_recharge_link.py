#!/usr/bin/env python3
"""One-shot correction of the balance notification link on the Tencent primary.

Only scheme and authority change. Original values stay in root-only evidence;
SSH and psql receive private payloads on stdin. Default execution is offline.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import runpy
import stat
import subprocess
import time
import types
import urllib.parse
import uuid

HERE = pathlib.Path(__file__).parent
POST_SHA = '4ad1856dc17f1de1818a80c3100f950ad9e02c309e7ba737464838db8c541725'
TARGET = '43.165.182.52'
KEY = 'balance_low_notify_recharge_url'
OLD_PREFIX = 'http://13.231.143.136:8081'
NEW_PREFIX = 'https://api.b4im.com'
SYSTEM_ID = '7626048206782038050'
TIMELINE = 2
SCOPE = pathlib.Path('/var/lib/sub2api-migration-20261016/recharge-link-independent-20261017')


def need(condition, code):
    if not condition:
        raise RuntimeError(code)


def transform(value):
    need(type(value) is str and 0 < len(value) <= 8192 and
         not any(ord(c) < 33 or ord(c) == 127 for c in value), 'invalid-original-url')
    try:
        parsed = urllib.parse.urlsplit(value)
        need(parsed.scheme == 'http' and parsed.netloc == '13.231.143.136:8081' and
             parsed.hostname == '13.231.143.136' and parsed.port == 8081 and
             value.startswith(OLD_PREFIX), 'unexpected-original-url')
    except ValueError:
        raise RuntimeError('invalid-original-url') from None
    suffix = value[len(OLD_PREFIX):]
    need(not suffix or suffix[0] in '/?#', 'invalid-url-boundary')
    # Replacing the prefix directly retains even empty '?' / '#' delimiters.
    return NEW_PREFIX + suffix


def validate_row(row):
    need(type(row) is dict and set(row) == {'id', 'key', 'value', 'xmin', 'updated_at'} and
         type(row['id']) is int and row['id'] > 0 and row['key'] == KEY and
         type(row['xmin']) is str and re.fullmatch(r'[0-9]{1,20}', row['xmin']) and
         type(row['updated_at']) is str and row['updated_at'], 'unexpected-setting-row')
    return row


def validate_primary(proof):
    need(type(proof) is dict and proof.get('system_identifier') == SYSTEM_ID and
         proof.get('pg_wal_timeline') == TIMELINE and proof.get('primary') is True and
         proof.get('pg_authority') == proof.get('redis_authority') == 'TENCENT' and
         proof.get('persistent_endpoints') == {'v': 1, 'postgres': 'target', 'redis': 'target'} and
         proof.get('app_ready') is True and proof.get('background_ready') is True,
         'tencent-primary-not-confirmed')
    return proof


def private_dir(folder):
    for path in [folder, *folder.parents]:
        info = path.lstat()
        need(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and
             info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o022,
             'untrusted-journal-directory')
    need(folder.stat().st_uid == os.geteuid() and stat.S_IMODE(folder.stat().st_mode) == 0o700,
         'journal-directory-not-private')


def write_private(path, value, replace=False):
    private_dir(path.parent)
    need(not path.is_symlink(), 'journal-symlink-refused')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp') if replace else path
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def update_sql(before, replacement):
    validate_row(before)
    need(transform(before['value']) == replacement, 'replacement-out-of-scope')
    # COPY data does not become an SQL literal or bind parameter in statement
    # logs. Hex encoding removes COPY escaping ambiguity, including backslashes.
    data = '\t'.join((str(before['id']), before['xmin'],
                      before['value'].encode().hex(), replacement.encode().hex()))
    return ("BEGIN; SET LOCAL statement_timeout='2s'; SET LOCAL lock_timeout='1s'; "
        "SET LOCAL log_statement='none'; SET LOCAL log_duration=off; "
        "SET LOCAL log_min_error_statement='panic'; SET LOCAL log_error_verbosity='terse';\n"
        "DO $guard$ BEGIN IF pg_is_in_recovery() OR "
        "(SELECT system_identifier::text FROM pg_control_system()) <> '" + SYSTEM_ID + "' OR "
        "(SELECT timeline_id FROM pg_control_checkpoint()) <> " + str(TIMELINE) + " THEN "
        "RAISE EXCEPTION 'wrong-primary'; END IF; END $guard$;\n"
        "CREATE TEMP TABLE recharge_expected (id bigint, xmin_text text, old_hex text, new_hex text) ON COMMIT DROP;\n"
        "COPY recharge_expected FROM STDIN;\n" + data + "\n\\.\n"
        "DO $cas$ DECLARE affected integer; BEGIN "
        "UPDATE public.settings AS s SET value=convert_from(decode(e.new_hex,'hex'),'UTF8'),updated_at=clock_timestamp() "
        "FROM recharge_expected AS e WHERE s.key='" + KEY + "' AND s.id=e.id AND s.xmin::text=e.xmin_text "
        "AND s.value=convert_from(decode(e.old_hex,'hex'),'UTF8'); "
        "GET DIAGNOSTICS affected=ROW_COUNT; IF affected <> 1 THEN "
        "RAISE EXCEPTION 'setting-cas-conflict'; END IF; END $cas$;\n"
        "SELECT row_to_json(r) FROM (SELECT s.id,s.key,s.value,s.xmin::text AS xmin,s.updated_at "
        "FROM public.settings AS s JOIN recharge_expected AS e ON s.id=e.id "
        "WHERE s.key='" + KEY + "') AS r; COMMIT;\n")


class Backend:
    def __init__(self, library):
        self.library = library
        rpc, cfg = library.load_target()
        self.backend = library.TargetBackend(rpc, cfg)

    def preflight(self):
        return validate_primary(self.backend.preflight())

    def read(self):
        return validate_row(self.backend.sql("SELECT row_to_json(r) FROM "
            "(SELECT id,key,value,xmin::text AS xmin,updated_at FROM public.settings "
            "WHERE key='" + KEY + "') AS r"))

    def update_once(self, before, replacement):
        self.preflight()
        b = self.backend
        raw = b.b.command(['/usr/bin/docker', 'exec', '--user', 'postgres', '-i', self.library.PG_ID,
            'psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1', '-h', '/tmp', '-p', '25432',
            '-U', 'sub2api', '-d', 'sub2api'], update_sql(before, replacement).encode())
        return validate_row(b.r.strict_json(raw, 65536))


def transaction(backend, folder):
    private_dir(folder)
    need(not any(folder.iterdir()), 'existing-evidence-refuses-repeat')
    report = {'v': 1, 'key': KEY, 'ok': False, 'mutation_attempted': False,
              'automatic_retry': False, 'started_at': time.time()}
    write_private(folder / 'report.json', report)
    try:
        report['before_primary'] = validate_primary(backend.preflight())
        before = validate_row(backend.read())
        replacement = transform(before['value'])
        write_private(folder / 'before.json', before)
        # Abort before intent if a concurrent admin has changed the setting.
        need(backend.read() == before, 'setting-changed-before-intent')
        need(validate_primary(backend.preflight()) == report['before_primary'], 'target-runtime-changed')
        write_private(folder / 'intent.json', {'key': KEY, 'expected': before,
            'replacement': replacement, 'created_at': time.time(), 'no_repeat': True})
        report.update(mutation_attempted=True, write_intent_durable=True)
        write_private(folder / 'report.json', report, replace=True)
        # Any lost response is reconciled with one readback; never retry writes.
        try:
            changed = backend.update_once(before, replacement)
        except Exception:
            changed = None
            report['write_response_unavailable'] = True
        after = validate_row(backend.read())
        write_private(folder / 'after.json', after)
        need(after['id'] == before['id'] and after['key'] == KEY and after['value'] == replacement and
             after['xmin'] != before['xmin'] and (changed is None or after == changed),
             'setting-write-not-confirmed')
        report['after_primary'] = validate_primary(backend.preflight())
        need(report['after_primary'] == report['before_primary'], 'target-runtime-changed')
        report.update(ok=True, scheme_authority_replaced=True, suffix_preserved=True,
                      app_restart_performed=False, notification_sent=False)
    except Exception:
        report['error'] = 'recharge-link-failed-inspect-private-evidence-no-repeat'
    report['finished_at'] = time.time()
    write_private(folder / 'report.json', report, replace=True)
    return report


def remote(library_raw, inspect=False):
    need(os.geteuid() == 0 and os.uname().sysname == 'Linux', 'tencent-root-required')
    need(hashlib.sha256(library_raw).hexdigest() == POST_SHA, 'reviewed-library-changed')
    library = types.ModuleType('recharge_target_library')
    exec(compile(library_raw, 'reviewed-target-library', 'exec'), library.__dict__)
    library.private_root_dir(SCOPE.parent, private=True)
    if inspect:
        private_dir(SCOPE)
        return json.loads(library.root_read(SCOPE / 'report.json'))
    # mkdir is itself the durable exclusive reservation; never reuse a scope.
    SCOPE.mkdir(mode=0o700)
    fd = os.open(SCOPE.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return transaction(Backend(library), SCOPE)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--inspect', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply and not args.inspect:
        print(json.dumps({'offline': True, 'key': KEY, 'target_address': TARGET,
                          'changed': False, 'private_evidence': str(SCOPE)}))
        return 0
    library = (HERE / 'postcutover_canary.py').read_bytes()
    need(hashlib.sha256(library).hexdigest() == POST_SHA, 'reviewed-library-changed')
    command = runpy.run_path(str(HERE / 'prepare_channel.py'))['TARGET']
    need(command[-1] == 'ubuntu@43.165.177.136' and 'StrictHostKeyChecking=yes' in command,
         'reviewed-target-transport-changed')
    command = command[:-1] + ['ubuntu@' + TARGET, 'sudo -n python3 -I -']
    source = "__name__='recharge_link_remote'\n__file__='independent_recharge_link.py'\n" + pathlib.Path(__file__).read_text()
    source += ("\ntry:\n result=remote(base64.b64decode(" + repr(base64.b64encode(library).decode()) +
               "), inspect=" + repr(args.inspect) + ")\n print(json.dumps(result),flush=True)\n"
               "except Exception:\n print(json.dumps({'ok':False,'error':'recharge-remote-refused-no-repeat'}),flush=True)\n")
    try:
        result = subprocess.run(command, input=source.encode(), capture_output=True, timeout=40)
        need(result.returncode == 0, 'remote-unavailable')
        value = json.loads(result.stdout)
    except Exception:
        value = {'ok': False, 'error': 'recharge-transport-uncertain-inspect-private-evidence-no-repeat'}
    print(json.dumps({**value, 'private_evidence': str(SCOPE)}))
    return 0 if value.get('ok') is True else 1


if __name__ == '__main__':
    raise SystemExit(main())
