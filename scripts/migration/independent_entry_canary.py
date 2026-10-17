#!/usr/bin/env python3
"""Bounded mainland-to-Tencent stream check, with exact Tencent billing reads.

Each named lane has its own durable no-repeat journal. No historical migration
operation is repeated. Secrets travel over SSH stdin, never process arguments.
"""
import argparse
import hashlib
import json
import pathlib
import runpy
import subprocess
import time
import uuid

HERE = pathlib.Path(__file__).parent
ROOT = pathlib.Path.home() / '.codex/backups/sub2api-aws-independent-entry-20261017'
TARGET_IP = '43.165.182.52'
MODEL_SHA = 'a8458fb80ca426e9ad203584578be6df3d1d16a36c1bd3b7a71e5bdd99d1e20f'
POST_SHA = '4ad1856dc17f1de1818a80c3100f950ad9e02c309e7ba737464838db8c541725'


def need(ok, code):
    if not ok:
        raise RuntimeError(code)


def reviewed(name, expected):
    raw = (HERE / name).read_bytes()
    need(hashlib.sha256(raw).hexdigest() == expected, 'reviewed-library-changed')
    return raw


class Reads:
    def __init__(self):
        self.program = reviewed('postcutover_canary.py', POST_SHA).decode()
        old = runpy.run_path(str(HERE / 'prepare_channel.py'))['TARGET']
        self.target = old[:-1] + ['ubuntu@' + TARGET_IP]

    def read(self, action, query=None):
        need(action in ('preflight', 'sql'), 'read-only-operation-required')
        # The payload stays in the encrypted stdin stream, including any key
        # lookup SQL. Do not include it in commands, evidence or exception text.
        program = ("__name__='independent_entry_read'\n" + self.program +
                   "\ntry:\n rpc,cfg=load_target()\n backend=TargetBackend(rpc,cfg)\n" +
                   (" value=backend.preflight()\n" if action == 'preflight' else
                    ' value=backend.sql(' + repr(query) + ')\n') +
                   " print(json.dumps({'ok':True,'value':value}))\n" +
                   "except Exception as error:\n" +
                   " print(json.dumps({'ok':False,'busy':getattr(error,'code',None)=='OPERATION_BUSY'}))\n")
        for attempt in range(4):
            try:
                result = subprocess.run(self.target + ['sudo -n python3 -I -'],
                    input=program.encode(), capture_output=True, timeout=25)
                need(result.returncode == 0, 'target-read-unavailable')
                value = json.loads(result.stdout)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                raise RuntimeError('target-read-unavailable-no-secret-output') from None
            if value.get('ok') is True:
                return value['value']
            need(value.get('busy') is True and attempt < 3, 'target-read-refused')
            time.sleep(0.15)

    def preflight(self):
        return self.read('preflight')

    def sql(self, query):
        return self.read('sql', query)


def check(lane):
    need(lane in ('before-dns', 'after-aws-stop'), 'unknown-canary-lane')
    reviewed('model_canary.py', MODEL_SHA)
    # Functions retain their defining globals; use a module to bind only this
    # newly reviewed destination while retaining TLS and certificate checks.
    import types
    model = types.ModuleType('independent_entry_model')
    exec(compile(reviewed('model_canary.py', MODEL_SHA), 'reviewed-model-canary', 'exec'), model.__dict__)
    model.TARGET_IP = TARGET_IP
    path = ROOT / ('canary-' + lane + '.json')
    model.no_symlinks(ROOT)
    report = {'v': 1, 'lane': lane, 'ok': False, 'request_attempted': False,
              'target_address': TARGET_IP, 'proxy': False,
              'started_at': time.time(), 'stream': {}, 'automatic_retry': False}
    model.private_write(path, report)  # Existing intent always refuses a repeat.
    try:
        reads = Reads()
        before = reads.preflight()
        token = model.load_token()
        model.sql = reads.sql
        key = model.key_preflight(token)
        report['target_preflight'] = before
        report['request_id'] = 'migration-canary-independent-' + uuid.uuid4().hex
        supplied = str(uuid.uuid4())
        report.update(request_attempted=True, post_intent_durable=True)
        model.private_write(path, report, replace=True)
        model.model_request('target', token, report['request_id'], supplied, report['stream'])
        effective = report['stream']['effective_client_request_id']
        key['effective_client_request_id'] = effective
        deadline = time.monotonic() + 30
        while True:
            billing = model.exact_billing('client:' + effective, key['api_key_id'])
            need(billing.get('usage_count', 0) <= 1 and billing.get('dedup_count', 0) <= 1,
                 'duplicate-exact-request-billing')
            if billing.get('usage_count') == billing.get('dedup_count') == 1:
                report['billing'] = model.validate_billing(billing, report['stream']['usage'], key)
                break
            need(time.monotonic() < deadline, 'exact-billing-not-settled')
            time.sleep(0.25)
        report['target_postflight'] = reads.preflight()
        need(report['target_postflight'] == before, 'runtime-changed-during-canary')
        report['ok'] = True
    except Exception:
        report['error'] = 'independent-canary-failed-inspect-private-evidence-no-repeat'
    report['finished_at'] = time.time()
    model.private_write(path, report, replace=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', choices=('before-dns', 'after-aws-stop'))
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({'offline': True, 'target_address': TARGET_IP,
                          'billable_request_sent': False}))
        return
    result = check(args.apply)
    print(json.dumps({key: result.get(key) for key in
        ('ok', 'lane', 'request_attempted', 'target_address', 'stream', 'billing', 'error')}))
    need(result['ok'], 'canary-not-passed-no-repeat')


if __name__ == '__main__':
    main()
