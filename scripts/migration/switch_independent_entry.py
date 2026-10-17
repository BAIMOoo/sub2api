#!/usr/bin/env python3
"""One new DNS handover; keep the existing AWS connection path untouched."""
import argparse
import json
import pathlib
import runpy
import subprocess
import time

HERE = pathlib.Path(__file__).parent
ROOT = pathlib.Path.home() / '.codex/backups/sub2api-aws-independent-entry-20261017'


def proof():
    canary = json.loads((ROOT / 'canary-before-dns.json').read_text())
    web = json.loads((ROOT / 'web-before.json').read_text())
    checks = runpy.run_path(str(HERE / 'independent_entry_canary.py'))
    fresh = checks['Reads']().preflight()
    assert canary['ok'] is True and canary['target_postflight'] == fresh
    assert canary['stream']['transport']['peer_ip'] == '43.165.182.52'
    assert web['ok'] is True and web['pinned_ip'] == '43.165.182.52'
    assert web['proxy'] is False
    result = subprocess.run(['curl', '--noproxy', '*', '--resolve',
        'api.b4im.com:443:43.165.182.52', '--connect-timeout', '4', '--max-time', '8',
        '-fsS', '-o', '/dev/null', '-w', '%{http_code} %{ssl_verify_result}',
        'https://api.b4im.com/health'], capture_output=True, timeout=10)
    assert result.returncode == 0 and result.stdout == b'200 0'
    # The unchanged runtime identity is re-proved above. Keep the actual model
    # check time in the proof; the DNS transaction also enforces its age.
    return {'v': 1, 'observed_at': min(canary['finished_at'], time.time()),
        'target_address': '43.165.182.52', 'pg_authority': fresh['pg_authority'],
        'redis_authority': fresh['redis_authority'], 'application_ready': fresh['app_ready'],
        'runtime_endpoints': {'postgres': 'target', 'redis': 'target'},
        'persisted_endpoints': {key: fresh['persistent_endpoints'][key]
                                for key in ('postgres', 'redis')},
        'domestic_entry': {'address': '43.165.182.52', 'domain': 'api.b4im.com',
            'https_status': 200, 'tls_verify_result': 0, 'no_proxy': True, 'pinned_ip': True},
        'web_bootstrap_passed': True, 'stream_canary_passed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    if not parser.parse_args().apply:
        print(json.dumps({'offline': True, 'dns_changed': False,
                          'target': '43.165.182.52', 'existing_connections_preserved': True}))
        return
    dns = runpy.run_path(str(HERE / 'independent_entry_dns.py'))
    folder = ROOT / 'dns'
    folder.mkdir(mode=0o700)  # Never repeat a reserved write lane.
    report = dns['transaction'](dns['Client'](dns['load_private_token']()), folder, proof)
    print(json.dumps({key: report.get(key) for key in
        ('ok', 'outcome', 'patch_count', 'target', 'error')}))
    if not report['ok']:
        raise SystemExit('dns-handover-not-confirmed-inspect-evidence-no-repeat')


if __name__ == '__main__':
    main()
