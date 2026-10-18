"""Privileged read-only snapshot helper. Not imported by the probe runner.

The Docker socket and database credentials stay in this root process. Only a
sanitized state receipt is published to the unprivileged monitor.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import time

from .evidence import atomic_json, canonical


def inspect(name):
    return json.loads(subprocess.check_output(['docker', 'inspect', name], stderr=subprocess.DEVNULL))[0]


def query(app, pg_container, sql, port=25432):
    env = dict(v.split('=', 1) for v in app['Config']['Env'] if '=' in v)
    cmd = ['docker', 'exec', '-i', pg_container, 'sh', '-c',
           'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$1" -U "$2" -d "$3" -At',
           'sh', str(port), env['DATABASE_USER'], env['DATABASE_DBNAME']]
    result = subprocess.run(cmd, input=(env['DATABASE_PASSWORD'] + '\n' + sql).encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
    if result.returncode:
        raise ValueError('database_snapshot_failed')
    return json.loads(result.stdout)


def snapshot(config):
    app = inspect(config['app_container'])
    account_id, group_id = int(config['account_id']), int(config['group_id'])
    # Credentials and usage windows are deliberately absent. Unknown future
    # relevant settings remain part of the generation hash.
    sql = '''select json_build_object(
      'account', (select json_build_object('id',id,'type',type,'status',status,
        'schedulable',schedulable,'proxy_id',proxy_id,'model_mapping',credentials->'model_mapping',
        'fingerprint',extra->'codex_fingerprint_mode','tls',extra->'enable_tls_fingerprint',
        'seed',extra->'codex_fingerprint_seed','device',extra->'openai_device_id',
        'oauth_account',credentials->>'chatgpt_account_id')
        from accounts where id=%d and deleted_at is null),
      'proxy', (select to_jsonb(p)-'name'-'created_at'-'updated_at' from proxies p
        where id=(select proxy_id from accounts where id=%d)),
      'group', (select to_jsonb(g)-'name'-'description'-'created_at'-'updated_at' from groups g where id=%d),
      'settings',(select jsonb_object_agg(key,value) from settings where key like '%%codex%%'
        or key like '%%fingerprint%%' or key like 'openai_%%'));
    ''' % (account_id, account_id, group_id)
    data = query(app, config['pg_container'], sql)
    health = False
    conn = http.client.HTTPConnection('127.0.0.1', int(config['gateway_port']), timeout=5)
    try:
        conn.request('GET', '/health')
        health = conn.getresponse().status == 200
    finally:
        conn.close()
    material = {'image': app['Image'], 'config': app['Config'], 'host': app['HostConfig'], 'db': data}
    # Keyed digest prevents guessing private config values from this public-to-
    # the-monitor receipt; no environment or DB row reaches the output.
    import hmac
    generation = hmac.new(config['snapshot_secret'].encode(), canonical(material), hashlib.sha256).hexdigest()
    labels = app['Config'].get('Labels') or {}
    output = {'schema': 1, 'at': time.time(), 'generation': generation, 'container_id': app['Id'],
              'image': app['Image'], 'running': app['State']['Running'], 'health_ok': health,
              'source': labels.get('io.sub2api.alignment.source', labels.get('io.sub2api.migration.baseline')),
              'binary_sha256': labels.get('io.sub2api.alignment.binary', labels.get('io.sub2api.migration.binary')),
              'account_id': account_id, 'proxy_id': data['account']['proxy_id'],
              'oauth': data['account']['type'] == 'oauth',
              'oauth_account_sha256': hashlib.sha256((data['account'].get('oauth_account') or '').encode()).hexdigest(),
              'account_available': data['account']['status'] == 'active' and data['account']['schedulable']}
    atomic_json(config['output'], output)
    os.chown(config['output'], int(config['monitor_uid']), int(config['monitor_gid']))
    return output


def clean_observer():
    """Root owns cleanup of the app's group-readable, sanitized spool only."""
    import re
    import shutil
    root = Path('/var/lib/sub2api-alignment')
    for directory in (root / 'observer').iterdir():
        if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', directory.name):
            continue
        report = root / 'runs' / directory.name / 'report.json'
        info = json.loads(report.read_text()) if report.is_file() and not report.is_symlink() else {}
        days = 30 if info.get('status') == 'pass' else 90
        # Never remove a live run. Unknown/interrupted reports retain 90 days.
        if time.time() - directory.stat().st_mtime > days * 86400:
            shutil.rmtree(directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        cfg = json.loads(Path(args.config).read_text())
        snapshot(cfg)
        clean_observer()
    except Exception as exc:
        print('production snapshot failed: ' + type(exc).__name__)
        raise SystemExit(1)
