#!/usr/bin/env python3
"""Offline source-retirement safety checks; no production access."""
import contextlib
import copy
import importlib.util
import io
import pathlib
import shlex
import tempfile
import json
import unittest
from unittest import mock

P=pathlib.Path(__file__).with_name('retire_aws_entry.py')
S=importlib.util.spec_from_file_location('retire_aws_entry',P)
M=importlib.util.module_from_spec(S); S.loader.exec_module(M)


def good():
    return {'state':{'phase':'COMPLETE_WITH_WAIT_BREACH','pg':'TENCENT','redis':'TENCENT','window':None},
      'sockets':[],'boot':'boot1','monotonic':100.,'shared_identity':{'caddy':['1284574','a']},
      'channel_identity':['3273896','b'],
      'containers':{n:{'id':cid,'running':False,'pid':0,'oom':False,'restart':'no','exit':127 if n=='sub2api-redis' else 0}
                    for n,cid in M.CONTAINERS.items()},
      'units':{n:{'ActiveState':'active','SubState':'running','MainPID':str(pid),'Job':''} for n,pid in M.SHARED.items()}}


def proof():
    return {'observed_at':100,'dns_address':M.TARGET,'target_ready':True,'pg_authority':'TENCENT','redis_authority':'TENCENT',
            'runtime_endpoints':{'postgres':'target','redis':'target'},'persisted_endpoints':{'postgres':'target','redis':'target'}}


def nat():
    return '-P OUTPUT ACCEPT\n-A OUTPUT -d 127.0.0.1/32 -p tcp -m tcp --dport 8081 -m owner --uid-owner 997 -m conntrack --ctstate NEW -m comment --comment '+M.COMMENT+' -j REDIRECT --to-ports 18081\n-A POSTROUTING -s 172.18.0.0/16 -j MASQUERADE\n'


def filters():
    return '-P INPUT ACCEPT\n-A INPUT -s 172.18.0.2/32 -d 172.18.0.1/32 -i br-abc -p tcp -m tcp --dport 26379 -m comment --comment '+M.EXT+':redis-allow -j ACCEPT\n-A INPUT -d 172.18.0.1/32 -p tcp -m tcp --dport 26379 -m comment --comment '+M.EXT+':redis-deny -j REJECT --reject-with tcp-reset\n-A INPUT -p tcp --dport 22 -j ACCEPT\n'


class RetirementTests(unittest.TestCase):
    def test_default_offline(self):
        with mock.patch.object(M,'command',side_effect=AssertionError('no command')),contextlib.redirect_stdout(io.StringIO()) as out:
            M.main()
        self.assertIn('"production_actions": false',out.getvalue())

    def test_script_generation_does_not_connect(self):
        with mock.patch.object(M,'remote',side_effect=AssertionError('no apply')):
            script=M.source_script('sample')
        compile(script,'generated','exec')

    def test_conservative_socket_states(self):
        for state in ('ESTAB','CLOSE-WAIT','FIN-WAIT-1','FIN-WAIT-2','LAST-ACK','SYN-SENT','SYN-RECV','CLOSING'):
            with self.subTest(state=state):
                self.assertEqual(len(M.sockets(state+' 0 0 127.0.0.1:18081 127.0.0.1:43210')),1)

    def test_non_live_socket_states(self):
        for state in ('LISTEN','TIME-WAIT','CLOSED'):
            self.assertEqual(M.sockets(state+' 0 0 127.0.0.1:18081 127.0.0.1:43210'),[])

    def test_caddy_original_upstream_counted(self):
        self.assertTrue(M.sockets('ESTAB 0 0 127.0.0.1:43210 127.0.0.1:8081'))

    def test_shared_public_https_ignored(self):
        self.assertEqual(M.sockets('ESTAB 0 0 10.0.0.1:443 1.2.3.4:43210'),[])

    def test_all_data_forward_directions_counted(self):
        for port in (18092,25432,26379,15432,16379):
            for pair in [('127.0.0.1:'+str(port),'127.0.0.1:43210'),('127.0.0.1:43210','127.0.0.1:'+str(port))]:
                self.assertTrue(M.sockets('ESTAB 0 0 '+' '.join(pair)))

    def test_malformed_sockets_fail_closed(self):
        with self.assertRaises(M.Refusal):M.sockets('bad row')

    def test_firewall_plans_exact_and_leave_unrelated_rules(self):
        for raw,kind in [(nat(),'nat'),(filters(),'filter')]:
            plan=M.firewall_plan(raw,kind)
            remaining=[line for line in raw.splitlines() if line not in [x[0] for x in plan]]
            self.assertEqual(remaining, ['-P OUTPUT ACCEPT','-A POSTROUTING -s 172.18.0.0/16 -j MASQUERADE'] if kind=='nat' else ['-P INPUT ACCEPT','-A INPUT -p tcp --dport 22 -j ACCEPT'])

    def test_ambiguous_nat_comment_refused(self):
        for raw in [nat()+nat().splitlines()[1]+'\n',nat().replace('--uid-owner 997','--uid-owner 0'),nat().replace('--comment '+M.COMMENT,'--comment other --comment '+M.COMMENT)]:
            with self.assertRaises(M.Refusal):M.firewall_plan(raw,'nat')

    def test_missing_or_modified_filter_refused(self):
        for raw in [filters().replace('26379','6379'),filters().replace(M.EXT+':redis-deny',M.EXT+':redis-other'),filters().splitlines()[0]+'\n']:
            with self.assertRaises(M.Refusal):M.firewall_plan(raw,'filter')

    def test_valid_source_retirement(self):
        self.assertIs(M.validate_snapshot(good())['state']['window'],None)

    def test_active_bridge_blocks_before_mutation(self):
        value=good();value['sockets']=[{'state':'ESTAB'}]
        with self.assertRaisesRegex(M.Refusal,'active-source-forward'):M.validate_snapshot(value)

    def test_authority_or_window_blocks(self):
        for field,value in [('phase','COMPLETE'),('pg','AWS'),('redis','AWS'),('window',{'kind':'combined'})]:
            row=good();row['state'][field]=value
            with self.assertRaises(M.Refusal):M.validate_snapshot(row)

    def test_source_containers_must_remain_exactly_retired(self):
        for name in M.CONTAINERS:
            for field,value in [('running',True),('restart','always'),('id','x'*64),('pid',123),('oom',True),('exit',9)]:
                row=good();row['containers'][name][field]=value
                with self.assertRaises(M.Refusal):M.validate_snapshot(row)

    def test_shared_services_pinned(self):
        for name in M.SHARED:
            row=good();row['units'][name]['MainPID']='1'
            with self.assertRaises(M.Refusal):M.validate_snapshot(row)

    def test_exact_stable_window_and_freshness(self):
        current=good();current['monotonic']=165
        samples=[]
        for stamp in (100,130,160):
            row=good();row['monotonic']=stamp;samples.append(row)
        M.stable(samples,current)
        for changed in [samples[:2],[samples[0],samples[0],samples[2]]]:
            with self.assertRaises(M.Refusal):M.stable(changed,current)
        stale=copy.deepcopy(current);stale['monotonic']=171
        with self.assertRaises(M.Refusal):M.stable(samples,stale)

    def test_identity_changes_or_non_idle_sample_blocks(self):
        for field,value in [('boot','boot2'),('channel_identity',['old','ticks']),('shared_identity',{}),('sockets',[{'state':'ESTAB'}])]:
            samples=[]
            for stamp in (100,130,160):
                row=good();row['monotonic']=stamp;samples.append(row)
            samples[1][field]=value;current=good();current['monotonic']=165
            with self.assertRaises(M.Refusal):M.stable(samples,current)

    def test_fresh_target_and_dns_proof(self):
        M.validate_proof(proof(),110)
        for field,value in [('observed_at',0),('dns_address','13.231.143.136'),('target_ready',False),('pg_authority','AWS'),('redis_authority','AWS'),('runtime_endpoints',{'postgres':'source','redis':'source'}),('persisted_endpoints',{})]:
            row=proof();row[field]=value
            with self.assertRaises(M.Refusal):M.validate_proof(row,110)


    def orchestrate(self, folder, fail=False):
        scope=folder/'retire';scope.mkdir(mode=0o700)
        for index,stamp in enumerate((40,70,100)):
            row=good();row['monotonic']=stamp
            (scope/('sample-%06d.json'%index)).write_text(json.dumps(row))
        current=good();current['monotonic']=105
        rules={'nat':nat(),'filter':filters()};calls=[]
        def fake_command(argv):
            calls.append(argv)
            if argv[0]=='/usr/bin/systemctl':
                if fail:raise M.Refusal('injected-stop-unknown')
                return ''
            kind=argv[argv.index('-t')+1]
            if argv[-1]=='-S':return rules[kind]
            self.assertIn('-D',argv)
            tokens=['-A']+argv[argv.index('-D')+1:]
            matched=[x for x in rules[kind].splitlines() if shlex.split(x)==tokens]
            self.assertEqual(len(matched),1)
            rules[kind]='\n'.join(x for x in rules[kind].splitlines() if x!=matched[0])+'\n'
            return ''
        def fake_private(path):
            return path.read_text() if path.parent==scope else '[Unit]\nowned-unit-body\n'
        original_need=M.need
        with mock.patch.object(M,'need',side_effect=lambda ok,code:original_need(ok or code=='private-scope-required',code)), \
             mock.patch.object(M,'ROOT',folder),mock.patch.object(M,'SCOPE',scope), \
             mock.patch.object(M.os,'geteuid',return_value=0), \
             mock.patch.object(M,'snapshot',return_value=current), \
             mock.patch.object(M,'private',side_effect=fake_private), \
             mock.patch.object(M,'command',side_effect=fake_command), \
             mock.patch.object(M,'stopped'),mock.patch.object(M.time,'time',return_value=110):
            if fail:
                with self.assertRaisesRegex(M.Refusal,'injected-stop-unknown'):M.remote('apply',proof())
            else:
                self.assertTrue(M.remote('apply',proof())['ok'])
            count=len(calls)
            with self.assertRaisesRegex(M.Refusal,'existing-intent-refuse-replay'):M.remote('apply',proof())
            self.assertEqual(len(calls),count)
        return scope,calls,rules

    def test_one_shot_success_preserves_unrelated_firewall_and_shared_services(self):
        with tempfile.TemporaryDirectory() as temp:
            scope,calls,rules=self.orchestrate(pathlib.Path(temp))
            self.assertTrue((scope/'intent.json').exists())
            self.assertTrue((scope/'before.json').exists())
            self.assertTrue((scope/'result.json').exists())
            self.assertIn('MASQUERADE',rules['nat']);self.assertIn('--dport 22',rules['filter'])
            mutations=[x for x in calls if x[0]=='/usr/bin/systemctl']
            self.assertEqual(mutations,[['/usr/bin/systemctl','disable','--now',M.GUARD,M.RUNNER],
                                        ['/usr/bin/systemctl','disable','--now',M.CHANNEL]])
            self.assertEqual(sum('-D' in x for x in calls),3)

    def test_unknown_stop_is_durable_and_never_replayed(self):
        with tempfile.TemporaryDirectory() as temp:
            scope,calls,rules=self.orchestrate(pathlib.Path(temp),fail=True)
            self.assertTrue((scope/'intent.json').exists())
            self.assertTrue((scope/'failure.json').exists())
            self.assertFalse((scope/'result.json').exists())
            self.assertEqual(sum('-D' in x for x in calls),0)
            self.assertEqual(rules,{'nat':nat(),'filter':filters()})


if __name__ == '__main__':unittest.main()
