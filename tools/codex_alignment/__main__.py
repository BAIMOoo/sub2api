import argparse
import json
import os
from pathlib import Path
import sys
import time

from .evidence import atomic_json, digest
from .notifications import Outbox, email_send, read_env, wechat_send
from .runner import Runner, repository_state
from .schedule import Schedule, lock


def outbox(cfg):
    senders = {}
    mail_path = Path(cfg['smtp_env'])
    if mail_path.exists():
        mail = read_env(mail_path)
        senders['email'] = lambda text, event: email_send(mail, text, event, cfg['proxy_url'])
    wx_path = Path(cfg['wechat_config'])
    if wx_path.exists():
        if wx_path.stat().st_mode & 0o077:
            raise ValueError('wechat_file_permissions')
        wx = json.loads(wx_path.read_text())
        senders['wechat'] = lambda text, event: wechat_send(wx, text, event)
    return Outbox(Path(cfg['state_dir']) / 'outbox', senders)


def run_cli(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['tick', 'acceptance', 'notify-test', 'status'])
    args = parser.parse_args(argv)
    os.umask(0o077)
    path = Path(args.config)
    if path.stat().st_mode & 0o077:
        raise ValueError('config_file_permissions')
    cfg = json.loads(path.read_text())
    from .egress import configure
    configure(cfg['proxy_url'])
    root = Path(cfg['state_dir'])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    guard = lock(root / 'run.lock')
    if guard is None:
        print('alignment: existing run owns lock; trigger will coalesce')
        return 0
    try:
        if args.command == 'status':
            latest = root / 'latest.json'
            if latest.exists():
                report = json.loads(latest.read_text())
                print(json.dumps({k: report[k] for k in ('run', 'status', 'finished_at', 'findings')}))
            else:
                print('{"status":"not_run"}')
            return 0
        box = outbox(cfg)
        if args.command == 'notify-test':
            box.observe('pass', ['delivery_test'], 'Sub2API Codex 请求对齐检测：通知通道验收测试。', force=True)
            pending = box.flush()
            print(json.dumps({'notification_test': 'pending' if pending else 'delivered'}))
            return int(bool(pending))
        box.flush()
        now = time.time()
        snapshot = json.loads(Path(cfg['snapshot_file']).read_text())
        cache = root / 'repository.json'
        if not cache.exists() or now - cache.stat().st_mtime > 300:
            try:
                atomic_json(cache, repository_state(cfg['repository']))
            except Exception:
                # Daily production checks still run during a GitHub outage.
                pass
        repo = json.loads(cache.read_text()) if cache.exists() else {'head': None, 'conclusion': None}
        generation = digest({'production': snapshot['generation'], 'head': repo['head'],
                             'ci': repo['conclusion'], 'monitor_config': digest(cfg)})
        schedule = Schedule(root / 'schedule.json')
        schedule.observe(generation, now)
        if schedule.state.get('running'):
            interrupted = schedule.state.pop('running')
            box.observe('unverified', ['previous_run_interrupted'],
                        'Sub2API 请求对齐检测：上一轮中断，结果未验证。请检查监控服务和运行资源。')
            schedule.save()
        if args.command == 'tick' and not schedule.due(now):
            return 0
        reason = 'acceptance' if args.command == 'acceptance' else 'daily_or_change'
        schedule.started(generation, now)
        report = Runner(cfg).run(reason, generation)
        schedule.finished(generation)
        findings = report['findings']
        text = ('Sub2API Codex 请求对齐检测：' + report['status'] + '\n运行：' + report['run'] +
                '\n耗时：' + str(report['elapsed_seconds']) + ' 秒\n' +
                ('问题：' + '\n'.join(findings[:12]) if findings else '已验证项目恢复正常。') +
                '\n原生直连同一请求的完整报文仍未验证；详情见服务器脱敏报告。')
        # Include stable details, excluding dynamic hashes/IDs, so a new drift
        # location under the same scenario triggers a meaningful notification.
        signature = [{'name': r['name'], 'status': r['status'],
                      'diffs': [{k: v for k, v in item.items() if k in {
                          'body_diff', 'header_contract_diff', 'tls_diff', 'identity_failures', 'reason'}}
                                for item in r.get('replays', [])]} for r in report['scenarios']]
        box.observe(report['status'], {'findings': findings, 'scenarios': signature}, text)
        pending = box.flush()
        print(json.dumps({'run': report['run'], 'status': report['status'], 'notification_pending': bool(pending)}))
        return 0
    except Exception as exc:
        # Persist inability even when snapshot/release lookup fails before a run.
        if 'box' in locals():
            box.observe('unverified', ['monitor_operation_failed_' + type(exc).__name__],
                        'Sub2API 请求对齐检测暂时无法运行，请检查服务日志；此前结果不代表当前状态。')
            box.flush()
        raise
    finally:
        guard.close()


if __name__ == '__main__':
    try:
        sys.exit(run_cli())
    except Exception as exc:
        # stdout/journal must never receive config, HTTP bodies or error URLs.
        print('alignment: operation failed (' + type(exc).__name__ + ')', file=sys.stderr)
        sys.exit(1)
