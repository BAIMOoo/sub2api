"""Run serial native captures and production probes; never repair the service."""
import copy
import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import threading
import tempfile
import time
from urllib.parse import urlsplit
import uuid

from .capture import Budget, Capture
from .evidence import (atomic_json, canonical, compare_replay, differences, digest, opaque,
                       retain, sanitize, ticket)
from .releases import install, release, github, prune_binaries
from .protocol import completion
from urllib.error import HTTPError


def native(binary, home, model, capture, timeout, scenario='basic'):
    with tempfile.TemporaryDirectory(prefix='alignment-work-') as work, capture.serve() as proxy:
        expected = 'OK' if scenario == 'basic' else 'ALIGNMENT_' + uuid.uuid4().hex
        Path(work, 'probe.txt').write_text(expected + '\n')
        env = {'PATH': str(Path(binary).parent) + ':/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(home),
               'LANG': 'C.UTF-8', 'HTTPS_PROXY': proxy, 'HTTP_PROXY': proxy, 'ALL_PROXY': proxy,
               'https_proxy': proxy, 'http_proxy': proxy, 'all_proxy': proxy, 'NO_PROXY': '', 'no_proxy': ''}
        if not capture.hello_only:
            env['CODEX_CA_CERTIFICATE'] = str(capture.cert)
            env['SSL_CERT_FILE'] = str(capture.cert)
        prompt = 'Reply with exactly OK. Do not use tools.'
        if scenario in {'tool', 'compact'}:
            prompt = 'Read the local file probe.txt using the shell tool, then reply with exactly its contents. Do not use the network.'
        command = [str(binary), 'exec', '--skip-git-repo-check', '--ephemeral', '--sandbox', 'read-only',
                   '--json', '-m', model, '-c', 'model_provider="openai"',
                   '-c', 'features.apps=false', '-c', 'features.plugins=false',
                   '-c', 'analytics.enabled=false']
        if scenario == 'compact':
            command += ['-c', 'model_auto_compact_token_limit=1']
        command.append(prompt)
        proc = subprocess.Popen(command, cwd=work, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        buffers = [bytearray(), bytearray()]
        def drain(stream, buffer):
            while True:
                piece = stream.read(4096)
                if not piece:
                    break
                buffer.extend(piece)
                if len(buffer) > 65536:
                    del buffer[:-65536]
        readers = [threading.Thread(target=drain, args=(stream, buffer), daemon=True)
                   for stream, buffer in zip((proc.stdout, proc.stderr), buffers)]
        for reader in readers:
            reader.start()
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if capture.hello_only and capture.hellos:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('native_deadline')
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            for reader in readers:
                reader.join(timeout=2)
            proc.stdout.close()
            proc.stderr.close()
            # Bounded diagnostics stay in memory, never in reports or logs.
            # Callers persist only explicit error categories.
            capture.cli_output = bytes(buffers[0])
            capture.cli_stderr = bytes(buffers[1])
            capture.answer_verified = native_answer_matches(capture.cli_output, expected)
        return proc.returncode


def native_answer_matches(output, expected):
    answer = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        item = event.get('item', {})
        if event.get('type') == 'item.completed' and item.get('type') == 'agent_message':
            answer = item.get('text', '').strip()
    return answer == expected


def gateway(cfg, run, sample, expires, path, body, pairs=()):
    target = urlsplit(cfg['gateway_url'])
    if target.scheme != 'http' or target.hostname != '127.0.0.1' or target.path not in ('', '/'):
        raise ValueError('loopback_gateway_required')
    headers = {k: v for k, v in pairs if k.lower() not in {
        'authorization', 'cookie', 'chatgpt-account-id', 'proxy-authorization', 'host',
        'content-length', 'content-type', 'content-encoding', 'connection', 'transfer-encoding', 'x-sub2api-alignment'}}
    headers.update({'Authorization': 'Bearer ' + cfg['gateway_key'], 'Content-Type': 'application/json',
                    'X-Sub2API-Alignment': ticket(cfg['secret'], run, sample, expires)})
    remaining = expires - time.time()
    if remaining <= 0:
        raise TimeoutError('run_deadline')
    connection = http.client.HTTPConnection('127.0.0.1', target.port, timeout=min(180, remaining))
    try:
        connection.request('POST', path, body=canonical(body), headers=headers)
        response = connection.getresponse()
        raw = response.read((8 << 20) + 1)
        if len(raw) > 8 << 20:
            raise ValueError('gateway_response_too_large')
        return completion(response.status, response.getheader('Content-Type', ''), raw, path,
                          response.getheader('Content-Encoding', ''))
    finally:
        connection.close()


def gateway_records(cfg, run, sample):
    records = []
    for path in sorted((Path(cfg['observer_output']) / run).glob(sample + '-*.json')):
        record = json.loads(path.read_text())
        if record['sample'] == sample and record['run'] == run:
            records.append(record)
    return records


def mode_of(body):
    if not isinstance(body, dict):
        return 'unknown'
    if any(isinstance(item, dict) and item.get('type') == 'additional_tools' for item in body.get('input', [])):
        return 'lite'
    return 'standard' if isinstance(body.get('tools'), list) else 'unknown'


def tool_continuation(body):
    return any(isinstance(item, dict) and item.get('type') in {
        'function_call_output', 'custom_tool_call_output', 'tool_search_output'} for item in body.get('input', []))


def compaction_observed(requests):
    triggered = any(any(isinstance(i, dict) and i.get('type') == 'compaction_trigger'
                        for i in r['body'].get('input', [])) and
                    r.get('response', {}).get('compaction_output') for r in requests)
    resumed = any(any(isinstance(i, dict) and i.get('type') in {'compaction', 'compaction_summary'}
                      for i in r['body'].get('input', [])) for r in requests)
    return bool(triggered and resumed)


def repository_state(repo):
    if repo != 'BAIMOoo/sub2api':
        raise ValueError('unexpected_repository')
    head = github('repos/' + repo + '/commits/main')['sha']
    try:
        runs = github('repos/' + repo + '/actions/workflows/codex-alignment.yml/runs?branch=main&per_page=20')['workflow_runs']
    except HTTPError as exc:
        if exc.code != 404:
            raise
        runs = []
    matching = [r for r in runs if r['head_sha'] == head and r['event'] in {'push', 'workflow_dispatch'}]
    latest = max(matching, key=lambda r: r['id']) if matching else None
    return {'head': head, 'status': latest['status'] if latest else 'missing',
            'conclusion': latest['conclusion'] if latest else None, 'run_id': latest['id'] if latest else None}


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg['state_dir'])

    def run(self, reason, generation):
        started = time.time()
        run = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
        directory = self.root / 'runs' / run
        directory.mkdir(parents=True, mode=0o700)
        report = {'schema': 1, 'run': run, 'started_at': started, 'trigger': reason,
                  'generation': generation, 'scenarios': [], 'findings': [],
                  'boundaries': ['native_http_uses_custom_ca', 'tls_is_separate_measurement',
                                 'native_same_transaction_direct_http_unverified', 'fresh_gateway_connections',
                                 'opaque_turn_metadata_contents_unverified', 'window_uuid_suffix_relationship_unverified',
                                 'oauth_refresh_tunnels_are_encrypted_and_not_counted_as_model_http'],
                  'baseline': {'version': self.cfg['approved_version'], 'approval_scope': 'existing_body_fixtures_only'}}
        report['native_config'] = {'apps': False, 'plugins': False, 'analytics': False,
                                   'sandbox': 'read-only', 'ephemeral': True,
                                   'http_selection': 'local_websocket_upgrade_refusal'}
        expires = int(started + min(3600, self.cfg.get('watchdog_seconds', 3600)))
        native_budget = Budget(self.cfg.get('native_run_max_requests', 64))
        try:
            retain(self.root, max_bytes=self.cfg.get('max_evidence_bytes', 512 << 20))
            snapshot = json.loads(Path(self.cfg['snapshot_file']).read_text())
            if time.time() - snapshot['at'] > 180:
                raise ValueError('production_snapshot_stale')
            report['production'] = snapshot
            if not (snapshot.get('running') and snapshot.get('oauth') and snapshot.get('account_available')):
                raise ValueError('production_account_unavailable')
            if snapshot.get('account_id') != self.cfg['account_id'] or snapshot.get('proxy_id') != self.cfg['proxy_id']:
                raise ValueError('production_probe_binding_changed')
            if snapshot.get('oauth_account_sha256') != self.cfg['oauth_account_sha256']:
                raise ValueError('production_oauth_identity_changed')
            if not snapshot.get('health_ok'):
                raise ValueError('production_unhealthy')
            try:
                report['repository'] = repository_state(self.cfg['repository'])
            except Exception:
                report['repository'] = {'status': 'unavailable', 'conclusion': None}
            if report['repository']['conclusion'] != 'success':
                report['findings'].append('repository_regression_not_verified')
            current = release()
            report['latest_stable'] = current['tag_name']
            last_release = self.root / 'last-release.json'
            previous = json.loads(last_release.read_text()) if last_release.exists() else {}
            version_changed = previous.get('version') != current['tag_name']
            report['official_release_changed'] = version_changed
            versions = [release(self.cfg['approved_version'])]
            if current['tag_name'] != versions[0]['tag_name']:
                versions.append(current)
            home = Path(self.cfg['native_home'])
            if not (home / 'auth.json').exists():
                raise ValueError('native_login_required')
            expanded = reason in {'acceptance', 'version_changed'} or version_changed
            rotation = 'tool' if datetime.datetime.now(datetime.timezone.utc).toordinal() % 2 else 'compact'
            report['coverage_gaps'] = self.cfg.get('unavailable_modes', {})
            report['findings'].extend('coverage:' + mode + ':' + why for mode, why in report['coverage_gaps'].items())
            for info in versions:
                binary, provenance = install(self.root / 'binaries', info)
                _, host_provenance = install(self.root / 'binaries', info, component='codex-code-mode-host')
                version = provenance['version']
                hello = Capture(self.cfg['proxy_url'], self.cfg['secret'], directory / version / 'tls',
                                self.root / 'ca', hello_only=True)
                native(binary, home, next(iter(self.cfg['models'].values())), hello, 30)
                tls = hello.hellos[0] if hello.hellos else None
                if tls is None:
                    report['findings'].append(version + ':native_tls_unverified')
                for expected_mode, model in self.cfg['models'].items():
                    scenarios = ['basic', 'tool', 'compact'] if expanded else ['basic', rotation]
                    for scenario in scenarios:
                        if time.time() >= expires - 30:
                            raise TimeoutError('run_deadline')
                        name = 'v' + version.replace('.', '_') + '-' + expected_mode + '-' + scenario
                        cap = Capture(self.cfg['proxy_url'], self.cfg['secret'], directory / name,
                                      self.root / 'ca', max_requests=self.cfg.get('native_max_requests', 16),
                                      budget=native_budget)
                        phase = time.monotonic()
                        code = native(binary, home, model, cap, min(240, expires - time.time()), scenario)
                        row = {'name': name, 'binary': provenance, 'model': model, 'expected_mode': expected_mode,
                               'cli_exit': code, 'answer_verified': cap.answer_verified, 'code_mode_host': host_provenance, 'native_attempts': cap.count, 'capture_errors': cap.errors,
                               'opaque_oauth_refresh_tunnels': cap.auth_tunnels,
                               'connection_closures': cap.connection_closures,
                               'local_websocket_refusals': cap.websocket_refusals,
                               'replays': [], 'status': 'unverified'}
                        requests = [r for r in cap.requests if r['method'] == 'POST']
                        if scenario == 'tool' and not any(tool_continuation(r['body']) for r in requests):
                            row['reason'] = 'native_tool_continuation_not_observed'
                        elif scenario == 'compact' and not compaction_observed(requests):
                            row['reason'] = 'native_compact_not_observed'
                        elif not cap.answer_verified:
                            row['reason'] = 'native_expected_answer_not_observed'
                        elif code != 0 or not requests or cap.errors or any(r['status'] is None or r['status'] // 100 != 2 or not r.get('response', {}).get('complete') for r in requests):
                            row['reason'] = 'native_capture_or_provider_incomplete'
                        else:
                            for i, request in enumerate(requests):
                                sample = name + '-' + str(i)
                                pairs = request['pairs']
                                account = next((v for k, v in pairs if k.lower() == 'chatgpt-account-id'), '')
                                if not account or hashlib.sha256(account.encode()).hexdigest() != self.cfg['oauth_account_sha256']:
                                    raise ValueError('native_oauth_account_mismatch')
                                mode = mode_of(request['body'])
                                path = '/v1/responses/compact' if request['path'].endswith('/compact') else '/v1/responses'
                                result = gateway(self.cfg, run, sample, expires, path, request['body'], pairs)
                                records = gateway_records(self.cfg, run, sample)
                                captured = [r['data'] for r in records if r['kind'] == 'http' and r['data']['path'] == request['path']]
                                handshake = [r['data'] for r in records if r['kind'] == 'tls']
                                comparison = compare_replay(request['evidence'], captured[-1] if captured else None)
                                comparison.update(sample=sample, result=result, observed_mode=mode)
                                comparison['tls_diff'] = differences(
                                    {k: v for k, v in tls.items() if k != 'evidence'},
                                    {k: v for k, v in handshake[-1].items() if k != 'evidence'}
                                ) if tls and handshake else ['missing_tls_evidence']
                                if comparison['tls_diff']:
                                    comparison['status'] = 'drift' if tls and handshake else 'unverified'
                                if not result['complete'] or any(r['kind'] == 'incomplete' for r in records):
                                    comparison['provider_verified'] = False
                                    if comparison['status'] != 'drift':
                                        comparison['status'] = 'unverified'
                                if path == '/v1/responses' and mode != expected_mode:
                                    comparison['status'] = 'unverified'
                                    comparison['reason'] = 'requested_mode_not_observed'
                                # Preserve raw gateway evidence alongside this immutable report.
                                for j, record in enumerate(records):
                                    atomic_json(directory / name / ('gateway-%d-%d.json' % (i, j)), record)
                                row['replays'].append(comparison)
                            states = [r['status'] for r in row['replays']]
                            row['status'] = 'drift' if 'drift' in states else 'unverified' if 'unverified' in states else 'pass'
                        row['seconds'] = round(time.monotonic() - phase, 3)
                        report['scenarios'].append(row)
                        if row['status'] != 'pass':
                            report['findings'].append(name + ':' + row.get('reason', row['status']))
                        atomic_json(directory / 'progress.json', report)
            self.compatibility(report, run, expires, directory)
            catalog_path = home / 'models_cache.json'
            if catalog_path.exists():
                catalog = json.loads(catalog_path.read_text())
                models = catalog.get('models', [])
                standard = [m for m in models if isinstance(m, dict) and m.get('use_responses_lite') is False
                            and m.get('visibility') == 'list']
                report['model_catalog'] = {'sha256': digest(catalog), 'listed_standard_models': len(standard)}
                if standard and 'standard' in report['coverage_gaps']:
                    report['findings'].append('coverage:standard_model_now_available_review_selection')
            prune_binaries(self.root / 'binaries', {self.cfg['approved_version'], current['tag_name'].removeprefix('rust-v')})
            atomic_json(last_release, {'version': current['tag_name'], 'run': run})
        except Exception as exc:
            safe = str(exc) if type(exc) in (ValueError, TimeoutError) and str(exc).replace('_', '').isalnum() else type(exc).__name__
            report['findings'].append('incomplete:' + safe)
        report['status'] = ('drift' if any(r['status'] == 'drift' for r in report['scenarios']) else
                            'unverified' if report['findings'] else 'pass')
        previous_report = json.loads((self.root / 'latest.json').read_text()) if (self.root / 'latest.json').exists() else {}
        report['last_verified_at'] = report.get('started_at') if report['status'] == 'pass' else previous_report.get('last_verified_at')
        report['finished_at'] = time.time()
        report['native_upstream_attempts'] = native_budget.count
        report['elapsed_seconds'] = round(report['finished_at'] - started, 3)
        report['runner_peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report['child_peak_rss_kib'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        atomic_json(directory / 'report.json', report)
        atomic_json(self.root / 'latest.json', report)
        return report

    def compatibility(self, report, run, expires, directory):
        model = self.cfg.get('compatibility_model', next(iter(self.cfg['models'].values())))
        session = str(uuid.uuid4())
        cases = [('responses', '/v1/responses', {'model': model, 'input': 'Reply with exactly OK.', 'stream': True}),
                 ('chat', '/v1/chat/completions', {'model': model, 'messages': [{'role': 'user', 'content': 'Reply with exactly OK.'}], 'stream': True}),
                 ('messages', '/v1/messages', {'model': model, 'max_tokens': 32, 'messages': [{'role': 'user', 'content': 'Reply with exactly OK.'}], 'stream': True})]
        for name, path, body in cases:
            sample = 'compat-' + name
            result = gateway(self.cfg, run, sample, expires, path, body,
                             [('session-id', session), ('anthropic-version', '2023-06-01')])
            records = gateway_records(self.cfg, run, sample)
            http = [r['data'] for r in records if r['kind'] == 'http' and r['data']['method'] == 'POST']
            violations = []
            if not result['complete'] or not http or any(r['kind'] == 'incomplete' for r in records):
                violations.append('missing_success_or_evidence')
            if http:
                b = http[-1]['body']
                if b.get('store') is not False or b.get('stream') is not True:
                    violations.append('codex_store_stream_contract')
                if b.get('model') != opaque(self.cfg['secret'], model):
                    violations.append('model_mapping_changed')
                if not isinstance(b.get('input'), list) or not b['input']:
                    violations.append('missing_converted_input')
            row = {'name': sample, 'status': 'unverified' if violations else 'pass', 'violations': violations, 'result': result}
            report['scenarios'].append(row)
            if violations:
                report['findings'].append(sample + ':unverified')
            for j, record in enumerate(records):
                atomic_json(directory / sample / ('gateway-%d.json' % j), record)
