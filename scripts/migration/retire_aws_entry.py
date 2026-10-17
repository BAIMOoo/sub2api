#!/usr/bin/env python3
"""One-shot AWS migration-entry retirement. Default is offline; no remote access.

source_script('sample') records one source-owned observation. Call it at least
three times, >=30 seconds apart. source_script('apply', fresh_proof) performs
one retirement after a final independent snapshot. An intent forbids replay.
"""
import fcntl
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import time

ROOT = pathlib.Path('/var/lib/sub2api-migration-20261016')
SCOPE = ROOT / 'retire-aws-entry-20261017'
TARGET = '43.165.182.52'
CHANNEL = 'sub2api-migration-channel-20261016.service'
GUARD = 'sub2api-migration-recovery-20261016.service'
RUNNER = 'sub2api-migration-runner-20261016.service'
SHARED = {'caddy.service': 1284574, 'codex-remote-relay.service': 1307343}
CONTAINERS = {'sub2api': 'cba57e76f48d0e4cfb0280bcc963205dc910a0d0b4bd752e555bb2fa44b9f55e',
 'sub2api-postgres': 'c99ec20ad4468b29698083cdc6827c853141717f0cf0a7a98bfed32e788414ee',
 'sub2api-redis': 'e2f41a97ee0a36a9a5bd3e36b8f65874411419b3dae3867097323b3e730c6835'}
FORWARDS = [('-L','127.0.0.1:18081:127.0.0.1:8082'),('-R','127.0.0.1:15432:172.18.0.3:5432'),
 ('-R','127.0.0.1:16379:172.18.0.2:6379'),('-L','172.18.0.1:26379:127.0.0.1:26379'),
 ('-L','127.0.0.1:18092:127.0.0.1:18092'),('-L','127.0.0.1:25432:127.0.0.1:25432')]
COMMENT = 'sub2api-live-migration-20261016'
EXT = 'sub2api-channel-extension-20261016'
RULE = ['-p','tcp','-d','127.0.0.1/32','--dport','8081','-m','owner','--uid-owner','997',
 '-m','conntrack','--ctstate','NEW','-m','comment','--comment',COMMENT,'-j','REDIRECT','--to-ports','18081']


class Refusal(RuntimeError):
    pass


def need(ok, code):
    if not ok:
        raise Refusal(code)


def sockets(raw):
    """Both bridge halves count conservatively; TIME-WAIT is not a live socket."""
    found = []
    for row in raw.splitlines():
        fields = row.split()
        need(len(fields) >= 5, 'invalid-socket-row')
        state, local, peer = fields[0], fields[3], fields[4]
        def port(value):
            last = value.rsplit(':', 1)[-1]
            return int(last) if last.isdigit() else None
        if state not in ('LISTEN', 'TIME-WAIT', 'CLOSED') and (
                port(local) in (18081,18092,25432,26379,15432,16379) or
                port(peer) in (8081,18081,18092,25432,26379,15432,16379)):
            found.append({'state':state,'local':local,'peer':peer})
    return found


def normalized(tokens):
    # iptables serializes source/destination/interface before protocol; compare
    # exact flag/value multisets so canonical ordering changes remain harmless.
    need(len(tokens) % 2 == 0, 'invalid-firewall-token-pairs')
    pairs = list(zip(tokens[::2], tokens[1::2]))
    return sorted(pair for pair in pairs if pair != ('-m','tcp'))


def firewall_plan(raw, kind):
    lines = raw.splitlines()
    owned = []
    for line in lines:
        tokens = shlex.split(line)
        marker = COMMENT if kind == 'nat' else EXT
        if not any(marker in token for token in tokens):
            continue
        need(tokens.count('--comment') == 1, 'ambiguous-firewall-comment')
        pos = tokens.index('--comment')
        need(pos+1 < len(tokens), 'invalid-firewall-comment')
        comment = tokens[pos+1]
        if kind == 'nat':
            need(normalized(tokens) == normalized(['-A','OUTPUT']+RULE), 'bridge-rule-drifted')
        else:
            need(comment in (EXT+':redis-allow', EXT+':redis-deny'), 'redis-rule-comment-drifted')
            if comment.endswith(':redis-allow'):
                need(tokens[:2] == ['-A','INPUT'] and tokens.count('-i') == 1, 'invalid-redis-interface')
                interface = tokens[tokens.index('-i')+1]
                need(re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', interface), 'invalid-redis-interface')
                expected = ['-A','INPUT','-i',interface,'-s','172.18.0.2/32','-d','172.18.0.1/32',
                            '-p','tcp','--dport','26379','-m','comment','--comment',comment,'-j','ACCEPT']
            else:
                expected = ['-A','INPUT','-d','172.18.0.1/32','-p','tcp','--dport','26379',
                            '-m','comment','--comment',comment,'-j','REJECT','--reject-with','tcp-reset']
            need(normalized(tokens) == normalized(expected), 'redis-rule-drifted')
        owned.append((line,tokens))
    need(len(owned) == (1 if kind == 'nat' else 2), 'owned-firewall-rule-count')
    need(len({tokens[tokens.index('--comment')+1] for _,tokens in owned}) == len(owned), 'duplicate-owned-comment')
    return owned


def validate_snapshot(value):
    state = value['state']
    need(state.get('phase') == 'COMPLETE_WITH_WAIT_BREACH' and state.get('pg') == state.get('redis') == 'TENCENT'
         and state.get('window') is None, 'source-authority-not-complete')
    need(not value['sockets'], 'active-source-forward-connections')
    for name, expected in CONTAINERS.items():
        row = value['containers'][name]
        need(row['id'] == expected and row['running'] is False and row['restart'] == 'no'
             and row['pid'] == 0 and row['oom'] is False and row['exit'] == (127 if name == 'sub2api-redis' else 0),
             'source-container-not-retired')
    for name,pid in SHARED.items():
        row = value['units'][name]
        need(row['ActiveState'] == 'active' and row['SubState'] == 'running' and row['MainPID'] == str(pid)
             and not row['Job'], 'shared-service-changed')
    return value


def validate_proof(proof, now):
    need(type(proof) is dict and type(proof.get('observed_at')) in (int,float) and
         0 <= now-proof['observed_at'] <= 20 and proof.get('dns_address') == TARGET and
         proof.get('target_ready') is True and proof.get('pg_authority') == proof.get('redis_authority') == 'TENCENT'
         and proof.get('runtime_endpoints') == proof.get('persisted_endpoints') == {'postgres':'target','redis':'target'},
         'fresh-dns-target-proof-required')


def stable(samples, current):
    need(len(samples) >= 3, 'three-source-samples-required')
    last = samples[-3:]
    for row in last:
        validate_snapshot(row)
        need(row['boot'] == current['boot'] and row['shared_identity'] == current['shared_identity']
             and row['channel_identity'] == current['channel_identity'], 'sample-identity-changed')
    need(all(b['monotonic']-a['monotonic'] >= 30 for a,b in zip(last,last[1:]))
         and last[-1]['monotonic']-last[0]['monotonic'] >= 60
         and 0 <= current['monotonic']-last[-1]['monotonic'] <= 10, 'stable-observation-window-required')


def command(argv):
    r = subprocess.run(argv, capture_output=True, timeout=12, env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C'})
    need(r.returncode == 0, 'command-failed-no-retry')
    return r.stdout.decode('utf-8', errors='strict')


def unit(name):
    keys = 'ActiveState SubState MainPID Job FragmentPath DropInPaths UnitFileState ExecMainStartTimestampMonotonic ControlGroup'.split()
    raw = command(['/usr/bin/systemctl','show',name,'--property='+','.join(keys)])
    value = dict(x.split('=',1) for x in raw.splitlines())
    need(set(value) == set(keys) and not value['DropInPaths'], 'unit-policy-drifted')
    return value


def private(path):
    need(not path.is_symlink(), 'symlink-refused')
    info = path.stat()
    need(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022, 'untrusted-file')
    return path.read_text()


def write_new(name, value):
    raw = json.dumps(value, sort_keys=True).encode()+b'\n'
    fd = os.open(SCOPE/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd,'wb') as out:
        out.write(raw); out.flush(); os.fsync(out.fileno())
    fd = os.open(SCOPE, os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def snapshot():
    units = {name:unit(name) for name in (*SHARED,GUARD,RUNNER,CHANNEL)}
    channel_text = private(pathlib.Path('/etc/systemd/system')/CHANNEL)
    starts = [x.removeprefix('ExecStart=') for x in channel_text.splitlines() if x.startswith('ExecStart=')]
    need(len(starts) == 1, 'channel-command-ambiguous')
    args = shlex.split(starts[0])
    need(args[-1] == 'ubuntu@'+TARGET and [(a,args[i+1]) for i,a in enumerate(args) if a in ('-L','-R')] == FORWARDS,
         'channel-forward-drifted')
    need(units[CHANNEL]['FragmentPath'] == '/etc/systemd/system/'+CHANNEL, 'channel-unit-location-changed')
    containers = {}
    for name in CONTAINERS:
        row = json.loads(command(['/usr/bin/docker','inspect',name]))[0]
        s = row['State']; containers[name] = {'id':row['Id'],'running':s['Running'],'pid':s['Pid'],
            'oom':s['OOMKilled'],'exit':s['ExitCode'],'restart':row['HostConfig']['RestartPolicy']['Name']}
    state = json.loads(private(ROOT/'runner/state.json'))
    result = {'observed_at':time.time(),'monotonic':time.monotonic(),
        'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'state':{k:state.get(k) for k in ('phase','pg','redis','window')},'containers':containers,'units':units,
        'sockets':sockets(command(['/usr/bin/ss','-Htanp'])),
        'shared_identity':{name:[units[name]['MainPID'],units[name]['ExecMainStartTimestampMonotonic']] for name in SHARED},
        'channel_identity':[units[CHANNEL]['MainPID'],units[CHANNEL]['ExecMainStartTimestampMonotonic']]}
    return result


def stopped(names):
    for name in names:
        row = unit(name)
        need(row['MainPID'] == '0' and row['ActiveState'] in ('inactive','failed') and not row['Job']
             and row['UnitFileState'] in ('disabled','masked'), 'retired-unit-not-stopped')
        group = row['ControlGroup'] or '/system.slice/'+name
        path = pathlib.Path('/sys/fs/cgroup'+group)/'cgroup.events'
        need(not path.exists() or dict(x.split() for x in path.read_text().splitlines()).get('populated') == '0',
             'retired-cgroup-not-empty')


def remote(action, proof=None):
    need(os.geteuid() == 0 and action in ('sample','inspect','apply'), 'root-fixed-action-required')
    need(not any(p.is_symlink() for p in (ROOT,SCOPE)), 'symlink-scope-refused')
    if not SCOPE.exists(): SCOPE.mkdir(mode=0o700)
    need(SCOPE.stat().st_uid == 0 and stat.S_IMODE(SCOPE.stat().st_mode) == 0o700, 'private-scope-required')
    fd = os.open(SCOPE/'lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        current = snapshot()
        if action == 'inspect': return {'inspection':current,'intent_exists':(SCOPE/'intent.json').exists()}
        need(not (SCOPE/'intent.json').exists(), 'existing-intent-refuse-replay')
        samples = [json.loads(private(p)) for p in sorted(SCOPE.glob('sample-*.json'))]
        if action == 'sample':
            write_new('sample-%06d.json'%len(samples), current)
            validate_snapshot(current)
            return {'sample_count':len(samples)+1,'observed_at':current['observed_at'],'idle':True}
        validate_proof(proof,time.time()); validate_snapshot(current); stable(samples,current)
        firewall = {kind:command(['/usr/sbin/iptables','-w','2','-t',kind,'-S']) for kind in ('nat','filter')}
        plans = {kind:firewall_plan(raw,kind) for kind,raw in firewall.items()}
        unit_text = {name:private(pathlib.Path('/etc/systemd/system')/name) for name in (GUARD,RUNNER,CHANNEL)}
        write_new('before.json',{'snapshot':current,'firewall':firewall,'unit_text':unit_text,'proof':proof})
        write_new('intent.json',{'action':'retire-aws-migration-entry','created_at':time.time(),'target':TARGET})
        step = 0
        def phase(name):
            nonlocal step
            write_new('phase-%02d.json'%step,{'phase':name,'at':time.time()}); step += 1
        try:
            validate_proof(proof,time.time()); validate_snapshot(snapshot())
            phase('stop-source-control-intent')
            command(['/usr/bin/systemctl','disable','--now',GUARD,RUNNER]); stopped((GUARD,RUNNER))
            validate_snapshot(snapshot())
            phase('stop-channel-intent')
            command(['/usr/bin/systemctl','disable','--now',CHANNEL]); stopped((GUARD,RUNNER,CHANNEL))
            validate_snapshot(snapshot())
            for kind in ('nat','filter'):
                expected = firewall[kind]
                for line,tokens in plans[kind]:
                    need(command(['/usr/sbin/iptables','-w','2','-t',kind,'-S']) == expected, 'firewall-concurrent-change')
                    phase('delete-'+kind+'-rule-intent')
                    command(['/usr/sbin/iptables','-w','2','-t',kind,'-D']+tokens[1:])
                    expected = '\n'.join(x for x in expected.splitlines() if x != line)+'\n'
                    need(command(['/usr/sbin/iptables','-w','2','-t',kind,'-S']) == expected, 'unrelated-firewall-change')
            final = validate_snapshot(snapshot()); stopped((GUARD,RUNNER,CHANNEL))
            write_new('result.json',{'ok':True,'observed_at':time.time(),'snapshot':final})
            return {'ok':True,'source_retired':True,'target_reverse_listener_check_required':True}
        except Exception as error:
            code = str(error) if isinstance(error,Refusal) else 'retirement-outcome-unknown'
            write_new('failure.json',{'ok':False,'reason':code,'at':time.time(),'no_retry':True})
            raise Refusal(code) from None
    finally:
        os.close(fd)


def source_script(action, proof=None):
    """Root orchestrator supplies fresh DNS/target proof; source owns drain proof."""
    need(action in ('sample','inspect','apply'), 'invalid-action')
    raw = pathlib.Path(__file__).read_text()
    return "ns={'__name__':'retire_aws_entry'}\nexec(compile("+repr(raw)+",'retire_aws_entry.py','exec'),ns)\nprint(ns['json'].dumps(ns['remote']("+repr(action)+","+repr(proof)+"),sort_keys=True))\n"


def main():
    print(json.dumps({'offline':True,'production_actions':False,'remote_interface':'source_script(action, proof)'}))


if __name__ == '__main__':
    main()
